"""Транспортная сессия MCP как контекст доказательств (T122, ADR 0002).

Идентификатор сессии берётся из транспорта, а не из аргумента инструмента:
пользовательский ``session_id`` позволял бы одной сессии читать доказательства
другой, просто назвав её. FastMCP даёт ``Context.session_id`` для HTTP и
stdio-соединений; вне запроса (прямой вызов функции в тестах или скрипте)
контекста нет, и тогда используется процессный локальный идентификатор —
явно названный, чтобы его нельзя было спутать с транспортной сессией.
"""

from __future__ import annotations

import logging
import os
import uuid

logger = logging.getLogger("ukraine-laws")

__all__ = ["LOCAL_SESSION_PREFIX", "current_session_id"]

#: Префикс сессии процесса без транспортного контекста (тесты, скрипты).
LOCAL_SESSION_PREFIX = "local-process:"

_LOCAL_SESSION_ID = f"{LOCAL_SESSION_PREFIX}{os.getpid()}:{uuid.uuid4().hex[:8]}"


def current_session_id() -> str:
    """Идентификатор действующей сессии.

    Транспортная сессия FastMCP, если вызов идёт из обработчика MCP-запроса;
    иначе локальная сессия процесса. Никакой аргумент не может её подменить.
    """
    try:
        from fastmcp.server.dependencies import get_context

        context = get_context()
    except Exception:
        return _LOCAL_SESSION_ID
    try:
        session_id = str(context.session_id or "").strip()
    except Exception:
        session_id = ""
    return f"mcp:{session_id}" if session_id else _LOCAL_SESSION_ID
