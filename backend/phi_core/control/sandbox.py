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
It fails closed with :class:`SandboxError` if this platform cannot
actually enforce ``RLIMIT_AS`` at the configured ceiling (Darwin/XNU,
CPython issue 78783), unless ``PHI_SANDBOX_ALLOW_UNENFORCED_MEMORY=1``.
``run_isolated`` executes a callable inside that sandbox in a *separate*
process (``multiprocessing`` ``spawn`` context, not the caller's event
loop/process): the child's environment is rebuilt from a fixed allowlist
(never a credential denylist) before running, CPU/memory/output-size
``resource.setrlimit`` ceilings are applied, outbound sockets are denied
by monkeypatching ``socket.socket``, and the run is wall-clock bounded by
draining the result queue with a timeout before joining the process
(SIGKILL if still alive). The child's return value is validated against
a fixed contract (path/count/status only, never an arbitrary payload) and
any raised exception is forwarded scrubbed and length-capped, never
verbatim. ``destroy_sandbox`` removes the workspace tree and flips
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
import queue as _queue_module
import resource
import shutil
import socket
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4
from phi_core.paths import SANDBOX_DIR, is_safe_scoped_id
from phi_core.security import scrub_persisted_text

from . import limits
from .records import CleanupManifest, SandboxRecord

# D1/D7: an explicit allowlist of environment variables the raw-data
# worker is permitted to inherit. Replaces a substring denylist
# ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL", "MONGO_URL")
# that missed APP_ENCRYPTION_KEY and ATTESTATION_SIGNING_KEY (D7) and,
# separately, was applied after os.environ had already been cleared to {}
# so it never actually ran against anything (D1). An allowlist cannot
# miss a credential shape the way a denylist can: anything not named here
# never reaches the child, full stop.
_ALLOWLISTED_ENV_KEYS = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE")

# Fail-closed switch (Wave R-b): a DEDICATED env var, never PHI_ENV, which
# is already an unrelated master kill switch for several other things
# (boot-time config validation, API token requirement, encryption-key
# auto-generation, cookie Secure, HSTS, a free lead_reviewer grant) and
# ships as PHI_ENV=dev in backend/.env.example. Hanging the memory
# ceiling on that same switch would mean one copied line disables five
# unrelated protections plus this one.
_MEMORY_LIMIT_UNENFORCED_OVERRIDE_ENV = "PHI_SANDBOX_ALLOW_UNENFORCED_MEMORY"


def _probe_memory_limit_enforceable() -> bool:
    """Whether ``RLIMIT_AS`` can be set to the real configured sandbox
    memory ceiling on this platform. Not "does setrlimit accept some
    arbitrarily huge finite value" (Darwin/XNU accepts those too) but
    "can this system enforce the ceiling it actually configures"
    (``limits.MAX_SANDBOX_MEMORY_BYTES``): Darwin/XNU rejects an
    ``RLIMIT_AS`` value below the process's already-mapped virtual
    address space with ``EINVAL``, which CPython mistranslates as
    ``ValueError('current limit exceeds maximum limit')`` (CPython issue
    78783, open, documented XNU limitation) -- and a modern Python
    process's shared-library mappings alone can exceed the 1 GiB default
    ceiling. Only the soft limit is touched: lowering the hard limit is a
    one-way ratchet without elevated privilege, so touching it here would
    make the finally-restore itself capable of failing. An identity write
    (re-setting the limit already in effect, almost always
    ``RLIM_INFINITY``) succeeds on Darwin too, so it is NOT a valid
    capability probe.
    """
    original_soft, original_hard = resource.getrlimit(resource.RLIMIT_AS)
    probe_soft = (
        limits.MAX_SANDBOX_MEMORY_BYTES
        if original_hard == resource.RLIM_INFINITY
        else min(limits.MAX_SANDBOX_MEMORY_BYTES, original_hard)
    )
    try:
        resource.setrlimit(resource.RLIMIT_AS, (probe_soft, original_hard))
        return True
    except (ValueError, OSError):
        return False
    finally:
        try:
            resource.setrlimit(resource.RLIMIT_AS, (original_soft, original_hard))
        except (ValueError, OSError):
            pass


_MEMORY_LIMIT_ENFORCEABLE = _probe_memory_limit_enforceable()


class SandboxError(RuntimeError):
    """Raised on sandbox creation/destruction/execution failure."""


class SandboxTimeout(SandboxError):
    """Raised when the isolated worker exceeds ``max_wall_seconds``."""


class SandboxPathViolation(SandboxError):
    """Raised when a path is not contained within a sandbox's workspace."""


class SandboxReturnContractViolation(SandboxError):
    """Raised when a sandboxed worker's return value is not a path, count,
    or status (see ``_validate_return_contract``)."""


