"""Phase 14 (scale and resilience): execution retry at multi-file scale.

``test_control_execution_idempotency.py`` proves Phase 9's idempotency
spine (``task_id``/``attempt_id``, ``ExecutionTask``/``ExecutionResult``)
against a single one-column, one-file manifest. This file exercises the
same spine at a genuinely larger, multi-file scale, and adds the one
scenario that single-file fixture cannot express at all: a retry after
Executor crashes partway through a *multi-file* batch (having already
durably written some files' exports before the crash), proving the
retry neither re-transforms the files that already succeeded nor
duplicates their exports, and that only the retry's own successful
attempt is ever recorded as the durable result.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from phi_core.agents.reasoning import Executor
from phi_core.control.records import VerifiedClassificationManifest
from phi_core.control.store import MemoryControlStore
from phi_core.control.testing import make_ctx
from phi_core.paths import DATA_DIR

_N_FILES = 10


def _manifest(run_id: str) -> VerifiedClassificationManifest:
    return VerifiedClassificationManifest(
        run_id=run_id, preview_review_id="", unresolved_items=0,
        status="verified_for_execution",
    )


def _uploaded_csv(content: str) -> str:
    """A dataset path ``PathPolicyValidator`` accepts (docs #52): under
    ``DATA_DIR``, matching ``test_control_execution_idempotency.py``'s
    own fixture convention."""
    path = DATA_DIR / "uploads" / f"{uuid4().hex}.csv"
    path.write_text(content, encoding="utf-8")
    return str(path)


def _many_files_and_decisions(n: int) -> tuple[list[dict], list[dict]]:
    files = []
    decisions = []
    for i in range(n):
        src = _uploaded_csv(f"name,age\nJane Doe {i},3{i}\n")
        file_id = f"f{i}"
        files.append({
            "file_id": file_id, "kind": "dataset", "stored_path": src,
            "subtype": "csv", "columns": ["name", "age"],
        })
        decisions.append({"file_id": file_id, "column": "name", "action": "drop"})
        decisions.append({"file_id": file_id, "column": "age", "action": "keep"})
    return files, decisions


# ---- multi-file idempotency at scale ---------------------------------------


@pytest.mark.asyncio
async def test_multi_file_execution_persists_one_task_and_result_covering_every_file() -> None:
    """Widens ``test_control_execution_idempotency.py``'s single-file
    ``test_first_attempt_persists_task_and_successful_result`` to
    ``_N_FILES`` files in one manifest: still exactly one
    ``ExecutionTask``/``ExecutionResult`` pair, and every file's export
    is present, not just the first or last."""
    store = MemoryControlStore()
    run_id = uuid4().hex
    ctx = make_ctx("Executor", run_id=run_id, store=store)
    manifest = _manifest(run_id)
    files, decisions = _many_files_and_decisions(_N_FILES)

    result = await Executor(ctx).run(files, decisions, manifest=manifest, store=store)

    assert set(result["exports"]) == {f["file_id"] for f in files}
    task_id = f"execution:{manifest.manifest_id}"
    tasks = await store.find_many("execution_tasks", {"manifest_id": manifest.manifest_id})
    assert len(tasks) == 1
    assert len(tasks[0]["decision_refs"]) == len(decisions)
    results = await store.find_many("execution_results", {"task_id": task_id})
    assert len(results) == 1
    assert results[0]["success"] is True


@pytest.mark.asyncio
async def test_retry_with_the_same_manifest_after_a_successful_multi_file_run_never_re_transforms_any_file(monkeypatch) -> None:
    """Widens ``test_control_execution_idempotency.py``'s single-file
    ``test_retry_with_same_manifest_never_re_runs_the_transform`` to
    ``_N_FILES`` files: a retry against a manifest that already
    succeeded must never re-enter ``_apply_decisions`` for *any* file,
    not just skip re-transforming a single one."""
    store = MemoryControlStore()
    run_id = uuid4().hex
    ctx = make_ctx("Executor", run_id=run_id, store=store)
    manifest = _manifest(run_id)
    files, decisions = _many_files_and_decisions(_N_FILES)

    first = await Executor(ctx).run(files, decisions, manifest=manifest, store=store)

    calls: list[int] = []
    real_apply = Executor._apply_decisions

    async def _counting_apply(self, *a, **kw):
        calls.append(1)
        return await real_apply(self, *a, **kw)

    monkeypatch.setattr(Executor, "_apply_decisions", _counting_apply)
    second = await Executor(ctx).run(files, decisions, manifest=manifest, store=store)

    assert calls == [], "a retry against an already-succeeded multi-file manifest must never re-enter the transform loop"
    assert second == first
    assert len(second["exports"]) == _N_FILES


# ---- retry after a genuine mid-batch crash ---------------------------------


@pytest.mark.asyncio
async def test_retry_after_a_crash_partway_through_a_multi_file_batch_recovers_without_duplicating_exports(monkeypatch) -> None:
    """A batch of dataset files plus one metadata file: the metadata
    file's redaction call is made to raise on the *first* attempt only
    (simulating a genuine mid-batch Executor crash after some dataset
    files already wrote real exports to disk), then succeed on retry.

    Per docs #53, the idempotency spine is keyed at the whole-manifest
    level (one ``ExecutionTask``/``ExecutionResult`` pair per attempt_id,
    not per file) -- a failed attempt records a failed
    ``ExecutionResult`` and does NOT short-circuit the next attempt
    (only a prior *successful* result does, per
    ``_prior_execution_result``). This proves the retry: (1) genuinely
    re-runs the full transform loop (since the first attempt never
    succeeded), (2) reaches every file including the ones that crashed
    on attempt 1, and (3) produces exactly one final export per file --
    never two artifacts for a file whose export was already written
    once during the failed first attempt.
    """
    store = MemoryControlStore()
    run_id = uuid4().hex
    ctx = make_ctx("Executor", run_id=run_id, store=store)
    manifest = _manifest(run_id)

    dataset_before = _uploaded_csv("name\nJane Doe\n")
    metadata_path = DATA_DIR / "uploads" / f"{uuid4().hex}.csv"
    metadata_path.write_text("variable,description\nname,participant name\n", encoding="utf-8")
    dataset_after = _uploaded_csv("name\nJohn Roe\n")

    files = [
        {"file_id": "f_before", "kind": "dataset", "stored_path": dataset_before,
         "subtype": "csv", "columns": ["name"]},
        {"file_id": "f_meta", "kind": "metadata", "stored_path": str(metadata_path), "subtype": "csv"},
        {"file_id": "f_after", "kind": "dataset", "stored_path": dataset_after,
         "subtype": "csv", "columns": ["name"]},
    ]
    decisions = [
        {"file_id": "f_before", "column": "name", "action": "drop"},
        {"file_id": "f_after", "column": "name", "action": "drop"},
    ]

    real_redact = Executor._redact_metadata_maybe_sandboxed
    attempt_count = {"n": 0}

    async def _crash_once_then_succeed(self, src, dst):
        attempt_count["n"] += 1
        if attempt_count["n"] == 1:
            raise RuntimeError("simulated executor crash mid-batch (metadata redaction)")
        return await real_redact(self, src, dst)

    monkeypatch.setattr(Executor, "_redact_metadata_maybe_sandboxed", _crash_once_then_succeed)

    # Attempt 1: crashes on the metadata file (the 2nd file in the
    # batch), after f_before's dataset export has already been written
    # to disk by the same _apply_decisions call.
    with pytest.raises(RuntimeError, match="simulated executor crash mid-batch"):
        await Executor(ctx).run(files, decisions, manifest=manifest, store=store)

    task_id = f"execution:{manifest.manifest_id}"
    results_after_crash = await store.find_many("execution_results", {"task_id": task_id})
    assert len(results_after_crash) == 1
    assert results_after_crash[0]["success"] is False
    assert results_after_crash[0]["failure_class"] == "RuntimeError"
    tasks_after_crash = await store.find_many("execution_tasks", {"manifest_id": manifest.manifest_id})
    assert len(tasks_after_crash) == 1
    assert tasks_after_crash[0]["state"] == "running"  # never transitioned by a failed attempt

    # Attempt 2 (the retry): the same manifest, the same store -- no
    # prior successful result exists yet, so this must genuinely re-run
    # the full transform, this time reaching every file.
    second = await Executor(ctx).run(files, decisions, manifest=manifest, store=store)

    assert set(second["exports"]) == {"f_before", "f_meta", "f_after"}
    assert attempt_count["n"] == 2  # exactly one crash, one recovery -- no extra silent retries

    results_after_retry = await store.find_many("execution_results", {"task_id": task_id})
    assert len(results_after_retry) == 2  # the failed attempt's record is kept, not overwritten
    assert [r["success"] for r in results_after_retry] == [False, True]
    tasks_after_retry = await store.find_many("execution_tasks", {"manifest_id": manifest.manifest_id})
    assert len(tasks_after_retry) == 2  # one ExecutionTask row per attempt, per the spine's own design

    # A THIRD call against the same manifest now finds the successful
    # result from attempt 2 and must not re-run at all -- proving the
    # post-recovery state is exactly as idempotent as a first-try success.
    real_apply = Executor._apply_decisions
    calls: list[int] = []

    async def _counting_apply(self, *a, **kw):
        calls.append(1)
        return await real_apply(self, *a, **kw)

    monkeypatch.setattr(Executor, "_apply_decisions", _counting_apply)
    third = await Executor(ctx).run(files, decisions, manifest=manifest, store=store)

    assert calls == []
    assert third == second
