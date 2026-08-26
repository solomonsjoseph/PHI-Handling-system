"""Fail-closed resource ceilings for control-plane work.

Each value may be configured with an environment variable of the same name.
Manifest budgets are intersected with these ceilings by ``CapabilityPolicy``.
"""
from __future__ import annotations

import os


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


MAX_DELEGATION_DEPTH = _int_env("MAX_DELEGATION_DEPTH", 3)
# Pipeline's fixed, code-defined sequence creates at most 35 direct child
# tasks.  Its 48 lifetime ceiling leaves headroom while
# MAX_PARALLEL_TASKS_PER_PARENT=4, MAX_TASKS_PER_RUN=64, and
# MAX_PARALLEL_TASKS_PER_RUN=6 continue to bound dynamic delegation.
MAX_CHILDREN_PER_TASK = _int_env("MAX_CHILDREN_PER_TASK", 48)
MAX_TASKS_PER_RUN = _int_env("MAX_TASKS_PER_RUN", 64)
MAX_PARALLEL_TASKS_PER_RUN = _int_env("MAX_PARALLEL_TASKS_PER_RUN", 6)
MAX_PARALLEL_TASKS_PER_PARENT = _int_env("MAX_PARALLEL_TASKS_PER_PARENT", 4)
MAX_ATTEMPTS_PER_TASK = _int_env("MAX_ATTEMPTS_PER_TASK", 3)
MAX_TASK_WALL_S = _float_env("MAX_TASK_WALL_S", 180.0)
MAX_RUN_WALL_S = _float_env("MAX_RUN_WALL_S", 900.0)
MAX_INPUT_BYTES = _int_env("MAX_INPUT_BYTES", 262144)
MAX_OUTPUT_BYTES = _int_env("MAX_OUTPUT_BYTES", 262144)
MAX_TOKENS_PER_TASK = _int_env("MAX_TOKENS_PER_TASK", 8000)
MAX_TOKENS_PER_RUN = _int_env("MAX_TOKENS_PER_RUN", 400000)
ASSUMED_USD_PER_1K_TOKENS = _float_env("ASSUMED_USD_PER_1K_TOKENS", 0.02)
MAX_COST_PER_TASK_USD = _float_env(
    "MAX_COST_PER_TASK_USD", MAX_TOKENS_PER_TASK / 1000 * ASSUMED_USD_PER_1K_TOKENS
)
MAX_COST_PER_RUN_USD = _float_env(
    "MAX_COST_PER_RUN_USD", MAX_TOKENS_PER_RUN / 1000 * ASSUMED_USD_PER_1K_TOKENS
)
MAX_TOOL_CALLS_PER_TASK = _int_env("MAX_TOOL_CALLS_PER_TASK", 3)
MAX_TOOL_CALLS_PER_RUN = _int_env("MAX_TOOL_CALLS_PER_RUN", 60)
MAX_ARTIFACT_BYTES_PER_RUN = _int_env("MAX_ARTIFACT_BYTES_PER_RUN", 2147483648)
LEASE_SECONDS = _int_env("LEASE_SECONDS", 60)
HEARTBEAT_INTERVAL_S = _int_env("HEARTBEAT_INTERVAL_S", 20)
LEASE_RECONCILE_INTERVAL_S = _int_env("LEASE_RECONCILE_INTERVAL_S", 15)
REVERSAL_CLAIM_TTL_S = _int_env("REVERSAL_CLAIM_TTL_S", 300)
MAX_RATE_BUCKET_KEYS = _int_env("MAX_RATE_BUCKET_KEYS", 10000)
MAX_CHATGPT_LOGINS = _int_env("MAX_CHATGPT_LOGINS", 32)
MAX_SESSION_PROGRESS_EVENTS = _int_env("MAX_SESSION_PROGRESS_EVENTS", 500)
MAX_OUTBOX_ENTRIES_PER_DOC = _int_env("MAX_OUTBOX_ENTRIES_PER_DOC", 32)
MAX_CHECKPOINT_PAYLOAD_REFS = _int_env("MAX_CHECKPOINT_PAYLOAD_REFS", 16)
# D16: also the real Mongo TTL index lifetime on `web_cache.fetched_at`
# (`server.py::_startup_maintenance`), not just the application-level
# staleness check `StoreResearchCache.get` performs before that.
WEB_CACHE_REFRESH_DAYS = _int_env("WEB_CACHE_REFRESH_DAYS", 7)
