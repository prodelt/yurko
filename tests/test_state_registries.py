from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.contracts import (
    SourceAdapterError,
    SourceRecordNotFound,
    SourceRequiresHuman,
)
from registries.state_registries import DebtorsRegistry, OpenDataCatalog, ProzorroRegistry

ERB_RESPONSE = {
    "results": [
        {
            "debtorName": "ТОВ ПРИКЛАД",
            "debtorCode": "12345678",
            "vpNum": "67890123",
            "executiveService": "Відділ ДВС",
        },
        {"debtorName": "ТОВ ПРИКЛАД 2", "debtorCode": "87654321"},
    ]
}

CKAN_RESPONSE = {
    "success": True,
    "result": {
        "count": 1,
        "results": [
            {
                "id": "abc-123",
                "name": "single-tax-payers",
                "title": "Реєстр платників єдиного податку",
                "notes": "Офіційний реєстр платників." + "x" * 400,
                "organization": {"title": "ДПС України"},
                "resources": [
                    {"format": "CSV"},
                    {"format": "json"},
                    {"format": "CSV"},
                ],
            }
        ],
    },
}


TENDER_DATA = {
    "id": "a" * 32,
    "tenderID": "UA-2026-05-01-000001-a",
    "title": "Закупівля медичного обладнання",
    "description": "Опис закупівлі. " * 100,
    "status": "active.tendering",
    "procurementMethodType": "aboveThresholdUA",
    "value": {"amount": 1500000.0, "currency": "UAH"},
    "procuringEntity": {
        "name": "КНП Міська лікарня",
        "identifier": {"id": "12345678", "legalName": "КНП Міська лікарня"},
    },
    "dateModified": "2026-05-02T10:00:00+03:00",
    "awards": [{}],
    "documents": [{}, {}],
}


