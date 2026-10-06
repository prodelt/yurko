"""Тікет 21 — ELI → CELEX для права ЄС і картка документа EU.

Мережа не використовується. Відповіді SPARQL лежать у
``tests/fixtures/eu_cellar_sparql`` і скопійовані з живих відповідей Cellar
16.09.2026 без правок (типізовані літерали, URI мов, повні переліки мов).
Запити, якими їх знято, — ті самі ``_card_query``/ELI-запит адаптера.
Синтетичні лише: неоднозначний ELI з кількома CELEX (живо такого не знайдено:
виправлення мають власні ELI ``…/corrigendum/<дата>/oj``) і збій SPARQL.
Розмітка практичних вказівок — та сама форма пункту, що й у
``test_cjeu_public_procedure.py``.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.contracts import FailureCode, NotFound  # noqa: E402
from sources.base import AdapterPayload  # noqa: E402
from sources.eu_law import EuLawAdapter, normalize_eli  # noqa: E402

FIXTURES = Path(__file__).parent.parent / "fixtures" / "eu_cellar_sparql"

PD_CELEX = "32024Q02173"
PD_ELI = "http://data.europa.eu/eli/proc_rules/2024/2173/oj"
CONSOLIDATION_ELI = "http://data.europa.eu/eli/reg/2014/8888/2023-12-19"

PRACTICE_DIRECTIONS_XHTML = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
<table><tbody>
<tr><td valign="top"/><td valign="top"><p class="oj-normal">9.</p></td>
<td valign="top"><span>Unrelated point 9 text.</span></td></tr>
<tr><td valign="top"/><td valign="top"><p class="oj-normal">10.</p></td>
<td valign="top"><span>The Court shall give a ruling on the anonymity of the parties.</span></td></tr>
</tbody></table>
</body></html>
"""

CONSOLIDATED_XHTML = """<html><body>
<div class="eli-subdivision" id="art_2"><p>Article 2</p><p>Product registers.</p></div>
</body></html>
"""


class _Response:
    def __init__(
        self, *, status_code: int = 200, text: str = "", json_data: dict[str, Any] | None = None
    ) -> None:
        self.status_code = status_code
        self.text = text
        self._json = json_data

    def json(self) -> dict[str, Any]:
        if self._json is None:
            raise ValueError("no json")
        return self._json


def _recorded(name: str) -> _Response:
    return _Response(json_data=json.loads((FIXTURES / name).read_text(encoding="utf-8")))


def _bindings(*rows: dict[str, str]) -> _Response:
    return _Response(
        json_data={
            "results": {
                "bindings": [
                    {key: {"type": "literal", "value": value} for key, value in row.items()}
                    for row in rows
                ]
            }
        }
    )


#: Ключі, за якими фальшива сесія впізнає запит адаптера.
ELI_QUERY = 'resource_legal_eli "'
CARD_QUERY = "expression_title"
CONSOLIDATIONS_QUERY = "act_consolidated_based_on_resource_legal"


class _Session:
    """Відповідає за формою запиту: SPARQL — за ключовим словом, документ — XHTML."""

    def __init__(
        self, *, sparql: dict[str, _Response] | None = None, document: _Response | None = None
    ) -> None:
        self._sparql = sparql or {}
        self._document = document
        self.sparql_queries: list[str] = []
        self.document_urls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        query = str((kwargs.get("params") or {}).get("query") or "")
        if query:
            self.sparql_queries.append(query)
            for keyword, response in self._sparql.items():
                if keyword in query:
                    return response
            return _bindings()
        self.document_urls.append(url)
        if self._document is None:
            raise AssertionError(f"unexpected document request {url}")
        return self._document


#: Дата, якою знято фікстури Cellar: картка рахує чинну консолідацію від
#: «сьогодні», тож без закріпленого годинника нова майбутня консолідація у
#: фікстурі змінювала б відповідь залежно від дня прогону (тікет 22).
FIXTURES_TAKEN_ON = dt.date(2026, 9, 16)


def _adapter(session: _Session, *, today: dt.date = FIXTURES_TAKEN_ON) -> EuLawAdapter:
    return EuLawAdapter(session=session, today=lambda: today)  # type: ignore[arg-type]


# -- ELI: форма ідентифікатора ---------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        PD_ELI,
        "eli/proc_rules/2024/2173/oj",
        "ELI/PROC_RULES/2024/2173/OJ",
        "https://eur-lex.europa.eu/eli/proc_rules/2024/2173/oj/eng",
        "http://data.europa.eu/eli/proc_rules/2024/2173/oj/eng/html",
        "https://eur-lex.europa.eu/eli/proc_rules/2024/2173/oj?locale=en",
        f"{PD_ELI}/",
    ],
)
def test_eli_forms_normalize_to_the_work_uri(raw: str) -> None:
    assert normalize_eli(raw) == PD_ELI


