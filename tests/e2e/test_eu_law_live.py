"""T058 — живий тест адаптера права ЄС.

Мітка `live`: без `YURKO_LIVE=1` пропускається (не проходить) — середовище
розробки не має вихідного доступу до державних доменів (quickstart.md,
"Предварительные условия"). Пропуск — не доказ роботи адаптера; приймання
живих прогонів іде окремою задачею T113.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.provenance import SourceChannel  # noqa: E402
from sources.base import AdapterPayload  # noqa: E402
from sources.eu_law import EuLawAdapter  # noqa: E402


@pytest.mark.live
def test_live_fetch_regulation_2016_679_article_1() -> None:
    """Стаття 1 Регламенту (ЄС) 2016/679 із конвертом `source_channel = live`."""
    adapter = EuLawAdapter()

    result = adapter.fetch("32016R0679", article="1")

    assert isinstance(result, AdapterPayload)
    assert result.data["celex"] == "32016R0679"
    assert result.data["text"]
    assert result.provenance.source_channel is SourceChannel.LIVE
    assert result.provenance.attribution
    assert "2011/833/EU" in result.provenance.attribution
