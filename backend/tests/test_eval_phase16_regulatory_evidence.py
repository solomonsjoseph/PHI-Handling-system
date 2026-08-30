"""Phase 16 evaluation 5/9: regulatory evidence support.

Measures whether RegulationsExpert's findings cite genuinely supporting
evidence by reusing D12's real verification logic
(``phi_core.control.evidence.evaluate_claim``/``record_source``/
``is_tool_backed``, and ``phi_core.agents.experts._verify_research_reply``,
the exact function ``RegulationsExpert.rules_for`` calls internally) as the
check -- never a reimplementation of it.

Ground truth: 12 labeled (claim, candidate URL, domain-authority,
tool-backing) cases spanning every combination D12's rule actually decides
on: an authoritative-domain URL the search tool genuinely returned
(should verify), an authoritative-domain URL the model merely claims but
the tool never returned -- a fabricated/"fake" citation (should NOT
verify), and a tool-returned URL from a non-authoritative domain (should
NOT verify, since only ``source_authority``/``claim_support``/etc. are
even evaluated for an allow-listed domain).
"""
from __future__ import annotations

from typing import Any

import pytest
from phi_core.agents.experts import (
    _AUTHORITATIVE_LAW_DOMAINS,
    RegulationsExpert,
    _verify_research_reply,
)
from phi_core.control.testing import make_ctx

# (case_name, claimed_url, tool_returned_urls, should_verify)
LABELED_CASES: list[tuple[str, str, list[str], bool]] = [
    # authoritative domain, genuinely tool-backed -> VERIFIED
    ("ecfr_genuine", "https://www.ecfr.gov/current/title-45/section-164.514",
     ["https://www.ecfr.gov/current/title-45/section-164.514"], True),
    ("hhs_genuine", "https://www.hhs.gov/hipaa/for-professionals/privacy/index.html",
     ["https://www.hhs.gov/hipaa/for-professionals/privacy/index.html"], True),
    ("cornell_genuine", "https://www.law.cornell.edu/cfr/text/45/164.514",
     ["https://www.law.cornell.edu/cfr/text/45/164.514"], True),
    ("gdpr_info_genuine", "https://gdpr-info.eu/art-4-gdpr/",
     ["https://gdpr-info.eu/art-4-gdpr/"], True),
    ("govinfo_subdomain_genuine", "https://www.govinfo.gov/content/pkg/CFR-2020-title45-vol1/xml/CFR-2020-title45-vol1-sec164-514.xml",
     ["https://www.govinfo.gov/content/pkg/CFR-2020-title45-vol1/xml/CFR-2020-title45-vol1-sec164-514.xml"], True),
    # authoritative domain, but the model's claimed URL was never actually
    # returned by the search tool -- a fabricated/"fake" citation.
    ("ecfr_fake_citation", "https://www.ecfr.gov/current/title-45/section-999.999",
     ["https://www.hhs.gov/some-other-unrelated-page.html"], False),
    ("hhs_fake_citation", "https://www.hhs.gov/hipaa/made-up-page-that-was-not-returned.html",
     [], False),
    ("cornell_fake_citation", "https://www.law.cornell.edu/cfr/text/45/999.999",
     ["https://gdpr-info.eu/unrelated/"], False),
    # tool-backed (genuinely returned), but not an authoritative domain --
    # a real search hit from a secondary source (law firm blog, wiki).
    ("law_firm_blog_tool_backed", "https://www.somelawfirm.example/blog/hipaa-safe-harbor-explained",
     ["https://www.somelawfirm.example/blog/hipaa-safe-harbor-explained"], False),
    ("wikipedia_tool_backed", "https://en.wikipedia.org/wiki/Health_Insurance_Portability_and_Accountability_Act",
     ["https://en.wikipedia.org/wiki/Health_Insurance_Portability_and_Accountability_Act"], False),
    # neither authoritative nor tool-backed.
    ("random_blog_fabricated", "https://random-blog.example/hipaa-tips", [], False),
    ("second_random_fabricated", "https://not-a-real-source.example/law", ["https://unrelated.example/x"], False),
]


