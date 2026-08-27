"""SandboxManager: per-run isolated raw-processing workspace and worker
isolation (docs/MASTER_ARCHITECTURE_V2.md #21 and #85 Phase 2A, local
reference doc, never committed).

Genuine gap this closes: today ``Executor`` (``agents/reasoning.py``) reads
raw dataset rows as a plain in-process ``async`` coroutine, in the same
process, event loop, and class hierarchy as every LLM-calling agent, with no
network-deny, no resource ceiling, and no dedicated workspace of its own.
This module is deliberately *not* wired into ``agents/`` yet -- that is a
later phase's scope (per this session's established precedent) -- but
provides the typed contract and enforcement primitives that phase will call.

``create_sandbox`` allocates a per-run directory under
``phi_core.paths.SANDBOX_DIR`` (mode 0700) and a :class:`SandboxRecord`.
``run_isolated`` executes a callable inside that sandbox in a *separate*
process (``multiprocessing`` ``spawn`` context, not the caller's event
loop/process): the child strips provider-credential-shaped environment
variables before running, applies CPU/memory ``resource.setrlimit``
ceilings, denies outbound sockets by monkeypatching ``socket.socket``, and
is wall-clock bounded by the parent's ``Process.join(timeout)`` (SIGKILL on
timeout). ``destroy_sandbox`` removes the workspace tree and flips
``SandboxRecord.state``/``CleanupManifest.sandbox_destroyed``.

# ponytail: network-deny is enforced by monkeypatching ``socket.socket``
# inside the spawned child, not an OS-level netns/firewall rule. This
# blocks whatever bytecode ``func`` executes in-process but not a
# raw syscall from a C extension that bypasses the ``socket`` module, and
# does not survive `os.fork()`-after-patch inside `func` itself. Upgrade
# path: network namespace / seccomp / container runtime if the threat
# model needs to defend against untrusted native code, not just Python.
"""
from __future__ import annotations

import multiprocessing
import os
import resource
import shutil
import socket
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from phi_core.paths import SANDBOX_DIR, is_safe_scoped_id

from . import limits
from .records import CleanupManifest, SandboxRecord

# Environment variable name fragments that must never reach the raw-data
# worker process. Matched case-insensitively as a substring so provider-
# specific keys (ANTHROPIC_API_KEY, OPENAI_API_KEY, MONGO_URL's credentials
# embedded in the URL, ...) are all covered without an exhaustive allowlist.
_DENYLIST_ENV_FRAGMENTS = ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL", "MONGO_URL")


class SandboxError(RuntimeError):
    """Raised on sandbox creation/destruction/execution failure."""


class SandboxTimeout(SandboxError):
    """Raised when the isolated worker exceeds ``max_wall_seconds``."""


class SandboxPathViolation(SandboxError):
    """Raised when a path is not contained within a sandbox's workspace."""


def create_sandbox(
    run_id: str,
    *,
    max_cpu_seconds: int = limits.MAX_SANDBOX_CPU_SECONDS,
    max_memory_bytes: int = limits.MAX_SANDBOX_MEMORY_BYTES,
    max_wall_seconds: int = limits.MAX_SANDBOX_WALL_SECONDS,
) -> SandboxRecord:
    """Allocate a fresh, uniquely-named workspace for ``run_id``.

    A random suffix (not just ``run_id``) keeps repeat sandboxes for the
    same run from colliding with a not-yet-destroyed prior one.
    """
    if not is_safe_scoped_id(run_id):
        raise SandboxError(f"run_id is not a safe scoped identifier: {run_id!r}")
    workspace = SANDBOX_DIR / run_id / uuid4().hex
    workspace.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(workspace, 0o700)
    return SandboxRecord(
        run_id=run_id,
        workspace_path=str(workspace),
        max_cpu_seconds=max_cpu_seconds,
        max_memory_bytes=max_memory_bytes,
        max_wall_seconds=max_wall_seconds,
    )


