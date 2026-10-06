from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.contracts import SourceAdapterError
from registries.court_registry import CourtDecisionsRegistry

SEARCH_HTML = """
<html><body><table>
<tr><td><a href="/Review/123456789">Рішення</a></td>
<td>Справа № 910/12/24</td><td>Господарське</td><td>01.06.2026</td></tr>
<tr><td><a href="/Review/987654321">Постанова</a></td>
<td>Справа № 757/45/25</td><td>Цивільне</td><td>02.06.2026</td></tr>
</table></body></html>
"""

DECISION_HTML = (
    "<html><body><div>"
    "РІШЕННЯ ІМЕНЕМ УКРАЇНИ. Справа № 910/12/24. "
    "Господарський суд міста Києва розглянув справу про стягнення заборгованості. "
    + "Суд встановив наступне. " * 30
    + "</div></body></html>"
)

CAPTCHA_HTML = """
<html><body>
<div>Документів у системі: 1 3 7 2 7 7 2 1 7</div>
<form>
<div>Логін: <input type="text" name="login"></div>
<div>Пароль: <input type="password" name="pass"></div>
<p>З метою упередження перешкоджанню стабільній роботі Реєстру шляхом здійснення
автоматичних або автоматизованих запитів на пошук та копіювання ("викачування") бази
даних, запроваджено інтерактивний елемент захисту системи. Введіть суму цифр,
зображених на малюнку:</p>
<input type="text" name="captcha">
<button>Перевірити</button>
<button>Закрити</button>
</form>
</body></html>
"""


def _response(text: str) -> MagicMock:
    response = MagicMock()
    response.text = text
    response.raise_for_status.return_value = None
    return response


@pytest.fixture()
def session() -> MagicMock:
    session = MagicMock()
    session.post.return_value = _response(SEARCH_HTML)
    session.get.return_value = _response(DECISION_HTML)
    return session


@pytest.fixture()
def registry(tmp_path: Path, session: MagicMock) -> CourtDecisionsRegistry:
    return CourtDecisionsRegistry(tmp_path, session=session)


def test_search_parses_review_links(registry: CourtDecisionsRegistry) -> None:
    result = registry.search("стягнення заборгованості", max_results=10)

    assert result["found"] == 2
    first = result["results"][0]
    assert first["decision_id"] == "123456789"
    assert first["url"].endswith("/Review/123456789")
    assert "910/12/24" in first["snippet"]


def test_search_respects_max_results(registry: CourtDecisionsRegistry) -> None:
    result = registry.search("заборгованість", max_results=1)

    assert result["found"] == 1


def test_search_posts_query_and_case_number(
    registry: CourtDecisionsRegistry, session: MagicMock
) -> None:
    registry.search("позов", case_number="910/12/24", max_results=5)

    payload = session.post.call_args.kwargs["data"]
    assert payload["SearchExpression"] == "позов"
    assert payload["CaseNumber"] == "910/12/24"


def test_search_wraps_network_errors_and_backs_off(
    registry: CourtDecisionsRegistry, session: MagicMock
) -> None:
    session.post.side_effect = RuntimeError("network down")

    with pytest.raises(SourceAdapterError):
        registry.search("позов")

    # second call short-circuits via backoff without touching the network
    session.post.reset_mock()
    with pytest.raises(SourceAdapterError):
        registry.search("позов")
    session.post.assert_not_called()


def test_get_decision_fetches_and_caches(
    registry: CourtDecisionsRegistry, session: MagicMock, tmp_path: Path
) -> None:
    decision = registry.get_decision("123456789")

    assert decision["from_cache"] is False
    assert "РІШЕННЯ ІМЕНЕМ УКРАЇНИ" in decision["text"]
    assert (tmp_path / "123456789.json").exists()

    session.get.reset_mock()
    cached = registry.get_decision("123456789")
    assert cached["from_cache"] is True
    session.get.assert_not_called()


def test_get_decision_rejects_invalid_id(registry: CourtDecisionsRegistry) -> None:
    with pytest.raises(ValueError):
        registry.get_decision("../etc/passwd")


def test_get_decision_rejects_stub_pages(
    registry: CourtDecisionsRegistry, session: MagicMock
) -> None:
    session.get.return_value = _response("<html><body>404</body></html>")

    with pytest.raises(SourceAdapterError):
        registry.get_decision("111")


def test_get_decision_ignores_corrupt_cache(
    registry: CourtDecisionsRegistry, tmp_path: Path
) -> None:
    (tmp_path / "123456789.json").write_text("{not json", encoding="utf-8")

    decision = registry.get_decision("123456789")

    assert decision["from_cache"] is False
    assert json.loads((tmp_path / "123456789.json").read_text(encoding="utf-8"))


def test_health_reports_base_url_and_cache(registry: CourtDecisionsRegistry) -> None:
    registry.get_decision("123456789")

    health = registry.health()
    assert health.ok is True
    assert health.adapter == "court_decisions"
    assert health.details["cached_decisions"] == 1


def test_base_url_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COURT_REGISTRY_URL", "https://mirror.example/")

    registry = CourtDecisionsRegistry(tmp_path, session=MagicMock())

    assert registry.health().details["base_url"] == "https://mirror.example"


def test_search_detects_captcha_page(registry: CourtDecisionsRegistry, session: MagicMock) -> None:
    session.post.return_value = _response(CAPTCHA_HTML)

    with pytest.raises(SourceAdapterError) as exc_info:
        registry.search("позов")

    assert "antibot protection" in str(exc_info.value)

    # second call short-circuits via backoff
    session.post.reset_mock()
    with pytest.raises(SourceAdapterError):
        registry.search("позов")
    session.post.assert_not_called()


def test_get_decision_detects_captcha_page(
    registry: CourtDecisionsRegistry, session: MagicMock
) -> None:
    session.get.return_value = _response(CAPTCHA_HTML)

    with pytest.raises(SourceAdapterError) as exc_info:
        registry.get_decision("123456789")

    assert "antibot protection" in str(exc_info.value)

    # second call short-circuits via backoff
    session.get.reset_mock()
    with pytest.raises(SourceAdapterError):
        registry.get_decision("123456789")
    session.get.assert_not_called()
