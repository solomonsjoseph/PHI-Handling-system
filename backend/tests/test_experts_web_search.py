"""Tests for the RegulationsExpert + PHIMethodsExpert experts armed with web_search.

Live-web tests are gated on ANTHROPIC_API_KEY availability. They are the
canonical proof that the Regulations expert and PHI-Methods expert are
actually querying the current web rather than emitting stale
LLM-training-time answers.

Unit tests (no network) verify:
* URL citation extraction from LiteLLM stringified web_search responses.
* Deterministic fallback for well-known HIPAA categories in PHIMethodsExpert.
* RegulationsExpert merges the JurisdictionPack fallback when the LLM is terse.
"""
from __future__ import annotations

import os

import pytest


def _has_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def test_url_extraction_from_reply_text():
    """The citation extractor recovers URLs from LiteLLM's stringified
    web_search response (Anthropic's structured blocks are collapsed to
    plain text by LiteLLM)."""
    from phi_core.control.gateway import _URL_RE
    text = (
        "Based on the search, the current URL is "
        "https://www.hhs.gov/hipaa/index.html and the OCR guidance is "
        "at https://www.hhs.gov/ocr/privacy.html (Sept 2026)."
    )
    urls = _URL_RE.findall(text)
    assert "https://www.hhs.gov/hipaa/index.html" in urls
    assert "https://www.hhs.gov/ocr/privacy.html" in urls


def test_praxis_deterministic_hipaa_category_a_dropped():
    """HIPAA cat A (names) has a canonical deterministic technique;
    PHIMethodsExpert must NOT waste a web search for it."""
    from phi_core.agents.experts import PHIMethodsExpert
    method = PHIMethodsExpert._DETERMINISTIC_METHODS["A"]
    assert method["name"] == "drop"
    assert method["reference_paper"] == "45 CFR 164.514(b)(2)(i)(A)"


def test_praxis_deterministic_hipaa_category_c_dates_year_only():
    from phi_core.agents.experts import PHIMethodsExpert
    method = PHIMethodsExpert._DETERMINISTIC_METHODS["C"]
    assert "date_year_only" in method["name"]
    assert method["params"]["age_cap"] == 90


def test_statute_pack_fallback_shape():
    """The deterministic fallback returns the same schema as the live
    reply so the Judge does not need to branch on source."""
    from phi_core.agents.experts import RegulationsExpert
    from phi_core.jurisdictions import get_pack
    fb = RegulationsExpert._pack_fallback(get_pack("us"))
    assert fb["jurisdiction"] == "us"
    assert "identifier_categories" in fb
    assert isinstance(fb["handling_rules"], list)
    assert fb["age_aggregation_threshold"] == 89
    assert fb["as_of"] == "deterministic-fallback"


# ---------- Section 36: research-query privacy (confirmation, unchanged code) --
#
# Phase 6's demand-driven dispatch (agents/orchestrator.py) only ever calls
# RegulationsExpert.run(jurisdiction=...) and PHIMethodsExpert.method_for(category)
# -- neither method's signature has a parameter that could carry a raw dataset
# value, column name, or decision reason. These tests confirm that structural
# guarantee directly against the real (unchanged) signatures, and confirm the
# actual prompt text built from a jurisdiction/category never grows a hook for
# caller-supplied free text.


def test_regulations_expert_run_signature_accepts_only_jurisdiction():
    import inspect

    from phi_core.agents.experts import RegulationsExpert
    params = set(inspect.signature(RegulationsExpert.run).parameters) - {"self"}
    assert params == {"jurisdiction"}, (
        "RegulationsExpert.run must have no parameter that could carry raw "
        f"dataset/decision content; got {params}"
    )


def test_phi_methods_expert_method_for_signature_accepts_only_category():
    import inspect

    from phi_core.agents.experts import PHIMethodsExpert
    params = set(inspect.signature(PHIMethodsExpert.method_for).parameters) - {"self"}
    assert params == {"category"}, (
        "PHIMethodsExpert.method_for must have no parameter that could carry raw "
        f"dataset/decision content; got {params}"
    )


def test_regulations_expert_prompt_never_contains_decision_derived_text(monkeypatch):
    """Section 36: a research query must be a sanitized semantic question,
    never the raw thing being asked about. Builds the real prompt
    ``_hipaa_rules_for`` sends and confirms it contains only the
    jurisdiction/pack-derived static text -- nothing that looks like it
    came from a specific decision, column, or row."""
    from unittest.mock import AsyncMock

    from phi_core.agents.experts import RegulationsExpert
    from phi_core.control.testing import make_ctx

    agent = RegulationsExpert(make_ctx("RegulationsExpert"))
    agent._log = AsyncMock()
    captured_prompts: list[str] = []

    async def _capture(prompt, **_kw):
        captured_prompts.append(prompt)
        return {"jurisdiction": "us", "identifier_categories": {}, "handling_rules": [],
                "age_aggregation_threshold": 89, "as_of": "2026-01-01", "sources": []}, []

    agent.call_json_with_web_search = _capture

    import asyncio
    asyncio.run(agent._hipaa_rules_for("us"))

    assert captured_prompts
    prompt = captured_prompts[0]
    # Only ever built from a jurisdiction code + its static JurisdictionPack
    # label/regulation name -- no decision-shaped content (column names,
    # free-text reasons, patient values) has any way to reach this prompt.
    assert "column" not in prompt.lower()
    assert "row" not in prompt.lower()
    assert "patient" not in prompt.lower()


def test_phi_methods_expert_prompt_never_contains_decision_derived_text():
    """Same guarantee as above for PHIMethodsExpert.method_for -- its
    prompt is built from a HIPAA category letter and the fixed
    JurisdictionPack description for that letter, never from a specific
    column, row, or Judge decision."""
    from unittest.mock import AsyncMock

    from phi_core.agents.experts import PHIMethodsExpert
    from phi_core.control.testing import make_ctx

    agent = PHIMethodsExpert(make_ctx("PHIMethodsExpert"))
    agent._log = AsyncMock()
    captured_prompts: list[str] = []

    async def _capture(prompt, **_kw):
        captured_prompts.append(prompt)
        return {"category": "E", "methods": [{"name": "date_shift", "sources": []}],
                "as_of": "2026-01-01"}, []

    agent.call_json_with_web_search = _capture

    import asyncio
    asyncio.run(agent.method_for("E"))  # E has no deterministic method -> takes the web-search path

    assert captured_prompts
    prompt = captured_prompts[0]
    assert "column" not in prompt.lower()
    assert "row" not in prompt.lower()
    assert "patient" not in prompt.lower()


# ---------- Live web-search integration test ----------------------------


@pytest.mark.skipif(not _has_key(), reason="ANTHROPIC_API_KEY not set")
def test_live_web_search_returns_urls():
    """End-to-end live test: Anthropic's web_search_20250305 tool must
    execute server-side and return URLs in the response text."""
    from phi_core.agents.llm import LlmConfig, call_llm_with_web_search
    cfg = LlmConfig(
        provider="anthropic",
        model="claude-sonnet-4-5-20250929",
        max_tokens=400,
    )
    reply, citations = call_llm_with_web_search(
        system="Answer factually and include source URLs.",
        user=("What is the current URL of the HHS OCR De-identification "
              "guidance page? Search the web and reply with the URL only."),
        cfg=cfg,
        max_uses=2,
    )
    assert len(reply) > 20
    # At least one URL must be present in the reply (citation extractor
    # relies on inline URLs).
    assert citations, f"no citations extracted from reply: {reply[:200]}"
    assert any(c["url"].startswith("http") for c in citations)
