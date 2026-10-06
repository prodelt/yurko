"""T149 — судові рішення як доказ: що підтверджує цитату, а що ні.

Мережа мокнута (принцип V): зразки розмітки взяті з живих проб 2026-09-07 і
скорочені до структурно значущого мінімуму. Тест перевіряє не розбір як такий, а
межу доказовості: **справжній текст рішення підтверджує цитату, а картка, резюме
й метадані — ні**, і хибний локатор не підмінюється сусіднім.

Розподіл між сусідніми файлами: розбір ЄСПЛ по кроках — `test_echr.py`,
процедура Суду ЄС — `test_cjeu_public_procedure.py`; тут — Міжнародний суд ООН.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.provenance import PublicationKind  # noqa: E402
from sources.base import AdapterPayload  # noqa: E402
from sources.icj import IcjAdapter  # noqa: E402

# --- Зразки розмітки, скорочені з живих проб --------------------------------

ICJ_CARD_HTML = """<html><body>
  <a href="/sites/default/files/case-related/70/070-19860627-JUD-01-00-EN.pdf">Judgment EN</a>
  <a href="/sites/default/files/case-related/70/070-19860627-JUD-01-00-FR.pdf">Arrêt FR</a>
</body></html>"""


class _Response:
    def __init__(self, *, status_code: int = 200, text: str = "", content: bytes = b"") -> None:
        self.status_code = status_code
        self.text = text
        self.content = content or text.encode("utf-8")
        self.headers: dict[str, str] = {}
        self.url = "https://example.invalid/"

    def json(self) -> Any:  # pragma: no cover - не використовується цими тестами
        raise ValueError("not json")


class _Session:
    """Черга відповідей; фіксує кожен запит, щоб перевірити, що пішло в мережу."""

    def __init__(self, *responses: Any) -> None:
        self._queue = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> Any:
        self.calls.append({"url": url, **kwargs})
        if not self._queue:
            raise AssertionError(f"незапланований запит: {url}")
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# ---------------------------------------------------------------------------
# ICJ: захист не обходиться, відмова називає ручний шлях
# ---------------------------------------------------------------------------


def test_icj_judgment_behind_a_challenge_refuses_with_a_manual_path() -> None:
    challenge = _Response(status_code=403, text="<html><title>Just a moment...</title></html>")
    adapter = IcjAdapter(session=_Session(challenge))

    result = adapter.fetch("070-19860627-JUD-01-00-EN", path="78")

    assert not isinstance(result, AdapterPayload), "текст не вигадується замість відмови"
    payload = result.as_output()
    assert payload["code"] in ("not_covered", "source_unavailable")
    details = payload.get("details", {})
    assert details.get("manual_path") or payload.get("manual_path")


def test_icj_card_is_readable_but_confirms_nothing() -> None:
    adapter = IcjAdapter(session=_Session(_Response(text=ICJ_CARD_HTML)))

    result = adapter.card("70")

    if isinstance(result, AdapterPayload):
        assert result.provenance.confirmable is False
        assert result.provenance.publication_kind is PublicationKind.CARD
    else:  # картка теж може бути недоступна — тоді це чесна відмова, не текст
        assert result.as_output()["code"] in ("not_covered", "source_unavailable", "not_found")


def test_icj_public_card_matches_the_get_case_contract() -> None:
    """T-icj-card (2026-09-09): ``get_case`` кличе саме ``public_card``.

    Реєстр раніше ніс лише ``Operation.CARD`` (для ``get_law_metadata``),
    тоді як ``get_case`` завжди маршрутизує ``public_case_card`` і шукає
    метод ``public_card`` — точнісінько той розрив, який дав
    ``get_case("ICJ", <номер>)`` → ``not_covered`` попри те, що картка
    читається. ``public_card`` — тонкий переклад форми над ``card()``, і
    цей тест ловить розбіжність форми (``court``/``published_status``/
    ``public_documents``), а не самого запиту.
    """
    adapter = IcjAdapter(session=_Session(_Response(text=ICJ_CARD_HTML)))

    result = adapter.public_card("70")

    assert isinstance(result, AdapterPayload)
    assert result.data["case_number"] == "70"
    assert result.data["court"]
    assert result.data["published_status"] == "published"
    assert result.data["published"] is True
    assert result.data["public_documents"]
    assert result.data["public_documents"][0]["document_id"] == "070-19860627-JUD-01-00-EN"
    assert result.provenance.confirmable is False
    assert result.provenance.publication_kind is PublicationKind.CARD
