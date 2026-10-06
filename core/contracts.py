"""Validated contracts for source adapters and tool boundaries.

Three things live here and are worth naming, because each of them is a
constitutional principle in executable form:

* **`legal_order` on every tool** (T016, principle VII). The legal order is a
  parameter, not a family of tools: adding a jurisdiction adds an enum value.
  Tools that work with law require it; tools bound to Ukrainian registers by
  definition take it as an optional filter.
* **The provenance envelope is not optional** (T016, principle IV). An output
  model that carries the text of a norm inherits :class:`_LegalTextOutput`, and
  that model refuses to validate without the envelope. "A tool returning the
  text of a norm without an envelope does not conform" is checked here, not
  left to the caller's good intentions.
* **Ten typed failures** (T009). A refusal is an answer, not an exception:
  every one of them is a plain model with a code, a Ukrainian explanation and
  the extra fields that particular refusal cannot be honest without. None of
  them derives from ``Exception`` — deliberately, and a test asserts it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from enum import Enum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.legal_orders import (
    KNOWN_LEGAL_ORDER_CODES,
    is_known_legal_order,
    normalize_legal_order_code,
)
from core.provenance import ENVELOPE_FIELDS

#: Версия контракта инструментов. 2.0.0 (ADR 0008) — единый переход на
#: document_id/path/paths, legal_order/language/as_of, unsupported_filter.
#: 3.0.0 (ADR 0011) — задача и аттестация: `plan_task`, `attest_claim`,
#: `render_attested`, `get_session_ledger`, `end_task`, три новых отказа и
#: пометка `navigation_only` в выдаче поиска. Изменение ломающее и внесено до
#: появления внешних потребителей, пока его цена минимальна (принцип VII).
#: 3.1.0 (ADR 0013) — воркфлоу задачи: `begin_task`, `task_step`, `task_state`.
#: Изменение **дополняющее**: ни один инструмент 3.0.0 не изменил ни входа, ни
#: выхода, ни смысла. Поэтому вторая цифра, а не первая — методика, привязанная
#: к 3.0.0, остаётся верной.
#: 3.1.1 (T-cites, 2026-09-09) — `search_decisions` получил параметр `cites`
#: (CELEX акта → документи, що його цитують) и поле выхода `cites`; старые
#: вызовы без него ведут себя как раньше. PATCH — новое необязательное поле,
#: а не изменение существующего смысла.
#: 4.0.0 (T279/T280, `specs/005-court-sandbox-deliverables/contracts/render-v4.md`)
#: — контракт печати и вывода. Изменение **ломающее**, и внесено оно намеренно
#: сейчас: принцип VII конституции — «Ломающие изменения контракта SHOULD
#: вноситься до появления внешних потребителей, пока их цена минимальна».
#: Внешних потребителей у ядра нет: единственный потребитель — плагин этого же
#: репозитория, и он обновляется тем же коммитом. Восемь изменений §0
#: контракта: `skeleton.task_id` и `Section.purpose` изъяты (дублирование и
#: свободный заголовок), `Claim.quote` → `Claim.quote_segments`, `case_ref`
#: стал структурой `CaseLocator` с обязательным локатором, `proceeding_language`
#: обязателен для видов `audience="court"`, восемь новых отказов и четыре новых
#: предупреждения, `AttestedDraft` называет записанные ядром пути, большой
#: вывод отдаётся страницами.
#: Попадает в каждый ответ как ``schema_version``; методика привязывается к ней.
CONTRACT_VERSION = "4.0.0"


class SourcePolicy(str, Enum):
    """Allowed access policies for official source adapters."""

    API = "api"
    OPEN_DATA_DUMP = "open_data_dump"
    HTML_PRINT = "html_print"
    BROWSER_REQUIRED = "browser_required"
    RESTRICTED = "restricted"


class ErrorCode(str, Enum):
    """Stable user-facing error codes for MCP tool responses."""

    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    SOURCE_UNAVAILABLE = "source_unavailable"


class SourceDocument(BaseModel):
    """Immutable source document returned by a source adapter."""

    model_config = ConfigDict(frozen=True)

    law_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: str = ""
    text: str = Field(min_length=1)
    source: str = Field(min_length=1)
    source_policy: SourcePolicy
    adapter: str = Field(min_length=1)
    amendment_date: str | None = None
    retrieved_at: str | None = None
    content_hash: str | None = None

    def with_hash(self) -> "SourceDocument":
        if self.content_hash:
            return self
        digest = content_hash(self.text)
        return self.model_copy(update={"content_hash": digest})

    def as_cache_payload(self) -> dict[str, Any]:
        payload = self.with_hash().model_dump(mode="json", exclude_none=True)
        payload["source_policy"] = str(payload["source_policy"])
        return payload


class SourceMetadata(BaseModel):
    """Immutable metadata snapshot for a source adapter and law."""

    model_config = ConfigDict(frozen=True)

    law_id: str
    adapter: str
    source_policy: SourcePolicy
    url: str = ""
    title: str = ""
    backoff: dict[str, Any] = Field(default_factory=dict)


class SourceHealth(BaseModel):
    """Immutable source adapter health status."""

    model_config = ConfigDict(frozen=True)

    adapter: str
    ok: bool
    source_policy: SourcePolicy
    details: dict[str, Any] = Field(default_factory=dict)


class SourceAdapterError(RuntimeError):
    """Internal adapter failure that can be translated into user-facing errors."""


class SourceRequiresHuman(SourceAdapterError):
    """Джерело працює, але відповідає лише людині за браузером.

    Третій стан поруч із «джерело мовчить» і «запису немає»: reCAPTCHA,
    вхід за паролем, підпис. Капчу за людину проєкт не розв'язує (рішення
    власника, тікет 05), тож це межа покриття, а не збій — і відповідь має
    вести до ручного шляху, а не радити спробувати пізніше.
    """


class SourceRecordNotFound(SourceAdapterError):
    """Джерело відповіло: такого запису немає.

    Окремий тип, бо це протилежність збою. «Такого тендера немає» під кодом
    ``source_unavailable`` радить юристові спробувати пізніше там, де пробувати
    нічого: запис не з'явиться. Та сама плутанина коштувала тікета 15 — порожній
    результат замість межі покриття. Наслідок технічний теж є: відмову джерела
    адаптер рахує в backoff, а відповідь «немає» рахувати не повинен, інакше
    один помилковий ідентифікатор блокує здорове джерело.
    """


# ---------------------------------------------------------------------------
# T009 — десять типизированных отказов
# ---------------------------------------------------------------------------


class FailureCode(str, Enum):
    """Коды отказов из `contracts/envelope-and-errors.md`.

    ``not_covered`` и пустой результат — **разные** ответы. Пустота означает
    «искали и не нашли», непокрытие — «здесь не искали и не могли».
    """

    NOT_FOUND = "not_found"
    NOT_COVERED = "not_covered"
    SOURCE_UNAVAILABLE = "source_unavailable"
    #: Той самий код, що й ``ErrorCode.INVALID_INPUT`` схеми виклику, але від
    #: адаптера: ідентифікатор не тієї форми не йде до джерела (тікет 21).
    INVALID_INPUT = "invalid_input"
    UNKNOWN_LEGAL_ORDER = "unknown_legal_order"
    REVISION_UNKNOWN = "revision_unknown"
    TREATY_STATUS_UNKNOWN = "treaty_status_unknown"
    NO_CANONICAL_TEXT = "no_canonical_text"
    BLOCKED_BY_LICENSE = "blocked_by_license"
    UPSTREAM_STUB_DETECTED = "upstream_stub_detected"
    PARTIAL_RESULT = "partial_result"
    #: Одиннадцатый отказ (ADR 0008): фильтр, который источник не поддерживает,
    #: не игнорируется молча — молчаливый фильтр создаёт ложную полноту.
    UNSUPPORTED_FILTER = "unsupported_filter"
    #: Отказ на уровне задачи (ADR 0011): вопрос не закрывается ни одной
    #: возможностью. Не то же самое, что not_covered одного источника.
    QUESTION_UNANSWERABLE = "question_unanswerable"
    #: Отказы рендера: ссылка, которую написала модель, и утверждение о норме
    #: без аттестации. Оба — исполнение принципа II для генерируемого текста.
    UNBOUND_CITATION_IN_PROSE = "unbound_citation_in_prose"
    UNATTESTED_NORM_CLAIM = "unattested_norm_claim"
    UNKNOWN_DOCUMENT_KIND = "unknown_document_kind"
    #: Восемь отказов контракта печати 4.0.0 (render-v4.md §1, §2). Каждый
    #: назван потому, что соответствующий дефект **измерен** в прогоне 09.09, а
    #: не потому, что его вообразили: раздел с придуманным идентификатором,
    #: приписанный каталогом и не напечатанный раздел, факт дела без страницы
    #: или пункта, недоступный каталог вывода, документ суду без языка
    #: производства, отчёт стресс-теста, который пробуют экспортировать,
    #: пометка ядра, вписанная моделью в прозу, и рабочая пометка ревизии в
    #: имени файла пакета.
    UNKNOWN_SECTION = "unknown_section"
    MISSING_REQUIRED_SECTION = "missing_required_section"
    CASE_REF_WITHOUT_LOCATOR = "case_ref_without_locator"
    OUTPUT_DIR_UNAVAILABLE = "output_dir_unavailable"
    PROCEEDING_LANGUAGE_MISSING = "proceeding_language_missing"
    EXPORT_FORBIDDEN_FOR_KIND = "export_forbidden_for_kind"
    MARK_IN_PROSE = "mark_in_prose"
    REVISION_MARKER_IN_FILENAME = "revision_marker_in_filename"
    #: Приписування іншій стороні твердження, яке не підтверджено (FR-544):
    #: блокує друк розділу, а не попереджає.
    ATTRIBUTION_NOT_CONFIRMED = "attribution_not_confirmed"
    #: П'ять відмов карти 006 (contracts/core.md §5). Кожна названа тому, що
    #: без неї відповідний випадок мав би тихий обхід: врізаний пакет,
    #: неоднозначна аттестація, вгадане ім'я вимкненого інструмента, друга
    #: задача в тому самому транспортному сеансі й число, пораховане на
    #: невиміряному порозі.
    BATCH_TOO_LARGE = "batch_too_large"
    DUPLICATE_CLAIM_ID = "duplicate_claim_id"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    SESSION_TASK_CONFLICT = "session_task_conflict"
    CALIBRATION_MISSING = "calibration_missing"


class WarningCode(str, Enum):
    """Предупреждение печати: документ рендерится, но юрист извещён.

    Отличие от отказа — не в тяжести, а в том, кто вправе решать. Отказ
    означает «этого напечатать нельзя»; предупреждение — «напечатано, и вот что
    в этом стоит увидеть». Перечень закрыт по той же причине, что и перечень
    отказов: предупреждение свободным текстом невозможно ни сосчитать, ни
    проверить тестом.
    """

    #: 3.0.0: цитата обрезана внутри подпункта; язык подтверждения беднее
    #: заявленного задачей.
    TRUNCATED_QUOTE = "truncated_quote"
    LANGUAGE_SHORTFALL = "language_shortfall"
    #: 4.0.0: доля символов на аппарат ссылок выше 15 % (FR-509, SC-507);
    #: язык производства разошёлся с языком задачи (FR-519); объём превысил
    #: практические указания форума (FR-522); путь взят обходом каталога, а не
    #: из реестра вывода (render-v4 §2).
    APPARATUS_SHARE_EXCEEDED = "apparatus_share_exceeded"
    PROCEEDING_LANGUAGE_MISMATCH = "proceeding_language_mismatch"
    VOLUME_EXCEEDS_FORUM_GUIDANCE = "volume_exceeds_forum_guidance"
    REGISTRY_MISS = "registry_miss"


class TypedFailure(BaseModel):
    """Отказ как ответ: код, объяснение на украинском, обязательные детали.

    Наследники не являются исключениями и не бросаются. Тот, кто получил отказ,
    получил результат работы, а не аварию — недоступность одного источника не
    должна нарушать работу остальных (принцип IX).
    """

    model_config = ConfigDict(frozen=True)

    failure_code: ClassVar[FailureCode]

    #: Свободное пояснение. Пустое означает «построй из полей».
    message: str = ""

    @model_validator(mode="after")
    def explanation_is_never_empty(self) -> "TypedFailure":
        if not self.message.strip():
            object.__setattr__(self, "message", self.explain())
        return self

    def explain(self) -> str:
        """Объяснение на украинском. Наследник переопределяет."""
        raise NotImplementedError

    def details(self) -> dict[str, Any]:
        """Дополнительные поля отказа — всё, кроме самого пояснения."""
        return self.model_dump(mode="json", exclude={"message"})

    def as_output(self) -> dict[str, Any]:
        """Отказ в виде ответа инструмента."""
        return {
            "error": self.message,
            "code": self.failure_code.value,
            "details": self.details(),
        }


class _ActionableFailure(TypedFailure):
    """Отказ печати: к объяснению добавлен рецепт — действие, снимающее отказ.

    Поле называется ``manual_path`` и появилось не здесь: так оно называется у
    ``not_covered``, ``unsupported_filter`` и ``question_unanswerable`` с 002
    (принцип III — «отказ обязан назвать, что делать вместо»). Отказы печати
    3.1.1 его не несли, и прогон 09.09 показал цену: тринадцать отказов подряд
    за сорок восемь минут, каждый из которых объяснял, **что** не так, и ни
    один — **что сделать**.

    Заполняется так же, как объяснение: пустое значение означает «построй из
    полей», и :meth:`recipe` наследника его строит. Отказ без рецепта не
    конструируется — это проверяется тестом, а не намерением.
    """

    manual_path: str = ""

    @model_validator(mode="after")
    def the_refusal_names_the_action_that_lifts_it(self) -> "_ActionableFailure":
        if not self.manual_path.strip():
            object.__setattr__(self, "manual_path", self.recipe())
        if not self.manual_path.strip():
            raise ValueError(
                f"відмова {self.failure_code.value} без дії, яка її знімає: "
                "це зупинка, а не відповідь (принцип III)"
            )
        return self

    def recipe(self) -> str:
        """Действие, снимающее отказ. Наследник переопределяет."""
        raise NotImplementedError


class NotFound(TypedFailure):
    """Документа с таким идентификатором у источника нет."""

    failure_code: ClassVar[FailureCode] = FailureCode.NOT_FOUND

    identifier: str = Field(min_length=1)
    id_format_hint: str = Field(min_length=1)

    def explain(self) -> str:
        return (
            f"Документ з ідентифікатором «{self.identifier}» у джерелі відсутній. "
            f"Очікуваний формат ідентифікатора: {self.id_format_hint}."
        )


class InvalidIdentifier(TypedFailure):
    """Идентификатор не той формы: к источнику не отправляется вовсе.

    Отличается от ``not_found``: там источник спросили и документа нет, здесь
    не спрашивали, потому что строку нельзя безопасно вставить в запрос.
    """

    failure_code: ClassVar[FailureCode] = FailureCode.INVALID_INPUT

    identifier: str = Field(min_length=1)
    id_format_hint: str = Field(min_length=1)

    def explain(self) -> str:
        return (
            f"Ідентифікатор «{self.identifier}» має некоректну форму і до джерела не "
            f"надсилався. Очікуваний формат: {self.id_format_hint}."
        )


class NotCovered(TypedFailure):
    """Слой источника — ``not_covered``: отвечать по существу запрещено.

    Ручной путь обязателен и не имеет значения по умолчанию: отказ без него
    не построишь, а значит и не отдашь как честный (принцип III).
    """

    failure_code: ClassVar[FailureCode] = FailureCode.NOT_COVERED

    legal_order: str = Field(min_length=2)
    manual_path: str = Field(min_length=1)
    subject: str = ""
    #: Операция, для которой нет проверенной возможности (FR-203): чтение,
    #: поиск, редакция на дату, карточка. Пусто — источника нет вовсе.
    operation: str = ""
    #: Источник, к которому относится отказ, если он известен.
    source_id: str = ""

    @field_validator("manual_path")
    @classmethod
    def manual_path_is_not_whitespace(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError(
                "отказ not_covered обязан нести непустой ручной путь: без него "
                "он неотличим от «искали и не нашли» (принцип III)"
            )
        return stripped

    def explain(self) -> str:
        subject = f" на запит «{self.subject}»" if self.subject else ""
        if self.operation:
            return (
                f"Операція «{self.operation}» для правопорядку {self.legal_order}{subject} "
                "не має перевіреної можливості — це не порожній результат, а межа покриття. "
                f"Ручний шлях: {self.manual_path}"
            )
        return (
            f"Джерела для правопорядку {self.legal_order}{subject} у нас немає — "
            f"це не порожній результат, а межа покриття. Ручний шлях: {self.manual_path}"
        )


class SourceUnavailable(_ActionableFailure):
    """Источник недоступен, пригодной копии нет.

    T356. До набора 005 этот отказ был обычным :class:`TypedFailure`: он
    называл **когда** повторить и не называл **что делать вместо**. Живой
    вызов 10.09.2026 (`get_article(eu, <CELEX>, 2)` при недоступном
    EUR-Lex) вернул время следующей попытки и ни одного действия.

    Цена измерена: недоступность источника — самый частый отказ в работе
    юриста, и именно он дал **38 % времени сеанса в простое** в прогоне
    09.09.2026 при пороге SC-513 ≤ 5 %. FR-537 требует «состояние ожидания с
    названным временем **или** ручной путь с адресом» — вторая половина
    отсутствовала. Теперь обе стоят **вместе**, а не вместо друг друга.
    """

    failure_code: ClassVar[FailureCode] = FailureCode.SOURCE_UNAVAILABLE

    source: str = Field(min_length=1)
    retry_after: str = Field(min_length=1)
    #: Причина, отличающая внешний сбой от внутренней границы: например,
    #: ``evidence_capacity`` — лимит сессионной памяти доказательств достигнут,
    #: фрагмент не выдаётся до регистрации (contracts/envelope-and-errors.md).
    reason: str = ""

    def recipe(self) -> str:
        """Что делать, пока источник молчит. Ожидание — не единственный ход."""
        return (
            f"Не чекайте мовчки: питання, які не залежать від джерела {self.source}, "
            "ідуть далі — робота триває, а це питання переходить у стан очікування. "
            "Ручний шлях, якщо чекати не можна: відкрийте документ у первинному "
            "джерелі своїм браузером, збережіть його в теку справи і працюйте з "
            "тим файлом. Наступна автоматична спроба: "
            f"{self.retry_after}."
        )

    @field_validator("retry_after")
    @classmethod
    def retry_after_is_not_whitespace(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("отказ source_unavailable обязан назвать время разумной попытки")
        return stripped

    def explain(self) -> str:
        if self.reason == "evidence_capacity":
            return (
                "Ліміт сесійної пам'яті доказів досягнуто: фрагмент не видається без "
                "реєстрації, а раніше видані фрагменти не витісняються непомітно. "
                f"Розумна наступна спроба: {self.retry_after}."
            )
        return (
            f"Джерело {self.source} тимчасово недоступне, придатної копії немає. "
            f"Розумна наступна спроба: {self.retry_after}."
        )


class UnknownLegalOrder(TypedFailure):
    """Передан неизвестный код правопорядка."""

    failure_code: ClassVar[FailureCode] = FailureCode.UNKNOWN_LEGAL_ORDER

    received: str = ""
    known_codes: tuple[str, ...] = Field(min_length=1)

    def explain(self) -> str:
        return (
            f"Невідомий правопорядок «{self.received}». "
            f"Відомі коди: {', '.join(self.known_codes)}."
        )


class RevisionUnknown(TypedFailure):
    """Редакция на запрошенную дату неизвестна.

    Подстановка действующей редакции запрещена: она отвечает не на тот вопрос,
    который задали, и делает это молча.
    """

    failure_code: ClassVar[FailureCode] = FailureCode.REVISION_UNKNOWN

    document_id: str = Field(min_length=1)
    requested_date: str = Field(min_length=1)
    known_bounds: tuple[str, ...] = Field(min_length=1)

    def explain(self) -> str:
        return (
            f"Редакція документа {self.document_id} станом на {self.requested_date} невідома. "
            f"Відомі межі сусідніх редакцій: {'; '.join(self.known_bounds)}. "
            "Чинну редакцію замість запитаної не підставляємо."
        )


class TreatyStatusUnknown(TypedFailure):
    """Статус договора для этого государства не выверен."""

    failure_code: ClassVar[FailureCode] = FailureCode.TREATY_STATUS_UNKNOWN

    treaty_id: str = Field(min_length=1)
    state: str = Field(min_length=1)
    depositary_url: str = Field(min_length=1)

    def explain(self) -> str:
        return (
            f"Участь держави {self.state} у договорі {self.treaty_id} нами не звірено. "
            f"Сторінка депозитарію: {self.depositary_url}"
        )


class NoCanonicalText(TypedFailure):
    """У нормы нет канонического текста.

    Обычная норма международного права существует, но текста, который можно
    процитировать как её формулировку, у неё нет (R-09).
    """

    failure_code: ClassVar[FailureCode] = FailureCode.NO_CANONICAL_TEXT

    subject: str = Field(min_length=1)
    reflecting_documents: tuple[str, ...] = Field(min_length=1)
    #: Положительное основание (ADR 0002, закрытие ревью H1): запись реестра
    #: обычных норм, проверенная юристом. Без него этот исход не конструируется:
    #: пустая выдача, CAPTCHA или неизвестная редакция не доказывают отсутствие
    #: канонического текста.
    basis_id: str = Field(min_length=1)
    basis_source_urls: tuple[str, ...] = Field(min_length=1)
    reviewer: str = Field(min_length=1)
    verified_at: str = Field(min_length=1)

    @field_validator("basis_id", "reviewer", "verified_at")
    @classmethod
    def basis_is_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError(
                "no_canonical_text без проверенного юристом основания не является исходом: "
                "basis_id, reviewer и verified_at обязательны (принцип II)"
            )
        return stripped

    def explain(self) -> str:
        return (
            f"Норма «{self.subject}» не має канонічного тексту, тож цитувати її як "
            "формулювання не можна. Документи, що її відображають: "
            f"{'; '.join(self.reflecting_documents)}. Підстава: {self.basis_id}, "
            f"перевірено {self.reviewer} {self.verified_at}."
        )


class UpstreamStubDetected(TypedFailure):
    """Источник вернул заглушку вместо документа.

    Возвращается вместо содержимого, даже если источник ответил успешно:
    формально успешный ответ с заглушкой опаснее явной ошибки.
    """

    failure_code: ClassVar[FailureCode] = FailureCode.UPSTREAM_STUB_DETECTED

    source: str = Field(min_length=1)
    detected_by: str = Field(min_length=1)
    document_id: str = ""

    def explain(self) -> str:
        target = f" для {self.document_id}" if self.document_id else ""
        return (
            f"Джерело {self.source}{target} повернуло не документ, а заглушку. "
            f"Ознака, за якою розпізнано підміну: {self.detected_by}."
        )


class PartialResult(TypedFailure):
    """Источник ограничил выдачу, полнота не гарантирована.

    Усечение всегда видимо: усечённый результат не может быть подан как полный.
    """

    failure_code: ClassVar[FailureCode] = FailureCode.PARTIAL_RESULT

    received: int = Field(ge=0)
    limited_by: str = Field(min_length=1)

    def explain(self) -> str:
        return (
            f"Отримано {self.received} записів, і це не повна вибірка: {self.limited_by}. "
            "Повнота не гарантується."
        )


class UnsupportedFilter(TypedFailure):
    """Фильтр не поддерживается источником; молча игнорировать его запрещено.

    ADR 0008: ``date_from``/``date_to`` применяются только там, где поддержка
    доказана; иначе — этот отказ с ручным путём, а не ложная полнота.
    """

    failure_code: ClassVar[FailureCode] = FailureCode.UNSUPPORTED_FILTER

    source_id: str = Field(min_length=1)
    filter: str = Field(min_length=1)
    manual_path: str = Field(min_length=1)

    def explain(self) -> str:
        return (
            f"Фільтр «{self.filter}» джерелом {self.source_id} не підтримується; мовчки "
            f"ігнорувати його не можна — вибірка була б хибно повною. Ручний шлях: {self.manual_path}"
        )


class CaseRefWithoutLocator(_ActionableFailure):
    """Факт дела назван без страницы или пункта документа папки (FR-511)."""

    failure_code: ClassVar[FailureCode] = FailureCode.CASE_REF_WITHOUT_LOCATOR

    claim_id: str = Field(min_length=1)
    file: str = ""

    def explain(self) -> str:
        where = f" з документа «{self.file}»" if self.file else ""
        return (
            f"Факт справи {self.claim_id}{where} названо без локатора. Факт без "
            "сторінки або пункту перевірити неможливо: юрист не знає, де в теці "
            "його читати, і позначка факту стає словом честі, а не посиланням."
        )

    def recipe(self) -> str:
        return (
            "Назвіть локатор факту у форматі «с. N» або «п. N» — сторінку чи "
            "пункт документа теки справи, з якого взято факт."
        )


class ProceedingLanguageMissing(_ActionableFailure):
    """Документ суду печатается без объявленного языка производства (FR-519)."""

    failure_code: ClassVar[FailureCode] = FailureCode.PROCEEDING_LANGUAGE_MISSING

    kind: str = Field(min_length=1)
    audience: str = "court"

    def explain(self) -> str:
        return (
            f"Вид {self.kind} адресовано «{self.audience}», а мову провадження не "
            "названо. Мова провадження — не мова зручності юриста: документ "
            "іншою мовою суд не читає, і дізнатися про це після подання пізно."
        )

    def recipe(self) -> str:
        return (
            "Назвіть proceeding_language (код мови провадження форуму) у виклику "
            "render_attested або в begin_task задачі."
        )


_FAILURE_CODE_VALUES = frozenset(code.value for code in FailureCode) | frozenset(
    code.value for code in ErrorCode
)

#: Ручной путь на случай, когда его не назвал никто. Пустой ``manual_path``
#: запрещён (FR-527, FR-654), а отказ без действия — это отказ, который юрист
#: снимает догадкой.
FALLBACK_MANUAL_PATH = (
    "Прочитайте reason, виправте названий параметр і повторіть виклик; якщо "
    "причина не в параметрі — візьміть джерело офіційним каналом вручну і "
    "продовжуйте роботу з ним."
)


def is_failure(payload: dict[str, Any]) -> bool:
    """Является ли ответ отказом.

    Пустой результат отказом не является: у него нет кода. Именно это и
    отличает «искали и не нашли» от «здесь не искали и не могли».
    """
    return str(payload.get("code") or "") in _FAILURE_CODE_VALUES


def unknown_legal_order(received: Any) -> dict[str, Any]:
    """Готовый отказ ``unknown_legal_order`` со списком известных кодов."""
    return UnknownLegalOrder(
        received=str(received or ""),
        known_codes=KNOWN_LEGAL_ORDER_CODES,
    ).as_output()


# ---------------------------------------------------------------------------
# Document, Revision, Unit — сущности из data-model.md
# ---------------------------------------------------------------------------


class DocumentType(str, Enum):
    """Акт, судебное решение, договор или мягкое право."""

    ACT = "act"
    DECISION = "decision"
    TREATY = "treaty"
    SOFT_LAW = "soft_law"


class Revision(BaseModel):
    """Состояние документа на интервал времени.

    Запрос с датой обязан возвращать редакцию, чей интервал накрывает дату,
    вместе с границами интервала. Если редакция на дату неизвестна — это отказ
    :class:`RevisionUnknown`, а не подстановка действующей (принцип IV).
    """

    model_config = ConfigDict(frozen=True)

    document_id: str = Field(min_length=1)
    valid_from: str = Field(min_length=1)
    valid_to: str | None = None
    consolidation_id: str = ""
    #: До какой даты источник подтверждает отсутствие последующих изменений
    #: (ADR 0007). ``valid_to=None`` без ``known_through`` не обещает вечную
    #: действительность: интервал доказан только до ``known_through``.
    known_through: str | None = None
    #: Официальные публикации, из которых выведены границы интервала.
    interval_basis_urls: tuple[str, ...] = ()
    #: Изменяющие акты, учтённые в этой редакции (CELEX/ELI/номера).
    amending_acts: tuple[str, ...] = ()
    #: Вступление в силу и начало применения — разные события.
    effective_date: str | None = None
    application_date: str | None = None
    #: Публикационная сила текста редакции: official_journal / consolidated / unknown.
    publication_kind: str = "unknown"

    @field_validator("valid_from")
    @classmethod
    def valid_from_is_required(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("редакция без даты начала не является редакцией")
        return stripped

    @field_validator("valid_to")
    @classmethod
    def blank_is_open_ended(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def the_interval_runs_forward(self) -> "Revision":
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError(
                f"редакция {self.document_id}: интервал {self.valid_from}..{self.valid_to} "
                "заканчивается раньше, чем начинается"
            )
        return self

    @property
    def is_current(self) -> bool:
        """Null в верхней границе означает действующую редакцию."""
        return self.valid_to is None

    def covers(self, date: str) -> bool:
        """Накрывает ли редакция дату (ISO-8601, лексикографическое сравнение)."""
        if date < self.valid_from:
            return False
        return self.valid_to is None or date <= self.valid_to

    def proves(self, date: str) -> bool:
        """Доказан ли интервал для даты, а не только накрыт (ADR 0007).

        Закрытый интервал доказан своими границами. Открытый — только до
        ``known_through``: дата позже последней проверенной границы остаётся
        ``revision_unknown``, потому что позднейшие изменения не исключены.
        """
        if not self.covers(date):
            return False
        if self.valid_to is not None:
            return True
        return self.known_through is not None and date <= self.known_through


class Unit(BaseModel):
    """Минимальная адресуемая часть документа: статья, пункт, параграф.

    ``text`` — только полученный от источника. Реконструированный, дополненный
    или восстановленный по памяти текст сюда не попадает (принцип II), и
    пустая строка тоже не является текстом нормы.
    """

    model_config = ConfigDict(frozen=True, validate_assignment=True)

    document_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    text: str = Field(min_length=1)
    revision: Revision | None = None
    #: Тип документа, к которому принадлежит единица. Нужен ровно для одного
    #: правила: редакция обязательна для актов и бессмысленна для решений.
    doc_type: DocumentType = DocumentType.ACT

    @field_validator("document_id", "path", "text")
    @classmethod
    def not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("единица цитирования без документа, адреса или текста неполна")
        return stripped

    @model_validator(mode="after")
    def an_act_unit_carries_its_revision(self) -> "Unit":
        if self.doc_type is DocumentType.ACT and self.revision is None:
            raise ValueError(
                f"единица {self.path!r} акта {self.document_id!r} без редакции: "
                "не зная редакции, нельзя сказать, что именно процитировано"
            )
        return self


# ---------------------------------------------------------------------------
# Базовые модели входа и выхода
# ---------------------------------------------------------------------------


class _ToolInput(BaseModel):
    """Base immutable model for MCP tool input contracts."""

    model_config = ConfigDict(frozen=True)


class _LawToolInput(_ToolInput):
    """Вход инструмента, работающего с правом: правопорядок обязателен.

    Ломающее изменение R-01. Совместимость со старыми сигнатурами не
    поддерживается: единственный известный потребитель — собственная методика,
    которая обновляется вместе с сервером.
    """

    legal_order: str

    @field_validator("legal_order", mode="before")
    @classmethod
    def legal_order_is_known(cls, value: Any) -> str:
        code = normalize_legal_order_code(value)
        if not is_known_legal_order(code):
            raise ValueError(
                f"unknown_legal_order: {value!r}; известные коды: "
                f"{', '.join(KNOWN_LEGAL_ORDER_CODES)}"
            )
        return code


class _RegistryToolInput(_ToolInput):
    """Вход инструмента по реестрам: правопорядок — фильтр, а не обязательный ключ.

    Эти инструменты привязаны к конкретному реестру по определению — украинскому
    (ЄРБ, Prozorro, data.gov.ua), — поэтому правопорядок здесь необязателен и имеет осмысленное
    значение по умолчанию; пустое значение означает «без фильтра».
    """

    legal_order: str = ""

    @field_validator("legal_order", mode="before")
    @classmethod
    def legal_order_is_known_if_given(cls, value: Any) -> str:
        code = normalize_legal_order_code(value)
        if not code:
            return ""
        if not is_known_legal_order(code):
            raise ValueError(
                f"unknown_legal_order: {value!r}; известные коды: "
                f"{', '.join(KNOWN_LEGAL_ORDER_CODES)}"
            )
        return code


class _ToolOutput(BaseModel):
    """Base immutable model for MCP tool output contracts.

    Every answer says which legal order it answered for. Empty means the tool is
    not bound to one (a register listing, a diagnostic) — never "we forgot".
    """

    model_config = ConfigDict(frozen=True, extra="allow")

    legal_order: str = ""


class _LegalTextOutput(_ToolOutput):
    """Ответ, содержащий текст нормы, решения или договора.

    Такой ответ обязан нести конверт происхождения целиком. Модель отказывается
    валидироваться без него — именно это и означает «инструмент, вернувший
    текст без конверта, не соответствует контракту» (`contracts/README.md`).
    """

    @model_validator(mode="after")
    def the_envelope_is_present_in_full(self) -> "_LegalTextOutput":
        present = set(self.model_dump(mode="json"))
        missing = ENVELOPE_FIELDS - present
        if missing:
            raise ValueError(
                "ответ содержит текст нормы, но конверт происхождения неполон; "
                f"нет полей: {', '.join(sorted(missing))}"
            )
        return self


class ListLawsInput(_RegistryToolInput):
    category_filter: str = ""

    @field_validator("category_filter", mode="before")
    @classmethod
    def normalize_category_filter(cls, value: Any) -> str:
        return str(value or "").strip()


class ResolveLawInput(_LawToolInput):
    query: str

    @field_validator("query", mode="before")
    @classmethod
    def normalize_query(cls, value: Any) -> str:
        return str(value or "").strip()


#: Метка роли агента: короткий ярлык, не свободный текст. Наружу она не
#: уходит и нигде не сохраняется, но поле, принимающее произвольную строку,
#: рано или поздно принимает факт из дела (принцип VI), поэтому форма закрыта.
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


def _normalize_agent_id(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not _AGENT_ID_RE.match(text):
        raise ValueError(
            "agent_id: очікується коротка мітка ролі (літери, цифри, . _ : -), "
            f"отримано {text[:40]!r}"
        )
    return text


#: Ярлык задачи, вопроса, утверждения, раздела. Как и ``agent_id``, это метка,
#: а не текст: поле, принимающее произвольную строку, рано или поздно принимает
#: факт из дела (принцип VI), а ярлык ещё и попадает в идентификатор аттестации.
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,63}$")


def _normalize_label(value: object, field_name: str, *, required: bool = False) -> str:
    text = str(value or "").strip()
    if not text:
        if required:
            raise ValueError(f"{field_name}: порожній ідентифікатор")
        return ""
    if not _LABEL_RE.match(text):
        raise ValueError(
            f"{field_name}: очікується коротка мітка (літери, цифри, . _ : -), "
            f"отримано {text[:40]!r}"
        )
    return text


def _internal_address(value: Any, field_name: str = "path", *, required: bool = False) -> str:
    """Адрес **внутри** документа: статья, пункт, параграф — но не файл.

    Поле называется ``path`` по наследству от 001, и имя обманчиво: речь о
    пункте нормы, а не о файле на диске. Пока значение не проверялось, поле с
    таким именем оставалось местом, куда рано или поздно пришёл бы настоящий
    путь — а ядро материалов дела не получает (конституция, принцип VI).
    Проверка закрывает это место, не ломая контракт инструмента переименованием
    (карта 006, T394).
    """
    text = str(value or "").strip()
    if not text:
        if required:
            raise ValueError(f"{field_name} не може бути порожнім")
        return ""
    lowered = text.lower()
    if "://" in lowered or lowered.startswith(("file:", "\\\\")):
        raise ValueError(
            f"{field_name}: очікується адреса всередині документа (стаття, пункт, "
            f"параграф), а не URL чи файл — отримано {text[:60]!r}"
        )
    if "\\" in text or re.search(r"[A-Za-z]:[/\\]", text) or text.startswith("/"):
        raise ValueError(
            f"{field_name}: очікується адреса всередині документа, а не шлях у "
            f"файловій системі — отримано {text[:60]!r}"
        )
    if ".." in text or text.lower().endswith(
        (".pdf", ".docx", ".doc", ".txt", ".rtf", ".zip", ".xml", ".html")
    ):
        raise ValueError(
            f"{field_name}: очікується адреса всередині документа, а не ім'я файла "
            f"— отримано {text[:60]!r}"
        )
    return text


def _normalize_str_tuple(value: object) -> tuple[str, ...]:
    """Список строк из чего угодно разумного; пустые элементы отбрасываются."""
    if value is None:
        return ()
    if isinstance(value, str):
        items: list[Any] = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise ValueError("очікується перелік рядків")
    return tuple(str(item).strip() for item in items if str(item).strip())


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _normalize_iso_date(value: Any, field_name: str) -> str:
    """Дата ``YYYY-MM-DD`` или пустая строка; иное — ошибка входа, не догадка."""
    text = str(value or "").strip()
    if not text:
        return ""
    if not _ISO_DATE.match(text):
        raise ValueError(f"{field_name}: очікується дата у форматі YYYY-MM-DD, отримано {text!r}")
    try:
        dt.date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{field_name}: {text!r} не є календарною датою") from None
    return text


def _normalize_language(value: Any) -> str:
    """Код языка ISO 639-1/2 в нижнем регистре или пустая строка (язык источника)."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if not re.match(r"^[a-z]{2,3}$", text):
        raise ValueError(f"language: очікується код мови (uk, en, lv…), отримано {text!r}")
    return text


