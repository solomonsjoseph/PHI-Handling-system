"""Bounded worker-pool batching, shared by Operator and Reviewer.

Executor/Operator doc's confirmed shape: not one worker per operation, and
not one operation per worker call either, but a small pool pulling batches
of 5-10, so wallclock scales with ``total_checks / (pool_size * batch_size)``.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional


async def run_batched(
    items: list[Any],
    check: Callable[[list[Any]], list[dict]],
    *,
    batch_size: int = 8,
    pool_size: int = 6,
    on_batch: Optional[Callable[[int, list[dict]], Awaitable[None]]] = None,
) -> list[dict]:
    """Check every item in ``items``, ``batch_size`` at a time, across a
    pool of at most ``pool_size`` concurrently running batches.

    Each batch's ``check(batch)`` runs in its own worker thread
    (``asyncio.to_thread``), bounded by an ``asyncio.Semaphore(pool_size)``.
    Batches are independent scheduling units: ``check`` sees only its own
    batch and must never compare records across batches. ``on_batch(index,
    results)`` is awaited as soon as that batch finishes, in whatever order
    batches actually complete, so verdicts surface incrementally rather than
    being held until the whole run finishes; ``index`` is that batch's
    position in ``items``, not its completion order. The returned list
    concatenates every batch's results in original item order regardless of
    completion order, and every item is checked exactly once.
    """
    if not items:
        return []

    batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
    semaphore = asyncio.Semaphore(pool_size)
    slots: list[Optional[list[dict]]] = [None] * len(batches)

    async def run_one(index: int, batch: list[Any]) -> None:
        async with semaphore:
            batch_results = await asyncio.to_thread(check, batch)
        slots[index] = batch_results
        if on_batch is not None:
            await on_batch(index, batch_results)

    await asyncio.gather(*(run_one(i, batch) for i, batch in enumerate(batches)))

    results: list[dict] = []
    for batch_results in slots:
        assert batch_results is not None
        results.extend(batch_results)
    return results
