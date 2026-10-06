"""Раскладка пакета: колесо содержит то, что импортирует сервер (T241, FR-423).

Дефект, ради которого этот файл существует, жил незамеченным: в
``[tool.setuptools] py-modules`` не было семи модулей верхнего уровня, которые
``server.py`` импортирует прямо или через свои импорты. Он был латентным, потому
что поставка юриста запускает ``server.py`` из репозитория
(``plugin/yurko/.mcp.json`` → ``${CLAUDE_PLUGIN_ROOT}/../../server.py``), а не
ставит колесо. Первый же ``pip install .`` дал бы пакет, который падает на
импорте — и падал бы не там, где ошибка.

Проверка считает замыкание импортов от ``server.py`` разбором AST, а не
запуском: список модулей не должен зависеть от того, какие ветки кода
выполнились.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _local_modules() -> set[str]:
    return {path.stem for path in ROOT.glob("*.py")}


def _local_packages() -> set[str]:
    """Каталоги верхнего уровня с ``__init__.py`` — пакеты, а не модули.

    Колесо берёт их отдельным списком (``packages``), и забытый там пакет
    ставится ровно так же тихо, как забытый модуль в ``py-modules``. Дефект
    измерен на ``document_pipeline`` в 005: пакет появился, ``py-modules`` его
    не берёт по определению, а старая проверка искала только ``*.py`` в корне.
    """
    return {
        path.name
        for path in ROOT.iterdir()
        if path.is_dir() and (path / "__init__.py").exists() and not path.name.startswith(".")
    }


def _imports_of(path: Path, local: set[str]) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found & local


def _closure_from_server(local: set[str]) -> set[str]:
    seen: set[str] = set()
    queue = ["server"]
    while queue:
        module = queue.pop()
        if module in seen:
            continue
        seen.add(module)
        queue.extend(_imports_of(ROOT / f"{module}.py", local))
    for source in (ROOT / "sources").glob("*.py"):
        seen.update(_imports_of(source, local))
    return seen


def test_wheel_declares_every_module_the_server_imports() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = set(config["tool"]["setuptools"]["py-modules"])
    local = _local_modules()

    missing = sorted(_closure_from_server(local) - declared)
    assert not missing, (
        "модули, которые сервер импортирует, но которых нет в py-modules: "
        f"{', '.join(missing)} — установленный пакет упал бы на импорте"
    )


def test_wheel_declares_every_local_package_the_server_imports() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = set(config["tool"]["setuptools"]["packages"])
    packages = _local_packages()

    imported: set[str] = set()
    for path in [*ROOT.glob("*.py"), *(p for pkg in packages for p in (ROOT / pkg).glob("*.py"))]:
        imported |= _imports_of(path, packages)

    missing = sorted(imported - declared)
    assert not missing, (
        "пакеты, которые импортирует код продукта, но которых нет в packages: "
        f"{', '.join(missing)} — установленный пакет упал бы на импорте"
    )