def test_consolidation_eli_keeps_its_date_and_drops_language() -> None:
    assert normalize_eli(f"{CONSOLIDATION_ELI}/eng") == CONSOLIDATION_ELI


def test_celex_is_not_an_eli() -> None:
    assert normalize_eli("32014R8888") is None


# -- ELI → CELEX -----------------------------------------------------------------


def test_get_article_by_eli_reads_the_celex_document_and_echoes_both_ids() -> None:
    session = _Session(
        sparql={ELI_QUERY: _recorded("eli_proc_rules_2024_2173_oj.json")},
        document=_Response(text=PRACTICE_DIRECTIONS_XHTML),
    )

    result = _adapter(session).fetch(PD_ELI, path="10")

    assert isinstance(result, AdapterPayload)
    assert "anonymity" in result.data["text"]
    assert result.data["celex"] == PD_CELEX
    assert result.data["eli"] == PD_ELI
    assert session.document_urls[0].endswith("/32024Q02173")


def test_uppercase_eli_is_resolved_not_false_not_found() -> None:
    session = _Session(
        sparql={ELI_QUERY: _recorded("eli_proc_rules_2024_2173_oj.json")},
        document=_Response(text=PRACTICE_DIRECTIONS_XHTML),
    )

    result = _adapter(session).fetch("ELI/PROC_RULES/2024/2173/OJ", path="10")

    assert isinstance(result, AdapterPayload)
    assert f'"{PD_ELI}"' in session.sparql_queries[0]


def test_unknown_eli_is_not_found_with_hint_not_source_unavailable() -> None:
    session = _Session(sparql={ELI_QUERY: _recorded("eli_proc_rules_2024_9999_oj.json")})

    result = _adapter(session).fetch("http://data.europa.eu/eli/proc_rules/2024/9999/oj", path="1")

    assert isinstance(result, NotFound)
    assert "ELI" in result.id_format_hint
    assert session.document_urls == []


def test_consolidation_eli_reads_that_consolidation() -> None:
    session = _Session(
        sparql={ELI_QUERY: _recorded("eli_reg_2014_8888_2023-12-19.json")},
        document=_Response(text=CONSOLIDATED_XHTML),
    )

    result = _adapter(session).fetch(f"{CONSOLIDATION_ELI}/eng", path="2")

    assert isinstance(result, AdapterPayload)
    assert result.data["resolved_document_id"] == "02014R8888-20231219"
    assert result.data["eli"] == CONSOLIDATION_ELI
    assert session.document_urls[0].endswith("/02014R8888-20231219")


def test_unknown_consolidation_eli_hint_names_consolidation_dates() -> None:
    session = _Session(sparql={ELI_QUERY: _bindings()})

    result = _adapter(session).fetch("http://data.europa.eu/eli/reg/2014/8888/2023-12-20", path="2")

    assert isinstance(result, NotFound)
    assert "консолідац" in result.id_format_hint


def test_short_eli_form_resolves_once_and_is_cached() -> None:
    session = _Session(
        sparql={ELI_QUERY: _recorded("eli_proc_rules_2024_2173_oj.json")},
        document=_Response(text=PRACTICE_DIRECTIONS_XHTML),
    )
    adapter = _adapter(session)

    first = adapter.fetch("eli/proc_rules/2024/2173/oj", path="10")
    second = adapter.fetch(f"{PD_ELI}/eng", path="9")

    assert isinstance(first, AdapterPayload) and isinstance(second, AdapterPayload)
    assert first.data["eli"] == PD_ELI and second.data["celex"] == PD_CELEX
    assert sum(ELI_QUERY in query for query in session.sparql_queries) == 1


def test_ambiguous_eli_prefers_base_act_and_exposes_alternatives() -> None:
    """Синтетично: живо ELI з кількома CELEX не траплявся, але вибір мусить бути детермінований."""
    session = _Session(
        sparql={ELI_QUERY: _bindings({"celex": "32014R8888R(01)"}, {"celex": "32014R8888"})},
        document=_Response(text=CONSOLIDATED_XHTML),
    )

    result = _adapter(session).fetch("http://data.europa.eu/eli/reg/2014/8888/oj", path="2")

    assert isinstance(result, AdapterPayload)
    assert result.data["celex"] == "32014R8888"
    assert result.data["eli_alternatives"] == ["32014R8888R(01)"]


