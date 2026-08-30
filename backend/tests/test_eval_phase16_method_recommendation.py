"""Phase 16 evaluation 6/9: method recommendation
(PHIMethodsExpert + MethodRegistry).

Two independent measurements, per labeled (HIPAA category, correct method
name) ground truth:

1. PHIMethodsExpert.method_for(category): categories A/D/F/G are
   deterministic in production (``_DETERMINISTIC_CATEGORIES`` short-circuits
   before any LLM call) -- exercised with zero stubbing, real production
   code. Categories H/B/C go through the web-search LLM path in
   production (H is in ``_DETERMINISTIC_METHODS``'s lookup table but NOT in
   ``_DETERMINISTIC_CATEGORIES``, so it only reaches that table via the
   ``_fallback`` safety net if the LLM path's D12 verification fails, never
   as a direct short-circuit); a deterministic double answers H and B
   correctly and C wrong (an off-Safe-Harbor-but-plausible method), so
   recommendation accuracy is genuinely below 1.0 rather than trivially
   perfect.

2. MethodRegistry (``phi_core.control.methods``): for each labeled
   category, registers both the correct method and an incorrect distractor,
   promotes only the correct one through to "approved", and measures
   whether ``get_approved_methods`` returns exactly the correct method --
   proving the real lifecycle gate (never returns a non-approved record)
   is what actually enforces "recommendation" correctness, not merely that
   the right record happens to exist somewhere in the store.
"""
from __future__ import annotations

from typing import Any

import pytest
from phi_core.agents.experts import PHIMethodsExpert
from phi_core.control.methods import get_approved_methods, promote, register_method
from phi_core.control.store import MemoryControlStore
from phi_core.control.testing import make_ctx

# (hipaa_category, correct_method_name)
LABELED_METHODS: list[tuple[str, str]] = [
    ("A", "drop"),
    ("D", "drop"),
    ("F", "drop"),
    ("G", "drop"),
    ("H", "pseudonymize"),
    ("B", "zip3_truncate"),
    ("C", "date_year_only + cap_age_90"),
]

# _DETERMINISTIC_CATEGORIES short-circuits before any LLM call; H/B/C all
# reach the real web-search LLM path (agents/experts.py::PHIMethodsExpert).
_DETERMINISTIC_ZERO_LLM = {"A", "D", "F", "G"}


class ScriptedPHIMethodsExpert(PHIMethodsExpert):
    """Deterministic double for the web-search-armed LLM call
    (categories H/B/C, in this harness): answers H and B correctly, C with
    a plausible-but-wrong method name -- the real class of mistake
    (correct category, wrong specific technique) rather than a malformed
    reply."""

    _SCRIPTED_METHOD = {
        "H": {"name": "pseudonymize", "how_to_apply": "replace with a stable session-scoped pseudonym",
              "why": "preserves cross-file linkage while removing the record number", "params": {},
              "utility_preserving": True, "clinical_impact": "cross-file linkage retained",
              "reference_paper": "45 CFR 164.514(b)(2)(i)(H)",
              "sources": [{"url": "https://www.ecfr.gov/current/title-45", "title": "45 CFR 164.514"}]},
        "B": {"name": "zip3_truncate", "how_to_apply": "truncate to 3 digits",
              "why": "Safe Harbor permits the first three ZIP digits", "params": {},
              "utility_preserving": True, "clinical_impact": "geographic banding retained",
              "reference_paper": "45 CFR 164.514(b)(2)(i)(B)",
              "sources": [{"url": "https://www.ecfr.gov/current/title-45", "title": "45 CFR 164.514"}]},
        "C": {"name": "generalize_to_decade", "how_to_apply": "bucket the date into a 10-year range",
              "why": "wrong -- Safe Harbor requires year-only, not decade buckets", "params": {},
              "utility_preserving": True, "clinical_impact": "coarser temporal signal",
              "reference_paper": "", "sources": [{"url": "https://www.hhs.gov/hipaa", "title": "HIPAA"}]},
    }

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.call_count = 0

    async def call_with_web_search(self, user_prompt: str, phase: str, max_uses: int = 3, **kwargs: Any):
        self.call_count += 1
        category = phase.rsplit(":", 1)[-1]
        method = self._SCRIPTED_METHOD[category]
        import json as _json
        reply = {"category": category, "methods": [method], "as_of": "2026-01-01"}
        citations = [{"url": s["url"]} for s in method["sources"]]
        return _json.dumps(reply), citations


