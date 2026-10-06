"""Вхід МСП: запускає ``server.py`` окремим процесом з оточенням за білим списком.

Навіщо окремий процес, а не рядок у документації. Змінна оточення, поставлена
чимось, що запускає клієнта (трасування ``LANGSMITH_*``, ключі хмарних моделей),
доїде до сервера й може відправити роботу юриста третій стороні. Блок ``env`` у
``.mcp.json`` цього не закриває: він **додає** змінні до успадкованих, а не
замінює їх. Закриває лише запуск із побудованим оточенням: змінна, якої немає
в білому списку, не доходить до процесу сервера **на рівні ОС**, а не видаляється
з ``os.environ`` уже всередині нього.

Що робить цей модуль:

* :func:`sanitized_environment` будує оточення: імена з білого списку
  (:data:`ALLOWED_NAMES`) і будь-які з префіксами :data:`ALLOWED_PREFIXES`
  (``YURKO_``, ``FASTMCP_``). ``PYTHONIOENCODING=utf-8`` ставиться завжди, а
  ``FASTMCP_CHECK_FOR_UPDATES=off`` — якщо не задано: перевірка оновлень є
  мережевим викликом, якого юрист не замовляв;
* :func:`resolve_python` шукає інтерпретатор, що імпортує ядро (``fastmcp``):
  ``YURKO_PYTHON`` (без перевірки) → поточний → ``.venv-dev`` → ``.venv`` теки коду.
  Не знайшовся жоден — :class:`PythonNotFound` з командою лікування, а не
  ``ModuleNotFoundError`` із процесу сервера трьома екранами глибше;
* :func:`main` запускає ``server.py`` цим інтерпретатором і прозоро передає йому
  stdin/stdout/stderr — транспорт stdio працює, клієнт різниці не бачить.
  ``--self-check`` друкує оточення, яке справді отримав дочірній процес.

Змінні налаштування з README (розділ «Налаштування») входять до білого списку
поіменно; ``PYTHONPATH`` і ``PYTHONHOME`` у ньому немає навмисно: через них можна
підмінити код, який імпортує сервер.

Межа білого списку: він не захищає від коду, що вже виконується в процесі сервера.
Це межа оточення, а не пісочниця.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

__all__ = [
    "ALLOWED_NAMES",
    "ALLOWED_PREFIXES",
    "CORE_PROBE_MODULE",
    "VENV_DIR_NAMES",
    "YURKO_PYTHON_ENV",
    "PythonNotFound",
    "sanitized_environment",
    "resolve_python",
    "server_command",
    "main",
]

#: Переменные, без которых процесс на Windows и на POSIX не запускается или
#: теряет способность ходить по HTTPS. Список не «на всякий случай»: каждая
#: строка здесь либо нужна интерпретатору, либо нужна TLS.
_OS_ESSENTIALS: frozenset[str] = frozenset(
    {
        # Windows
        "SystemRoot",
        "SYSTEMROOT",
        "windir",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "OS",
        "LOCALAPPDATA",
        "APPDATA",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "USERNAME",
        # POSIX
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        # TLS: без своего набора корней сервер не дойдёт до государственных
        # источников на машине с корпоративным корнем.
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
    }
)

#: Прокси пропускаются намеренно. Прокси видит запросы — это верно; но прокси
#: юриста принадлежит юристу или его организации, а не поставщику платформы, и
#: молча оборвать единственный путь в сеть означало бы поставить продукт,
#: который у половины пользователей не работает без объяснения. Решение
#: названо здесь, а не спрятано в отсутствии строки.
_PROXY: frozenset[str] = frozenset(
    {"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"}
)

#: Змінні інтерпретатора, які поставка задає осмислено. ``PYTHONPATH`` і
#: ``PYTHONHOME`` тут відсутні навмисно: вони підміняють код, який імпортує сервер
#: (аудит 22.1, № 20), а сервер стартує тим інтерпретатором і тими пакетами, що
#: вибрав :func:`resolve_python`, — і не потребує ні того, ні другого.
_PYTHON: frozenset[str] = frozenset(
    {
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
        "VIRTUAL_ENV",
    }
)

#: Власні змінні продукту без префікса ``YURKO_`` — ті, що названі в README
#: (розділи «Запуск» і «Налаштування»). Кожна тут потрібна серверу: без запису
#: у списку задокументоване «``RADA_LIVE_CHANNEL=0`` вимикає живий канал Ради»
#: мовчки не діяло б (аудит 22.1, № 8). Що починається з ``YURKO_``, проходить
#: по префіксу: це оточення, яким розпоряджається власник продукту.
_PRODUCT: frozenset[str] = frozenset(
    {
        "USE_PG_BACKEND",
        "DATABASE_URL",
        "EMBEDDING_PROVIDER",
        "MCP_TRANSPORT",
        "PORT",
        "UKRAINE_LAWS_API_KEY",
        "LOG_LEVEL",
        # Живий канал Ради й адреси реєстрів, які можна перевизначити.
        "RADA_LIVE_CHANNEL",
        "COURT_REGISTRY_URL",
        "ERB_API_URL",
        "PROZORRO_API_URL",
        "PROZORRO_SEARCH_URL",
        "DATA_GOV_UA_API_URL",
    }
)

ALLOWED_NAMES: frozenset[str] = _OS_ESSENTIALS | _PROXY | _PYTHON | _PRODUCT
ALLOWED_PREFIXES: tuple[str, ...] = ("YURKO_", "FASTMCP_")


#: Модуль ядра, наличие которого доказывает, что интерпретатор способен
#: поднять ``server.py``. Измерено 2026-09-09: системный ``python`` на PATH
#: даёт ``ModuleNotFoundError: No module named 'fastmcp'``, а `.venv-dev`
#: репозитория — импортирует. Проба — тот же приём, что уже используется в
#: ``_self_check`` ниже: спросить дочерний процесс, а не поверить обещанию.
CORE_PROBE_MODULE = "fastmcp"

#: Переменная-переопределение: владелец называет интерпретатор явно, и он
#: используется без пробы — переозначение доверяют, а не перепроверяют.
YURKO_PYTHON_ENV = "YURKO_PYTHON"

#: Каталоги окружений, в которых ищется рабочий интерпретатор, когда текущий
#: (``sys.executable``) пробы не проходит. Порядок значим: `.venv-dev` — venv
#: разработки этого репозитория (CLAUDE.md), `.venv` — прежнее умолчание.
VENV_DIR_NAMES: tuple[str, ...] = (".venv-dev", ".venv")


class PythonNotFound(RuntimeError):
    """Ни один кандидат в интерпретаторы не импортирует ядро."""


def _venv_python(repo_root: Path, venv_name: str) -> Path:
    """Путь к интерпретатору внутри venv-каталога — по правилам его платформы."""
    venv_dir = repo_root / venv_name
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _passes_core_probe(python_exe: str) -> bool:
    """Способен ли ``python_exe`` импортировать ядро (``fastmcp``).

    Спрашивается дочерний процесс, а не наличие пакета в текущем — сервер
    запускается отдельным процессом, и годится только то, что видит он.
    """
    try:
        result = subprocess.run(
            [python_exe, "-c", f"import {CORE_PROBE_MODULE}"],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _remediation_message(tried: list[str], repo_root: Path) -> str:
    """Одна строка на украинском с командой лечения — не диагноз без выхода."""
    venv_python = _venv_python(repo_root, VENV_DIR_NAMES[0])
    return (
        "Ядро Yurko не знайшло Python із fastmcp (перевірено: "
        + ", ".join(tried)
        + f"); встановіть залежності: {venv_python} -m pip install -r requirements.txt "
        f"(або задайте {YURKO_PYTHON_ENV} на робочий інтерпретатор)."
    )


def resolve_python(repo_root: Path | None = None) -> str:
    """Интерпретатор для запуска ``server.py``: своё дело — не молчаливый провал.

    Порядок: ``YURKO_PYTHON`` (явное переозначение владельца, без пробы) →
    текущий интерпретатор, если проходит пробу импорта → ``.venv-dev`` → ``.venv``
    внутри корня репозитория, первый прошедший пробу. Не прошёл ни один —
    :class:`PythonNotFound` с командой лечения в одну строку.
    """
    override = os.environ.get(YURKO_PYTHON_ENV, "").strip()
    if override:
        return override
    if _passes_core_probe(sys.executable):
        return sys.executable
    root = repo_root if repo_root is not None else Path(__file__).resolve().parent
    tried = [sys.executable]
    for venv_name in VENV_DIR_NAMES:
        candidate = _venv_python(root, venv_name)
        tried.append(str(candidate))
        if candidate.is_file() and _passes_core_probe(str(candidate)):
            return str(candidate)
    raise PythonNotFound(_remediation_message(tried, root))


def _allowed(name: str) -> bool:
    return name in ALLOWED_NAMES or name.startswith(ALLOWED_PREFIXES)


def sanitized_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Окружение процесса ядра: только то, что названо, и ничего сверх.

    Умолчание — не «пусто, если не задано»: ``PYTHONIOENCODING=utf-8``
    ставится всегда, потому что раскрытие ИИ на Windows приходило кодовой
    страницей и было принято моделью за инъекцию (T217), а
    ``FASTMCP_CHECK_FOR_UPDATES=off`` — потому что проверка обновлений это
    сетевой вызов, которого юрист не заказывал.
    """
    values = dict(os.environ if source is None else source)
    built = {name: value for name, value in values.items() if _allowed(name)}
    # Кодировка потока — **не** выбор пользователя, поэтому здесь присваивание, а
    # не `setdefault`. Наблюдено 2026-09-09: окружение, из которого запускался
    # набор, несло `PYTHONIOENCODING=utf-8:surrogateescape`, и белый список
    # честно пропускал его в ядро. Обработчик ошибок другой — а по этому потоку
    # идёт JSON-RPC, и подставленный суррогат ломает не текст, а протокол.
    built["PYTHONIOENCODING"] = "utf-8"
    built.setdefault("FASTMCP_CHECK_FOR_UPDATES", "off")
    return built


