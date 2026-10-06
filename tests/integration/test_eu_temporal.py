"""T135 — резолвер редакції на дату (ADR 0007) на офлайн-фікстурі.

Мережа тут не використовується: SPARQL-відповіді побудовані з синтетичного
маніфесту нижче — вигаданого регламенту ``32015R8888`` з дванадцятьма
датованими консолідаціями і складом трьох із них. Акт і тексти статей вигадані;
справжня лише форма: документні тіла — згорнуті, але за формою справжні
фрагменти розмітки Cellar (research/13, §A1–A3): базовий акт OJ
(``div#NNN.MMM`` + таблиці) і консолідація (``span.no-parag`` +
``div.norm.inline-element``).
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.contracts import (  # noqa: E402
    NotFound,
    RevisionUnknown,
    SourceUnavailable,
    TypedFailure,
)
from core.provenance import PublicationKind  # noqa: E402
from sources.base import AdapterPayload  # noqa: E402
from sources.eu_law import EuLawAdapter  # noqa: E402

_CELLAR_CELEX = "https://publications.europa.eu/resource/celex"

#: Синтетичний маніфест: вигаданий регламент і його датовані консолідації. Форма —
#: як у живій відповіді Cellar SPARQL (перелік консолідацій від базового акта і склад
#: окремих консолідацій), значення — тестові.
_MANIFEST: dict[str, Any] = {
    "consolidations": [
        {"celex": f"02015R8888-{day}", "date": f"{day[:4]}-{day[4:6]}-{day[6:]}"}
        for day in (
            "20150301",
            "20170515",
            "20190910",
            "20210610",
            "20210701",
            "20220110",
            "20220405",
            "20220420",
            "20220701",
            "20240318",
            "20250717",
            "20250807",
        )
    ],
    "compositions": {
        "02015R8888-20210610": [
            {"celex": "32015R8888", "date": "2015-02-10"},
            {"celex": "32021R8101", "date": "2021-06-07"},
            {"celex": "02015R8888-20190910", "date": "2019-09-10"},
        ],
        "02015R8888-20220405": [
            {"celex": "32015R8888", "date": "2015-02-10"},
            {"celex": "32022R8201", "date": "2022-04-04"},
            {"celex": "32022R8202", "date": "2022-04-04"},
            {"celex": "02015R8888-20220110", "date": "2022-01-10"},
        ],
        "02015R8888-20220420": [
            {"celex": "32015R8888", "date": "2015-02-10"},
            {"celex": "32022R8203", "date": "2022-04-14"},
            {"celex": "02015R8888-20220405", "date": "2022-04-05"},
            {"celex": "32022R8201R(01)", "date": "2022-04-19"},
        ],
    },
}

BASE_CELEX = "32015R8888"

# -- документні тіла: згорнуті, але за формою справжні фрагменти Cellar -----

BASE_ACT_XHTML = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
<div class="eli-container">
<div class="eli-subdivision" id="art_2">
<p class="oj-ti-art">Article 2</p>
<div id="002.001"><p class="oj-normal">1.   All operators placing the products listed in
Annex I on the market shall keep a register of their suppliers.</p></div>
</div>
</div>
</body></html>
"""

