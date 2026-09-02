"""Phase 3 steps 5/6: `session_export`, `session_bundle`, and
`session_reversal_key` route through `ArtifactService.open_for_download`,
hash-bound to the canonical artifact_id rather than a raw filesystem path.

These tests exercise the full lazy-certify-then-serve path end to end
against a real `ArtifactService` + `MemoryControlStore` (no Mongo needed),
since nothing else in the codebase yet calls `certify_publication` on the
pipeline's behalf (that lands with Phase 5's
`Manager.authorize_publication`).
"""
from __future__ import annotations

import pytest
from phi_core.control.artifacts import ArtifactService
from phi_core.control.store import MemoryControlStore


class _StubDB:
    """Minimal stand-in Mongo doc-store: only what the download routes touch."""

    def __init__(self, doc: dict):
        self.doc = doc
        self.sessions = self
        self.agent_log = self
        self.updates: list[tuple[tuple, dict]] = []

    async def find_one(self, *_args, **_kwargs):
        return self.doc

    async def update_one(self, *args, **kwargs):
        self.updates.append((args, kwargs))
        update = args[1]
        self.doc.update(update.get("$set", {}))
        for key in update.get("$unset", {}):
            self.doc.pop(key, None)


def _make_session(sid: str, run_id: str, artifact_id: str, suffix: str, *, file_id: str = "dataset") -> dict:
    return {
        "id": sid,
        "status": "complete",
        "_pipeline_run_id": run_id,
        "export_paths": {file_id: f"/staging/{sid}/{run_id}/{artifact_id}{suffix}"},
        "guard_report": {
            "status": "clean",
            "results": [{"file_id": file_id, "status": "clean"}],
        },
    }


async def _stage_clean_artifact(store: MemoryControlStore, sid: str, run_id: str,
                                content: bytes = b"clean export bytes") -> str:
    service = ArtifactService(store, session_id=sid, run_id=run_id)
    artifact_id, tmp = await service.stage("dataset_export", "dataset__export.csv",
                                           "restricted_metadata", "export")
    tmp.write_bytes(content)
    await service.finalize(artifact_id)
    return artifact_id


@pytest.mark.asyncio
async def test_session_export_lazily_certifies_a_fresh_clean_result(monkeypatch):
    """Nothing else has called certify_publication yet for this run; the
    first clean download request must still succeed by certifying the
    current clean set itself."""
    import server as srv

    sid, run_id = "1" * 32, "2" * 32
    store = MemoryControlStore()
    artifact_id = await _stage_clean_artifact(store, sid, run_id)
    db = _StubDB(_make_session(sid, run_id, artifact_id, ".csv"))

    monkeypatch.setattr(srv, "get_db", lambda: db)
    monkeypatch.setattr(srv, "_artifact_service",
                        lambda _db, s, r: ArtifactService(store, session_id=s, run_id=r))

    response = await srv.session_export(sid, "dataset")

    assert response.status_code == 200
    assert response.path.read_bytes() == b"clean export bytes"
    # The served identity is the opaque artifact_id -- never an original
    # upload filename.
    assert response.filename == artifact_id


@pytest.mark.asyncio
async def test_session_export_second_request_reuses_the_certified_generation(monkeypatch):
    """A second request against the same clean result must not mint a new
    publication generation -- the artifact stays servable without
    recertifying every time."""
    import server as srv
    from phi_core.control.records import PublicationPointer

    sid, run_id = "3" * 32, "4" * 32
    store = MemoryControlStore()
    artifact_id = await _stage_clean_artifact(store, sid, run_id)
    db = _StubDB(_make_session(sid, run_id, artifact_id, ".csv"))

    monkeypatch.setattr(srv, "get_db", lambda: db)
    monkeypatch.setattr(srv, "_artifact_service",
                        lambda _db, s, r: ArtifactService(store, session_id=s, run_id=r))

    first = await srv.session_export(sid, "dataset")
    second = await srv.session_export(sid, "dataset")
    assert first.status_code == second.status_code == 200

    pointers = await store.find_many("publication_pointers", {"session_id": sid})
    assert len(pointers) == 1
    assert PublicationPointer.model_validate(pointers[0]).generation == 1