def test_verify_research_reply_matches_the_label_for_every_case():
    """Direct measurement against ``_verify_research_reply`` -- the real
    function RegulationsExpert.rules_for calls to decide whether a
    web-searched reply's citations are genuinely supporting evidence."""
    pairs: list[tuple[bool, bool]] = []
    for i, (name, url, tool_urls, label) in enumerate(LABELED_CASES):
        verified, verified_sources = _verify_research_reply(
            run_id=f"run-{i}", task_id=f"task-{i}",
            subject=f"regulations_expert:us:{name}", statement="HIPAA Safe Harbor identifier categories",
            reply_sources=[{"url": url, "title": name}],
            citations=[{"url": u} for u in tool_urls],
            allow_list=_AUTHORITATIVE_LAW_DOMAINS,
        )
        pairs.append((verified, label))
        if label:
            assert verified_sources and verified_sources[0]["url"] == url, (
                f"{name}: expected the verified source list to carry the claimed url"
            )
        else:
            assert not verified_sources, f"{name}: expected zero verified sources for a case that must not verify"

    tp = sum(1 for p, label in pairs if p and label)
    fp = sum(1 for p, label in pairs if p and not label)
    fn = sum(1 for p, label in pairs if not p and label)
    tn = sum(1 for p, label in pairs if not p and not label)
    accuracy = (tp + tn) / len(pairs)
    print(f"\n[Phase16][regulatory_evidence] D12 verification accuracy: {round(accuracy, 4)} "
          f"over {len(pairs)} labeled cases (tp={tp} fp={fp} fn={fn} tn={tn})")
    mismatches = [name for (name, _u, _t, label), (verified, _l) in zip(LABELED_CASES, pairs, strict=True) if verified != label]
    assert not mismatches, f"D12 verification disagreed with the label on: {mismatches}"
    assert fp == 0, "D12 must never verify a fabricated or non-authoritative citation (zero tolerance for a false VERIFIED)"


class ScriptedRegulationsExpert(RegulationsExpert):
    """Deterministic double for the web-search-armed LLM call: returns a
    scripted reply (a jurisdiction rules payload citing one labeled case's
    URL) plus the citations the "search tool" genuinely returned for that
    case, so the real ``rules_for``/``_hipaa_rules_for`` pipeline -- cache
    lookup, the D12 verification gate, deterministic-pack merge -- runs
    unmodified end to end."""

    def __init__(self, ctx: Any, url: str, tool_urls: list[str]) -> None:
        super().__init__(ctx)
        self._url = url
        self._tool_urls = tool_urls

    async def call_with_web_search(self, user_prompt: str, phase: str, max_uses: int = 3, **kwargs: Any):
        reply = {
            "jurisdiction": "us", "regulation": "HIPAA Safe Harbor",
            "citation": "45 CFR 164.514", "identifier_categories": {},
            "handling_rules": [], "age_aggregation_threshold": 90,
            "as_of": "2026-01-01", "sources": [{"url": self._url, "title": "scripted"}],
        }
        import json as _json
        return _json.dumps(reply), [{"url": u} for u in self._tool_urls]


@pytest.mark.asyncio
async def test_regulations_expert_rules_for_end_to_end_on_genuine_and_fake_citation():
    """Real, unstubbed ``RegulationsExpert.rules_for`` (cache-miss path,
    D12 gate, deterministic-pack merge) driven by the scripted web-search
    double -- proves the class-level integration, not just the leaf
    verification function, honors D12."""
    genuine_url = "https://www.ecfr.gov/current/title-45/section-164.514"
    ctx = make_ctx("RegulationsExpert", session_id="s1")
    expert = ScriptedRegulationsExpert(ctx, genuine_url, [genuine_url])
    reply = await expert.rules_for("us")
    assert reply["sources"] == [{"url": genuine_url, "title": "scripted"}]

    fake_url = "https://www.ecfr.gov/current/title-45/section-999.999"
    ctx2 = make_ctx("RegulationsExpert", session_id="s2")
    expert2 = ScriptedRegulationsExpert(ctx2, fake_url, [])  # tool returned nothing -- a fabricated citation
    reply2 = await expert2.rules_for("us")
    # D12 rejects the fabricated citation and RegulationsExpert falls back
    # to the deterministic pack (never an empty/broken reply, and never a
    # source list carrying the unverified URL).
    assert reply2["sources"] == []
    assert reply2["as_of"] == "deterministic-fallback"
    print("\n[Phase16][regulatory_evidence] end-to-end rules_for: genuine citation kept, "
          "fabricated citation rejected and fell back to the deterministic pack")
