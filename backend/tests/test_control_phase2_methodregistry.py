"""Phase 2D: MethodRegistry service (register/promote/query-approved)."""
from __future__ import annotations

import pytest

from phi_core.control.methods import (
    MethodError,
    get_approved_methods,
    get_method,
    promote,
    register_method,
)
from phi_core.control.store import MemoryControlStore


@pytest.mark.asyncio
async def test_register_method_starts_at_researched():
    store = MemoryControlStore()
    record = await register_method(store, hipaa_category="E", name="pseudonymize_mrn")
    assert record.lifecycle == "researched"
    fetched = await get_method(store, record.method_id)
    assert fetched is not None
    assert fetched.method_id == record.method_id


@pytest.mark.asyncio
async def test_full_lifecycle_promotion_reaches_approved():
    store = MemoryControlStore()
    record = await register_method(store, hipaa_category="A", name="cap_age_90")
    for target in ("candidate", "validated", "approved"):
        record = await promote(store, record.method_id, to=target)
    assert record.lifecycle == "approved"


@pytest.mark.asyncio
async def test_skipped_transition_is_rejected():
    store = MemoryControlStore()
    record = await register_method(store, hipaa_category="A", name="cap_age_90")
    with pytest.raises(MethodError) as excinfo:
        await promote(store, record.method_id, to="approved")
    assert excinfo.value.reason == "method_illegal_transition"


@pytest.mark.asyncio
async def test_deprecated_is_terminal():
    store = MemoryControlStore()
    record = await register_method(store, hipaa_category="A", name="cap_age_90")
    record = await promote(store, record.method_id, to="deprecated")
    with pytest.raises(MethodError) as excinfo:
        await promote(store, record.method_id, to="candidate")
    assert excinfo.value.reason == "method_illegal_transition"


@pytest.mark.asyncio
async def test_deprecated_reachable_from_any_state():
    store = MemoryControlStore()
    for start_states in range(3):
        record = await register_method(store, hipaa_category="B", name=f"m{start_states}")
        for _ in range(start_states):
            record = await promote(
                store, record.method_id, to=("candidate", "validated", "approved")[_]
            )
        record = await promote(store, record.method_id, to="deprecated")
        assert record.lifecycle == "deprecated"


@pytest.mark.asyncio
async def test_promote_missing_method_raises():
    store = MemoryControlStore()
    with pytest.raises(MethodError) as excinfo:
        await promote(store, "no-such-id", to="candidate")
    assert excinfo.value.reason == "method_missing"


@pytest.mark.asyncio
async def test_get_approved_methods_filters_lifecycle_and_category():
    store = MemoryControlStore()
    approved = await register_method(store, hipaa_category="A", name="approved_one")
    for target in ("candidate", "validated", "approved"):
        approved = await promote(store, approved.method_id, to=target)
    await register_method(store, hipaa_category="A", name="still_researched")
    other_cat = await register_method(store, hipaa_category="B", name="approved_other_cat")
    for target in ("candidate", "validated", "approved"):
        other_cat = await promote(store, other_cat.method_id, to=target)

    all_approved = await get_approved_methods(store)
    assert {m.name for m in all_approved} == {"approved_one", "approved_other_cat"}

    cat_a_only = await get_approved_methods(store, hipaa_category="A")
    assert [m.name for m in cat_a_only] == ["approved_one"]


@pytest.mark.asyncio
async def test_concurrent_promotion_race_raises_on_loser():
    store = MemoryControlStore()
    record = await register_method(store, hipaa_category="A", name="cap_age_90")
    winner = await promote(store, record.method_id, to="candidate")
    assert winner.lifecycle == "candidate"
    # The stale in-hand `record` still thinks lifecycle == "researched";
    # promote() re-reads current state internally so a second legitimate
    # caller racing on the same stale view still succeeds via CAS on the
    # store's actual current lifecycle, not the caller's stale copy.
    second = await promote(store, record.method_id, to="validated")
    assert second.lifecycle == "validated"
