"""Транспорт джерела: з'єднання, що пережило простій, не валить читання.

Живий виклик 15.09.2026: ``get_article(eu, <CELEX>, 5, FR)`` після 44 хвилин
простою тієї самої сесії Cellar (попередній ``query_law`` EU о 13:10) відповів
через 21 с ``source_unavailable``, а ``get_case`` за чотири секунди до того —
окремою, свіжою сесією до того самого хоста — успішно. Пулове keep-alive
з'єднання, яке мережа по дорозі мовчки скинула, ``requests`` перевикористовує
без жодної повторної спроби. Мережа тут не використовується: сесія підмінена
моделлю такого пулу.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest
import requests

from sources.transport import SourceTransport


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _PooledSession:
    """Сесія з одним keep-alive з'єднанням, яке мережа скидає після простою.

    Скинуте з'єднання не видно, доки ним не скористаються: запит по ньому
    падає ``ConnectionError`` (у живому виклику — після 21 с ретрансмісій TCP).
    ``close()`` закриває пул, і наступний запит відкриває свіже з'єднання.
    """

    def __init__(self, clock: _Clock, *, dropped_after: float) -> None:
        self._clock = clock
        self._dropped_after = dropped_after
        self._open_since_last_use: float | None = None
        self.attempts: list[str] = []

    def get(self, url: str, **kwargs: Any) -> str:
        last = self._open_since_last_use
        if last is not None and self._clock.now - last > self._dropped_after:
            self._open_since_last_use = None
            self.attempts.append("stale")
            raise requests.ConnectionError("Connection aborted: WinError 10054")
        self.attempts.append("ok")
        self._open_since_last_use = self._clock.now
        return "response"

    post = get

    def close(self) -> None:
        self._open_since_last_use = None


def test_request_after_long_idle_does_not_use_the_dropped_connection() -> None:
    clock = _Clock()
    session = _PooledSession(clock, dropped_after=300)
    transport = SourceTransport(session, clock=clock)  # type: ignore[arg-type]

    transport.get("https://publications.europa.eu/resource/celex/32014R8888")
    clock.now += 44 * 60
    response = transport.get("https://publications.europa.eu/resource/celex/32014R8888")

    assert response == "response"
    # Жодної спроби по скинутому з'єднанню: саме вона коштувала 21 с очікування.
    assert session.attempts == ["ok", "ok"]


def test_connection_dropped_within_idle_budget_is_retried_once_on_a_fresh_one() -> None:
    """Мережа може скинути з'єднання й раніше за межу простою (зміна VPN)."""
    clock = _Clock()
    session = _PooledSession(clock, dropped_after=5)
    transport = SourceTransport(session, clock=clock)  # type: ignore[arg-type]

    transport.get("https://publications.europa.eu/webapi/rdf/sparql")
    clock.now += 30
    response = transport.get("https://publications.europa.eu/webapi/rdf/sparql")

    assert response == "response"
    assert session.attempts == ["ok", "stale", "ok"]


class _ScriptedSession:
    def __init__(self, outcomes: list[BaseException | str]) -> None:
        self._outcomes = list(outcomes)
        self.attempts = 0

    def get(self, url: str, **kwargs: Any) -> str:
        self.attempts += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    post = get


def test_second_connection_error_reaches_the_caller() -> None:
    session = _ScriptedSession([requests.ConnectionError("a"), requests.ConnectionError("b")])
    transport = SourceTransport(session)  # type: ignore[arg-type]

    with pytest.raises(requests.ConnectionError):
        transport.get("https://publications.europa.eu/webapi/rdf/sparql")
    assert session.attempts == 2


@pytest.mark.parametrize(
    "error", [requests.ReadTimeout("slow"), requests.ConnectTimeout("unreachable")]
)
def test_timeout_is_not_repeated(error: requests.RequestException) -> None:
    """Джерело, що мовчить увесь тайм-аут, мовчатиме й удруге — подвоювати очікування не можна."""
    session = _ScriptedSession([error, "response"])
    transport = SourceTransport(session)  # type: ignore[arg-type]

    with pytest.raises(requests.Timeout):
        transport.get("https://publications.europa.eu/webapi/rdf/sparql")
    assert session.attempts == 1


# ---------------------------------------------------------------------------
# Тікет 15: три однакові помічники читачів Cellar живуть в одному місці
# ---------------------------------------------------------------------------


def test_both_cellar_readers_take_the_helpers_from_the_transport_module() -> None:
    """``_unavailable``/``_get``/``_sparql_bindings`` були скопійовані дослівно.

    Копія відмови коштувала б рівно того, що вже сталося з транспортом: правило
    полагодили в одному читачі й не помітили другого. Тепер обидва беруть ті
    самі функції з :mod:`sources.transport`.
    """
    from sources.eu_case_law import EuCaseLawAdapter
    from sources.eu_law import EuLawAdapter
    from sources.transport import CellarReader

    assert issubclass(EuLawAdapter, CellarReader)
    assert issubclass(EuCaseLawAdapter, CellarReader)
    for name in ("_unavailable", "_get", "_sparql_bindings"):
        assert getattr(EuLawAdapter, name) is getattr(CellarReader, name), name
        assert getattr(EuCaseLawAdapter, name) is getattr(CellarReader, name), name