def validate_sandbox_path(record: SandboxRecord, candidate: str | Path) -> Path:
    """PathPolicyValidator: refuse any path outside ``record.workspace_path``."""
    base = Path(record.workspace_path).resolve()
    resolved = (base / candidate if not Path(candidate).is_absolute() else Path(candidate)).resolve()
    try:
        resolved.relative_to(base)
    except ValueError:
        raise SandboxPathViolation(f"path escapes sandbox workspace: {candidate!r}") from None
    return resolved


def destroy_sandbox(
    record: SandboxRecord, manifest: CleanupManifest | None = None
) -> tuple[SandboxRecord, CleanupManifest | None]:
    """Remove the workspace tree and mark the record (and optional
    ``CleanupManifest``) destroyed. Idempotent: destroying an already-
    destroyed sandbox is a no-op success, not an error."""
    path = Path(record.workspace_path)
    try:
        if path.exists():
            shutil.rmtree(path)
        updated = record.model_copy(update={"state": "destroyed", "destroyed_at": _now()})
    except OSError as exc:
        updated = record.model_copy(update={"state": "destroy_failed", "failure_details": str(exc)})
        if manifest is not None:
            manifest = manifest.model_copy(update={"sandbox_destroyed": False})
        return updated, manifest
    if manifest is not None:
        manifest = manifest.model_copy(update={"sandbox_destroyed": True})
    return updated, manifest


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _stripped_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not any(fragment in key.upper() for fragment in _DENYLIST_ENV_FRAGMENTS)
    }


def _deny_sockets() -> None:
    def _raise(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("network access denied inside sandbox worker (Phase 2A network-deny)")

    socket.socket = _raise  # type: ignore[assignment,method-assign]


def _child_entry(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    max_cpu_seconds: int,
    max_memory_bytes: int,
    queue: "multiprocessing.Queue[tuple[bool, Any]]",
) -> None:
    os.environ.clear()
    os.environ.update(_stripped_env())
    resource.setrlimit(resource.RLIMIT_CPU, (max_cpu_seconds, max_cpu_seconds))
    resource.setrlimit(resource.RLIMIT_AS, (max_memory_bytes, max_memory_bytes))
    _deny_sockets()
    try:
        queue.put((True, func(*args, **kwargs)))
    except BaseException as exc:  # noqa: BLE001 - forward any failure to the parent
        queue.put((False, f"{type(exc).__name__}: {exc}"))


def run_isolated(
    record: SandboxRecord,
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Run ``func(*args, **kwargs)`` inside a separate process bounded by
    ``record``'s CPU/memory/wall-clock ceilings, with network access denied
    and provider-credential env vars stripped. Raises :class:`SandboxTimeout`
    on wall-clock exceedance and :class:`SandboxError` on any other failure.

    ``func`` and its arguments must be picklable (``multiprocessing``
    ``spawn`` context): this is a raw-data worker boundary, not a general
    RPC layer.
    """
    if record.state != "active":
        raise SandboxError(f"cannot run in a sandbox that is not active: {record.state!r}")
    ctx = multiprocessing.get_context("spawn")
    queue: "multiprocessing.Queue[tuple[bool, Any]]" = ctx.Queue()
    proc = ctx.Process(
        target=_child_entry,
        args=(func, args, kwargs, record.max_cpu_seconds, record.max_memory_bytes, queue),
    )
    proc.start()
    proc.join(record.max_wall_seconds)
    if proc.is_alive():
        proc.kill()
        proc.join()
        raise SandboxTimeout(f"sandbox worker exceeded {record.max_wall_seconds}s wall-clock limit")
    if queue.empty():
        raise SandboxError(f"sandbox worker exited without a result (exitcode={proc.exitcode})")
    ok, payload = queue.get()
    if not ok:
        raise SandboxError(f"sandbox worker raised: {payload}")
    return payload