def test_fetch_of_base_act_read_in_consolidation_does_not_pass_base_eli_as_document_eli() -> None:
    session = _Session(
        sparql={
            ELI_QUERY: _bindings({"celex": "32014R8888"}),
            CONSOLIDATIONS_QUERY: _recorded("consolidations_32014R8888.json"),
        },
        document=_Response(text=CONSOLIDATED_XHTML),
    )

    result = _adapter(session).fetch("http://data.europa.eu/eli/reg/2014/8888/oj", path="2")

    assert isinstance(result, AdapterPayload)
    assert result.data["resolved_document_id"].startswith("02014R8888-")
    assert result.data["eli"] is None
    assert result.data["basic_act_eli"] == "http://data.europa.eu/eli/reg/2014/8888/oj"


# -- некоректний ідентифікатор ---------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        '32014R8888" } DROP',
        "32014R8888\\",
        "32014R8888\nSELECT",
        'http://data.europa.eu/eli/reg/2014/8888/oj"\\',
        "eli/reg/2014",
    ],
)
def test_malformed_id_is_invalid_input_and_never_reaches_sparql(bad: str) -> None:
    session = _Session()
    adapter = _adapter(session)

    for result in (
        adapter.fetch(bad, path="1"),
        adapter.card(bad),
        adapter.revisions(bad),
        adapter.resolve_revision(bad, "2024-01-01"),
    ):
        assert getattr(result, "failure_code", None) is FailureCode.INVALID_INPUT, result
    assert session.sparql_queries == [] and session.document_urls == []


@pytest.mark.parametrize("celex", ["32020Q0214(01)", "12016E/PRO/03", "02014R8888-20231219"])
def test_real_celex_shapes_are_accepted(celex: str) -> None:
    session = _Session(sparql={CARD_QUERY: _recorded("card_32020Q0214_01.json")})

    result = _adapter(session).card(celex)

    assert getattr(result, "failure_code", None) is not FailureCode.INVALID_INPUT


# -- ELI в інших точках входу EU -------------------------------------------------


def test_revisions_accept_eli() -> None:
    session = _Session(
        sparql={
            ELI_QUERY: _bindings({"celex": "32014R8888"}),
            CONSOLIDATIONS_QUERY: _recorded("consolidations_32014R8888.json"),
        }
    )

    result = _adapter(session).revisions("http://data.europa.eu/eli/reg/2014/8888/oj")

    assert isinstance(result, AdapterPayload)
    assert result.data["base_celex"] == "32014R8888"
    assert result.data["found"] >= 10


def test_proc_rules_eli_is_procedure_class() -> None:
    import server
    from core.legal_orders import DocumentClass

    assert server._document_class_for("EU", PD_ELI) is DocumentClass.PROCEDURE
    assert server._document_class_for("EU", "eli/PROC_RULES/2024/2173/oj") is (
        DocumentClass.PROCEDURE
    )
    assert server._document_class_for("EU", "http://data.europa.eu/eli/reg/2014/8888/oj") is (
        DocumentClass.ACT
    )


# -- картка документа (get_law_metadata) -----------------------------------------


def test_card_of_practice_directions_carries_identity_dates_and_languages() -> None:
    session = _Session(sparql={CARD_QUERY: _recorded("card_32024Q02173.json")})

    result = _adapter(session).card(PD_CELEX)

    assert isinstance(result, AdapterPayload)
    data = result.data
    assert data["resolved_document_id"] == PD_CELEX
    assert (
        data["title"] == "Practice directions to parties concerning cases brought before the Court"
    )
    assert data["eli"] == PD_ELI
    assert data["resource_type"] == "PROC_RULES"
    assert data["date_document"] == "2024-07-02"
    assert data["entry_into_force"] == "2024-09-01"
    assert data["in_force"] is True
    assert data["official_journal"] == {"id": "L_202402173", "publication_date": "2024-08-30"}
    assert len(data["languages"]) == 24 and "ga" in data["languages"]
    assert data["latest_consolidation"] is None
    assert data["eurlex_url"].endswith("/legal-content/EN/ALL/?uri=CELEX:32024Q02173")
    assert result.provenance.citation_format == "CELEX 32024Q02173"
    assert result.provenance.content_kind.value == "metadata"
    assert "2011/833/EU" in (result.provenance.attribution or "")


def test_card_eurlex_url_follows_requested_language() -> None:
    session = _Session(sparql={CARD_QUERY: _recorded("card_32024Q02173.json")})

    result = _adapter(session).card(PD_CELEX, language="fr")

    assert isinstance(result, AdapterPayload)
    assert "/legal-content/FR/ALL/" in result.data["eurlex_url"]


