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

# D8: modules that call phi_core.db.get_db() directly (confirmed via
# `grep -rl "from phi_core.db import get_db" tests/`), guarded here rather
# than with a `pytestmark` line in each file so this mechanism stays
# entirely inside this file's ownership boundary.
_MONGO_GUARDED_MODULES = {
    "test_admin_assurance.py",
    "test_admin_hold.py",
    "test_control_migrate.py",
    "test_control_store_effect_key.py",
    "test_control_phase12_cleanup_wiring.py",
    "test_resilience_restart_resume.py",  # Phase 14: calls phi_core.db.get_db() directly
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


# Wave R-c Step 4 makes sandboxing implicit across many previously-plain
# Executor-exercising tests (dataset transforms, metadata redaction, header
# reads, narrative export). On a platform that cannot enforce RLIMIT_AS
# (this includes macOS/Darwin, see docs/THREAT_MODEL_BACKEND.md),
# create_sandbox() fails closed unless PHI_SANDBOX_ALLOW_UNENFORCED_MEMORY=1
# is explicitly set, per phi_core/control/sandbox.py's documented fail-closed
# switch. Set it as the test-session default so the wide majority of tests
# exercise real sandboxed *behavior* (isolation, env stripping, return-value
# shape) rather than universally hitting SandboxError on this platform.
# Excluded by exact test name: the one test whose entire point is to prove
# the fail-closed behavior fires when the override is genuinely absent
# (test_control_phase2_sandbox_and_raw_data_boundary.py already has its own
# local, file-scoped version of this same exclusion; this mirrors it at the
# suite level now that sandboxing is no longer confined to that one file).
_SANDBOX_FAIL_CLOSED_TEST_NAME = (
    "test_create_sandbox_fails_closed_when_memory_limit_is_unenforceable_without_override"
)


@pytest.fixture(autouse=True)
def _suite_default_allow_unenforced_sandbox_memory(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.name != _SANDBOX_FAIL_CLOSED_TEST_NAME:
        monkeypatch.setenv("PHI_SANDBOX_ALLOW_UNENFORCED_MEMORY", "1")
