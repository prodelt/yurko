"""Белый список окружения процесса ядра (T240, FR-419).

Проверяется механизм, а не намерение: тест запускает вход ровно так, как его
запускает поставка юриста, и спрашивает **дочерний процесс**, что он получил.
Проверка словаря доказывала бы, что модуль собирался передать, — а вопрос в
том, что процесс действительно увидел.

Почему это вообще проверяется. Движок воркфлоу — LangGraph, и с ним в процесс
приходит ``langsmith`` (обязательная зависимость ``langchain-core``, который
импортирует его на уровне модуля). Измерено 2026-09-08: с
``LANGSMITH_TRACING=true`` тривиальный граф попытался отправить содержимое
своих узлов на ``api.smith.langchain.com``. Блок ``env`` в ``.mcp.json``
переменные **добавляет**, а не заменяет, поэтому заданная снаружи доехала бы
до ядра.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server_env  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _child_environment(extra: dict[str, str]) -> dict[str, str]:
    """Что получил процесс, запущенный входом из окружения ``os.environ + extra``."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "server_env.py"), "--self-check"],
        env={**os.environ, **extra},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    parsed: dict[str, str] = json.loads(result.stdout)
    return parsed


#: Чужі ключі й телеметрія: белый список не пропускает их в процесс ядра.
DENIED_EXAMPLES: tuple[str, ...] = (
    "LANGSMITH_TRACING",
    "LANGSMITH_API_KEY",
    "GOOGLE_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
)


@pytest.mark.parametrize("name", DENIED_EXAMPLES)
def test_a_variable_set_from_outside_does_not_reach_the_server_process(name: str) -> None:
    """Заданная снаружи переменная до процесса ядра не доходит."""
    child = _child_environment({name: "true"})

    assert name not in child, (
        f"{name} доехала до процесса ядра: белый список не сработал, и содержимое "
        "узлов воркфлоу может уйти третьей стороне"
    )


def test_the_server_process_still_gets_what_it_needs_to_run() -> None:
    """Белый список не должен ломать запуск: интерпретатор и TLS на месте."""
    child = _child_environment({"LANGSMITH_TRACING": "true"})

    assert child.get("PATH"), "без PATH дочерний процесс не найдёт ни интерпретатора, ни DLL"
    # T217: раскрытие ИИ на Windows приходило кодовой страницей и было принято
    # моделью за инъекцию. Кодировка задаётся всегда, а не когда повезёт.
    assert child.get("PYTHONIOENCODING") == "utf-8"
    # Проверка обновлений — сетевой вызов, которого юрист не заказывал.
    assert child.get("FASTMCP_CHECK_FOR_UPDATES") == "off"


def test_the_products_own_variables_pass_by_prefix() -> None:
    """``YURKO_*`` — окружение владельца продукта и проходит целиком."""
    child = _child_environment(
        {"YURKO_OUTPUT_DIR": str(ROOT), "YURKO_PROFILE": "legal", "LANGSMITH_TRACING": "true"}
    )

    assert child.get("YURKO_OUTPUT_DIR") == str(ROOT)
    assert child.get("YURKO_PROFILE") == "legal"
    assert "LANGSMITH_TRACING" not in child


#: Змінні з README (розділи «Запуск» і «Налаштування»), що не починаються з префіксів
#: ``YURKO_``/``FASTMCP_``: без запису в білому списку вони мовчки не діяли б
#: (аудит 22.1, № 8).
DOCUMENTED_SETTINGS: tuple[str, ...] = (
    "USE_PG_BACKEND",
    "DATABASE_URL",
    "MCP_TRANSPORT",
    "PORT",
    "UKRAINE_LAWS_API_KEY",
    "RADA_LIVE_CHANNEL",
    "COURT_REGISTRY_URL",
    "ERB_API_URL",
    "PROZORRO_API_URL",
    "PROZORRO_SEARCH_URL",
    "DATA_GOV_UA_API_URL",
)


