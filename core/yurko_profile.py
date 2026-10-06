"""Профиль обработки: собственное исполнение против инженерного стенда (T125, ADR 0009).

Принцип VIII (NON-NEGOTIABLE): сторонние размещённые сервисы не участвуют в
обработке пользовательских запросов. Профиль — исполняемая форма этой границы:

* ``legal`` (по умолчанию) — принимаемый юридический профиль. Hosted-эмбеддинги
  (Gemini) и hosted-база (Supabase/любой не локальный DATABASE_URL) блокируют
  запуск; свободный текст пользователя не уходит ни одному источнику — поиск
  по свободному тексту выполняется только локально, иначе честный отказ с
  ручным путём. Наружу уходят только заново собранные публичные идентификаторы
  и структурные параметры (белый список адаптера).
* ``engineering`` — инженерный стенд: те же проверки только предупреждают,
  свободный поиск в официальных источниках разрешён. Такой профиль не получает
  приёмку юридического использования (ADR 0009, H3) и говорит об этом в
  ``list_coverage``/``get_methodology``.

Граница профиля описывает сервер. Клиент и модель, вызывающие сервер, входят в
аттестацию профиля документально (docs/release-002.md), сервер их проверить не
может — и не делает вид, что может.
"""

from __future__ import annotations

import logging
import os
from enum import Enum
from typing import Mapping, MutableMapping
from urllib.parse import urlparse

logger = logging.getLogger("ukraine-laws")

__all__ = [
    "Profile",
    "ProfileViolation",
    "current_profile",
    "free_text_egress_allowed",
    "profile_summary",
    "validate_environment",
]


class Profile(str, Enum):
    LEGAL = "legal"
    ENGINEERING = "engineering"


class ProfileViolation(RuntimeError):
    """Конфигурация несовместима с принимаемым профилем; запуск запрещён."""


_LOCAL_DB_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "host.docker.internal"})


def current_profile() -> Profile:
    raw = os.getenv("YURKO_PROFILE", "legal").strip().lower()
    if raw in ("engineering", "eng", "dev"):
        return Profile.ENGINEERING
    return Profile.LEGAL


def free_text_egress_allowed() -> bool:
    """Можно ли отправлять свободный текст пользователя внешнему источнику."""
    return current_profile() is Profile.ENGINEERING


def _database_is_own(database_url: str, env: Mapping[str, str] | None = None) -> bool:
    """Локальная база или явное свидетельство владельца ``YURKO_OWN_DATABASE=1``.

    ``env`` — то же окружение, по которому идёт вся остальная проверка. До
    T477 эта функция читала ``os.environ`` напрямую, и :func:`validate_environment`
    с явным словарём отвечала не про него: свидетельство владельца в словаре
    игнорировалось, а в процессе — подхватывалось. Проверка, берущая половину
    состояния не оттуда, откуда остальную, рано или поздно отвечает про другое
    окружение — и именно это делало её непроверяемой тестом.
    """
    if not database_url:
        return True
    source = os.environ if env is None else env
    if str(source.get("YURKO_OWN_DATABASE", "") or "").strip().lower() in ("1", "true", "yes"):
        return True
    try:
        host = (urlparse(database_url).hostname or "").lower()
    except ValueError:
        return False
    return host in _LOCAL_DB_HOSTS


def validate_environment(env: dict[str, str] | None = None) -> list[str]:
    """Проверить окружение на hosted-пути. Возвращает перечень нарушений.

    В профиле ``legal`` любое нарушение — :class:`ProfileViolation` при старте
    сервера (см. ``server.py``); в ``engineering`` — предупреждение в журнале.
    """
    values = dict(os.environ if env is None else env)
    violations: list[str] = []

    provider = values.get("EMBEDDING_PROVIDER", "").strip().lower()
    google_key = values.get("GOOGLE_API_KEY", "").strip()
    if provider == "gemini" or (not provider and google_key):
        violations.append(
            "hosted embeddings: EMBEDDING_PROVIDER=gemini (або GOOGLE_API_KEY без явного "
            "EMBEDDING_PROVIDER=null|local) надсилає текст запиту Google"
        )

    use_pg = values.get("USE_PG_BACKEND", "0").strip().lower() in ("1", "true", "yes")
    database_url = values.get("DATABASE_URL", "").strip()
    if use_pg and database_url and not _database_is_own(database_url, values):
        violations.append(
            "hosted database: DATABASE_URL вказує на не локальний хост без "
            "YURKO_OWN_DATABASE=1 — база під чужим контролем у потоці запиту"
        )

    # ``GOOGLE_API_KEY`` тут з 11.09.2026 (T478). Доти перевірка була
    # непослідовною: ключ Google ловився лише як **провайдер ембедингів** — і
    # явний ``EMBEDDING_PROVIDER=null`` знімав це зауваження разом із ключем,
    # який лишався в оточенні. Виходило, що ключ, яким у цьому профілі
    # користуватися заборонено, тримати в оточенні сервера було можна. Для
    # решти провайдерів таке правило вже стояло; винятком був той один, який
    # продукт справді вміє викликати, — тобто найнебезпечніший.
    #
    # Завантаження корпусу це не зачіпає: ``enforce_profile`` викликає лише
    # ``server.py`` при старті, а ``ingest.yml`` — окремий процес, і ключ там
    # на місці. Рахувати ембединги публічних актів офлайн і надсилати запит
    # юриста в реальному часі — різні речі, і принцип VIII забороняє другу.
    for name in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"):
        if values.get(name, "").strip():
            violations.append(
                f"{name} присутній в оточенні сервера: сервер не викликає hosted-модель, "
                "ключ у його оточенні — зайва поверхня (приберіть із .env сервера)"
            )

    # T240/FR-419. Движок воркфлоу тянет ``langsmith`` в процесс: он
    # обязательная зависимость ``langchain-core``, который импортирует его на
    # уровне модуля. Измерено 2026-09-08: одной ``LANGSMITH_TRACING`` довольно,
    # чтобы содержимое узлов ушло на ``api.smith.langchain.com`` (на тривиальном
    # графе — 3 783 байта). Основной механизм — белый список окружения в
    # ``server_env.py``; эта проверка вторая и независимая: если переменная всё
    # же оказалась в процессе, профиль ``legal`` не стартует.
    for name in sorted(values):
        upper = name.upper()
        if (upper.startswith("LANGSMITH_") or upper.startswith("LANGCHAIN_")) and values[
            name
        ].strip():
            violations.append(
                f"{name} присутній в оточенні сервера: змінні трасування LangSmith "
                "надсилають вміст вузлів воркфлоу на api.smith.langchain.com. "
                "Запускайте ядро через server_env.py"
            )
    return violations


