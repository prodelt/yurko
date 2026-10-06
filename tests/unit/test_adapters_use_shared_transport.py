"""Тікети 23 і 27 — мережа джерела йде через :mod:`sources.transport`, не повз нього.

Власна ``requests.Session`` у читачі виглядає невинно, доки мережа по дорозі не
скине простояне keep-alive-з'єднання: тоді читач без спільного транспорту
відповідає ``source_unavailable`` там, де сусідній читач до того самого хоста
відповідає нормально (живий збій 15.09.2026, який і породив
:mod:`sources.transport`). Розбіжність тиха: тести проходять, бо підмінена
сесія ніколи не буває простояною.

Тому гейт двоскладовий: у вихідному коді ``sources/`` і ``registries/`` немає
прямого звернення до мережі — крім поіменних винятків нижче, кожен зі своєю
причиною, — і кожен читач несе саме ``SourceTransport`` над тією сесією, яку
йому дали. Перелік винятків важливіший за перелік підкорених модулів: новий
файл потрапляє під гейт сам, і щоб його обійти, доведеться написати чому.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from registries.court_registry import CourtDecisionsRegistry  # noqa: E402
from registries.open_data_discovery import OpenDataDiscovery  # noqa: E402
from registries.scraper import Scraper  # noqa: E402
from registries.state_registries import (  # noqa: E402
    DebtorsRegistry,
    OpenDataCatalog,
    ProzorroRegistry,
)
from sources.echr import EchrAdapter  # noqa: E402
from sources.icj import IcjAdapter  # noqa: E402
from sources.transport import SourceTransport  # noqa: E402

_ROOT = Path(__file__).parent.parent.parent

#: Файли, яким дозволено ходити в мережу самотужки, і чому саме їм.
#: Виняток тут — це рішення, а не недогляд: додаючи рядок, називайте причину.
_EXCEPTIONS = {
    "sources/transport.py": "сам транспорт",
    "registries/rada_open_data.py": (
        "власна політика повторів, підібрана під data.rada (обрив приблизно "
        "одного з'єднання з п'ятнадцяти на рукостисканні TLS); спільне правило "
        "«остаточна відмова не повторюється» вже бере звідси — is_settled_refusal"
    ),
    "registries/source_adapters.py": (
        "проба зв'язності робить HEAD, якого транспорт не вміє; пул тут не "
        "перевикористовується — проба на те й проба"
    ),
    "search/embeddings.py": ("не джерело права, а API ембедингів Google із власним backoff на 429"),
}

#: Пряме звернення до мережі: ``self._session.get(…)``, ``session.post(…)``,
#: ``requests.request(…)``.
_DIRECT_CALL = re.compile(
    r"\b(?:requests|[\w.]*session)\s*\.\s*(?:get|post|put|head|request)\s*\(", re.IGNORECASE
)


def _guarded_files() -> list[Path]:
    files = sorted(_ROOT.glob("sources/*.py")) + sorted(_ROOT.glob("registries/*.py"))
    return [path for path in files if path.name != "__init__.py"]


@pytest.mark.parametrize("path", _guarded_files(), ids=lambda path: path.name)
def test_module_does_not_reach_the_network_past_the_shared_transport(path: Path) -> None:
    relative = path.relative_to(_ROOT).as_posix()
    source = path.read_text(encoding="utf-8")

    offenders = [
        f"{relative}:{number}: {line.strip()}"
        for number, line in enumerate(source.splitlines(), start=1)
        if _DIRECT_CALL.search(line)
    ]

    if relative in _EXCEPTIONS:
        assert offenders, (
            f"{relative} більше не ходить у мережу сам — приберіть виняток "
            f"({_EXCEPTIONS[relative]})"
        )
        return
    assert not offenders, "мережа повз sources.transport:\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    "factory",
    [
        EchrAdapter,
        IcjAdapter,
        DebtorsRegistry,
        ProzorroRegistry,
        OpenDataCatalog,
        Scraper,
    ],
)
def test_reader_wraps_the_given_session_in_the_shared_transport(factory: Any) -> None:
    session = requests.Session()

    reader = factory(session=session)

    transport = reader._transport
    assert isinstance(transport, SourceTransport)
    assert transport.session is session


@pytest.mark.parametrize("factory", [CourtDecisionsRegistry])
def test_cached_registry_wraps_the_given_session_too(factory: Any, tmp_path: Path) -> None:
    """Той самий гейт для реєстру, якому ще потрібна тека кешу."""
    session = requests.Session()

    reader = factory(cache_dir=tmp_path, session=session)

    assert isinstance(reader._transport, SourceTransport)
    assert reader._transport.session is session


def test_open_data_discovery_wraps_the_given_session(tmp_path: Path) -> None:
    session = requests.Session()

    discovery = OpenDataDiscovery(tmp_path, session=session)

    assert isinstance(discovery._transport, SourceTransport)
    assert discovery._transport.session is session
