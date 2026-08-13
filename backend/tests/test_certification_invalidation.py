"""Regression coverage for invalidating stale export certification on reruns."""
from __future__ import annotations

import asyncio

from types import SimpleNamespace

import pytest


class _StubDB:
    def __init__(self, doc: dict):
        self.doc = doc
        self.sessions = self
        self.agent_log = self
        self.updates: list[tuple[tuple, dict]] = []
        self.inserted: list[dict] = []

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

    response = await srv.session_export("sid", "dataset", force=False)

    assert response.status_code == 403
    assert db.updates == []


@pytest.mark.asyncio
async def test_processing_session_refuses_force_without_recording_override(monkeypatch, tmp_path):
    """The audited force path is unavailable until the session completes."""
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

    response = await srv.session_export("sid", "dataset", force=True)

    assert response.status_code == 403
    assert db.updates == []


@pytest.mark.asyncio
async def test_processing_session_refuses_stale_clean_bundle(monkeypatch):
    """A prior aggregate clean report cannot certify a bundle during a rerun."""
    from fastapi import HTTPException
    import phi_core.bundle as bundle
    import server as srv

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
    from fastapi import HTTPException
    import server as srv

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
    scheduled = []

    async def fake_cfg():
        return SimpleNamespace(provider="test", model="test")

    def hold_worker(coro):
        scheduled.append(coro)
        coro.close()

    monkeypatch.setattr(srv, "get_db", lambda: db)
    monkeypatch.setattr(srv, "_current_llm_cfg", fake_cfg)
    monkeypatch.setattr(srv.asyncio, "create_task", hold_worker)

    assert (await srv.session_handle("sid", principal="reviewer"))["status"] == "started"
    assert db.doc["status"] == "classifying"
    assert "guard_report" not in db.doc
    assert "export_paths" not in db.doc
    assert len(scheduled) == 1

    assert (await srv.session_export("sid", "dataset", force=False, principal="reviewer")).status_code == 403
    assert (await srv.session_export("sid", "dataset", force=True, principal="reviewer")).status_code == 403
    with pytest.raises(HTTPException) as excinfo:
        await srv.session_bundle("sid", publication=False, attestation_pdf=False, principal="reviewer")
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_overlapping_handles_claim_only_one_worker(monkeypatch):
    """The conditional launch claim rejects an active conflicting request."""
    from fastapi import HTTPException
    import server as srv

    db = _ConditionalStubDB({
        "id": "sid",
        "owner": "reviewer",
        "intake_status": "ready",
        "status": "complete",
        "files": [],
    })
    scheduled = []

    async def fake_cfg():
        return SimpleNamespace(provider="test", model="test")

    def hold_worker(coro):
        scheduled.append(coro)
        coro.close()

    monkeypatch.setattr(srv, "get_db", lambda: db)
    monkeypatch.setattr(srv, "_current_llm_cfg", fake_cfg)
    monkeypatch.setattr(srv.asyncio, "create_task", hold_worker)

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
    assert len(scheduled) == 1


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
        def __init__(self, **_kwargs):
            self.call_failures = 0

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
        object(),
        emit,
        on_phase,
        run_id="old-claim",
    ))

    assert db.doc == {
        "id": "sid",
        "status": "classifying",
        "_pipeline_run_id": "newer-claim",
        "files": [],
    }



@pytest.mark.asyncio
async def test_completed_blocked_export_retains_audited_force_override(monkeypatch, tmp_path):
    """A settled blocked result still permits the existing audited force flow."""
    import server as srv

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

    response = await srv.session_export("sid", "dataset", force=True)

    assert response.status_code == 200
    assert db.updates[-1][0][1]["$push"]["guard_overrides"]["file_id"] == "dataset"


@pytest.mark.asyncio
async def test_completed_clean_export_retains_normal_download(monkeypatch, tmp_path):
    """A settled clean per-file certification still permits a normal download."""
    import server as srv

    export = tmp_path / "clean.csv"
    export.write_text("clean export", encoding="utf-8")
    db = _ConditionalStubDB({
        "id": "sid",
        "status": "complete",
        "export_paths": {"dataset": str(export)},
        "guard_report": {
            "status": "clean",
            "results": [{"file_id": "dataset", "status": "clean"}],
        },
    })
    monkeypatch.setattr(srv, "get_db", lambda: db)

    response = await srv.session_export("sid", "dataset")

    assert response.status_code == 200
    assert db.updates == []


@pytest.mark.asyncio
async def test_human_review_tail_claims_awaiting_session_before_scheduling(monkeypatch):
    """Only one token-claimed human-review tail may publish its completion."""
    from fastapi import HTTPException
    import server as srv

    db = _ConditionalStubDB({
        "id": "sid",
        "owner": "reviewer",
        "intake_status": "ready",
        "status": "awaiting_human_review",
        "_pipeline_run_id": "classification-claim",
        "files": [],
        "agent_decisions": [{
            "file_id": "dataset",
            "column": "subject_id",
            "action": "human_review",
        }],
    })
    scheduled = []

    async def fake_cfg():
        return SimpleNamespace(provider="test", model="test")

    def hold_worker(coro):
        scheduled.append(coro)
        coro.close()

    monkeypatch.setattr(srv, "get_db", lambda: db)
    monkeypatch.setattr(srv, "_current_llm_cfg", fake_cfg)
    monkeypatch.setattr(srv.asyncio, "create_task", hold_worker)

    response = await srv.session_human_review(
        "sid",
        srv.HumanReviewSubmit(
            reviewer="reviewer",
            actual_knowledge_ack=True,
            resolutions=[{
                "file_id": "dataset",
                "column": "subject_id",
                "action": "drop",
            }],
        ),
        principal="reviewer",
    )

    assert response == {"status": "resuming"}
    assert db.doc["status"] == "anonymizing"
    assert db.doc["_pipeline_run_id"] != "classification-claim"
    assert len(scheduled) == 1
    with pytest.raises(HTTPException) as excinfo:
        await srv.session_handle("sid", principal="reviewer")
    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_stale_unresolved_human_review_cannot_overwrite_claimed_tail(monkeypatch):
    """An unresolved stale review cannot overwrite a newer tail's decisions."""
    from fastapi import HTTPException
    import server as srv

    decisions = [{
        "file_id": "dataset",
        "column": "subject_id",
        "action": "human_review",
    }]
    db = _ConditionalStubDB({
        "id": "sid",
        "status": "anonymizing",
        "_pipeline_run_id": "newer-tail-claim",
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