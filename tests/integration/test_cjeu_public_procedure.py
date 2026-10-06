"""T153 (частина джерела) — процедурні акти Суду ЄС і публічна картка (T150–T151).

Мережа тут не використовується: розмітка Статуту (Протокол 3), Регламенту
Суду й Практичних вказівок — згорнуті, але за формою справжні фрагменти,
зняті живим запитом Cellar (research/13-runtime-probes-2026-09-07.md, §A5).
Консолідованих Статуту й Регламенту Суду в Cellar немає (0 рядків за
``act_consolidated_consolidates_resource_legal``/``based_on``) — читання без
``as_of`` повертає редакцію CELEX як є, з попередженням і переліком
змінюючих актів; ``as_of`` для цих документів завжди дає ``revision_unknown``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.contracts import RevisionUnknown  # noqa: E402
from sources.base import AdapterPayload  # noqa: E402
from sources.eu_case_law import EuCaseLawAdapter  # noqa: E402
from sources.eu_law import EuLawAdapter  # noqa: E402

STATUTE_CELEX = "12016E/PRO/03"
ROP_CELEX = "32012Q0929(01)"
PRACTICE_DIRECTIONS_CELEX = "32024Q02173"

#: Статут (Протокол 3): статті без `art_` id — `p.ti-art` + подальші `p.normal`
#: до наступної `p.ti-art` (research/13, §A5, дослівно за формою).
STATUTE_XHTML = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
<p id="d1e48-210-1" class="ti-art">Article 22</p>
<p class="normal">Unrelated text of Article 22.</p>
<p id="d1e267-210-1" class="ti-art">Article 23</p>
<p class="normal">In the cases governed by Article 267 of the Treaty on the Functioning of
the European Union, the decision of the court or tribunal of a Member State which
suspends its proceedings and refers a case to the Court shall be notified to the Court
by the court or tribunal concerned.</p>
<p class="normal">Within two months of this notification, the parties, the Member States,
the Commission and, where appropriate, the institution which adopted the act the
validity or interpretation of which is in dispute, may lodge statements of case or
written observations with the Court.</p>
<p id="d1e300-210-1" class="ti-art">Article 23a (*)</p>
<p class="normal">Unrelated text of Article 23a.</p>
</body></html>
"""

#: Регламент Суду: `div.eli-subdivision#art_N`, як у базовому акті OJ
#: (research/13, §A5, дослівно за формою: art_51, art_96 з частиною 096.001).
ROP_XHTML = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
<div class="eli-subdivision" id="art_51">
<p class="oj-ti-art">Article 51</p>
<div class="eli-title" id="art_51.tit_1"><p class="oj-sti-art">Extension on account of
distance</p></div>
<p class="oj-normal">The procedural time-limits shall be extended on account of
distance by a single period of 10 days.</p>
</div>
<div class="eli-subdivision" id="art_96">
<p class="oj-ti-art">Article 96</p>
<div class="eli-title" id="art_96.tit_1"><p class="oj-sti-art">Participation in
preliminary ruling proceedings</p></div>
<div id="096.001">
<p class="oj-normal">1.   Pursuant to Article 23 of the Statute, the following shall
be authorised to submit observations to the Court:</p>
<table><tbody><tr><td><p class="oj-normal">(a)</p></td>
<td><p class="oj-normal">the parties to the main proceedings,</p></td></tr></tbody></table>
</div>
</div>
<div class="eli-subdivision" id="art_105">
<p class="oj-ti-art">Article 105</p>
<div class="eli-title" id="art_105.tit_1"><p class="oj-sti-art">Expedited
procedure</p></div>
<div id="105.001">
<p class="oj-normal">1.   At the request of the referring court or tribunal or,
exceptionally, of his own motion, the President of the Court may decide that a
reference for a preliminary ruling is to be determined pursuant to an expedited
procedure.</p>
</div>
</div>
</body></html>
"""

#: Практичні вказівки: пункти без id — клітинка «N.» і сусідня клітинка тексту
#: (research/13, §A5, дослівно за формою пункту 14).
PRACTICE_DIRECTIONS_XHTML = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
<table><tbody>
<tr><td valign="top"/><td valign="top"><p class="oj-normal">13.</p></td>
<td valign="top"><span>Unrelated point 13 text.</span></td></tr>
<tr><td valign="top"/><td valign="top"><p class="oj-normal">14.</p></td>
<td valign="top"><span>On account of the non-adversarial nature of preliminary ruling
proceedings, the lodging of statements of case or written observations by the
interested persons referred to in Article 23 of the Statute does not involve any
specific formalities.</span></td></tr>
</tbody></table>
</body></html>
"""


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, text: str = "") -> None:
        self.status_code = status_code
        self.text = text

    def json(self) -> dict[str, Any]:
        return {"results": {"bindings": []}}