def create_sandbox(
    run_id: str,
    *,
    max_cpu_seconds: int = limits.MAX_SANDBOX_CPU_SECONDS,
    max_memory_bytes: int = limits.MAX_SANDBOX_MEMORY_BYTES,
    max_wall_seconds: int = limits.MAX_SANDBOX_WALL_SECONDS,
    max_output_bytes: int = limits.MAX_SANDBOX_OUTPUT_BYTES,
) -> SandboxRecord:
    """Allocate a fresh, uniquely-named workspace for ``run_id``.

    A random suffix (not just ``run_id``) keeps repeat sandboxes for the
    same run from colliding with a not-yet-destroyed prior one.

    Fails closed when this platform cannot actually enforce the memory
    ceiling (``_MEMORY_LIMIT_ENFORCEABLE`` is False, e.g. Darwin/XNU, see
    ``_probe_memory_limit_enforceable``): a raw-data sandbox with no real
    memory ceiling is not the boundary this module promises. Set
    ``PHI_SANDBOX_ALLOW_UNENFORCED_MEMORY=1`` to explicitly accept that
    gap (e.g. local development on macOS).
    """
    if not is_safe_scoped_id(run_id):
        raise SandboxError(f"run_id is not a safe scoped identifier: {run_id!r}")
    if not _MEMORY_LIMIT_ENFORCEABLE and os.environ.get(_MEMORY_LIMIT_UNENFORCED_OVERRIDE_ENV) != "1":
        raise SandboxError(
            "sandbox memory ceiling cannot be enforced on this platform "
            "(RLIMIT_AS rejects the configured ceiling here, see CPython "
            "issue 78783); set PHI_SANDBOX_ALLOW_UNENFORCED_MEMORY=1 to "
            "run raw-data workers without a real memory ceiling anyway"
        )
    run_dir = SANDBOX_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(run_dir, 0o700)  # D9: mkdir's mode only applies to the leaf
    workspace = run_dir / uuid4().hex
    workspace.mkdir(mode=0o700)
    os.chmod(workspace, 0o700)
    return SandboxRecord(
        run_id=run_id,
        workspace_path=str(workspace),
        max_cpu_seconds=max_cpu_seconds,
        max_memory_bytes=max_memory_bytes,
        max_wall_seconds=max_wall_seconds,
        max_output_bytes=max_output_bytes,
        memory_limit_enforced=_MEMORY_LIMIT_ENFORCEABLE,
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


def _allowlisted_env() -> dict[str, str]:
    return {key: os.environ[key] for key in _ALLOWLISTED_ENV_KEYS if key in os.environ}


def _deny_sockets() -> None:
    def _raise(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("network access denied inside sandbox worker (Phase 2A network-deny)")

    socket.socket = _raise  # type: ignore[assignment,method-assign]


# D3 (child half): a raw exception's text can embed the offending
# cell/column content (e.g. a ValueError raised while validating a row).
# That text is later SHA-256 chained into the trace and streamed over
# SSE, so only a scrubbed, length-capped summary ever leaves the child.
_MAX_FORWARDED_ERROR_CHARS = 2000

# Return contract: a sandboxed worker may only hand back a workspace-
# relative artifact path, a count, or a status value -- never an
# arbitrary object. An arbitrary payload crossing back into the parent
# process (the same process that runs every LLM agent) would relocate
# the raw-data read into the parent without creating a real boundary;
# callers must write real row data to a workspace artifact and hand back
# its path instead.
_ALLOWED_RETURN_TYPES = (str, int, float, bool, type(None))


def _validate_return_contract(payload: Any) -> None:
    if not isinstance(payload, _ALLOWED_RETURN_TYPES):
        raise SandboxReturnContractViolation(
            "sandbox worker return value violates the return contract: "
            "expected a path/count/status (str, int, float, bool, or "
            f"None), got {type(payload).__name__}"
        )


def _child_entry(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    max_cpu_seconds: int,
    max_memory_bytes: int,
    max_output_bytes: int,
    queue: "multiprocessing.Queue[tuple[bool, Any]]",
) -> None:
    allowlisted_env = _allowlisted_env()
    os.environ.clear()
    os.environ.update(allowlisted_env)
    resource.setrlimit(resource.RLIMIT_CPU, (max_cpu_seconds, max_cpu_seconds))
    resource.setrlimit(resource.RLIMIT_FSIZE, (max_output_bytes, max_output_bytes))
    if _MEMORY_LIMIT_ENFORCEABLE:
        resource.setrlimit(resource.RLIMIT_AS, (max_memory_bytes, max_memory_bytes))
    _deny_sockets()
    try:
        result = func(*args, **kwargs)
        _validate_return_contract(result)
        queue.put((True, result))
    except BaseException as exc:  # noqa: BLE001 - forward any failure to the parent
        message = scrub_persisted_text(str(exc))[:_MAX_FORWARDED_ERROR_CHARS]
        queue.put((False, f"{type(exc).__name__}: {message}"))


_PROCESS_JOIN_GRACE_SECONDS = 5


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

    D2: the result is drained from the queue BEFORE joining the process,
    never after. A child process that has put an item on a
    ``multiprocessing.Queue`` larger than the OS pipe buffer (~64 KiB)
    will not actually terminate until that item is fed through the pipe
    by its feeder thread -- documented CPython behavior, not a bug in the
    child. Calling ``proc.join()`` first, before anything drains the
    queue, deadlocks until the wall-clock timeout fires and raises a
    spurious ``SandboxTimeout`` for a child that already finished its
    work correctly.
    """
    if record.state != "active":
        raise SandboxError(f"cannot run in a sandbox that is not active: {record.state!r}")
    ctx = multiprocessing.get_context("spawn")
    result_queue: "multiprocessing.Queue[tuple[bool, Any]]" = ctx.Queue()
    proc = ctx.Process(
        target=_child_entry,
        args=(
            func,
            args,
            kwargs,
            record.max_cpu_seconds,
            record.max_memory_bytes,
            record.max_output_bytes,
            result_queue,
        ),
    )
    proc.start()
    try:
        ok, payload = result_queue.get(timeout=record.max_wall_seconds)
    except _queue_module.Empty:
        if proc.is_alive():
            proc.kill()
        proc.join(_PROCESS_JOIN_GRACE_SECONDS)
        raise SandboxTimeout(
            f"sandbox worker exceeded {record.max_wall_seconds}s wall-clock limit"
        ) from None
    proc.join(_PROCESS_JOIN_GRACE_SECONDS)
    if proc.is_alive():
        proc.kill()
        proc.join()
    if not ok:
        raise SandboxError(f"sandbox worker raised: {payload}")
    return payload
