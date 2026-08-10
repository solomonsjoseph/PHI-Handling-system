"""SEC-001 / SEC-002 / SEC-003 regression tests.

These lock down the guard-gated download boundary + corpus ground-truth
concealment. Any future refactor that reintroduces a fail-open path or
leaks the corpus answer key should fail these tests.
"""
from __future__ import annotations

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
    """Sibling reads carry require_api_token; corpus verify must match."""
    from server import app
    # Find the route function for GET /api/corpus/study/verify/{sid}
    matching = [
        r for r in app.router.routes
        if getattr(r, "path", "") == "/api/corpus/study/verify/{sid}"
    ]
    assert matching, "corpus verify route not registered"
    dep_fns = {d.call.__name__ for d in matching[0].dependant.dependencies}
    assert "require_api_token" in dep_fns, (
        "SEC-002: /api/corpus/study/verify/{sid} is not token-gated"
    )


# ---- SEC-001 -----------------------------------------------------------


class _StubDB:
    """Tiny stand-in Mongo doc-store for the download-gate tests."""
    def __init__(self, doc):
        self._doc = doc
        self.sessions = self

    async def find_one(self, *_args, **_kwargs):
        return self._doc

    async def update_one(self, *_args, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_bundle_refuses_when_guard_missing(monkeypatch):
    """Legacy /finalize used to skip the guard entirely; the /bundle
    endpoint must still refuse (fail-closed) when guard_report is absent."""
    from fastapi import HTTPException
    import server as srv
    doc = {"id": "sid", "status": "complete", "export_paths": {"a": "/tmp/x"}}
    monkeypatch.setattr(srv, "get_db", lambda: _StubDB(doc))
    with pytest.raises(HTTPException) as excinfo:
        await srv.session_bundle("sid", publication=False, attestation_pdf=False)
    assert excinfo.value.status_code == 403
    assert "not certified" in excinfo.value.detail.lower()


@pytest.mark.asyncio
async def test_bundle_refuses_when_guard_blocked(monkeypatch):
    from fastapi import HTTPException
    import server as srv
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
