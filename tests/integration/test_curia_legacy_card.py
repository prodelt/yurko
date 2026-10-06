"""Картка справи Суду ЄС: другий канал — легасі-інтерфейс CURIA (15.09.2026).

Живий виклик ``get_case`` на відкриту справу без публікацій повернув
``not_published`` без жодної відомості про справу: Cellar публікації ще не має, а
картка шукалася лише там.
Легасі-інтерфейс ``juris.curia.europa.eu/juris`` віддає ту саму картку серверним
HTML (liste.jsf → fiche.jsf, documents.jsf): дата подання, суд, що звернувся,
предмет, мова справи, перелік документів.

Мережа тут не використовується: фікстури C-600/26 і C-605/26 — публічні сторінки
суду, зняті живим запитом 15.09.2026 (``tests/fixtures/curia``), без службових
jsessionid/cid. C-9998/26 — синтетична сторінка тієї самої розмітки (вигаданий
номер): справа зареєстрована, документів немає.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import unquote

import requests

from core.contracts import SourceUnavailable
from sources.base import AdapterPayload
from sources.curia_legacy import parse_case_documents
from sources.eu_case_law import EuCaseLawAdapter

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "curia"


class _Response:
    def __init__(self, *, status_code: int = 200, text: str = "", json_data: Any = None) -> None:
        self.status_code = status_code
        self.text = text
        self._json_data = json_data

    def json(self) -> Any:
        if self._json_data is None:
            raise ValueError("no json body")
        return self._json_data


class _CuriaSession:
    """Cellar без публікацій; сторінки CURIA — з фікстур за номером справи."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        params = dict(kwargs.get("params") or {})
        self.calls.append({"url": url, **kwargs})
        if "sparql" in url:
            return _Response(json_data={"results": {"bindings": []}})
        page = url.rstrip("/").rsplit("/", 1)[-1]
        if page in ("liste.jsf", "documents.jsf"):
            slug = str(params["num"]).replace("/", "-")
            return _Response(text=(_FIXTURES / f"{slug}_{page[:-4]}.html").read_text("utf-8"))
        if page == "fiche.jsf":
            parts = unquote(str(params["id"])).split(";")
            slug = f"{parts[0]}-{parts[1]}-{parts[2]}"
            return _Response(text=(_FIXTURES / f"{slug}_fiche.html").read_text("utf-8"))
        raise AssertionError(f"unexpected request {url}")


def test_case_absent_from_cellar_is_read_from_the_curia_legacy_card() -> None:
    session = _CuriaSession()
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.public_card("C-9998/26")

    assert isinstance(result, AdapterPayload)
    data = result.data
    assert data["channel"] == "curia_legacy_card"
    assert data["case_number"] == "C-9998/26"
    assert data["case_name"] == "Placeholder"
    assert data["lodged_on"] == "2026-06-04"
    assert data["referring_court"] == "Tribunal judiciaire de Paris"
    assert data["referring_state"] == "France"
    assert data["subject_matter"] == ["Agriculture - Direct payments"]
    assert data["procedure"] == "Reference for a preliminary ruling"
    assert data["language_of_case"] == ["French"]
    # Відсутність документів — не помилка: провадження відкрите, публікацій ще немає.
    # Статус це й каже: справа зареєстрована, а не «не опубліковано» навздогад.
    assert data["public_documents"] == []
    assert data["published"] is False
    assert data["published_status"] == "registered_no_documents"
    assert data["source_url"] == (
        "https://juris.curia.europa.eu/juris/liste.jsf?num=C-9998/26&language=en"
    )
    assert data["fetched_at"]
    assert result.provenance.confirmable is False


def test_documents_published_only_in_curia_are_listed_with_dates_and_links() -> None:
    """C-605/26: у CURIA — запит про преюдиціальне рішення від 05.06.2026, у Cellar — нічого."""
    adapter = EuCaseLawAdapter(session=_CuriaSession())  # type: ignore[arg-type]

    result = adapter.public_card("C-605/26")

    assert isinstance(result, AdapterPayload)
    data = result.data
    assert data["published_status"] == "published"
    assert data["documents_complete"] is True
    assert data["referring_court"] == "Fővárosi Törvényszék"
    assert data["referring_state"] == "Hungary"
    assert data["subject_matter"] == [
        "area of freedom, security and justice - judicial cooperation in criminal matters",
        "area of freedom, security and justice - police cooperation",
        "Fundamental rights - Charter of Fundamental Rights",
    ]
    assert data["public_documents"] == [
        {
            "title": "Request for a preliminary ruling",
            "ecli": None,
            "date": "2026-06-05",
            "pdf_url": (
                "https://juris.curia.europa.eu/juris/showPdf.jsf?text=&docid=313827"
                "&pageIndex=0&doclang=en&mode=req&dir=&occ=first&part=1"
            ),
        }
    ]


