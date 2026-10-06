"""Залежності: `pyproject.toml`, `uv.lock` і `requirements*.txt` не розходяться (тікет 51, № 6 і 14).

Холодний аудит 29.09.2026: плагін запускався як
``uv run --no-project --with-requirements requirements.txt``, де було
``fastmcp>=0.1.0`` — версії розв'язувалися в день першого запуску (аудитор отримав
fastmcp 4.0.10 і mcp 2.2.0), а ``uv.lock`` не використовувався й давно відстав
(тягнув LangGraph, якого в коді вже немає). До того ж ``requirements.txt`` ставив
Playwright, Postgres і Alembic, які режиму stdio не потрібні, з іншими межами
версій, ніж у ``pyproject.toml``.

Тепер єдине джерело версій — ``uv.lock``; ``requirements.txt`` (stdio, плагін) і
``requirements-hosted.txt`` (Docker: плюс Postgres і Playwright) — його експорт із
хешами. Тут перевіряється, що вони справді збігаються.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Що режиму stdio не потрібно й що тому не має потрапляти в базове встановлення.
EXTRA_ONLY = ("playwright", "psycopg", "pgvector", "alembic", "sqlalchemy")


def _name(requirement: str) -> str:
    match = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    assert match, requirement
    return re.sub(r"[-_.]+", "-", match.group(1)).lower()


def _pinned(path: Path) -> dict[str, str]:
    """{ім'я: версія} із файла, де кожен рядок — ``name==version`` (uv export)."""
    pins: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line[0] in " #-":
            continue
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?==([^\s;\\]+)", line)
        assert match, f"{path.name}: рядок не закріплено через == : {line!r}"
        pins[_name(match.group(1))] = match.group(3)
    return pins


def _pyproject() -> dict[str, object]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_requirements_pin_every_package_and_carry_hashes() -> None:
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    pins = _pinned(ROOT / "requirements.txt")

    assert pins, "requirements.txt порожній"
    assert text.count("--hash=sha256:") >= len(pins), "не кожен пакет має хеш"


def test_requirements_cover_the_declared_dependencies_and_nothing_extra() -> None:
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    declared = {_name(requirement) for requirement in project["dependencies"]}
    pins = set(_pinned(ROOT / "requirements.txt"))

    assert declared <= pins, f"у requirements.txt немає: {sorted(declared - pins)}"
    heavy = sorted(set(EXTRA_ONLY) & pins)
    assert not heavy, f"режиму stdio не потрібні, а requirements.txt їх ставить: {heavy}"


def test_hosted_requirements_add_postgres_and_browser_on_top_of_the_base() -> None:
    base = _pinned(ROOT / "requirements.txt")
    hosted = _pinned(ROOT / "requirements-hosted.txt")

    assert set(base) <= set(hosted)
    assert set(EXTRA_ONLY) <= set(hosted)
    assert {name: hosted[name] for name in base} == base, "версії базового набору розійшлися"


def test_the_lock_is_free_of_the_removed_workflow_engine() -> None:
    """LangGraph і `langsmith` вирізано разом із шарами процесу; лок не має їх тягнути."""
    locked = (ROOT / "uv.lock").read_text(encoding="utf-8")

    for gone in ("langgraph", "langsmith", "langchain-core"):
        assert f'name = "{gone}"' not in locked, f"{gone} лишився в uv.lock"


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv не встановлено")
def test_requirements_are_exactly_the_export_of_the_lock() -> None:
    """Розбіжність із локом — це розбіжність між тим, що перевірено, і тим, що ставиться."""

    def export(*flags: str) -> list[str]:
        result = subprocess.run(
            ["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--no-header", *flags],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.splitlines()

    def committed(name: str) -> list[str]:
        lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
        return [line for line in lines if not line.startswith("#")]

    hint = "перегенеруйте: див. заголовок requirements*.txt"
    assert export() == committed(
        "requirements.txt"
    ), f"requirements.txt відстав від uv.lock; {hint}"
    assert export("--extra", "postgres", "--extra", "browser") == committed(
        "requirements-hosted.txt"
    ), f"requirements-hosted.txt відстав від uv.lock; {hint}"


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv не встановлено")
def test_the_lock_is_current_with_pyproject() -> None:
    result = subprocess.run(
        ["uv", "lock", "--check", "--offline"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