@pytest.mark.parametrize("name", DOCUMENTED_SETTINGS)
def test_a_documented_setting_reaches_the_server_process(name: str) -> None:
    """Задокументована змінна доходить до процесу сервера — перевіряє дитина, а не словник."""
    child = _child_environment({name: "documented-value"})

    assert child.get(name) == "documented-value", (
        f"{name} названа в README, але до процесу сервера не доходить: "
        "задокументований перемикач мовчки не діє"
    )


def _readme_section(readme: str, heading: str) -> str:
    match = re.search(rf"^## {heading}\n(.*?)(?=^## |\Z)", readme, re.DOTALL | re.MULTILINE)
    assert match, f"у README немає розділу «{heading}»"
    return match.group(1)


def _variable_names(cell: str) -> set[str]:
    """Імена змінних оточення в кодових відрізках рядка: `NAME` або `NAME=value`."""
    names: set[str] = set()
    for code in re.findall(r"`([^`\n]+)`", cell):
        name = code.split("=")[0].strip()
        if re.fullmatch(r"[A-Z][A-Z0-9_]+", name):
            names.add(name)
    return names


def _readme_variables() -> set[str]:
    """Змінні, названі в README: перша колонка таблиці «Settings» і рядок про HTTP mode."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    names: set[str] = set()
    for line in _readme_section(readme, "Settings").splitlines():
        if line.startswith("|") and line.count("|") >= 3:
            names |= _variable_names(line.split("|")[1])
    for line in _readme_section(readme, "Run it locally").splitlines():
        if line.startswith("HTTP mode"):
            names |= _variable_names(line)
    return names


def test_every_variable_named_in_the_readme_settings_passes_the_whitelist() -> None:
    """Розбіжність README і білого списку ловиться тут, а не в юриста, що вимкнув канал."""
    names = _readme_variables()
    assert {"RADA_LIVE_CHANNEL", "COURT_REGISTRY_URL", "YURKO_PROFILE", "PORT"} <= names, names

    built = server_env.sanitized_environment({name: "x" for name in names})

    missing = sorted(names - set(built))
    assert not missing, f"README називає, а білий список не пропускає: {missing}"


@pytest.mark.parametrize("name", ["PYTHONPATH", "PYTHONHOME"])
def test_python_path_variables_do_not_reach_the_server_process(name: str) -> None:
    """Через них можна підмінити код сервера, тож у процес вони не потрапляють (№ 20).

    Перевіряється побудоване оточення, а не дочірній процес: ``PYTHONHOME`` із
    хибним значенням не дав би запуститися й самому входові.
    """
    built = server_env.sanitized_environment({name: "/somewhere/else", "PATH": "/usr/bin"})

    assert name not in built
    assert built["PATH"] == "/usr/bin"


def test_the_whitelist_is_a_whitelist_and_not_a_list_of_bans() -> None:
    """Неизвестное имя не проходит: запрещено всё, чего нет в списке."""
    built = server_env.sanitized_environment(
        {"YURKO_OUTPUT_DIR": "/tmp/case", "SOME_FUTURE_TRACER": "on", "PATH": "/usr/bin"}
    )

    assert "SOME_FUTURE_TRACER" not in built
    assert built["YURKO_OUTPUT_DIR"] == "/tmp/case"
    assert built["PATH"] == "/usr/bin"


def test_the_legal_profile_refuses_to_start_if_a_tracing_variable_got_through() -> None:
    """Второй, независимый механизм: профиль ``legal`` с трассировкой не стартует.

    Белый список — граница окружения; если её обошли (запустили ``server.py``
    напрямую), профиль обязан отказать, а не работать молча.
    """
    from core import yurko_profile

    violations = yurko_profile.validate_environment({"LANGSMITH_TRACING": "true"})

    assert violations, "переменная трассировки в окружении ядра — нарушение принципа VIII"
    assert any("api.smith.langchain.com" in violation for violation in violations)


def test_an_empty_tracing_variable_is_not_a_violation() -> None:
    """Пустое значение не включает отправку и не должно ронять запуск."""
    from core import yurko_profile

    assert yurko_profile.validate_environment({"LANGCHAIN_ENDPOINT": ""}) == []


# --- resolve_python: ядро ищет свой интерпретатор само ----------------------
#
# Измерено 2026-09-09: системный ``python`` на PATH юриста не импортирует
# fastmcp, а `.mcp.json` запускал вход как ``${YURKO_PYTHON:-python}`` — без
# переменной ядро не стартовало, и молча. Тест «оба пути»: находка через venv
# и явный отказ с командой лечения, когда не нашлось ничего.


def test_resolve_python_keeps_the_current_interpreter_when_it_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Текущий интерпретатор годится — искать venv не нужно."""
    monkeypatch.delenv(server_env.YURKO_PYTHON_ENV, raising=False)
    monkeypatch.setattr(server_env, "_passes_core_probe", lambda exe: exe == sys.executable)

    assert server_env.resolve_python() == sys.executable


