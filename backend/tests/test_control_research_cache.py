"""D16 acceptance tests for StoreResearchCache (control/context.py)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from phi_core.control.context import StoreResearchCache
from phi_core.control.store import MemoryControlStore


@pytest.mark.asyncio
async def test_put_then_get_round_trips_and_stamps_provenance():
    store = MemoryControlStore()
    cache = StoreResearchCache(store)

    await cache.put("regulation_rules", "us", "content here", source="web_search")
    doc = await cache.get("regulation_rules", "us")

    assert doc is not None
    assert doc["content"] == "content here"
    assert doc["evidence_state"] == "UNVERIFIED"  # tool-backed source
    assert doc["policy_version"]
    assert isinstance(doc["fetched_at"], datetime)  # native Date, not an isoformat string


@pytest.mark.asyncio
async def test_llm_sourced_entries_are_marked_unknown_evidence_state():
    store = MemoryControlStore()
    cache = StoreResearchCache(store)

    await cache.put("regulation_rules", "us", "content", source="llm")
    doc = await cache.get("regulation_rules", "us")

    assert doc["evidence_state"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_get_is_a_miss_when_the_policy_version_has_changed(monkeypatch):
    from phi_core.control import policy as policy_module

    store = MemoryControlStore()
    cache = StoreResearchCache(store)
    await cache.put("regulation_rules", "us", "content", source="llm")

    monkeypatch.setattr(policy_module, "POLICY_VERSION", "policy/2")
    doc = await cache.get("regulation_rules", "us")

    assert doc is None


@pytest.mark.asyncio
async def test_get_is_a_miss_when_fetched_at_is_older_than_the_refresh_window(monkeypatch):
    from phi_core.control import limits as limits_module

    store = MemoryControlStore()
    cache = StoreResearchCache(store)
    await cache.put("regulation_rules", "us", "content", source="llm")

    monkeypatch.setattr(limits_module, "WEB_CACHE_REFRESH_DAYS", 7)
    stored = await store.get_one("web_cache", {"topic": "regulation_rules", "jurisdiction": "us"})
    stale = dict(stored)
    stale["fetched_at"] = datetime.now(timezone.utc) - timedelta(days=8)
    await store.replace_one("web_cache", {"topic": "regulation_rules", "jurisdiction": "us"}, stale)

    doc = await cache.get("regulation_rules", "us")

    assert doc is None


@pytest.mark.asyncio
async def test_get_is_a_miss_not_a_crash_on_an_unparseable_fetched_at():
    from phi_core.control.policy import POLICY_VERSION

    store = MemoryControlStore()
    await store.insert("web_cache", {
        "topic": "regulation_rules", "jurisdiction": "us", "content": "x", "source": "llm",
        "policy_version": POLICY_VERSION,
        "fetched_at": "not-a-timestamp",
    })
    cache = StoreResearchCache(store)

    doc = await cache.get("regulation_rules", "us")

    assert doc is None


@pytest.mark.asyncio
async def test_get_is_a_miss_when_fetched_at_is_entirely_missing():
    store = MemoryControlStore()
    from phi_core.control.policy import POLICY_VERSION

    await store.insert("web_cache", {
        "topic": "regulation_rules", "jurisdiction": "us", "content": "x", "source": "llm",
        "policy_version": POLICY_VERSION,
    })
    cache = StoreResearchCache(store)

    doc = await cache.get("regulation_rules", "us")

    assert doc is None
