"""SEC-001 / SEC-002 / SEC-003 regression tests.

These lock down the guard-gated download boundary + corpus ground-truth
concealment. Any future refactor that reintroduces a fail-open path or
leaks the corpus answer key should fail these tests.
"""
from __future__ import annotations

import io
import zipfile

import pytest


def _scrub(doc: dict) -> dict:
    """Import the private scrubber via the server module (single source of truth)."""
    from server import _scrub_session_document
    return _scrub_session_document(doc)


# ---- SEC-003 -----------------------------------------------------------


def test_scrub_strips_corpus_ground_truth_but_keeps_summary():
    """The planted answer key must not leak via session reads. Summary
    counters are safe and useful for the UI so they stay."""
    doc = {
        "id": "sid",
        "status": "complete",
        "corpus_ground_truth": {"planted": [{"value": "415-555-1234"}]},
        "corpus_summary": {"total_cells": 96, "by_category": {"D": 3}},
    }
    out = _scrub(doc)
    assert "corpus_ground_truth" not in out, "SEC-003: ground truth leaked to session read"
    assert out.get("corpus_summary") == {"total_cells": 96, "by_category": {"D": 3}}


# ---- SEC-002 -----------------------------------------------------------


def test_corpus_verify_endpoint_is_token_gated():
    """Sibling reads carry an identity dependency; corpus verify must match."""
    from server import app
    # Find the route function for GET /api/corpus/study/verify/{sid}
    matching = [
        r for r in app.router.routes
        if getattr(r, "path", "") == "/api/corpus/study/verify/{sid}"
    ]
    assert matching, "corpus verify route not registered"
    dep_fns = {d.call.__name__ for d in matching[0].dependant.dependencies}
    assert "resolve_principal" in dep_fns, (
        "SEC-002: /api/corpus/study/verify/{sid} is not owner-scoped"
    )


# ---- SEC-001 -----------------------------------------------------------


class _StubDB:
    """Tiny stand-in Mongo doc-store for the download-gate tests."""
    def __init__(self, doc):
        self._doc = doc
        self.sessions = self
        self.updates = []

    async def find_one(self, *_args, **_kwargs):
        return self._doc

    async def update_one(self, *args, **kwargs):
        self.updates.append((args, kwargs))
        return None


@pytest.mark.asyncio
async def test_bundle_refuses_when_guard_missing(monkeypatch):
    """Legacy /finalize used to skip the guard entirely; the /bundle
    endpoint must still refuse (fail-closed) when guard_report is absent."""
    import server as srv
    from fastapi import HTTPException
    doc = {"id": "sid", "status": "complete", "export_paths": {"a": "/tmp/x"}}
    monkeypatch.setattr(srv, "get_db", lambda: _StubDB(doc))
    with pytest.raises(HTTPException) as excinfo:
        await srv.session_bundle("sid", publication=False, attestation_pdf=False)
    assert excinfo.value.status_code == 403
    assert "not certified" in excinfo.value.detail.lower()


@pytest.mark.asyncio
async def test_bundle_refuses_when_guard_blocked(monkeypatch):
    import server as srv
    from fastapi import HTTPException
    doc = {"id": "sid", "status": "complete",
           "export_paths": {"a": "/tmp/x"},
           "guard_report": {"status": "blocked", "results": []}}
    monkeypatch.setattr(srv, "get_db", lambda: _StubDB(doc))
    with pytest.raises(HTTPException) as excinfo:
        await srv.session_bundle("sid", publication=False, attestation_pdf=False)
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_export_refuses_when_guard_result_missing(monkeypatch, tmp_path):
    import server as srv
    p = tmp_path / "export.txt"
    p.write_text("clean text", encoding="utf-8")
    doc = {"id": "sid", "status": "complete",
           "export_paths": {"a": str(p)},
           "guard_report": {"status": "clean", "results": []}}  # no per-file entry for "a"
    monkeypatch.setattr(srv, "get_db", lambda: _StubDB(doc))
    resp = await srv.session_export("sid", "a", force=False)
    assert resp.status_code == 403
    body = resp.body.decode()
    assert "not_certified" in body or "not certified" in body.lower()