class _DocumentSession:
    """Одна документна відповідь на весь запит — досить для читання без as_of.

    SPARQL (пошук чинної консолідації) отримує порожню видачу: процесуальні
    акти в Cellar не консолідовані, і читається сам CELEX.
    """

    def __init__(self, body: str) -> None:
        self._body = body
        self.calls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append(url)
        return _FakeResponse(status_code=200, text=self._body)


# -- Статут: стаття 23 --------------------------------------------------------


def test_statute_article_23_reads_by_heading_text() -> None:
    adapter = EuLawAdapter(session=_DocumentSession(STATUTE_XHTML))  # type: ignore[arg-type]

    result = adapter.fetch(STATUTE_CELEX, path="23")

    assert isinstance(result, AdapterPayload)
    assert "Within two months of this notification" in result.data["text"]
    assert "Article 267" in result.data["text"]
    # Сусідня стаття 23a не потрапляє в текст статті 23 (точний збіг заголовка).
    assert "Unrelated text of Article 23a" not in result.data["text"]


def test_statute_has_no_consolidation_notice_and_amending_acts() -> None:
    adapter = EuLawAdapter(session=_DocumentSession(STATUTE_XHTML))  # type: ignore[arg-type]

    result = adapter.fetch(STATUTE_CELEX, path="23")

    assert isinstance(result, AdapterPayload)
    assert "revision_notice" in result.data
    assert "32024R2019" in result.data["amending_acts"]
    assert "консолідованої" in result.data["revision_notice"].lower() or (
        "консолід" in result.data["revision_notice"].lower()
    )


# -- Регламент Суду: статті 51, 96, 105 --------------------------------------


@pytest.mark.parametrize(
    "article,expected_snippet",
    [
        ("51", "extended on account of"),
        ("96", "Participation in"),
        ("105", "Expedited"),
    ],
)
def test_rules_of_procedure_articles_read_by_id(article: str, expected_snippet: str) -> None:
    adapter = EuLawAdapter(session=_DocumentSession(ROP_XHTML))  # type: ignore[arg-type]

    result = adapter.fetch(ROP_CELEX, path=article)

    assert isinstance(result, AdapterPayload)
    assert expected_snippet in result.data["text"]
    # Локатор доказательства — адрес, которым норму цитируют; внутренний
    # идентификатор элемента разметки остаётся рядом, в unit_id.
    assert result.data["locator"] == article
    assert result.data["unit_id"] == f"art_{article}"


class _NoConsolidationSession:
    """SPARQL завжди 0 рядків — Регламент Суду в Cellar без консолідацій."""

    def get(self, url: str, **kwargs: Any) -> Any:
        class _Resp:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return {"results": {"bindings": []}}

        return _Resp()


def test_rules_of_procedure_as_of_is_always_revision_unknown() -> None:
    """Немає консолідації Регламенту Суду в Cellar — `as_of` чесно недоступний."""
    adapter = EuLawAdapter(session=_NoConsolidationSession())  # type: ignore[arg-type]

    result = adapter.resolve_revision(ROP_CELEX, "2026-01-01")

    assert isinstance(result, RevisionUnknown)


