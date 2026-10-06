"""HTTP-транспорт джерела: пулове з'єднання не переживає довгого простою.

Живий виклик 15.09.2026: ``get_article(eu, <CELEX>, 5, FR)`` через 21 с
відповів ``source_unavailable``, хоча ``get_case`` за чотири секунди до того
дістався того самого хоста Cellar. Різниця була одна: у читача права ЄС уже
була сесія ``requests`` з keep-alive-з'єднанням від ``query_law`` 44 хвилини
тому, а в читача практики — ні. Мережа по дорозі (NAT, VPN, міжмережевий
екран) мовчки забуває простояне з'єднання; ``urllib3`` перевіряє перед
повторним використанням лише те, чи не закрив його сервер, а мовчазне
скидання побачити не може. Запит іде в мертве з'єднання, Windows 21 с
ретрансмітить і здається, а ``requests`` за замовчуванням не повторює нічого.

Звідси два правила, і обидва — про з'єднання, а не про джерело:

* після простою, довшого за :data:`IDLE_RESET_SECONDS`, пул закривається до
  запиту, і запит іде свіжим з'єднанням (коштує TLS-рукостискання, а не 21 с);
* ``ConnectionError`` посеред запиту (з'єднання обірване, а не джерело
  відмовило) повторюється один раз свіжим з'єднанням. Тайм-аут не
  повторюється: джерело, що мовчить 30 с, мовчатиме й удруге. Не
  повторюються й остаточні відповіді мережі — ім'я, якого немає (DNS), і
  закритий порт: ``urllib3`` до того вже вичерпав власні спроби, і повтор
  лише подвоїть очікування перед певною відмовою.

Відповідь джерела (будь-який HTTP-код) транспорт не тлумачить — це справа
адаптера.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any, Callable

import requests

from core.contracts import SourceUnavailable
from core.source_endpoints import endpoint

__all__ = [
    "IDLE_RESET_SECONDS",
    "CellarReader",
    "SourceTransport",
    "is_settled_refusal",
]

#: Скільки секунд простою пулове з'єднання вважається живим. Жива проба
#: 15.09.2026: Cellar тримає keep-alive щонайменше 120 с, а мовчазне скидання
#: 44-хвилинного з'єднання доведене відмовою. Межа береться знизу — зайве
#: рукостискання дешевше за 21 с очікування й хибний ``source_unavailable``.
IDLE_RESET_SECONDS = 60.0

#: Причини, за яких повторювати нічого: мережа вже відповіла остаточно.
#: ``gaierror`` — імені не існує (DNS), ``ConnectionRefusedError`` — порт
#: закритий. Жива проба 16.09.2026 показує, як ``requests`` їх подає:
#: ``ConnectionError → MaxRetryError → NameResolutionError → gaierror`` і
#: ``ConnectionError → MaxRetryError → NewConnectionError → ConnectionRefusedError``.
_SETTLED_REFUSALS: tuple[type[BaseException], ...] = (socket.gaierror, ConnectionRefusedError)


def is_settled_refusal(error: BaseException) -> bool:
    """Чи несе ланцюг причин остаточну відповідь мережі.

    Публічна: одна політика повторів на випуск. Читач відкритих даних Ради
    (``registries/open_data_discovery.py``) бере її звідси, а не заводить
    другу таку саму — рев'ю тікета 15 назвало це Divergent Change.
    """
    seen = 0
    current: BaseException | None = error
    while current is not None and seen < 8:
        if isinstance(current, _SETTLED_REFUSALS):
            return True
        following = current.__cause__ or current.__context__
        if following is None and current.args:
            first = current.args[0]
            following = first if isinstance(first, BaseException) else None
        current = following
        seen += 1
    return False


class SourceTransport:
    """GET до джерела поверх ``requests.Session`` із правилами вище."""

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        idle_reset_seconds: float = IDLE_RESET_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session = session or requests.Session()
        self._idle_reset_seconds = idle_reset_seconds
        self._clock = clock
        self._last_used: float | None = None
        self._lock = threading.Lock()

    def _reset_if_idle(self) -> None:
        with self._lock:
            now = self._clock()
            last = self._last_used
            self._last_used = now
        if last is not None and now - last > self._idle_reset_seconds:
            close = getattr(self.session, "close", None)
            if callable(close):
                close()

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        """``session.get`` з правилами модуля; виняток ``requests`` — нагору."""
        self._reset_if_idle()
        try:
            return self.session.get(url, **kwargs)
        except requests.Timeout:
            # ConnectTimeout — нащадок і Timeout, і ConnectionError: відсікається
            # тут, до гілки повтору.
            raise
        except requests.ConnectionError as error:
            if is_settled_refusal(error):
                # Ім'я, яке не розв'язується, і закритий порт — відповідь
                # мережі, а не випадковість: ``urllib3`` до цього вже вичерпав
                # власні спроби (``MaxRetryError``). Повтор лише подвоїть
                # очікування перед певною відмовою.
                raise
            # Обірване з'єднання ``requests`` із пулу вже викинув; повтор піде
            # свіжим. Лише один: друга обірваність поспіль — стан мережі, а не
            # випадковість, і про неї має дізнатися адаптер.
            return self.session.get(url, **kwargs)

    def post(self, url: str, *, safe_to_repeat: bool = False, **kwargs: Any) -> requests.Response:
        """``session.post`` з правилами модуля; повтор — лише з дозволу викликача.

        Скидання простояного пулу до запиту ``POST`` не змінює нічого, крім
        зайвого рукостискання, тому діє завжди. А от повтор обірваного запиту
        для ``POST`` безпечний не сам по собі: обірватися могло вже після того,
        як сервер його прийняв, і друга спроба зробила б роботу двічі.
        Вирішує це не транспорт, а той, хто знає зміст запиту, — звідси
        ``safe_to_repeat``. Пошукові запити наших реєстрів нічого не змінюють
        і оголошують його явно.
        """
        self._reset_if_idle()
        try:
            return self.session.post(url, **kwargs)
        except requests.Timeout:
            raise
        except requests.ConnectionError as error:
            if not safe_to_repeat or is_settled_refusal(error):
                raise
            return self.session.post(url, **kwargs)


class CellarReader:
    """Спільні правила поводження з відповіддю Cellar для читачів права ЄС.

    Три помічники нижче були скопійовані дослівно в ``sources/eu_law.py`` і
    ``sources/eu_case_law.py``. Ціна копії вже виміряна на самому транспорті:
    правило «не перевикористовувати простояне з'єднання» полагодили в одному
    читачі й не помітили другого, і відмова 15.09.2026 прийшла саме звідти.

    Клас нічого не знає про адаптер, крім двох імен, які той зобов'язаний
    мати: ``source_id`` у відмові й ``_transport`` для запиту.
    """

    #: Час наступної розумної спроби у відмові. Однаковий для обох читачів:
    #: за ним стоїть один і той самий Cellar.
    retry_after = "через 15 хвилин"

    source_id: str
    _transport: SourceTransport

    def _unavailable(self, reason: str) -> SourceUnavailable:
        return SourceUnavailable(source=self.source_id, retry_after=self.retry_after, reason=reason)

    def _get(self, url: str, **kwargs: Any) -> Any:
        """Відповідь Cellar або ``source_unavailable`` з названою причиною.

        Причина — тип винятку чи HTTP-код, а не текст: 15.09.2026 однакова
        відмова «через 15 хвилин» не дала відрізнити обірване з'єднання від
        відмови джерела, і розслідування почалося з вгадування.
        """
        try:
            return self._transport.get(url, **kwargs)
        except requests.RequestException as error:
            return self._unavailable(f"network_error:{type(error).__name__}")

    def _sparql_bindings(self, query: str) -> list[dict[str, Any]] | SourceUnavailable:
        """Рядки видачі SPARQL, або відказ джерела. Порожня видача — не відказ."""
        response = self._get(
            endpoint("eu.cellar_sparql"),
            params={"query": query, "format": "application/sparql-results+json"},
            headers={"Accept": "application/sparql-results+json"},
            timeout=30,
        )
        if isinstance(response, SourceUnavailable):
            return response
        if response.status_code >= 400:
            return self._unavailable(f"http_{response.status_code}")
        try:
            bindings = response.json().get("results", {}).get("bindings", [])
        except ValueError:
            return self._unavailable("invalid_sparql_json")
        return list(bindings)
