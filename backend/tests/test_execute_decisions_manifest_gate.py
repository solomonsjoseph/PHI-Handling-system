"""Phase 9 item 7: the manifest-freeze gate's invariant, exercised at the
real call site (``orchestrator.py::execute_decisions``), not just at the
``control/manifest.py`` unit level. A run whose frozen manifest has
already been flipped to ``invalidated`` (R-b's lineage invalidation)
must be refused before Executor ever runs -- not merely refused by
``ensure_frozen_manifest`` in isolation.
"""
from __future__ import annotations

import time

import pytest
from phi_core.agents import orchestrator
from phi_core.control.artifacts import MANIFEST_COLLECTION
from phi_core.control.manifest import manifest_artifact_id
from phi_core.control.records import VerifiedClassificationManifest
from phi_core.control.store import MemoryControlStore
from phi_core.control.testing import start_test_run


class _FakeSessions:
    def __init__(self):
        self.updates: list[tuple] = []

    async def find_one(self, *_a, **_kw):
        return None

    async def update_one(self, *args, **_kwargs):
        self.updates.append(args)


class _FakeDb:
    def __init__(self):
        self.sessions = _FakeSessions()


class _FakeManager:
    def __init__(self):
        self.logged: list[tuple] = []

    async def _log(self, *args, **_kwargs):
        self.logged.append(args)

    async def close_run(self, outcome):
        return {"outcome": outcome}


async def _seed_invalidated_manifest(store: MemoryControlStore, run_id: str) -> VerifiedClassificationManifest:
    artifact_id = manifest_artifact_id(run_id)
    manifest = VerifiedClassificationManifest(
        run_id=run_id, preview_review_id="", unresolved_items=0, status="invalidated",
    )
    document = manifest.model_dump(mode="python")
    document["artifact_id"] = artifact_id
    await store.insert(MANIFEST_COLLECTION, document)
    return manifest


@pytest.mark.asyncio
async def test_execute_decisions_refuses_execution_from_an_invalidated_manifest(monkeypatch) -> None:
    store = MemoryControlStore()
    run = await start_test_run(store, "s" * 32)
    run_id = run.run_id

    invalidated = await _seed_invalidated_manifest(store, run_id)

    executor_calls: list[int] = []

    class FakeExecutor:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx

        async def run(self, *_a, **_kw):
            executor_calls.append(1)
            return {"exports": {}}

    monkeypatch.setattr(orchestrator, "Executor", FakeExecutor)

    async def make_ctx(_agent):
        raise AssertionError("make_ctx('Executor') must never be reached once the manifest gate refuses")

    async def make_child_ctx(_agent, _parent_task_id):
        raise AssertionError("no child context should be built before the manifest gate resolves")

    async def complete_and_accept(_ctx, _result):
        return True

    async def on_phase(_phase, _payload):
        return None

    async def close_last_phase():
        return None

    manager = _FakeManager()
    db = _FakeDb()

    result = await orchestrator.execute_decisions(
        db=db, sid="s", session={"id": "s"}, session_filter={"id": "s"},
        files=[{"file_id": "f1", "kind": "dataset", "stored_path": "/no/such/file.csv",
                "subtype": "csv", "columns": ["name"]}],
        decisions=[{"file_id": "f1", "column": "name", "action": "drop"}],
        statute={}, praxis_methods={}, dictionary_by_column={},
        make_ctx=make_ctx, make_child_ctx=make_child_ctx,
        complete_and_accept=complete_and_accept,
        manager=manager, on_phase=on_phase, close_last_phase=close_last_phase,
        phase_timings={}, run_started=time.perf_counter(),
        sentinel_report={"preview_status": "PASS"},
        run_id=run_id, store=store,
    )

    assert executor_calls == [], "Executor must never run once the manifest is invalidated"
    assert result["status"] == "awaiting_human_review"

    # The manifest on file is still the SAME invalidated one -- no
    # replacement was silently minted under the same artifact_id.
    stored = await store.get_one(MANIFEST_COLLECTION, {"artifact_id": manifest_artifact_id(run_id)})
    assert stored["status"] == "invalidated"
    assert stored["manifest_id"] == invalidated.manifest_id

    open_requests = await store.find_many("human_review_requests", {"run_id": run_id, "state": "open"})
    assert len(open_requests) == 1
    assert "manifest_freeze_refused" in open_requests[0]["reason_codes"]


@pytest.mark.asyncio
async def test_execute_decisions_freezes_and_runs_when_no_manifest_exists_yet(monkeypatch, tmp_path) -> None:
    """Contrast case: with no prior manifest and clean freeze conditions,
    execute_decisions freezes one and Executor genuinely runs -- the gate
    blocks a stale manifest, not every execution."""
    from uuid import uuid4

    from phi_core.paths import DATA_DIR

    store = MemoryControlStore()
    run = await start_test_run(store, "s" * 32)
    run_id = run.run_id

    src = DATA_DIR / "uploads" / f"{uuid4().hex}.csv"
    src.write_text("name\nJane Doe\n", encoding="utf-8")

    executor_calls: list[int] = []

    class FakeExecutor:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx

        async def run(self, *_a, **_kw):
            executor_calls.append(1)
            return {"exports": {"f1": str(src)}, "pseudonym_count": 0, "reversal_key_blob": None}

    monkeypatch.setattr(orchestrator, "Executor", FakeExecutor)

    class FakeOperator:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx

        async def run(self, **_kw):
            return {"failed_file_ids": [], "verdicts": []}

    class FakeReviewer:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx

        async def run(self, **kw):
            return {"exports": kw.get("exports", {}), "findings": []}

    monkeypatch.setattr(orchestrator, "DeterministicVerifier", FakeOperator)
    monkeypatch.setattr(orchestrator, "Reviewer", FakeReviewer)

    async def make_ctx(agent):
        from phi_core.control.testing import make_ctx as _make_ctx
        return _make_ctx(agent, run_id=run_id, store=store)

    async def make_child_ctx(agent, _parent_task_id):
        return await make_ctx(agent)

    async def complete_and_accept(_ctx, _result):
        return True

    async def on_phase(_phase, _payload):
        return None

    async def close_last_phase():
        return None

    manager = _FakeManager()
    db = _FakeDb()

    try:
        await orchestrator.execute_decisions(
            db=db, sid="s", session={"id": "s"}, session_filter={"id": "s"},
            files=[{"file_id": "f1", "kind": "dataset", "stored_path": str(src),
                    "subtype": "csv", "columns": ["name"]}],
            decisions=[{"file_id": "f1", "column": "name", "action": "drop"}],
            statute={}, praxis_methods={}, dictionary_by_column={},
            make_ctx=make_ctx, make_child_ctx=make_child_ctx,
            complete_and_accept=complete_and_accept,
            manager=manager, on_phase=on_phase, close_last_phase=close_last_phase,
            phase_timings={}, run_started=time.perf_counter(),
            sentinel_report={"preview_status": "PASS"},
            run_id=run_id, store=store,
        )
    except Exception:
        # Downstream stages (Publish Guard, Auditor, Ledger, Herald) are
        # not faked here -- only the manifest-gate-to-Executor boundary
        # this test targets matters; a later-stage error is acceptable as
        # long as Executor was actually reached and ran.
        pass

    assert executor_calls == [1], "Executor must run once a fresh manifest is legitimately frozen"
    stored = await store.get_one(MANIFEST_COLLECTION, {"artifact_id": manifest_artifact_id(run_id)})
    assert stored is not None
    assert stored["status"] == "verified_for_execution"
