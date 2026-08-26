"""D12 evidence-verification contracts for the agents that web-search:
Statute, Praxis (see test_praxis_multi_method.py), Scout, and
CorpusResearcher (see test_corpus_researcher.py). This file covers
Statute and Scout, whose evidence-gate tests do not already live
elsewhere.

Every test proves the same shape: a model-authored URL absent from the
response's own tool citations can never verify, and every
agent falls back to its documented deterministic behavior when nothing
verifies.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from phi_core.control.testing import make_ctx


def _statute():
    from phi_core.agents.experts import Statute

    return Statute(make_ctx("Statute"))


@pytest.mark.asyncio
async def test_hipaa_rules_falls_back_when_reported_sources_are_not_tool_backed():
    """A model-authored `sources` URL that never appears in the response's
    actual tool citations must never verify -- Statute falls back to the
    deterministic jurisdiction pack instead of shipping it."""
    from phi_core.jurisdictions import get_pack

    agent = _statute()
    agent._log = AsyncMock()  # type: ignore[method-assign]
    reply = {
        "jurisdiction": "us",
        "regulation": "45 CFR 164.514",
        "identifier_categories": {"A": "Names"},
        "handling_rules": ["drop names"],
        "age_aggregation_threshold": 89,
        "as_of": "2026-01-01",
        "sources": [{"url": "https://example.com/not-really-hhs", "title": "forged"}],
    }
    agent.call_json_with_web_search = AsyncMock(return_value=(reply, []))  # type: ignore[method-assign]

    with patch.object(agent.ctx.cache, "get", new=AsyncMock(return_value=None)), \
         patch.object(agent.ctx.cache, "put", new=AsyncMock()) as cache_put:
        result = await agent._hipaa_rules_for("us")

    assert result == agent._pack_fallback(get_pack("us"))
    assert cache_put.await_args.kwargs["source"] == "llm"


@pytest.mark.asyncio
async def test_hipaa_rules_keeps_only_the_tool_backed_source_on_an_authoritative_domain():
    """A source whose URL is genuinely present in the response's tool
    citations AND on the authoritative-domain allow-list reaches VERIFIED
    and survives; a second, unverified source on the same reply does not."""
    agent = _statute()
    agent._log = AsyncMock()  # type: ignore[method-assign]
    real_url = "https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-C/part-164"
    forged_url = "https://example.com/forged"
    reply = {
        "jurisdiction": "us",
        "regulation": "45 CFR 164.514",
        "identifier_categories": {"A": "Names"},
        "handling_rules": ["drop names"],
        "age_aggregation_threshold": 89,
        "as_of": "2026-01-01",
        "sources": [{"url": real_url, "title": "eCFR"}, {"url": forged_url, "title": "forged"}],
    }
    agent.call_json_with_web_search = AsyncMock(  # type: ignore[method-assign]
        return_value=(reply, [{"url": real_url}]),
    )

    with patch.object(agent.ctx.cache, "get", new=AsyncMock(return_value=None)), \
         patch.object(agent.ctx.cache, "put", new=AsyncMock()) as cache_put:
        result = await agent._hipaa_rules_for("us")

    assert result["sources"] == [{"url": real_url, "title": "eCFR"}]
    assert cache_put.await_args.kwargs["source"] == "web_search"


@pytest.mark.asyncio
async def test_adjacent_regimes_trims_a_regime_source_that_is_not_tool_backed():
    """Per-regime sources are verified independently: a regime whose
    reported URL matches the response's tool citations on an
    authoritative domain keeps it; a sibling regime whose URL was never
    actually returned by the tool has its source dropped, not trusted."""
    from phi_core.agents.experts import Statute

    agent = _statute()
    agent._log = AsyncMock()  # type: ignore[method-assign]
    real_url = "https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-A/part-46"
    regimes = [regime.copy() for regime in Statute._ADJACENT_REGIMES_FALLBACK]
    regimes[0] = dict(regimes[0], sources=[{"url": real_url, "title": "Common Rule"}])
    regimes[1] = dict(regimes[1], sources=[{"url": "https://example.com/forged", "title": "forged"}])
    reply = {"adjacent_regimes": regimes}
    agent.call_json_with_web_search = AsyncMock(  # type: ignore[method-assign]
        return_value=(reply, [{"url": real_url}]),
    )

    with patch.object(agent.ctx.cache, "get", new=AsyncMock(return_value=None)), \
         patch.object(agent.ctx.cache, "put", new=AsyncMock()):
        result = await agent._adjacent_regimes_for("us")

    by_name = {r["name"]: r for r in result["adjacent_regimes"]}
    assert by_name[regimes[0]["name"]]["sources"] == [{"url": real_url, "title": "Common Rule"}]
    assert by_name[regimes[1]["name"]]["sources"] == []


def _scout():
    from phi_core.agents.outward import Scout

    return Scout(make_ctx("Scout"))


@pytest.mark.asyncio
async def test_scout_keeps_a_tool_backed_citation_on_an_allow_listed_domain():
    from phi_core.agents.outward import _verify_citation

    real_url = "https://github.com/microsoft/presidio"
    result = _verify_citation(
        run_id="run", task_id="task", system_name="Presidio",
        citation=real_url, cited_urls={real_url},
    )

    assert result == real_url


@pytest.mark.asyncio
async def test_scout_clears_a_citation_absent_from_tool_citations():
    from phi_core.agents.outward import _verify_citation

    result = _verify_citation(
        run_id="run", task_id="task", system_name="Comprehend Medical",
        citation="https://aws.amazon.com/comprehend/medical/", cited_urls=set(),
    )

    assert result == ""


@pytest.mark.asyncio
async def test_scout_leaves_a_non_url_citation_untouched():
    from phi_core.agents.outward import _verify_citation

    result = _verify_citation(
        run_id="run", task_id="task", system_name="iSchemaView",
        citation="vendor documentation, not a URL", cited_urls=set(),
    )

    assert result == "vendor documentation, not a URL"


@pytest.mark.asyncio
async def test_scout_run_clears_unverified_citations_and_caches_as_llm():
    agent = _scout()
    agent._log = AsyncMock()  # type: ignore[method-assign]
    forged_url = "https://example.com/forged-presidio-page"
    reply = {
        "systems": [
            {"name": "Presidio", "kind": "open", "vendor": "Microsoft",
             "strengths": [], "weaknesses": [], "reads_row_values": False,
             "citation": forged_url},
        ],
        "summary": "landscape",
    }
    agent.call_json_with_web_search = AsyncMock(return_value=(reply, []))  # type: ignore[method-assign]

    with patch.object(agent.ctx.cache, "get", new=AsyncMock(return_value=None)), \
         patch.object(agent.ctx.cache, "put", new=AsyncMock()) as cache_put:
        result = await agent.run()

    assert result["systems"][0]["citation"] == ""
    assert cache_put.await_args.kwargs["source"] == "llm"


@pytest.mark.asyncio
async def test_scout_run_keeps_a_verified_citation_and_caches_as_web_search():
    agent = _scout()
    agent._log = AsyncMock()  # type: ignore[method-assign]
    real_url = "https://github.com/microsoft/presidio"
    reply = {
        "systems": [
            {"name": "Presidio", "kind": "open", "vendor": "Microsoft",
             "strengths": [], "weaknesses": [], "reads_row_values": False,
             "citation": real_url},
        ],
        "summary": "landscape",
    }
    agent.call_json_with_web_search = AsyncMock(  # type: ignore[method-assign]
        return_value=(reply, [{"url": real_url}]),
    )

    with patch.object(agent.ctx.cache, "get", new=AsyncMock(return_value=None)), \
         patch.object(agent.ctx.cache, "put", new=AsyncMock()) as cache_put:
        result = await agent.run()

    assert result["systems"][0]["citation"] == real_url
    assert cache_put.await_args.kwargs["source"] == "web_search"
