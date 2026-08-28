"""Phase 2A exit criteria: SandboxManager, per-run isolated workspace,
raw-data worker isolation (separate process, network-deny, resource limits,
credential stripping), and path containment
(docs/MASTER_ARCHITECTURE_V2.md #21 and #85, local reference doc, never
committed)."""
from __future__ import annotations

import json
import os
import resource
import tempfile
import time
from pathlib import Path

import pytest
from phi_core.control.records import CleanupManifest, SandboxRecord
from phi_core.control.sandbox import (
    SandboxError,
    SandboxPathViolation,
    SandboxTimeout,
    create_sandbox,
    destroy_sandbox,
    run_isolated,
    validate_sandbox_path,
)
from phi_core.paths import SANDBOX_DIR

# This whole file exercises "normal" sandbox behavior on a platform
# (Darwin) where RLIMIT_AS cannot be set to any finite value (CPython issue
# 78783, open, documented XNU limitation), so `create_sandbox` fails closed
# by default (see `sandbox.py::_MEMORY_LIMIT_ENFORCEABLE`). This autouse
# fixture supplies the explicit opt-in for every test except the one that
# specifically proves the fail-closed default, which is deliberately left
# untouched so that test needs no monkeypatch.delenv: conftest.py sets no
# such variable, so leaving it alone already reproduces the true default.
_FAIL_CLOSED_TEST_NAME = "test_create_sandbox_fails_closed_when_memory_limit_is_unenforceable_without_override"


@pytest.fixture(autouse=True)
def _allow_unenforced_sandbox_memory_by_default(request, monkeypatch):
    if request.node.name != _FAIL_CLOSED_TEST_NAME:
        monkeypatch.setenv("PHI_SANDBOX_ALLOW_UNENFORCED_MEMORY", "1")


def _run_id() -> str:
    import uuid

    return uuid.uuid4().hex


def test_create_sandbox_allocates_isolated_workspace():
    run_id = _run_id()
    record = create_sandbox(run_id)
    try:
        workspace = Path(record.workspace_path)
        assert workspace.is_dir()
        assert workspace.is_relative_to(SANDBOX_DIR)
        assert oct(workspace.stat().st_mode)[-3:] == "700"
        assert record.state == "active"
        assert record.network_denied is True
    finally:
        destroy_sandbox(record)


def test_create_sandbox_rejects_unsafe_run_id():
    with pytest.raises(SandboxError):
        create_sandbox("../../etc")


def test_two_sandboxes_for_same_run_do_not_collide():
    run_id = _run_id()
    first = create_sandbox(run_id)
    second = create_sandbox(run_id)
    try:
        assert first.workspace_path != second.workspace_path
        assert Path(first.workspace_path).is_dir()
        assert Path(second.workspace_path).is_dir()
    finally:
        destroy_sandbox(first)
        destroy_sandbox(second)


def test_destroy_sandbox_removes_workspace_and_marks_manifest():
    record = create_sandbox(_run_id())
    manifest = CleanupManifest(run_id=record.run_id)
    workspace = Path(record.workspace_path)
    assert workspace.exists()

    updated, updated_manifest = destroy_sandbox(record, manifest)

    assert not workspace.exists()
    assert updated.state == "destroyed"
    assert updated.destroyed_at
    assert updated_manifest.sandbox_destroyed is True


def test_destroy_sandbox_is_idempotent():
    record = create_sandbox(_run_id())
    first, _ = destroy_sandbox(record)
    second, _ = destroy_sandbox(first)
    assert second.state == "destroyed"


def test_validate_sandbox_path_rejects_traversal():
    record = create_sandbox(_run_id())
    try:
        inside = validate_sandbox_path(record, "sub/file.csv")
        assert Path(record.workspace_path) in inside.parents

        with pytest.raises(SandboxPathViolation):
            validate_sandbox_path(record, "../../../etc/passwd")
        with pytest.raises(SandboxPathViolation):
            validate_sandbox_path(record, "/etc/passwd")
    finally:
        destroy_sandbox(record)


def _add(a: int, b: int) -> int:
    return a + b


def _spin_forever() -> None:
    while True:
        pass


def _read_secret_env() -> str:
    return os.environ.get("ANTHROPIC_API_KEY", "")


def _attempt_network() -> str:
    import socket as _socket

    try:
        _socket.socket()
        return "socket-created"
    except OSError as exc:
        return f"denied: {exc}"


