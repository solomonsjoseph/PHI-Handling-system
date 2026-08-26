"""Shared pytest configuration: load backend/.env so ANTHROPIC_API_KEY and
MONGO_URL are visible to unit tests that check integrations directly.

Tests that require a live LLM guard on ``ANTHROPIC_API_KEY``; without the
key they skip. Tests that require Mongo guard on ``MONGO_URL``.
"""
from __future__ import annotations

import os
from pathlib import Path

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


import pytest


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
