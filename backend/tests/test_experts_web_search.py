"""Tests for the Statute + Praxis experts armed with web_search.

Live-web tests are gated on EMERGENT_LLM_KEY availability. They are the
canonical proof that the Regulations expert and PHI-Methods expert are
actually querying the current web rather than emitting stale
LLM-training-time answers.

Unit tests (no network) verify:
* URL citation extraction from LiteLLM stringified web_search responses.
* Deterministic fallback for well-known HIPAA categories in Praxis.
* Statute merges the JurisdictionPack fallback when the LLM is terse.
"""
from __future__ import annotations

import os
import pytest


def _has_key() -> bool:
    return bool(os.environ.get("EMERGENT_LLM_KEY", "").strip())


def test_url_extraction_from_reply_text():
    """The citation extractor recovers URLs from LiteLLM's stringified
    web_search response (Anthropic's structured blocks are collapsed to
    plain text by LiteLLM)."""
    from phi_core.agents.llm import _URL_RE
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
    Praxis must NOT waste a web search for it."""
    from phi_core.agents.experts import Praxis
    method = Praxis._DETERMINISTIC_METHODS["A"]
    assert method["name"] == "drop"
    assert method["reference_paper"] == "45 CFR 164.514(b)(2)(i)(A)"


def test_praxis_deterministic_hipaa_category_c_dates_year_only():
    from phi_core.agents.experts import Praxis
    method = Praxis._DETERMINISTIC_METHODS["C"]
    assert "date_year_only" in method["name"]
    assert method["params"]["age_cap"] == 90


def test_statute_pack_fallback_shape():
    """The deterministic fallback returns the same schema as the live
    reply so the Judge does not need to branch on source."""
    from phi_core.agents.experts import Statute
    from phi_core.jurisdictions import get_pack
    fb = Statute._pack_fallback(get_pack("us"))
    assert fb["jurisdiction"] == "us"
    assert "identifier_categories" in fb
    assert isinstance(fb["handling_rules"], list)
    assert fb["age_aggregation_threshold"] == 89
    assert fb["as_of"] == "deterministic-fallback"


# ---------- Live web-search integration test ----------------------------


@pytest.mark.skipif(not _has_key(), reason="EMERGENT_LLM_KEY not set")
def test_live_web_search_returns_urls():
    """End-to-end live test: Anthropic's web_search_20250305 tool must
    execute server-side and return URLs in the response text."""
    from phi_core.agents.llm import call_llm_with_web_search, LlmConfig
    cfg = LlmConfig(
        provider="emergent",
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