def test_run_isolated_executes_in_separate_process_and_returns_result():
    record = create_sandbox(_run_id())
    try:
        assert run_isolated(record, _add, 2, 3) == 5
    finally:
        destroy_sandbox(record)


def test_run_isolated_strips_provider_credentials_from_worker_env():
    os.environ["ANTHROPIC_API_KEY"] = "sk-should-not-reach-worker"
    try:
        record = create_sandbox(_run_id())
        try:
            assert run_isolated(record, _read_secret_env) == ""
        finally:
            destroy_sandbox(record)
    finally:
        del os.environ["ANTHROPIC_API_KEY"]


def _read_env_snapshot() -> str:
    return json.dumps({
        "APP_ENCRYPTION_KEY": os.environ.get("APP_ENCRYPTION_KEY"),
        "ATTESTATION_SIGNING_KEY": os.environ.get("ATTESTATION_SIGNING_KEY"),
        "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY"),
        "PATH": os.environ.get("PATH"),
        "HOME": os.environ.get("HOME"),
        "TMPDIR": os.environ.get("TMPDIR"),
    })


def test_run_isolated_allowlists_env_and_strips_all_credential_shapes():
    """D1/D7: the child's environment must be built from an explicit
    allowlist, not a substring denylist (which missed APP_ENCRYPTION_KEY
    and ATTESTATION_SIGNING_KEY -- D7) applied after the environment was
    already wiped to {} (D1), which also silently dropped PATH/HOME/TMPDIR
    the worker may need."""
    os.environ["APP_ENCRYPTION_KEY"] = "should-not-reach-worker"
    os.environ["ATTESTATION_SIGNING_KEY"] = "should-not-reach-worker"
    os.environ["ANTHROPIC_API_KEY"] = "sk-should-not-reach-worker"
    os.environ.setdefault("TMPDIR", tempfile.gettempdir())
    try:
        record = create_sandbox(_run_id())
        try:
            raw = run_isolated(record, _read_env_snapshot)
        finally:
            destroy_sandbox(record)
    finally:
        del os.environ["APP_ENCRYPTION_KEY"]
        del os.environ["ATTESTATION_SIGNING_KEY"]
        del os.environ["ANTHROPIC_API_KEY"]
    snapshot = json.loads(raw)
    assert snapshot["APP_ENCRYPTION_KEY"] is None
    assert snapshot["ATTESTATION_SIGNING_KEY"] is None
    assert snapshot["ANTHROPIC_API_KEY"] is None
    assert snapshot["PATH"] is not None
    assert snapshot["HOME"] is not None
    assert snapshot["TMPDIR"] is not None


def test_run_isolated_denies_network_by_default():
    record = create_sandbox(_run_id())
    try:
        result = run_isolated(record, _attempt_network)
        assert result.startswith("denied:")
    finally:
        destroy_sandbox(record)


def test_run_isolated_enforces_wall_clock_timeout():
    record = create_sandbox(_run_id(), max_wall_seconds=2)
    try:
        start = time.monotonic()
        with pytest.raises(SandboxTimeout):
            run_isolated(record, _spin_forever)
        elapsed = time.monotonic() - start
        assert elapsed < 10  # killed promptly, not left running
    finally:
        destroy_sandbox(record)


def test_run_isolated_refuses_on_non_active_sandbox():
    record = create_sandbox(_run_id())
    destroyed, _ = destroy_sandbox(record)
    with pytest.raises(SandboxError):
        run_isolated(destroyed, _add, 1, 1)


