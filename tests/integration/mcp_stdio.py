"""Настоящая MCP-сессия по stdio: подпроцесс ядра и разбор ответа (T178).

Живая цепочка проверяется только так: клиент поднимает ``server.py``
подпроцессом ровно теми переменными окружения, которыми его поднимет плагин, и
говорит с ним по протоколу. Вызов функции сервера напрямую проверял бы другую
программу — без транспортной сессии, от которой зависит память доказательств
(``session_context.py``).

Форма ответа принадлежит клиентской библиотеке и уже менялась: полезная нагрузка
лежала под ключом ``result``, потом стала самим ``structured_content``.
:func:`tool_result` принимает обе, чтобы прогон падал на поведении сервера, а не
на переименовании поля в чужом пакете.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp import StdioServerParameters

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Ключи hosted-моделей в окружении сервера — нарушение профиля ``legal``
#: (``yurko_profile.validate_environment``), поэтому в подпроцесс не попадают.
_LEAKED_KEYS = ("GOOGLE_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")


def stdio_server_params(**extra_env: str) -> StdioServerParameters:
    """Параметры запуска ядра по stdio — те же, что в ``plugin/yurko/.mcp.json``."""
    env = {
        **os.environ,
        "YURKO_PROFILE": "legal",
        "USE_PG_BACKEND": "0",
        "EMBEDDING_PROVIDER": "null",
        "MCP_TRANSPORT": "stdio",
        "PYTHONIOENCODING": "utf-8",
        "FASTMCP_CHECK_FOR_UPDATES": "off",
        **extra_env,
    }
    for leaked in _LEAKED_KEYS:
        env.pop(leaked, None)
    return StdioServerParameters(
        command=str(REPO_ROOT / ".venv-dev" / "Scripts" / "python.exe"),
        # Вход тот же, что в ``plugin/yurko/.mcp.json``: ядро поднимается через
        # белый список окружения (T240), а не напрямую. Прогон, который
        # запускал бы ``server.py``, проверял бы не ту программу, которую
        # получает юрист.
        args=[str(REPO_ROOT / "server_env.py")],
        env=env,
        cwd=str(REPO_ROOT),
    )


def tool_result(call: Any) -> dict[str, Any]:
    """Полезная нагрузка вызова инструмента, независимо от версии клиента."""
    payload = getattr(call, "structured_content", None)
    if payload is None:
        payload = getattr(call, "structuredContent", None)
    payload = payload or {}
    inner = payload.get("result")
    return inner if isinstance(inner, dict) else payload
