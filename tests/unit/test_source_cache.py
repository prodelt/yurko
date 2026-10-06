"""T247 — каталог дискового кеша источников: где он и почему не в папке дела.

Смысл модуля один: 181 МиБ служебного индекса не должны оказаться в каталоге
вывода задачи (``YURKO_OUTPUT_DIR``), то есть в папке дела юриста. Поэтому
здесь проверяется не «функция что-то возвращает», а три границы: переменная
``YURKO_CACHE_DIR`` решает всё; умолчание берётся у ОС, а не у каталога вывода;
имя файла не может увести запись за пределы каталога кеша.

Обе ветки умолчания (Windows и POSIX) проверяются на любой машине — за это
отвечает чистая :func:`source_cache.resolve_cache_dir`, принимающая окружение и
признак ОС аргументами. Ветка, проверяемая только на той ОС, где запускают
тесты, — это ветка, о которой известно лишь то, что она компилируется.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core import source_cache  # noqa: E402

# ---------------------------------------------------------------------------
# Где лежит кеш
# ---------------------------------------------------------------------------


def test_named_directory_wins_on_both_platforms() -> None:
    """``YURKO_CACHE_DIR`` — последнее слово владельца машины, на любой ОС."""
    environ = {
        source_cache.CACHE_DIR_ENV: r"D:\yurko-cache",
        "LOCALAPPDATA": r"C:\Profiles\u\AppData\Local",
        "XDG_CACHE_HOME": "/var/cache",
    }

    assert source_cache.resolve_cache_dir(environ, windows=True) == Path(r"D:\yurko-cache")
    assert source_cache.resolve_cache_dir(environ, windows=False) == Path(r"D:\yurko-cache")


def test_blank_variable_is_not_a_directory_name() -> None:
    """Пустая переменная — это «не задано», а не каталог с пустым именем."""
    environ = {source_cache.CACHE_DIR_ENV: "   ", "XDG_CACHE_HOME": "/var/cache"}

    assert source_cache.resolve_cache_dir(environ, windows=False) == Path("/var/cache/yurko")


def test_windows_default_is_local_app_data() -> None:
    environ = {"LOCALAPPDATA": r"C:\Profiles\u\AppData\Local"}

    resolved = source_cache.resolve_cache_dir(environ, windows=True)

    assert resolved == Path(r"C:\Profiles\u\AppData\Local") / "yurko" / "cache"


def test_windows_without_local_app_data_falls_back_under_home() -> None:
    """Без ``%LOCALAPPDATA%`` кеш всё равно машинно-локальный, а не в профиле дела."""
    resolved = source_cache.resolve_cache_dir({}, windows=True)

    assert resolved == Path.home() / "AppData" / "Local" / "yurko" / "cache"


def test_posix_default_respects_xdg() -> None:
    environ = {"XDG_CACHE_HOME": "/var/cache"}

    assert source_cache.resolve_cache_dir(environ, windows=False) == Path("/var/cache/yurko")


def test_posix_without_xdg_uses_dot_cache() -> None:
    assert source_cache.resolve_cache_dir({}, windows=False) == Path.home() / ".cache" / "yurko"


def test_output_directory_never_becomes_the_cache_directory() -> None:
    """Ради чего модуль и появился: каталог дела кешем не становится.

    ``YURKO_OUTPUT_DIR`` — папка, куда пишется работа юриста и куда смотрит
    сторож записи плагина. Индекс открытого набора судебных решений там не
    нужен ни юристу, ни суду, а весит он больше самого дела.
    """
    environ = {
        "YURKO_OUTPUT_DIR": r"D:\dela\case-17",
        "LOCALAPPDATA": r"C:\Profiles\u\AppData\Local",
    }

    resolved = source_cache.resolve_cache_dir(environ, windows=True)

    assert Path(r"D:\dela\case-17") not in resolved.parents
    assert resolved != Path(r"D:\dela\case-17")


# ---------------------------------------------------------------------------
# Создание каталога и имена файлов
# ---------------------------------------------------------------------------


def test_cache_dir_creates_the_directory_and_reads_environment_at_call_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Переменная читается на вызове: обёртка запуска ставит её до старта ядра."""
    target = tmp_path / "nested" / "cache"
    monkeypatch.setenv(source_cache.CACHE_DIR_ENV, str(target))

    assert not target.exists()
    assert source_cache.cache_dir() == target
    assert target.is_dir()

    other = tmp_path / "second"
    monkeypatch.setenv(source_cache.CACHE_DIR_ENV, str(other))
    assert source_cache.cache_dir() == other


def test_cache_path_is_inside_the_cache_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(source_cache.CACHE_DIR_ENV, str(tmp_path))

    assert source_cache.cache_path("lv_dagr.sqlite3") == tmp_path / "lv_dagr.sqlite3"


@pytest.mark.parametrize("name", ["", "  ", ".", "..", "../out", "a/b", r"a\b"])
def test_cache_path_refuses_names_that_leave_the_cache_directory(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Имя с разделителем увело бы запись наружу — например, в папку дела."""
    monkeypatch.setenv(source_cache.CACHE_DIR_ENV, str(tmp_path))

    with pytest.raises(ValueError):
        source_cache.cache_path(name)


# ---------------------------------------------------------------------------
# Атомарная замена
# ---------------------------------------------------------------------------


def test_atomic_replace_puts_the_built_file_in_place(tmp_path: Path) -> None:
    final = tmp_path / "index.sqlite3"
    final.write_bytes(b"old")
    tmp = tmp_path / "index.sqlite3.building"
    tmp.write_bytes(b"new")

    source_cache.atomic_replace(tmp, final)

    assert final.read_bytes() == b"new"
    assert not tmp.exists()


def test_atomic_replace_refuses_a_different_directory(tmp_path: Path) -> None:
    """Между томами ``os.replace`` не атомарен — значит, это не замена, а копия."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    tmp = elsewhere / "index.sqlite3.building"
    tmp.write_bytes(b"new")
    final = tmp_path / "index.sqlite3"

    with pytest.raises(ValueError, match="том"):
        source_cache.atomic_replace(tmp, final)

    assert not final.exists()
