"""Tests for the shared bounded worker-pool batching helper (Task 26).

The Operator agent this file used to also test (Task 27) was retired in
Phase 10 (docs #54: "Operator -> migrate useful deterministic
verification into DeterministicVerifier then remove"); its migrated
tests now live in ``tests/test_deterministic_verifier.py``. The Task 28/
30 full-pipeline proofs below never referenced the ``Operator`` class
directly (they exercise ``orchestrator.run_pipeline`` end to end, with
every OTHER agent faked) and are unaffected by the retirement -- both
still pass against the real ``DeterministicVerifier`` now wired in
``execute_decisions``.
"""
from __future__ import annotations

import asyncio
import csv
import threading
from pathlib import Path

import pytest
from phi_core.agents.batching import run_batched
from phi_core.agents.llm import LlmConfig
from phi_core.control.store import MemoryControlStore
from phi_core.control.testing import complete_fake_task, start_test_run


def _fixed_check(batch: list[int]) -> list[dict]:
    """Deterministic per-item check with no cross-record comparison."""
    return [{"value": v, "verdict": "pass"} for v in batch]


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_size", [1, 5, 10])
@pytest.mark.parametrize("pool_size", [1, 6])
async def test_output_invariant_across_batch_and_pool_sizes(batch_size, pool_size):
    """The flattened, ordered result never depends on how work is chunked
    or how many workers run it concurrently."""
    items = list(range(23))

    result = await run_batched(items, _fixed_check, batch_size=batch_size, pool_size=pool_size)

    assert [r["value"] for r in result] == items
    assert all(r["verdict"] == "pass" for r in result)


@pytest.mark.asyncio
async def test_each_item_checked_exactly_once():
    """No item is dropped, duplicated, or handed to more than one batch."""
    items = list(range(17))
    lock = threading.Lock()
    seen: list[int] = []

    def check(batch: list[int]) -> list[dict]:
        with lock:
            seen.extend(batch)
        return [{"value": v} for v in batch]

    await run_batched(items, check, batch_size=4, pool_size=3)

    assert sorted(seen) == items


@pytest.mark.asyncio
async def test_on_batch_called_once_per_actual_batch_with_that_batchs_index():
    """23 items at batch_size=8 makes 3 batches (8, 8, 7); each fires
    on_batch exactly once, carrying its own position in `items`, and
    verdicts surface as soon as that batch finishes rather than being
    held until the whole run completes."""
    items = list(range(23))
    calls: dict[int, list[dict]] = {}

    async def on_batch(index: int, results: list[dict]) -> None:
        assert index not in calls, "on_batch fired twice for the same batch"
        calls[index] = results

    result = await run_batched(items, _fixed_check, batch_size=8, on_batch=on_batch)

    assert set(calls) == {0, 1, 2}
    assert [len(calls[i]) for i in (0, 1, 2)] == [8, 8, 7]
    assert sum(len(r) for r in calls.values()) == len(items)
    # on_batch's per-batch payloads reassemble the same ordered result.
    assert calls[0] + calls[1] + calls[2] == result


@pytest.mark.asyncio
async def test_empty_input_produces_no_checks_or_callbacks_and_empty_list():
    calls = []

    async def on_batch(index: int, results: list[dict]) -> None:
        calls.append((index, results))

    def check(batch: list[int]) -> list[dict]:
        raise AssertionError("check must never run on empty input")

    result = await run_batched([], check, on_batch=on_batch)

    assert result == []
    assert calls == []


@pytest.mark.asyncio
async def test_pool_size_bounds_and_reaches_concurrent_batches():
    """`pool_size` is a hard concurrency ceiling, not just a chunking knob:
    with 9 single-item batches and pool_size=3, exactly 3 checks run at
    once per wave. Every running check must rendezvous with `pool_size`
    peers at a barrier; a bound violation (a 4th thread also reaching the
    barrier) or an implementation that never overlaps batches (fewer than
    3 threads ever reaching it) both surface as a barrier timeout, so this
    is deterministic rather than a timing guess.
    """
    items = list(range(9))
    pool_size = 3
    barrier = threading.Barrier(pool_size, timeout=5)
    lock = threading.Lock()
    active = 0
    max_active = 0

    def check(batch: list[int]) -> list[dict]:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            barrier.wait()
        finally:
            with lock:
                active -= 1
        return [{"value": batch[0]}]

    result = await run_batched(items, check, batch_size=1, pool_size=pool_size)

    assert max_active == pool_size
    assert [r["value"] for r in result] == items