@pytest.mark.asyncio
async def test_export_serves_only_on_clean_per_file(monkeypatch, tmp_path):
    import server as srv
    p = tmp_path / "export.txt"
    p.write_text("clean text", encoding="utf-8")
    doc = {"id": "sid", "status": "complete",
           "export_paths": {"a": str(p)},
           "guard_report": {"status": "clean",
                             "results": [{"file_id": "a", "status": "clean"}]}}
    monkeypatch.setattr(srv, "get_db", lambda: _StubDB(doc))
    resp = await srv.session_export("sid", "a", force=False)
    # FileResponse: status_code is 200 by default.
    assert getattr(resp, "status_code", 200) == 200


@pytest.mark.asyncio
async def test_export_refuses_skipped_per_file_status(monkeypatch, tmp_path):
    """A file the Guard did not scan cannot be downloaded without review."""
    import server as srv

    p = tmp_path / "export.txt"
    p.write_text("clean text", encoding="utf-8")
    doc = {
        "id": "sid",
        "status": "complete",
        "export_paths": {"a": str(p)},
        "guard_report": {
            "status": "clean",
            "results": [{"file_id": "a", "status": "skipped"}],
        },
    }
    monkeypatch.setattr(srv, "get_db", lambda: _StubDB(doc))

    response = await srv.session_export("sid", "a", force=False)

    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "results",
    [
        [{"file_id": "a", "status": "clean"}, {"file_id": "a", "status": "blocked"}],
        [{"file_id": "a", "status": "blocked"}, {"file_id": "a", "status": "clean"}],
    ],
)
async def test_export_refuses_conflicting_per_file_guard_results(monkeypatch, tmp_path, results):
    """Conflicting stale Guard rows must not certify a file in either order."""
    import server as srv

    p = tmp_path / "export.txt"
    p.write_text("conflicting", encoding="utf-8")
    doc = {
        "id": "sid",
        "status": "complete",
        "export_paths": {"a": str(p)},
        "guard_report": {"status": "clean", "results": results},
    }
    monkeypatch.setattr(srv, "get_db", lambda: _StubDB(doc))

    response = await srv.session_export("sid", "a", force=False)

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_export_refuses_duplicate_clean_per_file_results(monkeypatch, tmp_path):
    """Duplicate clean rows are stale report data, not certification."""
    import server as srv

    p = tmp_path / "export.txt"
    p.write_text("duplicate clean", encoding="utf-8")
    doc = {
        "id": "sid",
        "status": "complete",
        "export_paths": {"a": str(p)},
        "guard_report": {
            "status": "clean",
            "results": [
                {"file_id": "a", "status": "clean"},
                {"file_id": "a", "status": "clean"},
            ],
        },
    }
    db = _StubDB(doc)
    monkeypatch.setattr(srv, "get_db", lambda: db)

    response = await srv.session_export("sid", "a", force=False)

    assert response.status_code == 403
    assert db.updates == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "results",
    [
        [{"file_id": "a", "status": "skipped"}],
        [],
    ],
)
async def test_export_force_refuses_skipped_or_missing_results(monkeypatch, tmp_path, results):
    """Only explicitly blocked files may use the audited force override."""
    import server as srv

    p = tmp_path / "export.txt"
    p.write_text("not certified", encoding="utf-8")
    doc = {
        "id": "sid",
        "status": "complete",
        "export_paths": {"a": str(p)},
        "guard_report": {"status": "clean", "results": results},
    }
    db = _StubDB(doc)
    monkeypatch.setattr(srv, "get_db", lambda: db)

    response = await srv.session_export("sid", "a", force=True)

    assert response.status_code == 403
    assert db.updates == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "results",
    [
        [{"file_id": "a", "status": "blocked"}, {"file_id": "a", "status": "blocked"}],
        [{"file_id": "a", "status": "blocked"}, {"file_id": "a", "status": "clean"}],
        [{"file_id": "a", "status": "clean"}, {"file_id": "a", "status": "blocked"}],
    ],
)
async def test_export_force_refuses_duplicate_or_conflicting_results(monkeypatch, tmp_path, results):
    """Force cannot override malformed Guard results or create an audit record."""
    import server as srv

    p = tmp_path / "export.txt"
    p.write_text("ambiguous", encoding="utf-8")
    doc = {
        "id": "sid",
        "status": "complete",
        "export_paths": {"a": str(p)},
        "guard_report": {"status": "blocked", "results": results},
    }
    db = _StubDB(doc)
    monkeypatch.setattr(srv, "get_db", lambda: db)

    response = await srv.session_export("sid", "a", force=True)

    assert response.status_code == 403
    assert db.updates == []




