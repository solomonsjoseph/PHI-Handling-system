"""Phase 14 (scale and resilience): bounded concurrency under load.

Batch classification (Judge, via ``agents.batching.run_batched``, shared
with Operator/Reviewer), parallel research (RegulationsExpert +
PHIMethodsExpert), and research deduplication (one PHIMethodsExpert call
per distinct HIPAA category, however many columns share it) already have
correctness coverage elsewhere (``test_control_bounds.py``,
``test_architecture_boundaries.py``, ``test_operator.py``,
``test_demand_driven_research.py``). This file does not repeat those --
it proves each mechanism's bound and dedup guarantee still hold *at
scale* (many more items/columns/categories than those files exercise, and
in the concurrency cases, with unmodified production-default ``limits``
values rather than shrunk-for-isolation test values), and additionally
proves genuine parallelism (peak concurrent overlap > 1), which none of
the existing files assert.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest
from phi_core.agents.batching import run_batched
from phi_core.agents.llm import LlmConfig
from phi_core.control import limits
from phi_core.control.policy import MANIFESTS, CapabilityDenied
from phi_core.control.records import ResourceBudget
from phi_core.control.store import MemoryControlStore
from phi_core.control.testing import complete_fake_task, start_test_run

SESSION_ID = "c" * 32
_COLUMNS = [f"col{i}" for i in range(30)]
# 30 columns, only 5 distinct HIPAA categories -- 6x duplication.
_CATEGORY_CYCLE = ["A", "B", "C", "D", "E"]


def _pipeline_files() -> list[dict]:
    return [{"kind": "dataset", "file_id": "f1", "columns": _COLUMNS}]


def _decision(column: str, *, category: str) -> dict:
    return {
        "file_id": "f1", "column": column, "action": "keep", "phi_category": category,
        "confidence": 0.9, "reason": f"{column} decision", "subject": "participant",
        "citation": f"45 CFR 164.514(b)(2)(i)({category})",
    }


def _decisions_at_scale() -> list[dict]:
    return [
        _decision(column, category=_CATEGORY_CYCLE[i % len(_CATEGORY_CYCLE)])
        for i, column in enumerate(_COLUMNS)
    ]


def _drive_research_scale(monkeypatch, *, regulations_expert_cls, phi_methods_expert_cls):
    """A leaner, column-count-parameterised sibling of
    ``test_demand_driven_research._drive_pipeline``: drives the real
    ``orchestrator.run_pipeline`` end to end with ``_COLUMNS``/
    ``_decisions_at_scale()`` (30 columns, 5 distinct categories) instead
    of that file's fixed 4-column fixture, so the dedup and concurrency
    assertions below exercise a genuinely larger fan-in than the existing
    2-column case ever does. No live network, no live server: every
    specialist and the LLM-facing agents are monkeypatched fakes, exactly
    matching the house style ``test_demand_driven_research.py`` documents.
    """
    from phi_core.agents import orchestrator

    class FakeLexicon:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx

        async def run(self, **_kw):
            return await complete_fake_task(self.ctx, {"columns": []})

    class FakeInstrument(FakeLexicon):
        async def run(self, **_kw):
            return await complete_fake_task(self.ctx, {"fields": []})

    class FakeSchema(FakeLexicon):
        pass

    decisions = _decisions_at_scale()

    class FakeJudge:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx
            self.call_failures = 0
            self.last_message_id = None

        async def run(self, **_kw):
            return await complete_fake_task(self.ctx, {"decisions": decisions})

    class FakeReviewer:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx
            self.call_failures = 0

        async def _log(self, *_a, **_kw):
            return None

        async def preview(self, **_kw):
            return await complete_fake_task(self.ctx, {"issues": [{
                "file_id": "f1", "column": _COLUMNS[0],
                "severity": "blocking", "problem": "policy review needed",
            }]})

    monkeypatch.setattr(orchestrator, "Lexicon", FakeLexicon)
    monkeypatch.setattr(orchestrator, "Instrument", FakeInstrument)
    monkeypatch.setattr(orchestrator, "Schema", FakeSchema)
    monkeypatch.setattr(orchestrator, "Judge", FakeJudge)
    monkeypatch.setattr(orchestrator, "RegulationsExpert", regulations_expert_cls)
    monkeypatch.setattr(orchestrator, "PHIMethodsExpert", phi_methods_expert_cls)
    monkeypatch.setattr(orchestrator, "Reviewer", FakeReviewer)

    class FakeSessions:
        async def find_one(self, *_a, **_kw):
            return None

        async def update_one(self, *_a, **_kw):
            return None

    class FakeAgentLog:
        async def insert_one(self, *_a, **_kw):
            return None

    class FakeDb:
        def __init__(self):
            self.sessions = FakeSessions()
            self.agent_log = FakeAgentLog()

    async def emit(_msg):
        return None

    async def on_phase(_phase, _payload):
        return None

    async def _go():
        store = MemoryControlStore()
        await start_test_run(store, "session")
        return await orchestrator.run_pipeline(
            {"id": "session", "files": _pipeline_files()},
            FakeDb(), LlmConfig(provider="anthropic", model="test", max_tokens=100),
            emit, on_phase, control_store=store,
        )

    return asyncio.run(_go())


# ---- research deduplication at scale ---------------------------------------


def test_phi_methods_expert_dedup_holds_at_scale_many_columns_few_categories(monkeypatch) -> None:
    """30 columns collapse to exactly 5 distinct HIPAA categories
    (docs section 33/89's dedup contract): PHIMethodsExpert.method_for
    is called exactly 5 times, RegulationsExpert.run exactly once,
    regardless of the 6x duplication factor. Widens
    ``test_demand_driven_research.py``'s own 2-column/1-category proof
    to a realistic wide-dataset scale."""
    method_calls: list[str] = []
    regulations_calls: list[int] = []

    class CountingRegulationsExpert:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx

        async def _log(self, *_a, **_kw):
            return None

        async def run(self, **_kw):
            regulations_calls.append(1)
            return await complete_fake_task(self.ctx, {
                "regulation": "HIPAA Safe Harbor", "citation": "45 CFR 164.514",
                "handling_rules": [], "sources": [],
            })

    class CountingPHIMethodsExpert:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx

        async def _log(self, *_a, **_kw):
            return None

        async def method_for(self, category):
            method_calls.append(category)
            return {"category": category, "methods": []}

    _drive_research_scale(
        monkeypatch,
        regulations_expert_cls=CountingRegulationsExpert,
        phi_methods_expert_cls=CountingPHIMethodsExpert,
    )

    assert sorted(method_calls) == sorted(_CATEGORY_CYCLE)
    assert len(regulations_calls) == 1


def test_regulations_and_phi_methods_experts_run_genuinely_concurrently(monkeypatch) -> None:
    """``_dispatch_demand_driven_research`` launches RegulationsExpert and
    every PHIMethodsExpert category call side by side (``asyncio.gather``/
    ``asyncio.create_task``) -- this proves that concurrency is real, not
    accidental interleaving: every one of the 6 research calls (1
    RegulationsExpert + 5 PHIMethodsExpert categories) must be in flight
    at once at some point, not serialized end to end. Asserts on peak
    concurrent overlap and on how tightly clustered the six *start*
    timestamps are (robust to unrelated CPU-bound work elsewhere in the
    faked pipeline stretching the calls' own *end* timestamps, which a
    plain total-wall-clock assertion would not be)."""
    lock = threading.Lock()
    active = 0
    max_active = 0
    start_times: list[float] = []
    _SLEEP_S = 0.05
    t0 = time.perf_counter()

    def _enter() -> None:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            start_times.append(time.perf_counter() - t0)

    def _exit() -> None:
        nonlocal active
        with lock:
            active -= 1

    class SlowRegulationsExpert:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx

        async def _log(self, *_a, **_kw):
            return None

        async def run(self, **_kw):
            _enter()
            try:
                await asyncio.sleep(_SLEEP_S)
                return await complete_fake_task(self.ctx, {
                    "regulation": "HIPAA Safe Harbor", "citation": "45 CFR 164.514",
                    "handling_rules": [], "sources": [],
                })
            finally:
                _exit()

    class SlowPHIMethodsExpert:
        def __init__(self, ctx=None, *_a, **_kw):
            self.ctx = ctx

        async def _log(self, *_a, **_kw):
            return None

        async def method_for(self, category):
            _enter()
            try:
                await asyncio.sleep(_SLEEP_S)
                return {"category": category, "methods": []}
            finally:
                _exit()

    _drive_research_scale(
        monkeypatch,
        regulations_expert_cls=SlowRegulationsExpert,
        phi_methods_expert_cls=SlowPHIMethodsExpert,
    )

    assert len(start_times) == 1 + len(_CATEGORY_CYCLE)  # 1 regulations + 5 category calls
    # Peak overlap: at least 2 of the 6 research calls were genuinely
    # in flight at the same instant, not merely dispatched in quick
    # succession one after another.
    assert max_active >= 2
    # All 6 start timestamps land in one tight cluster, well under one
    # _SLEEP_S apart from the first -- a serialized implementation
    # (each call waiting for the previous one's full _SLEEP_S to finish
    # before starting) would spread these out by multiples of _SLEEP_S.
    assert max(start_times) - min(start_times) < _SLEEP_S


# ---- bounded concurrency composed at production-default scale -------------


@pytest.mark.asyncio
async def test_per_parent_and_run_wide_ceilings_compose_under_load_at_production_defaults(monkeypatch) -> None:
    """Uses the real, unmodified ``limits`` values (no monkeypatching down
    to a tiny number for isolation, unlike every existing per-bound test
    in ``test_control_bounds.py``): spreads concurrent ``create_child_work``
    calls across enough distinct parents that the run-wide live-task
    ceiling (``MAX_PARALLEL_TASKS_PER_RUN``) is reached before any single
    parent's own ceiling (``MAX_PARALLEL_TASKS_PER_PARENT``) would be,
    proving the two bounds compose correctly (whichever is tighter wins)
    under genuine concurrent load, not just individually."""
    from phi_core.control import manager as manager_module
    from phi_core.control import policy as policy_module
    from phi_core.control.manager import Manager
    from phi_core.control.policy import CapabilityPolicy
    from phi_core.control.tasks import TaskService

    # Executor's own manifest (unlike Ledger/Herald, which need a real
    # provider) carries `providers=frozenset()`, so `CapabilityPolicy(None)`
    # can issue its grants with no LlmConfig -- exactly the property
    # `test_control_tasks.py` documents for using "executor" task types in
    # a provider-free rig. It has no `allowed_child_task_types` of its own
    # (a real leaf agent), so this test grants it one self-referential
    # entry ("executor" children of an "executor" parent) purely to build
    # a genuine two-level parent/grandchild tree for the compose proof
    # below -- the same monkeypatched-``MANIFESTS`` technique
    # `test_control_bounds.py`/`test_architecture_boundaries.py` already
    # use, never a change to the real manifest file.
    patched = {**MANIFESTS, "Executor": MANIFESTS["Executor"].model_copy(
        update={"allowed_child_task_types": frozenset({"executor"}), "max_children": 100},
    )}
    monkeypatch.setattr(manager_module, "MANIFESTS", patched)
    monkeypatch.setattr(policy_module, "MANIFESTS", patched)

    assert limits.MAX_PARALLEL_TASKS_PER_PARENT < limits.MAX_PARALLEL_TASKS_PER_RUN, (
        "this test's whole point is exercising the run-wide ceiling before any "
        "single parent's ceiling -- requires the real defaults to keep that shape"
    )

    store = MemoryControlStore()
    tasks = TaskService(store, CapabilityPolicy(None))
    orch = Manager(store, tasks)
    run = await orch.start_run(session_id=SESSION_ID, principal="operator-1")
    root_docs = await store.find_many("work_items", {"run_id": run.run_id})
    root_task_id = root_docs[0]["task_id"]

    # Enough distinct parents that, if every parent could reach its own
    # per-parent ceiling, the total would exceed MAX_PARALLEL_TASKS_PER_RUN
    # -- forcing the run-wide bound to be the one that actually governs.
    n_parents = (limits.MAX_PARALLEL_TASKS_PER_RUN // limits.MAX_PARALLEL_TASKS_PER_PARENT) + 2
    parent_ids = []
    for _ in range(n_parents):
        child = await orch.create_child_work(
            run_id=run.run_id, parent_task_id=root_task_id, task_type="executor",
            input_ref={}, budget=ResourceBudget(),
        )
        parent_ids.append(child.task_id)

    async def _attempt(parent_id: str) -> bool:
        try:
            await orch.create_child_work(
                run_id=run.run_id, parent_task_id=parent_id, task_type="executor",
                input_ref={}, budget=ResourceBudget(),
            )
            return True
        except CapabilityDenied:
            return False

    # Each parent attempts up to MAX_PARALLEL_TASKS_PER_PARENT grandchildren,
    # all fired concurrently across all parents at once.
    attempts = [
        parent_id
        for parent_id in parent_ids
        for _ in range(limits.MAX_PARALLEL_TASKS_PER_PARENT)
    ]
    results = await asyncio.gather(*[_attempt(p) for p in attempts])

    # The run already holds `n_parents` live tasks (the parents themselves)
    # plus the root -- MAX_PARALLEL_TASKS_PER_RUN counts every live task in
    # the run, parents included, so the number of grandchildren that can
    # still be admitted is bounded by whatever headroom remains under that
    # run-wide ceiling, never by the (looser, per-parent) MAX_PARALLEL_TASKS_PER_PARENT
    # ceiling alone.
    live_after = await store.find_many("work_items", {"run_id": run.run_id})
    live_non_terminal = [
        t for t in live_after
        if t.get("state") not in ("succeeded", "failed", "cancelled", "rejected", "superseded")
    ]
    assert len(live_non_terminal) <= limits.MAX_PARALLEL_TASKS_PER_RUN
    admitted = sum(results)
    assert admitted == limits.MAX_PARALLEL_TASKS_PER_RUN - n_parents - 1  # -1 for the root task
    assert admitted < limits.MAX_PARALLEL_TASKS_PER_PARENT * n_parents  # the run-wide ceiling actually bit


# ---- batch classification/review scaling under realistic load -------------


@pytest.mark.asyncio
async def test_run_batched_wallclock_scales_with_pool_and_batch_size_under_operator_scale_load() -> None:
    """``run_batched``'s own docstring claims wall-clock scales with
    ``total_checks / (pool_size * batch_size)``. ``test_operator.py``'s
    ``test_pool_size_bounds_and_reaches_concurrent_batches`` proves the
    concurrency ceiling is exact at a 9-item scale; this proves the
    *scaling claim itself* holds at Operator's real defaults
    (``batch_size=8``, ``pool_size=6``) with a load large enough
    (240 items -> 30 batches -> 5 waves) that a serialized implementation
    (or a broken semaphore that only ever runs 1 batch at a time) would
    show up as a wall-clock multiple of the true bound, not a rounding
    artifact."""
    _SLEEP_S = 0.03
    items = list(range(240))
    batch_size = 8
    pool_size = 6
    expected_waves = len(items) / batch_size / pool_size  # 240/8/6 = 5 waves

    def check(batch: list[int]) -> list[dict]:
        time.sleep(_SLEEP_S)
        return [{"value": v, "verdict": "pass"} for v in batch]

    started = time.perf_counter()
    result = await run_batched(items, check, batch_size=batch_size, pool_size=pool_size)
    elapsed = time.perf_counter() - started

    assert [r["value"] for r in result] == items
    # A fully serial (batch_size=1-equivalent) run would take 30 * _SLEEP_S;
    # bounded concurrency keeps it near expected_waves * _SLEEP_S. Generous
    # slack (3x) absorbs scheduler jitter without hiding a real regression
    # (a serial implementation would be ~6x over, not ~3x).
    assert elapsed < expected_waves * _SLEEP_S * 3
