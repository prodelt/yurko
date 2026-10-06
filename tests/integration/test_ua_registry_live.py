"""Живе читання рішення ЄДРСР за ідентифікатором (T433, FR-661).

Під маркером ``live``: без ``YURKO_LIVE=1`` пропускається, і пропущене не
дорівнює пройденому (`CLAUDE.md` §3).

Що саме доводить цей файл, а що ні. Успішне читання названого рішення з
конвертом походження — доводить FR-661. Відмова на антиботі — не доводить:
вона перевіряється детермінованим тестом (`tests/unit/test_ua_registry_identity.py`)
і без мережі. Тут потрібне саме **успішне** читання, бо його не можна
підмінити перевіркою відмов.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

pytestmark = pytest.mark.live

#: Рішення, яке читається. Обране як стабільне й давно опубліковане: реєстр
#: не змінює опублікованих рішень, тому ідентифікатор не протухає від часу.
#: Якщо джерело його прибрало — це теж результат, і він має бути видимим,
#: а не схованим за автопідбором іншого рішення.
KNOWN_DECISION_ID = "118432000"


@pytest.fixture()
def registry(tmp_path: Path):
    from registries import court_registry

    return court_registry.CourtDecisionsRegistry(tmp_path)


def _reachable() -> bool:
    """Чи встановлюється з'єднання з реєстром узагалі.

    Різниця між «джерело недосяжне звідси» і «продукт зламався» — не
    формальність: перше закривається доступом, друге правкою коду, і плутати
    їх означає шукати дефект там, де його немає. Проба 11.09.2026 з цього
    середовища: ConnectTimeout на 443 порт — з'єднання не встановлюється
    зовсім, це не заглушка антибота.
    """
    import socket

    try:
        socket.create_connection(("reyestr.court.gov.ua", 443), timeout=10).close()
        return True
    except OSError:
        return False


def test_a_named_decision_is_read_with_provenance(registry) -> None:
    from core.contracts import SourceAdapterError

    if not _reachable():
        pytest.fail(
            "реєстр недосяжний із цього середовища: з'єднання з "
            "reyestr.court.gov.ua:443 не встановлюється. Вимога FR-661 "
            "лишається ВІДКРИТОЮ — ручний шлях успішного читання не замінює "
            "(plan.md, стадія 5). Дефекту в продукті це не означає: детерміновані "
            "перевірки відмов і тотожності зелені без мережі"
        )

    try:
        answer = registry.get_decision(KNOWN_DECISION_ID)
    except SourceAdapterError as exc:
        pytest.fail(
            f"джерело відповіло, але читання {KNOWN_DECISION_ID} не вдалося: {exc}. "
            "Це вже про продукт або про поведінку джерела, а не про доступ"
        )

    assert len(answer["text"]) > 200
    assert answer["language"] == "uk"
    assert answer["publisher"]
    assert answer["source_channel"]
    assert answer["retrieved_at"]
    assert answer["url"].endswith(KNOWN_DECISION_ID)


def test_the_same_decision_reads_from_cache_the_second_time(registry) -> None:
    """Опубліковане рішення незмінне — повторне читання не б'є по джерелу."""
    if not _reachable():
        pytest.skip("реєстр недосяжний із цього середовища; див. тест вище")

    registry.get_decision(KNOWN_DECISION_ID)

    second = registry.get_decision(KNOWN_DECISION_ID)

    assert second["from_cache"] is True
