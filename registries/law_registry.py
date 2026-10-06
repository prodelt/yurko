"""Law index loading and resolution utilities."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

# The name `_dynamic_law` gives a document it has never read. It is a hypothesis
# about where to look, not the document's name — it must never reach storage
# (ticket 15, defect 2a: ККУ was persisted as "Zakon Rada document 2341-14").
DYNAMIC_TITLE_PREFIX = "Zakon Rada document"


def dynamic_title_for(law_id: str) -> str:
    """The provisional name used until the real one is read off the document."""
    return f"{DYNAMIC_TITLE_PREFIX} {str(law_id).strip()}"


def is_placeholder_title(title: Any, law_id: str = "") -> bool:
    """True when `title` names nothing: empty, the registry stub, or the id itself."""
    candidate = str(title or "").strip()
    if not candidate:
        return True
    if candidate.casefold().startswith(DYNAMIC_TITLE_PREFIX.casefold()):
        return True
    return bool(law_id) and candidate == str(law_id).strip()


class LawRegistry:
    """Loads and resolves laws from cache/laws.json."""

    _dynamic_ttl_days = 7
    _law_id_pattern = re.compile(
        r"^[a-zа-яіїєґ]*\d+[a-zа-яіїєґ]*\d*[-_/]\d+" r"(?:[-_](?:[а-яіїєґ]{1,3}|[ivxlcdm]{1,6}))?$",
        re.IGNORECASE | re.UNICODE,
    )

    def __init__(self, json_path: str | Path) -> None:
        self._json_path = Path(json_path)
        self._laws, self._aliases = self._load_index()
        self._normalized_ids = {
            self._normalize_id(law_id): law_id for law_id in self._laws if law_id
        }
        self._normalized_aliases = {}
        for alias, law_id in self._aliases.items():
            normalized_alias = self._normalize_text(alias)
            if normalized_alias:
                self._normalized_aliases[normalized_alias] = law_id

    def _load_index(self) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        raw_text = self._json_path.read_text(encoding="utf-8")
        raw_index = json.loads(raw_text)
        raw_laws = raw_index.get("laws", {})

        if isinstance(raw_laws, list):
            return self._from_list_format(raw_laws)

        if not isinstance(raw_laws, dict):
            raise ValueError("laws.json has unsupported 'laws' format.")

        aliases = raw_index.get("aliases", {})
        if not isinstance(aliases, dict):
            aliases = {}

        laws = {law_id: dict(meta) for law_id, meta in raw_laws.items()}
        derived_aliases = self._derive_aliases(laws)
        derived_aliases.update({str(k): str(v) for k, v in aliases.items()})
        return laws, derived_aliases

    def _from_list_format(
        self, laws_list: list[dict[str, Any]]
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        laws: dict[str, dict[str, Any]] = {}
        aliases: dict[str, str] = {}

        for law in laws_list:
            law_id = str(law.get("id", "")).strip()
            if not law_id:
                continue

            url = str(law.get("url", "")).strip()
            if url and not url.startswith(("http://", "https://")):
                url = f"https://{url}"

            laws[law_id] = {
                "title": law.get("title", law_id),
                "url": url,
                "print_url": law.get("print_url") or f"{url}/print",
                "last_scraped": law.get("last_scraped"),
                "cache_ttl_days": int(law.get("ttl_days", 30)),
                "category": law.get("category"),
                "notes": law.get("notes", ""),
            }
            aliases[law_id] = law_id
            aliases[str(law.get("title", law_id))] = law_id

            for alias in law.get("aliases", []):
                aliases[str(alias)] = law_id

        return laws, aliases

    def _derive_aliases(self, laws: dict[str, dict[str, Any]]) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for law_id, law in laws.items():
            aliases[law_id] = law_id
            title = str(law.get("title", "")).strip()
            if title:
                aliases[title] = law_id
            for alias in law.get("aliases", []) or []:
                aliases[str(alias)] = law_id
        return aliases

    def list_laws(self) -> list[dict[str, Any]]:
        return [
            {
                "id": law_id,
                "title": law.get("title", law_id),
                "url": law.get("url", ""),
                "cache_ttl_days": int(law.get("cache_ttl_days", 30)),
                "category": law.get("category"),
                "notes": law.get("notes", ""),
            }
            for law_id, law in sorted(self._laws.items())
        ]

    def get_law(self, law_id: str) -> dict[str, Any] | None:
        canonical_id = self._canonical_id(law_id)
        law = self._laws.get(canonical_id)
        if law is None and self._looks_like_official_law_id(canonical_id):
            return self._dynamic_law(canonical_id)
        if law is None:
            return None
        return {"id": canonical_id, **dict(law)}

    def get_ttl_days(self, law_id: str) -> int:
        law = self.get_law(law_id) or {}
        return int(law.get("cache_ttl_days", 30))

    def is_volatile(self, law_id: str) -> bool:
        canonical_id = self._canonical_id(law_id)
        law = self._laws.get(canonical_id, {})
        return bool(law.get("volatile", False))

    def find_by_alias(self, query: str) -> list[dict[str, Any]]:
        variants = self._query_variants(query)
        if not variants:
            return []

        matches: list[dict[str, Any]] = []
        seen: set[str] = set()

        for normalized_query in variants:
            direct = self._normalized_aliases.get(normalized_query)
            if direct and direct not in seen:
                law = self.get_law(direct)
                if law:
                    matches.append(law)
                    seen.add(direct)

            for alias, law_id in self._normalized_aliases.items():
                if normalized_query not in alias and alias not in normalized_query:
                    continue
                if law_id in seen:
                    continue
                law = self.get_law(law_id)
                if law:
                    matches.append(law)
                    seen.add(law_id)

        return matches

    def resolve(self, query: str) -> dict[str, Any] | None:
        normalized = self._normalize_text(query)
        if not normalized:
            return None

        by_id = self.get_law(query.strip())
        if by_id:
            return by_id

        matches = self.find_by_alias(query)
        if matches:
            return matches[0]

        return None

    def _canonical_id(self, law_id: str) -> str:
        official_id = self._extract_official_id(law_id)
        if official_id:
            law_id = official_id
        normalized_id = self._normalize_id(law_id)
        return self._normalized_ids.get(normalized_id, law_id)

    def _extract_official_id(self, value: str) -> str | None:
        candidate = str(value or "").strip()
        if not candidate.startswith(("http://", "https://")):
            return None

        parsed = urlparse(candidate)
        host = parsed.netloc.lower()
        if not host.endswith("zakon.rada.gov.ua"):
            return None

        parts = [unquote(part) for part in parsed.path.split("/") if part]
        for prefix in (("laws", "show"), ("go",)):
            if parts[: len(prefix)] != list(prefix) or len(parts) <= len(prefix):
                continue
            law_id = parts[len(prefix)].removesuffix("#Text").strip()
            return law_id.removesuffix("/print") if law_id else None
        return None

    def _looks_like_official_law_id(self, value: str) -> bool:
        candidate = str(value or "").strip()
        if not candidate or any(char.isspace() for char in candidate):
            return False
        return bool(self._law_id_pattern.fullmatch(candidate))

    def _dynamic_law(self, law_id: str) -> dict[str, Any]:
        clean_id = law_id.strip()
        url = f"https://zakon.rada.gov.ua/laws/show/{clean_id}"
        return {
            "id": clean_id,
            "title": dynamic_title_for(clean_id),
            "url": url,
            "print_url": f"{url}/print",
            "last_scraped": None,
            "cache_ttl_days": self._dynamic_ttl_days,
            "category": "dynamic",
            "notes": "Dynamically discovered from official Zakon Rada ID or URL.",
            "dynamic": True,
            "title_is_placeholder": True,
        }

    def _query_variants(self, query: str) -> list[str]:
        normalized = self._normalize_text(query)
        if not normalized:
            return []

        variants = {normalized}
        if normalized.startswith("зу "):
            variants.add(normalized.replace("зу ", "закон ", 1))
            variants.add(normalized.replace("зу ", "закон україни ", 1))
        if normalized.startswith("закон україни "):
            variants.add(normalized.replace("закон україни ", "закон ", 1))
            variants.add(normalized.replace("закон україни ", "", 1))
        if normalized.startswith("закон "):
            variants.add(normalized.replace("закон ", "", 1))
        return [variant for variant in variants if variant]

    def _normalize_id(self, law_id: str) -> str:
        return str(law_id or "").strip().lower().replace("№", "")

    def _normalize_text(self, value: str) -> str:
        return " ".join(str(value or "").strip().lower().replace("№", "").split())