@pytest.mark.asyncio
async def test_session_bundle_refuses_on_a_tampered_export(monkeypatch):
    """D14: hash verification happens at bundle time too -- a byte flipped
    on disk after Publish Guard scanned it refuses the whole bundle."""
    import server as srv
    from fastapi import HTTPException

    sid, run_id = "5" * 32, "6" * 32
    store = MemoryControlStore()
    artifact_id = await _stage_clean_artifact(store, sid, run_id)
    db = _StubDB(_make_session(sid, run_id, artifact_id, ".csv"))

    monkeypatch.setattr(srv, "get_db", lambda: db)
    monkeypatch.setattr(srv, "_artifact_service",
                        lambda _db, s, r: ArtifactService(store, session_id=s, run_id=r))

    # Certify once (as a normal export download would), then tamper with
    # the published bytes.
    await srv.session_export(sid, "dataset")
    from phi_core.paths import PUBLISHED_DIR, run_scoped_dir
    published = run_scoped_dir(PUBLISHED_DIR, sid, run_id) / artifact_id
    published.write_bytes(b"tampered")

    with pytest.raises(HTTPException) as excinfo:
        await srv.session_bundle(sid, publication=False, attestation_pdf=False)
    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_session_reversal_key_refuses_on_a_tampered_export(monkeypatch):
    """The reversal key is only meaningful alongside a hash-verified
    publication; a tampered export refuses it too, not just the bundle."""
    import server as srv
    from fastapi import HTTPException
    from phi_core.crypto import encrypt_reversal_map

    sid, run_id = "7" * 32, "8" * 32
    store = MemoryControlStore()
    artifact_id = await _stage_clean_artifact(store, sid, run_id)
    doc = _make_session(sid, run_id, artifact_id, ".csv")
    doc["reversal_key_blob"] = encrypt_reversal_map({"salt": "s", "map": {"a": "b"}})
    db = _StubDB(doc)

    monkeypatch.setattr(srv, "get_db", lambda: db)
    monkeypatch.setattr(srv, "_artifact_service",
                        lambda _db, s, r: ArtifactService(store, session_id=s, run_id=r))

    await srv.session_export(sid, "dataset")
    from phi_core.paths import PUBLISHED_DIR, run_scoped_dir
    published = run_scoped_dir(PUBLISHED_DIR, sid, run_id) / artifact_id
    published.write_bytes(b"tampered")

    with pytest.raises(HTTPException) as excinfo:
        await srv.session_reversal_key(sid)
    assert excinfo.value.status_code == 409
    # The blob must not have been consumed by a refused request.
    assert "reversal_key_blob" in db.doc


@pytest.mark.asyncio
async def test_session_reversal_key_serves_normally_when_exports_are_intact(monkeypatch):
    import server as srv
    from phi_core.crypto import encrypt_reversal_map

    sid, run_id = "9" * 32, "a" * 32
    store = MemoryControlStore()
    artifact_id = await _stage_clean_artifact(store, sid, run_id)
    doc = _make_session(sid, run_id, artifact_id, ".csv")
    doc["reversal_key_blob"] = encrypt_reversal_map({"salt": "s", "map": {"a": "b"}})
    db = _StubDB(doc)

    monkeypatch.setattr(srv, "get_db", lambda: db)
    monkeypatch.setattr(srv, "_artifact_service",
                        lambda _db, s, r: ArtifactService(store, session_id=s, run_id=r))

    response = await srv.session_reversal_key(sid)

    assert response.status_code == 200
    assert "reversal_key_blob" not in db.doc  # single-consumption, unchanged behaviour


@pytest.mark.asyncio
async def test_no_route_accepts_a_force_style_override_parameter():
    """Acceptance: a caller-supplied override on any download/publish
    route is rejected outright because the parameter simply does not
    exist -- there is no code path left that could honour it."""
    import inspect

    import server as srv

    for fn in (srv.session_export, srv.session_bundle, srv.session_reversal_key):
        params = inspect.signature(fn).parameters
        assert "force" not in params
        assert "guard_overrides" not in params

    with pytest.raises(TypeError):
        await srv.session_export("sid", "dataset", force=True)  # type: ignore[call-arg]