def test_card_of_regulation_names_latest_consolidation() -> None:
    session = _Session(
        sparql={
            CARD_QUERY: _recorded("card_32014R8888.json"),
            CONSOLIDATIONS_QUERY: _recorded("consolidations_32014R8888.json"),
        }
    )

    result = _adapter(session).card("32014R8888")

    assert isinstance(result, AdapterPayload)
    latest = result.data["latest_consolidation"]
    assert latest["consolidation_id"] == "02014R8888-20260807"
    assert latest["date"] == "2026-08-07"
    assert result.data["title"].startswith("Regulation (EU) No 8888/2014 of 10 March 2014")
    assert result.data["official_journal"] == {
        "id": "JOL_2014_070_R_0001_01",
        "publication_date": None,
    }


def test_card_does_not_pass_a_future_consolidation_off_as_the_current_one() -> None:
    """Чинна консолідація картки — не пізніша за «сьогодні» (тікет 22)."""
    session = _Session(
        sparql={
            CARD_QUERY: _recorded("card_32014R8888.json"),
            CONSOLIDATIONS_QUERY: _recorded("consolidations_32014R8888.json"),
        }
    )

    result = _adapter(session, today=dt.date(2024, 1, 5)).card("32014R8888")

    assert isinstance(result, AdapterPayload)
    assert result.data["latest_consolidation"] == {
        "consolidation_id": "02014R8888-20240103",
        "date": "2024-01-03",
    }


def test_card_of_repealed_practice_directions_is_not_in_force_with_suffix_celex() -> None:
    session = _Session(
        sparql={
            CARD_QUERY: _recorded("card_32020Q0214_01.json"),
            CONSOLIDATIONS_QUERY: _bindings(
                {"celex": "02020Q0214(01)-20200301"},
            ),
        }
    )

    result = _adapter(session).card("32020Q0214(01)")

    assert isinstance(result, AdapterPayload)
    assert result.data["in_force"] is False
    assert result.data["end_of_validity"] == "2024-08-31"
    assert any(CONSOLIDATIONS_QUERY in query for query in session.sparql_queries)
    latest = result.data["latest_consolidation"]
    assert latest["consolidation_id"] == "02020Q0214(01)-20200301"
    assert latest["act_in_force"] is False
    assert latest["act_end_of_validity"] == "2024-08-31"


@pytest.mark.parametrize("raw", ["true", "false"])
def test_card_in_force_accepts_boolean_words(raw: str) -> None:
    row = json.loads((FIXTURES / "card_32024Q02173.json").read_text(encoding="utf-8"))
    row["results"]["bindings"][0]["in_force"]["value"] = raw
    session = _Session(sparql={CARD_QUERY: _Response(json_data=row)})

    result = _adapter(session).card(PD_CELEX)

    assert isinstance(result, AdapterPayload)
    assert result.data["in_force"] is (raw == "true")


def test_card_by_eli_resolves_to_celex() -> None:
    session = _Session(
        sparql={
            ELI_QUERY: _recorded("eli_proc_rules_2024_2173_oj.json"),
            CARD_QUERY: _recorded("card_32024Q02173.json"),
        }
    )

    result = _adapter(session).card(PD_ELI)

    assert isinstance(result, AdapterPayload)
    assert result.data["resolved_document_id"] == PD_CELEX
    assert result.data["resource_type"] == "PROC_RULES"


def test_card_of_unknown_celex_is_not_found() -> None:
    """Живий Cellar на невідомий CELEX віддає один рядок порожніх агрегатів, а не нуль рядків."""
    session = _Session(sparql={CARD_QUERY: _recorded("card_32099R9999.json")})

    result = _adapter(session).card("32099R9999")

    assert isinstance(result, NotFound)


def test_card_with_unproven_as_of_keeps_metadata_and_reports_revision_status() -> None:
    session = _Session(
        sparql={
            CARD_QUERY: _recorded("card_32014R8888.json"),
            CONSOLIDATIONS_QUERY: _recorded("consolidations_32014R8888.json"),
        }
    )

    result = _adapter(session).card("32014R8888", as_of="2014-03-18")

    assert isinstance(result, AdapterPayload)
    assert result.data["resource_type"] == "REG"
    assert result.data["revision_as_of"]["status"] == "revision_unknown"
    assert result.data["revision_as_of"]["known_bounds"]


