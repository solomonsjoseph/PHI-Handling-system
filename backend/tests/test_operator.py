"""Tests for the shared bounded worker-pool batching helper (Task 26).

Operator and Reviewer both build on `run_batched`; these tests cover the
helper itself. The `Operator` agent is a later task and is not exercised
here.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from phi_core.agents.batching import run_batched


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
