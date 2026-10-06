"""Тексти, які бачить користувач, — українською й без посилань на те, чого немає в поставці.

Аудит пакета 29.09.2026 (знахідка 16): `list_coverage` → `profile.notice` містив
суміш російської й української та посилання на ADR 0009; описи джерел — на
`research/NN-….md`, `ADR NNNN` і «тікет NN». У пакеті, що їде зовнішньому
користувачеві, цих файлів немає, а російська вставка в українському тексті —
дефект вигляду продукту. Перевіряється сама відповідь `list_coverage`, а не
вихідний код: те, що дійшло до моделі й юриста.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402

#: Літери, яких немає в українській абетці.
_RUSSIAN_ONLY = re.compile("[ыэъёЫЭЪЁ]")

#: Посилання на внутрішні артефакти розробки, яких немає в поставці.
_INTERNAL_REFERENCE = re.compile(
    r"\bADR\s*\d+|research/|specs?/|\bтікет\w*|\bT\d{3}\b|\bFR-\d+|\bR-\d{2}\b|\bпринцип\w*\s+[IVX]+\b",
    re.IGNORECASE,
)


def _strings(node: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _strings(value, f"{path}[{index}]")
    elif isinstance(node, str):
        yield path, node


def test_list_coverage_has_no_russian_letters_and_no_internal_references() -> None:
    answer = server.list_coverage()

    offenders = [
        f"{path}: {text[:120]!r}"
        for path, text in _strings(answer)
        if _RUSSIAN_ONLY.search(text) or _INTERNAL_REFERENCE.search(text)
    ]
    assert not offenders, "\n".join(offenders)


def test_profile_notice_is_plain_ukrainian() -> None:
    notice = server.list_coverage()["profile"]["notice"]

    assert "вільний текст" in notice
    assert not _RUSSIAN_ONLY.search(notice)
    assert "ADR" not in notice
