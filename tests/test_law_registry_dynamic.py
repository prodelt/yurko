from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from registries.law_registry import LawRegistry

REGISTRY_PATH = Path(__file__).parent.parent / "cache" / "laws.json"


def test_registry_returns_dynamic_metadata_for_unknown_official_id() -> None:
    registry = LawRegistry(REGISTRY_PATH)

    law = registry.get_law("361-20")

    assert law is not None
    assert law["id"] == "361-20"
    assert law["url"] == "https://zakon.rada.gov.ua/laws/show/361-20"
    assert law["print_url"] == "https://zakon.rada.gov.ua/laws/show/361-20/print"
    assert law["dynamic"] is True


def test_registry_extracts_dynamic_id_from_zakon_rada_url() -> None:
    registry = LawRegistry(REGISTRY_PATH)

    law = registry.get_law("https://zakon.rada.gov.ua/laws/show/361-20#Text")

    assert law is not None
    assert law["id"] == "361-20"


def test_registry_does_not_treat_free_text_as_dynamic_law() -> None:
    registry = LawRegistry(REGISTRY_PATH)

    assert registry.resolve("not a legal id") is None


def test_server_resolves_dynamic_law_id() -> None:
    import server

    result = server.resolve_law_id("UA", "361-20")

    assert result["id"] == "361-20"
    assert result["url"] == "https://zakon.rada.gov.ua/laws/show/361-20"
