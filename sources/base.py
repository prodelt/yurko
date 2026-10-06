"""Общий контракт адаптера источника (T013).

Всё, что обязан уметь адаптер любого источника, собрано здесь, и обязанностей
ровно четыре:

1. **Объявить политику источника.** Каждый адаптер декларирует, откуда и на
   каком основании он берёт данные: официальный интерфейс, неофициальный,
   разбор страниц или выверенный нами набор (принцип V). Политика — не
   комментарий, а поле, которое видно в диагностике.
2. **Отвечать конвертом.** Ответ с текстом нормы, решения или договора несёт
   :class:`~provenance.ProvenanceStamp`. Ответ без конверта контракту не
   соответствует, и :class:`AdapterPayload` сконструировать без него нельзя.
3. **Возвращать типизированный отказ, а не бросать исключение.** Недоступный
   источник обязан вернуть отказ и не нарушить работу остальных (принцип IX).
   Ошибка, всё же вырвавшаяся наружу, превращается в ``source_unavailable``
   декоратором :func:`typed_failures` — но это страховка, а не разрешение.
4. **Отличать документ от заглушки.** Формально успешный ответ, содержащий
   страницу проверки на автоматический доступ или оглавление, обязан давать
   ``upstream_stub_detected`` вместо содержимого (принцип III). Проверка —
   :func:`sources.stub_detection.detect_stub`; здесь она сведена к
   :meth:`RegistryAdapter.reject_stub`.

Адаптеры украинских реестров (``source_adapters.py``, ``court_registry.py``,
``state_registries.py``) на этот контракт не переводятся: у них свой, более
старый, и переписывать работающий канал ради единообразия — усложнение без
обоснования. Новые источники наследуют отсюда.
"""

from __future__ import annotations

import functools
import logging
from abc import ABC, abstractmethod
from typing import Any, Callable, Pattern, TypeVar

from core.contracts import (
    SourceHealth,
    SourcePolicy,
    SourceUnavailable,
    TypedFailure,
    UpstreamStubDetected,
)
from core.legal_orders import Source
from core.provenance import ProvenanceStamp
from sources.stub_detection import StubVerdict, detect_stub

logger = logging.getLogger("ukraine-laws")

__all__ = [
    "AdapterPayload",
    "AdapterResult",
    "RegistryAdapter",
    "typed_failures",
]


class AdapterPayload:
    """Успешный ответ адаптера: данные плюс обязательный конверт.

    Конверт не является необязательным аргументом и не имеет значения по
    умолчанию — ответ с текстом нормы без происхождения не должен уметь
    существовать даже как промежуточное значение.
    """

    __slots__ = ("data", "provenance", "attribution")

    def __init__(
        self,
        data: Any,
        provenance: ProvenanceStamp,
        *,
        attribution: str | None = None,
    ) -> None:
        if not isinstance(provenance, ProvenanceStamp):
            raise TypeError(
                "ответ адаптера обязан нести конверт происхождения "
                "(contracts/README.md): без него он не соответствует контракту"
            )
        self.data = data
        self.provenance = provenance
        self.attribution = attribution

    @property
    def ok(self) -> bool:
        return True

    def as_output(self) -> dict[str, Any]:
        """Данные и конверт одним словарём, пригодным для ответа инструмента."""
        payload: dict[str, Any] = (
            dict(self.data) if isinstance(self.data, dict) else {"data": self.data}
        )
        payload.update(self.provenance.as_envelope())
        return payload


#: Что адаптер возвращает: полезная нагрузка либо типизированный отказ.
#: Третьего не предусмотрено — исключение контрактом не является.
AdapterResult = AdapterPayload | TypedFailure

_F = TypeVar("_F", bound=Callable[..., Any])


def typed_failures(source_id: str, *, retry_after: str = "через 15 хвилин") -> Callable[[_F], _F]:
    """Превратить вырвавшееся исключение в отказ ``source_unavailable``.

    Страховка на границе, а не способ не думать об ошибках: адаптер обязан
    возвращать отказ сам, с осмысленным кодом и объяснением. Этот декоратор
    существует ровно для того, чтобы забытый случай уронил один источник,
    а не весь ответ юристу (принцип IX).
    """

    def decorate(function: _F) -> _F:
        @functools.wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return function(*args, **kwargs)
            except Exception as error:  # noqa: BLE001 — граница адаптера
                # Тип ошибки, не её текст: текст может содержать запрос
                # пользователя, а запросы мы не журналируем (FR-029).
                logger.warning(
                    "adapter %s failed in %s: %s",
                    source_id,
                    function.__name__,
                    type(error).__name__,
                )
                return SourceUnavailable(source=source_id, retry_after=retry_after)

        return wrapper  # type: ignore[return-value]

    return decorate