@pytest.mark.asyncio
async def test_export_force_override_still_records_blocked_download(monkeypatch, tmp_path):
    """The audited override remains available only for blocked files."""
    import server as srv

    p = tmp_path / "blocked.txt"
    p.write_text("operator-reviewed", encoding="utf-8")
    doc = {
        "id": "sid",
        "status": "complete",
        "export_paths": {"a": str(p)},
        "guard_report": {
            "status": "blocked",
            "results": [{"file_id": "a", "status": "blocked"}],
        },
    }
    db = _StubDB(doc)
    monkeypatch.setattr(srv, "get_db", lambda: db)

    response = await srv.session_export("sid", "a", force=True, principal="op1")

    assert getattr(response, "status_code", 200) == 200
    assert len(db.updates) == 1
    args, kwargs = db.updates[0]
    assert args[0] == {"id": "sid", "owner": "op1"}
    assert kwargs == {}
    override = args[1]["$push"]["guard_overrides"]
    assert override["file_id"] == "a"
    assert override["overridden_at"]


def test_bundle_omits_unclean_and_unreported_exports(tmp_path):
    """Only per-file clean Guard results may enter the shareable bundle."""
    from phi_core.bundle import BundleOptions, build_bundle

    clean = tmp_path / "clean.csv"
    blocked = tmp_path / "blocked.csv"
    skipped = tmp_path / "skipped.csv"
    duplicate_clean = tmp_path / "duplicate_clean.csv"
    conflicted = tmp_path / "conflicted.csv"
    unreported = tmp_path / "unreported.csv"
    clean.write_text("safe", encoding="utf-8")
    blocked.write_text("unsafe", encoding="utf-8")
    skipped.write_text("unscanned", encoding="utf-8")
    duplicate_clean.write_text("ambiguous", encoding="utf-8")
    conflicted.write_text("ambiguous", encoding="utf-8")
    unreported.write_text("stale", encoding="utf-8")
    session = {
        "id": "sid",
        "export_paths": {
            "clean": str(clean),
            "blocked": str(blocked),
            "skipped": str(skipped),
            "duplicate_clean": str(duplicate_clean),
            "conflicted": str(conflicted),
            "unreported": str(unreported),
        },
        "files": [
            {"file_id": "clean", "kind": "dataset", "original_name": "clean.csv"},
            {"file_id": "blocked", "kind": "dataset", "original_name": "blocked.csv"},
            {"file_id": "skipped", "kind": "dataset", "original_name": "skipped.csv"},
            {
                "file_id": "duplicate_clean",
                "kind": "dataset",
                "original_name": "duplicate_clean.csv",
            },
            {"file_id": "conflicted", "kind": "dataset", "original_name": "conflicted.csv"},
            {"file_id": "unreported", "kind": "dataset", "original_name": "unreported.csv"},
        ],
        "guard_report": {
            "status": "clean",
            "results": [
                {"file_id": "clean", "status": "clean"},
                {"file_id": "blocked", "status": "blocked"},
                {"file_id": "skipped", "status": "skipped"},
                {"file_id": "duplicate_clean", "status": "clean"},
                {"file_id": "duplicate_clean", "status": "clean"},
                {"file_id": "conflicted", "status": "clean"},
                {"file_id": "conflicted", "status": "blocked"},
            ],
        },
    }

    data, _ = build_bundle(session, BundleOptions())

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
    assert "safe_to_share/datasets/clean.csv" in names
    assert "safe_to_share/datasets/blocked.csv" not in names
    assert "safe_to_share/datasets/unreported.csv" not in names
    assert "safe_to_share/datasets/skipped.csv" not in names
    assert "safe_to_share/datasets/conflicted.csv" not in names
    assert "safe_to_share/datasets/duplicate_clean.csv" not in names