def test_resolve_python_falls_back_to_dot_venv_dev(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Текущий интерпретатор не проходит пробу — находится `.venv-dev`."""
    monkeypatch.delenv(server_env.YURKO_PYTHON_ENV, raising=False)
    venv_python = server_env._venv_python(tmp_path, ".venv-dev")
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("не справжній python, лише позначка файлу\n", encoding="utf-8")

    monkeypatch.setattr(server_env, "_passes_core_probe", lambda exe: exe == str(venv_python))

    assert server_env.resolve_python(repo_root=tmp_path) == str(venv_python)


def test_resolve_python_prefers_venv_dev_over_venv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Обидва venv придатні — обирається `.venv-dev` (порядок VENV_DIR_NAMES)."""
    monkeypatch.delenv(server_env.YURKO_PYTHON_ENV, raising=False)
    dev_python = server_env._venv_python(tmp_path, ".venv-dev")
    dev_python.parent.mkdir(parents=True)
    dev_python.write_text("dev\n", encoding="utf-8")
    plain_python = server_env._venv_python(tmp_path, ".venv")
    plain_python.parent.mkdir(parents=True)
    plain_python.write_text("plain\n", encoding="utf-8")

    monkeypatch.setattr(
        server_env, "_passes_core_probe", lambda exe: exe in (str(dev_python), str(plain_python))
    )

    assert server_env.resolve_python(repo_root=tmp_path) == str(dev_python)


def test_resolve_python_raises_with_one_ukrainian_line_when_nothing_works(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ни один кандидат не подошёл — одна строка на украинском с командой лечения."""
    monkeypatch.delenv(server_env.YURKO_PYTHON_ENV, raising=False)
    monkeypatch.setattr(server_env, "_passes_core_probe", lambda exe: False)

    with pytest.raises(server_env.PythonNotFound) as excinfo:
        server_env.resolve_python(repo_root=tmp_path)

    message = str(excinfo.value)
    assert "\n" not in message, "діагноз має бути одним рядком"
    assert "pip install -r requirements.txt" in message
    assert ".venv-dev" in message


def test_resolve_python_trusts_the_explicit_override_without_probing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``YURKO_PYTHON`` — приоритетное переозначение, и проба его не касается."""
    monkeypatch.setenv(server_env.YURKO_PYTHON_ENV, "C:/custom/python.exe")

    def _boom(exe: str) -> bool:
        raise AssertionError("проба не должна вызываться для явного переозначения")

    monkeypatch.setattr(server_env, "_passes_core_probe", _boom)

    assert server_env.resolve_python() == "C:/custom/python.exe"


def test_server_command_uses_the_resolved_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Команда запуска ядра берёт интерпретатор из :func:`resolve_python`."""
    monkeypatch.setattr(server_env, "resolve_python", lambda repo_root=None: "/fake/python")

    command = server_env.server_command()

    assert command[0] == "/fake/python"
    assert command[1].endswith("server.py")


def test_main_exits_nonzero_with_one_line_when_no_interpreter_found(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``main`` не падает трассировкой — печатает диагноз и выходит ненулём."""

    def _raise(repo_root: Path | None = None) -> str:
        raise server_env.PythonNotFound("одна строка з командою лікування")

    monkeypatch.setattr(server_env, "resolve_python", _raise)

    exit_code = server_env.main([])

    assert exit_code != 0
    captured = capsys.readouterr()
    assert "одна строка з командою лікування" in captured.err
