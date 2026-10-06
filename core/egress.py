"""Исходящий трафик: белый список хостов, схем и редиректов (T125, ADR 0009).

Инструкция не устраняет сетевой путь — устраняет только код. Этот модуль
ставит один сторож на все HTTP-запросы процесса через ``requests``:

* хост обязан входить в белый список официальных источников (адреса из
  :mod:`source_endpoints` плюс хосты, объявленные адаптерами);
* схема — только ``https``;
* адреса-литералы, loopback и приватные сети запрещены (SSRF);
* редиректы не следуют автоматически: каждая цель проверяется тем же правилом,
  не более пяти переходов;
* ответ сохраняется как данные, не как инструкции — это обязанность вызывающего,
  здесь фиксируется только граница получателя.

Свободный текст пользователя сторож не распознаёт — это задача профиля
(:mod:`profile`) и белых списков полей в каждом адаптере (ADR 0009, H2).
"""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from core import source_endpoints

logger = logging.getLogger("ukraine-laws")

__all__ = [
    "EgressDenied",
    "allow_host",
    "allowed_hosts",
    "assert_identifier",
    "check_url",
    "install_guard",
    "upgrade_scheme",
]


class EgressDenied(requests.RequestException):
    """Запрос отклонён сторожем исходящего трафика (не сбой источника)."""


#: Хосты официальных источников, объявленные адаптерами сверх source_endpoints.
_EXTRA_HOSTS: set[str] = {
    "eur-lex.europa.eu",
    "curia.europa.eu",
    "www.icj-cij.org",
    "icj-cij.org",
    "api.icj-cij.org",
    "reyestr.court.gov.ua",
    "erb.minjust.gov.ua",
    "data.gov.ua",
    "public-api.prozorro.gov.ua",
    # Пошук і читання Prozorro живуть на різних хостах одного видавця: картку
    # тендера віддає CDB `public-api…`, а пошук за номером — портал
    # `prozorro.gov.ua/api/search/tenders`. Другого в списку не було, тож пошук
    # не доходив до мережі взагалі, а карта покриття записала причину як «503
    # від джерела» (тікет 24, жива проба 16.09.2026).
    "prozorro.gov.ua",
    "public.api.openprocurement.org",
    "zakon.rada.gov.ua",
    "data.rada.gov.ua",
}

#: Хосты, которым разрешён http без TLS. Сейчас таких нет: все источники
#: отвечают по https.
_HTTP_ALLOWED_HOSTS: set[str] = set()

_lock = threading.Lock()
_installed = False
_original_request: Any = None


def allow_host(host: str) -> None:
    """Объявить хост официального источника (вызывается адаптером при импорте)."""
    cleaned = str(host or "").strip().lower()
    if cleaned:
        with _lock:
            _EXTRA_HOSTS.add(cleaned)


def allowed_hosts() -> frozenset[str]:
    hosts: set[str] = set()
    for url in source_endpoints.all_endpoints().values():
        host = (urlparse(url).hostname or "").lower()
        if host:
            hosts.add(host)
    with _lock:
        hosts.update(_EXTRA_HOSTS)
    return frozenset(hosts)


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def check_url(url: str) -> str:
    """Вернуть URL, если он допустим, иначе поднять :class:`EgressDenied`."""
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").lower()
    if not host:
        raise EgressDenied(f"egress denied: URL без хоста ({url!r})")
    if _is_ip_literal(host):
        raise EgressDenied(f"egress denied: адреса-літерали заборонені ({host})")
    if host in ("localhost",) or host.endswith(".local") or host.endswith(".internal"):
        raise EgressDenied(f"egress denied: локальний хост ({host})")
    if parsed.scheme == "http":
        if host not in _HTTP_ALLOWED_HOSTS:
            raise EgressDenied(f"egress denied: лише https ({url!r})")
    elif parsed.scheme != "https":
        raise EgressDenied(f"egress denied: схема {parsed.scheme!r} заборонена")
    if host not in allowed_hosts():
        raise EgressDenied(f"egress denied: хост {host} не в білому списку офіційних джерел")
    return url


_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/()\-]{0,199}$")


def assert_identifier(value: str, *, pattern: re.Pattern[str] | None = None) -> str:
    """Проверить, что наружу уходит структурный идентификатор, а не текст.

    Белый список поля адаптера (ADR 0009, H2): CELEX/ELI/ECLI/номер/дата.
    Пробелы и произвольные фразы не проходят — такие значения адаптер обязан
    отвергнуть до сети.
    """
    text = str(value or "").strip()
    rule = pattern or _IDENTIFIER_RE
    if not rule.match(text):
        raise EgressDenied(
            f"egress denied: значення {text[:40]!r} не є структурним ідентифікатором"
        )
    return text


_MAX_REDIRECTS = 5


def upgrade_scheme(url: str) -> str:
    """Поднять ``http`` до ``https`` для хоста, который обслуживает оба.

    Cellar на согласование содержания отвечает ``303`` с ``Location`` на
    ``http://publications.europa.eu/...`` — тот же ресурс доступен по ``https``
    (живая проба 2026-09-07). Понижать схему нельзя, поэтому вместо отказа
    цель поднимается: незашифрованный переход не выполняется ни разу, а
    источник остаётся читаемым. Хост, у которого измерен только ``http``,
    остаётся как есть.
    """
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "http" or not host or host in _HTTP_ALLOWED_HOSTS:
        return url
    if host not in allowed_hosts():
        return url
    return parsed._replace(scheme="https").geturl()


def _guarded_request(self: requests.Session, method: str, url: str, **kwargs: Any) -> Any:
    """Обёртка ``requests.Session.request``: проверка цели и ручные редиректы."""
    check_url(url)
    follow = kwargs.pop("allow_redirects", True)
    kwargs["allow_redirects"] = False
    response = _original_request(self, method, url, **kwargs)
    hops = 0
    while follow and response.is_redirect and hops < _MAX_REDIRECTS:
        location = response.headers.get("location", "")
        if not location:
            break
        target = upgrade_scheme(urljoin(response.url, location))
        check_url(target)
        hops += 1
        next_method = (
            "GET" if response.status_code in (301, 302, 303) and method != "HEAD" else method
        )
        if next_method == "GET":
            kwargs.pop("data", None)
            kwargs.pop("json", None)
        response = _original_request(self, next_method, target, **kwargs)
    return response


def install_guard() -> None:
    """Поставить сторож на ``requests.Session.request`` для всего процесса."""
    global _installed, _original_request
    with _lock:
        if _installed:
            return
        _original_request = requests.Session.request
        requests.Session.request = _guarded_request  # type: ignore[assignment,method-assign]
        _installed = True
    logger.info("egress guard installed: %d allowed hosts", len(allowed_hosts()))
