"""Tests for the Rada open data client.

Fixtures under ``tests/fixtures/rada_*`` are cut verbatim from live responses
captured 2026-09-03; the header of each file records the exact request. Network
is mocked here (Constitution V) — the live evidence lives in ticket 05.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from registries import rada_open_data  # noqa: E402
from registries.rada_open_data import (  # noqa: E402
    STATUS_IN_FORCE,
    TYPE_CODE,
    TYPE_LAW,
    RadaOpenDataClient,
    RadaOpenDataError,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _response(
    status_code: int = 200,
    content: bytes = b"",
    headers: dict[str, str] | None = None,
) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.content = content
    response.headers = headers or {}
    return response


class _Recorder:
    """Records every outgoing call and hands back queued responses."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.queue: list[MagicMock] = []

    def __call__(self, method: str, url: str, **kwargs: Any) -> MagicMock:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.queue:
            raise AssertionError(f"unexpected request to {url}")
        return self.queue.pop(0)

    def __len__(self) -> int:
        return len(self.calls)

    def __getitem__(self, index: int) -> dict[str, Any]:
        return self.calls[index]


@pytest.fixture
def patched_request(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(rada_open_data.requests, "request", recorder)
    return recorder


def _queue(recorder: _Recorder, *responses: MagicMock) -> None:
    recorder.queue.extend(responses)


# ---------------------------------------------------------------- channel 1


def test_fetch_document_returns_text_and_uses_the_open_data_user_agent(
    tmp_path: Path, patched_request: _Recorder
) -> None:
    body = (FIXTURES / "rada_document.html").read_bytes()
    _queue(patched_request, _response(200, body))

    client = RadaOpenDataClient(tmp_path)
    result = client.fetch_document("2210-14")

    assert result["source"] == "rada_open_data"
    assert result["char_count"] > 500
    assert "data.rada.gov.ua/laws/show/2210-14" in result["url"]
    call = patched_request[0]
    assert call["headers"]["User-Agent"] == "OpenData"
    # Following the redirect lands on the public site's 34 KB shell, which looks
    # like a success and is not one.
    assert call["allow_redirects"] is False


def test_fetch_document_treats_a_redirect_as_failure(
    tmp_path: Path, patched_request: _Recorder
) -> None:
    _queue(
        patched_request,
        _response(302, b"", {"Location": "https://zakon.rada.gov.ua/laws/show/922-19"}),
    )

    client = RadaOpenDataClient(tmp_path)
    with pytest.raises(RadaOpenDataError, match="User-Agent"):
        client.fetch_document("922-19")


def test_fetch_document_rejects_a_too_short_body(
    tmp_path: Path, patched_request: _Recorder
) -> None:
    _queue(patched_request, _response(200, b"<html><body>nope</body></html>"))

    client = RadaOpenDataClient(tmp_path)
    with pytest.raises(RadaOpenDataError, match="chars"):
        client.fetch_document("922-19")


def test_recent_changes_reads_the_list_key(tmp_path: Path, patched_request: _Recorder) -> None:
    payload = {
        "cnt": 401,
        "from": 1,
        "list": [
            {
                "nreg": "n0352500-26",
                "nazva": "Про облікову ціну банківських металів",
                "typ": 95,
                "orgdat": 20260903,
            }
        ],
    }
    _queue(patched_request, _response(200, json.dumps(payload).encode("utf-8")))

    client = RadaOpenDataClient(tmp_path)
    result = client.recent_changes(max_results=5)

    assert result["total"] == 401
    assert result["results"][0]["law_id"] == "n0352500-26"
    assert result["results"][0]["changed_at"] == "20260903"


def test_dump_freshness_asks_with_head_and_compares_timestamps(
    tmp_path: Path, patched_request: _Recorder
) -> None:
    _queue(
        patched_request,
        _response(
            200,
            b"",
            {"Last-Modified": "Thu, 03 Sep 2026 08:58:06 GMT", "Content-Length": "46700037"},
        ),
    )

    client = RadaOpenDataClient(tmp_path)
    result = client.dump_freshness()

    assert patched_request[0]["method"] == "HEAD"
    assert result["remote_bytes"] == 46700037
    assert result["local_exists"] is False
    assert result["stale"] is True


# ---------------------------------------------------------------- channel 2


def test_search_parses_the_results_page(tmp_path: Path, patched_request: _Recorder) -> None:
    page = (FIXTURES / "rada_search_page.html").read_bytes()
    _queue(patched_request, _response(200, page))

    client = RadaOpenDataClient(tmp_path)
    result = client.search(
        "шахрайство",
        types=(TYPE_LAW, TYPE_CODE),
        status=STATUS_IN_FORCE,
        use_cache=False,
    )

    # 47 is the count block, not the 294280 of the <h1> collection title.
    assert result["found"] == 47
    assert result["format"] == "page"
    assert result["token"] == "u67f0cfba-bfed-46db-88e2-eec894fb6e5e"
    assert result["results"][0]["law_id"] == "4619-20"
    assert result["results"][0]["status"] == "Чинний"
    assert "Про внесення змін" in result["results"][0]["title"]


def test_search_repeats_the_type_parameter(tmp_path: Path, patched_request: _Recorder) -> None:
    page = (FIXTURES / "rada_search_page.html").read_bytes()
    _queue(patched_request, _response(200, page))

    client = RadaOpenDataClient(tmp_path)
    client.search("шахрайство", types=(TYPE_LAW, TYPE_CODE), use_cache=False)

    params = patched_request[0]["params"]
    # A comma-joined "1,21" is accepted and silently ignored: 563 hits against 47.
    assert params["typs"] == ["1", "21"]
    assert patched_request[0]["headers"]["User-Agent"].startswith("Mozilla/5.0")


def test_search_parses_the_redirect_shortcut(tmp_path: Path, patched_request: _Recorder) -> None:
    _queue(patched_request, _response(302, b"", {"Location": "/laws/main/161-14"}))

    client = RadaOpenDataClient(tmp_path)
    result = client.search("оренда землі", textl="1", use_cache=False)

    assert result["format"] == "redirect"
    assert [row["law_id"] for row in result["results"]] == ["161-14"]


def test_search_decodes_ids_from_a_multi_id_redirect(
    tmp_path: Path, patched_request: _Recorder
) -> None:
    _queue(
        patched_request,
        _response(302, b"", {"Location": "/laws/main/922-19,254%D0%BA/96-%D0%B2%D1%80"}),
    )

    client = RadaOpenDataClient(tmp_path)
    result = client.search("закупівлі", textl="1", use_cache=False)

    assert [row["law_id"] for row in result["results"]] == ["922-19", "254к/96-вр"]


def test_search_does_not_retry_a_403(tmp_path: Path, patched_request: _Recorder) -> None:
    _queue(patched_request, _response(403, b""))

    client = RadaOpenDataClient(tmp_path, sleeper=lambda _: None)
    with pytest.raises(RadaOpenDataError, match="User-Agent profile"):
        client.search("шахрайство", use_cache=False)
    assert len(patched_request) == 1


def test_search_rejects_a_query_shorter_than_rada_accepts(tmp_path: Path) -> None:
    client = RadaOpenDataClient(tmp_path)
    with pytest.raises(RadaOpenDataError, match="3 characters"):
        client.search("ко")


def test_search_serves_the_second_call_from_cache(
    tmp_path: Path, patched_request: _Recorder
) -> None:
    page = (FIXTURES / "rada_search_page.html").read_bytes()
    _queue(patched_request, _response(200, page))

    client = RadaOpenDataClient(tmp_path)
    first = client.search("шахрайство", types=(TYPE_LAW,))
    second = client.search("шахрайство", types=(TYPE_LAW,))

    assert first["from_cache"] is False
    assert second["from_cache"] is True
    assert second["found"] == first["found"]
    assert len(patched_request) == 1


# ---------------------------------------------------------------- plumbing


def test_a_dropped_connection_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    page = (FIXTURES / "rada_search_page.html").read_bytes()
    attempts: list[int] = []

    def flaky(method: str, url: str, **kwargs: Any) -> MagicMock:
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("UNEXPECTED_EOF_WHILE_READING")
        return _response(200, page)

    monkeypatch.setattr(rada_open_data.requests, "request", flaky)

    client = RadaOpenDataClient(tmp_path, sleeper=lambda _: None)
    result = client.search("шахрайство", types=(TYPE_LAW,), use_cache=False)

    assert len(attempts) == 3
    assert result["found"] == 47


def test_giving_up_after_the_last_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def always_fails(method: str, url: str, **kwargs: Any) -> MagicMock:
        raise ConnectionError("UNEXPECTED_EOF_WHILE_READING")

    monkeypatch.setattr(rada_open_data.requests, "request", always_fails)

    client = RadaOpenDataClient(tmp_path, attempts=2, sleeper=lambda _: None)
    with pytest.raises(RadaOpenDataError, match="after 2 attempts"):
        client.fetch_document("922-19")
