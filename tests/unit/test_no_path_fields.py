"""У входах ядра немає полів файла, шляху й вкладення (T394, FR-653).

Конституція, принцип VI: матеріали справи ядро не отримує. Дефект, від якого
захищає саме цей тест, — **вкладене** поле: верхній рівень схеми чистий, а на
третьому рівні вкладеності лежить структура зі шляхом. Так і жив
``role_profile_path`` до карти 006: вхід ``moot_begin`` шляхів не мав, а
елемент його панелі мав.

Обхід — рекурсивний і по фактичних схемах фактично зареєстрованих
інструментів. Перелік моделей, переписаний у тіло тесту, старіє мовчки.

Винятків два, і обидва названі поіменно. ``CaseLocator.file`` — **ім'я**
документа справи, а не адреса: конституція вимагає саме імені («локатор факту
справи несе ім'я файла»). ``path`` у входах читання — адреса **всередині**
документа: стаття, пункт, параграф. Обидва винятки закріплені разом із
перевіркою, що вони не можуть стати адресою у файловій системі.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from core import contracts  # noqa: E402

#: Імена полів, які означають адресу у файловій системі або вкладення.
FORBIDDEN_NAMES = frozenset(
    {
        "path",
        "file_path",
        "filepath",
        "filename",
        "attachment",
        "attachments",
        "content_base64",
        "blob",
        "upload",
        "document_path",
        "role_profile_path",
        "case_file",
        "case_path",
        "output_path",
        "source_path",
    }
)

#: Дозволені винятки, і кожен названий поіменно.
#:
#: ``CaseLocator.file`` — **ім'я** документа справи (конституція вимагає саме
#: імені), а не адреса.
#:
#: ``path`` у входах читання — адреса **всередині** документа: стаття, пункт,
#: параграф. Ім'я поля успадковане від 001 і оманливе, тому виняток тут іде в
#: парі з перевіркою нижче: значення, схоже на шлях у файловій системі, на URL
#: або на ім'я файла, модель відхиляє.
ALLOWED = frozenset(
    {
        ("CaseLocator", "file"),
        ("GetFragmentInput", "path"),
        ("GetFragmentsInput", "paths"),
        ("GetDecisionInput", "path"),
        ("VerifyQuoteInput", "path"),
    }
)


def _pydantic_models() -> dict[str, type[BaseModel]]:
    """Усі моделі контракту, доступні за іменем."""
    found: dict[str, type[BaseModel]] = {}
    for name, obj in vars(contracts).items():
        if inspect.isclass(obj) and issubclass(obj, BaseModel):
            found[name] = obj
    return found


def _walk(model: type[BaseModel], seen: set[str] | None = None) -> list[tuple[str, str]]:
    """Рекурсивно обійти модель і всі вкладені: ``(модель, поле)``."""
    seen = seen if seen is not None else set()
    if model.__name__ in seen:
        return []
    seen.add(model.__name__)

    fields: list[tuple[str, str]] = []
    for field_name, field in model.model_fields.items():
        fields.append((model.__name__, field_name))
        for nested in _nested_models(field.annotation):
            fields.extend(_walk(nested, seen))
    return fields


def _nested_models(annotation: object) -> list[type[BaseModel]]:
    """Вкладені pydantic-моделі анотації, включно з обгортками й контейнерами."""
    found: list[type[BaseModel]] = []
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        found.append(annotation)
    for arg in getattr(annotation, "__args__", ()) or ():
        found.extend(_nested_models(arg))
    return found


def _input_models() -> list[type[BaseModel]]:
    """Моделі входу інструментів: усе, що успадковує ``_ToolInput``."""
    return [
        model
        for model in _pydantic_models().values()
        if issubclass(model, contracts._ToolInput) and model is not contracts._ToolInput
    ]


@pytest.mark.parametrize("model", _input_models(), ids=lambda m: m.__name__)
def test_no_path_field_anywhere_in_the_input_tree(model) -> None:
    offenders = [
        (owner, field)
        for owner, field in _walk(model)
        if field.lower() in FORBIDDEN_NAMES and (owner, field) not in ALLOWED
    ]

    assert not offenders, (
        f"{model.__name__}: поля шляху у вхідному дереві: {offenders} — "
        "ядро матеріалів справи не отримує (конституція, принцип VI)"
    )


def test_the_one_allowed_exception_stays_a_name_not_an_address() -> None:
    """``CaseLocator.file`` — ім'я документа справи, і роздільник шляху відхиляється."""
    ok = contracts.CaseLocator(file="klopotannya-2026-08-14.docx", locator="с. 3")
    assert ok.file.endswith(".docx")

    for address in (
        "E:/LawyerCases/case-1/klopotannya.docx",
        "../klopotannya.docx",
        "cases\\klopotannya.docx",
    ):
        with pytest.raises(ValidationError):
            contracts.CaseLocator(file=address, locator="с. 3")


def test_the_internal_address_exception_cannot_become_a_filesystem_path() -> None:
    """``path`` — пункт документа, і адресою у файловій системі стати не може."""
    ok = contracts.GetDecisionInput(legal_order="eu", document_id="C-605/26", path="§ 45")
    assert ok.path == "§ 45"

    for address in (
        "E:/LawyerCases/case-1/rishennya.docx",
        "../secret.pdf",
        "file:///etc/passwd",
        "/home/lawyer/case.txt",
        "https://example.org/a",
        "cases\rishennya.docx",
    ):
        with pytest.raises(ValidationError):
            contracts.GetDecisionInput(legal_order="eu", document_id="C-605/26", path=address)