#: Консолідація 2021-06-10 — до Article 5a (внесена 32022R8201, 2022-04-04).
CONSOLIDATED_BEFORE_5A_XHTML = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
<div class="eli-container">
<div class="eli-subdivision" id="art_5">
<p class="title-article-norm">Article 5</p>
<div class="norm"><span class="no-parag">1.  </span>
<div class="norm inline-element">By way of derogation from Article 2, the competent
authorities may exempt certain small operators.</div></div>
</div>
</div>
</body></html>
"""

#: Консолідація 2022-04-05 — Article 5a присутня (розмітка як у research/13, §A3).
CONSOLIDATED_WITH_5A_XHTML = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
<div class="eli-container">
<div class="eli-subdivision" id="art_5">
<p class="title-article-norm">Article 5</p>
<div class="norm"><span class="no-parag">1.  </span>
<div class="norm inline-element">By way of derogation from Article 2, the competent
authorities may exempt certain small operators, if the following
conditions are met:
<div class="grid-container grid-list">
<div class="list grid-list-column-1"><span>(a)&#160;</span></div>
<div class="grid-list-column-2"><p class="norm">the operator holds an inspection
report issued before the product was entered in Annex I.</p></div>
</div>
</div></div>
</div>
<div class="eli-subdivision" id="art_5a">
  <p class="title-article-norm" id="id-090b86ea">Article 5a</p>
  <div class="norm"><span class="no-parag">1.  </span>
    <div class="norm inline-element">By way of derogation from Article 2, the competent
    authorities of a Member State may grant an operator a temporary exemption, after
    having determined that a judicial or administrative authority of a Member State
    has ordered the withdrawal of the operator's products from the market in the
    public interest, provided that the withdrawn products are kept in storage.</div></div>
  <div class="norm"><span class="no-parag">2.  </span><div class="norm inline-element">
    The Member State concerned shall inform the other Member States and the Commission
    of any exemption granted under paragraph 1.</div></div>
</div>
</div>
</body></html>
"""

_DOCUMENTS: dict[str, str] = {
    BASE_CELEX: BASE_ACT_XHTML,
    "02015R8888-20210610": CONSOLIDATED_BEFORE_5A_XHTML,
    "02015R8888-20220405": CONSOLIDATED_WITH_5A_XHTML,
}


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        text: str = "",
        json_data: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self._json_data = json_data

    @property
    def content(self) -> bytes:
        return self.text.encode("utf-8")

    def json(self) -> dict[str, Any]:
        if self._json_data is None:
            raise ValueError("no json body")
        return self._json_data


def _sparql_bindings(rows: list[dict[str, str]], *keys: str) -> dict[str, Any]:
    bindings = [{key: {"value": row[key]} for key in keys if key in row} for row in rows]
    return {"results": {"bindings": bindings}}


#: Ключ, за яким фальшива сесія впізнає SPARQL-запит переліку консолідацій.
CONSOLIDATIONS_QUERY = "act_consolidated_based_on_resource_legal"


def _is_notice_request(kwargs: dict[str, Any]) -> bool:
    return "notice=tree" in str((kwargs.get("headers") or {}).get("Accept") or "")