def test_rules_of_procedure_carries_amending_acts_warning() -> None:
    adapter = EuLawAdapter(session=_DocumentSession(ROP_XHTML))  # type: ignore[arg-type]

    result = adapter.fetch(ROP_CELEX, path="96")

    assert isinstance(result, AdapterPayload)
    assert "32026Q01335" in result.data["amending_acts"]
    assert "не доведено" in result.data["revision_notice"]


# -- Практичні вказівки: пункт 14 ---------------------------------------------


def test_practice_directions_point_14_reads_by_table_cell() -> None:
    adapter = EuLawAdapter(session=_DocumentSession(PRACTICE_DIRECTIONS_XHTML))  # type: ignore[arg-type]

    result = adapter.fetch(PRACTICE_DIRECTIONS_CELEX, path="14")

    assert isinstance(result, AdapterPayload)
    assert "non-adversarial nature" in result.data["text"]
    assert "Unrelated point 13" not in result.data["text"]
    assert result.data["locator"] == "14"
    assert result.data["unit_id"] == "point_14"


# -- Публічна картка непубікованого провадження (T151) -----------------------


class _EmptySparqlSession:
    """SPARQL завжди повертає 0 рядків — жодна публікація не знайдена.

    Другий канал картки — легасі-сторінки CURIA — відповідає синтетичною
    сторінкою C-9998/26 (``tests/fixtures/curia``): провадження відкрите,
    документів нуль.
    """

    _CURIA = Path(__file__).parent.parent / "fixtures" / "curia"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> Any:
        self.calls.append(url)
        page = url.rsplit("/", 1)[-1].removesuffix(".jsf")
        text = ""
        if page in ("liste", "fiche", "documents"):
            text = (self._CURIA / f"C-9998-26_{page}.html").read_text(encoding="utf-8")

        class _Resp:
            status_code = 200

            def __init__(self, body: str) -> None:
                self.text = body

            def json(self) -> dict[str, Any]:
                return {"results": {"bindings": []}}

        return _Resp(text)


def test_unpublished_case_card_is_honest_not_confirmable() -> None:
    adapter = EuCaseLawAdapter(session=_EmptySparqlSession())  # type: ignore[arg-type]

    result = adapter.public_card("C-9998/26")

    assert isinstance(result, AdapterPayload)
    assert result.data["published"] is False
    assert result.data["published_status"] == "registered_no_documents"
    assert result.data["public_documents"] == []
    assert result.data["checked_at"]
    assert result.data["checked_sources"]
    assert "curia.europa.eu" in result.data["manual_path"]
    assert result.provenance.confirmable is False


class _OneCaseSparqlSession:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> Any:
        self.calls.append(url)

        class _Resp:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return {
                    "results": {
                        "bindings": [
                            {
                                "celex": {"value": "62021CJ0100"},
                                "date": {"value": "2023-03-21"},
                                "kind": {
                                    "value": "http://publications.europa.eu/resource/"
                                    "authority/resource-type/CJ"
                                },
                            }
                        ]
                    }
                }

        return _Resp()


def test_published_case_card_lists_documents() -> None:
    adapter = EuCaseLawAdapter(session=_OneCaseSparqlSession())  # type: ignore[arg-type]

    result = adapter.public_card("C-100/21")

    assert isinstance(result, AdapterPayload)
    assert result.data["published"] is True
    assert result.data["published_status"] == "published"
    assert result.data["public_documents"][0]["celex"] == "62021CJ0100"
    assert "judgment" in result.data["public_documents"][0]["kind"]
    assert result.provenance.confirmable is False  # картка — не текст рішення


# -- живі перевірки -----------------------------------------------------------


@pytest.mark.live
def test_live_statute_rop_and_practice_directions() -> None:
    law_adapter = EuLawAdapter()

    statute = law_adapter.fetch(STATUTE_CELEX, path="23")
    rop_51 = law_adapter.fetch(ROP_CELEX, path="51")
    directions_14 = law_adapter.fetch(PRACTICE_DIRECTIONS_CELEX, path="14")

    assert isinstance(statute, AdapterPayload)
    assert isinstance(rop_51, AdapterPayload)
    assert isinstance(directions_14, AdapterPayload)