def server_command() -> list[str]:
    """Команда запуска ядра. Интерпретатор ищется сам (:func:`resolve_python`),
    а не берётся молча от того, кто запустил вход: тот же ``sys.executable``
    без ``YURKO_PYTHON`` на машине юриста — системный ``python``, у которого
    ядра нет (см. :data:`CORE_PROBE_MODULE`)."""
    return [resolve_python(), str(Path(__file__).resolve().parent / "server.py")]


def _self_check() -> int:
    """Показать, что видит в окружении процесс, запущенный этим входом.

    Запускается тот же самый способ, которым запускается ядро: отдельный
    процесс с построенным окружением. Печатает не то, что модуль собирался
    передать, а то, что дочерний процесс действительно получил, — иначе
    проверялся бы словарь, а не механизм.
    """
    probe = "import json, os, sys; sys.stdout.write(json.dumps(dict(os.environ)))"
    result = subprocess.run(
        [sys.executable, "-c", probe],
        env=sanitized_environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        return result.returncode
    sys.stdout.write(result.stdout)
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "--self-check":
        return _self_check()

    try:
        command = server_command() + arguments
    except PythonNotFound as error:
        # Одна строка на stderr и ненулевой код — не молчаливый
        # ``ModuleNotFoundError`` из процесса ядра тремя экранами ниже.
        sys.stderr.write(str(error) + "\n")
        return 1

    process = subprocess.Popen(command, env=sanitized_environment())
    try:
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        return process.wait()
    finally:
        if process.poll() is None:
            # Клиент закрыл stdin и ушёл: ядро без входа не нужно, а
            # осиротевший процесс на машине юриста — мусор, который он не
            # заказывал.
            process.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