def test_a_dropped_connection_becomes_a_named_source_unavailable() -> None:
    """Причина відмови називає тип винятку, а не лише час наступної спроби."""
    from core.contracts import SourceUnavailable
    from sources.transport import CellarReader

    class _Reader(CellarReader):
        source_id = "eu_law_eurlex_cellar"

        def __init__(self) -> None:
            self._transport = SourceTransport(
                _ScriptedSession(  # type: ignore[arg-type]
                    [requests.ConnectionError("a"), requests.ConnectionError("b")]
                )
            )

    failure = _Reader()._get("https://publications.europa.eu/webapi/rdf/sparql")
    assert isinstance(failure, SourceUnavailable)
    assert failure.reason == "network_error:ConnectionError"
    assert failure.source == "eu_law_eurlex_cellar"


# ---------------------------------------------------------------------------
# Тікет 15: повтор — про скинуте з'єднання, а не про будь-який ConnectionError
# ---------------------------------------------------------------------------


def _wrapped(cause: BaseException) -> requests.ConnectionError:
    """``requests`` загортає причину так, як її бачить викликач (жива проба)."""
    error = requests.ConnectionError("Max retries exceeded")
    error.__cause__ = cause
    return error


@pytest.mark.parametrize(
    "cause",
    [
        __import__("socket").gaierror(11001, "getaddrinfo failed"),
        ConnectionRefusedError(61, "Connection refused"),
    ],
    ids=["dns", "refused"],
)
def test_a_name_that_does_not_resolve_and_a_refused_port_are_not_retried(
    cause: BaseException,
) -> None:
    """Хост, якого немає, і закритий порт — відповідь мережі, а не випадковість.

    Повтор тут подвоює очікування перед певною відмовою й нічого не рятує:
    ``urllib3`` до цього вже вичерпав власні спроби (``MaxRetryError``).
    Повторюється лише з'єднання, обірване посеред запиту.
    """
    session = _ScriptedSession([_wrapped(cause), "response"])
    transport = SourceTransport(session)  # type: ignore[arg-type]

    with pytest.raises(requests.ConnectionError):
        transport.get("https://publications.europa.eu/webapi/rdf/sparql")
    assert session.attempts == 1


def test_a_connection_dropped_mid_request_is_still_retried() -> None:
    """Межа звужена, але не прибрана: скинуте пулове з'єднання повторюється."""
    session = _ScriptedSession([requests.ConnectionError("dropped"), "response"])
    transport = SourceTransport(session)  # type: ignore[arg-type]

    assert transport.get("https://publications.europa.eu/webapi/rdf/sparql") == "response"
    assert session.attempts == 2


# ---------------------------------------------------------------------------
# Тікет 27: POST повторюється лише з дозволу того, хто знає зміст запиту
# ---------------------------------------------------------------------------


def test_post_is_not_repeated_unless_the_caller_says_it_is_safe() -> None:
    """Обірватися могло вже після того, як сервер прийняв запит.

    Транспорт цього не знає, тому за замовчуванням не вирішує за викликача:
    повтор зробив би роботу двічі.
    """
    session = _ScriptedSession([requests.ConnectionError("dropped"), "response"])
    transport = SourceTransport(session)  # type: ignore[arg-type]

    with pytest.raises(requests.ConnectionError):
        transport.post("https://example.invalid/search")
    assert session.attempts == 1


def test_a_search_post_that_changes_nothing_is_retried_once() -> None:
    session = _ScriptedSession([requests.ConnectionError("dropped"), "response"])
    transport = SourceTransport(session)  # type: ignore[arg-type]

    assert transport.post("https://example.invalid/search", safe_to_repeat=True) == "response"
    assert session.attempts == 2


def test_post_does_not_repeat_a_settled_refusal_even_when_allowed() -> None:
    """Імені, якого немає, повтор не вигадає — лише подвоїть очікування."""
    session = _ScriptedSession([_wrapped(socket.gaierror("no such host")), "response"])
    transport = SourceTransport(session)  # type: ignore[arg-type]

    with pytest.raises(requests.ConnectionError):
        transport.post("https://example.invalid/search", safe_to_repeat=True)
    assert session.attempts == 1


def test_post_after_long_idle_also_opens_a_fresh_connection() -> None:
    """Скидання простояного пулу — про з'єднання, а не про метод запиту."""
    clock = _Clock()
    session = _PooledSession(clock, dropped_after=300)
    transport = SourceTransport(session, idle_reset_seconds=60, clock=clock)  # type: ignore[arg-type]

    transport.post("https://example.invalid/search")
    clock.now += 2640
    transport.post("https://example.invalid/search")

    assert session.attempts == ["ok", "ok"], "простояне з'єднання не мало піти в діло"