@pytest.mark.asyncio
async def test_no_shared_mutable_state_leaks_between_batches():
    """Each batch's `check` call only ever sees its own slice of items,
    never another batch's records, regardless of pool size."""
    items = list(range(20))

    def check(batch: list[int]) -> list[dict]:
        # A leaking implementation would hand every worker the same
        # underlying list; mutating it here must never affect other
        # batches' views.
        batch.append(-1)
        return [{"value": v, "batch_len": len(batch)} for v in batch if v != -1]

    result = await run_batched(items, check, batch_size=4, pool_size=4)

    assert [r["value"] for r in result] == items
    assert all(r["batch_len"] == 5 for r in result)


def test_run_batched_is_a_plain_coroutine_function():
    assert asyncio.iscoroutinefunction(run_batched)


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"batch_size": 0},
    {"batch_size": -1},
    {"pool_size": 0},
    {"pool_size": -3},
])
async def test_invalid_batch_or_pool_size_raises_before_any_work(kwargs):
    """A bad `batch_size`/`pool_size` is rejected up front, even against
    an empty item list, rather than producing an empty range or a
    semaphore that can never be acquired."""
    calls = []

    def check(batch: list[int]) -> list[dict]:
        calls.append(batch)
        return [{"value": v} for v in batch]

    with pytest.raises(ValueError):
        await run_batched([1, 2, 3], check, **kwargs)
    with pytest.raises(ValueError):
        await run_batched([], check, **kwargs)

    assert calls == []


@pytest.mark.asyncio
async def test_check_returning_wrong_result_count_raises_clear_error():
    """check() must return exactly one result per item; a silent
    truncation or duplication is a programming error, not data to smuggle
    through as misaligned output."""
    def check(batch: list[int]) -> list[dict]:
        return [{"value": batch[0]}]  # always one result, regardless of batch size

    with pytest.raises(ValueError, match="exactly one result per item"):
        await run_batched([1, 2, 3, 4], check, batch_size=2, pool_size=2)


@pytest.mark.asyncio
async def test_a_failing_check_cancels_unstarted_siblings_and_stops_cleanly():
    """A serial pool (pool_size=1) makes batch order deterministic: 0 and
    1 complete and are delivered before 2 fails; 3 and 4 are still parked
    on the pool and are cancelled without ever running `check`. Nothing
    keeps executing in the background after `run_batched` has raised."""
    items = [0, 1, 2, 3, 4]
    checked: list[int] = []
    batches_seen: list[int] = []

    def check(batch: list[int]) -> list[dict]:
        v = batch[0]
        checked.append(v)
        if v == 2:
            raise RuntimeError("boom")
        return [{"value": v}]

    async def on_batch(index: int, results: list[dict]) -> None:
        batches_seen.append(index)

    with pytest.raises(RuntimeError, match="boom"):
        await run_batched(items, check, batch_size=1, pool_size=1, on_batch=on_batch)

    assert checked == [0, 1, 2]
    assert batches_seen == [0, 1]

    await asyncio.sleep(0.05)
    assert checked == [0, 1, 2], "a cancelled sibling ran check() after run_batched raised"


@pytest.mark.asyncio
async def test_on_batch_fires_while_a_later_batch_is_still_blocked():
    """True incremental delivery, proven causally rather than by timing:
    batch 1's check cannot return until on_batch(0, ...) unblocks it, so
    if on_batch(0, ...) has run, batch 1 is provably still mid-check."""
    items = [0, 1]
    slow_started = threading.Event()
    allow_slow_finish = threading.Event()
    fast_on_batch_seen = threading.Event()

    def check(batch: list[int]) -> list[dict]:
        v = batch[0]
        if v == 1:
            slow_started.set()
            assert allow_slow_finish.wait(timeout=5), \
                "fast batch's on_batch never unblocked the slow batch"
        return [{"value": v}]

    async def on_batch(index: int, results: list[dict]) -> None:
        if index == 0:
            assert slow_started.wait(timeout=2), "sibling batch never started concurrently"
            allow_slow_finish.set()
            fast_on_batch_seen.set()

    result = await run_batched(items, check, batch_size=1, pool_size=2, on_batch=on_batch)

    assert fast_on_batch_seen.is_set()
    assert [r["value"] for r in result] == [0, 1]


