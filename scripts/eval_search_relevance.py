"""Раннер набора релевантности выдачи (T222, FR-413, FR-414, SC-403).

Что он проверяет и чем отличается от теста. Тест доказывает, что код ведёт себя
как задумано на подставленном ответе. Здесь проверяется другое: **что источник
в живой сети отдаёт именно тот документ, о котором спросили**. Прогон
2026-09-07 объявил три возможности проверенными на том основании, что источник
ответил, — и выдача при этом была посторонней. Значит проверкой считается не
ответ, а наличие в ответе заведомо известного документа.

Половина набора — отрицательные случаи (``expect: refusal``). Без них раннер
доказывал бы только, что поиск иногда работает, и не поймал бы возврат
молчаливой подмены.

Живая сеть здесь допустима и обязательна: скрипт, а не тест (принцип V). Отчёт
кладётся в ``.planning/``.

Запуск::

    .venv-dev/Scripts/python.exe scripts/eval_search_relevance.py \
        --report .planning/eval-search-relevance-2026-09-08.md
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tests.integration.mcp_stdio import stdio_server_params, tool_result  # noqa: E402

SUITE = REPO_ROOT / "evals" / "search-relevance.json"


def _values_of(payload: dict[str, Any], field: str) -> list[str]:
    """Все значения поля в ответе — и на верхнем уровне, и в записях выдачи.

    Смотреть только в ``results`` нельзя: карточка провадження отдаёт документы
    в ``public_documents``, а ``resolve_law_id`` — прямо на верхнем уровне.
    Поле ищется там, где источник его положил, а не там, где удобно.
    """
    found: list[str] = []
    top = payload.get(field)
    if isinstance(top, str) and top:
        found.append(top)
    for key in ("results", "public_documents", "items", "documents"):
        rows = payload.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get(field), str):
                found.append(row[field])
    return found


def _contains(payload: dict[str, Any], field: str, wanted: str) -> bool:
    """Есть ли запрошенное значение среди значений поля.

    Номера заяв объединённого дела приходят склейкой («31253/96;14038/88;…»),
    поэтому сравнение идёт по частям, а не по целой строке.
    """
    for value in _values_of(payload, field):
        parts = {part.strip().rstrip("+") for part in value.replace(",", ";").split(";")}
        if wanted in parts or wanted == value.strip():
            return True
    return False


def _judge(case: dict[str, Any], payload: dict[str, Any]) -> tuple[str, str]:
    """Оценка одного элемента: ``pass`` / ``fail`` и почему."""
    expect = str(case.get("expect") or "hit")
    code = str(payload.get("code") or "")

    if expect == "refusal":
        if code:
            return "pass", f"отказ `{code}` — выдачи не было"
        found = payload.get("found")
        return "fail", (
            "источник ответил вместо отказа"
            + (f" (найдено {found})" if found is not None else "")
            + " — выдача запроса не касается, а выдана за результат"
        )

    if expect == "absent":
        # Третий исход, который нельзя путать ни с находкой, ни с отказом:
        # источник прочитан целиком, и запрошенного документа в нём нет. Для
        # наборов метаданных это существенно — «в наборе нет» и «выдача усечена»
        # означают для юриста разное, а до T229 второе выдавалось за первое.
        if code:
            return "fail", f"ожидалось доказанное отсутствие, получен отказ `{code}`"
        if payload.get("found"):
            return "fail", f"источник вернул {payload.get('found')} записей вместо отсутствия"
        if not payload.get("complete"):
            return "fail", (
                "выдача пуста, но неполна: это не доказанное отсутствие, "
                "а нечитанный остаток набора"
            )
        return "pass", "набор прочитан полностью, записи в нём нет (complete=true, found=0)"

    if expect == "not_published":
        status = str(payload.get("published_status") or "")
        if code:
            return "pass", f"отказ `{code}`"
        if status in (
            "not_published",
            "registered_no_documents",
            "case_not_found",
            "unknown_case_number_format",
        ):
            return "pass", f"`published_status={status}`"
        return "fail", f"ожидалось отсутствие публикации, получено `published_status={status}`"

    if code:
        return "fail", f"источник отказал: `{code}` — {payload.get('error') or ''}"

    marker = case.get("must_contain") or {}
    field, wanted = str(marker.get("field") or ""), str(marker.get("value") or "")
    if not field:
        return "pass", "ответ получен (маркер не задан)"
    if _contains(payload, field, wanted):
        return "pass", f"в выдаче есть `{field}={wanted}`"
    seen = ", ".join(_values_of(payload, field)[:3]) or "(поле в ответе отсутствует)"
    return "fail", f"в выдаче НЕТ `{field}={wanted}`; что вернулось: {seen}"


async def _run(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    rows: list[dict[str, Any]] = []
    async with stdio_client(stdio_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for case in cases:
                args = {"legal_order": case["legal_order"], **(case.get("args") or {})}
                try:
                    payload = tool_result(await session.call_tool(case["tool"], args))
                except Exception as failure:  # noqa: BLE001 — сбой вызова тоже результат
                    payload = {"code": type(failure).__name__, "error": str(failure)}
                verdict, why = _judge(case, payload)
                rows.append({**case, "verdict": verdict, "why": why})
    return rows


def _report(rows: list[dict[str, Any]], generated_at: str) -> str:
    failed = [row for row in rows if row["verdict"] == "fail"]
    lines = [
        f"# Релевантность выдачи: прогон {generated_at}",
        "",
        "Проверкой считается не ответ источника, а **наличие в ответе заведомо",
        "известного документа**. Элемент, где выдача обязанного документа не",
        "содержит, — это провал возможности, а не провал раннера.",
        "",
        f"Элементов: {len(rows)}; прошло: {len(rows) - len(failed)}; провалено: {len(failed)}.",
        "",
        "| Элемент | Правопорядок | Инструмент | Ожидалось | Итог | Чем подтверждено |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        mark = "прошло" if row["verdict"] == "pass" else "**ПРОВАЛ**"
        lines.append(
            f"| `{row['case_id']}` | {row['legal_order']} | `{row['tool']}` | "
            f"{row.get('expect', 'hit')} | {mark} | {row['why']} |"
        )
    lines += ["", "## Что стоит за каждым элементом", ""]
    for row in rows:
        lines.append(f"- **`{row['case_id']}`** — {row.get('known_document', '')}")
    if failed:
        lines += [
            "",
            "## Провалы — что делать",
            "",
            "Провал означает: возможность не подтверждена прогоном, а значит её слой в",
            "`legal_orders.py` должен быть понижен до `not_covered` с `verified_at=None`",
            "и ручным путём (FR-414). Молчаливо оставить обещание нельзя.",
            "",
        ]
        for row in failed:
            lines.append(f"- `{row['case_id']}` ({row['legal_order']}/{row['tool']}): {row['why']}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=SUITE)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--json", dest="raw", type=Path, default=None)
    args = parser.parse_args(argv)

    suite = json.loads(args.suite.read_text(encoding="utf-8"))
    rows = asyncio.run(_run(suite["cases"]))
    generated_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    report = _report(rows, generated_at)

    if args.raw is not None:
        args.raw.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report, encoding="utf-8")
    encoding = sys.stdout.encoding or "utf-8"
    sys.stdout.write(report.encode(encoding, errors="replace").decode(encoding))
    # Провал набора — ненулевой код возврата: иначе «зелёный прогон» ничего не
    # сторожит и отчёт читают глазами.
    return 1 if any(row["verdict"] == "fail" for row in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
