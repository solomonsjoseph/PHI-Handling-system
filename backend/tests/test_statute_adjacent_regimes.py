"""Regression coverage for Statute's advisory US adjacent-regime research."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest


def _agent():
    from phi_core.agents.experts import Statute
    from phi_core.control.testing import make_ctx

    return Statute(make_ctx("Statute"))


@pytest.mark.asyncio
async def test_adjacent_regimes_fall_back_without_blocking_when_search_fails():
    from phi_core.agents.experts import Statute

    agent = _agent()
    agent._log = AsyncMock()  # type: ignore[method-assign]
    agent.call_json_with_web_search = AsyncMock(side_effect=RuntimeError("offline"))  # type: ignore[method-assign]

    with patch.object(agent.ctx.cache, "get", new=AsyncMock(return_value=None)), \
         patch.object(agent.ctx.cache, "put", new=AsyncMock()) as cache_put:
        result = await agent._adjacent_regimes_for("us")

    regimes = result["adjacent_regimes"]
    by_name = {regime["name"]: regime for regime in regimes}
    assert set(by_name) == {
        "45 CFR 46 (Common Rule)",
        "42 CFR Part 2",
        "FERPA",
        "Privacy Act of 1974",
        "State law (non-exhaustive)",
    }
    for name in (
        "45 CFR 46 (Common Rule)",
        "42 CFR Part 2",
        "FERPA",
        "Privacy Act of 1974",
    ):
        assert by_name[name]["citation"]
    assert "non-exhaustive" in by_name["State law (non-exhaustive)"]["advisory"]
    cache_put.assert_awaited_once()
    assert cache_put.await_args.args[0:2] == ("adjacent_regulations", "us")
    assert result == {"adjacent_regimes": Statute._ADJACENT_REGIMES_FALLBACK}


@pytest.mark.asyncio
async def test_adjacent_regimes_reject_scalar_entries_from_web_reply():
    from phi_core.agents.experts import Statute

    agent = _agent()
    agent.call_json_with_web_search = AsyncMock(return_value=(
        {"adjacent_regimes": ["not a regime"] * 5},
        [],
    ))  # type: ignore[method-assign]

    with patch.object(agent.ctx.cache, "get", new=AsyncMock(return_value=None)), \
         patch.object(agent.ctx.cache, "put", new=AsyncMock()):
        result = await agent._adjacent_regimes_for("us")

    assert result == {"adjacent_regimes": Statute._ADJACENT_REGIMES_FALLBACK}




@pytest.mark.asyncio
async def test_adjacent_regimes_reject_untrusted_cached_entries():
    from phi_core.agents.experts import Statute

    agent = _agent()
    agent._log = AsyncMock()  # type: ignore[method-assign]
    agent.call_json_with_web_search = AsyncMock()  # type: ignore[method-assign]
    cached = {"content": json.dumps({"adjacent_regimes": ["not a regime"] * 5})}

    with patch.object(agent.ctx.cache, "get", new=AsyncMock(return_value=cached)), \
         patch.object(agent.ctx.cache, "put", new=AsyncMock()) as cache_put:
        result = await agent._adjacent_regimes_for("us")

    assert result == {"adjacent_regimes": Statute._ADJACENT_REGIMES_FALLBACK}
    agent.call_json_with_web_search.assert_not_awaited()
    cache_put.assert_not_awaited()


@pytest.mark.asyncio
async def test_adjacent_regimes_reject_cached_non_string_name():
    from phi_core.agents.experts import Statute

    agent = _agent()
    agent._log = AsyncMock()  # type: ignore[method-assign]
    agent.call_json_with_web_search = AsyncMock()  # type: ignore[method-assign]
    regimes = [regime.copy() for regime in Statute._ADJACENT_REGIMES_FALLBACK]
    regimes[0]["name"] = ["not", "a", "string"]
    cached = {"content": json.dumps({"adjacent_regimes": regimes})}

    with patch.object(agent.ctx.cache, "get", new=AsyncMock(return_value=cached)), \
         patch.object(agent.ctx.cache, "put", new=AsyncMock()) as cache_put:
        result = await agent._adjacent_regimes_for("us")

    assert result == {"adjacent_regimes": Statute._ADJACENT_REGIMES_FALLBACK}
    agent.call_json_with_web_search.assert_not_awaited()
    cache_put.assert_not_awaited()
@pytest.mark.asyncio
async def test_adjacent_regimes_reject_incomplete_or_noncanonical_dict_entries():
    from phi_core.agents.experts import Statute

    incomplete = {
        "adjacent_regimes": [
            {"name": regime["name"]}
            for regime in Statute._ADJACENT_REGIMES_FALLBACK
        ],
    }
    noncanonical = {
        "adjacent_regimes": [
            regime.copy()
            for regime in Statute._ADJACENT_REGIMES_FALLBACK
        ],
    }
    noncanonical["adjacent_regimes"][0]["citation"] = "incorrect citation"

    for payload in (incomplete, noncanonical):
        agent = _agent()
        agent.call_json_with_web_search = AsyncMock(return_value=(payload, []))  # type: ignore[method-assign]

        with patch.object(agent.ctx.cache, "get", new=AsyncMock(return_value=None)), \
             patch.object(agent.ctx.cache, "put", new=AsyncMock()):
            result = await agent._adjacent_regimes_for("us")

        assert result == {"adjacent_regimes": Statute._ADJACENT_REGIMES_FALLBACK}


@pytest.mark.asyncio
async def test_non_us_jurisdiction_has_no_adjacent_research_call():
    agent = _agent()
    agent.call_json_with_web_search = AsyncMock()  # type: ignore[method-assign]

    with patch.object(agent.ctx.cache, "get", new=AsyncMock()) as cache_get, \
         patch.object(agent.ctx.cache, "put", new=AsyncMock()) as cache_put:
        result = await agent._adjacent_regimes_for("eu")

    assert result == {"adjacent_regimes": []}
    cache_get.assert_not_awaited()
    cache_put.assert_not_awaited()
    agent.call_json_with_web_search.assert_not_awaited()


@pytest.mark.asyncio
async def test_rules_for_merges_adjacent_regimes_without_changing_hipaa_reply():
    agent = _agent()
    hipaa = {
        "jurisdiction": "us",
        "identifier_categories": {"A": "Names"},
        "handling_rules": [],
        "age_aggregation_threshold": 89,
    }
    adjacent = {"adjacent_regimes": [{"name": "FERPA"}]}
    agent._hipaa_rules_for = AsyncMock(return_value=hipaa)  # type: ignore[method-assign]
    agent._adjacent_regimes_for = AsyncMock(return_value=adjacent)  # type: ignore[method-assign]

    result = await agent.rules_for("us")

    assert result is hipaa
    assert result["adjacent_regimes"] == adjacent["adjacent_regimes"]
    agent._hipaa_rules_for.assert_awaited_once_with("us")
    agent._adjacent_regimes_for.assert_awaited_once_with("us")
