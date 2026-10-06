from __future__ import annotations

import io
import os
import sys
import time
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from registries.open_data_discovery import OpenDataDiscovery, OpenDataUnavailable


def _zip_doc(lines: list[str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        payload = "\n".join(lines).encode("windows-1251")
        archive.writestr("doc.txt", payload)
    return buffer.getvalue()


def test_open_data_discovery_finds_law_by_title(tmp_path: Path) -> None:
    archive = _zip_doc(
        [
            "1\t361-20\tПро медіа\t0\t95\t70:20221213:\t\t2\t20221213",
            "2\t922-19\tПро публічні закупівлі\t0\t95\t70:20151225:\t\t2\t20151225",
        ]
    )
    response = MagicMock()
    response.content = archive
    response.raise_for_status.return_value = None
    http = MagicMock(return_value=response)

    original_get = _patched_get(http)
    try:
        discovery = OpenDataDiscovery(tmp_path)
        result = discovery.search("публічні закупівлі", max_results=5)
    finally:
        _restore_get(original_get)

    assert result["found"] == 1
    assert result["results"][0]["law_id"] == "922-19"
    assert result["results"][0]["url"] == "https://zakon.rada.gov.ua/laws/show/922-19"
    assert result["results"][0]["accepted_at"] == "20151225"
    assert result["results"][0]["updated_at"] == "20151225"
    # Каталог щойно завантажено — це не кеш, і ``freshness_notice`` мовчить.
    assert result["from_cache"] is False
    assert result["catalogue_freshness"] == "live"
    assert result["freshness_notice"] == ""


def test_open_data_discovery_resolves_confident_title_match(tmp_path: Path) -> None:
    archive = _zip_doc(
        [
            "1\t2849-20\tПро медіа\t0\t95\t1:20221213:2849-IX\t\t17\t20260423",
            "2\t922-19\tПро публічні закупівлі\t0\t95\t1:20151225:922-VIII\t\t17\t20260423",
        ]
    )
    response = MagicMock()
    response.content = archive
    response.raise_for_status.return_value = None
    http = MagicMock(return_value=response)

    original_get = _patched_get(http)
    try:
        discovery = OpenDataDiscovery(tmp_path)
        result = discovery.resolve_best("Про медіа")
    finally:
        _restore_get(original_get)

    assert result is not None
    assert result["law_id"] == "2849-20"


def test_open_data_discovery_rejects_weak_single_token_match(tmp_path: Path) -> None:
    archive = _zip_doc(
        [
            "1\t2849-20\tПро медіа\t0\t95\t1:20221213:2849-IX\t\t17\t20260423",
            "2\t922-19\tПро публічні закупівлі\t0\t95\t1:20151225:922-VIII\t\t17\t20260423",
        ]
    )
    response = MagicMock()
    response.content = archive
    response.raise_for_status.return_value = None
    http = MagicMock(return_value=response)

    original_get = _patched_get(http)
    try:
        discovery = OpenDataDiscovery(tmp_path)
        result = discovery.resolve_best("Про")
    finally:
        _restore_get(original_get)

    assert result is None


def test_open_data_discovery_asks_the_open_data_host_with_the_open_data_user_agent(
    tmp_path: Path,
) -> None:
    response = MagicMock()
    response.content = _zip_doc(["1\t922-19\tПро публічні закупівлі\t0\t95\t1:20151225:922-VIII"])
    response.raise_for_status.return_value = None
    http = MagicMock(return_value=response)

    original_get = _patched_get(http)
    try:
        OpenDataDiscovery(tmp_path).search("публічні закупівлі", max_results=5)
    finally:
        _restore_get(original_get)

    url = http.call_args.args[0]
    headers = http.call_args.kwargs["headers"]
    assert url.startswith("https://data.rada.gov.ua/")
    # data.rada відповідає лише на точний рядок "OpenData"; будь-який інший —
    # 302 на zakon.rada, де robots.txt має "User-Agent: * Disallow: /".
    assert headers["User-Agent"] == "OpenData"
    assert http.call_args.kwargs["allow_redirects"] is False


def test_open_data_discovery_retries_a_dropped_handshake(tmp_path: Path) -> None:
    response = MagicMock()
    response.content = _zip_doc(["1\t922-19\tПро публічні закупівлі\t0\t95\t1:20151225:922-VIII"])
    response.raise_for_status.return_value = None
    http = MagicMock(side_effect=[requests.exceptions.SSLError("handshake"), response])

    original_get = _patched_get(http)
    try:
        discovery = OpenDataDiscovery(tmp_path, sleeper=lambda _: None)
        result = discovery.search("публічні закупівлі", max_results=5)
    finally:
        _restore_get(original_get)

    assert http.call_count == 2
    assert result["found"] == 1


def _aged_archive(tmp_path: Path, *, days: int) -> None:
    """Каталог карток на диску, останній раз завантажений ``days`` днів тому."""
    archive = tmp_path / "doc.zip"
    archive.write_bytes(_zip_doc(["1\t922-19\tПро публічні закупівлі\t0\t95\t1:20151225:922-VIII"]))
    moment = time.time() - 60 * 60 * 24 * days
    os.utime(archive, (moment, moment))


def _patched_get(http: MagicMock) -> Any:
    """Підмінити сам вихід у мережу, лишивши повтори каталогу на місці.

    Відколи каталог ходить через :class:`SourceTransport` (тікет 27),
    підміняти ``requests.get`` немає сенсу — сесія його не кличе. Підміна
    стоїть на ``SourceTransport.get``, тому аргументи в ``http.call_args``
    ті самі, що були, а власний цикл спроб ``OpenDataDiscovery`` перевіряється
    як і раніше.
    """
    from sources.transport import SourceTransport

    original = SourceTransport.get
    SourceTransport.get = http  # type: ignore[method-assign]
    return original


def _restore_get(original: Any) -> None:
    from sources.transport import SourceTransport

    SourceTransport.get = original  # type: ignore[method-assign]


def test_a_stale_archive_is_served_but_never_passed_off_as_just_fetched(
    tmp_path: Path,
) -> None:
    """Рев'ю тікета 15: застарілий каталог — відповідь, але з названим віком.

    Сценарій, який тут ловиться: data.rada лежить тиждень, а ``discover_laws``
    віддає каталог місячної давнини під сьогоднішнім ``retrieved_at``. Юрист
    читає це як «перевірено щойно» й не знає, що закону, ухваленого минулого
    тижня, у відповіді не могло бути взагалі.
    """
    _aged_archive(tmp_path, days=30)
    http = MagicMock(side_effect=requests.exceptions.SSLError("handshake"))

    original = _patched_get(http)
    try:
        discovery = OpenDataDiscovery(tmp_path, attempts=2, sleeper=lambda _: None)
        result = discovery.search("публічні закупівлі", max_results=5)
    finally:
        _restore_get(original)

    assert result["found"] == 1
    assert result["catalogue_freshness"] == "stale"
    # Вік каталогу названо датою, а не самим лише прапорцем.
    assert result["catalogue_fetched_at"] < result["retrieved_at"]
    assert result["stale_since"] == result["catalogue_fetched_at"]
    assert "застар" in result["freshness_notice"].lower()
    assert result["catalogue_fetched_at"][:4] in result["freshness_notice"]


def test_the_three_states_of_the_catalogue_are_told_apart(tmp_path: Path) -> None:
    """``from_cache`` означало три різні речі й завжди було ``True``."""
    body = MagicMock()
    body.content = _zip_doc(["1\t922-19\tПро публічні закупівлі\t0\t95\t1:20151225:922-VIII"])
    body.raise_for_status.return_value = None
    body.status_code = 200
    body.url = "https://data.rada.gov.ua/ogd/zak/laws/data/csv/doc.zip"

    original = _patched_get(MagicMock(return_value=body))
    try:
        live = OpenDataDiscovery(tmp_path).search("публічні закупівлі")
        # Другий виклик тим самим каталогом: свіжий кеш, у мережу не йдемо.
        cached = OpenDataDiscovery(tmp_path).search("публічні закупівлі")
    finally:
        _restore_get(original)

    assert live["catalogue_freshness"] == "live"
    assert live["from_cache"] is False
    assert cached["catalogue_freshness"] == "cache"
    assert cached["from_cache"] is True

    _aged_archive(tmp_path, days=30)
    original = _patched_get(MagicMock(side_effect=requests.exceptions.SSLError("handshake")))
    try:
        stale = OpenDataDiscovery(tmp_path, attempts=1).search("публічні закупівлі")
    finally:
        _restore_get(original)
    assert stale["catalogue_freshness"] == "stale"
    assert stale["from_cache"] is True


def test_a_truncated_download_does_not_destroy_the_working_catalogue(tmp_path: Path) -> None:
    """Обрізаний архів не заміщає робочий: інакше доба відмов ``BadZipFile``.

    ``os.replace`` атомарний, і саме тому небезпечний: 13,4 МБ, обірвані на
    половині, стають «свіжим» каталогом, після чого ``_archive_is_fresh``
    цілу добу каже «свіжо», а кожен виклик падає на розпакуванні.
    """
    _aged_archive(tmp_path, days=30)
    good = (tmp_path / "doc.zip").read_bytes()

    truncated = MagicMock()
    truncated.content = good[: len(good) // 2]
    truncated.raise_for_status.return_value = None
    truncated.status_code = 200
    truncated.url = "https://data.rada.gov.ua/ogd/zak/laws/data/csv/doc.zip"

    original = _patched_get(MagicMock(return_value=truncated))
    try:
        result = OpenDataDiscovery(tmp_path, attempts=1).search("публічні закупівлі")
    finally:
        _restore_get(original)

    assert (tmp_path / "doc.zip").read_bytes() == good
    assert result["found"] == 1
    assert result["catalogue_freshness"] == "stale"


def test_a_name_that_does_not_resolve_is_not_retried(tmp_path: Path) -> None:
    """Та сама межа, що й у ``sources.transport``: DNS — відповідь, не випадковість."""
    import socket

    _aged_archive(tmp_path, days=3)
    failure = requests.exceptions.ConnectionError("no such host")
    failure.__cause__ = socket.gaierror(11001, "getaddrinfo failed")
    http = MagicMock(side_effect=failure)

    original = _patched_get(http)
    try:
        OpenDataDiscovery(tmp_path, attempts=3, sleeper=lambda _: None).search("закупівлі")
    finally:
        _restore_get(original)

    assert http.call_count == 1


def test_the_whole_download_fits_in_the_declared_budget() -> None:
    """Один виклик MCP не має права блокувати юриста довше за оголошений бюджет."""
    from registries.open_data_discovery import DOWNLOAD_BUDGET_SECONDS, OpenDataDiscovery

    discovery = OpenDataDiscovery.__new__(OpenDataDiscovery)
    assert DOWNLOAD_BUDGET_SECONDS <= 90
    assert discovery.worst_case_seconds(attempts=3, request_timeout=25) <= DOWNLOAD_BUDGET_SECONDS


def test_a_redirect_inside_the_open_data_host_is_followed(tmp_path: Path) -> None:
    """301 у межах data.rada — переїзд файлу, а не відмова від UA OpenData."""
    moved = MagicMock()
    moved.status_code = 301
    moved.headers = {"Location": "https://data.rada.gov.ua/ogd/zak/laws/data/csv/doc2.zip"}
    moved.content = b""

    landed = MagicMock()
    landed.status_code = 200
    landed.content = _zip_doc(["1\t922-19\tПро публічні закупівлі\t0\t95\t1:20151225:922-VIII"])
    landed.raise_for_status.return_value = None
    landed.headers = {}

    http = MagicMock(side_effect=[moved, landed])
    original = _patched_get(http)
    try:
        result = OpenDataDiscovery(tmp_path, attempts=1).search("публічні закупівлі")
    finally:
        _restore_get(original)

    assert result["catalogue_freshness"] == "live"
    assert result["found"] == 1
    assert http.call_args_list[1].args[0].endswith("doc2.zip")


def test_a_redirect_leaving_the_open_data_host_is_refused(tmp_path: Path) -> None:
    """302 на zakon.rada — не переїзд, а «UA OpenData не прийнято»."""
    away = MagicMock()
    away.status_code = 302
    away.headers = {"Location": "https://zakon.rada.gov.ua/laws/main"}
    away.content = b""

    original = _patched_get(MagicMock(return_value=away))
    try:
        with pytest.raises(OpenDataUnavailable, match="zakon.rada.gov.ua"):
            OpenDataDiscovery(tmp_path, attempts=1).search("публічні закупівлі")
    finally:
        _restore_get(original)


def test_server_discover_laws_output_contract(monkeypatch) -> None:
    import server

    monkeypatch.setattr(
        server.open_data_discovery,
        "search",
        lambda query, max_results: {
            "query": query,
            "results": [
                {
                    "law_id": "922-19",
                    "title": "Про публічні закупівлі",
                    "url": "https://zakon.rada.gov.ua/laws/show/922-19",
                    "print_url": "https://zakon.rada.gov.ua/laws/show/922-19/print",
                    "accepted_at": "20151225",
                    "updated_at": "20260423",
                    "source": "rada_open_data_doc_cards",
                }
            ],
            "found": 1,
            "source": "https://data.rada.gov.ua/ogd/zak/laws/data/csv/doc.zip",
            "from_cache": True,
            "retrieved_at": "2026-05-12T00:00:00+00:00",
        },
    )

    result = server.discover_laws("публічні закупівлі", 5)

    assert result["found"] == 1
    assert result["results"][0]["law_id"] == "922-19"


def test_server_discover_laws_says_the_catalogue_is_stale(monkeypatch) -> None:
    """Вік каталогу доходить до юриста, а не гасне в описі каналу.

    ``discover_laws`` дописує речення про застарілу копію до свого пояснення
    про канал, а не затирає його своїм.
    """
    import server

    monkeypatch.setattr(
        server.open_data_discovery,
        "search",
        lambda query, max_results: {
            "query": query,
            "results": [],
            "found": 0,
            "source": "https://data.rada.gov.ua/ogd/zak/laws/data/csv/doc.zip",
            "from_cache": True,
            "catalogue_freshness": "stale",
            "catalogue_fetched_at": "2026-08-12T00:00:00+00:00",
            "stale_since": "2026-08-12T00:00:00+00:00",
            "freshness_notice": (
                "Каталог карток застарілий: завантажити свіжий не вдалося, відповідь "
                "зібрано з копії від 2026-08-12T00:00:00+00:00."
            ),
            "retrieved_at": "2026-09-16T00:00:00+00:00",
        },
    )

    result = server.discover_laws("щось, чого немає", 5)

    assert result["catalogue_freshness"] == "stale"
    assert result["stale_since"] == "2026-08-12T00:00:00+00:00"
    notice = result["freshness_notice"]
    assert "локального каталогу карток" in notice
    assert "застарілий" in notice
    assert "2026-08-12" in notice
