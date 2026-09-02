"""Phase 9: the idempotency spine (docs #53) wired into Executor.run.

A retry that supplies the same ``manifest`` and a shared ``store`` must
never re-run the transformation loop once a prior attempt already
succeeded -- it returns the recorded result unchanged. A first attempt
persists both an ``ExecutionTask`` and a successful ``ExecutionResult``
keyed by the manifest's task_id; a failing attempt persists a failed
``ExecutionResult`` instead of silently dropping the record.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from phi_core.agents.reasoning import Executor
from phi_core.control.records import VerifiedClassificationManifest
from phi_core.control.store import MemoryControlStore
from phi_core.control.testing import make_ctx
from phi_core.paths import DATA_DIR


def _manifest(run_id: str) -> VerifiedClassificationManifest:
    return VerifiedClassificationManifest(
        run_id=run_id, preview_review_id="", unresolved_items=0,
        status="verified_for_execution",
    )


def _uploaded_csv(content: str) -> str:
    """A dataset path PathPolicyValidator accepts (docs #52): under
    DATA_DIR, not a bare pytest tmp_path, since these tests exercise the
    idempotency spine with a real manifest and therefore the real
    pre-execution validators too."""
    path = DATA_DIR / "uploads" / f"{uuid4().hex}.csv"
    path.write_text(content, encoding="utf-8")
    return str(path)


@pytest.mark.asyncio
async def test_first_attempt_persists_task_and_successful_result(stub_executor_dataset_codegen) -> None:
    store = MemoryControlStore()
    run_id = uuid4().hex
    ctx = make_ctx("Executor", run_id=run_id, store=store)
    manifest = _manifest(run_id)

    src = _uploaded_csv("name\nJane Doe\n")
    files = [{"file_id": "f1", "kind": "dataset", "stored_path": src, "subtype": "csv", "columns": ["name"]}]
    decisions = [{"file_id": "f1", "column": "name", "action": "drop"}]

    result = await Executor(ctx).run(files, decisions, manifest=manifest, store=store)

    assert "f1" in result["exports"]
    task_id = f"execution:{manifest.manifest_id}"
    tasks = await store.find_many("execution_tasks", {"manifest_id": manifest.manifest_id})
    assert len(tasks) == 1
    assert tasks[0]["state"] == "running"
    results = await store.find_many("execution_results", {"task_id": task_id})
    assert len(results) == 1
    assert results[0]["success"] is True


@pytest.mark.asyncio
async def test_retry_with_same_manifest_never_re_runs_the_transform(monkeypatch, stub_executor_dataset_codegen) -> None:
    store = MemoryControlStore()
    run_id = uuid4().hex
    ctx = make_ctx("Executor", run_id=run_id, store=store)
    manifest = _manifest(run_id)

    src = _uploaded_csv("name\nJane Doe\n")
    files = [{"file_id": "f1", "kind": "dataset", "stored_path": src, "subtype": "csv", "columns": ["name"]}]
    decisions = [{"file_id": "f1", "column": "name", "action": "drop"}]

    first = await Executor(ctx).run(files, decisions, manifest=manifest, store=store)

    calls: list[int] = []
    real_apply = Executor._apply_decisions

    async def _counting_apply(self, *a, **kw):
        calls.append(1)
        return await real_apply(self, *a, **kw)

    monkeypatch.setattr(Executor, "_apply_decisions", _counting_apply)

    second = await Executor(ctx).run(files, decisions, manifest=manifest, store=store)

    assert calls == [], "a retry against the same manifest must never re-enter the transform loop"
    assert second == first


@pytest.mark.asyncio
async def test_pending_human_review_raises_before_any_spine_record_is_written() -> None:
    store = MemoryControlStore()
    run_id = uuid4().hex
    ctx = make_ctx("Executor", run_id=run_id, store=store)
    manifest = _manifest(run_id)

    files = [{"file_id": "f1", "kind": "dataset", "stored_path": "/no/such/file.csv",
              "subtype": "csv", "columns": ["name"]}]
    decisions = [{"file_id": "f1", "column": "name", "action": "human_review"}]

    with pytest.raises(ValueError):
        await Executor(ctx).run(files, decisions, manifest=manifest, store=store)

    task_id = f"execution:{manifest.manifest_id}"
    assert await store.find_many("execution_results", {"task_id": task_id}) == []


@pytest.mark.asyncio
async def test_transform_failure_is_recorded_as_a_failed_execution_result() -> None:
    store = MemoryControlStore()
    run_id = uuid4().hex
    ctx = make_ctx("Executor", run_id=run_id, store=store)
    manifest = _manifest(run_id)

    async def _boom(self, files, decisions, omit_by_file):
        raise RuntimeError("disk exploded")

    executor = Executor(ctx)
    executor._apply_decisions = _boom.__get__(executor, Executor)

    with pytest.raises(RuntimeError, match="disk exploded"):
        await executor.run([], [], manifest=manifest, store=store)

    task_id = f"execution:{manifest.manifest_id}"
    results = await store.find_many("execution_results", {"task_id": task_id})
    assert len(results) == 1
    assert results[0]["success"] is False
    assert results[0]["failure_class"] == "RuntimeError"
