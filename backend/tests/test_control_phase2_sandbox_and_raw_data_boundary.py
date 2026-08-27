"""Phase 2A exit criteria: SandboxManager, per-run isolated workspace,
raw-data worker isolation (separate process, network-deny, resource limits,
credential stripping), and path containment
(docs/MASTER_ARCHITECTURE_V2.md #21 and #85, local reference doc, never
committed)."""
from __future__ import annotations

import os
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
