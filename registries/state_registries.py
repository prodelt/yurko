"""Adapters for state registries beyond legislation.

- ``DebtorsRegistry`` — Єдиний реєстр боржників (erb.minjust.gov.ua), the
  official enforcement-debtors register. Live data, never cached.
- ``OpenDataCatalog`` — the national open-data portal (data.gov.ua, standard
  CKAN API) for discovering any official registry/dataset beyond the built-in
  adapters.
- ``ProzorroRegistry`` — public procurement (Prozorro): the official central
  database API for tender data plus the portal search API for free-text /
  company-code lookup.

Government API shapes drift, so every endpoint is overridable via env
(``ERB_API_URL``, ``DATA_GOV_UA_API_URL``, ``PROZORRO_API_URL``,
``PROZORRO_SEARCH_URL``) and responses are parsed defensively: an unexpected
payload degrades to a typed error, never a crash.
"""

from __future__ import annotations

import datetime as dt
import os
from datetime import timezone
from typing import Any

import requests

from core.contracts import (
    SourceAdapterError,
    SourceRecordNotFound,
    SourceRequiresHuman,
    SourceHealth,
    SourcePolicy,
)
from registries.scraper import BackoffSentinel
from sources.transport import SourceTransport

_USER_AGENT = "Mozilla/5.0 ukraine-laws-mcp/2"

#: Ендпойнт, який справді кличе фронтенд ЄРБ (`app/services/SearchRegistryFactory.js`),
#: з тілом ``{searchType, paging, filter, reCaptchaToken}``. Наш попередній URL —
#: ``/api/erbexternal/searchdebtorsexternal`` — існує (mORMot відповідає JSON 400),
#: але сайт його не кличе, тому відмова читалася як «джерело не працює», хоча не
#: працював запит (жива проба 16.09.2026, тікет 28).
_ERB_DEFAULT_URL = "https://erb.minjust.gov.ua/listDebtorsEndpoint"

#: Чому пошуку немає. Сайт бере ключ капчі з власного ``/getConfig``
#: (reCAPTCHA v3, ``grecaptcha.execute``) і кладе токен у тіло запиту; сервер
#: його перевіряє й без дійсного відповідає 403
#: ``{"success":false,"errMsg":"Forbidden"}`` — однаково на відсутній, порожній
#: і підроблений токен. Капчу за людину не розв'язуємо (тікет 05).
_ERB_CAPTCHA_REFUSAL = (
    "Пошук у ЄРБ стереже reCAPTCHA v3: сервер erb.minjust.gov.ua перевіряє токен "
    "і без дійсного відповідає 403 (жива проба 16.09.2026). Капчу за людину не "
    "розв'язуємо, тож машинного пошуку боржника немає. Ручний шлях: "
    "https://erb.minjust.gov.ua — код юрособи або РНОКПП у форму пошуку в браузері"
)
_DATA_GOV_UA_DEFAULT_URL = "https://data.gov.ua/api/3/action/package_search"
_PROZORRO_API_DEFAULT_URL = "https://public-api.prozorro.gov.ua/api/2.5"
_PROZORRO_SEARCH_DEFAULT_URL = "https://prozorro.gov.ua/api/search/tenders"