def _json_response(payload: object, status_code: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(
            f"{status_code}", response=response
        )
    else:
        response.raise_for_status.return_value = None
    return response


class TestDebtorsRegistry:
    # Чотири попередні тести розбирали видачу ЄРБ на тілі запиту
    # ``{"searchType", "debtorName", "debtorCode"}``. Живе джерело такого тіла
    # не приймає взагалі (тікет 28), тож тести були зелені рівно доти, доки
    # функція не працювала, — і саме вони ховали дефект. Знято разом із кодом,
    # який вони описували; лишилася межа й причина.

    # -- Тікет 28: ЄРБ живий, але за reCAPTCHA v3 ---------------------------

    def test_search_names_the_captcha_instead_of_calling_the_source_unavailable(self) -> None:
        """Жива проба 16.09.2026: реєстр працює, пошук стереже reCAPTCHA v3.

        Справжній ендпойнт — ``/listDebtorsEndpoint`` із тілом
        ``{searchType, paging, filter, reCaptchaToken}``; без дійсного токена
        сервер відповідає 403 ``{"success":false,"errMsg":"Forbidden"}``.
        Ключ капчі видає сам сайт (``/getConfig``), а перевіряє її сервер.
        «Тимчасово недоступний» тут — хибна порада: чекати нема на що.
        """
        session = MagicMock()
        registry = DebtorsRegistry(session=session)

        with pytest.raises(SourceRequiresHuman) as refusal:
            registry.search(code="00032129")

        said = str(refusal.value)
        assert "reCAPTCHA" in said
        assert "erb.minjust.gov.ua" in said
        session.post.assert_not_called()
        session.get.assert_not_called()

    def test_the_captcha_boundary_does_not_poison_the_backoff(self) -> None:
        """Джерело здорове — блокувати його наступні запити нема за що."""
        registry = DebtorsRegistry(session=MagicMock())

        with pytest.raises(SourceRequiresHuman):
            registry.search(code="00032129")

        assert registry.backoff.is_blocked("search") is False
        assert registry.health().ok is True

    def test_the_api_url_points_at_the_endpoint_the_site_actually_calls(self) -> None:
        """Наш URL вів на ендпойнт, якого фронтенд не кличе.

        ``/api/erbexternal/searchdebtorsexternal`` існує (mORMot відповідає
        JSON 400), але сайт шле запит на ``/listDebtorsEndpoint``. Через це
        відмова читалася як «джерело не працює», хоча не працював запит.
        """
        registry = DebtorsRegistry(session=MagicMock())

        assert registry.health().details["api_url"].endswith("/listDebtorsEndpoint")

    def test_api_url_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ERB_API_URL", "https://mirror.example/api")

        registry = DebtorsRegistry(session=MagicMock())

        assert registry.health().details["api_url"] == "https://mirror.example/api"


class TestProzorroRegistry:
    def test_get_tender_summarizes_cdb_payload(self) -> None:
        session = MagicMock()
        session.get.return_value = _json_response({"data": TENDER_DATA})
        registry = ProzorroRegistry(session=session)

        tender = registry.get_tender("a" * 32)

        assert tender["tender_id"] == "UA-2026-05-01-000001-a"
        assert tender["status"] == "active.tendering"
        assert tender["value_amount"] == 1500000.0
        assert tender["procuring_entity_code"] == "12345678"
        assert tender["awards"] == 1
        assert tender["documents"] == 2
        assert tender["url"] == "https://prozorro.gov.ua/tender/UA-2026-05-01-000001-a"
        assert len(tender["description"]) <= 500
        called_url = session.get.call_args.args[0]
        assert called_url.endswith(f"/tenders/{'a' * 32}")

    def test_get_tender_rejects_unexpected_payload(self) -> None:
        session = MagicMock()
        session.get.return_value = _json_response({"error": "not found"})
        registry = ProzorroRegistry(session=session)

        with pytest.raises(SourceAdapterError):
            registry.get_tender("a" * 32)

    def test_search_parses_result_list(self) -> None:
        session = MagicMock()
        session.post.return_value = _json_response({"data": [TENDER_DATA]})
        registry = ProzorroRegistry(session=session)

        result = registry.search("медичне обладнання")

        assert result["found"] == 1
        assert result["results"][0]["tender_id"] == "UA-2026-05-01-000001-a"
        assert session.post.call_args.kwargs["json"] == {"text": "медичне обладнання"}

    def test_search_wraps_errors_and_backs_off(self) -> None:
        session = MagicMock()
        session.post.side_effect = RuntimeError("boom")
        registry = ProzorroRegistry(session=session)

        with pytest.raises(SourceAdapterError):
            registry.search("обладнання")
        assert registry.health().ok is False

    # -- Тікет 24: «такого тендера немає» ≠ «джерело недоступне" ------------

    def test_a_tender_that_does_not_exist_is_not_an_outage(self) -> None:
        """Жива проба 16.09.2026: CDB на невідомий id відповідає 404 із тілом
        ``{"status":"error","errors":[{"name":"tender_id","description":"Not Found"}]}``.

        Це відповідь джерела, а не його мовчання. Видавати її за недоступність —
        значить сказати юристові «спробуйте пізніше» там, де пробувати нічого:
        такого тендера немає й не буде.
        """
        session = MagicMock()
        session.get.return_value = _json_response(
            {"status": "error", "errors": [{"name": "tender_id", "description": "Not Found"}]},
            status_code=404,
        )
        registry = ProzorroRegistry(session=session)

        with pytest.raises(SourceRecordNotFound):
            registry.get_tender("a" * 32)

    def test_a_missing_tender_does_not_poison_the_backoff(self) -> None:
        """Джерело здорове — питати його далі можна.

        Доти помилковий id блокував адаптер нарівні зі збоєм мережі: наступні
        запити падали в backoff, хоча Prozorro відповідав нормально.
        """
        session = MagicMock()
        session.get.return_value = _json_response(
            {"status": "error", "errors": [{"description": "Not Found"}]}, status_code=404
        )
        registry = ProzorroRegistry(session=session)

        with pytest.raises(SourceRecordNotFound):
            registry.get_tender("a" * 32)

        assert registry.backoff.is_blocked("a" * 32) is False
        assert registry.health().ok is True

    def test_a_real_outage_is_still_an_outage(self) -> None:
        session = MagicMock()
        session.get.return_value = _json_response({}, status_code=503)
        registry = ProzorroRegistry(session=session)

        with pytest.raises(SourceAdapterError) as failure:
            registry.get_tender("a" * 32)
        assert not isinstance(failure.value, SourceRecordNotFound)
        assert registry.backoff.is_blocked("a" * 32) is True

    # -- Тікет 24: пошук порталу приймає POST, а не GET ---------------------

    def test_search_posts_the_query_because_the_portal_refuses_get(self) -> None:
        """Жива проба 16.09.2026: ``GET /api/search/tenders`` → 405 Method Not
        Allowed (HTML), ``POST`` із тілом ``{"text": …}`` → 200 і видача.

        Пошук не працював зовсім, і це ховалося за тією самою відмовою
        «джерело тимчасово недоступне».
        """
        session = MagicMock()
        session.post.return_value = _json_response(
            {"page": 0, "per_page": 20, "total": 1, "data": [TENDER_DATA]}
        )
        registry = ProzorroRegistry(session=session)

        result = registry.search("UA-2026-05-01-000001-a")

        session.get.assert_not_called()
        assert session.post.call_args.kwargs["json"] == {"text": "UA-2026-05-01-000001-a"}
        assert result["found"] == 1
        assert result["results"][0]["tender_id"] == "UA-2026-05-01-000001-a"

    def test_an_empty_search_is_an_empty_answer_not_a_failure(self) -> None:
        """``total: 0`` — джерело сказало «немає», і це повна, чесна відповідь."""
        session = MagicMock()
        session.post.return_value = _json_response({"page": 0, "total": 0, "data": []})
        registry = ProzorroRegistry(session=session)

        result = registry.search("UA-0000-00-00-000000-a")

        assert result["found"] == 0
        assert result["results"] == []
        assert registry.health().ok is True

    def test_the_coverage_map_no_longer_calls_prozorro_unreachable(self) -> None:
        """Карта казала «джерело недоступне», а джерело відповідало.

        Причина була не в Prozorro: пошуковий хост не стояв у списку сторожа
        виходу, а `get_tender` узагалі не мав оголошеної можливості, хоча
        працював. Жива проба 16.09.2026 (тікет 24) закриває обидві розбіжності.
        """
        from core import legal_orders

        legal_orders.register_default_sources()
        caps = {
            cap.operation: cap
            for cap in legal_orders.registered_capabilities()
            if cap.source_id == "ua_prozorro"
        }

        search = caps[legal_orders.Operation.SEARCH_BY_IDENTIFIER]
        assert search.layer is legal_orders.CoverageLayer.REACHABLE_BY_ID
        assert search.verified_at == "2026-09-16"
        assert "недоступне" not in search.limitations

        read = caps[legal_orders.Operation.READ_DOCUMENT]
        assert read.layer is legal_orders.CoverageLayer.REACHABLE_BY_ID
        assert read.verified_at == "2026-09-16"

    def test_the_search_host_is_declared_to_the_egress_guard(self) -> None:
        """Пошук і читання Prozorro живуть на різних хостах одного видавця.

        У списку сторожа стояв лише ``public-api.prozorro.gov.ua``, тож пошук
        падав ще до мережі — а причину карта покриття записала як «джерело
        віддає 503». Той самий випадок, що з читачем ФРН: «у мене працює»
        розходилося з продуктом.
        """
        from urllib.parse import urlparse

        from core import egress
        from registries.state_registries import _PROZORRO_SEARCH_DEFAULT_URL

        host = urlparse(_PROZORRO_SEARCH_DEFAULT_URL).hostname or ""

        assert host in egress.allowed_hosts(), f"{host} не оголошено сторожу виходу"

    def test_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROZORRO_API_URL", "https://api.example/v2/")
        monkeypatch.setenv("PROZORRO_SEARCH_URL", "https://search.example/tenders")

        registry = ProzorroRegistry(session=MagicMock())

        details = registry.health().details
        assert details["api_url"] == "https://api.example/v2"
        assert details["search_url"] == "https://search.example/tenders"


class TestOpenDataCatalog:
    def test_search_normalizes_ckan_datasets(self) -> None:
        session = MagicMock()
        session.get.return_value = _json_response(CKAN_RESPONSE)
        catalog = OpenDataCatalog(session=session)

        result = catalog.search("платники податку")

        assert result["found"] == 1
        dataset = result["results"][0]
        assert dataset["title"] == "Реєстр платників єдиного податку"
        assert dataset["publisher"] == "ДПС України"
        assert dataset["url"] == "https://data.gov.ua/dataset/single-tax-payers"
        assert dataset["formats"] == ["CSV", "JSON"]
        assert len(dataset["description"]) <= 300

    def test_search_passes_query_params(self) -> None:
        session = MagicMock()
        session.get.return_value = _json_response(CKAN_RESPONSE)
        catalog = OpenDataCatalog(session=session)

        catalog.search("реєстр", max_results=7)

        params = session.get.call_args.kwargs["params"]
        assert params == {"q": "реєстр", "rows": 7}

    def test_search_tolerates_malformed_payload(self) -> None:
        session = MagicMock()
        session.get.return_value = _json_response({"success": True, "result": {}})
        catalog = OpenDataCatalog(session=session)

        result = catalog.search("реєстр")

        assert result["found"] == 0

    def test_search_wraps_errors(self) -> None:
        session = MagicMock()
        session.get.side_effect = RuntimeError("boom")
        catalog = OpenDataCatalog(session=session)

        with pytest.raises(SourceAdapterError):
            catalog.search("реєстр")
        assert catalog.health().ok is False
