"""Сервер пише кеш у каталог кешу ОС, а не в теку коду (тікет 51, знахідка 22; тікет 50).

Плагін ставиться в теку, яку клієнт вважає своєю (а пакет, покладений поруч зі
справою, — у теку справи). Кеш продукту там — це і сміття серед матеріалів справи,
і зміна встановленого пакета. Єдине, що сервер читає з теки коду, — ``cache/laws.json``:
дані поставки, а не кеш.

Перевіряється справжній старт: окремий процес імпортує ``server`` (імпорт будує всі
реєстри, а вони створюють свої каталоги) із заданим ``YURKO_CACHE_DIR``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import source_cache  # noqa: E402

_PROBE = """
import json
import server

print(json.dumps({
    "laws_index": str(server.LAWS_INDEX_PATH),
    "local_texts": str(server.LOCAL_TEXTS_DIR),
    "open_data": str(server.open_data_discovery._cache_dir),
    "court_decisions": str(server.court_registry._cache_dir),
    "rada": str(server.rada_client._cache_dir),
}))
"""


def _tree(base: Path) -> dict[str, int]:
    """Усе під ``base`` як {відносний шлях: розмір}; каталоги — розмір -1."""
    if not base.exists():
        return {}
    return {
        path.relative_to(base).as_posix(): -1 if path.is_dir() else path.stat().st_size
        for path in sorted(base.rglob("*"))
    }


def test_importing_the_server_writes_nothing_into_the_code_tree(tmp_path: Path) -> None:
    code_cache = ROOT / "cache"
    before = _tree(code_cache)
    cache = tmp_path / "yurko-cache"

    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=ROOT,
        env={**os.environ, "YURKO_CACHE_DIR": str(cache), "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    where = json.loads(result.stdout.strip().splitlines()[-1])
    assert _tree(code_cache) == before, "імпорт server змінив cache/ у теці коду"

    # Реєстр законів — дані поставки: читається з теки коду, поза кешем.
    assert Path(where["laws_index"]) == code_cache / "laws.json"
    for name in ("local_texts", "open_data", "court_decisions", "rada"):
        location = Path(where[name])
        assert cache in location.parents, f"{name}: {location} лежить не в YURKO_CACHE_DIR"
        assert location.is_dir()
        assert ROOT not in location.parents


def test_hot_cache_of_laws_is_not_the_content_addressed_text_cache() -> None:
    """``ua_laws`` (``<law_id>.json``) і ``texts`` (хеші та ``index.json``) — різні каталоги.

    Якби гарячий кеш законів був у ``texts``, лічильник кешованих законів рахував би
    ``index.json`` як закон, а очищення одного зачіпало б інший.
    """
    assert source_cache.LAWS_CACHE_SUBDIR != source_cache.TEXT_CACHE_SUBDIR


def test_registry_cache_dir_is_under_the_cache_dir_and_created() -> None:
    path = source_cache.registry_cache_dir(source_cache.OPEN_DATA_CACHE_SUBDIR)

    assert path.is_dir()
    assert path.parent == source_cache.cache_dir()


@pytest.mark.parametrize("name", ["", "  ", ".", "..", "../etc", "a/b", "a\\b"])
def test_registry_cache_dir_takes_one_path_segment(name: str) -> None:
    with pytest.raises(ValueError):
        source_cache.registry_cache_dir(name)
