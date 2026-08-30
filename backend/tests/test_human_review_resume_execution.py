"""End-to-end proof that `session_human_review`'s resume worker actually
drives `orchestrator.execute_decisions` to completion -- the extraction
Phase 4 step 4 introduced (deleting `_run_tail` in favor of the same
shared tail a fresh run uses). Every other `test_human_review_invariant.py`
test only exercises the synchronous decision-resolution half of the route;
the background `asyncio.create_task(worker())` it launches was never
actually awaited to completion anywhere until this file.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest


def _matches(doc: dict, query: dict) -> bool:
    """Enough of Mongo's query language for this route's own filters:
    plain equality, `$in`, `$nin`, and `$exists`."""
    for key, expected in query.items():
        actual = doc.get(key)
        if isinstance(expected, dict) and set(expected) <= {"$in", "$nin", "$exists"}:
            if "$in" in expected and actual not in expected["$in"]:
                return False
            if "$nin" in expected and actual in expected["$nin"]:
                return False
            if "$exists" in expected and (key in doc) != expected["$exists"]:
                return False
        elif actual != expected:
            return False
    return True


class _FakeCollection:
    """Minimal in-memory stand-in for an ``AsyncIOMotorCollection``, enough
    for ``MongoControlStore`` to read and write control-plane records
    (``capability_grants``, ``work_items``, ``trace_events``, ``agent_log``,
    ``gate_results``, ...) without a real Mongo connection."""

    def __init__(self):
        self.docs: list[dict] = []

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def find_one(self, query, *_a, **_kw):
        for d in self.docs:
            if _matches(d, query):
                return dict(d)
        return None

    def find(self, query):
        async def _cursor():
            for d in self.docs:
                if _matches(d, query):
                    yield dict(d)
        return _cursor()

    async def replace_one(self, query, replacement):
        for i, d in enumerate(self.docs):
            if _matches(d, query):
                self.docs[i] = dict(replacement)
                return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)

    async def update_one(self, query, update):
        for d in self.docs:
            if _matches(d, query):
                d.update(update.get("$set", {}))
                for key in update.get("$unset", {}):
                    d.pop(key, None)
                return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)

    async def delete_one(self, query):
        for i, d in enumerate(self.docs):
            if _matches(d, query):
                del self.docs[i]
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)


class _StubDB:
    """`.sessions` is the session document itself (single-session tests
    only need one); every other collection name (subscript or attribute,
    matching real Motor's `db["x"]` == `db.x`) is a fresh `_FakeCollection`."""

    def __init__(self, doc: dict):
        self.doc = doc
        self.sessions = self
        self.updates: list[dict] = []
        self._collections: dict[str, _FakeCollection] = {}

    def __getitem__(self, name: str) -> _FakeCollection:
        return self._collections.setdefault(name, _FakeCollection())

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]

    async def find_one(self, query, *_a, **_kw):
        if _matches(self.doc, query):
            return dict(self.doc)
        return None

    async def update_one(self, query, update):
        if not _matches(self.doc, query):
            return SimpleNamespace(matched_count=0)
        self.updates.append(dict(update.get("$set", {})))
        self.doc.update(update.get("$set", {}))
        for key in update.get("$unset", {}):
            self.doc.pop(key, None)
        return SimpleNamespace(matched_count=1)


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> str:
    import csv
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_session_human_review_resume_worker_runs_execute_decisions_to_completion(tmp_path, monkeypatch):
    import server as srv
    from phi_core.agents import orchestrator
    from phi_core.control.records import WorkItem
    from phi_core.control.store import MongoControlStore

    src = tmp_path / "dm.csv"
    sha = _write_csv(src, ["id", "field"], [["1", "x"], ["2", "y"]])

    session_doc = {
        "id": "a" * 32,
        "owner": "alice",
        "status": "awaiting_human_review",
        "jurisdiction": "us",
        "files": [{"file_id": "f1", "kind": "dataset", "subtype": "csv",
                   "stored_path": str(src), "sha256": sha, "columns": ["id", "field"]}],
        "agent_decisions": [
            {"file_id": "f1", "column": "id", "action": "drop", "phi_category": "A",
             "citation": "45 CFR 164.514(b)(2)(i)(A)", "confidence": 0.95, "reason": "identifier"},
            {"file_id": "f1", "column": "field", "action": "human_review",
             "suggested_action": "keep", "suggested_reason": "clinical value",
             "phi_category": "R", "confidence": 0.5, "reason": "needs review"},
        ],
        "agent_specialists": {"lexicon": {"columns": []}},
        "agent_statute": {},
        "agent_praxis": {},
        "dataset_file_downloads": [{"file_id": "f1", "downloaded_by": "alice", "decision_version": 0}],
    }
    db = _StubDB(session_doc)
    monkeypatch.setattr(srv, "get_db", lambda: db)


    async def _complete(ctx, result):
        if ctx is not None and ctx.tasks is not None:
            await ctx.tasks.complete(result)

    class FakeExecutor:
        def __init__(self, ctx=None, *_a, **_kw):
            self._ctx = ctx

        async def run(self, files, decisions, omit_by_file=None, *, manifest=None, store=None):
            dst = tmp_path / "f1_export.csv"
            dst.write_text("field\nx\ny\n", encoding="utf-8")
            result = {"exports": {"f1": str(dst)}}
            await _complete(self._ctx, result)
            return result

    class FakeOperator:
        def __init__(self, ctx=None, *_a, **_kw):
            self._ctx = ctx

        async def run(self, files, decisions, exports, omit_by_file=None, sandbox=None):
            result = {"failed_file_ids": [], "verdicts": []}
            await _complete(self._ctx, result)
            return result

    class FakeReviewer:
        def __init__(self, ctx=None, *_a, **_kw):
            self._ctx = ctx

        async def run(self, decisions, operator_result, exports, omit_by_file=None):
            result = {"exports": exports, "findings": []}
            await _complete(self._ctx, result)
            return result

    class FakeAuditor:
        def __init__(self, ctx=None, *_a, **_kw):
            self._ctx = ctx

        async def _log(self, *_a, **_kw):
            return None

        async def run(self, **_kw):
            result = {"verdict": "clean", "issues": [], "metrics": {}, "confidence": 1.0, "summary": "ok"}
            await _complete(self._ctx, result)
            return result

    class FakeScout:
        def __init__(self, ctx=None, *_a, **_kw):
            self._ctx = ctx

        async def _log(self, *_a, **_kw):
            return None

        async def run(self, **_kw):
            await _complete(self._ctx, {})
            return {}

    class FakeLedger:
        def __init__(self, ctx=None, compare_ctx=None, aggregate_ctx=None, **_kw):
            self._ctxs = [c for c in (ctx, compare_ctx, aggregate_ctx) if c is not None]

        async def run(self, **_kw):
            result = {"summary": "ledger"}
            for c in self._ctxs:
                await _complete(c, result)
            return result

    class FakeHerald:
        def __init__(self, ctx=None, abstract_ctx=None, sections_ctx=None, **_kw):
            self._ctxs = [c for c in (ctx, abstract_ctx, sections_ctx) if c is not None]

        async def run(self, **_kw):
            result = {"abstract": "herald"}
            for c in self._ctxs:
                await _complete(c, result)
            return result

    monkeypatch.setattr(orchestrator, "Executor", FakeExecutor)
    monkeypatch.setattr(orchestrator, "DeterministicVerifier", FakeOperator)
    monkeypatch.setattr(orchestrator, "Reviewer", FakeReviewer)
    monkeypatch.setattr(orchestrator, "Auditor", FakeAuditor)
    monkeypatch.setattr(orchestrator, "Scout", FakeScout)
    monkeypatch.setattr(orchestrator, "Ledger", FakeLedger)
    monkeypatch.setattr(orchestrator, "Herald", FakeHerald)

    body = srv.HumanReviewSubmit(
        resolutions=[{"file_id": "f1", "column": "field", "mode": "approve"}],
        client_event_id="ce-approve-a",
        comment="",
        actual_knowledge_ack=True,
    )
    resp = await srv.session_human_review("a" * 32, body, "alice")
    assert resp["status"] == "resuming"  # Phase 8: result also carries typed human_decisions (docs #46)
    assert len(db["work_items"].docs) == 1

    # `session_human_review` only enqueues now (Phase 4 step 2/4); drive
    # the enqueued `pipeline_resume` task directly through the same
    # handler a `Worker` instance would claim and dispatch to.
    work_item = WorkItem.model_validate(db["work_items"].docs[0])
    await srv._handle_pipeline_resume(MongoControlStore(db), work_item)

    # The final state proves the shared `execute_decisions` tail actually
    # ran: Ledger/Herald outputs landed, status reflects a clean run with
    # no columns still pending, and the resume-specific bookkeeping
    # (`session_review`, `pending_review`, `human_review_required`) that
    # only `execute_decisions`'s `extra_completion_fields` merge produces
    # is present.
    assert session_doc["status"] == "complete"
    assert session_doc["agent_ledger"] == {"summary": "ledger"}
    assert session_doc["agent_herald"] == {"abstract": "herald"}
    assert session_doc["export_paths"] == {"f1": str(tmp_path / "f1_export.csv")}
    assert session_doc["pending_review"] == []
    assert session_doc["human_review_required"] is False
    assert len(session_doc["session_review"]) == 1
    assert session_doc["session_review"][0]["reviewer"] == "alice"
    assert session_doc["session_review"][0]["resolved_columns"] == [{"file_id": "f1", "column": "field"}]


