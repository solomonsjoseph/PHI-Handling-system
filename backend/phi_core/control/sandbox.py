"""SandboxManager: per-run isolated raw-processing workspace and worker
isolation (docs/MASTER_ARCHITECTURE_V2.md #21 and #85 Phase 2A, local
reference doc, never committed).

Genuine gap this closes: today ``Executor`` (``agents/reasoning.py``) reads
raw dataset rows as a plain in-process ``async`` coroutine, in the same
process, event loop, and class hierarchy as every LLM-calling agent, with no
network-deny, no resource ceiling, and no dedicated workspace of its own.
This module is wired into ``agents/`` as of Wave R-c step 4:
``Executor`` (``agents/reasoning.py``) routes its three remaining
raw-row-work call sites (``_redact_metadata_file``, ``_read_dataset_
headers``, ``read_narrative``) through ``run_isolated`` when
``ctx.sandbox`` is attached (``ActivationFactory.activate(...,
needs_sandbox=True)``). Contexts built via ``control.testing.make_ctx``
(every pre-existing unit test) leave ``ctx.sandbox`` unset and keep
calling those three functions in-process, a documented permanent
compatibility path, not a retiring debt. Dataset row TRANSFORMATION
(rewrite plan Task 11) no longer routes through this multiprocessing
sandbox at all: Executor's ``generate_with_retry``/``run_generated``
(``agents/codegen.py``) run generated code inside the far more isolated
``control/runner.py`` Docker container instead, and this same
``SandboxRecord`` still gates that path's own two data-touching checks
(``assert_no_dataset_literals``, ``assert_no_formula_injection_in_
outputs``). ``apply_column_actions_to_dataset``, the fixed per-cell
action table Executor used to drive here, moved to
``control/transform_primitives.py`` as a reference-only implementation
(``DeterministicVerifier``'s recompute oracle, the corpus-replay
harness) with no live Executor call site left at all.

``create_sandbox`` allocates a per-run directory under
``phi_core.paths.SANDBOX_DIR`` (mode 0700) and a :class:`SandboxRecord`.
It fails closed with :class:`SandboxError` if this platform cannot
actually enforce ``RLIMIT_AS`` at the configured ceiling (Darwin/XNU,
CPython issue 78783), unless ``PHI_SANDBOX_ALLOW_UNENFORCED_MEMORY=1``.
``run_isolated`` executes a callable inside that sandbox in a *separate*
process (``multiprocessing`` ``spawn`` context, not the caller's event
loop/process): the child's environment is rebuilt from a fixed allowlist
(never a credential denylist) before running, with a runner-controlled
``PATH`` and no ``HOME`` at all; CPU/memory/output-size
``resource.setrlimit`` ceilings are applied; outbound sockets are denied
by monkeypatching ``socket.socket``; and the run is wall-clock bounded by
draining the result queue with a timeout before joining the process
(SIGKILL if still alive).

Rewrite plan step 4: the child's return value is no longer merely
type-checked. Every call declares a mandatory ``return_kind`` --
``"path"`` (a workspace-relative artifact the worker itself wrote,
re-validated to actually exist), ``"count"`` (a plain ``int``),
``"status"`` (a short, closed-shape lowercase token), or ``"json"`` (a
size-capped, ``json.loads``-parseable string for the handful of
first-party helpers that already return a small structured summary --
never a raw dataset row) -- and the contract is enforced *twice*: once
inside the child before ``queue.put`` (fail fast, cheap), and again in
the parent after ``queue.get`` (defense in depth against a corrupted or
maliciously crafted queue payload, and the same check step 5's
container-based runner reuses against a far less trusted boundary). A
worker returning ``df.to_csv()`` -- the concrete leak this closes -- fails
every one of the four kinds: it is not a valid JSON document, not a
resolvable existing workspace file, not an ``int``, and not a short
status token.

Any exception the wrapped worker raises (or that this module's own
contract validation raises) is *never* forwarded to the caller verbatim.
The child forwards only the exception's type name plus a
``scrub_persisted_text``-scrubbed, length-capped summary; this module's
own contract-violation messages are, by construction, always a fixed
string that never embeds the offending payload itself (a rejection
message is exactly the kind of place the same leak could otherwise slip
out a second time). The parent reconstructs a structured
:class:`SandboxWorkerFailure` carrying ``.diagnostic = {"kind", "code",
"detail_ref"}`` -- ``code`` is one of ``missing_output``, ``wrong_schema``,
``timeout``, ``path_violation``, ``import_denied``, ``runtime_error`` --
whose ``str()`` form is that same short structured summary, never the
free text. The free text lives behind ``detail_ref`` in this module's
bounded in-process detail store, reachable only via
``get_sandbox_error_detail``: an explicit escape hatch this module does
not itself authorize. Nothing in this codebase calls it yet; any future
caller wiring it into a human-review, trace, or report surface owns that
gating decision itself, the same way ``GET /api/sessions/{sid}/human-
review/source/{file_id}`` gates raw source inspection at the API layer,
not here.

``destroy_sandbox`` removes the workspace tree and flips
``SandboxRecord.state``/``CleanupManifest.sandbox_destroyed``.

# ponytail: network-deny is enforced by monkeypatching ``socket.socket``
# inside the spawned child, not an OS-level netns/firewall rule. This
# blocks whatever bytecode ``func`` executes in-process but not a
# raw syscall from a C extension that bypasses the ``socket`` module, and
# does not survive `os.fork()`-after-patch inside `func` itself. Upgrade
# path: network namespace / seccomp / container runtime if the threat
# model needs to defend against untrusted native code, not just Python
# (rewrite plan step 5: ``control/runner.py``'s ``ContainerRunner``, for
# model-generated code specifically -- this module's multiprocessing path
# stays the boundary for trusted first-party raw-data helpers).
"""
from __future__ import annotations