#: Ключі hosted-провайдерів: у юридичному профілі жоден із них ядру не потрібен.
HOSTED_MODEL_KEYS: tuple[str, ...] = (
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
)


def conform_environment(env: MutableMapping[str, str] | None = None) -> list[str]:
    """Прибрати з оточення процесу те, чим у профілі ``legal`` користуватися не можна.

    Навіщо це окремо від :func:`enforce_profile`. Перевірка відповідає на
    питання «чи можна так запускатися», і відповідь «ні» зупиняє старт. Але
    частину «ні» ядро здатне виправити саме, і тоді падіння — не безпека, а
    відмова працювати через чужу конфігурацію, яку ніхто не збирався
    використовувати.

    Виміряно 11.09.2026 на живому деплої: Render синхронізує змінні з
    ``render.yaml`` лише при явному застосуванні блупринта, а звичайний push
    деплоїть код і лишає оточення сервісу як було. Новий образ зустрів
    ``GOOGLE_API_KEY`` від часів інженерного стенда, впав із
    :class:`ProfileViolation` — і платформа лишила працювати стару збірку. Зовні
    це виглядало як «нічого не сталося».

    Що тут робиться і чим це **не** є. Це не приховування порушення: ключ
    справді видаляється з ``os.environ``, тобто стає недоступним будь-якому коду
    в процесі, а база, про яку немає свідчення власника, справді не
    використовується — прапорець вимикається до того, як його прочитають. Після
    цього :func:`validate_environment` бачить оточення таким, яким воно стало, і
    все, чого ядро виправити не змогло, зупиняє старт як раніше.

    Кожна дія повертається рядком: мовчазне приведення було б гіршим за
    падіння — юрист має знати, що саме вимкнено і чому.
    """
    source = os.environ if env is None else env
    actions: list[str] = []
    if current_profile() is not Profile.LEGAL:
        return actions

    for name in HOSTED_MODEL_KEYS:
        if str(source.get(name, "") or "").strip():
            del source[name]
            actions.append(
                f"{name} прибрано з оточення процесу: у профілі legal ядро hosted-моделей "
                "не викликає, і ключ у оточенні лишався б зайвою поверхнею"
            )

    use_pg = str(source.get("USE_PG_BACKEND", "0") or "").strip().lower() in ("1", "true", "yes")
    database_url = str(source.get("DATABASE_URL", "") or "").strip()
    if use_pg and database_url and not _database_is_own(database_url, source):
        source["USE_PG_BACKEND"] = "0"
        actions.append(
            "USE_PG_BACKEND вимкнено: база не локальна і свідчення власника "
            "(YURKO_OWN_DATABASE=1) немає. Міжнародне право це не зачіпає — воно "
            "читається адаптерами напряму; український корпус лишається недоступним, "
            "доки свідчення не з'явиться"
        )
    return actions


def enforce_profile() -> list[str]:
    """Применить профиль к текущему окружению при старте.

    ``legal`` с нарушениями — исключение; ``engineering`` — предупреждения.
    Возвращает перечень нарушений для диагностики.
    """
    violations = validate_environment()
    if not violations:
        return []
    if current_profile() is Profile.LEGAL:
        raise ProfileViolation(
            "Профіль legal не допускає hosted-обробки; виправте оточення або запустіть "
            "YURKO_PROFILE=engineering як інженерний стенд (без юридичної приймання): "
            + "; ".join(violations)
        )
    for violation in violations:
        logger.warning("engineering profile: %s", violation)
    return violations


def profile_summary() -> dict[str, object]:
    profile = current_profile()
    return {
        "profile": profile.value,
        "free_text_egress": free_text_egress_allowed(),
        "legal_acceptance": profile is Profile.LEGAL,
        "notice": (
            "Профіль legal: вільний текст користувача не надсилається джерелам; клієнт і "
            "модель мають виконуватися під власним контролем."
            if profile is Profile.LEGAL
            else "Профіль engineering: інженерний стенд, юридичної приймання не має."
        ),
    }
