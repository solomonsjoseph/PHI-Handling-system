"""Regression coverage for invalidating stale export certification on reruns."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from phi_core.agents.llm import LlmConfig
from phi_core.control.store import MemoryControlStore


class _FakeCollection:
    """Minimal in-memory stand-in for an ``AsyncIOMotorCollection``, enough
    for ``MongoControlStore`` to read and write control-plane records
    (``capability_grants``, ``work_items``, ``trace_events``, ...) without a
    real Mongo connection."""

    def __init__(self):
        self.docs: list[dict] = []

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def find_one(self, query):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return dict(d)
        return None

    def find(self, query):
        async def _cursor():
            for d in self.docs:
                if all(d.get(k) == v for k, v in query.items()):
                    yield dict(d)
        return _cursor()

    async def replace_one(self, query, replacement):
        for i, d in enumerate(self.docs):
            if all(d.get(k) == v for k, v in query.items()):
                self.docs[i] = dict(replacement)
                return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)

    async def update_one(self, query, update):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                d.update(update.get("$set", {}))
                for key in update.get("$unset", {}):
                    d.pop(key, None)
                return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)

    async def delete_one(self, query):
        for i, d in enumerate(self.docs):
            if all(d.get(k) == v for k, v in query.items()):
                del self.docs[i]
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)


class _StubDB:
    def __init__(self, doc: dict):
        self.doc = doc
        self.sessions = self
        self.agent_log = self
        self.updates: list[tuple[tuple, dict]] = []
        self.inserted: list[dict] = []
        self._collections: dict[str, _FakeCollection] = {}

    def __getitem__(self, name: str) -> _FakeCollection:
        return self._collections.setdefault(name, _FakeCollection())

    def __getattr__(self, name: str) -> _FakeCollection:
        # Real Motor databases resolve db.<collection> the same as
        # db["<collection>"]; only reached when normal attribute lookup
        # (self.sessions, self.agent_log, ...) has already failed. Guard
        # against dunder/private probes so framework introspection still
        # gets a normal AttributeError.
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]

    async def find_one(self, *_args, **_kwargs):
        return self.doc

    async def update_one(self, *args, **kwargs):
        self.updates.append((args, kwargs))
        update = args[1]
        self.doc.update(update.get("$set", {}))
        for key in update.get("$unset", {}):
            self.doc.pop(key, None)

    async def insert_one(self, doc):
        self.inserted.append(doc)

@pytest.mark.asyncio
async def test_processing_session_refuses_stale_clean_export_without_override(monkeypatch, tmp_path):
    """A rerun must not serve bytes certified by a prior completed run."""
    import server as srv

    export = tmp_path / "export.csv"
    export.write_text("stale PHI", encoding="utf-8")
    db = _StubDB({
        "id": "sid",
        "status": "classifying",
        "export_paths": {"dataset": str(export)},
        "guard_report": {
            "status": "clean",
            "results": [{"file_id": "dataset", "status": "clean"}],
        },
    })
    monkeypatch.setattr(srv, "get_db", lambda: db)

    response = await srv.session_export("sid", "dataset")

    assert response.status_code == 403
    assert db.updates == []


@pytest.mark.asyncio
async def test_processing_session_refuses_export_regardless_of_result(monkeypatch, tmp_path):
    """A blocked per-file result stays refused until the session completes;
    there is no override parameter that could change that."""
    import server as srv

    export = tmp_path / "export.csv"
    export.write_text("stale PHI", encoding="utf-8")
    db = _StubDB({
        "id": "sid",
        "status": "classifying",
        "export_paths": {"dataset": str(export)},
        "guard_report": {
            "status": "blocked",
            "results": [{"file_id": "dataset", "status": "blocked"}],
        },
    })
    monkeypatch.setattr(srv, "get_db", lambda: db)

    response = await srv.session_export("sid", "dataset")

    assert response.status_code == 403
    assert db.updates == []


@pytest.mark.asyncio
async def test_processing_session_refuses_stale_clean_bundle(monkeypatch):
    """A prior aggregate clean report cannot certify a bundle during a rerun."""
    import phi_core.bundle as bundle
    import server as srv
    from fastapi import HTTPException

    db = _StubDB({
        "id": "sid",
        "status": "classifying",
        "guard_report": {"status": "clean", "results": []},
    })
    monkeypatch.setattr(srv, "get_db", lambda: db)
    calls = []

    def build_bundle(*args, **kwargs):
        calls.append((args, kwargs))
        return b"", "stale.zip"

    monkeypatch.setattr(bundle, "build_bundle", build_bundle)

    with pytest.raises(HTTPException) as excinfo:
        await srv.session_bundle("sid", publication=False, attestation_pdf=False)

    assert excinfo.value.status_code == 403
    assert calls == []


@pytest.mark.asyncio
async def test_completed_session_builds_bundle_from_clean_report(monkeypatch):
    """A completed session with fresh clean certification remains downloadable."""
    import phi_core.bundle as bundle
    import server as srv

    db = _StubDB({
        "id": "sid",
        "status": "complete",
        "guard_report": {
            "status": "clean",
            "results": [{"file_id": "dataset", "status": "clean"}],
        },
    })
    calls = []

    def build_bundle(*args, **kwargs):
        calls.append((args, kwargs))
        return b"certified bundle", "safe.zip"

    monkeypatch.setattr(srv, "get_db", lambda: db)
    monkeypatch.setattr(bundle, "build_bundle", build_bundle)

    response = await srv.session_bundle("sid", publication=False, attestation_pdf=False)

    assert response.status_code == 200
    assert response.body == b"certified bundle"
    assert response.headers["content-disposition"] == 'attachment; filename="safe.zip"'
    assert len(calls) == 1




class _UpdateResult:
    def __init__(self, matched_count: int):
        self.matched_count = matched_count


class _ConditionalStubDB(_StubDB):
    """In-memory Mongo subset that honors equality and $in claim filters."""

    @staticmethod
    def _matches(doc: dict, query: dict) -> bool:
        for key, expected in query.items():
            actual = doc.get(key)
            if isinstance(expected, dict):
                if "$in" in expected:
                    if actual not in expected["$in"]:
                        return False
                    continue
                if "$exists" in expected:
                    if (key in doc) != expected["$exists"]:
                        return False
                    continue
            if actual != expected:
                return False
        return True
    async def update_one(self, query, update, **kwargs):
        self.updates.append(((query, update), kwargs))
        if not self._matches(self.doc, query):
            return _UpdateResult(0)
        self.doc.update(update.get("$set", {}))
        for key in update.get("$unset", {}):
            self.doc.pop(key, None)
        return _UpdateResult(1)


@pytest.mark.asyncio
async def test_handle_claim_revokes_old_exports_before_worker_runs(monkeypatch, tmp_path):
    """Accepted reruns revoke clean and force download rights before scheduling."""
    import server as srv
    from fastapi import HTTPException

    export = tmp_path / "old.csv"
    export.write_text("old export", encoding="utf-8")
    db = _ConditionalStubDB({
        "id": "sid",
        "owner": "reviewer",
        "intake_status": "ready",
        "status": "complete",
        "files": [],
        "export_paths": {"dataset": str(export)},
        "guard_report": {
            "status": "clean",
            "results": [{"file_id": "dataset", "status": "clean"}],
        },
    })

    async def fake_cfg():
        return SimpleNamespace(provider="test", model="test")

    monkeypatch.setattr(srv, "get_db", lambda: db)
    monkeypatch.setattr(srv, "_current_llm_cfg", fake_cfg)

    assert (await srv.session_handle("sid", principal="reviewer"))["status"] == "started"
    assert db.doc["status"] == "classifying"
    assert "guard_report" not in db.doc
    assert "export_paths" not in db.doc
    assert len(db["work_items"].docs) == 1
    assert db["work_items"].docs[0]["task_type"] == "pipeline_run"

    assert (await srv.session_export("sid", "dataset", principal="reviewer")).status_code == 403
    with pytest.raises(HTTPException) as excinfo:
        await srv.session_bundle("sid", publication=False, attestation_pdf=False, principal="reviewer")
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_overlapping_handles_claim_only_one_worker(monkeypatch):
    """The conditional launch claim rejects an active conflicting request."""
    import server as srv
    from fastapi import HTTPException

    db = _ConditionalStubDB({
        "id": "sid",
        "owner": "reviewer",
        "intake_status": "ready",
        "status": "complete",
        "files": [],
    })

    async def fake_cfg():
        return SimpleNamespace(provider="test", model="test")

    monkeypatch.setattr(srv, "get_db", lambda: db)
    monkeypatch.setattr(srv, "_current_llm_cfg", fake_cfg)

    first, second = await asyncio.gather(
        srv.session_handle("sid", principal="reviewer"),
        srv.session_handle("sid", principal="reviewer"),
        return_exceptions=True,
    )

    responses = [r for r in (first, second) if isinstance(r, dict)]
    conflicts = [r for r in (first, second) if isinstance(r, HTTPException)]
    assert responses == [{"status": "started", "llm": {"provider": "test", "model": "test"}}]
    assert len(conflicts) == 1
    assert conflicts[0].status_code == 409
    assert len(db["work_items"].docs) == 1


def test_stale_pipeline_worker_cannot_publish_over_newer_claim(monkeypatch):
    """A stale run token cannot restore certification once another run is active."""
    from phi_core.agents import orchestrator

    db = _ConditionalStubDB({
        "id": "sid",
        "status": "classifying",
        "_pipeline_run_id": "newer-claim",
        "files": [],
    })

    class EmptyAgent:
        def __init__(self, *_a, **_kwargs):
            self.call_failures = 0
            self.last_message_id = None

        async def run(self, **_kwargs):
            return {}

    class FakePraxis(EmptyAgent):
        async def method_for(self, _category):
            return {}

    class FakeJudge(EmptyAgent):
        async def run(self, **_kwargs):
            return {"decisions": []}

    class FakeSentinel(EmptyAgent):
        async def run(self, **_kwargs):
            return {"issues": []}

    class FakeExecutor(EmptyAgent):
        async def run(self, **_kwargs):
            return {"exports": {}}

    monkeypatch.setattr(orchestrator, "Statute", EmptyAgent)
    monkeypatch.setattr(orchestrator, "Praxis", FakePraxis)
    monkeypatch.setattr(orchestrator, "Judge", FakeJudge)
    monkeypatch.setattr(orchestrator, "Sentinel", FakeSentinel)
    monkeypatch.setattr(orchestrator, "Executor", FakeExecutor)
    monkeypatch.setattr(orchestrator, "Auditor", EmptyAgent)
    monkeypatch.setattr(orchestrator, "Scout", EmptyAgent)
    monkeypatch.setattr(orchestrator, "Ledger", EmptyAgent)
    monkeypatch.setattr(orchestrator, "Herald", EmptyAgent)

    async def emit(_message):
        return None

    async def on_phase(_phase, _payload):
        return None

    asyncio.run(orchestrator.run_pipeline(
        {"id": "sid", "files": []},
        db,
        LlmConfig(provider="anthropic", model="test", max_tokens=100),
        emit,
        on_phase,
        run_id="old-claim",
        control_store=MemoryControlStore(),
    ))

    assert db.doc == {
        "id": "sid",
        "status": "classifying",
        "_pipeline_run_id": "newer-claim",
        "files": [],
    }



@pytest.mark.asyncio
async def test_completed_blocked_export_has_no_override_and_records_nothing(monkeypatch, tmp_path):
    """A settled blocked result stays blocked; D14/Phase 3 removed the
    audited ``force`` override entirely, so there is no parameter left to
    exercise it with and no ``guard_overrides`` record is ever written."""
    import inspect

    import server as srv

    assert "force" not in inspect.signature(srv.session_export).parameters

    export = tmp_path / "blocked.csv"
    export.write_text("reviewed export", encoding="utf-8")
    db = _ConditionalStubDB({
        "id": "sid",
        "status": "complete",
        "export_paths": {"dataset": str(export)},
        "guard_report": {
            "status": "blocked",
            "results": [{"file_id": "dataset", "status": "blocked"}],
        },
    })
    monkeypatch.setattr(srv, "get_db", lambda: db)

    with pytest.raises(TypeError):
        await srv.session_export("sid", "dataset", force=True)  # type: ignore[call-arg]

    response = await srv.session_export("sid", "dataset")

    assert response.status_code == 403
    assert db.updates == []


@pytest.mark.asyncio
async def test_completed_clean_export_retains_normal_download(monkeypatch):
    """A settled clean per-file certification still permits a normal
    download, now served through ArtifactService.open_for_download bound
    to the canonical artifact_id/sha256 rather than a raw path lookup."""
    import server as srv
    from phi_core.control.artifacts import ArtifactService
    from phi_core.control.store import MemoryControlStore

    sid = "c" * 32
    run_id = "d" * 32
    store = MemoryControlStore()
    service = ArtifactService(store, session_id=sid, run_id=run_id)
    artifact_id, tmp = await service.stage("dataset_export", "dataset__export.csv", "restricted_metadata", "export")
    tmp.write_bytes(b"clean export")
    await service.finalize(artifact_id)

    db = _ConditionalStubDB({
        "id": sid,
        "status": "complete",
        "_pipeline_run_id": run_id,
        "export_paths": {"dataset": f"/staging/{sid}/{run_id}/{artifact_id}.csv"},
        "guard_report": {
            "status": "clean",
            "results": [{"file_id": "dataset", "status": "clean"}],
        },
    })
    monkeypatch.setattr(srv, "get_db", lambda: db)
    monkeypatch.setattr(srv, "_artifact_service",
                        lambda _db, s, r: ArtifactService(store, session_id=s, run_id=r))

    response = await srv.session_export(sid, "dataset")

    assert response.status_code == 200
    assert response.path.read_bytes() == b"clean export"
    assert db.updates == []


@pytest.mark.asyncio
async def test_human_review_tail_claims_awaiting_session_before_scheduling(monkeypatch):
    """Only one token-claimed human-review tail may publish its completion."""
    import server as srv
    from fastapi import HTTPException

    db = _ConditionalStubDB({
        "id": "sid",
        "owner": "reviewer",
        "intake_status": "ready",
        "status": "awaiting_human_review",
        "_pipeline_run_id": "classification-claim",
        "files": [{"file_id": "dataset", "kind": "dataset", "stored_path": "/tmp/dataset.csv", "columns": ["subject_id"]}],
        "agent_decisions": [{
            "file_id": "dataset",
            "column": "subject_id",
            "action": "human_review",
            "suggested_action": "drop",
            "suggested_reason": "direct identifier",
        }],
        "dataset_file_downloads": [{"file_id": "dataset"}],
    })

    async def fake_cfg():
        return SimpleNamespace(provider="test", model="test")

    monkeypatch.setattr(srv, "get_db", lambda: db)
    monkeypatch.setattr(srv, "_current_llm_cfg", fake_cfg)

    response = await srv.session_human_review(
        "sid",
        srv.HumanReviewSubmit(
            reviewer="reviewer",
            actual_knowledge_ack=True,
            resolutions=[{
                "file_id": "dataset",
                "column": "subject_id",
                "mode": "approve",
            }],
        ),
        principal="reviewer",
    )

    assert response == {"status": "resuming"}
    assert db.doc["status"] == "anonymizing"
    assert db.doc["_pipeline_run_id"] != "classification-claim"
    assert len(db["work_items"].docs) == 1
    with pytest.raises(HTTPException) as excinfo:
        await srv.session_handle("sid", principal="reviewer")
    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_human_review_resume_persists_and_exposes_phase_timings(monkeypatch):
    """A resumed tail emits phase events and exposes its measured timings."""
    import server as srv
    from phi_core import paths
    from phi_core.agents import orchestrator
    from phi_core.control.records import WorkItem
    from phi_core.control.store import MongoControlStore

    db = _ConditionalStubDB({
        "id": "sid",
        "owner": "reviewer",
        "intake_status": "ready",
        "status": "awaiting_human_review",
        "files": [{"file_id": "dataset", "kind": "dataset", "stored_path": "/tmp/dataset.csv", "columns": ["subject_id"]}],
        "agent_decisions": [{
            "file_id": "dataset",
            "column": "subject_id",
            "action": "human_review",
            "suggested_action": "drop",
            "suggested_reason": "direct identifier",
        }],
        "dataset_file_downloads": [{"file_id": "dataset"}],
    })
    emitted = []

    class FakeExecutor:
        def __init__(self, *_a, **_kwargs):
            pass

        async def run(self, **_kwargs):
            return {"exports": {}}

    class FakeAuditor:
        def __init__(self, *_a, **_kwargs):
            pass

        async def _log(self, *_a, **_kw):
            return None

        async def run(self, **_kwargs):
            return {"verdict": "clean", "issues": [], "metrics": {}, "confidence": 1.0, "summary": "ok"}

    class FakeOperator:
        def __init__(self, *_a, **_kwargs):
            pass

        async def run(self, **_kwargs):
            return {"failed_file_ids": [], "verdicts": []}

    class FakeReviewer:
        def __init__(self, *_a, **_kwargs):
            pass

        async def run(self, exports, **_kwargs):
            return {"exports": exports, "findings": []}

    class FakeScout:
        def __init__(self, *_a, **_kwargs):
            pass

        async def _log(self, *_a, **_kw):
            return None

        async def run(self, **_kwargs):
            return {}

    class FakeLedger:
        def __init__(self, *_a, **_kwargs):
            pass

        async def run(self, **_kwargs):
            return {}

    class FakeHerald:
        def __init__(self, *_a, **_kwargs):
            pass

        async def run(self, **_kwargs):
            return {}

    async def fake_cfg():
        return SimpleNamespace(provider="anthropic", model="test")

    async def fake_emit(_sid, event, **_kwargs):
        emitted.append(event)

    monkeypatch.setattr(srv, "get_db", lambda: db)
    monkeypatch.setattr(srv, "_current_llm_cfg", fake_cfg)
    monkeypatch.setattr(srv, "_emit", fake_emit)
    monkeypatch.setattr(orchestrator, "Executor", FakeExecutor)
    monkeypatch.setattr(orchestrator, "Operator", FakeOperator)
    monkeypatch.setattr(orchestrator, "Reviewer", FakeReviewer)
    monkeypatch.setattr(orchestrator, "Auditor", FakeAuditor)
    monkeypatch.setattr(orchestrator, "Scout", FakeScout)
    monkeypatch.setattr(orchestrator, "Ledger", FakeLedger)
    monkeypatch.setattr(orchestrator, "Herald", FakeHerald)
    monkeypatch.setattr(paths, "cleanup_session_unpacked", lambda _sid: None)

    assert (await srv.session_human_review(
        "sid",
        srv.HumanReviewSubmit(
            reviewer="reviewer",
            actual_knowledge_ack=True,
            resolutions=[{
                "file_id": "dataset",
                "column": "subject_id",
                "mode": "approve",
            }],
        ),
        principal="reviewer",
    )) == {"status": "resuming"}

    # `session_human_review` only enqueues now (Phase 4 step 2/4); drive
    # the enqueued `pipeline_resume` task directly through the same
    # handler a `Worker` instance would claim and dispatch to.
    assert len(db["work_items"].docs) == 1
    work_item = WorkItem.model_validate(db["work_items"].docs[0])
    await srv._handle_pipeline_resume(MongoControlStore(db), work_item)

    results = await srv.session_results("sid", principal="reviewer")

    assert results["phase_timings"]
    assert results["run_elapsed_s"] >= 0
    assert "executor" in results["phase_timings"]
    assert any(event.phase == "agent_phase:executor" for event in emitted)


@pytest.mark.asyncio
async def test_stale_unresolved_human_review_cannot_overwrite_claimed_tail(monkeypatch):
    """An unresolved stale review cannot overwrite a newer tail's decisions."""
    import server as srv
    from fastapi import HTTPException

    decisions = [{
        "file_id": "dataset",
        "column": "custom_flagged_field",
        "action": "human_review",
    }]
    db = _ConditionalStubDB({
        "id": "sid",
        "status": "anonymizing",
        "_pipeline_run_id": "newer-tail-claim",
        "files": [{"file_id": "dataset", "kind": "dataset", "stored_path": "/tmp/dataset.csv",
                   "columns": ["custom_flagged_field"]}],
        "agent_decisions": decisions,
    })
    monkeypatch.setattr(srv, "get_db", lambda: db)

    with pytest.raises(HTTPException) as excinfo:
        await srv.session_human_review(
            "sid",
            srv.HumanReviewSubmit(
                reviewer="reviewer",
                actual_knowledge_ack=True,
                resolutions=[],
            ),
        )

    assert excinfo.value.status_code == 409
    assert db.doc["agent_decisions"] == decisions


@pytest.mark.asyncio
async def test_stale_worker_terminal_events_cannot_close_new_run_stream(monkeypatch):
    """After A completes and B claims, A's terminal events cannot reach B."""
    import server as srv

    db = _ConditionalStubDB({
        "id": "sid",
        "status": "classifying",
        "_pipeline_run_id": "run-b",
        "progress": [],
    })
    queue = asyncio.Queue()
    monkeypatch.setattr(srv, "get_db", lambda: db)
    monkeypatch.setitem(srv._progress_queues, "sid", queue)

    await srv._emit("sid", srv.ProgressEvent(phase="complete", message="A complete"), run_id="run-a")
    await srv._emit("sid", srv.ProgressEvent(phase="__end__", message="A stream end"), run_id="run-a")

    assert queue.empty()
    assert db.doc["progress"] == []