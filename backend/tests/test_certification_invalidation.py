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
    assert db["work_items"].docs[0]["input_ref"] == {"run_type": "study"}

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
        "dataset_file_downloads": [{"file_id": "dataset", "downloaded_by": "reviewer", "decision_version": 0}],
    })

    async def fake_cfg():
        return SimpleNamespace(provider="test", model="test")

    monkeypatch.setattr(srv, "get_db", lambda: db)
    monkeypatch.setattr(srv, "_current_llm_cfg", fake_cfg)

    response = await srv.session_human_review(
        "sid",
        srv.HumanReviewSubmit(
            client_event_id="ce-tail-claim",
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
    assert db.doc["_pipeline_run_id"] == "classification-claim"
    assert len(db["work_items"].docs) == 1
    assert db["work_items"].docs[0]["task_type"] == "pipeline_resume"
    assert db["work_items"].docs[0]["input_ref"] == {"run_type": "study"}
    with pytest.raises(HTTPException) as excinfo:
        await srv.session_handle("sid", principal="reviewer")
    assert excinfo.value.status_code == 409

@pytest.mark.asyncio
async def test_a_successful_submission_persists_its_event_and_resolves_the_durable_request(monkeypatch):
    """D13 steps 5/9: a durable open HumanReviewRequest for this run gets a
    HumanReviewEvent recording the final result, and is marked resolved via
    SuperOrchestrator.consume_review_event once the submission actually
    resolves the pipeline (not merely defers)."""
    import server as srv
    from phi_core.control.records import HumanReviewRequest, WorkflowRun

    db = _ConditionalStubDB({
        "id": "sid",
        "owner": "reviewer",
        "intake_status": "ready",
        "status": "awaiting_human_review",
        "_pipeline_run_id": "run-idem",
        "files": [{"file_id": "dataset", "kind": "dataset", "stored_path": "/tmp/dataset.csv", "columns": ["subject_id"]}],
        "agent_decisions": [{
            "file_id": "dataset",
            "column": "subject_id",
            "action": "human_review",
            "suggested_action": "drop",
            "suggested_reason": "direct identifier",
        }],
        "dataset_file_downloads": [{"file_id": "dataset", "downloaded_by": "reviewer", "decision_version": 0}],
    })
    request = HumanReviewRequest(run_id="run-idem", session_id="sid", workflow_version="wf/1",
                                 task_id="", node="human_review_decisions")
    db["human_review_requests"].docs.append(request.model_dump())
    run = WorkflowRun(run_id="run-idem", session_id="sid", state="awaiting_human_review",
                      node="human_review_decisions")
    db["workflow_runs"].docs.append(run.model_dump())

    async def fake_cfg():
        return SimpleNamespace(provider="test", model="test")

    monkeypatch.setattr(srv, "get_db", lambda: db)
    monkeypatch.setattr(srv, "_current_llm_cfg", fake_cfg)

    result = await srv.session_human_review(
        "sid",
        srv.HumanReviewSubmit(
            client_event_id="ce-resolve-1", actual_knowledge_ack=True,
            resolutions=[{"file_id": "dataset", "column": "subject_id", "mode": "approve"}],
        ),
        principal="reviewer",
    )

    assert result == {"status": "resuming"}
    events = db["human_review_events"].docs
    assert len(events) == 1
    assert events[0]["request_id"] == request.request_id
    assert events[0]["client_event_id"] == "ce-resolve-1"
    assert events[0]["result"] == {"status": "resuming"}
    assert db["human_review_requests"].docs[0]["state"] == "resolved"


@pytest.mark.asyncio
async def test_client_event_id_is_idempotent_while_the_request_stays_open(monkeypatch):
    """D13 step 3: while a durable HumanReviewRequest is still open (a
    defer-only round resolves nothing, so it stays open across
    submissions), a resubmission under the same client_event_id and body
    is answered from the stored result rather than reprocessed; the same
    key with a different body is 409, not silently accepted as a second
    event."""
    import server as srv
    from fastapi import HTTPException
    from phi_core.control.records import HumanReviewRequest

    db = _ConditionalStubDB({
        "id": "sid",
        "owner": "reviewer",
        "intake_status": "ready",
        "status": "awaiting_human_review",
        "_pipeline_run_id": "run-open",
        "files": [{"file_id": "dataset", "kind": "dataset", "stored_path": "/tmp/dataset.csv", "columns": ["research_flag"]}],
        "agent_decisions": [{
            "file_id": "dataset",
            "column": "research_flag",
            "action": "human_review",
            "suggested_action": "drop",
            "suggested_reason": "direct identifier",
        }],
        "dataset_file_downloads": [{"file_id": "dataset", "downloaded_by": "reviewer", "decision_version": 0}],
    })
    request = HumanReviewRequest(run_id="run-open", session_id="sid", workflow_version="wf/1",
                                 task_id="", node="human_review_decisions")
    db["human_review_requests"].docs.append(request.model_dump())

    def _defer_body():
        return srv.HumanReviewSubmit(
            client_event_id="ce-defer-idem", actual_knowledge_ack=False,
            resolutions=[{"file_id": "dataset", "column": "research_flag", "mode": "defer"}],
        )

    monkeypatch.setattr(srv, "get_db", lambda: db)

    first = await srv.session_human_review("sid", _defer_body(), principal="reviewer")
    assert first == {"status": "still_awaiting", "unresolved": 1}
    assert len(db["human_review_events"].docs) == 1
    assert db["human_review_requests"].docs[0]["state"] == "open"

    # Same key, same body: answered from the stored result, not reprocessed.
    replay = await srv.session_human_review("sid", _defer_body(), principal="reviewer")
    assert replay == {"status": "still_awaiting", "unresolved": 1}
    assert len(db["human_review_events"].docs) == 1

    # Same key, different body: rejected, not silently accepted as a new event.
    different_body = srv.HumanReviewSubmit(
        client_event_id="ce-defer-idem", actual_knowledge_ack=True,
        resolutions=[{"file_id": "dataset", "column": "subject_id", "mode": "approve"}],
    )
    with pytest.raises(HTTPException) as excinfo:
        await srv.session_human_review("sid", different_body, principal="reviewer")
    assert excinfo.value.status_code == 409
    assert len(db["human_review_events"].docs) == 1

@pytest.mark.asyncio
async def test_confirm_auditor_confidence_requires_audit_version(monkeypatch):
    """D13 step 4: confirm_auditor_confidence=True without an audit_version
    is rejected outright, not silently accepted as an unconfirmed no-op."""
    import server as srv
    from fastapi import HTTPException

    db = _ConditionalStubDB({
        "id": "sid", "owner": "reviewer", "intake_status": "ready",
        "status": "awaiting_human_review", "_pipeline_run_id": "run-audit-1",
        "files": [{"file_id": "dataset", "kind": "dataset", "stored_path": "/tmp/dataset.csv",
                   "columns": ["research_flag"]}],
        "agent_decisions": [{
            "file_id": "dataset", "column": "research_flag", "action": "human_review",
            "suggested_action": "drop", "suggested_reason": "direct identifier",
        }],
    })
    monkeypatch.setattr(srv, "get_db", lambda: db)

    with pytest.raises(HTTPException) as excinfo:
        await srv.session_human_review(
            "sid",
            srv.HumanReviewSubmit(
                client_event_id="ce-audit-noversion", confirm_auditor_confidence=True,
                resolutions=[],
            ),
            principal="reviewer",
        )
    assert excinfo.value.status_code == 400
    assert "audit_version" in excinfo.value.detail


@pytest.mark.asyncio
async def test_confirm_auditor_confidence_rejects_stale_audit_version(monkeypatch):
    """D13 step 7: a confirmation naming an audit_version other than the
    open request's own is a stale confirmation -- rejected, not accepted as
    confirming whatever the current verdict happens to be."""
    import server as srv
    from fastapi import HTTPException
    from phi_core.control.records import HumanReviewRequest

    db = _ConditionalStubDB({
        "id": "sid", "owner": "reviewer", "intake_status": "ready",
        "status": "awaiting_human_review", "_pipeline_run_id": "run-audit-2",
        "files": [{"file_id": "dataset", "kind": "dataset", "stored_path": "/tmp/dataset.csv",
                   "columns": ["research_flag"]}],
        "agent_decisions": [{
            "file_id": "dataset", "column": "research_flag", "action": "human_review",
            "suggested_action": "drop", "suggested_reason": "direct identifier",
        }],
    })
    request = HumanReviewRequest(run_id="run-audit-2", session_id="sid", workflow_version="wf/1",
                                  task_id="", node="human_review_audit", audit_version="verdict-v1")
    db["human_review_requests"].docs.append(request.model_dump())
    monkeypatch.setattr(srv, "get_db", lambda: db)

    with pytest.raises(HTTPException) as excinfo:
        await srv.session_human_review(
            "sid",
            srv.HumanReviewSubmit(
                client_event_id="ce-audit-stale", confirm_auditor_confidence=True,
                audit_version="verdict-v2-stale", resolutions=[],
            ),
            principal="reviewer",
        )
    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_confirm_auditor_confidence_accepts_matching_audit_version(monkeypatch):
    """The mirror of the stale case: a confirmation naming exactly the open
    request's audit_version is accepted and recorded on the resulting
    HumanReviewEvent."""
    import server as srv
    from phi_core.control.records import HumanReviewRequest

    db = _ConditionalStubDB({
        "id": "sid", "owner": "reviewer", "intake_status": "ready",
        "status": "awaiting_human_review", "_pipeline_run_id": "run-audit-3",
        "files": [{"file_id": "dataset", "kind": "dataset", "stored_path": "/tmp/dataset.csv",
                   "columns": ["research_flag"]}],
        "agent_decisions": [{
            "file_id": "dataset", "column": "research_flag", "action": "human_review",
            "suggested_action": "drop", "suggested_reason": "direct identifier",
        }],
    })
    request = HumanReviewRequest(run_id="run-audit-3", session_id="sid", workflow_version="wf/1",
                                  task_id="", node="human_review_audit", audit_version="verdict-v1")
    db["human_review_requests"].docs.append(request.model_dump())
    monkeypatch.setattr(srv, "get_db", lambda: db)

    result = await srv.session_human_review(
        "sid",
        srv.HumanReviewSubmit(
            client_event_id="ce-audit-match", confirm_auditor_confidence=True,
            audit_version="verdict-v1", resolutions=[],
        ),
        principal="reviewer",
    )
    assert result["status"] == "still_awaiting"
    events = db["human_review_events"].docs
    assert len(events) == 1
    assert events[0]["kind"] == "audit_confidence_confirmation"
    assert events[0]["audit_version"] == "verdict-v1"


@pytest.mark.asyncio
async def test_stale_download_at_a_different_decision_version_does_not_satisfy_the_gate(monkeypatch):
    """D13 step 8: a download recorded against an older decision_version
    does not satisfy the actual-knowledge gate for the current one -- the
    reviewer must have opened the file as it exists now, not as it existed
    before a prior decision mutation bumped the run's decision_version."""
    import server as srv
    from fastapi import HTTPException
    from phi_core.control.records import WorkflowRun

    db = _ConditionalStubDB({
        "id": "sid", "owner": "reviewer", "intake_status": "ready",
        "status": "awaiting_human_review", "_pipeline_run_id": "run-dl-1",
        "files": [{"file_id": "dataset", "kind": "dataset", "stored_path": "/tmp/dataset.csv",
                   "columns": ["subject_id"]}],
        "agent_decisions": [{
            "file_id": "dataset", "column": "subject_id", "action": "human_review",
            "suggested_action": "drop", "suggested_reason": "direct identifier",
        }],
        "dataset_file_downloads": [{"file_id": "dataset", "downloaded_by": "reviewer", "decision_version": 0}],
    })
    run = WorkflowRun(run_id="run-dl-1", session_id="sid", state="awaiting_human_review",
                       node="human_review_decisions", decision_version=1)
    db["workflow_runs"].docs.append(run.model_dump())
    monkeypatch.setattr(srv, "get_db", lambda: db)

    with pytest.raises(HTTPException) as excinfo:
        await srv.session_human_review(
            "sid",
            srv.HumanReviewSubmit(
                client_event_id="ce-stale-dl", actual_knowledge_ack=True,
                resolutions=[{"file_id": "dataset", "column": "subject_id", "mode": "approve"}],
            ),
            principal="reviewer",
        )
    assert excinfo.value.status_code == 400
    assert "dataset" in excinfo.value.detail


@pytest.mark.asyncio
async def test_download_at_the_current_decision_version_satisfies_the_gate(monkeypatch):
    """The mirror of the stale case: a download recorded at exactly the
    run's current decision_version does satisfy the gate."""
    import server as srv
    from phi_core.control.records import WorkflowRun

    db = _ConditionalStubDB({
        "id": "sid", "owner": "reviewer", "intake_status": "ready",
        "status": "awaiting_human_review", "_pipeline_run_id": "run-dl-2",
        "files": [{"file_id": "dataset", "kind": "dataset", "stored_path": "/tmp/dataset.csv",
                   "columns": ["subject_id"]}],
        "agent_decisions": [{
            "file_id": "dataset", "column": "subject_id", "action": "human_review",
            "suggested_action": "drop", "suggested_reason": "direct identifier",
        }],
        "dataset_file_downloads": [{"file_id": "dataset", "downloaded_by": "reviewer", "decision_version": 1}],
    })
    run = WorkflowRun(run_id="run-dl-2", session_id="sid", state="awaiting_human_review",
                       node="human_review_decisions", decision_version=1)
    db["workflow_runs"].docs.append(run.model_dump())

    async def fake_cfg():
        return SimpleNamespace(provider="test", model="test")

    monkeypatch.setattr(srv, "get_db", lambda: db)
    monkeypatch.setattr(srv, "_current_llm_cfg", fake_cfg)

    result = await srv.session_human_review(
        "sid",
        srv.HumanReviewSubmit(
            client_event_id="ce-current-dl", actual_knowledge_ack=True,
            resolutions=[{"file_id": "dataset", "column": "subject_id", "mode": "approve"}],
        ),
        principal="reviewer",
    )
    assert result["status"] == "resuming"


@pytest.mark.asyncio
async def test_build_review_event_is_the_same_typed_record_on_both_review_surfaces(monkeypatch):
    """D13 plan step 5: one typed `HumanReviewEvent` record, built by one
    function (`_build_review_event`), is used by both the still-awaiting
    surface (`session_human_review`'s early-return branch) and the
    resuming surface (the branch that calls
    `SuperOrchestrator.consume_review_event`). This proves parity: given
    the same submission, the two call sites render fields identically
    except the ones that legitimately differ by surface (`run_id`,
    `result`, and `seq`, which counts prior events for the same
    request_id)."""
    import server as srv

    body = srv.HumanReviewSubmit(
        client_event_id="ce-parity", actual_knowledge_ack=True,
        resolutions=[{"file_id": "dataset", "column": "subject_id", "mode": "approve"}],
    )

    still_awaiting_event = await srv._build_review_event(
        MemoryControlStore(), request_id="req-1", run_id="prior-run", session_id="sid",
        principal="reviewer", body=body, body_hash="deadbeef", decision_version=3,
        result={"status": "still_awaiting", "unresolved": 1},
    )
    resuming_event = await srv._build_review_event(
        MemoryControlStore(), request_id="req-1", run_id="resume-run", session_id="sid",
        principal="reviewer", body=body, body_hash="deadbeef", decision_version=3,
        result={"status": "resuming"},
    )

    surface_specific = {"event_id", "run_id", "submitted_at", "result", "seq"}
    still_dump = still_awaiting_event.model_dump()
    resuming_dump = resuming_event.model_dump()
    shared_fields = set(still_dump) - surface_specific
    assert shared_fields, "expected at least one field common to both surfaces"
    for field in shared_fields:
        assert still_dump[field] == resuming_dump[field], field
    # Both surfaces build against an empty request, so seq is identical too.
    assert still_dump["seq"] == resuming_dump["seq"] == 1
    # The surface-specific fields really do differ -- proving this is a
    # parity test of one record shape, not two records that happen to be
    # equal because nothing distinguishing was passed in.
    assert still_dump["run_id"] != resuming_dump["run_id"]
    assert still_dump["result"] != resuming_dump["result"]


@pytest.mark.asyncio
async def test_comment_resolution_never_auto_applies_regardless_of_confidence(monkeypatch):
    """D13 step 6: a model's interpretation of a reviewer's free-text
    comment always lands in ``pending_confirmation``, even at high
    self-reported confidence. The prior ``>=0.60`` auto-apply let an LLM's
    guess become the operative de-identification decision with no human
    ever confirming it; this proves that path is gone and a second,
    explicit reviewer confirmation is required before anything applies."""
    import server as srv
    from phi_core.agents import reasoning
    from phi_core.control import activation

    db = _ConditionalStubDB({
        "id": "sid",
        "owner": "reviewer",
        "intake_status": "ready",
        "status": "awaiting_human_review",
        "_pipeline_run_id": "run-1",
        "files": [{"file_id": "dataset", "kind": "dataset", "stored_path": "/tmp/dataset.csv",
                   "columns": ["research_flag"]}],
        "agent_decisions": [{
            "file_id": "dataset",
            "column": "research_flag",
            "action": "human_review",
            "suggested_action": "drop",
            "suggested_reason": "direct identifier",
        }],
        "dataset_file_downloads": [{"file_id": "dataset", "downloaded_by": "reviewer", "decision_version": 0}],
    })

    class FakeActivationFactory:
        def __init__(self, *_a, **_kw):
            pass

        async def activate(self, **_kw):
            return SimpleNamespace()

    class FakeJudge:
        def __init__(self, *_a, **_kw):
            pass

        async def resolve_comment(self, **_kw):
            # Deliberately high confidence: proves the removal is
            # unconditional, not merely a lowered threshold.
            return {"action": "drop", "reason": "direct identifier, no research value", "confidence": 0.99}

    async def fake_cfg():
        return SimpleNamespace(provider="test", model="test")

    monkeypatch.setattr(srv, "get_db", lambda: db)
    monkeypatch.setattr(srv, "_current_llm_cfg", fake_cfg)
    monkeypatch.setattr(activation, "ActivationFactory", FakeActivationFactory)
    monkeypatch.setattr(reasoning, "Judge", FakeJudge)

    response = await srv.session_human_review(
        "sid",
        srv.HumanReviewSubmit(
            client_event_id="ce-comment-round",
            actual_knowledge_ack=True,
            resolutions=[{
                "file_id": "dataset",
                "column": "research_flag",
                "mode": "comment",
                "comment": "this is a direct identifier with no research value; drop it",
            }],
        ),
        principal="reviewer",
    )

    assert response == {"status": "still_awaiting", "unresolved": 1}
    decision = db.doc["agent_decisions"][0]
    assert decision["action"] == "human_review"
    assert decision["pending_confirmation"] == {
        "action": "drop", "reason": "direct identifier, no research value", "confidence": 0.99,
    }
    assert db.doc["status"] == "awaiting_human_review"  # never resumed the pipeline
    assert len(db["work_items"].docs) == 0

    # Round 2: only an explicit reviewer confirmation applies it.
    response2 = await srv.session_human_review(
        "sid",
        srv.HumanReviewSubmit(
            client_event_id="ce-approve-round",
            actual_knowledge_ack=True,
            resolutions=[{"file_id": "dataset", "column": "research_flag", "mode": "approve"}],
        ),
        principal="reviewer",
    )
    assert response2 == {"status": "resuming"}
    decision2 = db.doc["agent_decisions"][0]
    assert decision2["action"] == "drop"
    assert decision2["provenance"] == "human_comment_inferred"



@pytest.mark.asyncio
async def test_cancel_submits_the_existing_run_to_super_orchestrator(monkeypatch):
    import server as srv
    from phi_core.control import superorchestrator as super_module

    db = _ConditionalStubDB({
        "id": "sid",
        "owner": "reviewer",
        "status": "classifying",
        "_pipeline_run_id": "a" * 32,
    })
    calls: list[dict] = []

    class FakeSuperOrchestrator:
        def __init__(self, *_args):
            pass

        async def cancel_run(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(srv, "get_db", lambda: db)
    monkeypatch.setattr(super_module, "SuperOrchestrator", FakeSuperOrchestrator)

    response = await srv.session_cancel("sid", principal="reviewer")

    assert response == {"status": "cancel_requested", "already_settled": False}
    assert db.doc["cancel_requested"] is True
    assert calls == [{
        "session_id": "sid",
        "run_id": "a" * 32,
        "principal": "reviewer",
        "reason": "operator requested cancel via /api/sessions/{sid}/cancel",
    }]


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
        "dataset_file_downloads": [{"file_id": "dataset", "downloaded_by": "reviewer", "decision_version": 0}],
    })
    emitted = []

    async def _complete(ctx, result):
        if ctx is not None and ctx.tasks is not None:
            await ctx.tasks.complete(result)

    class FakeExecutor:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self._ctx = ctx

        async def run(self, **_kwargs):
            result = {"exports": {}}
            await _complete(self._ctx, result)
            return result

    class FakeAuditor:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self._ctx = ctx

        async def _log(self, *_a, **_kw):
            return None

        async def run(self, **_kwargs):
            result = {"verdict": "clean", "issues": [], "metrics": {}, "confidence": 1.0, "summary": "ok"}
            await _complete(self._ctx, result)
            return result

    class FakeOperator:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self._ctx = ctx

        async def run(self, **_kwargs):
            result = {"failed_file_ids": [], "verdicts": []}
            await _complete(self._ctx, result)
            return result

    class FakeReviewer:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self._ctx = ctx

        async def run(self, exports, **_kwargs):
            result = {"exports": exports, "findings": []}
            await _complete(self._ctx, result)
            return result

    class FakeScout:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self._ctx = ctx

        async def _log(self, *_a, **_kw):
            return None

        async def run(self, **_kwargs):
            await _complete(self._ctx, {})
            return {}

    class FakeLedger:
        def __init__(self, ctx=None, compare_ctx=None, aggregate_ctx=None, **_kwargs):
            self._ctxs = [c for c in (ctx, compare_ctx, aggregate_ctx) if c is not None]

        async def run(self, **_kwargs):
            for c in self._ctxs:
                await _complete(c, {})
            return {}

    class FakeHerald:
        def __init__(self, ctx=None, abstract_ctx=None, sections_ctx=None, **_kwargs):
            self._ctxs = [c for c in (ctx, abstract_ctx, sections_ctx) if c is not None]

        async def run(self, **_kwargs):
            for c in self._ctxs:
                await _complete(c, {})
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
            client_event_id="ce-cancel-check",
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
                client_event_id="ce-stale-tail",
                actual_knowledge_ack=True,
                resolutions=[],
            ),
            principal="reviewer",
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