"""Regression tests for PHIMethodsExpert's per-category method reports."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from phi_core.agents.experts import PHIMethodsExpert
from phi_core.control.testing import make_ctx


def _phi_methods_expert() -> PHIMethodsExpert:
    return PHIMethodsExpert(make_ctx("PHIMethodsExpert"))


@pytest.mark.asyncio
async def test_method_for_c_returns_multiple_schema_complete_methods_and_names_category(monkeypatch):
    agent = _phi_methods_expert()
    agent._log = AsyncMock()
    year_only_url = "https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-C/part-164#p-164.514(b)(2)(i)(C)"
    offset_url = "https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-C/part-164#p-164.514(b)(1)"
    searched = AsyncMock(return_value=({
        "category": "C",
        "methods": [
            {
                "name": "Year-only dates",
                "how_to_apply": "Replace every date with its calendar year.",
                "why": "Safe Harbor removes date elements other than the year.",
                "params": {"format": "YYYY"},
                "utility_preserving": True,
                "clinical_impact": "Retains annual trends.",
                "reference_paper": "45 CFR 164.514(b)(2)(i)(C)",
                "sources": [{"url": year_only_url, "title": "45 CFR 164.514"}],
            },
            {
                "name": "Per-patient random date offset",
                "how_to_apply": "Apply one random offset to every date for each patient.",
                "why": "Requires Expert Determination, not Safe Harbor.",
                "params": {"maximum_days": 365},
                "utility_preserving": True,
                "clinical_impact": "Retains intervals within each patient.",
                "reference_paper": "45 CFR 164.514(b)(1)",
                "sources": [{"url": offset_url, "title": "45 CFR 164.514"}],
            },
        ],
        "as_of": "2026-08-18",
    }, [{"url": year_only_url}, {"url": offset_url}]))
    agent.call_json_with_web_search = searched

    reply = await agent.method_for("C")

    assert len(reply["methods"]) == 2
    for method in reply["methods"]:
        assert method["name"]
        assert method["how_to_apply"]
        assert method["why"]
        assert method["params"]
    prompt = searched.await_args.args[0]
    assert "dates directly related to individual + ages >89" in prompt.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["A", "D", "F", "G"])
async def test_drop_only_categories_are_single_method_without_search(monkeypatch, category):
    agent = _phi_methods_expert()
    agent._log = AsyncMock()
    agent.call_json_with_web_search = AsyncMock()

    reply = await agent.method_for(category)

    assert len(reply["methods"]) == 1
    assert reply["methods"][0]["name"] == "drop"
    agent.call_json_with_web_search.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_failure_returns_deterministic_b_method_wrapped(monkeypatch):
    agent = _phi_methods_expert()
    agent._log = AsyncMock()
    agent.call_json_with_web_search = AsyncMock(side_effect=RuntimeError("search unavailable"))

    reply = await agent.method_for("B")

    assert reply["methods"] == [PHIMethodsExpert._DETERMINISTIC_METHODS["B"]]


@pytest.mark.asyncio
async def test_judge_summary_lists_method_names_and_any_utility_preservation():
    from phi_core.agents.reasoning import Judge
    judge = Judge(make_ctx("Judge"))
    judge.call_json = AsyncMock(return_value={"decisions": []})

    await judge.run(
        schema={"columns": []}, instrument={}, lexicon={}, statute={},
        praxis={"C": {"methods": [
            {"name": "year_only", "utility_preserving": True},
            {"name": "date_offset", "utility_preserving": False},
        ]}},
    )

    prompt = judge.call_json.await_args.args[0]
    assert "year_only" in prompt
    assert "date_offset" in prompt
    assert "'utility_preserving': True" in prompt
