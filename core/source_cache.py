"""Каталог дискового кеша источников: где лежит то, что дорого построить (T247).

Почему это отдельный модуль, а не путь внутри адаптера. Каталог вывода задачи
(``YURKO_OUTPUT_DIR``, :mod:`task_workflow`) — это папка дела юриста: в неё
пишется то, что юрист потом отдаёт суду и клиенту, и туда же смотрит сторож
записи (``plugin/yurko/hooks/guard.py``). Индекс открытого набора судебных
решений Латвии — 181 МиБ служебных данных, которые к делу отношения не имеют;
положить их в папку дела значит замусорить материалы дела и заставить юриста
копировать эти мегабайты вместе с делом. Поэтому кеш живёт в каталоге кеша
пользователя ОС, а не рядом с выводом, и место называется одной переменной
``YURKO_CACHE_DIR`` — она проходит в процесс ядра по префиксу ``YURKO_``
(``server_env.py``), дописывать её в белый список не нужно.

Ради чего здесь :func:`atomic_replace`, а не обычная запись на место. Построение
индекса занимает десятки секунд и может оборваться посередине — падением
процесса, обрывом сети, закрытием клиента. Запись сразу в рабочий файл в этом
случае оставила бы усечённую базу, которая открывается и отвечает — то есть
молча выдавала бы часть набора за набор (принцип III). Сборка идёт во временный
файл в том же каталоге, и только целиком построенный результат встаёт на место
одним ``os.replace``: либо старый кеш, либо новый, третьего состояния на диске
не бывает (принцип IX).

Только stdlib: модуль обязан импортироваться в любом окружении, включая то, где
ещё ничего не установлено, — иначе он не сможет быть местом, куда падают
диагностические артефакты.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "CACHE_DIR_ENV",
    "COURT_DECISIONS_CACHE_SUBDIR",
    "LAWS_CACHE_SUBDIR",
    "OPEN_DATA_CACHE_SUBDIR",
    "RADA_CACHE_SUBDIR",
    "TEXT_CACHE_SUBDIR",
    "TEXT_INDEX_NAME",
    "atomic_replace",
    "cache_dir",
    "cache_path",
    "compare_with_cached",
    "load_text_index",
    "lookup_source",
    "public_text_cache_path",
    "registry_cache_dir",
    "remember_source",
    "resolve_cache_dir",
    "source_key",
    "store_public_text",
    "text_index_path",
]

#: Имя переменной окружения, которой владелец машины называет каталог кеша.
CACHE_DIR_ENV = "YURKO_CACHE_DIR"

#: Имя продукта внутри каталога кеша ОС. Отдельная папка, а не файлы вперемешку
#: с чужими: удалить кеш продукта целиком должно быть одним действием.
_PRODUCT_DIR = "yurko"


def resolve_cache_dir(environ: Mapping[str, str], *, windows: bool) -> Path:
    """Куда положить кеш при данном окружении и данной ОС — чистая функция.

    Вынесена отдельно и принимает окружение и признак ОС аргументами, чтобы
    обе ветки (Windows и POSIX) проверялись тестом на любой машине. Ветка,
    проверяемая только на той ОС, где запускают тесты, — это ветка, о которой
    известно, что она компилируется, и ничего больше.

    Каталог не создаётся: решение «где» и действие «создать» разделены, потому
    что первое нужно и диагностике, которая ничего создавать не должна.
    """
    named = str(environ.get(CACHE_DIR_ENV) or "").strip()
    if named:
        return Path(named).expanduser()
    if windows:
        # %LOCALAPPDATA% — штатное место машинно-локальных данных Windows:
        # оно не уезжает в перемещаемый профиль, а 181 МиБ индекса в
        # перемещаемом профиле означали бы 181 МиБ по сети при каждом входе.
        local = str(environ.get("LOCALAPPDATA") or "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / _PRODUCT_DIR / "cache"
    xdg = str(environ.get("XDG_CACHE_HOME") or "").strip()
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / _PRODUCT_DIR


def cache_dir() -> Path:
    """Каталог кеша, созданный и готовый к записи.

    Окружение читается на каждом вызове, а не один раз при импорте: процесс
    ядра запускается обёрткой (``server_env.py``), и переменная, поставленная
    ею, обязана действовать без оглядки на то, в каком порядке случились
    импорты.
    """
    path = resolve_cache_dir(os.environ, windows=os.name == "nt")
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_path(name: str) -> Path:
    """Путь к файлу кеша по имени.

    Имя обязано быть одним сегментом: разделитель пути или ``..`` в нём вывели
    бы запись за пределы каталога кеша — например, в папку дела, — а это ровно
    то, ради чего модуль и появился. Проверка здесь, а не в вызывающем коде,
    потому что вызывающих будет много, а забыть достаточно одному.
    """
    cleaned = str(name or "").strip()
    if not cleaned or cleaned in (".", ".."):
        raise ValueError("имя файла кеша не может быть пустым или ссылкой на каталог")
    if "/" in cleaned or "\\" in cleaned or os.sep in cleaned:
        raise ValueError(f"имя файла кеша обязано быть одним сегментом пути: {cleaned!r}")
    return cache_dir() / cleaned


def atomic_replace(tmp: Path, final: Path) -> None:
    """Поставить построенный файл на место одним неделимым действием.

    ``os.replace`` атомарен только в пределах одной файловой системы; между
    томами он вырождается в копирование и перестаёт быть тем, ради чего его
    здесь зовут. Поэтому расхождение каталогов — ошибка вызывающего, а не
    случай, который стоит молча пережить: собирать надо через
    ``cache_path(name + ".building")``, рядом с целью.

    Замечание для Windows: ``os.replace`` на файл, который кто-то держит
    открытым, поднимает ``PermissionError``. Отсюда правило для читателей
    кеша — открывать соединение на запрос и закрывать его, не держа
    долгоживущий дескриптор.
    """
    if tmp.parent.resolve() != final.parent.resolve():
        raise ValueError(
            "временный файл обязан лежать в том же каталоге, что и целевой: "
            f"{tmp.parent} != {final.parent} — os.replace между томами не атомарен"
        )
    os.replace(tmp, final)


# ---------------------------------------------------------------------------
# Кеш реєстрів: те, що сервер пише під час роботи (тікет 51, знахідка 22)
#
# Раніше ці каталоги лежали в ``<тека коду>/cache/`` — у плагіні це тека
# встановленого пакета, а в пакеті, покладеному поруч зі справою, — тека самої
# справи: кеш продукту змішувався з матеріалами. Тепер вони живуть у
# :func:`cache_dir` (каталог ОС, ``YURKO_CACHE_DIR`` перекриває), а тека коду
# лишається тільки для читання. Єдиний файл, який сервер читає з теки коду, —
# ``cache/laws.json``: це дані поставки (реєстр законів), а не кеш.
# ---------------------------------------------------------------------------

#: Гарячий кеш текстів законів Ради: ``<law_id>.json`` (не плутати з
#: ``TEXT_CACHE_SUBDIR`` — там хеш-адресний кеш і ``index.json``).
LAWS_CACHE_SUBDIR = "ua_laws"

#: Каталог відкритих даних Ради (``doc.zip``, ≈13 МБ).
OPEN_DATA_CACHE_SUBDIR = "ua_open_data"

#: Тексти судових рішень з реєстру (незмінні після публікації).
COURT_DECISIONS_CACHE_SUBDIR = "ua_court_decisions"

#: Клієнт відкритих даних Ради: кеш пошуку й дамп текстів. Один каталог на
#: сервер — кеш спільний, а не другий незалежний.
RADA_CACHE_SUBDIR = "rada"


def registry_cache_dir(name: str) -> Path:
    """Підкаталог кешу реєстру, створений і готовий до запису.

    Ім'я — один сегмент шляху з констант цього модуля: так запис, що йшов би
    в ``..``, не виходить за каталог кешу (та сама вимога, що в
    :func:`cache_path`).
    """
    cleaned = str(name or "").strip()
    if not cleaned or cleaned in (".", "..") or "/" in cleaned or "\\" in cleaned:
        raise ValueError(f"ім'я підкаталогу кешу має бути одним сегментом шляху: {cleaned!r}")
    path = cache_dir() / cleaned
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Кеш публічних текстів джерел (T297, D-14, FR-530)
#
# Прочитані **публічні** тексти зберігаються за ``content_hash`` і переживають
# процес. Межа, яку кеш не перетинає: доказом лишається текст, звірений у
# цьому сеансі. Кеш дає не «доказ із минулого разу», а дешеве перечитування —
# хеш звіряється, збіг робить звірку миттєвою, розбіжність означає, що
# джерело змінилося, і це окремий видимий факт.
#
# Матеріалів справи тут немає і бути не може: кешується тільки те, що прийшло
# з публічного джерела через ``routing.perform_read`` (принцип I).
# ---------------------------------------------------------------------------

#: Підкаталог кешу для публічних текстів джерел.
TEXT_CACHE_SUBDIR = "texts"

#: Індекс кешу: за яким адресом лежить який текст. Без нього кеш адресується
#: тільки вмістом, а вміст відомий лише після читання — тобто відповісти
#: «чи це той самий текст, що минулого разу» було б нічим.
TEXT_INDEX_NAME = "index.json"


def public_text_cache_path(content_hash: str) -> Path:
    """Шлях до кешованого тексту за його ``content_hash``.

    Ім'я файла — сам хеш; підкаталог ``texts`` ізолює тексти від інших
    об'єктів кешу (індексів, лічильників).
    """
    cleaned = str(content_hash or "").strip()
    if not cleaned:
        raise ValueError("content_hash не може бути порожнім")
    if "/" in cleaned or "\\" in cleaned or os.sep in cleaned:
        raise ValueError(f"content_hash не повинен містити роздільник шляху: {cleaned!r}")
    d = cache_dir() / TEXT_CACHE_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d / cleaned


def store_public_text(content_hash: str, text: str) -> Path:
    """Зберегти публічний текст джерела за ``content_hash``.

    Запис атомарний: будується поруч із кінцевим файлом і переміщується
    одним ``os.replace``. Повертає шлях до збереженого файла.
    """
    target = public_text_cache_path(content_hash)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".building")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        atomic_replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return target


def source_key(
    *,
    legal_order: str = "",
    document_id: str = "",
    path: str = "",
    version_id: str = "",
    language: str = "",
) -> str:
    """Адреса джерела в індексі кешу — редакція і мовна версія входять у неї.

    Редакція входить навмисне: без неї кеш відповідав би «той самий текст»
    на питання про іншу редакцію того самого акта, а різниця редакцій — це
    саме те, заради чого юрист перечитує норму.
    """
    parts = [
        str(legal_order or "").strip().upper(),
        str(document_id or "").strip(),
        str(path or "").strip(),
        str(version_id or "").strip(),
        str(language or "").strip().lower(),
    ]
    return "|".join(parts)


def text_index_path() -> Path:
    """Шлях до індексу кешу текстів."""
    d = cache_dir() / TEXT_CACHE_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d / TEXT_INDEX_NAME


def load_text_index() -> dict[str, dict[str, object]]:
    """Індекс кешу; порожній словник, якщо його немає або він зіпсований.

    Зіпсований індекс не є помилкою роботи: кеш — прискорювач, і його втрата
    означає повне перечитування, а не відмову.
    """
    path = text_index_path()
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {str(k): v for k, v in loaded.items() if isinstance(v, dict)}


def remember_source(
    key: str,
    content_hash: str,
    text: str,
    *,
    session_id: str = "",
    source_url: str = "",
) -> bool:
    """Покласти текст у кеш і записати його адресу в індекс.

    Повертає ``False`` при будь-якій відмові файлової системи: кеш ніколи не
    ламає читання, заради якого його ведуть. ``session_id`` записується, щоб
    наступний сеанс міг оголосити обсяг перечитування **до** початку
    (FR-530), а не після.
    """
    cleaned_key = str(key or "").strip()
    cleaned_hash = str(content_hash or "").strip()
    if not cleaned_key or not cleaned_hash or not text:
        return False
    try:
        store_public_text(cleaned_hash, text)
        index = load_text_index()
        index[cleaned_key] = {
            "content_hash": cleaned_hash,
            "session_id": str(session_id or ""),
            "source_url": str(source_url or ""),
            "chars": len(text),
            "stored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        path = text_index_path()
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        atomic_replace(tmp, path)
        return True
    except (OSError, ValueError):
        return False


def lookup_source(key: str) -> dict[str, object] | None:
    """Що кеш знає про цю адресу; ``None`` — не знає нічого."""
    entry = load_text_index().get(str(key or "").strip())
    if not isinstance(entry, dict):
        return None
    return entry


def compare_with_cached(key: str, text: str) -> dict[str, object]:
    """Чи той самий текст лежав за цією адресою минулого разу (D-14).

    Три відповіді, і всі три названі: ``unknown`` — адреси в кеші немає,
    ``same`` — хеш збігся, ``changed`` — джерело змінилося. Останнє не
    гаситься і не «оновлюється мовчки»: розбіжність редакції — окремий факт,
    який юрист має побачити.
    """
    entry = lookup_source(key)
    digest = hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()
    if entry is None:
        return {"state": "unknown", "content_hash": digest, "previous_hash": ""}
    previous = str(entry.get("content_hash") or "")
    return {
        "state": "same" if previous == digest else "changed",
        "content_hash": digest,
        "previous_hash": previous,
    }