class RegistryAdapter(ABC):
    """Единый контракт адаптера источника."""

    #: Идентификатор источника; совпадает с ``Source.id`` в карте покрытия.
    source_id: str = ""
    #: Код правопорядка, который обслуживает адаптер.
    legal_order: str = ""
    #: Политика доступа к источнику — обязательное объявление (принцип V).
    source_policy: SourcePolicy = SourcePolicy.RESTRICTED

    # -- объявление политики источника -------------------------------------

    @abstractmethod
    def policy(self) -> Source:
        """Запись источника: слой, способ доступа, лицензия, ограничения.

        Возвращает ту же запись, что попадает в карту покрытия, — чтобы
        расхождение между тем, что адаптер делает, и тем, что карта о нём
        говорит, было невозможно (FR-004).
        """

    # -- работа с источником -----------------------------------------------

    @abstractmethod
    def fetch(self, document_id: str, **options: Any) -> AdapterResult:
        """Получить документ по идентификатору.

        Обязан вернуть :class:`AdapterPayload` с конвертом либо типизированный
        отказ. Бросать исключение контрактом не предусмотрено.
        """

    @abstractmethod
    def search(self, query: str, **options: Any) -> AdapterResult:
        """Найти документы по запросу. Те же обязательства, что у ``fetch``.

        Свободный ``query`` уходит источнику только в инженерном профиле
        (:mod:`yurko_profile`); в юридическом профиле адаптер получает лишь
        структурные параметры (``case_number``, ``date_from``, ``date_to``,
        ``max_results``) и обязан не отправлять текст пользователя.
        """

    def card(self, document_id: str, **options: Any) -> AdapterResult:
        """Карточка документа: реквизиты без текста (операция ``card``).

        По умолчанию — не покрыто: карточка не доказывает цитату, и источник,
        не объявивший её, не получает её «бесплатно».
        """
        from core.contracts import NotCovered

        policy = self.policy()
        return NotCovered(
            legal_order=self.legal_order,
            manual_path=policy.manual_path or policy.source_url,
            subject=document_id,
            operation="card",
            source_id=self.source_id,
        )

    #: Поддерживает ли ``search`` включительный фильтр по дате решения
    #: (``date_from``/``date_to``). ``False`` даёт ``unsupported_filter`` вместо
    #: молчаливого игнорирования (ADR 0008).
    supports_date_filter: bool = False

    #: Форма идентификатора документа у этого источника — известная до
    #: попытки чтения (T183). Раньше она жила только внутри отказа
    #: ``not_found``, то есть узнать её можно было, лишь ошибившись, и
    #: планировщик обещал бы то, что источник потом отклонит. Пустая строка
    #: означает «источник формы не объявил», а не «формы нет»: угадывать её
    #: за источник карта покрытия не станет.
    id_format_hint: str = ""

    #: Форма номера дела у этого форума (T182, FR-306). ``case_number``
    #: уходит источнику как параметр запроса, то есть покидает процесс; форма
    #: номера у форумов разная (ECLI и itemid у ЄСПЛ, номер жалобы «12345/06»,
    #: украинский номер дела с кириллицей), а общий шаблон идентификатора
    #: :data:`egress._IDENTIFIER_RE` кириллицу не пропускает. ``None`` —
    #: проверять общим шаблоном: он строже, а не слабее.
    case_number_pattern: Pattern[str] | None = None

    @abstractmethod
    def health(self) -> SourceHealth:
        """Живая проба доступности.

        Статический ``ok = True`` здоровьем не является: карта покрытия
        показывает, отвечает ли источник сейчас, а не отвечал ли когда-то.
        """

    # -- общее поведение ----------------------------------------------------

    def reject_stub(
        self,
        text: str,
        *,
        document_id: str = "",
        min_chars: int | None = None,
        extra_markers: tuple[str, ...] = (),
    ) -> UpstreamStubDetected | None:
        """Отказ, если источник вернул заглушку вместо документа.

        Возвращает отказ, который нужно отдать **вместо** содержимого, или
        ``None``, если пришёл документ. Формально успешный ответ с заглушкой
        опаснее явной ошибки: он выглядит здоровым и молча подменяет основание
        ответа.
        """
        verdict: StubVerdict | None = detect_stub(
            text,
            min_chars=min_chars,
            extra_markers=extra_markers,
        )
        if verdict is None:
            return None
        return UpstreamStubDetected(
            source=self.source_id,
            detected_by=verdict.as_detected_by(),
            document_id=document_id,
        )

    def __repr__(self) -> str:  # pragma: no cover - диагностика
        return f"<{type(self).__name__} {self.source_id!r} {self.legal_order!r}>"
