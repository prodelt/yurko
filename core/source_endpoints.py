"""Overridable addresses of the external sources (constitution, principle IX).

State interfaces change shape without notice — a host moves, a SPARQL endpoint
gets a new path, a print page grows a prefix. None of that is worth a release,
so every base address the adapters use is read from the environment with a
documented default:

    YURKO_ENDPOINT_<KEY>=https://...

``<KEY>`` is the registry key upper-cased with dots turned into underscores, e.g.
``YURKO_ENDPOINT_EU_CELLAR_SPARQL`` overrides ``eu.cellar_sparql``.

The defaults recorded here are the addresses measured on 2026-09-04 and cited in
``specs/001-eu-intl-law-expansion/research/``. They are *defaults*, not truth:
whatever the environment says wins, and nothing in this module reaches the
network — resolution is a pure string lookup so it is safe at import time.
"""

from __future__ import annotations

import os
from typing import Mapping

__all__ = [
    "DEFAULT_ENDPOINTS",
    "ENV_PREFIX",
    "env_var_name",
    "endpoint",
    "all_endpoints",
]

ENV_PREFIX = "YURKO_ENDPOINT_"

#: Base addresses per source. Keys are ``<legal order>.<source>`` in lower case.
DEFAULT_ENDPOINTS: Mapping[str, str] = {
    # --- Ukraine: the existing core -------------------------------------
    "ua.rada_laws": "https://zakon.rada.gov.ua",
    "ua.rada_open_data": "https://data.rada.gov.ua",
    "ua.court_decisions": "https://reyestr.court.gov.ua",
    "ua.debtors": "https://erb.minjust.gov.ua",
    "ua.open_data_catalog": "https://data.gov.ua",
    "ua.prozorro": "https://public-api.prozorro.gov.ua",
    # --- EU law and the Court of Justice ---------------------------------
    # Cellar is the machine-readable face of EUR-Lex: SPARQL for discovery,
    # content-negotiated REST for the document itself (research/01).
    "eu.cellar_sparql": "https://publications.europa.eu/webapi/rdf/sparql",
    "eu.cellar_resource": "https://publications.europa.eu/resource/cellar",
    # CELEX-addressed alias of the same REST endpoint: Cellar resolves a CELEX
    # id directly under /resource/celex/ without a prior SPARQL round trip.
    "eu.cellar_celex": "https://publications.europa.eu/resource/celex",
    "eu.eurlex": "https://eur-lex.europa.eu",
    # Legacy JSF face of CURIA: server-rendered case card (liste/fiche/documents).
    # curia.europa.eu/juris/ now 301-redirects to the InfoCuria SPA; this host
    # still answers with HTML (measured 2026-09-15). A page without a contract.
    "eu.curia_juris": "https://juris.curia.europa.eu/juris",
    # --- International fora ----------------------------------------------
    "echr.hudoc": "https://hudoc.echr.coe.int",
    "icj.court": "https://www.icj-cij.org",
}


def env_var_name(key: str) -> str:
    """Return the environment variable that overrides ``key``."""
    normalized = str(key or "").strip().lower().replace(".", "_").replace("-", "_")
    return f"{ENV_PREFIX}{normalized.upper()}"


def endpoint(key: str) -> str:
    """Return the base address for ``key``.

    The environment wins over the default. An unknown key with no override is a
    programming error, not a runtime condition, so it raises rather than
    returning an empty string that would silently produce requests to ``/``.
    """
    override = os.getenv(env_var_name(key), "").strip()
    if override:
        return override.rstrip("/")
    try:
        return DEFAULT_ENDPOINTS[str(key or "").strip().lower()].rstrip("/")
    except KeyError:
        raise KeyError(
            f"unknown source endpoint {key!r}; known keys: {', '.join(sorted(DEFAULT_ENDPOINTS))}"
        ) from None


def all_endpoints() -> dict[str, str]:
    """Return every endpoint as it will actually be used, overrides applied."""
    return {key: endpoint(key) for key in DEFAULT_ENDPOINTS}