import json
import multiprocessing
import os
import queue as _queue_module
import re
import resource
import shutil
import socket
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Literal
from uuid import uuid4

from phi_core.paths import SANDBOX_DIR, is_safe_scoped_id
from phi_core.security import scrub_persisted_text

from . import limits
from .records import CleanupManifest, SandboxRecord

# D1/D7 (kept), Wave R-c step 4 (narrowed further): an explicit allowlist
# of environment variables the raw-data worker is permitted to inherit.
# Replaces a substring denylist ("API_KEY", "SECRET", "TOKEN",
# "PASSWORD", "CREDENTIAL", "MONGO_URL") that missed APP_ENCRYPTION_KEY
# and ATTESTATION_SIGNING_KEY (D7) and, separately, was applied after
# os.environ had already been cleared to {} so it never actually ran
# against anything (D1). An allowlist cannot miss a credential shape the
# way a denylist can: anything not named here never reaches the child,
# full stop. `PATH` and `HOME` are deliberately absent: both are
# unpredictable, deployment-specific, and wider than anything a raw-data
# worker needs (a developer's PATH can carry arbitrary tool directories;
# HOME gates config/cache lookups no sandboxed helper here relies on).
# `_child_entry` sets `PATH` explicitly instead, to a fixed value this
# module controls, and leaves `HOME` unset entirely.
_ALLOWLISTED_ENV_KEYS = ("TMPDIR", "LANG", "LC_ALL", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE")

# A fixed, runner-controlled PATH for the sandboxed child -- never the
# parent process's own (arbitrarily wide, deployment-specific) PATH.
_CHILD_CONTROLLED_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

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


class SandboxPathViolation(SandboxError):
    """Raised when a path is not contained within a sandbox's workspace."""


class SandboxReturnContractViolation(SandboxError):
    """Raised when a sandboxed worker's return value does not conform to
    its declared ``return_kind`` (see ``_validate_return_contract``)."""


class SandboxMissingOutputError(SandboxReturnContractViolation):
    """Raised when a ``return_kind="path"`` worker's declared path
    resolves inside the workspace but the file does not actually exist --
    the worker claimed to have written an artifact it never produced."""


# --- structured worker-failure diagnostics (rewrite plan step 4) ---------
#
# The free-text detail behind every SandboxWorkerFailure/SandboxTimeout
# lives here, keyed by an opaque ref, never inline in the exception's own
# `str()`. Bounded FIFO eviction: this is per-process diagnostic scratch
# for the current server lifetime, not a durable audit record.
_ERROR_DETAIL_MAX_ENTRIES = 500
_error_details: "OrderedDict[str, str]" = OrderedDict()


def _store_error_detail(detail: str) -> str:
    ref = uuid4().hex
    _error_details[ref] = detail
    while len(_error_details) > _ERROR_DETAIL_MAX_ENTRIES:
        _error_details.popitem(last=False)
    return ref


def get_sandbox_error_detail(detail_ref: str) -> str | None:
    """Access the free-text detail behind a structured sandbox failure's
    ``detail_ref``. This module performs no authorization of its own --
    see the module docstring. Never wire this into an LLM prompt, a trace
    payload, or a generated report without an explicit access-control
    decision at the call site."""
    return _error_details.get(detail_ref)


_ERROR_CODE_BY_EXCEPTION_NAME: dict[str, str] = {
    "SandboxTimeout": "timeout",
    "SandboxPathViolation": "path_violation",
    "SandboxMissingOutputError": "missing_output",
    "SandboxReturnContractViolation": "wrong_schema",
    "ImportError": "import_denied",
    "ModuleNotFoundError": "import_denied",
}


def _classify_error_code(exception_type_name: str) -> str:
    return _ERROR_CODE_BY_EXCEPTION_NAME.get(exception_type_name, "runtime_error")


class SandboxWorkerFailure(SandboxError):
    """A structured, PHI-safe diagnostic for a sandboxed worker failure.

    ``str(exc)`` is always the short structural summary
    ``f"{code}[{kind}] ref={detail_ref}"`` -- it never embeds the
    worker's original free-text message. Programmatic callers read
    ``.diagnostic`` (``{"kind", "code", "detail_ref"}``) instead of
    parsing the string.
    """

    def __init__(self, *, kind: str, code: str, detail: str) -> None:
        self.kind = kind
        self.code = code
        self.detail_ref = _store_error_detail(detail)
        self.diagnostic = {"kind": kind, "code": code, "detail_ref": self.detail_ref}
        super().__init__(f"{code}[{kind}] ref={self.detail_ref}")


class SandboxTimeout(SandboxWorkerFailure):
    """Raised when the isolated worker exceeds ``max_wall_seconds``."""

    def __init__(self, max_wall_seconds: int) -> None:
        super().__init__(
            kind="SandboxTimeout",
            code="timeout",
            detail=f"sandbox worker exceeded {max_wall_seconds}s wall-clock limit",
        )


# Our own exception types raised inside `_validate_return_contract` (see
# below) are, by construction, always built from a fixed string that
# never embeds the candidate payload -- safe to reconstruct exactly in
# the parent by name+message rather than folding into the generic
# SandboxWorkerFailure/detail-store indirection every *other* (arbitrary,
# potentially content-bearing) worker exception goes through.
_OWN_EXCEPTION_TYPES: dict[str, type[SandboxError]] = {
    "SandboxReturnContractViolation": SandboxReturnContractViolation,
    "SandboxPathViolation": SandboxPathViolation,
    "SandboxMissingOutputError": SandboxMissingOutputError,
}


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
# SSE, so only a scrubbed, length-capped summary ever leaves the child --
# and even that summary now lives behind a detail_ref, never inline in
# any exception a caller might log or forward verbatim (see module
# docstring).
_MAX_FORWARDED_ERROR_CHARS = 2000

# --- Wave R-c step 4: the enforced return_kind contract -------------------
#
# A sandboxed worker may only hand back one of four kinds of value, never
# an arbitrary object: a workspace-relative artifact PATH the worker
# itself wrote (re-validated to exist), a plain COUNT, a short closed-
# shape STATUS token, or a size-capped JSON string for the handful of
# first-party helpers that already return a small structured summary
# (never a raw dataset row -- see each `_sandboxed_*` wrapper in
# `agents/reasoning.py` / `control/deterministic_verifier.py`). An
# arbitrary/unstructured payload crossing back into the parent process
# (the same process that runs every LLM agent) would relocate the raw-
# data read into the parent without creating a real boundary; callers
# that need to hand back real content either write it to a workspace
# artifact and return its path, or return a small, already-vetted JSON
# summary that itself never carries a raw cell value.
ReturnKind = Literal["path", "count", "status", "json"]
_RETURN_KINDS: frozenset[str] = frozenset({"path", "count", "status", "json"})

# A "status" is a short, identifier-shaped token -- never free text.
# This is the general-purpose equivalent of "membership in a fixed enum"
# for a low-level primitive whose many call sites each have their own
# small, distinct vocabulary: the shape constraint (not one shared global
# business vocabulary) is what closes the "str with no length bound"
# gap for this kind, uniformly, across every caller.
_STATUS_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

_MAX_PATH_RETURN_CHARS = 512
# 128 KiB: comfortably covers the largest first-party JSON summary this
# codebase produces today (a full-study classification/verdict list, or
# a multi-page instrument's extracted narrative text, wrapped as a JSON
# string) while remaining far short of what an actual dataset row-dump
# would produce for any study with enough rows to matter -- the boundary
# this cap exists to make conspicuous, not the only layer that catches it
# (scrub_persisted_text runs against every "status"/"json" payload too;
# `assert_no_dataset_literals`, step 9, and the container boundary, step
# 5, are the layers that reason about content rather than shape/size).
_MAX_JSON_RETURN_CHARS = 131072


def _validate_return_contract(payload: Any, return_kind: str, record: SandboxRecord) -> None:
    """Enforce the declared ``return_kind`` contract. Called both inside
    the child (before ``queue.put``, so a violation never even reaches
    the queue) and again in the parent (after ``queue.get``, defense in
    depth against a corrupted or crafted queue payload -- ``run_isolated``
    re-runs this exact check on whatever the child claimed to hand back).

    Every raised message here is a fixed, static string that never
    embeds the candidate payload itself: a payload violating this
    contract is exactly the shape of thing (an oversized/arbitrary
    string, e.g. ``df.to_csv()``) this function exists to keep out of
    every downstream log, trace, and report, so the rejection message
    must not become a second way for the same content to leak.
    """
    if return_kind == "count":
        if type(payload) is not int:
            raise SandboxReturnContractViolation(
                "sandbox worker declared return_kind='count' but did not "
                f"return a plain int (got {type(payload).__name__})"
            )
        return
    if return_kind == "status":
        if not isinstance(payload, str) or not _STATUS_TOKEN_RE.match(payload):
            raise SandboxReturnContractViolation(
                "sandbox worker declared return_kind='status' but did not "
                "return a short lowercase status token"
            )
        return
    if return_kind == "json":
        if not isinstance(payload, str) or len(payload) > _MAX_JSON_RETURN_CHARS:
            raise SandboxReturnContractViolation(
                "sandbox worker declared return_kind='json' but returned a "
                f"non-string or oversized (> {_MAX_JSON_RETURN_CHARS} chars) value"
            )
        try:
            json.loads(payload)
        except ValueError:
            raise SandboxReturnContractViolation(
                "sandbox worker declared return_kind='json' but returned a "
                "value that is not valid JSON"
            ) from None
        return
    if return_kind == "path":
        if not isinstance(payload, str) or len(payload) > _MAX_PATH_RETURN_CHARS:
            raise SandboxReturnContractViolation(
                "sandbox worker declared return_kind='path' but returned a "
                f"non-string or oversized (> {_MAX_PATH_RETURN_CHARS} chars) value"
            )
        try:
            resolved = validate_sandbox_path(record, payload)
        except SandboxPathViolation:
            raise SandboxPathViolation(
                "sandbox worker declared return_kind='path' but the "
                "returned value does not resolve inside the sandbox workspace"
            ) from None
        if not resolved.is_file():
            raise SandboxMissingOutputError(
                "sandbox worker declared return_kind='path' but the "
                "resolved artifact does not exist"
            )
        return
    raise SandboxReturnContractViolation(f"unknown return_kind: {return_kind!r}")


def _child_entry(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    record: SandboxRecord,
    return_kind: str,
    queue: "multiprocessing.Queue[tuple[bool, Any]]",
) -> None:
    allowlisted_env = _allowlisted_env()
    os.environ.clear()
    os.environ.update(allowlisted_env)
    os.environ["PATH"] = _CHILD_CONTROLLED_PATH
    resource.setrlimit(resource.RLIMIT_CPU, (record.max_cpu_seconds, record.max_cpu_seconds))
    resource.setrlimit(resource.RLIMIT_FSIZE, (record.max_output_bytes, record.max_output_bytes))
    if _MEMORY_LIMIT_ENFORCEABLE:
        resource.setrlimit(resource.RLIMIT_AS, (record.max_memory_bytes, record.max_memory_bytes))
    _deny_sockets()
    try:
        result = func(*args, **kwargs)
        _validate_return_contract(result, return_kind, record)
        if return_kind in ("status", "json") and isinstance(result, str):
            # Second line of defence (rewrite plan step 4): even a
            # payload that already satisfies its return_kind's structural
            # contract gets scrubbed for known PHI shapes before it
            # leaves the child. Skipped for "path": a validated path is,
            # by construction, a short filesystem-safe string resolving
            # to a real file the worker itself just wrote, and scrubbing
            # it could corrupt the exact bytes the parent must resolve.
            result = scrub_persisted_text(result)
        queue.put((True, result))
    except BaseException as exc:
        message = scrub_persisted_text(str(exc))[:_MAX_FORWARDED_ERROR_CHARS]
        queue.put((False, {"type": type(exc).__name__, "message": message}))


_PROCESS_JOIN_GRACE_SECONDS = 5


def run_isolated(
    record: SandboxRecord,
    func: Callable[..., Any],
    *args: Any,
    return_kind: ReturnKind,
    **kwargs: Any,
) -> Any:
    """Run ``func(*args, **kwargs)`` inside a separate process bounded by
    ``record``'s CPU/memory/wall-clock ceilings, with network access denied
    and provider-credential env vars stripped. Raises :class:`SandboxTimeout`
    on wall-clock exceedance and :class:`SandboxError` on any other failure.

    ``return_kind`` is mandatory and keyword-only: every call site must
    declare, up front, what shape of value it expects back -- see the
    module docstring and ``_validate_return_contract``.

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
    if return_kind not in _RETURN_KINDS:
        raise SandboxError(f"unknown return_kind: {return_kind!r}")
    ctx = multiprocessing.get_context("spawn")
    result_queue: "multiprocessing.Queue[tuple[bool, Any]]" = ctx.Queue()
    proc = ctx.Process(
        target=_child_entry,
        args=(func, args, kwargs, record, return_kind, result_queue),
    )
    proc.start()
    try:
        ok, payload = result_queue.get(timeout=record.max_wall_seconds)
    except _queue_module.Empty:
        if proc.is_alive():
            proc.kill()
        proc.join(_PROCESS_JOIN_GRACE_SECONDS)
        raise SandboxTimeout(record.max_wall_seconds) from None
    proc.join(_PROCESS_JOIN_GRACE_SECONDS)
    if proc.is_alive():
        proc.kill()
        proc.join()
    if not ok:
        exc_type_name = payload["type"]
        message = payload["message"]
        if exc_type_name in _OWN_EXCEPTION_TYPES:
            raise _OWN_EXCEPTION_TYPES[exc_type_name](message)
        raise SandboxWorkerFailure(
            kind=exc_type_name, code=_classify_error_code(exc_type_name), detail=message,
        )
    # Parent-side re-validation: never trust a queue payload just because
    # the child claims it already passed (see module docstring).
    _validate_return_contract(payload, return_kind, record)
    return payload