class DocumentInput(_LawToolInput):
    """Вход чтения документа: единый словарь контракта v2 (ADR 0008).

    ``document_id`` — идентификатор в терминах источника (CELEX, ELI, номер
    акта Ради, ECLI, идентификатор PDF ICJ). ``language`` —
    запрошенная языковая версия; пусто — аутентичный язык источника.
    ``as_of`` — дата, на которую нужна редакция; пусто — текущая.
    """

    document_id: str
    language: str = ""
    as_of: str = ""
    #: Роль, читающая в этой сессии: расходует собственную квоту памяти
    #: доказательств (T180). Пусто — чтение в общий лимит сессии.
    agent_id: str = ""

    @field_validator("document_id", mode="before")
    @classmethod
    def normalize_document_id(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("document_id не може бути порожнім")
        return text

    @field_validator("agent_id", mode="before")
    @classmethod
    def normalize_agent_id(cls, value: Any) -> str:
        return _normalize_agent_id(value)

    @field_validator("language", mode="before")
    @classmethod
    def normalize_language(cls, value: Any) -> str:
        return _normalize_language(value)

    @field_validator("as_of", mode="before")
    @classmethod
    def normalize_as_of(cls, value: Any) -> str:
        return _normalize_iso_date(value, "as_of")


class GetFragmentInput(DocumentInput):
    """Одна единица цитирования: ``path`` — адрес внутри документа."""

    path: str
    force_refresh: bool = False

    @field_validator("path", mode="before")
    @classmethod
    def normalize_path(cls, value: Any) -> str:
        return _internal_address(value, "path", required=True)


class GetFragmentsInput(DocumentInput):
    """Несколько единиц одним вызовом."""

    paths: list[str]
    force_refresh: bool = False

    @field_validator("paths", mode="before")
    @classmethod
    def normalize_paths(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("paths must be a list")
        paths = [str(item).strip() for item in value if str(item).strip()]
        if not paths:
            raise ValueError("paths must be a non-empty list")
        return paths


class QueryDocumentInput(DocumentInput):
    """Документ целиком либо фрагмент, отвечающий ``query`` (локальный поиск)."""

    query: str = ""
    tokens: int = 5000
    force_refresh: bool = False

    @field_validator("query", mode="before")
    @classmethod
    def normalize_query(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("tokens", mode="before")
    @classmethod
    def clamp_tokens(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 5000
        return max(200, min(parsed, 20000))


class SearchAcrossLawsInput(_LawToolInput):
    query: str
    max_results: int = 5

    @field_validator("query", mode="before")
    @classmethod
    def normalize_query(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("max_results", mode="before")
    @classmethod
    def clamp_max_results(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 5
        return max(1, min(parsed, 20))


class SearchArticlesInput(_LawToolInput):
    query: str
    document_id: str = ""
    max_results: int = 5

    @field_validator("query", mode="before")
    @classmethod
    def normalize_query(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("document_id", mode="before")
    @classmethod
    def normalize_document_id(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("max_results", mode="before")
    @classmethod
    def clamp_max_results(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 5
        return max(1, min(parsed, 20))


class DiscoverLawsInput(_RegistryToolInput):
    query: str
    max_results: int = 10

    @field_validator("query", mode="before")
    @classmethod
    def normalize_query(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("max_results", mode="before")
    @classmethod
    def clamp_max_results(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 10
        return max(1, min(parsed, 25))


#: CELEX акта (не судової практики): секторна цифра 1–9, потім рік (4 цифри),
#: буква виду документа, номер. Той самий загальний вигляд, що перевірений
#: живим SPARQL 2026-09-09 на CELEX регламенту (:mod:`sources.eu_law` тримає
#: канонічну версію цього шаблону для читання актів; тут — своя копія, бо
#: `contracts.py` не залежить від `sources`, а не навпаки). Судова практика
#: (сектор ``6`` із двобуквеним кодом виду) сюди не підходить навмисно:
#: ``cites`` питає, що цитує акт, а не що цитує рішення.
_CITED_ACT_CELEX_RE = re.compile(r"^\d\d{4}[A-Z]\d+$")


def _normalize_cites(value: Any) -> str:
    """``cites`` приймає лише CELEX акта — вільний текст сюди не проходить.

    До джерела значення `cites` іде як буквальний ідентифікатор SPARQL-фільтра
    (T-cites, ADR 0009): рядок, що не є CELEX, відхиляється тут, ще не
    покинувши межу інструмента, а не перетворюється на пошук за словами.
    """
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if not _CITED_ACT_CELEX_RE.match(text):
        raise ValueError(
            f"cites: очікується CELEX акта (наприклад 32016R0679), отримано {text[:40]!r}; "
            "вільний текст у цьому полі не приймається і джерелу не надсилається"
        )
    return text


class SearchDecisionsInput(_LawToolInput):
    """Поиск практики одного форума.

    ``date_from``/``date_to`` — включительные границы даты решения (ADR 0008);
    ``from > to`` — ошибка входа. Поддержка фильтра объявляется по источнику:
    без доказанной поддержки — отказ ``unsupported_filter``, не молчание.

    ``cites`` — структурная операция «документы, что цитируют акт» (T-cites):
    вход — CELEX акта, а не свободный текст; форма проверяется здесь и не
    доходит до источника, если ей не соответствует.
    """

    query: str = ""
    case_number: str = ""
    cites: str = ""
    date_from: str = ""
    date_to: str = ""
    max_results: int = 10

    @field_validator("query", "case_number", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("cites", mode="before")
    @classmethod
    def normalize_cites(cls, value: Any) -> str:
        return _normalize_cites(value)

    @field_validator("date_from", mode="before")
    @classmethod
    def normalize_date_from(cls, value: Any) -> str:
        return _normalize_iso_date(value, "date_from")

    @field_validator("date_to", mode="before")
    @classmethod
    def normalize_date_to(cls, value: Any) -> str:
        return _normalize_iso_date(value, "date_to")

    @field_validator("max_results", mode="before")
    @classmethod
    def clamp_max_results(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 10
        return max(1, min(parsed, 20))

    @model_validator(mode="after")
    def the_period_runs_forward(self) -> "SearchDecisionsInput":
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError(
                f"date_from {self.date_from} пізніше за date_to {self.date_to}: період порожній"
            )
        return self

    @property
    def wants_date_filter(self) -> bool:
        return bool(self.date_from or self.date_to)


class GetDecisionInput(_LawToolInput):
    """Судебный документ по идентификатору форума; ``path`` — пункт/параграф."""

    document_id: str
    language: str = ""
    path: str = ""
    agent_id: str = ""

    @field_validator("agent_id", mode="before")
    @classmethod
    def normalize_agent_id(cls, value: Any) -> str:
        return _normalize_agent_id(value)

    @field_validator("document_id", mode="before")
    @classmethod
    def normalize_document_id(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("document_id не може бути порожнім")
        return text

    @field_validator("language", mode="before")
    @classmethod
    def normalize_language(cls, value: Any) -> str:
        return _normalize_language(value)

    @field_validator("path", mode="before")
    @classmethod
    def normalize_path(cls, value: Any) -> str:
        return _internal_address(value, "path")


class VerifyQuoteInput(_LawToolInput):
    """Звірка цитати з текстом за адресою (тікет 17).

    ``path`` необов'язковий: порожній означає пошук по всьому тексту документа.
    ``agent_id`` тут відсутній навмисно — операція нічого не запам'ятовує і
    квоти доказів не витрачає.
    """

    document_id: str
    path: str = ""
    quote: str
    language: str = ""
    as_of: str = ""

    @field_validator("document_id", mode="before")
    @classmethod
    def normalize_document_id(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("document_id не може бути порожнім")
        return text

    @field_validator("path", mode="before")
    @classmethod
    def normalize_path(cls, value: Any) -> str:
        return _internal_address(value, "path")

    @field_validator("quote", mode="before")
    @classmethod
    def normalize_quote(cls, value: Any) -> str:
        text = str(value or "")
        if not text.strip():
            raise ValueError("quote не може бути порожнім")
        return text

    @field_validator("language", mode="before")
    @classmethod
    def normalize_language(cls, value: Any) -> str:
        return _normalize_language(value)

    @field_validator("as_of", mode="before")
    @classmethod
    def normalize_as_of(cls, value: Any) -> str:
        return _normalize_iso_date(value, "as_of")


class GetCaseInput(_LawToolInput):
    """Публичная карточка производства по номеру дела (ADR 0006)."""

    case_number: str

    @field_validator("case_number", mode="before")
    @classmethod
    def normalize_case_number(cls, value: Any) -> str:
        text = " ".join(str(value or "").split())
        if not text:
            raise ValueError("case_number не може бути порожнім")
        return text


class SearchDebtorsInput(_RegistryToolInput):
    name: str = ""
    code: str = ""
    max_results: int = 10

    @field_validator("name", "code", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("max_results", mode="before")
    @classmethod
    def clamp_max_results(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 10
        return max(1, min(parsed, 50))


class DiscoverRegistriesInput(_RegistryToolInput):
    query: str
    max_results: int = 10

    @field_validator("query", mode="before")
    @classmethod
    def normalize_query(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("max_results", mode="before")
    @classmethod
    def clamp_max_results(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 10
        return max(1, min(parsed, 25))


class SearchTendersInput(_RegistryToolInput):
    query: str
    max_results: int = 10

    @field_validator("query", mode="before")
    @classmethod
    def normalize_query(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("max_results", mode="before")
    @classmethod
    def clamp_max_results(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 10
        return max(1, min(parsed, 25))


class GetTenderInput(_RegistryToolInput):
    tender_id: str

    @field_validator("tender_id", mode="before")
    @classmethod
    def normalize_tender_id(cls, value: Any) -> str:
        return str(value or "").strip()


class ToolErrorOutput(_ToolOutput):
    error: str
    code: str
    details: dict[str, Any] = Field(default_factory=dict)


class ListLawsOutput(_ToolOutput):
    laws: list[dict[str, Any]]
    total: int
    version: str
    server_version: str


class ResolveLawOutput(_ToolOutput):
    id: str | None
    title: str | None = None
    url: str | None = None
    ttl_days: int | None = None
    cache_ttl_days: int | None = None
    cached: bool | None = None
    fresh: bool | None = None
    discovered: bool | None = None
    source: str | None = None
    accepted_at: str | None = None
    updated_at: str | None = None
    error: str | None = None
    code: str | None = None
    details: dict[str, Any] | None = None


class _EvidenceOutput(_LegalTextOutput):
    """Ответ с текстом, зарегистрированным как доказательство (T123).

    ``evidence_id`` — идентификатор фрагмента в памяти сессии; ``None`` только
    когда содержимое не годится как доказательство (карточка, резюме), и тогда
    ``confirmable=false`` говорит об этом прямо.
    """

    document_id: str
    resolved_document_id: str | None = None
    evidence_id: str | None = None
    confirmable: bool = False
    #: Текст пригоден только как навигация: цитировать его нельзя, сверка по
    #: нему даёт отказ. Обратное к ``confirmable`` и названо прямо (T181).
    navigation_only: bool = False
    locator: str = ""


class GetArticleOutput(_EvidenceOutput):
    title: str
    path: str | None = None
    text: str
    url: str
    source: str | None = None
    from_cache: bool
    stale: bool = False
    retrieved_at: str


class QueryLawOutput(_EvidenceOutput):
    title: str
    text: str
    url: str
    source: str | None = None
    from_cache: bool
    stale: bool = False
    retrieved_at: str
    char_count: int
    full_text_chars: int
    articles_count: int
    #: Совпал ли запрос с текстом документа. ``None`` — запрос не задавался либо
    #: адаптер локальным поиском не занимается. Поле добавлено, потому что без
    #: него ответ ничем не отличал «вот фрагмент по запросу» от «запрос не нашёлся,
    #: показано начало акта»: текст в обоих случаях подлинный, а утверждение —
    #: разное (принцип III, T248).
    query_matched: bool | None = None
    #: Непустое ровно тогда, когда ``query_matched`` ложно: словами, а не намёком.
    query_notice: str = ""


class _NavigationOutput(_ToolOutput):
    """Выдача поиска: адреса документов, а не доказательства (T181).

    Сниппет строится нами из текста и обрезается по длине, поэтому он не
    является полученной от источника единицей цитирования: у него нет ни
    ``evidence_id``, ни конверта происхождения. Поле объявлено здесь, а не
    дописывается в каждом инструменте, чтобы поиск не мог однажды вернуть
    выдачу без него.
    """

    navigation_only: bool = True
    navigation_notice: str = (
        "Сніппети пошуку — адреси для наступного читання, а не докази: "
        "цитувати їх не можна, звірка за ними дає no_evidence. "
        "Прочитайте документ за document_id, щоб отримати evidence_id."
    )

    @model_validator(mode="after")
    def navigation_is_stated_and_not_left_to_the_default(self) -> "_NavigationOutput":
        """Пометка попадает в ответ, даже если инструмент её не заполнил.

        ``dump_tool_output`` отдаёт только явно заданные поля, поэтому значение
        по умолчанию до модели не доходит. Пометка «это не доказательство» —
        не то поле, отсутствие которого допустимо: инструмент, забывший её,
        вернул бы сниппет, неотличимый от единицы цитирования.
        """
        self.__pydantic_fields_set__.update({"navigation_only", "navigation_notice"})
        return self


class SearchAcrossLawsOutput(_NavigationOutput):
    query: str
    results: list[dict[str, Any]]
    searched_laws: int
    found_in: int


class SearchArticlesOutput(_NavigationOutput):
    query: str
    law_id: str | None = None
    results: list[dict[str, Any]]
    found: int
    backend: str


class DiscoverLawsOutput(_ToolOutput):
    query: str
    results: list[dict[str, Any]]
    found: int
    source: str
    from_cache: bool
    retrieved_at: str
    #: Стан каталогу карток: ``live`` — щойно завантажено, ``cache`` — свіжа
    #: копія в межах TTL, ``stale`` — джерело недоступне, відповідь зібрано зі
    #: старої копії. ``from_cache`` розрізняло лише «мережа чи диск» і після
    #: першого ж запису ставало ``True`` завжди (рев'ю тікета 15).
    catalogue_freshness: str = ""
    #: Коли каталог на диску востаннє вдалося завантажити. ``retrieved_at``
    #: каже лише, коли склали цю відповідь.
    catalogue_fetched_at: str = ""
    #: Заповнено лише для ``stale``: з якої дати каталог не оновлювався.
    stale_since: str = ""


class GetMultipleArticlesOutput(_EvidenceOutput):
    title: str
    #: path → результат: текст и evidence_id либо отказ по каждому локатору.
    fragments: dict[str, dict[str, Any]]
    not_found: list[str]
    url: str
    source: str | None = None
    from_cache: bool
    retrieved_at: str
    #: Локатор ответа, из которого собран конверт. Верхний уровень здесь не
    #: несёт текста вовсе, и реквизиты фрагмента с него сняты (D7, проба
    #: 2026-09-09): ответ, где рядом стояли `evidence_id: null` и `content_hash`
    #: первой статьи, читался как хеш всего ответа.
    envelope_of: str | None = None

    @model_validator(mode="after")
    def the_envelope_is_present_in_full(self) -> "GetMultipleArticlesOutput":
        """Конверт происхождения требуется от **фрагментов**, а не от обёртки.

        Базовое правило ``_LegalTextOutput`` спрашивает полный конверт с ответа,
        содержащего текст нормы. Здесь текста нет ни в одном поле верхнего
        уровня: он лежит в ``fragments``, и провенанс принадлежит каждому
        фрагменту отдельно — у них разные локаторы, разные ``content_hash`` и
        разные ``evidence_id``. Требовать конверт сверху означало бы требовать
        реквизиты одного фрагмента как реквизиты всех, что и было дефектом.
        """
        for locator, fragment in self.fragments.items():
            if not isinstance(fragment, dict) or fragment.get("code"):
                # Отказ по одному локатору — не текст; провенанса он не несёт.
                continue
            for required in ("evidence_id", "content_hash"):
                if not fragment.get(required):
                    raise ValueError(
                        f"фрагмент {locator!r} несёт текст, но без {required}: "
                        "прочитанное без происхождения доказательством не является"
                    )
        return self


class SearchDecisionsOutput(_NavigationOutput):
    query: str
    case_number: str | None = None
    #: CELEX акта з запиту «документи, що цитують акт» (T-cites); ``None`` —
    #: цей запит цитувань не питав.
    cites: str | None = None
    results: list[dict[str, Any]]
    found: int
    source: str
    retrieved_at: str
    #: Полнота и применённые фильтры (ADR 0008): усечённая выдача не обещает полноты.
    complete: bool = False
    applied_filters: dict[str, Any] = Field(default_factory=dict)
    #: Отказы по отдельным источникам/фильтрам, не стирающие результаты остальных.
    failures: list[dict[str, Any]] = Field(default_factory=list)


class DecisionOutput(_EvidenceOutput):
    url: str
    text: str
    char_count: int
    from_cache: bool
    source: str
    retrieved_at: str
    #: Вид материала: judgment / decision / order / opinion / summary / communicated.
    material_kind: str = "unknown"


class VerifyQuoteOutput(_ToolOutput):
    """Стисла відповідь звірки: статус, фрагмент і походження — без тексту одиниці.

    Тексту одиниці тут немає навмисно: звірка відповідає на питання про рядок,
    а читання одиниці лишається за ``get_article``/``get_decision``, які видають
    доказ. ``notice`` обов'язковий: без нього відповідь читалася б як висновок
    про те, що цитата підтверджує тезу.
    """

    status: str
    document_id: str
    resolved_document_id: str | None = None
    source_id: str | None = None
    document_class: str | None = None
    #: Вид документа виведено з ідентифікатора, а не заявлено викликом: читач
    #: обрано здогадом, і відповідь про це каже прямо.
    document_class_guessed: bool = False
    path: str = ""
    locator: str = ""
    quote_length: int = 0
    fragment: str = ""
    context_before: str = ""
    context_after: str = ""
    position: int = 0
    normalizations: list[str] = Field(default_factory=list)
    differences: str = ""
    #: Чому фрагмента немає: спільного місця не знайшлося, текст завеликий для
    #: нечіткого пошуку або джерело відповіло порожнім текстом.
    reason: str = ""
    revision: str = ""
    language: str = ""
    source_url: str = ""
    fetched_at: str = ""
    authenticity: str = ""
    confirmable: bool = False
    notice: str = Field(min_length=1)


class SearchDebtorsOutput(_ToolOutput):
    name: str | None = None
    code: str | None = None
    results: list[dict[str, Any]]
    found: int
    source: str
    retrieved_at: str


class DiscoverRegistriesOutput(_ToolOutput):
    query: str
    results: list[dict[str, Any]]
    found: int
    source: str
    retrieved_at: str


class ListRegistriesOutput(_ToolOutput):
    registries: list[dict[str, Any]]
    total: int


class SearchTendersOutput(_ToolOutput):
    query: str
    results: list[dict[str, Any]]
    found: int
    source: str
    retrieved_at: str


class TenderOutput(_ToolOutput):
    tender_id: str | None = None
    title: str | None = None
    status: str | None = None
    url: str | None = None
    source: str
    retrieved_at: str


class LawMetadataOutput(_ToolOutput):
    """Карточка документа. Карточка не доказывает цитату: ``confirmable`` всегда false."""

    document_id: str
    resolved_document_id: str | None = None
    title: str
    url: str
    confirmable: bool = False
    content_kind: str = "metadata"
    publication_kind: str = "card"
    retrieved_at: str
    # Поля картки EU (тікет 21); інші правопорядки їх не заповнюють.
    celex: str | None = None
    eli: str | None = None
    eli_alternatives: list[str] | None = None
    eurlex_url: str | None = None
    resource_type: str | None = None
    celex_sector: str | None = None
    celex_document_type: str | None = None
    date_document: str | None = None
    entry_into_force: str | list[str] | None = None
    end_of_validity: str | list[str] | None = None
    in_force: bool | None = None
    official_journal: dict[str, str | None] | None = None
    languages: list[str] | None = None
    latest_consolidation: dict[str, Any] | None = None
    consolidations_unavailable: str | None = None
    revision_as_of: dict[str, Any] | None = None


class CaseCardOutput(_ToolOutput):
    """Публичная карточка производства: только опубликованное, с датой получения."""

    case_number: str
    court: str
    published_status: str
    public_documents: list[dict[str, Any]] = Field(default_factory=list)
    source_url: str
    fetched_at: str
    notice: str = ""


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def dump_tool_output(model_type: type[_ToolOutput], payload: dict[str, Any]) -> dict[str, Any]:
    """Провалидировать ответ и снабдить его версией контракта."""
    dumped = model_type.model_validate(payload).model_dump(mode="json", exclude_unset=True)
    dumped.setdefault("schema_version", CONTRACT_VERSION)
    return dumped


def parse_tool_input(model_type: type[_ToolInput], **kwargs: Any) -> Any:
    """Разобрать вход инструмента: модель либо готовый отказ.

    Неизвестный правопорядок — ``unknown_legal_order`` со списком кодов; любая
    другая ошибка схемы — ``invalid_input`` с названием поля и причиной. Старые
    имена параметров (``law_id``, ``article``) до этого места не доходят: их
    отвергает сигнатура инструмента на уровне протокола.
    """
    from pydantic import ValidationError

    try:
        return model_type(**kwargs)
    except ValidationError as error:
        problems = error.errors()
        for problem in problems:
            location = ".".join(str(part) for part in problem.get("loc", ()))
            message = str(problem.get("msg", ""))
            if location == "legal_order" or "unknown_legal_order" in message:
                return unknown_legal_order(kwargs.get("legal_order", ""))
        details = [
            {
                "field": ".".join(str(part) for part in problem.get("loc", ())),
                "message": str(problem.get("msg", "")).removeprefix("Value error, "),
            }
            for problem in problems
        ]
        named = ", ".join(sorted({d["field"] for d in details if d["field"]}))
        return friendly_error(
            "Некоректні параметри виклику: "
            + "; ".join(f"{d['field']}: {d['message']}" for d in details),
            ErrorCode.INVALID_INPUT,
            {
                "problems": details,
                # T390: ошибка схемы — тоже отказ, и она обязана назвать
                # действие. До карты 006 эта ветка отдавала поле и причину, но
                # не действие, и на ней держалась часть тех тринадцати отказов
                # 09.09, каждый из которых объяснял «что», не объясняя «что
                # делать».
                "manual_path": (
                    f"Виправте параметр {named} за поясненням і повторіть виклик."
                    if named
                    else FALLBACK_MANUAL_PATH
                ),
            },
        )


def friendly_error(
    message: str,
    code: ErrorCode,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return dump_tool_output(
        ToolErrorOutput,
        {
            "error": message,
            "code": code.value,
            "details": dict(details or {}),
        },
    )


# ---------------------------------------------------------------------------
# T186 — задача, доказательства, аттестация (контракт 3.0.0, data-model.md)
#
# Схемы этого раздела — формат передачи между ведущим и субагентами плагина
# (ADR 0011 §3) и вход/выход инструментов задачи. Передаются структуры, а не
# «контекст»: каждая передача между агентами — место, где теряется привязка
# утверждения к доказательству, и схема это место закрывает.
#
# Ни одна модель здесь не имеет поля файла, пути, вложения или base64: ядро
# материалов дела не принимает (принцип VI, FR-303). Тест перечисляет схемы
# инструментов и падает, если такое поле появится.
# ---------------------------------------------------------------------------


class ClaimKind(str, Enum):
    """Вид утверждения. От него зависит, чем оно вправе опираться."""

    NORM = "norm"
    CASE_LAW = "case_law"
    FACT_FROM_CASE = "fact_from_case"
    INFERENCE = "inference"
    OPINION = "opinion"


#: Утверждение, у которого доказательства нет и не заявлено. Пишется в текст
#: видимой пометкой, а не умалчивается.
UNSUPPORTED = "UNSUPPORTED"

#: Цитата доказательства и пояснение релевантности: пределы из data-model.md.
#: Ограничение — не экономия, а граница: пакет доказательств не должен стать
#: копией документа в контексте модели.
QUOTE_MAX_CHARS = 600


def _normalize_quote_segments(value: Any) -> tuple[str, ...]:
    """Сегменты цитаты: каждый в пределах :data:`QUOTE_MAX_CHARS`.

    Предел остаётся тот же и по той же причине (пакет доказательств не должен
    стать копией документа в контексте модели), но он больше не делает норму
    нецитируемой: норма длиннее предела подаётся сегментами. Мера принята
    потому, что обход был измерен — норму резали на два утверждения ради того,
    чтобы каждая половина влезла в ``Claim.quote`` (FR-526, SC-504).
    """
    segments = _normalize_str_tuple(value)
    for index, segment in enumerate(segments, start=1):
        if len(segment) > QUOTE_MAX_CHARS:
            raise ValueError(
                f"quote_segments[{index}]: {len(segment)} символів при межі "
                f"{QUOTE_MAX_CHARS}. Довгу норму подають кількома сегментами, "
                "а не одним задовгим і не двома твердженнями"
            )
    return segments


def _case_document_name(value: Any, field_name: str = "case_ref") -> str:
    """Имя документа дела: ярлык пометки, никогда не адрес файла.

    Единственное место, где эта проверка написана. Свойство «в ядре нет ни
    одного поля файла, пути или вложения ни в одном входе ни одного
    инструмента» держится тем, что каждое поле, где имя документа дела вообще
    появляется, проходит здесь: :class:`CaseRef`, :class:`CaseLocator`,
    :class:`CaseFolderEntry`.
    """
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name}: имя документа дела не может быть пустым")
    if any(sep in text for sep in ("/", "\\", ":")) or text.startswith("."):
        raise ValueError(
            f"{field_name}: ожидается имя документа, а не путь — ядро каталогов "
            "юриста не знает и не принимает (принцип VI)"
        )
    return text


class CaseLocator(BaseModel):
    """Где именно в папке дела лежит факт: имя документа, страница или пункт.

    Контракт 4.0.0 (render-v4 §1, FR-511). До него ``Claim.case_ref`` был голым
    именем файла, и проверить факт было нельзя: юрист не знал, на какой
    странице его читать. Локатор — вторая половина пометки факта, без которой
    первая половина есть слово честью.

    ``file`` — **имя**, а не путь: разделитель пути отвергается. Это и есть то
    место, где инвариант «в ядре нет полей файла и пути» мог бы сломаться, если
    бы структура принимала адрес; она принимает ярлык (``tests/unit/
    test_documents.py``, render-v4 §6).

    ``locator`` необязателен на уровне модели и обязателен на уровне печати:
    пустой даёт отказ :class:`CaseRefWithoutLocator` — отказ, а не исключение
    валидации, потому что он адресован юристу и обязан назвать действие
    (принцип III).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str = Field(min_length=1, max_length=120)
    locator: str = Field(default="", max_length=120)
    question_id: str = ""

    @field_validator("file", mode="before")
    @classmethod
    def file_is_a_name_not_a_path(cls, value: Any) -> str:
        return _case_document_name(value, "case_ref.file")

    @field_validator("locator", mode="before")
    @classmethod
    def normalize_locator(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("question_id", mode="before")
    @classmethod
    def normalize_question_id(cls, value: Any) -> str:
        return _normalize_label(value, "question_id")

    @property
    def has_locator(self) -> bool:
        return bool(self.locator.strip())

    def as_text(self) -> str:
        """Людськочитана позначка факту справи для друку.

        Контракт 4.0.0 (render-v4 §1, data-model.md §2): ``CaseFact`` несе
        ``mark_printed_by_core: True``, а позначку ставить ядро при друку —
        для цього йому потрібна текстова форма локатора.

        Роздільник — кома, а не типографська крапка: рядок іде в документ
        рівня подання (``[ФАКТ ІЗ МАТЕРІАЛІВ СПРАВИ: {name}]``), де «Ухвала
        суду.pdf, с. 3, п. 12» читається як звичайне посилання на матеріали,
        а «·» читається як службовий знак системи. Форма одна на весь
        продукт і живе тут: до 005 її повторював ``citations.py``.
        """
        if self.locator.strip():
            return f"{self.file}, {self.locator}"
        return self.file


class Claim(BaseModel):
    """Утверждение проекта и то, на чём оно стоит.

    Модель не пишет ссылку на норму — она называет ``evidence_id``; ссылку
    печатает сервер (ADR 0011 §4). Поэтому утверждение вида ``norm`` или
    ``case_law`` без доказательства не рендерится, а получает отказ
    ``unattested_norm_claim``.

    Контракт 4.0.0 забрал отсюда два поля-двойника (render-v4 §0, строки 2 и 3):

    * ``quote`` заменён на ``quote_segments``. Цитата подаётся один раз — в
      ``attest_claim``; здесь называются сегменты, по которым идёт сверка на
      печати, и норма длиннее предела больше не остаётся нецитированной.
    * ``case_ref`` из голого имени файла стал структурой :class:`CaseLocator`:
      имя без страницы или пункта — пометка, которую нельзя проверить.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    text: str = Field(min_length=1)
    kind: ClaimKind
    evidence_ids: tuple[str, ...] = ()
    case_ref: CaseLocator | None = None
    quote_segments: tuple[str, ...] = ()

    @field_validator("claim_id", mode="before")
    @classmethod
    def normalize_claim_id(cls, value: Any) -> str:
        return _normalize_label(value, "claim_id", required=True)

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def normalize_evidence_ids(cls, value: Any) -> tuple[str, ...]:
        return _normalize_str_tuple(value)

    @field_validator("quote_segments", mode="before")
    @classmethod
    def normalize_quote_segments(cls, value: Any) -> tuple[str, ...]:
        return _normalize_quote_segments(value)

    @property
    def support(self) -> str:
        """Чем подкреплено утверждение: доказательствами, делом или ничем."""
        if self.evidence_ids:
            return "evidence"
        if self.case_ref is not None:
            return "case_ref"
        return UNSUPPORTED

    @property
    def quoted_text(self) -> str:
        """Цитата целиком — сегменты подряд, как они стоят в источнике."""
        return "".join(self.quote_segments)

    def locator_refusal(self) -> CaseRefWithoutLocator | None:
        """Отказ ``case_ref_without_locator`` или ``None``, если локатор назван.

        Отказ строится здесь, а возвращает его печать: он адресован юристу и
        обязан назвать действие, а исключение валидации не называет ничего
        (принцип III, FR-511).
        """
        if self.kind is not ClaimKind.FACT_FROM_CASE or self.case_ref is None:
            return None
        if self.case_ref.has_locator:
            return None
        return CaseRefWithoutLocator(claim_id=self.claim_id, file=self.case_ref.file)

    @model_validator(mode="after")
    def a_norm_claim_names_its_evidence(self) -> "Claim":
        if self.kind in (ClaimKind.NORM, ClaimKind.CASE_LAW) and not self.evidence_ids:
            raise ValueError(
                f"твердження {self.claim_id!r} виду {self.kind.value} без evidence_id: "
                "посилання на норму друкує сервер з доказу, а не модель із пам'яті "
                "(принцип II)"
            )
        if self.kind is ClaimKind.FACT_FROM_CASE and self.case_ref is None:
            raise ValueError(
                f"твердження {self.claim_id!r} виду fact_from_case без case_ref: факт "
                "справи позначається джерелом, інакше він невідрізнюваний від норми"
            )
        if self.case_ref is not None and self.kind is not ClaimKind.FACT_FROM_CASE:
            raise ValueError(f"твердження {self.claim_id!r}: case_ref має лише факт справи")
        return self


class Section(BaseModel):
    """Раздел скелета: идентификатор и утверждения.

    Контракт 4.0.0 (render-v4 §0, строка 6) забрал отсюда ``purpose``.
    Назначение и заголовок раздела берутся из ``SectionPlan`` каталога видов по
    ``section_id``: свободный текст назначения печатался как заголовок и давал
    в документе строки вида ``s3 — w3_q1`` (SC-505). Идентификатор, которого
    нет в профиле вида, — отказ :class:`UnknownSection`, а не заголовок.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    section_id: str
    claims: tuple[Claim, ...] = ()

    @field_validator("section_id", mode="before")
    @classmethod
    def normalize_section_id(cls, value: Any) -> str:
        return _normalize_label(value, "section_id", required=True)


class ArgumentSkeleton(BaseModel):
    """Скелет документа: разделы и утверждения — вход рендера.

    ``task_id`` здесь больше нет (контракт 4.0.0, render-v4 §0, строка 1):
    задача называется один раз, в самом вызове. Два поля с одним смыслом в
    одном вызове дают расхождение «task_id скелета ≠ task_id задачи», которое
    ядро принимало молча, подписывая документ одной задачи именем другой.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sections: tuple[Section, ...] = ()

    @model_validator(mode="after")
    def claim_ids_are_unique_across_the_document(self) -> "ArgumentSkeleton":
        seen: set[str] = set()
        for section in self.sections:
            for claim in section.claims:
                if claim.claim_id in seen:
                    raise ValueError(
                        f"claim_id {claim.claim_id!r} повторюється: аттестація "
                        "прив'язується до ідентифікатора, і двійник робить її неоднозначною"
                    )
                seen.add(claim.claim_id)
        return self


# ---------------------------------------------------------------------------
# Пакетная аттестация (карта 006, T373; contracts/core.md §1)
# ---------------------------------------------------------------------------
#
# Зачем пакет. Прогон 10.09.2026 измерил цену вызова на утверждение: документ на
# сорок восемь тысяч знаков — это десятки отдельных обращений, и каждое из них
# юрист ждал. Пакет не ослабляет проверку: тот же дословный сверщик
# отрабатывает каждую опору, а агрегат не может оказаться `confirmed`, если
# хоть одна опора не подтверждена.
#
# Границы 256 утверждений и 1 МиБ — инженерные пределы ресурса, а не
# калибровочные пороги. Документ, который в них не влезает, получает явный
# отказ по размеру: молча аттестовать первые двести пятьдесят шесть утверждений
# и назвать это документом — ровно тот дефект, ради которого пакет и типизуется.


#: Страница большого ответа (FR-533). Размер по умолчанию и предел — не
#: экономия трафика: роль, получившая ответ, который не может прочитать
#: наличными средствами, не получила ответа вовсе.
PAGE_SIZE_DEFAULT = 20000
PAGE_SIZE_MAX = 80000


class _PagedInput(BaseModel):
    """Запрошенная страница большого ответа: номер и размер.

    Размер зажимается в пределы, как ``tokens`` у чтения: запрошенная страница
    в миллион символов — не отказ, а тот же ответ страницами.
    """

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=PAGE_SIZE_DEFAULT, ge=1, le=PAGE_SIZE_MAX)

    @field_validator("page", mode="before")
    @classmethod
    def page_starts_at_one(cls, value: Any) -> int:
        return max(1, int(value or 1))

    @field_validator("page_size", mode="before")
    @classmethod
    def clamp_page_size(cls, value: Any) -> int:
        return min(PAGE_SIZE_MAX, max(1, int(value or PAGE_SIZE_DEFAULT)))


# ---------------------------------------------------------------------------
# T190 — вход и выход инструментов задачи (контракт 3.0.0)
# ---------------------------------------------------------------------------


class _TaskToolInput(_ToolInput):
    """Вход инструмента задачи: правопорядок допускает ``*``.

    Задача идёт по нескольким правопорядкам сразу (принцип VII: правопорядок —
    параметр, а не семейство инструментов), поэтому здесь допустима звёздочка,
    как в карте покрытия и методике.
    """

    legal_order: str = "*"

    @field_validator("legal_order", mode="before")
    @classmethod
    def star_or_known(cls, value: Any) -> str:
        code = normalize_legal_order_code(value) or "*"
        if code == "*" or is_known_legal_order(code):
            return code
        raise ValueError(
            f"unknown_legal_order: {value!r}; известные коды: {', '.join(KNOWN_LEGAL_ORDER_CODES)}"
        )


class RenderAttestedInput(_TaskToolInput, _PagedInput):
    """Скелет документа для печати (контракт 4.0.0, render-v4 §1).

    ``task_id`` называется здесь и только здесь: у скелета своего больше нет.
    ``kind`` обязателен — пустое значение больше не означает «вид по
    умолчанию»: вид документа выбирает юрист, и молча напечатанный не тот вид
    замечают уже в суде.
    """

    task_id: str
    skeleton: ArgumentSkeleton
    style_profile_id: str = ""
    #: Языки, заявленные задачей (``Question.languages``). Пустой перечень —
    #: «задача языков не заявляла», а не «языки совпали»: разница видна в тексте
    #: документа, где норма, подтверждённая одной версией при заявленных двух,
    #: получает пометку. Поле необязательное, чтобы вызов без него оставался
    #: рабочим.
    languages: tuple[str, ...] = ()
    #: Вид документа из закрытого каталога (`document_kinds.DocumentKind`).
    #: Обязателен с 4.0.0; неизвестное значение отвергается печатью с
    #: `unknown_document_kind`, а не заменяется умолчанием.
    kind: str = Field(min_length=1)
    #: Язык производства форума. Обязателен для видов с ``audience="court"``
    #: (FR-519): документ иным языком суд не читает, и узнать об этом после
    #: подачи поздно. Отсутствие — отказ ``proceeding_language_missing``,
    #: который строит :meth:`proceeding_language_refusal`.
    proceeding_language: str = ""
    #: Реестры документов `04` и `07` (T295). Их текст ядро складывает **из
    #: данных**, а не из утверждений модели (`document-package.md` §1: «ядро з
    #: реєстру доводів», «ядро з coverage_checklist»), поэтому у этих двух
    #: видов вход другой: не скелет, а перечни. Поля необязательные — вызов
    #: любого другого вида остаётся прежним.
    counterparty_arguments: tuple[CounterpartyArgument, ...] = ()
    attribution_checks: tuple[AttributionCheck, ...] = ()
    #: Что из пакета другой стороны прочитано и откуда взято — по строке на
    #: документ. Раздел `1` документа `04` состоит из них.
    counterparty_package: tuple[str, ...] = ()
    #: Прогалины для документа `07`. Пустой перечень при ``kind="gaps"`` не
    #: ошибка: ядро строит их само из чек-листа покрытия задачи.
    gaps: tuple[Gap, ...] = ()
    #: ``evidence_id`` тексту, у якому прочитані практичні вказівки форуму про
    #: обсяг документа (FR-522). Число не зберігається в коді ядра: спека
    #: прямо забороняє брати вказівки форуму «з пам'яті моделі» (assumptions,
    #: `spec.md`) — воно береться механічним розбором тексту, який ядро вже
    #: прочитало і поклало в кеш сеансу тим самим шляхом, що й будь-яку іншу
    #: норму. Порожнє значення — перевірка не виконується, це не відмова.
    forum_guidance_evidence_id: str = ""

    @field_validator("task_id", mode="before")
    @classmethod
    def normalize_task_id(cls, value: Any) -> str:
        return _normalize_label(value, "task_id", required=True)

    @field_validator("forum_guidance_evidence_id", mode="before")
    @classmethod
    def normalize_forum_guidance_evidence_id(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("style_profile_id", mode="before")
    @classmethod
    def normalize_style_profile_id(cls, value: Any) -> str:
        return _normalize_label(value, "style_profile_id")

    @field_validator("kind", mode="before")
    @classmethod
    def normalize_kind(cls, value: Any) -> str:
        text = str(value or "").strip().lower()
        if not text:
            raise ValueError(
                "kind: вид документа обирає юрист. Порожнє значення більше не "
                "означає вид за замовчуванням (контракт 4.0.0): назвіть вид зі "
                "списку document_kinds.known_kinds()"
            )
        return text

    @field_validator("proceeding_language", mode="before")
    @classmethod
    def normalize_proceeding_language(cls, value: Any) -> str:
        return _normalize_language(value)

    @field_validator("languages", mode="before")
    @classmethod
    def normalize_languages(cls, value: Any) -> tuple[str, ...]:
        return tuple(code.lower() for code in _normalize_str_tuple(value))

    def proceeding_language_refusal(self) -> "ProceedingLanguageMissing | None":
        """Отказ ``proceeding_language_missing`` или ``None``, если язык назван.

        Адресат вида берётся из каталога: требование языка производства
        относится к документам суда, а не ко всякой печати. Неизвестный вид
        здесь молчит — на него отвечает ``unknown_document_kind``, и два отказа
        на один дефект были бы двумя разными рецептами.
        """
        import document_kinds

        profile = document_kinds.profile_for(self.kind)
        if profile is None or profile.audience != "court":
            return None
        if self.proceeding_language.strip():
            return None
        return ProceedingLanguageMissing(kind=self.kind, audience=profile.audience)

    def language_mismatch_warning(self) -> str:
        """``proceeding_language_mismatch`` или пусто (FR-519).

        Расхождение объявляется **до** печати и предупреждением, а не отказом:
        рабочая копия языком юриста — законный случай, молчание о ней — нет.
        """
        if not self.proceeding_language or not self.languages:
            return ""
        if self.languages[0] == self.proceeding_language:
            return ""
        return WarningCode.PROCEEDING_LANGUAGE_MISMATCH.value


# ---------------------------------------------------------------------------
# T219 — вход и выход инструментов воркфлоу задачи (контракт 3.1.0, FR-401…408)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# US3: Покриття, аргументи іншої сторони, перевірка атрибуції
# (data-model.md §3, render-v4 §5, FR-541…FR-548)
# ---------------------------------------------------------------------------


class CounterpartyStrength(str, Enum):
    """Оцінка сили аргументу іншої сторони (data-model.md §3)."""

    WEAK = "weak"
    MEDIUM = "medium"
    STRONG = "strong"


class CounterpartyArgument(BaseModel):
    """Один аргумент іншої сторони (data-model.md §3, FR-543).

    ``as_formulated`` береться з тексту іншої сторони з локатором — переказ
    із робочих матеріалів доводом іншої сторони не є.
    ``locator_in_their_package`` обов'язковий: без нього аргумент не
    приймається.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    no: int = Field(ge=1)
    as_formulated: str = Field(min_length=1)
    locator_in_their_package: str = Field(min_length=1)
    their_authority: str = ""
    their_authority_read: bool = False
    our_answer: str = ""
    our_evidence_ids: tuple[str, ...] = ()
    strength: CounterpartyStrength = CounterpartyStrength.MEDIUM


class AttributionCheck(BaseModel):
    """Перевірка атрибуції одного аргументу (data-model.md §3, FR-544).

    ``confirmed=False`` блокує друк розділу — прецедент помилки: іншій
    стороні приписано твердження, яке вона у своїй скарзі прямо заперечує.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    argument_no: int = Field(ge=1)
    attributed_statement: str = Field(min_length=1)
    locator: str = ""
    confirmed: bool = False


#: Причини, з яких джерела немає. Це підмножина кодів відмов
#: (:class:`FailureCode`), а не власний перелік: прогалина народжується з
#: відмови джерела, і два різні словники для одного явища розійшлися б.
_GAP_REASON_CODES: frozenset[str] = frozenset(
    {
        FailureCode.SOURCE_UNAVAILABLE.value,
        FailureCode.NOT_COVERED.value,
        FailureCode.NOT_FOUND.value,
        FailureCode.REVISION_UNKNOWN.value,
    }
)


class Gap(BaseModel):
    """Прогалина покриття (data-model.md §3, FR-514).

    ``in_case_folder=True`` виключає запис із прогалин: джерело є в теці
    справи, просто ще не прочитано. Числа записуються цифрами.
    ``is_decisive`` позначає прогалину, що здатна вирішально вплинути на
    результат справи — такі виводяться першими.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    gap_id: str = Field(min_length=1)
    what_is_missing: str = Field(min_length=1)
    why_it_matters: str = ""
    address: str = ""
    who: str = ""
    due: str = ""
    checkbox: bool = False
    priority: int = Field(default=0, ge=0)
    in_case_folder: bool = False
    is_decisive: bool = False
    #: Чому джерела немає — код відмови джерела (``source_unavailable``,
    #: ``not_covered``, ``not_found``, ``revision_unknown``) або порожньо, що
    #: означає «продукт це вміє, але ще не читав». Код визначає розділ
    #: документа `07`: прогалина, яка не називає причини, стоїть у розділі
    #: «Ще не прочитано», а не тоне серед відмов джерел, яких не було.
    reason_code: str = ""

    @field_validator("reason_code", mode="before")
    @classmethod
    def known_reason_code(cls, value: Any) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        if text not in _GAP_REASON_CODES:
            raise ValueError(
                f"reason_code {text!r} не є кодом відмови джерела; очікується "
                f"один із: {', '.join(sorted(_GAP_REASON_CODES))} — або порожньо"
            )
        return text


# ---------------------------------------------------------------------------
# Досбор моделей, чьи поля названы раньше своих типов
# ---------------------------------------------------------------------------
#
# ``RenderAttestedInput`` объявлен до моделей US3 (реестр доводов, звірки,
# прогалины), потому что стоит рядом с остальными входами инструментов, а те
# модели — рядом с покрытием. Аннотации в модуле строковые (``from __future__
# import annotations``), поэтому pydantic достраивает вход здесь, когда типы
# уже определены.
RenderAttestedInput.model_rebuild()