def test_document_links_do_not_carry_the_session_of_the_reading() -> None:
    """Жива сторінка вшиває в посилання jsessionid і cid свого сеансу JSF (15.09.2026).

    Такі адреси протухають разом із сеансом і видають, коли й ким читалося;
    фікстури знято вже без них, тому сеанс тут дописаний назад у рядок.
    """
    body = (_FIXTURES / "C-605-26_documents.html").read_text("utf-8")
    live = body.replace("/juris/showPdf.jsf?", "/juris/showPdf.jsf;jsessionid=0A1B2C3D?").replace(
        "&amp;part=1", "&amp;part=1&amp;cid=10912140"
    )

    parsed = parse_case_documents(live, base="https://juris.curia.europa.eu/juris/")

    assert parsed is not None
    _, documents = parsed
    assert documents[0]["pdf_url"] == (
        "https://juris.curia.europa.eu/juris/showPdf.jsf?text=&docid=313827"
        "&pageIndex=0&doclang=en&mode=req&dir=&occ=first&part=1"
    )


def test_every_listed_document_is_parsed_in_date_order() -> None:
    """C-600/26: два рядки — запит (PDF) і повідомлення в OJ (HTML); сторінка каже «2»."""
    body = (_FIXTURES / "C-600-26_documents.html").read_text("utf-8")

    parsed = parse_case_documents(body, base="https://juris.curia.europa.eu/juris/")

    assert parsed is not None
    declared, documents = parsed
    assert declared == 2
    assert [(item["date"], item["title"]) for item in documents] == [
        ("2026-06-04", "Request for a preliminary ruling"),
        ("2026-09-07", "Application (OJ)"),
    ]
    assert "pdf_url" in documents[0]
    assert documents[1]["html_url"].startswith(
        "https://juris.curia.europa.eu/juris/document/document.jsf?"
    )


def test_a_case_number_unknown_to_curia_is_said_so_not_invented() -> None:
    adapter = EuCaseLawAdapter(session=_CuriaSession())  # type: ignore[arg-type]

    result = adapter.public_card("C-9999/26")

    assert isinstance(result, AdapterPayload)
    assert result.data["case_found"] is False
    assert result.data["published_status"] == "case_not_found"
    assert result.data["public_documents"] == []
    assert "lodged_on" not in result.data


def test_get_case_tool_returns_the_curia_card(monkeypatch: Any) -> None:
    """Той самий виклик, що 15.09.2026 дав голе not_published, — через сам інструмент."""
    import server
    import sources

    adapter = EuCaseLawAdapter(session=_CuriaSession())  # type: ignore[arg-type]
    monkeypatch.setitem(sources._ADAPTERS, adapter.source_id, adapter)

    output = server.get_case(legal_order="eu", case_number="C-9998/26")

    assert "code" not in output, output
    assert output["source_id"] == "eu_cjeu_public_case_card"
    assert output["channel"] == "curia_legacy_card"
    assert output["lodged_on"] == "2026-06-04"
    assert output["referring_court"] == "Tribunal judiciaire de Paris"
    assert output["language_of_case"] == ["French"]
    assert output["public_documents"] == []
    assert output["source_url"].startswith("https://juris.curia.europa.eu/juris/liste.jsf")
    assert output["fetched_at"]


class _CuriaDownSession(_CuriaSession):
    def __init__(self, failure: BaseException | _Response) -> None:
        super().__init__()
        self._failure = failure

    def get(self, url: str, **kwargs: Any) -> _Response:
        if "juris.curia.europa.eu" in url:
            self.calls.append({"url": url, **kwargs})
            if isinstance(self._failure, BaseException):
                raise self._failure
            return self._failure
        return super().get(url, **kwargs)


class _CellarDownSession(_CuriaSession):
    def get(self, url: str, **kwargs: Any) -> _Response:
        if "sparql" in url:
            self.calls.append({"url": url, **kwargs})
            return _Response(status_code=503)
        return super().get(url, **kwargs)


def test_cellar_outage_falls_back_to_the_curia_card() -> None:
    """Рев'ю 15.09.2026: другий канал мусить рятувати й тоді, коли лежить сам Cellar."""
    adapter = EuCaseLawAdapter(session=_CellarDownSession())  # type: ignore[arg-type]

    result = adapter.public_card("C-9998/26")

    assert isinstance(result, AdapterPayload)
    assert result.data["channel"] == "curia_legacy_card"
    assert result.data["lodged_on"] == "2026-06-04"
    assert any("http_503" in item for item in result.data["checked_sources"])


class _AllDownSession(_CellarDownSession):
    def get(self, url: str, **kwargs: Any) -> _Response:
        if "juris.curia.europa.eu" in url:
            raise requests.ConnectionError("down")
        return super().get(url, **kwargs)