def _coerce_result_list(payload: Any) -> list[dict[str, Any]]:
    """Pull a list of result dicts out of common API envelope shapes."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("results", "items", "data", "list"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


class DebtorsRegistry:
    """Search the official enforcement-debtors register (ЄРБ)."""

    name = "debtors_registry"
    source_policy = SourcePolicy.API

    def __init__(self, session: Any | None = None, request_timeout: int = 25) -> None:
        self._api_url = os.getenv("ERB_API_URL", _ERB_DEFAULT_URL)
        #: Мережа — лише через спільний транспорт: скинуте після простою
        #: пулове з'єднання інакше стає хибним «джерело недоступне»
        #: (живий збій 15.09.2026, :mod:`sources.transport`).
        self._transport = SourceTransport(session or requests.Session())
        self._request_timeout = request_timeout
        self.backoff = BackoffSentinel()

    def search(self, name: str = "", code: str = "", max_results: int = 10) -> dict[str, Any]:
        # Відмова відома наперед, тому в мережу не йдемо: кожен запит без токена
        # капчі — гарантовані 403 і зайве навантаження на чужий сервер. Backoff
        # теж не чіпаємо: джерело здорове, блокувати його нема за що.
        raise SourceRequiresHuman(_ERB_CAPTCHA_REFUSAL)

    def health(self) -> SourceHealth:
        return SourceHealth(
            adapter=self.name,
            ok=not self.backoff.is_blocked("search"),
            source_policy=self.source_policy,
            details={
                "api_url": self._api_url,
                "search_backoff": self.backoff.status("search"),
            },
        )


class ProzorroRegistry:
    """Public procurement data: official Prozorro CDB API + portal search."""

    name = "prozorro"
    source_policy = SourcePolicy.API

    def __init__(self, session: Any | None = None, request_timeout: int = 25) -> None:
        self._api_url = os.getenv("PROZORRO_API_URL", _PROZORRO_API_DEFAULT_URL).rstrip("/")
        self._search_url = os.getenv("PROZORRO_SEARCH_URL", _PROZORRO_SEARCH_DEFAULT_URL)
        #: Мережа — лише через спільний транспорт: скинуте після простою
        #: пулове з'єднання інакше стає хибним «джерело недоступне»
        #: (живий збій 15.09.2026, :mod:`sources.transport`).
        self._transport = SourceTransport(session or requests.Session())
        self._request_timeout = request_timeout
        self.backoff = BackoffSentinel()

    def search(self, query: str, max_results: int = 10) -> dict[str, Any]:
        if self.backoff.is_blocked("search"):
            raise SourceAdapterError(f"{self.name} search is in backoff")

        try:
            # POST, а не GET: пошук порталу на GET відповідає 405 Method Not
            # Allowed сторінкою HTML (жива проба 16.09.2026, тікет 24), і пошук
            # не працював зовсім. Запит нічого не змінює, тому обірване
            # з'єднання транспорту дозволено повторити.
            response = self._transport.post(
                self._search_url,
                safe_to_repeat=True,
                json={"text": query},
                headers={"User-Agent": _USER_AGENT},
                timeout=self._request_timeout,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as error:
            self.backoff.mark_failed("search")
            raise SourceAdapterError(f"{self.name} search failed") from error

        self.backoff.mark_success("search")
        results = [self._summarize_tender(item) for item in _coerce_result_list(body)[:max_results]]
        return {
            "query": query,
            "results": results,
            "found": len(results),
            "source": self._search_url,
            "retrieved_at": dt.datetime.now(timezone.utc).isoformat(),
        }

    def get_tender(self, tender_id: str) -> dict[str, Any]:
        tender_id = str(tender_id).strip()
        if self.backoff.is_blocked(tender_id):
            raise SourceAdapterError(f"{self.name} is in backoff for {tender_id}")

        try:
            response = self._transport.get(
                f"{self._api_url}/tenders/{tender_id}",
                headers={"User-Agent": _USER_AGENT},
                timeout=self._request_timeout,
            )
            # 404 від CDB — відповідь, а не мовчання: у тілі стоїть
            # ``{"errors":[{"name":"tender_id","description":"Not Found"}]}``.
            # Джерело здорове, тому backoff тут не чіпається: інакше один
            # помилковий ідентифікатор блокував би наступні запити.
            if response.status_code == 404:
                self.backoff.mark_success(tender_id)
                raise SourceRecordNotFound(f"{self.name} has no tender {tender_id}")
            response.raise_for_status()
            body = response.json()
        except SourceRecordNotFound:
            raise
        except Exception as error:
            self.backoff.mark_failed(tender_id)
            raise SourceAdapterError(f"{self.name} failed to fetch tender {tender_id}") from error

        self.backoff.mark_success(tender_id)
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise SourceAdapterError(f"{self.name} returned an unexpected payload for {tender_id}")
        summary = self._summarize_tender(data)
        return {
            **summary,
            "internal_id": data.get("id"),
            "source": self._api_url,
            "retrieved_at": dt.datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _summarize_tender(item: dict[str, Any]) -> dict[str, Any]:
        # Both the CDB API and the portal search return tender-shaped dicts;
        # full tenders can be hundreds of KB, so keep a compact legal summary.
        tender_id = item.get("tenderID") or item.get("tenderId") or ""
        value = item.get("value") or {}
        entity = item.get("procuringEntity") or {}
        entity_id = entity.get("identifier") or {}
        return {
            "tender_id": tender_id or None,
            "title": item.get("title"),
            "status": item.get("status"),
            "description": str(item.get("description") or "")[:500],
            "procurement_method": item.get("procurementMethodType")
            or item.get("procurementMethod"),
            "value_amount": value.get("amount"),
            "value_currency": value.get("currency"),
            "procuring_entity": entity.get("name")
            or (entity.get("identifier") or {}).get("legalName"),
            "procuring_entity_code": entity_id.get("id"),
            "date_modified": item.get("dateModified"),
            "awards": len(item.get("awards") or []),
            "documents": len(item.get("documents") or []),
            "url": f"https://prozorro.gov.ua/tender/{tender_id}" if tender_id else None,
        }

    def health(self) -> SourceHealth:
        return SourceHealth(
            adapter=self.name,
            ok=not self.backoff.is_blocked("search"),
            source_policy=self.source_policy,
            details={
                "api_url": self._api_url,
                "search_url": self._search_url,
                "search_backoff": self.backoff.status("search"),
            },
        )


class OpenDataCatalog:
    """Discover official registries/datasets on data.gov.ua (CKAN API)."""

    name = "open_data_catalog"
    source_policy = SourcePolicy.API

    def __init__(self, session: Any | None = None, request_timeout: int = 25) -> None:
        self._api_url = os.getenv("DATA_GOV_UA_API_URL", _DATA_GOV_UA_DEFAULT_URL)
        #: Мережа — лише через спільний транспорт: скинуте після простою
        #: пулове з'єднання інакше стає хибним «джерело недоступне»
        #: (живий збій 15.09.2026, :mod:`sources.transport`).
        self._transport = SourceTransport(session or requests.Session())
        self._request_timeout = request_timeout
        self.backoff = BackoffSentinel()

    def search(self, query: str, max_results: int = 10) -> dict[str, Any]:
        if self.backoff.is_blocked("search"):
            raise SourceAdapterError(f"{self.name} is in backoff")

        params: dict[str, str | int] = {"q": query, "rows": max_results}
        try:
            response = self._transport.get(
                self._api_url,
                params=params,
                headers={"User-Agent": _USER_AGENT},
                timeout=self._request_timeout,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as error:
            self.backoff.mark_failed("search")
            raise SourceAdapterError(f"{self.name} search failed") from error

        self.backoff.mark_success("search")
        results = [
            self._normalize_dataset(item)
            for item in _coerce_result_list((body or {}).get("result", {}))[:max_results]
        ]
        return {
            "query": query,
            "results": results,
            "found": len(results),
            "source": self._api_url,
            "retrieved_at": dt.datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _normalize_dataset(item: dict[str, Any]) -> dict[str, Any]:
        organization = item.get("organization") or {}
        resources = item.get("resources") or []
        slug = item.get("name") or item.get("id") or ""
        return {
            "id": item.get("id"),
            "title": item.get("title") or slug,
            "description": str(item.get("notes") or "")[:300],
            "publisher": organization.get("title"),
            "url": f"https://data.gov.ua/dataset/{slug}" if slug else None,
            "resources": len(resources) if isinstance(resources, list) else 0,
            "formats": sorted(
                {
                    str(resource.get("format", "")).upper()
                    for resource in resources
                    if isinstance(resource, dict) and resource.get("format")
                }
            ),
        }

    def health(self) -> SourceHealth:
        return SourceHealth(
            adapter=self.name,
            ok=not self.backoff.is_blocked("search"),
            source_policy=self.source_policy,
            details={
                "api_url": self._api_url,
                "search_backoff": self.backoff.status("search"),
            },
        )
