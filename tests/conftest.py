"""Suite-wide guard for Constitution V: no live network calls in tests.

The guard belongs here, not in the product. `server.py` briefly decided whether
to use the live Rada channel by looking for `PYTEST_CURRENT_TEST` in the
environment — which makes the tested path differ from the shipped one by
construction, the exact shape of the two regressions this project has already
paid for. Production code must not know it is under test; the suite mutes the
network instead.

A test that wants the live channel injects its own fake client (see
`tests/test_routing.py`), which overrides this fixture's stand-in.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

#: Live scenarios need outgoing access to state domains, which the development
#: environment does not have (quickstart.md, "Предварительные условия"). They are
#: therefore *skipped* here, never reported as passed: a green tick for a check
#: that did not run is worse than a missing tick.
LIVE_ENV_FLAG = "YURKO_LIVE"


def _live_enabled() -> bool:
    return os.getenv(LIVE_ENV_FLAG, "").strip().lower() in ("1", "true", "yes")


# --- the suite never writes into the code tree and never reaches the network -------
#
# Two rules, one mechanism each. The server writes its caches under
# ``source_cache.cache_dir()``; the suite points that at a throwaway directory
# *before* any test module imports ``server`` (import builds the registries, and they
# create their cache directories), so a run neither touches the developer's own cache
# nor the package directory. And every outgoing connection is refused and recorded:
# product code catches network errors by design, so a test that reached the network
# would otherwise pass silently on a machine without one and download tens of
# megabytes on a machine with one (ticket 50).

_CACHE_ENV = "YURKO_CACHE_DIR"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", ""})


class _NetworkGuard:
    """Process-wide socket guard; the per-test fixture switches it and reads its log."""

    def __init__(self) -> None:
        self.active = True
        self.attempts: list[str] = []
        self.real_connect = socket.socket.connect
        self.real_connect_ex = socket.socket.connect_ex
        self.real_getaddrinfo = socket.getaddrinfo

    @staticmethod
    def _is_local(address: object) -> bool:
        # A str address is an AF_UNIX path; a tuple is (host, port, ...).
        if not isinstance(address, tuple):
            return True
        return str(address[0]).lower() in _LOOPBACK_HOSTS

    def install(self) -> None:
        guard = self

        def connect(sock: socket.socket, address: Any) -> None:
            if not guard.active or guard._is_local(address):
                return guard.real_connect(sock, address)
            guard.attempts.append(f"connect {address[0]}")
            raise ConnectionRefusedError(f"live network is disabled in tests: {address[0]}")

        def connect_ex(sock: socket.socket, address: Any) -> int:
            if not guard.active or guard._is_local(address):
                return guard.real_connect_ex(sock, address)
            guard.attempts.append(f"connect {address[0]}")
            return 111  # ECONNREFUSED

        def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
            name = "" if host is None else str(host).lower()
            if not guard.active or name in _LOOPBACK_HOSTS:
                return guard.real_getaddrinfo(host, *args, **kwargs)
            guard.attempts.append(f"dns {name}")
            raise socket.gaierror(socket.EAI_NONAME, f"live network is disabled in tests: {name}")

        socket.socket.connect = connect  # type: ignore[method-assign,assignment]
        socket.socket.connect_ex = connect_ex  # type: ignore[method-assign,assignment]
        socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]


_GUARD = _NetworkGuard()
_SESSION_CACHE: list[Path] = []


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"live: needs outgoing access to official sources; runs only with {LIVE_ENV_FLAG}=1",
    )
    # Before collection: `import server` at module level of a test file must already
    # see the throwaway cache directory.
    cache = Path(tempfile.mkdtemp(prefix="yurko-tests-cache-"))
    _SESSION_CACHE.append(cache)
    os.environ[_CACHE_ENV] = str(cache)
    _GUARD.install()


def pytest_unconfigure(config: pytest.Config) -> None:
    for cache in _SESSION_CACHE:
        shutil.rmtree(cache, ignore_errors=True)
    _SESSION_CACHE.clear()


def _snapshot_code_cache() -> dict[str, tuple[bool, int]]:
    """Every entry under ``<repo>/cache`` as {relative path: (is_dir, size)}."""
    base = ROOT / "cache"
    if not base.exists():
        return {}
    return {
        path.relative_to(base).as_posix(): (
            path.is_dir(),
            0 if path.is_dir() else path.stat().st_size,
        )
        for path in sorted(base.rglob("*"))
    }


@pytest.fixture(scope="session", autouse=True)
def _code_tree_cache_is_read_only() -> Iterator[None]:
    """``cache/`` in the code tree holds ``laws.json`` and nothing the suite may change."""
    before = _snapshot_code_cache()
    yield
    after = _snapshot_code_cache()
    if after != before:
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        changed = sorted(name for name in set(before) & set(after) if before[name] != after[name])
        pytest.fail(
            "the suite changed cache/ in the code tree "
            f"(added: {added[:5]}, removed: {removed[:5]}, changed: {changed[:5]}); "
            "server caches belong under YURKO_CACHE_DIR, tests use tmp_path",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _no_live_network(request: pytest.FixtureRequest) -> Iterator[None]:
    """Refuse and record outgoing connections; fail the test that made one.

    A test marked ``live`` is the one case that wants the network — and it only ever
    runs when the operator asked for it, so the guard is lifted there.
    """
    if _live_enabled() and request.node.get_closest_marker("live") is not None:
        _GUARD.active = False
        try:
            yield
        finally:
            _GUARD.active = True
        return
    _GUARD.active = True
    _GUARD.attempts.clear()
    yield
    attempts = list(_GUARD.attempts)
    _GUARD.attempts.clear()
    if attempts:
        pytest.fail(
            f"{request.node.nodeid} reached the network ({', '.join(sorted(set(attempts)))}): "
            "substitute the source (see tests/test_tool_contracts.py) or mark the test "
            f"@pytest.mark.live so it runs only with {LIVE_ENV_FLAG}=1",
            pytrace=False,
        )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if _live_enabled():
        return
    skip_live = pytest.mark.skip(
        reason=f"live source access disabled; set {LIVE_ENV_FLAG}=1 to run"
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


class _OfflineRadaClient:
    """Stands in for RadaOpenDataClient and refuses to touch the network."""

    def search(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("live Rada search is disabled in tests (Constitution V)")

    def fetch_document(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("live Rada fetch is disabled in tests (Constitution V)")


@pytest.fixture(autouse=True)
def _offline_rada(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the module-level Rada client for every test that imports server.

    A test marked ``live`` is the one case that wants the real client — and it only
    ever runs when the operator asked for it, so the mute is lifted there.
    """
    if _live_enabled() and request.node.get_closest_marker("live") is not None:
        return
    server = sys.modules.get("server")
    if server is not None and hasattr(server, "rada_client"):
        monkeypatch.setattr(server, "rada_client", _OfflineRadaClient(), raising=False)
