"""Shared pytest configuration: load backend/.env so ANTHROPIC_API_KEY and
MONGO_URL are visible to unit tests that check integrations directly.

Tests that require a live LLM guard on ``ANTHROPIC_API_KEY``; without the
key they skip. D8: this docstring previously claimed tests guard on
``MONGO_URL`` when zero such guards existed. They do now: exactly the
three modules that call the real ``phi_core.db.get_db()`` directly
(``test_admin_assurance.py``, ``test_admin_hold.py``,
``test_control_migrate.py``) are skipped via ``pytest_collection_modifyitems``
below when a live ``mongod`` is not reachable at ``MONGO_URL``, instead of
hanging for pymongo's server-selection timeout and then failing.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

try:
    from dotenv import load_dotenv
    _ENV = Path(__file__).resolve().parents[1] / ".env"
    if _ENV.exists():
        load_dotenv(_ENV)
except Exception:
    # dotenv is optional; the test-suite must still run in environments
    # where python-dotenv isn't installed.
    pass

# Sensible defaults for local unit tests when the .env file is absent.
os.environ.setdefault("DB_NAME", "phi_handling")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("PHI_ENV", "dev")
# D13 step 1: session_human_review authorizes against REVIEWER_PRINCIPALS,
# not merely session ownership. Only "dev" gets a role for free in
# PHI_ENV=dev; every other principal the test suite exercises as a
# reviewer needs an explicit entry here, same as a real deployment would
# configure for its own reviewer accounts.
os.environ.setdefault(
    "REVIEWER_PRINCIPALS",
    "dev:lead_reviewer,reviewer:lead_reviewer,reviewer-1:lead_reviewer,alice:lead_reviewer,"
    "operator:lead_reviewer",
)


def _mongo_up() -> bool:
    """Best-effort, protocol-agnostic reachability check: a plain TCP
    connect to MONGO_URL's host:port within 0.25s. Not a real handshake --
    it exists only to decide fast whether the three modules below should
    run against a live mongod or skip, instead of discovering the answer
    the slow way via pymongo's ~30s server-selection timeout."""
    import socket as _socket
    from urllib.parse import urlsplit

    parsed = urlsplit(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
    host = parsed.hostname or "localhost"
    port = parsed.port or 27017
    try:
        with _socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


needs_mongo = pytest.mark.skipif(not _mongo_up(), reason="mongod not reachable")

# D8: the exact three modules that call phi_core.db.get_db() directly
# (confirmed via `grep -rl "from phi_core.db import get_db" tests/`), guarded
# here rather than with a `pytestmark` line in each file so this mechanism
# stays entirely inside this file's ownership boundary.
_MONGO_GUARDED_MODULES = {
    "test_admin_assurance.py",
    "test_admin_hold.py",
    "test_control_migrate.py",
}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if Path(item.fspath).name in _MONGO_GUARDED_MODULES:
            item.add_marker(needs_mongo)


@pytest.fixture(autouse=True)
def _reset_pipeline_admission_control():
    """Tests that stub out asyncio.create_task (e.g. `hold_worker` patterns
    that `coro.close()` a scheduled worker instead of running it) never
    reach the worker's `finally: _release_pipeline_run()`. Without a reset,
    server._active_pipeline_count leaks upward across the whole test
    session and later tests see phantom 429 capacity-exhausted errors."""
    try:
        import server as srv
        srv._active_pipeline_count = 0
    except Exception:
        pass
    yield
    try:
        import server as srv
        srv._active_pipeline_count = 0
    except Exception:
        pass