def test_cellar_and_curia_both_down_is_a_typed_failure() -> None:
    adapter = EuCaseLawAdapter(session=_AllDownSession())  # type: ignore[arg-type]

    result = adapter.public_card("C-9998/26")

    assert isinstance(result, SourceUnavailable)
    assert result.reason == "curia_legacy:network_error:ConnectionError"


def test_curia_unreachable_is_a_typed_failure_with_a_manual_path() -> None:
    adapter = EuCaseLawAdapter(
        session=_CuriaDownSession(requests.ConnectionError("down"))  # type: ignore[arg-type]
    )

    result = adapter.public_card("C-9998/26")

    assert isinstance(result, SourceUnavailable)
    assert result.reason == "curia_legacy:network_error:ConnectionError"
    assert "https://juris.curia.europa.eu/juris/liste.jsf?num=C-9998/26" in result.manual_path


def test_a_changed_curia_page_is_not_taken_for_an_empty_case() -> None:
    """Якщо легасі-сторінку замінять SPA-оболонкою, картка не скаже «справи немає»."""
    shell = _Response(text="<html><body><app-root></app-root></body></html>")
    adapter = EuCaseLawAdapter(session=_CuriaDownSession(shell))  # type: ignore[arg-type]

    result = adapter.public_card("C-9998/26")

    assert isinstance(result, SourceUnavailable)
    assert result.reason == "curia_legacy:unrecognised_page"


class _SuffixSession(_CuriaSession):
    """``liste.jsf?num=C-605/26`` віддає перелік, у якому перша справа — інша.

    Легасі-пошук CURIA звужує номер, а не звіряє його: на ``C-605/26`` у видачу
    потрапляють і ``C-605/26 P``, і ``C-605/26 PPU``. Тут перший рядок переліку
    веде на картку іншої справи, а потрібна — другим.
    """

    def get(self, url: str, **kwargs: Any) -> _Response:
        response = super().get(url, **kwargs)
        page = url.rstrip("/").rsplit("/", 1)[-1]
        if page != "liste.jsf":
            return response
        other = response.text.replace(
            "fiche.jsf?id=C%3B605%3B26%3BRP%3B1%3BP%3B1%3BC2026%2F0605%2FP&amp;nat=or"
            "&amp;mat=or&amp;pcs=Oor&amp;jur=C%2CT%2CF&amp;num=C-605%252F26",
            "fiche.jsf?id=C%3B605%3B26%3BRX%3B1%3BP%3B1%3BC2026%2F0605%2FP&amp;nat=or"
            "&amp;mat=or&amp;pcs=Oor&amp;jur=C%2CT%2CF&amp;num=C-605%252F26%2520P",
        )
        assert other != response.text, "фікстура змінилася — підміна не спрацювала"
        return _Response(text=other)


def test_a_listing_whose_first_case_is_a_different_one_is_not_taken_for_the_answer() -> None:
    """Тікет 15: перший ``fiche.jsf`` у переліку — не обов'язково запитана справа.

    Картка чужої справи, видана під запитаним номером, гірша за відмову: юрист
    не має як помітити підміну, бо номер у відповіді — його власний.
    """
    adapter = EuCaseLawAdapter(session=_SuffixSession())  # type: ignore[arg-type]

    result = adapter.public_card("C-605/26")

    assert isinstance(result, SourceUnavailable)
    assert result.reason == "curia_legacy:case_number_mismatch"
    assert "https://juris.curia.europa.eu/juris/liste.jsf?num=C-605/26" in result.manual_path


def test_a_numberless_first_link_does_not_win_over_the_matching_one() -> None:
    """Рев'ю тікета 15: посилання без ``num`` перед потрібним не має вигравати.

    Саме цей шлях тікет і закривав: перше посилання бралося беззастережно.
    Достатньо було, щоб CURIA поставила перед рядком справи будь-яке інше
    посилання на ``fiche.jsf`` без параметра ``num`` — і юрист діставав чужу
    картку під власним номером.
    """
    from sources.curia_legacy import parse_case_list

    body = (_FIXTURES / "C-605-26_liste.html").read_text("utf-8")
    decoy = (
        '<a href="/juris/fiche.jsf?id=C%3B999%3B99%3BRP%3B1%3BP%3B1%3BC2099%2F0999%2FP'
        '&amp;language=en">інша справа</a>'
    )
    with_decoy = body.replace("<body", decoy + "<body", 1)
    assert with_decoy != body

    listing = parse_case_list(with_decoy, "C-605/26")

    assert listing is not None
    assert listing.fiche_id == "C;605;26;RP;1;P;1;C2026/0605/P"
    assert listing.number_mismatch is False
