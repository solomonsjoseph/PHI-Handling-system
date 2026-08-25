"""CorpusResearcher tests (Phase C3).

Unit tests verify the researcher enforces its grounding gate (refuses
ungrounded / uncited replies) and correctly caches. The live web-search
test only runs when ANTHROPIC_API_KEY is present.
"""
from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, patch

import pytest


def _has_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


@pytest.mark.asyncio
async def test_researcher_refuses_ungrounded_reply(monkeypatch):
    """The researcher MUST refuse to return a scenario without source
    citations. Otherwise it would encourage hallucinated study data —
    the exact opposite of Sir's spec."""
    from phi_core.agents.llm import LlmConfig
    from phi_corpus.researcher import CorpusResearcher

    ungrounded = {
        "scenario_id": "cardiology_v1",
        "label": "Cardiology cohort",
        "datasets": [{"filename": "x.csv", "columns": []}],
        # NOTE: sources deliberately empty.
    }

    async def _fake_call(prompt, phase, default=None, max_uses=3):
        return ungrounded, []

    class _FakeDb:
        agent_cache = None
        agent_log = None

    fake_db = _FakeDb()
    cfg = LlmConfig(provider="anthropic", model="test", max_tokens=100)
    agent = CorpusResearcher(session_id="t", llm=cfg, db=fake_db)
    agent.call_json_with_web_search = _fake_call  # type: ignore
    # Bypass mongo cache side effects with no-op stubs
    async def _log(*a, **k): return None
    agent._log = _log  # type: ignore
    with patch("phi_corpus.researcher.cache_get", new=AsyncMock(return_value=None)), \
         patch("phi_corpus.researcher.cache_put", new=AsyncMock(return_value=None)):
        reply = await agent.research("cardiology")

    assert "error" in reply
    assert "no web citations" in reply["error"]
    assert reply["candidate"] == ungrounded


@pytest.mark.asyncio
async def test_researcher_accepts_reply_with_citations(monkeypatch):
    """A grounded reply with sources passes through unchanged."""
    from phi_core.agents.llm import LlmConfig
    from phi_corpus.researcher import CorpusResearcher

    grounded = {
        "scenario_id": "diabetes_uk_biobank_v1",
        "label": "Diabetes cohort - UK Biobank subset",
        "jurisdictions": ["us"],
        "source_study": {"title": "UK Biobank", "sponsor": "Wellcome Trust",
                         "url": "https://ukbiobank.ac.uk", "nct_id": None,
                         "accessed_at": "2026-01-15"},
        "datasets": [{"filename": "cohort.csv",
                      "columns": [
                          {"name": "eid", "hipaa_category": "H",
                           "expected_action": "pseudonymize"},
                          {"name": "hba1c", "hipaa_category": "NONE",
                           "expected_action": "keep"},
                      ]}],
        "dictionary": [{"column_name": "eid", "description": "Encoded participant id"}],
        "sources": [{"url": "https://ukbiobank.ac.uk/enable-your-research/about-our-data",
                     "title": "UK Biobank data"}],
    }

    async def _fake_call(prompt, phase, default=None, max_uses=3):
        return grounded, []

    class _FakeDb:
        agent_cache = None
        agent_log = None

    fake_db = _FakeDb()
    cfg = LlmConfig(provider="anthropic", model="test", max_tokens=100)
    agent = CorpusResearcher(session_id="t2", llm=cfg, db=fake_db)
    agent.call_json_with_web_search = _fake_call  # type: ignore
    async def _log(*a, **k): return None
    agent._log = _log  # type: ignore
    with patch("phi_corpus.researcher.cache_get", new=AsyncMock(return_value=None)), \
         patch("phi_corpus.researcher.cache_put", new=AsyncMock(return_value=None)):
        reply = await agent.research("diabetes UK biobank")

    assert "error" not in reply
    assert reply["scenario_id"] == "diabetes_uk_biobank_v1"
    assert reply["sources"]


@pytest.mark.asyncio
async def test_researcher_uses_cache_on_second_call(monkeypatch):
    from phi_core.agents.llm import LlmConfig
    from phi_corpus.researcher import CorpusResearcher

    cached_payload = {
        "scenario_id": "onco_v1", "label": "Onco", "sources": [{"url": "u"}],
    }
    calls: list[str] = []

    async def _fake_call(*a, **k):
        calls.append("live")
        return {}, []

    class _FakeDb:
        agent_cache = None
        agent_log = None

    fake_db = _FakeDb()
    cfg = LlmConfig(provider="anthropic", model="test", max_tokens=100)
    agent = CorpusResearcher(session_id="t3", llm=cfg, db=fake_db)
    agent.call_json_with_web_search = _fake_call  # type: ignore
    async def _log(*a, **k): return None
    agent._log = _log  # type: ignore
    async def _cache_get(_db, _topic, _key):
        return {"content": json.dumps(cached_payload)}
    with patch("phi_corpus.researcher.cache_get", new=_cache_get), \
         patch("phi_corpus.researcher.cache_put", new=AsyncMock(return_value=None)):
        reply = await agent.research("oncology")
    assert reply == cached_payload
    assert calls == [], "web_search should NOT have been called on a cache hit"


# ---- Live integration test (skipped without ANTHROPIC_API_KEY) ----------


@pytest.mark.skipif(not _has_key(), reason="ANTHROPIC_API_KEY not set")
@pytest.mark.asyncio
async def test_researcher_live_returns_grounded_scenario():
    """Full stack: run the researcher against Claude native web_search on
    a well-known domain and assert the reply is grounded + shaped."""
    from motor.motor_asyncio import AsyncIOMotorClient
    from phi_core.agents.llm import LlmConfig
    from phi_corpus.researcher import CorpusResearcher

    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]
    # Bust cache to force live search
    await db.agent_cache.delete_many({"topic": "corpus_scenario"})
    cfg = LlmConfig(
        provider="anthropic",
        model="claude-sonnet-4-5-20250929",
        max_tokens=3000,
    )
    agent = CorpusResearcher(session_id="live", llm=cfg, db=db)
    reply = await agent.research("heart failure clinical outcomes")
    client.close()

    assert "error" not in reply, reply
    assert reply.get("sources"), "researcher must return sources"
    assert reply.get("datasets"), "researcher must return datasets"
    # At least one PHI column and one clinical column
    cols = reply["datasets"][0]["columns"]
    hipaa_cats = {c["hipaa_category"] for c in cols}
    assert hipaa_cats & {"A", "B", "C", "D", "F", "G", "H"}
    assert "NONE" in hipaa_cats
