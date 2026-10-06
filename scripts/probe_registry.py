"""Живые пробы записей реестра, за которыми не нашлось прогона (T249).

Аудит 2026-09-09 показал: часть возможностей стоит в реестре с ``verified_at``,
за которым нет ни одной команды с выводом. Правило владельца — «``verified_at``
без прогона с релевантной выдачей не ставится», — значит каждая такая запись
обязана либо получить прогон, либо опуститься.

Скрипт ходит в сеть и потому живёт здесь, а не в тестах (принцип V). Он ничего
не чинит и ничего не пишет в реестр: он печатает, что вернул источник, а решение
принимает ведущий и записывает отдельной правкой.

Запуск:
    .venv-dev/Scripts/python.exe scripts/probe_registry.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Профиль поставки: пробуем ровно то, что увидит юрист.
os.environ.setdefault("YURKO_PROFILE", "legal")
os.environ.setdefault("USE_PG_BACKEND", "0")
os.environ.setdefault("EMBEDDING_PROVIDER", "null")

import server  # noqa: E402


def _shape(value: Any, limit: int = 220) -> str:
    if isinstance(value, dict):
        code = value.get("code") or value.get("error")
        if code:
            return f"ОТКАЗ code={value.get('code')!r} error={str(value.get('error'))[:limit]}"
        keys = {
            k: value[k]
            for k in ("resolved_document_id", "id", "title", "url", "source", "found", "count")
            if k in value
        }
        text = str(value.get("text") or "")
        if text:
            keys["text_head"] = text[:120].replace("\n", " ")
        return "ОТВЕТ " + json.dumps(keys, ensure_ascii=False)[: limit * 3]
    return str(value)[:limit]


PROBES: tuple[tuple[str, str, Any], ...] = (
    (
        "ua_rada_open_data / card / act",
        "get_law_metadata('UA', '922-19') — карточка акта, известного наперёд",
        lambda: server.get_law_metadata("UA", "922-19"),
    ),
    (
        "ua_rada_open_data / read_document / act",
        "query_law('UA', '922-19') — чтение документа целиком",
        lambda: server.query_law("UA", "922-19"),
    ),
    (
        "eu_law_eurlex_cellar / read_document / act",
        "query_law('EU', '32016R0679') — Регламент 2016/679 целиком",
        lambda: server.query_law("EU", "32016R0679"),
    ),
    (
        "eu_law_eurlex_cellar / card / act",
        "get_law_metadata('EU', '32016R0679') — карточка акта ЕС",
        lambda: server.get_law_metadata("EU", "32016R0679"),
    ),
    (
        "ua_debtors_register / search_by_identifier",
        "search_debtors(code='00032129') — по коду юрлица, структурный идентификатор",
        lambda: server.search_debtors(code="00032129"),
    ),
    (
        "ua_prozorro / search_by_identifier",
        "search_tenders('UA-2024-01-15-000001') — по номеру закупки",
        lambda: server.search_tenders(query="UA-2024-01-15-000001"),
    ),
    (
        "ua_debtors_register / search_free_text",
        "search_debtors(name=…) — свободный текст: в профиле legal он не уходит вовсе",
        lambda: server.search_debtors(name="Укрзалізниця"),
    ),
    (
        "ua_prozorro / search_free_text",
        "search_tenders(free text) — то же самое",
        lambda: server.search_tenders(query="закупівля палива"),
    ),
)


def main() -> int:
    print("# Пробы реестра —", server.CONTRACT_VERSION)
    for name, what, call in PROBES:
        try:
            outcome = call()
        except Exception as error:  # noqa: BLE001
            outcome = {"code": "raised", "error": f"{type(error).__name__}: {error}"}
        print(f"\n## {name}\n{what}\n{_shape(outcome)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