# ---- Operator agent's own tests migrated to test_deterministic_verifier.py --
#
# (Task 27 shape/coverage/omit_by_file/corrupt-file checks -- see that
# file's own module docstring for the one test deliberately deleted
# rather than migrated, and docs/PHASE_STATUS.md's DELETED_TESTS entry.)


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)

# ---- Task 28: Operator wired between Executor and Publish Guard -----------


def test_run_pipeline_excludes_corrupted_export_and_ends_partially_complete(tmp_path, monkeypatch):
    """Full-pipeline-shaped proof, run directly against
    `orchestrator.run_pipeline` with the same fake-agent-double pattern
    `test_keep_verification_pipeline.py` uses against this same function:
    every agent except the real Operator is faked, Executor is faked to
    hand back one hand-corrupted export (finding 9's `cap_age_90`
    violation) alongside one clean export, and the assertions prove
    Operator's filtering and status change land end to end.

    A corrupted export must: (1) be excluded from the final `exports`
    dict used everywhere downstream (Publish Guard, Auditor, the
    completion `$set`), (2) be named in `operator_failures`, and (3)
    leave the run `partially_complete`, not `complete`.
    """
    from phi_core.agents import orchestrator
    from phi_core.agents.reviewer import Reviewer as _RealReviewer

    bad_export = tmp_path / "f1_export.csv"
    _write_csv(bad_export, ["age"], [["96"]])  # cap_age_90 shape violation
    good_export = tmp_path / "f2_export.csv"
    _write_csv(good_export, ["age"], [["45"]])  # valid cap_age_90 output

    class FakeSessions:
        def __init__(self):
            self.updates = []

        async def find_one(self, *_args, **_kwargs):
            return None

        async def update_one(self, *_args, **_kwargs):
            self.updates.append(_args[1])

    class FakeAgentLog:
        async def insert_one(self, *_args, **_kwargs):
            return None

    class FakeDb:
        def __init__(self):
            self.sessions = FakeSessions()
            self.agent_log = FakeAgentLog()

    async def _complete(ctx, result):
        if ctx is not None and ctx.tasks is not None:
            await ctx.tasks.complete(result)

    class FakeRegulationsExpert:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self._ctx = ctx

        async def _log(self, *_a, **_kw):
            return None

        async def run(self, **_kwargs):
            await _complete(self._ctx, {})
            return {}

    class FakePHIMethodsExpert:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self._ctx = ctx

        async def _log(self, *_a, **_kw):
            return None

        async def method_for(self, _category):
            await _complete(self._ctx, {})
            return {}

    class FakeLexicon:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self._ctx = ctx

        async def run(self, **_kwargs):
            result = {"columns": []}
            await _complete(self._ctx, result)
            return result

    class FakeInstrument(FakeLexicon):
        async def run(self, **_kwargs):
            result = {"fields": []}
            await _complete(self._ctx, result)
            return result

    class FakeSchema(FakeLexicon):
        pass

    class FakeJudge:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self._ctx = ctx
            self.call_failures = 0
            self.last_message_id = None

        async def run(self, **_kwargs):
            result = {"decisions": [
                {"file_id": "f1", "column": "age", "action": "cap_age_90",
                 "phi_category": "C", "citation": "45 CFR 164.514(b)(2)(i)(C)",
                 "confidence": 0.95, "reason": "Judge decision"},
                {"file_id": "f2", "column": "age", "action": "cap_age_90",
                 "phi_category": "C", "citation": "45 CFR 164.514(b)(2)(i)(C)",
                 "confidence": 0.95, "reason": "Judge decision"},
            ]}
            await _complete(self._ctx, result)
            return result

    class FakeReviewer(_RealReviewer):
        """Overrides only PREVIEW mode (the decide-loop's former Sentinel
        call); FINAL mode (``run``, the post-Executor coverage audit this
        test actually exercises against ``bad_export``/``good_export``)
        is inherited from the real class unchanged."""
        def __init__(self, ctx=None, *_a, **_kwargs):
            super().__init__(ctx)
            self._ctx = ctx
            self.call_failures = 0

        async def preview(self, **_kwargs):
            result = {"issues": []}
            await _complete(self._ctx, result)
            return result

    class FakeExecutor:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self._ctx = ctx

        async def run(self, **_kwargs):
            result = {"exports": {"f1": str(bad_export), "f2": str(good_export)}}
            await _complete(self._ctx, result)
            return result

    monkeypatch.setattr(orchestrator, "RegulationsExpert", FakeRegulationsExpert)
    monkeypatch.setattr(orchestrator, "PHIMethodsExpert", FakePHIMethodsExpert)
    monkeypatch.setattr(orchestrator, "Lexicon", FakeLexicon)
    monkeypatch.setattr(orchestrator, "Instrument", FakeInstrument)
    monkeypatch.setattr(orchestrator, "Schema", FakeSchema)
    monkeypatch.setattr(orchestrator, "Judge", FakeJudge)
    monkeypatch.setattr(orchestrator, "Reviewer", FakeReviewer)
    monkeypatch.setattr(orchestrator, "Executor", FakeExecutor)

    phase_events = []

    async def emit(_message):
        return None

    async def on_phase(phase, payload):
        phase_events.append((phase, payload))

    db = FakeDb()

    async def _go():
        store = MemoryControlStore()
        await start_test_run(store, "session")
        return await orchestrator.run_pipeline(
            {
                "id": "session",
                "files": [
                    {"kind": "dataset", "file_id": "f1", "subtype": "csv",
                     "stored_path": str(bad_export), "columns": ["age"]},
                    {"kind": "dataset", "file_id": "f2", "subtype": "csv",
                     "stored_path": str(good_export), "columns": ["age"]},
                ],
            },
            db,
            LlmConfig(provider="anthropic", model="test", max_tokens=100),
            emit,
            on_phase,
            control_store=store,
        )

    result = asyncio.run(_go())

    assert "f1" not in result["exports"]
    assert result["exports"] == {"f2": str(good_export)}
    assert result["operator_failures"] == ["f1"]
    assert result["status"] == "partially_complete"

    operator_events = [e for e in phase_events if e[0] == "operator"]
    assert len(operator_events) == 1
    assert operator_events[0][1]["decision_count"] == 2

    completion_update = db.sessions.updates[-1]["$set"]
    assert completion_update["status"] == "partially_complete"
    assert completion_update["operator_failures"] == ["f1"]
    assert completion_update["export_paths"] == {"f2": str(good_export)}


