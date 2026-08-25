"""Phase 4 step 6: rerun admission validation and run-fenced input cleanup."""
from __future__ import annotations

import hashlib

import pytest


@pytest.mark.asyncio
async def test_validate_rerun_inputs_passes_when_every_file_matches_its_recorded_hash(tmp_path):
    import server

    p = tmp_path / "clean.csv"
    p.write_text("a,b\n1,2\n", encoding="utf-8")
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    files = [{"file_id": "f1", "stored_path": str(p), "sha256": digest}]
    assert await server._validate_rerun_inputs(files) == []


@pytest.mark.asyncio
async def test_validate_rerun_inputs_flags_a_missing_file(tmp_path):
    import server

    files = [{"file_id": "f1", "stored_path": str(tmp_path / "gone.csv"), "sha256": "a" * 64}]
    assert await server._validate_rerun_inputs(files) == ["f1"]


@pytest.mark.asyncio
async def test_validate_rerun_inputs_flags_a_file_modified_since_intake(tmp_path):
    import server

    p = tmp_path / "changed.csv"
    p.write_text("a,b\n1,2\n", encoding="utf-8")
    original_digest = hashlib.sha256(p.read_bytes()).hexdigest()
    p.write_text("a,b\n1,TAMPERED\n", encoding="utf-8")
    files = [{"file_id": "f1", "stored_path": str(p), "sha256": original_digest}]
    assert await server._validate_rerun_inputs(files) == ["f1"]


@pytest.mark.asyncio
async def test_validate_rerun_inputs_reports_every_stale_file_not_only_the_first(tmp_path):
    import server

    good = tmp_path / "good.csv"
    good.write_text("a\n1\n", encoding="utf-8")
    good_digest = hashlib.sha256(good.read_bytes()).hexdigest()
    files = [
        {"file_id": "f1", "stored_path": str(good), "sha256": good_digest},
        {"file_id": "f2", "stored_path": str(tmp_path / "missing-a.csv"), "sha256": "a" * 64},
        {"file_id": "f3", "stored_path": str(tmp_path / "missing-b.csv"), "sha256": "b" * 64},
    ]
    assert await server._validate_rerun_inputs(files) == ["f2", "f3"]


@pytest.mark.asyncio
async def test_validate_rerun_inputs_skips_entries_with_no_hash_recorded():
    """A file entry with no `sha256` (should not happen post-intake, but
    must never crash) is neither flagged stale nor raises."""
    import server

    assert await server._validate_rerun_inputs([{"file_id": "f1", "stored_path": ""}]) == []


class _FencedFakeSessions:
    """Simulates a `_pipeline_run_id`-filtered `update_one` that a stale,
    already-superseded run's worker loses: the filter never matches once a
    newer run has claimed the session."""

    def __init__(self, matches: bool):
        self._matches = matches
        self.updates: list[dict] = []

    async def update_one(self, query, update):
        self.updates.append({"query": query, "update": update})
        from types import SimpleNamespace
        return SimpleNamespace(matched_count=1 if self._matches else 0)


class _FencedFakeDb:
    def __init__(self, matches: bool):
        self.sessions = _FencedFakeSessions(matches)


@pytest.mark.asyncio
async def test_fail_session_correlated_cleans_up_when_its_own_run_still_matches(monkeypatch):
    import server

    cleanup_calls: list[str] = []
    monkeypatch.setattr(server, "cleanup_session_unpacked", lambda sid: cleanup_calls.append(sid))

    async def _noop_emit(*_a, **_kw):
        return None
    monkeypatch.setattr(server, "_emit", _noop_emit)

    db = _FencedFakeDb(matches=True)
    await server._fail_session_correlated(
        db, "session-1", {"id": "session-1", "_pipeline_run_id": "run-a"}, RuntimeError("boom"), run_id="run-a",
    )
    assert cleanup_calls == ["session-1"]


@pytest.mark.asyncio
async def test_fail_session_correlated_skips_cleanup_when_a_newer_run_already_superseded_it(monkeypatch):
    """A stale worker from `run-a` crashes after the session has already
    moved on to `run-b`; its run-filtered update matches nothing, and it
    must not delete `run-b`'s unpacked input tree."""
    import server

    cleanup_calls: list[str] = []
    monkeypatch.setattr(server, "cleanup_session_unpacked", lambda sid: cleanup_calls.append(sid))

    async def _noop_emit(*_a, **_kw):
        return None
    monkeypatch.setattr(server, "_emit", _noop_emit)

    db = _FencedFakeDb(matches=False)
    await server._fail_session_correlated(
        db, "session-1", {"id": "session-1", "_pipeline_run_id": "run-a"}, RuntimeError("boom"), run_id="run-a",
    )
    assert cleanup_calls == []
