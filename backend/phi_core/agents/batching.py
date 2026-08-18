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
    batch and must never compare records across batches. It must return
    exactly one result per item it was given; a mismatched count raises
    ``ValueError`` rather than silently misaligning results.

    ``on_batch(index, results)`` is awaited as soon as that batch finishes,
    in whatever order batches actually complete, so verdicts surface
    incrementally rather than being held until the whole run finishes;
    ``index`` is that batch's position in ``items``, not its completion
    order. The returned list concatenates every batch's results in
    original item order regardless of completion order, and every item is
    checked exactly once.

    If ``check`` or ``on_batch`` raises for any batch, every sibling batch
    that has not yet started is cancelled, every sibling is awaited to
    completion so nothing keeps running (and no further ``on_batch`` call
    can happen) once this function has returned control to the caller, and
    the original exception is re-raised.

    Raises ``ValueError`` if ``batch_size`` or ``pool_size`` is less than 1.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")
    if pool_size < 1:
        raise ValueError(f"pool_size must be >= 1, got {pool_size}")

    if not items:
        return []

    batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
    semaphore = asyncio.Semaphore(pool_size)
    slots: list[Optional[list[dict]]] = [None] * len(batches)

    async def run_one(index: int, batch: list[Any]) -> None:
        expected_count = len(batch)
        async with semaphore:
            batch_results = await asyncio.to_thread(check, batch)
        if len(batch_results) != expected_count:
            raise ValueError(
                f"check returned {len(batch_results)} result(s) for batch "
                f"{index} of {expected_count} item(s); check must return "
                "exactly one result per item")
        slots[index] = batch_results
        if on_batch is not None:
            await on_batch(index, batch_results)

    tasks = [asyncio.ensure_future(run_one(i, batch)) for i, batch in enumerate(batches)]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        # asyncio.gather does not cancel siblings on the first failure: a
        # batch that hasn't started is cancelled outright, and every batch
        # (cancelled or already mid-flight) is awaited to completion before
        # we propagate, so nothing survives this call as a background task.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    results: list[dict] = []
    for batch_results in slots:
        results.extend(batch_results)
    return results
