"""Regression coverage for invalidating stale export certification on reruns."""
from __future__ import annotations

import asyncio

import pytest


class _StubDB:
    def __init__(self, doc: dict):
        self.doc = doc
        self.sessions = self
        self.updates: list[tuple[tuple, dict]] = []

    async def find_one(self, *_args, **_kwargs):
        return self.doc

    async def update_one(self, *args, **kwargs):
        self.updates.append((args, kwargs))
        update = args[1]
        self.doc.update(update.get("$set", {}))
        for key in update.get("$unset", {}):
            self.doc.pop(key, None)


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


def test_pipeline_invalidates_prior_certification_before_executor_work(monkeypatch):
    """The run entrypoint clears old certification before Executor can write."""
    from phi_core.agents import orchestrator

    prior = {
        "id": "sid",
        "status": "complete",
        "guard_report": {"status": "clean", "results": []},
        "export_paths": {"dataset": "/old/export.csv"},
        "files": [],
    }
    db = _StubDB(prior)

    class StopPipeline(Exception):
        pass

    class FakeStatute:
        def __init__(self, **_kwargs):
            pass

        async def run(self, **_kwargs):
            return {}

    class FakePraxis(FakeStatute):
        async def method_for(self, _category):
            return {}

    class FakeJudge(FakeStatute):
        async def run(self, **_kwargs):
            return {"decisions": []}

    class FakeSentinel(FakeStatute):
        async def run(self, **_kwargs):
            return {"issues": []}

    class FakeExecutor(FakeStatute):
        async def run(self, **_kwargs):
            assert db.doc["status"] == "classifying"
            assert "guard_report" not in db.doc
            assert "export_paths" not in db.doc
            raise StopPipeline()

    monkeypatch.setattr(orchestrator, "Statute", FakeStatute)
    monkeypatch.setattr(orchestrator, "Praxis", FakePraxis)
    monkeypatch.setattr(orchestrator, "Judge", FakeJudge)
    monkeypatch.setattr(orchestrator, "Sentinel", FakeSentinel)
    monkeypatch.setattr(orchestrator, "Executor", FakeExecutor)

    async def emit(_message):
        return None

    async def on_phase(_phase, _payload):
        return None

    with pytest.raises(StopPipeline):
        asyncio.run(orchestrator.run_pipeline(prior, db, object(), emit, on_phase))

    args, kwargs = db.updates[0]
    assert args[0] == {"id": "sid"}
    assert kwargs == {}
    assert args[1] == {
        "$set": {"status": "classifying"},
        "$unset": {"guard_report": "", "export_paths": ""},
    }