def _tree_notice(relation: str, celexes: list[str]) -> str:
    """Дерево нотатки Cellar REST (``Accept: application/xml;notice=tree``), згорнуте.

    Форма — як у живій пробі 22.09.2026: зв'язок — прямий нащадок
    ``NOTICE/WORK``, CELEX адресата — у ``SAMEAS/URI`` з ``TYPE=celex``. У живій
    нотатці більшість ланок консолідацій — без CELEX; ланка без CELEX і чужий
    зв'язок тут є, щоб розбір не брав зайвого.
    """
    links = "".join(f"""<{relation} type="link">
<EMBEDDED_NOTICE><WORK><RESOURCE_LEGAL_ID_CELEX type="data"><VALUE>{celex}</VALUE>
</RESOURCE_LEGAL_ID_CELEX></WORK></EMBEDDED_NOTICE>
<URI><VALUE>http://publications.europa.eu/resource/cellar/{index:08d}</VALUE>
<IDENTIFIER>{index:08d}</IDENTIFIER><TYPE>cellar</TYPE></URI>
<SAMEAS><URI><VALUE>http://publications.europa.eu/resource/celex/{celex}</VALUE>
<IDENTIFIER>{celex}</IDENTIFIER><TYPE>celex</TYPE></URI></SAMEAS>
</{relation}>""" for index, celex in enumerate(celexes))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<NOTICE decoding="eng" type="tree"><WORK>
<{relation} type="link"><SAMEAS><URI>
<VALUE>http://publications.europa.eu/resource/consolidation/2015R8888%2F20170515_0170010</VALUE>
<IDENTIFIER>2015R8888/20170515_0170010</IDENTIFIER><TYPE>consolidation</TYPE>
</URI></SAMEAS></{relation}>
<WORK_CITES_WORK type="link"><SAMEAS><URI><IDENTIFIER>32020R8000</IDENTIFIER>
<TYPE>celex</TYPE></URI></SAMEAS></WORK_CITES_WORK>
{links}
</WORK></NOTICE>
"""


class _ManifestSession:
    """Відповідає на SPARQL із фікстури-маніфесту й на GET документа зі словника.

    Не черга: маршрутизація за вмістом запиту, бо резолвер редакції та fetch
    можуть звертатися до SPARQL і документа в різному порядку для різних
    сценаріїв (ADR 0007).
    """

    def __init__(self, documents: dict[str, str]) -> None:
        self._documents = documents
        self.calls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append(url)
        if _is_notice_request(kwargs):
            raise AssertionError(f"нотатку REST запитано, хоча SPARQL відповідає: {url}")
        if "sparql" in url:
            query = str(kwargs.get("params", {}).get("query") or "")
            if CONSOLIDATIONS_QUERY in query:
                rows = _MANIFEST["consolidations"]
                return _FakeResponse(status_code=200, json_data=_sparql_bindings(rows, "celex"))
            for dated_celex, composition in _MANIFEST["compositions"].items():
                if f'"{dated_celex}"' in query:
                    return _FakeResponse(
                        status_code=200, json_data=_sparql_bindings(composition, "celex", "date")
                    )
            raise AssertionError(f"no scripted SPARQL response for query: {query[:200]}")

        celex = unquote(url.rsplit("/", 1)[-1])
        if celex not in self._documents:
            raise AssertionError(f"no scripted document response for {celex!r}")
        return _FakeResponse(status_code=200, text=self._documents[celex])


def _adapter() -> EuLawAdapter:
    return EuLawAdapter(session=_ManifestSession(_DOCUMENTS))  # type: ignore[arg-type]


# -- (a) до появи Article 5a: локатор «5a» відсутній САМЕ В ЦІЙ РЕДАКЦІЇ -----


def test_revision_before_5a_has_no_article_5a() -> None:
    adapter = _adapter()

    result = adapter.fetch(BASE_CELEX, as_of="2021-06-10", path="5a")

    assert isinstance(result, NotFound)
    assert "02015R8888-20210610" in result.identifier
    assert "редакції" in result.id_format_hint or "редакц" in result.explain()


def test_revision_before_5a_resolves_to_expected_interval() -> None:
    adapter = _adapter()

    revision = adapter.resolve_revision(BASE_CELEX, "2021-06-10")

    assert not isinstance(revision, RevisionUnknown)
    assert revision.consolidation_id == "02015R8888-20210610"
    assert revision.valid_from == "2021-06-10"
    assert revision.valid_to == "2021-06-30"  # день до наступної консолідації (20210701)
    assert revision.known_through is None
    assert revision.publication_kind == "consolidated"


# -- (b) після 2022-04-05: редакція 02015R8888-20220405, art_5a читається ----


def test_revision_after_5a_introduced_reads_article_5a() -> None:
    adapter = _adapter()

    result = adapter.fetch(BASE_CELEX, as_of="2022-04-10", path="5a")

    assert isinstance(result, AdapterPayload)
    assert result.data["resolved_document_id"] == "02015R8888-20220405"
    assert "Article 5a" in result.data["text"]
    assert result.data["locator"] == "5a"
    assert result.data["unit_id"] == "art_5a"
    assert result.provenance.publication_kind is PublicationKind.CONSOLIDATED


def test_revision_2023_12_19_amending_acts_from_manifest() -> None:
    adapter = _adapter()

    revision = adapter.resolve_revision(BASE_CELEX, "2022-04-05")

    assert not isinstance(revision, RevisionUnknown)
    assert revision.consolidation_id == "02015R8888-20220405"
    # Склад консолідації в маніфесті: базовий акт, дві поправки, попередня
    # консолідація — амендменти без базового акта й без попередньої (T133).
    assert set(revision.amending_acts) == {"32022R8201", "32022R8202"}
    assert BASE_CELEX not in revision.amending_acts
    assert "02015R8888-20220110" not in revision.amending_acts


# -- (c) дата пізніше known_through — revision_unknown -----------------------


def test_date_beyond_known_through_is_revision_unknown() -> None:
    adapter = _adapter()

    result = adapter.fetch(BASE_CELEX, as_of="2099-01-01", path="5a")

    assert isinstance(result, RevisionUnknown)
    assert "2025-08-07" in " ".join(result.known_bounds)


def test_date_before_first_consolidation_is_revision_unknown() -> None:
    adapter = _adapter()

    revision = adapter.resolve_revision(BASE_CELEX, "2000-01-01")

    assert isinstance(revision, RevisionUnknown)
    assert "2015-03-01" in " ".join(revision.known_bounds)


# -- (d) «5a» і «5(1)(a)» — різні локатори/тексти ----------------------------


def test_5a_and_5_1_a_are_different_units_in_same_revision() -> None:
    adapter = _adapter()

    unit_5a = adapter.fetch(BASE_CELEX, as_of="2022-04-10", path="5a")
    unit_5_1_a = adapter.fetch(BASE_CELEX, as_of="2022-04-10", path="5(1)(a)")

    assert isinstance(unit_5a, AdapterPayload)
    assert isinstance(unit_5_1_a, AdapterPayload)
    assert unit_5a.data["locator"] == "5a"
    assert unit_5_1_a.data["locator"] == "5(1)(a)"
    assert unit_5a.data["unit_id"] != unit_5_1_a.data["unit_id"]
    assert unit_5a.data["text"] != unit_5_1_a.data["text"]
    assert "inspection report" in unit_5_1_a.data["text"]
    assert "inspection report" not in unit_5a.data["text"]


# -- (d2) без as_of — чинна консолідація, а не первинний текст OJ -------------


#: Остання консолідація маніфесту; склад — синтетичний, як і решта маніфесту.
LATEST_CELEX = "02015R8888-20250807"
LATEST_COMPOSITION = [{"celex": "32025R8301"}, {"celex": "32025R8302"}, {"celex": BASE_CELEX}]


class _LatestManifestSession(_ManifestSession):
    """Маніфест плюс склад і тіло останньої консолідації (у маніфесті їх немає)."""

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        query = str(kwargs.get("params", {}).get("query") or "")
        if "sparql" in url and f'"{LATEST_CELEX}"' in query:
            self.calls.append(url)
            return _FakeResponse(
                status_code=200, json_data=_sparql_bindings(LATEST_COMPOSITION, "celex")
            )
        return super().get(url, **kwargs)


def test_article_inserted_by_an_amendment_is_read_without_as_of() -> None:
    """Живий виклик 15.09.2026: стаття, внесена поправкою, без ``as_of`` → not_found.

    Тут стаття 5a внесена (вигаданим) актом 32022R8201 і є в консолідації
    02015R8888-20250807, але без ``as_of`` читався первинний текст OJ, де її
    ще немає. Без дати юрист питає чинну норму.
    """
    session = _LatestManifestSession({**_DOCUMENTS, LATEST_CELEX: CONSOLIDATED_WITH_5A_XHTML})
    adapter = EuLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch(BASE_CELEX, path="5a")

    assert isinstance(result, AdapterPayload)
    assert "Article 5a" in result.data["text"]
    assert result.data["unit_id"] == "art_5a"
    assert result.data["resolved_document_id"] == LATEST_CELEX
    assert result.data["revision"]["consolidation_id"] == LATEST_CELEX
    assert result.data["revision"]["known_through"] == "2025-08-07"
    assert "32025R8301" in result.data["revision"]["amending_acts"]
    assert result.provenance.publication_kind is PublicationKind.CONSOLIDATED
    assert BASE_CELEX in result.data["aliases"]


class _FutureConsolidationSession(_LatestManifestSession):
    """Маніфест плюс консолідація, датована днем застосування поправки в майбутньому."""

    FUTURE = "02015R8888-20991231"

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        query = str(kwargs.get("params", {}).get("query") or "")
        if "sparql" in url and CONSOLIDATIONS_QUERY in query:
            self.calls.append(url)
            rows = [*_MANIFEST["consolidations"], {"celex": self.FUTURE}]
            return _FakeResponse(status_code=200, json_data=_sparql_bindings(rows, "celex"))
        return super().get(url, **kwargs)


def test_a_consolidation_dated_in_the_future_is_not_the_one_in_force() -> None:
    session = _FutureConsolidationSession({**_DOCUMENTS, LATEST_CELEX: CONSOLIDATED_WITH_5A_XHTML})
    adapter = EuLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch(BASE_CELEX, path="5a")

    assert isinstance(result, AdapterPayload)
    assert result.data["resolved_document_id"] == LATEST_CELEX
    assert result.data["revision"]["valid_to"] == "2099-12-30"


def test_consolidations_are_listed_from_the_basic_act_not_by_scanning_celex_prefixes() -> None:
    """Жива проба 22.09.2026: префіксний фільтр ``STRSTARTS`` — 6–16 с на акт.

    Він переглядав усі CELEX Cellar, і при тайм-ауті 30 с один повільний день
    джерела означав «чинну консолідацію не визначено». Зв'язок консолідації з
    базовим актом дає той самий перелік за 0,1 с.
    """
    session = _LatestManifestSession({**_DOCUMENTS, LATEST_CELEX: CONSOLIDATED_WITH_5A_XHTML})
    adapter = EuLawAdapter(session=session)  # type: ignore[arg-type]
    sent: list[str] = []
    real_get = session.get

    def spy(url: str, **kwargs: Any) -> _FakeResponse:
        sent.append(str(kwargs.get("params", {}).get("query") or ""))
        return real_get(url, **kwargs)

    session.get = spy  # type: ignore[method-assign]

    assert isinstance(adapter.fetch(BASE_CELEX, path="5a"), AdapterPayload)
    listing = [query for query in sent if CONSOLIDATIONS_QUERY in query]
    assert len(listing) == 1
    assert "STRSTARTS" not in listing[0]
    assert '"32015R8888"' in listing[0]


class _SparqlDownSession(_LatestManifestSession):
    """Cellar SPARQL віддає 503; REST — документи CELEX і, якщо задано, нотатки.

    ``notices`` — дерево нотатки за CELEX; ``None`` — нотатка теж недоступна
    (503), а сторінки документів ще читаються.
    """

    def __init__(self, documents: dict[str, str], notices: dict[str, str] | None = None) -> None:
        super().__init__(documents)
        self._notices = notices

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        if "sparql" in url:
            self.calls.append(url)
            return _FakeResponse(status_code=503)
        if _is_notice_request(kwargs):
            self.calls.append(f"notice:{url}")
            if self._notices is None:
                return _FakeResponse(status_code=503)
            celex = unquote(url.rsplit("/", 1)[-1])
            if celex not in self._notices:
                raise AssertionError(f"no scripted notice for {celex!r}")
            return _FakeResponse(status_code=200, text=self._notices[celex])
        return super().get(url, **kwargs)


#: Нотатки REST, з яких без SPARQL видно чинну консолідацію та її склад.
_NOTICES = {
    BASE_CELEX: _tree_notice(
        "RESOURCE_LEGAL_BASIS_FOR_ACT_CONSOLIDATED",
        [row["celex"] for row in _MANIFEST["consolidations"]],
    ),
    LATEST_CELEX: _tree_notice(
        "ACT_CONSOLIDATED_CONSOLIDATES_RESOURCE_LEGAL",
        [BASE_CELEX, "02015R8888-20250717", "32025R8301", "32025R8302", "32015R8888R(04)"],
    ),
}


def _sparql_down_adapter(session: _SparqlDownSession) -> EuLawAdapter:
    return EuLawAdapter(
        session=session,  # type: ignore[arg-type]
        today=lambda: dt.date(2025, 9, 22),
    )


def test_sparql_outage_reads_article_5a_from_the_consolidation_listed_in_the_rest_notice() -> None:
    """Перевірка 22.09.2026: SPARQL лежить — внесена стаття «не існує».

    Без SPARQL читався первинний текст OJ, де внесеної статті ще немає, і вона
    була недосяжна. Перелік консолідацій і склад тепер береться з дерева
    нотатки Cellar REST — тієї ж служби, що віддає текст.
    """
    session = _SparqlDownSession(
        {**_DOCUMENTS, LATEST_CELEX: CONSOLIDATED_WITH_5A_XHTML}, notices=_NOTICES
    )

    result = _sparql_down_adapter(session).fetch(BASE_CELEX, path="5a")

    assert isinstance(result, AdapterPayload)
    assert "Article 5a" in result.data["text"]
    assert result.data["resolved_document_id"] == LATEST_CELEX
    assert result.data["revision"]["known_through"] == "2025-08-07"
    assert result.data["revision"]["valid_from"] == "2025-08-07"
    assert set(result.data["revision"]["amending_acts"]) == {
        "32025R8301",
        "32025R8302",
        "32015R8888R(04)",
    }
    assert "current_revision_unavailable" not in result.data


def test_sparql_outage_resolves_a_dated_revision_from_the_rest_notice() -> None:
    session = _SparqlDownSession(
        _DOCUMENTS,
        notices={
            BASE_CELEX: _NOTICES[BASE_CELEX],
            "02015R8888-20220405": _tree_notice(
                "ACT_CONSOLIDATED_CONSOLIDATES_RESOURCE_LEGAL",
                [BASE_CELEX, "32022R8201", "32022R8202", "02015R8888-20220110"],
            ),
        },
    )

    revision = _sparql_down_adapter(session).resolve_revision(BASE_CELEX, "2022-04-10")

    assert not isinstance(revision, (RevisionUnknown, TypedFailure))
    assert revision.consolidation_id == "02015R8888-20220405"
    assert set(revision.amending_acts) == {"32022R8201", "32022R8202"}


def test_composition_of_a_consolidation_is_asked_once_per_process() -> None:
    """Склад датованої консолідації не змінюється — другий запит у мережу не йде.

    Без цього під час збою SPARQL кожне читання статті того самого акта знову
    чекало б відмови SPARQL на складі консолідації.
    """
    session = _SparqlDownSession(
        {**_DOCUMENTS, LATEST_CELEX: CONSOLIDATED_WITH_5A_XHTML}, notices=_NOTICES
    )
    adapter = _sparql_down_adapter(session)

    adapter.fetch(BASE_CELEX, path="5a")
    adapter.fetch(BASE_CELEX, path="5")

    assert session.calls.count(f"notice:{_CELLAR_CELEX}/{LATEST_CELEX}") == 1


def test_sparql_outage_without_as_of_still_reads_the_primary_text() -> None:
    """Рев'ю 15.09.2026: пошук чинної консолідації не має валити все читання.

    Без ``as_of`` консолідація — уточнення, а не передумова: коли не
    відповідають ні SPARQL, ні нотатка REST, текст CELEX досі читається, і
    відповідь прямо каже, що чинну консолідацію визначити не вдалося.
    """
    adapter = _sparql_down_adapter(_SparqlDownSession(_DOCUMENTS))

    result = adapter.fetch(BASE_CELEX, path="2")

    assert isinstance(result, AdapterPayload)
    assert result.data["resolved_document_id"] == BASE_CELEX
    assert "revision" not in result.data
    assert "http_503" in result.data["current_revision_unavailable"]
    assert result.provenance.publication_kind is PublicationKind.OFFICIAL_JOURNAL


def test_missing_article_with_unknown_consolidation_is_unavailable_not_not_found() -> None:
    """``not_found`` тут — хибне твердження: статтю могли внести пізніше.

    Перевіряльник, що бачить ``not_found`` на ст. 5a, робить висновок, що
    статті немає. Поки чинну консолідацію не визначено, відповідь джерела —
    «не можу сказати», тобто ``source_unavailable`` з причиною і ручним шляхом.
    """
    adapter = _sparql_down_adapter(_SparqlDownSession(_DOCUMENTS))

    result = adapter.fetch(BASE_CELEX, path="5a")

    assert isinstance(result, SourceUnavailable)
    assert result.reason.startswith("current_consolidation_unknown")
    assert "sparql:http_503" in result.reason
    assert "rest_notice:http_503" in result.reason
    assert "«5a»" in result.manual_path
    assert "первинному тексті" in result.manual_path
    assert "датований CELEX" in result.manual_path


# -- (e) publication_kind: консолідація vs офіційна публікація --------------


class _UnconsolidatedSession(_ManifestSession):
    """Той самий акт, але Cellar консолідацій не знає — читається первинний текст OJ."""

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        if "sparql" in url:
            self.calls.append(url)
            return _FakeResponse(status_code=200, json_data={"results": {"bindings": []}})
        return super().get(url, **kwargs)


def test_publication_kind_distinguishes_consolidated_from_official_journal() -> None:
    oj_adapter = EuLawAdapter(session=_UnconsolidatedSession(_DOCUMENTS))  # type: ignore[arg-type]

    oj_result = oj_adapter.fetch(BASE_CELEX, path="2")
    consolidated_result = _adapter().fetch("02015R8888-20220405", path="5a")

    assert isinstance(oj_result, AdapterPayload)
    assert isinstance(consolidated_result, AdapterPayload)
    assert oj_result.provenance.publication_kind is PublicationKind.OFFICIAL_JOURNAL
    assert consolidated_result.provenance.publication_kind is PublicationKind.CONSOLIDATED


# -- (f) годинник: «сьогодні» задається ззовні (тікет 22) -------------------


def test_current_revision_takes_today_from_the_injected_clock() -> None:
    """Чинна консолідація рахується від заданої дати, а не від дати прогону.

    Без ін'єкції годинника фікстура з майбутньою датою рано чи пізно стає
    минулою і мовчки починає давати іншу відповідь.
    """
    adapter = EuLawAdapter(
        session=_ManifestSession(_DOCUMENTS),  # type: ignore[arg-type]
        today=lambda: dt.date(2022, 4, 25),
    )

    revision = adapter.current_revision(BASE_CELEX)

    assert not isinstance(revision, (RevisionUnknown, TypedFailure))
    assert revision is not None
    assert revision.consolidation_id == "02015R8888-20220420"
    assert revision.valid_from == "2022-04-20"
    assert revision.valid_to == "2022-06-30"  # день до наступної консолідації (20220701)


def test_current_revision_ignores_consolidations_dated_after_the_clock() -> None:
    """Дата раніша за першу консолідацію — чинної консолідації немає."""
    adapter = EuLawAdapter(
        session=_ManifestSession(_DOCUMENTS),  # type: ignore[arg-type]
        today=lambda: dt.date(2015, 2, 1),
    )

    assert adapter.current_revision(BASE_CELEX) is None
