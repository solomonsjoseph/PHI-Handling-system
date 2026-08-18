"""Regression coverage for Statute's advisory US adjacent-regime research."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


class _FakeDb:
    agent_log = None
    web_cache = None


def _agent():
    from phi_core.agents.experts import Statute
    from phi_core.agents.llm import LlmConfig

    return Statute(
        session_id="statute-test",
        llm=LlmConfig(provider="emergent", model="test", max_tokens=100),
        db=_FakeDb(),
    )


@pytest.mark.asyncio
async def test_adjacent_regimes_fall_back_without_blocking_when_search_fails():
    from phi_core.agents.experts import Statute

    agent = _agent()
    agent._log = AsyncMock()  # type: ignore[method-assign]
    agent.call_json_with_web_search = AsyncMock(side_effect=RuntimeError("offline"))  # type: ignore[method-assign]

    with patch("phi_core.agents.experts.cache_get", new=AsyncMock(return_value=None)), \
         patch("phi_core.agents.experts.cache_put", new=AsyncMock()) as cache_put:
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
    assert cache_put.await_args.args[1:3] == ("adjacent_regulations", "us")
    assert result == {"adjacent_regimes": Statute._ADJACENT_REGIMES_FALLBACK}


@pytest.mark.asyncio
async def test_non_us_jurisdiction_has_no_adjacent_research_call():
    agent = _agent()
    agent.call_json_with_web_search = AsyncMock()  # type: ignore[method-assign]

    with patch("phi_core.agents.experts.cache_get", new=AsyncMock()) as cache_get, \
         patch("phi_core.agents.experts.cache_put", new=AsyncMock()) as cache_put:
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