# ---- Task 30: Reviewer wired between Operator and Publish Guard -----------


def test_run_pipeline_duplicate_judge_decision_fails_closed_before_executor(tmp_path, monkeypatch):
    """A duplicate Judge decision for the same (file_id, column) must never
    reach Executor at all.

    Before Phase 3's D11 gate, this scenario (two Judge decisions naming
    the same column) silently reached Executor and Operator -- Operator
    verified each decision independently and reported zero failures, so
    only Reviewer's own downstream recount (still covered directly by
    `test_reviewer.py::test_coverage_mismatch_when_zero_fail_verdicts_but_
    column_count_differs`) caught the mismatch after the fact. Now
    `run_decision_gates`'s `assert_exact_coverage` proves exactly-one-
    decision-per-real-column *before* Executor ever runs, so the duplicate
    is refused upstream and `run_pipeline` raises `DecisionGateFailure`
    instead of ever producing a partially-complete export.

    Every agent but Judge is a fake double; Executor is faked to record
    whether it was ever invoked, proving the gate runs strictly before it.
    """
    from phi_core.agents import orchestrator
    from phi_core.control.gates import DecisionGateFailure

    f1_export = tmp_path / "f1_export.csv"
    _write_csv(f1_export, ["field"], [[""]])  # dropped column: empty cell
    f2_export = tmp_path / "f2_export.csv"
    _write_csv(f2_export, ["field"], [[""]])  # dropped column: empty cell

    class FakeSessions:
        def __init__(self):
            self.updates = []

        async def find_one(self, *_args, **_kwargs):
            return None

        async def update_one(self, *_args, **_kwargs):
            self.updates.append(_args[1])

    class FakeAgentLog:
        async def insert_one(self, *_args, **_kwargs):
            return None

    class FakeDb:
        def __init__(self):
            self.sessions = FakeSessions()
            self.agent_log = FakeAgentLog()

    class FakeRegulationsExpert:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self.ctx = ctx

        async def _log(self, *_a, **_kw):
            return None

        async def run(self, **_kwargs):
            return await complete_fake_task(self.ctx, {})

    class FakePHIMethodsExpert:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self.ctx = ctx

        async def _log(self, *_a, **_kw):
            return None

        async def method_for(self, _category):
            return {}

    class FakeLexicon:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self.ctx = ctx

        async def run(self, **_kwargs):
            return await complete_fake_task(self.ctx, {"columns": []})

    class FakeInstrument(FakeLexicon):
        async def run(self, **_kwargs):
            return await complete_fake_task(self.ctx, {"fields": []})

    class FakeSchema(FakeLexicon):
        pass

    class FakeJudge:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self.ctx = ctx
            self.call_failures = 0
            self.last_message_id = None

        async def run(self, **_kwargs):
            return await complete_fake_task(self.ctx, {"decisions": [
                {"file_id": "f1", "column": "field", "action": "drop",
                 "phi_category": "G", "citation": "45 CFR 164.514(b)(2)(i)(G)",
                 "confidence": 0.95, "reason": "Judge decision"},
                {"file_id": "f1", "column": "field", "action": "drop",
                 "phi_category": "G", "citation": "45 CFR 164.514(b)(2)(i)(G)",
                 "confidence": 0.95, "reason": "Judge decision (duplicate)"},
                {"file_id": "f2", "column": "field", "action": "drop",
                 "phi_category": "G", "citation": "45 CFR 164.514(b)(2)(i)(G)",
                 "confidence": 0.95, "reason": "Judge decision"},
            ]})

    class FakeReviewer:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self.ctx = ctx
            self.call_failures = 0

        async def preview(self, **_kwargs):
            return await complete_fake_task(self.ctx, {"issues": []})

    executor_calls: list[int] = []

    class FakeExecutor:
        def __init__(self, ctx=None, *_a, **_kwargs):
            self.ctx = ctx

        async def run(self, **_kwargs):
            executor_calls.append(1)
            return await complete_fake_task(
                self.ctx, {"exports": {"f1": str(f1_export), "f2": str(f2_export)}}
            )

    monkeypatch.setattr(orchestrator, "RegulationsExpert", FakeRegulationsExpert)
    monkeypatch.setattr(orchestrator, "PHIMethodsExpert", FakePHIMethodsExpert)
    monkeypatch.setattr(orchestrator, "Lexicon", FakeLexicon)
    monkeypatch.setattr(orchestrator, "Instrument", FakeInstrument)
    monkeypatch.setattr(orchestrator, "Schema", FakeSchema)
    monkeypatch.setattr(orchestrator, "Judge", FakeJudge)
    monkeypatch.setattr(orchestrator, "Reviewer", FakeReviewer)
    monkeypatch.setattr(orchestrator, "Executor", FakeExecutor)

    phase_events = []

    async def emit(_message):
        return None

    async def on_phase(phase, payload):
        phase_events.append((phase, payload))

    db = FakeDb()

    async def _go():
        store = MemoryControlStore()
        await start_test_run(store, "session")
        return await orchestrator.run_pipeline(
            {
                "id": "session",
                "files": [
                    {"kind": "dataset", "file_id": "f1", "subtype": "csv",
                     "stored_path": str(f1_export), "columns": ["field"]},
                    {"kind": "dataset", "file_id": "f2", "subtype": "csv",
                     "stored_path": str(f2_export), "columns": ["field"]},
                ],
            },
            db,
            LlmConfig(provider="anthropic", model="test", max_tokens=100),
            emit,
            on_phase,
            control_store=store,
        )

    with pytest.raises(DecisionGateFailure) as excinfo:
        asyncio.run(_go())

    assert "duplicate_decision" in str(excinfo.value)
    assert not executor_calls, "Executor must never run once the coverage proof has failed"
    assert not any(phase == "reviewer" for phase, _ in phase_events)