def test_card_with_as_of_and_sparql_failure_keeps_metadata() -> None:
    session = _Session(
        sparql={
            CARD_QUERY: _recorded("card_32014R8888.json"),
            CONSOLIDATIONS_QUERY: _Response(status_code=503),
        },
        # Запасний перелік консолідацій — нотатка Cellar REST; тут мовчить і вона.
        document=_Response(status_code=503),
    )

    result = _adapter(session).card("32014R8888", as_of="2024-01-01")

    assert isinstance(result, AdapterPayload)
    assert result.data["revision_as_of"]["status"] == "source_unavailable"
    assert "rest_notice:http_503" in result.data["revision_as_of"]["reason"]


def test_card_with_proven_as_of_names_the_revision() -> None:
    session = _Session(
        sparql={
            CARD_QUERY: _recorded("card_32014R8888.json"),
            CONSOLIDATIONS_QUERY: _recorded("consolidations_32014R8888.json"),
        }
    )

    result = _adapter(session).card("32014R8888", as_of="2024-01-01")

    assert isinstance(result, AdapterPayload)
    revision = result.data["revision_as_of"]
    assert revision["status"] == "resolved"
    assert revision["consolidation_id"].startswith("02014R8888-2023")


def _with_adapter(session: _Session) -> Any:
    import sources
    from core import legal_orders

    legal_orders.register_default_sources()
    sources.load_adapters()
    original = sources.get_adapter("eu_law_eurlex_cellar")
    sources.register_adapter(_adapter(session))
    return original


def _restore(original: Any) -> None:
    import sources

    if original is not None:
        sources.register_adapter(original)


@pytest.mark.parametrize("document_id", [PD_CELEX, "32012Q0929(01)", PD_ELI])
def test_get_law_metadata_for_eu_returns_card_not_not_covered(document_id: str) -> None:
    """Акт, процесуальний документ (клас procedure) і ELI — усі мають картку."""
    import server

    original = _with_adapter(
        _Session(
            sparql={
                ELI_QUERY: _recorded("eli_proc_rules_2024_2173_oj.json"),
                CARD_QUERY: _recorded("card_32024Q02173.json"),
            }
        )
    )
    try:
        answer = server.get_law_metadata("EU", document_id)
    finally:
        _restore(original)

    assert "code" not in answer, answer
    assert answer["resource_type"] == "PROC_RULES"
    assert answer["official_journal"]["id"] == "L_202402173"
    assert answer["confirmable"] is False
    assert answer["content_kind"] == "metadata"


def test_law_metadata_output_declares_eu_card_fields() -> None:
    from core.contracts import LawMetadataOutput

    declared = set(LawMetadataOutput.model_fields)

    assert {
        "celex",
        "eli",
        "eli_alternatives",
        "eurlex_url",
        "resource_type",
        "date_document",
        "entry_into_force",
        "end_of_validity",
        "in_force",
        "official_journal",
        "languages",
        "latest_consolidation",
        "revision_as_of",
    } <= declared


def test_list_coverage_no_longer_reports_eu_card_as_not_covered() -> None:
    import server
    from core import legal_orders

    legal_orders.register_default_sources()
    coverage = server.list_coverage("EU")
    cellar = next(item for item in coverage["sources"] if item["id"] == "eu_law_eurlex_cellar")
    cards = [cap for cap in cellar["capabilities"] if cap["operation"] == "card"]

    assert cards and all(cap["layer"] == "connected" for cap in cards)
    assert {cap["document_class"] for cap in cards} >= {"act", "procedure"}


# -- citation_format -------------------------------------------------------------


def test_practice_direction_point_is_cited_as_point_not_article() -> None:
    session = _Session(document=_Response(text=PRACTICE_DIRECTIONS_XHTML))

    result = _adapter(session).fetch(PD_CELEX, path="10")

    assert isinstance(result, AdapterPayload)
    assert result.provenance.citation_format.endswith("CELEX 32024Q02173, п. 10")


def test_get_article_and_get_multiple_articles_label_points_alike() -> None:
    import server

    original = _with_adapter(_Session(document=_Response(text=PRACTICE_DIRECTIONS_XHTML)))
    try:
        single = server.get_article("EU", PD_CELEX, "10")
        several = server.get_multiple_articles("EU", PD_CELEX, ["10", "9"])
    finally:
        _restore(original)

    assert single["citation_format"].endswith(", п. 10"), single
    rendered = json.dumps(several, ensure_ascii=False)
    assert "ст. 10" not in rendered and "ст. 9" not in rendered
    labels = [str(item.get("citation_format") or "") for item in several["fragments"].values()]
    assert all(
        label.endswith(f", п. {path}") for label, path in zip(labels, ["10", "9"], strict=True)
    ), labels


def test_unparsed_locator_gets_no_article_prefix() -> None:
    from sources.eu_law import _locator_label

    assert _locator_label("recital 5") == "recital 5"
    assert _locator_label("5a") == "ст. 5a"