def _memory_limit_enforceable_probe() -> bool:
    """Duplicates sandbox.py's own platform probe so this test's RED/GREEN
    reflects create_sandbox's actual raise behavior, not whether an
    internal attribute happens to exist yet. Empirically (this exact
    machine, Darwin 25.6.0), setrlimit(RLIMIT_AS, ...) does NOT reject
    every finite value -- an arbitrarily huge one (e.g. 1 TiB) succeeds.
    What actually fails is any value below the process's already-mapped
    virtual address space, which the real configured ceiling
    (limits.MAX_SANDBOX_MEMORY_BYTES, 1 GiB by default) falls under on
    this machine. Probing with that real ceiling -- not an arbitrarily
    huge sentinel -- is what answers the question that matters ("can this
    platform enforce the memory bound it actually configures"). Only the
    soft limit is touched: lowering the hard limit is a one-way ratchet
    without elevated privilege, so touching it here would make the
    finally-restore itself capable of failing.
    """
    from phi_core.control import limits as limits_module

    original_soft, original_hard = resource.getrlimit(resource.RLIMIT_AS)
    probe_soft = (
        limits_module.MAX_SANDBOX_MEMORY_BYTES
        if original_hard == resource.RLIM_INFINITY
        else min(limits_module.MAX_SANDBOX_MEMORY_BYTES, original_hard)
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


def test_create_sandbox_fails_closed_when_memory_limit_is_unenforceable_without_override():
    """On a platform where RLIMIT_AS cannot be set to a finite value
    (Darwin), and with no PHI_SANDBOX_ALLOW_UNENFORCED_MEMORY override set,
    create_sandbox must refuse rather than silently hand back a sandbox
    with no real memory ceiling."""
    if _memory_limit_enforceable_probe():
        pytest.skip("this platform can enforce RLIMIT_AS; fail-closed branch is not exercised")
    with pytest.raises(SandboxError):
        create_sandbox(_run_id())


def _read_fsize_soft_limit() -> int:
    return resource.getrlimit(resource.RLIMIT_FSIZE)[0]


def test_run_isolated_bounds_output_size_via_rlimit_fsize():
    record = create_sandbox(_run_id())
    try:
        soft_limit = run_isolated(record, _read_fsize_soft_limit)
        assert soft_limit == record.max_output_bytes
        assert record.max_output_bytes > 0
    finally:
        destroy_sandbox(record)


_LARGE_PAYLOAD_BYTES = 200 * 1024  # comfortably above the ~64 KiB pipe buffer


def _return_large_payload() -> str:
    return "x" * _LARGE_PAYLOAD_BYTES


def test_run_isolated_round_trips_large_payload_without_deadlock():
    """D2: proc.join() must not precede queue.get(). A child that puts a
    result larger than the OS pipe buffer onto the queue will not
    terminate until that result is drained (documented CPython
    multiprocessing behavior: 'a process that has put items on a queue
    will wait before terminating until all the buffered items are fed by
    the feeder thread to the underlying pipe'). Joining first therefore
    deadlocks until the wall-clock timeout, raising a spurious
    SandboxTimeout instead of returning the real payload."""
    record = create_sandbox(_run_id(), max_wall_seconds=8)
    try:
        result = run_isolated(record, _return_large_payload)
        assert result == "x" * _LARGE_PAYLOAD_BYTES
    finally:
        destroy_sandbox(record)


def _raise_with_phi_shaped_message():
    raise ValueError("invalid cell value 'Jane Doe' (SSN 111-22-3333) in column 'patient_name'")


def _raise_with_huge_message():
    raise ValueError("x" * 50000)


def test_run_isolated_scrubs_exception_text_before_forwarding_to_parent():
    """D3 (child half): the child must forward only the exception's type
    name plus scrub_persisted_text(str(exc)), never the raw exception
    text -- a raw exception can embed the offending cell/column content,
    which then gets chained and streamed over SSE."""
    record = create_sandbox(_run_id())
    try:
        with pytest.raises(SandboxError) as excinfo:
            run_isolated(record, _raise_with_phi_shaped_message)
    finally:
        destroy_sandbox(record)
    message = str(excinfo.value)
    assert "Jane Doe" not in message
    assert "111-22-3333" not in message
    assert "ValueError" in message


def test_run_isolated_caps_forwarded_exception_text_length():
    record = create_sandbox(_run_id())
    try:
        with pytest.raises(SandboxError) as excinfo:
            run_isolated(record, _raise_with_huge_message)
    finally:
        destroy_sandbox(record)
    assert len(str(excinfo.value)) < 5000


def _return_raw_row_payload():
    return [{"patient_name": "Jane Doe", "mrn": "MRN1234567"}]


def test_run_isolated_rejects_non_conforming_return_payload():
    """A child worker's return value must be a path/count/status (str,
    int, float, bool, or None) only -- never an arbitrary object. An
    arbitrary payload crossing back into the parent process (the same
    process that runs every LLM agent) would relocate the raw-data read
    into the parent without creating a real boundary; the caller must
    write real row data to a workspace artifact and hand back its path
    instead."""
    record = create_sandbox(_run_id())
    try:
        with pytest.raises(SandboxError):
            run_isolated(record, _return_raw_row_payload)
    finally:
        destroy_sandbox(record)