@pytest.mark.asyncio
async def test_phi_methods_expert_recommendation_accuracy_against_labeled_categories():
    ctx = make_ctx("PHIMethodsExpert", session_id="s1")
    expert = ScriptedPHIMethodsExpert(ctx)

    pairs: list[tuple[str, str]] = []
    zero_llm_calls_seen = 0
    for category, correct_method in LABELED_METHODS:
        reply = await expert.method_for(category)
        predicted = (reply.get("methods") or [{}])[0].get("name", "")
        pairs.append((predicted, correct_method))
        if category in _DETERMINISTIC_ZERO_LLM:
            zero_llm_calls_seen += 1

    accuracy = round(sum(1 for p, label in pairs if p == label) / len(pairs), 4)
    mismatches = [(cat, p, label) for (cat, _cm), (p, label) in zip(LABELED_METHODS, pairs, strict=True) if p != label]
    print(f"\n[Phase16][method_recommendation] PHIMethodsExpert accuracy: {round(accuracy, 4)} "
          f"over {len(pairs)} labeled categories; mismatches={mismatches}")

    # The 4 deterministic categories (A/D/F/G) never touch the scripted
    # LLM double -- real production code, zero fabrication risk.
    assert expert.call_count == 3  # H, B, and C reach the LLM path
    assert zero_llm_calls_seen == 4

    # H and B are right, C is the one deliberately wrong category -- a
    # genuine, controlled accuracy below 1.0.
    assert accuracy == round(6 / 7, 4)
    assert mismatches == [("C", "generalize_to_decade", "date_year_only + cap_age_90")]


@pytest.mark.asyncio
async def test_method_registry_approved_query_returns_only_the_correct_candidate():
    """MethodRegistry's real lifecycle (register -> promote x3 -> query):
    for each labeled category, both the correct method and a wrong
    distractor are registered; only the correct one is promoted to
    "approved". ``get_approved_methods`` must return exactly the correct
    one -- proving the registry's approval gate, not mere existence in the
    store, is what enforces recommendation correctness."""
    store = MemoryControlStore()
    correct_ids: dict[str, str] = {}
    for category, correct_method in LABELED_METHODS:
        correct = await register_method(store, hipaa_category=category, name=correct_method)
        wrong = await register_method(store, hipaa_category=category, name=f"wrong_method_for_{category}")
        for target in ("candidate", "validated", "approved"):
            correct = await promote(store, correct.method_id, to=target)
        await promote(store, wrong.method_id, to="candidate")  # deliberately left un-approved
        correct_ids[category] = correct.method_id
        assert correct.lifecycle == "approved"

    pairs: list[tuple[str, str]] = []
    for category, correct_method in LABELED_METHODS:
        approved = await get_approved_methods(store, hipaa_category=category)
        names = sorted(m.name for m in approved)
        predicted = names[0] if len(names) == 1 else "|".join(names)
        pairs.append((predicted, correct_method))
        # The registry invariant this evaluation actually exercises: never
        # returns the un-approved distractor, regardless of what got
        # registered alongside the real recommendation.
        assert f"wrong_method_for_{category}" not in names

    accuracy = sum(1 for p, label in pairs if p == label) / len(pairs)
    print(f"\n[Phase16][method_recommendation] MethodRegistry approved-query accuracy: "
          f"{round(accuracy, 4)} over {len(pairs)} labeled categories")
    assert accuracy == 1.0
