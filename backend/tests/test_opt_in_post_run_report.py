"""Phase 17-B: proof that the opt-in post-run publication report (Scout ->
Ledger -> Herald, ``phi_core.agents.outward.run_post_run_report``) genuinely
runs only on demand, never as part of ``execute_decisions``'s mandatory PHI
handling flow, and that the ``POST /api/sessions/{sid}/post-run-report``
route works correctly against an already-complete session's persisted state.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from phi_core.agents import orchestrator


def _matches(doc: dict, query: dict) -> bool:
    return all(doc.get(k) == v for k, v in query.items())


class _FakeCollection:
    """Minimal in-memory stand-in for an ``AsyncIOMotorCollection``, enough
    for ``MongoControlStore`` to read and write control-plane records
    (``capability_grants``, ``work_items``, ``trace_events``, ``agent_log``,
    ...) without a real Mongo connection. Mirrors the identical helper used
    in ``test_human_review_resume_execution.py``/
    ``test_certification_invalidation.py``."""

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
    """``.sessions`` is the single session document under test (single-
    session tests only need one); every other collection name (subscript
    or attribute, matching real Motor's ``db["x"]`` == ``db.x``) is a fresh
    ``_FakeCollection``."""

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


# ---------------------------------------------------------------------------
# (b) execute_decisions never touches Scout/Ledger/Herald
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_decisions_never_instantiates_scout_ledger_herald(monkeypatch):
    """A full, clean ``execute_decisions`` completion must never construct
    Scout, Ledger, or Herald. Phase 17-B relocated all three to the opt-in
    post-run report; ``orchestrator.py`` does not even import them any
    more, but this proves the runtime behavior directly (against the real
    ``phi_core.agents.outward`` module, not merely by grepping imports) --
    a spy on each class's ``__init__`` must never fire, and the produced
    result/completion-set must carry no ``scout``/``ledger``/``herald``/
    ``audit`` keys."""
    from phi_core.agents import outward

    constructed: list[str] = []

    class _SpyScout:
        def __init__(self, *_a, **_kw):
            constructed.append("Scout")

    class _SpyLedger:
        def __init__(self, *_a, **_kw):
            constructed.append("Ledger")

    class _SpyHerald:
        def __init__(self, *_a, **_kw):
            constructed.append("Herald")

    monkeypatch.setattr(outward, "Scout", _SpyScout)
    monkeypatch.setattr(outward, "Ledger", _SpyLedger)
    monkeypatch.setattr(outward, "Herald", _SpyHerald)

    class FakeExecutor:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx

        async def run(self, **_kw):
            return {"exports": {}}

    class FakeOperator:
        def __init__(self, *_a, **_kw):
            pass

        async def run(self, **_kw):
            return {"failed_file_ids": [], "verdicts": []}

    class FakeReviewer:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx

        async def run(self, decisions, operator_result, exports, omit_by_file=None):
            return {"exports": exports, "findings": []}

    monkeypatch.setattr(orchestrator, "Executor", FakeExecutor)
    monkeypatch.setattr(orchestrator, "DeterministicVerifier", FakeOperator)
    monkeypatch.setattr(orchestrator, "Reviewer", FakeReviewer)

    class _FakeSessions:
        async def find_one(self, *_a, **_kw):
            return None

        async def update_one(self, *_a, **_kw):
            return None

    class _FakeDb:
        sessions = _FakeSessions()

    class _FakeManager:
        async def consult(self, **_kw):
            return SimpleNamespace(action="continue")

        async def close_run(self, outcome):
            return {"outcome": outcome}

        async def _log(self, *_a, **_kw):
            return None

    class _FakeCtx:
        tasks = None
        sandbox = None

    async def make_ctx(_agent):
        return _FakeCtx()

    async def make_child_ctx(_agent, _parent_task_id):
        raise AssertionError(
            "execute_decisions must never build a child context for Scout/Ledger/Herald "
            "sub-agents -- they are opt-in post-run only (Phase 17-B)."
        )

    async def complete_and_accept(_ctx, _result):
        return True

    async def on_phase(_phase, _payload):
        return None

    async def close_last_phase():
        return None

    result = await orchestrator.execute_decisions(
        db=_FakeDb(), sid="s", session={}, session_filter={"id": "s"},
        files=[], decisions=[], statute={}, praxis_methods={},
        dictionary_by_column={}, make_ctx=make_ctx, make_child_ctx=make_child_ctx,
        complete_and_accept=complete_and_accept, manager=_FakeManager(),
        on_phase=on_phase, close_last_phase=close_last_phase,
        phase_timings={}, run_started=time.perf_counter(),
    )

    assert result["status"] == "complete"
    assert constructed == [], f"execute_decisions constructed unexpected opt-in agents: {constructed}"
    for key in ("scout", "ledger", "herald", "audit"):
        assert key not in result, f"execute_decisions result unexpectedly carries {key!r}"


# ---------------------------------------------------------------------------
# (a)/(c) the opt-in endpoint genuinely runs Scout+Ledger+Herald on demand,
# against an already-complete session's persisted decisions
# ---------------------------------------------------------------------------


async def _complete(ctx, result):
    if ctx is not None and getattr(ctx, "tasks", None) is not None:
        await ctx.tasks.complete(result)


def _fake_outward_agents(monkeypatch, calls: list[str]):
    """Patch Scout/Ledger/Herald on the real ``outward`` module (the one
    ``run_post_run_report`` calls into) with lightweight doubles that record
    invocation and echo back what they were given, so assertions can prove
    both "it ran" and "it ran with the right, deterministically-derived
    input" without a live LLM key. Each double completes its own (and, for
    Ledger/Herald, its sub-agents') durable task -- real ``Agent`` subclasses
    get this for free from ``Agent.__init_subclass__``; a bare test double
    standing in for one does not (see ``control.testing.complete_fake_task``),
    and an activation whose task is never completed keeps counting against
    ``MAX_PARALLEL_TASKS_PER_RUN`` for the rest of the test."""
    from phi_core.agents import outward

    class FakeScout:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx

        async def run(self):
            calls.append("Scout.run")
            result = {"systems": [{"name": "Presidio", "kind": "open"}], "summary": "landscape"}
            await _complete(self.ctx, result)
            return result

    class FakeLedger:
        def __init__(self, ctx=None, compare_ctx=None, aggregate_ctx=None, *, complete_and_accept=None):
            self.ctx = ctx
            self._ctxs = [c for c in (ctx, compare_ctx, aggregate_ctx) if c is not None]

        async def run(self, decisions, audit, scout, benchmark_result=None):
            calls.append("Ledger.run")
            result = {
                "headline": "ledger headline",
                "our_system": {"decision_counts": audit.get("metrics", {})},
                "comparisons": [],
                "metrics_narrative": "",
                "recommendations": [],
                "benchmark_result": benchmark_result,
                "_scout_seen": scout,
            }
            for c in self._ctxs:
                await _complete(c, result)
            return result

    class FakeHerald:
        def __init__(self, ctx=None, abstract_ctx=None, sections_ctx=None, *, complete_and_accept=None):
            self.ctx = ctx
            self._ctxs = [c for c in (ctx, abstract_ctx, sections_ctx) if c is not None]

        async def run(self, ledger, audit, target_venue="JAMIA Open"):
            calls.append("Herald.run")
            result = {"title": "draft", "abstract": "", "sections": [], "references": [],
                     "target_venue": target_venue, "alt_venues": [], "_ledger_seen": ledger.get("headline")}
            for c in self._ctxs:
                await _complete(c, result)
            return result

    monkeypatch.setattr(outward, "Scout", FakeScout)
    monkeypatch.setattr(outward, "Ledger", FakeLedger)
    monkeypatch.setattr(outward, "Herald", FakeHerald)


@pytest.mark.asyncio
async def test_post_run_report_endpoint_runs_scout_ledger_herald_on_demand(monkeypatch):
    import server as srv

    session_doc = {
        "id": "sid",
        "owner": "reviewer",
        "status": "complete",
        "_pipeline_run_id": "r" * 32,
        "target_venue": "Custom Venue",
        "agent_decisions": [
            {"file_id": "f1", "column": "dob", "action": "drop"},
            {"file_id": "f1", "column": "age", "action": "cap_age_90"},
            {"file_id": "f1", "column": "site", "action": "keep"},
            {"file_id": "f1", "column": "notes", "action": "human_review"},
        ],
    }
    db = _StubDB(session_doc)
    monkeypatch.setattr(srv, "get_db", lambda: db)

    async def fake_cfg():
        return SimpleNamespace(provider="anthropic", model="test", base_url="")

    monkeypatch.setattr(srv, "_current_llm_cfg", fake_cfg)

    calls: list[str] = []
    _fake_outward_agents(monkeypatch, calls)

    response = await srv.session_post_run_report("sid", principal="reviewer")

    # (a) it genuinely ran, in the right order, exactly once each.
    assert calls == ["Scout.run", "Ledger.run", "Herald.run"]

    # (c) it worked correctly against the persisted decisions: Ledger's
    # deterministic decision-count roll-up (via the synthesized audit
    # dict) reflects the session's real agent_decisions, and Scout's
    # output actually flowed into Ledger, and Ledger's into Herald.
    assert response["ledger"]["our_system"]["decision_counts"]["columns_dropped"] == 1
    assert response["ledger"]["our_system"]["decision_counts"]["columns_kept"] == 1
    assert response["ledger"]["our_system"]["decision_counts"]["human_review_required"] == 1
    assert response["ledger"]["_scout_seen"]["summary"] == "landscape"
    assert response["herald"]["_ledger_seen"] == "ledger headline"
    assert response["herald"]["target_venue"] == "Custom Venue"
    assert "generated_at" in response

    # Persisted onto the session document under the same keys `GET
    # .../results` already reads (`agent_scout`/`agent_ledger`/`agent_herald`).
    assert session_doc["agent_scout"]["summary"] == "landscape"
    assert session_doc["agent_ledger"]["headline"] == "ledger headline"
    assert session_doc["agent_herald"]["title"] == "draft"
    assert session_doc["post_run_report_generated_at"] == response["generated_at"]


@pytest.mark.asyncio
async def test_post_run_report_requires_a_completed_session(monkeypatch):
    """A session still mid-run (or never started) has nothing to report on
    yet; the route must refuse rather than run Scout/Ledger/Herald against
    incomplete/absent decisions."""
    import server as srv
    from fastapi import HTTPException

    session_doc = {
        "id": "sid", "owner": "reviewer", "status": "classifying",
        "_pipeline_run_id": "r" * 32, "agent_decisions": [],
    }
    db = _StubDB(session_doc)
    monkeypatch.setattr(srv, "get_db", lambda: db)

    calls: list[str] = []
    _fake_outward_agents(monkeypatch, calls)

    with pytest.raises(HTTPException) as excinfo:
        await srv.session_post_run_report("sid", principal="reviewer")

    assert excinfo.value.status_code == 403
    assert calls == [], "Scout/Ledger/Herald must never run for an incomplete session"


@pytest.mark.asyncio
async def test_post_run_report_requires_a_completed_pipeline_run(monkeypatch):
    """A session with no ``_pipeline_run_id`` (never actually run) has no
    completed run to report on, even if its status were somehow marked
    complete."""
    import server as srv
    from fastapi import HTTPException

    session_doc = {"id": "sid", "owner": "reviewer", "status": "complete", "agent_decisions": []}
    db = _StubDB(session_doc)
    monkeypatch.setattr(srv, "get_db", lambda: db)

    calls: list[str] = []
    _fake_outward_agents(monkeypatch, calls)

    with pytest.raises(HTTPException) as excinfo:
        await srv.session_post_run_report("sid", principal="reviewer")

    assert excinfo.value.status_code == 403
    assert calls == []