@pytest.mark.asyncio
async def test_session_human_review_resume_worker_leaves_partially_complete_when_a_column_stays_deferred(tmp_path, monkeypatch):
    """One column approved, a second explicitly deferred: the resume must
    still execute (Executor/Operator/Reviewer run against the resolved
    column via `omit_by_file`), but the final status must be
    `partially_complete`, not `complete` -- exactly the property that
    made `_run_tail` a separate implementation from the fresh path before
    this extraction shared `omit_by_file` through `execute_decisions`."""
    import server as srv
    from phi_core.agents import orchestrator
    from phi_core.control.records import WorkItem
    from phi_core.control.store import MongoControlStore

    src = tmp_path / "dm.csv"
    sha = _write_csv(src, ["a", "b"], [["1", "x"], ["2", "y"]])

    session_doc = {
        "id": "b" * 32,
        "owner": "alice",
        "status": "awaiting_human_review",
        "jurisdiction": "us",
        "files": [{"file_id": "f1", "kind": "dataset", "subtype": "csv",
                   "stored_path": str(src), "sha256": sha, "columns": ["a", "b"]}],
        "agent_decisions": [
            {"file_id": "f1", "column": "a", "action": "human_review",
             "suggested_action": "keep", "suggested_reason": "clinical value",
             "phi_category": "R", "confidence": 0.5, "reason": "needs review"},
            {"file_id": "f1", "column": "b", "action": "human_review",
             "suggested_action": "drop", "suggested_reason": "identifier",
             "phi_category": "A", "confidence": 0.5, "reason": "needs review"},
        ],
        "agent_specialists": {"lexicon": {"columns": []}},
        "agent_statute": {},
        "agent_praxis": {},
        "dataset_file_downloads": [{"file_id": "f1", "downloaded_by": "alice", "decision_version": 0}],
    }
    db = _StubDB(session_doc)
    monkeypatch.setattr(srv, "get_db", lambda: db)

    executor_calls: list[dict] = []

    async def _complete(ctx, result):
        if ctx is not None and ctx.tasks is not None:
            await ctx.tasks.complete(result)

    class FakeExecutor:
        def __init__(self, ctx=None, *_a, **_kw):
            self._ctx = ctx

        async def run(self, files, decisions, omit_by_file=None, *, manifest=None, store=None):
            executor_calls.append({"decisions": decisions, "omit_by_file": omit_by_file})
            dst = tmp_path / "f1_export.csv"
            dst.write_text("a\nx\ny\n", encoding="utf-8")
            result = {"exports": {"f1": str(dst)}}
            await _complete(self._ctx, result)
            return result

    class FakeOperator:
        def __init__(self, ctx=None, *_a, **_kw):
            self._ctx = ctx

        async def run(self, files, decisions, exports, omit_by_file=None, sandbox=None):
            result = {"failed_file_ids": [], "verdicts": []}
            await _complete(self._ctx, result)
            return result

    class FakeReviewer:
        def __init__(self, ctx=None, *_a, **_kw):
            self._ctx = ctx

        async def run(self, decisions, operator_result, exports, omit_by_file=None):
            result = {"exports": exports, "findings": []}
            await _complete(self._ctx, result)
            return result

    class FakeAuditor:
        def __init__(self, ctx=None, *_a, **_kw):
            self._ctx = ctx

        async def _log(self, *_a, **_kw):
            return None

        async def run(self, **_kw):
            result = {"verdict": "clean", "issues": [], "metrics": {}, "confidence": 1.0, "summary": "ok"}
            await _complete(self._ctx, result)
            return result

    class FakeScout:
        def __init__(self, ctx=None, *_a, **_kw):
            self._ctx = ctx

        async def _log(self, *_a, **_kw):
            return None

        async def run(self, **_kw):
            await _complete(self._ctx, {})
            return {}

    class FakeLedger:
        def __init__(self, ctx=None, compare_ctx=None, aggregate_ctx=None, **_kw):
            self._ctxs = [c for c in (ctx, compare_ctx, aggregate_ctx) if c is not None]

        async def run(self, **_kw):
            for c in self._ctxs:
                await _complete(c, {})
            return {}

    class FakeHerald:
        def __init__(self, ctx=None, abstract_ctx=None, sections_ctx=None, **_kw):
            self._ctxs = [c for c in (ctx, abstract_ctx, sections_ctx) if c is not None]

        async def run(self, **_kw):
            for c in self._ctxs:
                await _complete(c, {})
            return {}

    monkeypatch.setattr(orchestrator, "Executor", FakeExecutor)
    monkeypatch.setattr(orchestrator, "DeterministicVerifier", FakeOperator)
    monkeypatch.setattr(orchestrator, "Reviewer", FakeReviewer)
    monkeypatch.setattr(orchestrator, "Auditor", FakeAuditor)
    monkeypatch.setattr(orchestrator, "Scout", FakeScout)
    monkeypatch.setattr(orchestrator, "Ledger", FakeLedger)
    monkeypatch.setattr(orchestrator, "Herald", FakeHerald)

    body = srv.HumanReviewSubmit(
        resolutions=[
            {"file_id": "f1", "column": "a", "mode": "approve"},
            {"file_id": "f1", "column": "b", "mode": "defer"},
        ],
        client_event_id="ce-approve-b",
        comment="",
        actual_knowledge_ack=True,
    )
    resp = await srv.session_human_review("b" * 32, body, "alice")
    assert resp["status"] == "resuming"  # Phase 8: result also carries typed human_decisions (docs #46)
    assert len(db["work_items"].docs) == 1
    work_item = WorkItem.model_validate(db["work_items"].docs[0])
    await srv._handle_pipeline_resume(MongoControlStore(db), work_item)

    assert len(executor_calls) == 1
    # Only the resolved column reaches Executor; the deferred one is
    # excluded from `decisions` and named in `omit_by_file` instead.
    assert {d["column"] for d in executor_calls[0]["decisions"]} == {"a"}
    assert executor_calls[0]["omit_by_file"] == {"f1": {"b"}}

    assert session_doc["status"] == "partially_complete"
    assert session_doc["pending_review"] == [{"file_id": "f1", "column": "b"}]
    assert session_doc["human_review_required"] is True
