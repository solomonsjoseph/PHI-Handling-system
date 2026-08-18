"""Regulation and PHI-methods expert agents (armed with web_search).

Statute - jurisdiction regulation expert (HIPAA, DPDPA, GDPR, PIPEDA, LGPD, ...)
Praxis  - PHI transformation methods expert (jittering, k-anonymity, hashing).

Both agents follow the same cache-first / web-search-second policy:

1. Try the 7-day Mongo cache (keyed on topic + jurisdiction / category).
2. On miss, call the LLM with Claude's provider-hosted ``web_search_20250305``
   tool enabled so the answer reflects the latest primary-law text or
   published method. Citations are stored alongside the JSON reply.
3. On tool error / timeout, fall back to the deterministic pack in
   ``phi_core/jurisdictions.py`` so the pipeline never blocks on an
   external service.

Both agents return JSON with a fixed schema so the Classifier (Judge) can
consume them without additional parsing logic.
"""
from __future__ import annotations

import asyncio
import json as _json
from typing import Any

from .base import Agent
from .cache import cache_get, cache_put
from ..jurisdictions import get_pack


class Statute(Agent):
    """Regulations expert. Reads primary-law citations for a jurisdiction."""

    NAME = "Statute"
    PROMPT = (
        "You are Statute, an expert on data-protection regulations. When "
        "asked about a jurisdiction and a data category, you MUST search "
        "the web for the current primary-law text (HIPAA CFR, GDPR articles, "
        "DPDPA sections, PIPEDA, LGPD, etc.) and cite it verbatim. "
        "Return JSON only with this schema: "
        '{"jurisdiction": str, "regulation": str, "citation": str, '
        '"identifier_categories": {"letter_or_key": "description"}, '
        '"handling_rules": [{"category": str, "rule": str, '
        '"recommended_transform": str, "citation": str}], '
        '"age_aggregation_threshold": int|null, '
        '"as_of": "YYYY-MM-DD", "sources": [{"url": str, "title": str}]}. '
        "For USA use 45 CFR 164.514 HIPAA Safe Harbor and cite each subsection. "
        "For India use DPDPA 2023 + Rules 2025. Prefer primary-law citations "
        "(the actual code text or regulator gazette). If a web search returns "
        "no result, still fill the schema from your training knowledge and "
        "mark ``sources`` as an empty list."
    )

    _ADJACENT_REGIMES_FALLBACK: list[dict[str, Any]] = [
        {
            "name": "45 CFR 46 (Common Rule)",
            "citation": "45 CFR Part 46",
            "applicability": (
                "Governs human-subjects research conduct, including IRB review "
                "and informed consent. It applies to the study process, not the "
                "de-identification transformation itself."
            ),
            "advisory": (
                "This system does not perform consent or IRB-review compliance "
                "checks. The study team remains responsible for Common Rule "
                "compliance outside this tool."
            ),
            "sources": [],
        },
        {
            "name": "42 CFR Part 2",
            "citation": "42 CFR Part 2",
            "applicability": (
                "Applies to substance-use-disorder treatment records held by "
                "federally assisted programs that provide diagnosis, treatment, "
                "or referral for treatment."
            ),
            "advisory": (
                "Part 2 remains distinct and can be stricter than HIPAA where it "
                "applies, including after the 2024 final rule partially aligned "
                "its requirements with HIPAA. The study team must determine "
                "whether Part 2 applies."
            ),
            "sources": [],
        },
        {
            "name": "FERPA",
            "citation": "20 U.S.C. § 1232g; 34 CFR Part 99",
            "applicability": (
                "Governs student education records. FERPA-protected records are "
                "generally excluded from the HIPAA Privacy Rule."
            ),
            "advisory": (
                "For a school-based health study, the study team must determine "
                "which law applies based on who maintains the record."
            ),
            "sources": [],
        },
        {
            "name": "Privacy Act of 1974",
            "citation": "5 U.S.C. § 552a",
            "applicability": (
                "Applies to records containing PII maintained in a system of "
                "records by a federal agency, including research conducted by or "
                "on behalf of that agency."
            ),
            "advisory": (
                "This system does not determine whether a federal system of "
                "records or a Privacy Act disclosure condition applies."
            ),
            "sources": [],
        },
        {
            "name": "State law (non-exhaustive)",
            "citation": "",
            "applicability": (
                "State law can impose requirements stricter than the federal "
                "floor. California CMIA and CCPA are examples only."
            ),
            "advisory": (
                "This is a non-exhaustive advisory note. Per-state research is "
                "not performed because session.jurisdiction is country-level."
            ),
            "sources": [],
        },
    ]

    async def rules_for(self, jurisdiction: str) -> dict[str, Any]:
        hipaa, adjacent = await asyncio.gather(
            self._hipaa_rules_for(jurisdiction),
            self._adjacent_regimes_for(jurisdiction),
        )
        hipaa["adjacent_regimes"] = adjacent["adjacent_regimes"]
        return hipaa

    async def _hipaa_rules_for(self, jurisdiction: str) -> dict[str, Any]:
        cached = await cache_get(self.db, "regulation_rules", jurisdiction)
        if cached:
            await self._log(f"statute.cache_hit:{jurisdiction}", "info",
                            {"topic": "regulation_rules"})
            return _json.loads(cached["content"])

        pack = get_pack(jurisdiction)
        prompt = (
            f"Jurisdiction: {jurisdiction} ({pack.label}).\n"
            f"Primary regulation: {pack.regulation}.\n"
            "Task: Web-search the CURRENT primary-law text and return the "
            "identifier categories + handling rules per the schema. Include "
            "web citations. Be precise about age aggregation thresholds and "
            "any restricted geographic prefixes."
        )
        try:
            reply, citations = await self.call_json_with_web_search(
                prompt,
                phase=f"statute.web_search:{jurisdiction}",
                default={
                    "jurisdiction": jurisdiction,
                    "regulation": pack.regulation,
                    "identifier_categories": pack.identifier_categories,
                    "handling_rules": [],
                    "age_aggregation_threshold": pack.age_aggregation_threshold,
                    "as_of": "cache-fallback",
                    "sources": [],
                },
                max_uses=3,
                status_text=f"Researching {jurisdiction} data-protection law online",
            )
            # Merge tool citations if the LLM did not include them itself.
            if not reply.get("sources") and citations:
                reply["sources"] = citations
        except Exception as e:  # pragma: no cover — defensive fallback
            await self._log(f"statute.error:{jurisdiction}", "info", {"error": str(e)})
            reply = self._pack_fallback(pack)

        # Merge with the deterministic pack so downstream never sees a
        # blank identifier_categories dict even if the LLM was terse.
        if not reply.get("identifier_categories"):
            reply["identifier_categories"] = pack.identifier_categories
        if reply.get("age_aggregation_threshold") is None:
            reply["age_aggregation_threshold"] = pack.age_aggregation_threshold

        await cache_put(self.db, "regulation_rules", jurisdiction,
                        _json.dumps(reply),
                        source="web_search" if reply.get("sources") else "llm")
        return reply

    async def _adjacent_regimes_for(self, jurisdiction: str) -> dict[str, Any]:
        if jurisdiction != "us":
            return {"adjacent_regimes": []}

        cached = await cache_get(self.db, "adjacent_regulations", jurisdiction)
        if cached:
            await self._log(f"statute.cache_hit:{jurisdiction}", "info",
                            {"topic": "adjacent_regulations"})
            return _json.loads(cached["content"])

        prompt = (
            "Jurisdiction: us.\n"
            "Research these adjacent US PHI/PII regimes using primary sources "
            "only. Return JSON only with "
            '{"adjacent_regimes": [{"name": str, "citation": str, '
            '"applicability": str, "advisory": str, '
            '"sources": [{"url": str, "title": str}]}]}.\n'
            "Return exactly five entries: (1) 45 CFR 46 (Common Rule), covering "
            "IRB review and informed consent for the study process, not the "
            "de-identification transformation; its advisory must say this "
            "system performs no consent or IRB-review check. (2) 42 CFR Part 2, "
            "for substance-use-disorder treatment records from federally "
            "assisted programs, which remains stricter than HIPAA where it "
            "applies after the 2024 final rule's partial alignment. (3) FERPA, "
            "citation '20 U.S.C. § 1232g; 34 CFR Part 99', for student "
            "education records; FERPA-protected records are generally excluded "
            "from the HIPAA Privacy Rule, so school-based studies must determine "
            "which law applies from who maintains the record. (4) Privacy Act of "
            "1974, citation '5 U.S.C. § 552a', when research is conducted by or "
            "on behalf of a federal agency. (5) State law (non-exhaustive), with "
            "an empty citation and an advisory that per-state research is not "
            "performed because jurisdiction is country-level; California CMIA and "
            "CCPA may be examples only. These entries are advisory only."
        )
        try:
            reply, citations = await self.call_json_with_web_search(
                prompt,
                phase=f"statute.adjacent_web_search:{jurisdiction}",
                default={"adjacent_regimes": self._ADJACENT_REGIMES_FALLBACK},
                max_uses=3,
                expect_key="adjacent_regimes",
                min_items=5,
                status_text="Researching adjacent US data-protection regimes online",
            )
            regimes = reply.get("adjacent_regimes") if isinstance(reply, dict) else None
            if not isinstance(regimes, list) or len(regimes) < 5:
                reply = {"adjacent_regimes": self._ADJACENT_REGIMES_FALLBACK}
            elif citations:
                for regime in regimes:
                    if isinstance(regime, dict) and not regime.get("sources"):
                        regime["sources"] = citations
        except Exception as e:  # pragma: no cover — defensive fallback
            await self._log(f"statute.adjacent_error:{jurisdiction}", "info",
                            {"error": str(e)})
            reply = {"adjacent_regimes": self._ADJACENT_REGIMES_FALLBACK}

        await cache_put(
            self.db,
            "adjacent_regulations",
            jurisdiction,
            _json.dumps(reply),
            source=(
                "web_search"
                if any(regime.get("sources") for regime in reply["adjacent_regimes"])
                else "llm"
            ),
        )
        return reply

    @staticmethod
    def _pack_fallback(pack) -> dict[str, Any]:
        return {
            "jurisdiction": pack.id,
            "regulation": pack.regulation,
            "citation": pack.regulation,
            "identifier_categories": pack.identifier_categories,
            "handling_rules": [
                {"category": k, "rule": v,
                 "recommended_transform": "context-specific", "citation": pack.regulation}
                for k, v in pack.identifier_categories.items()
            ],
            "age_aggregation_threshold": pack.age_aggregation_threshold,
            "as_of": "deterministic-fallback",
            "sources": [],
        }

    async def run(self, jurisdiction: str) -> dict[str, Any]:
        return await self.rules_for(jurisdiction)


class Praxis(Agent):
    """PHI transformation methods expert. Reports candidate methods by category."""

    NAME = "Praxis"
    PROMPT = (
        "You are Praxis, an expert in PHI transformation techniques. You MUST "
        "search the web for current methods and return JSON only with this "
        'schema: {"category": str, "methods": [{"name": str, '
        '"how_to_apply": str, "why": str, "params": object, '
        '"utility_preserving": bool, "clinical_impact": str, '
        '"reference_paper": str, "sources": [{"url": str, "title": str}]}], '
        '"as_of": "YYYY-MM-DD"}. '
        "Give every distinct current method that genuinely applies. "
        "For methods requiring HIPAA Expert Determination rather than Safe "
        "Harbor, say so in that method's why field."
    )

    _SAFE_HARBOR_SOURCE = {
        "url": (
            "https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-C/"
            "part-164/subpart-E/section-164.514"
        ),
        "title": "45 CFR 164.514, Other requirements relating to uses and disclosures of protected health information",
    }
    _DETERMINISTIC_METHODS: dict[str, dict[str, Any]] = {
        "A": {
            "name": "drop", "how_to_apply": "Remove the name column.",
            "why": "Names carry no clinical signal and Safe Harbor removes them.",
            "params": {}, "utility_preserving": False,
            "clinical_impact": "No clinical signal retained.",
            "reference_paper": "45 CFR 164.514(b)(2)(i)(A)",
            "sources": [_SAFE_HARBOR_SOURCE],
        },
        "B": {
            "name": "zip3_truncate",
            "how_to_apply": "Keep the first three ZIP digits and apply the restricted-prefix deny list.",
            "why": "Safe Harbor permits only the first three ZIP digits subject to its population rule.",
            "params": {"length": 3, "restricted_prefixes_denylist": True},
            "utility_preserving": True,
            "clinical_impact": "Coarse geographic banding retained.",
            "reference_paper": "45 CFR 164.514(b)(2)(i)(B)",
            "sources": [_SAFE_HARBOR_SOURCE],
        },
        "C": {
            "name": "date_year_only + cap_age_90",
            "how_to_apply": "Replace date elements with the year and represent ages over 89 as 90+.",
            "why": "Safe Harbor removes date elements other than the year and aggregates ages over 89.",
            "params": {"age_cap": 90},
            "utility_preserving": True,
            "clinical_impact": "Annual temporal signal retained.",
            "reference_paper": "45 CFR 164.514(b)(2)(i)(C)",
            "sources": [_SAFE_HARBOR_SOURCE],
        },
        "D": {
            "name": "drop", "how_to_apply": "Remove the telephone-number column.",
            "why": "Telephone numbers carry no clinical signal and Safe Harbor removes them.",
            "params": {}, "utility_preserving": False,
            "clinical_impact": "No clinical signal retained.",
            "reference_paper": "45 CFR 164.514(b)(2)(i)(D)",
            "sources": [_SAFE_HARBOR_SOURCE],
        },
        "F": {
            "name": "drop", "how_to_apply": "Remove the email-address column.",
            "why": "Email addresses carry no clinical signal and Safe Harbor removes them.",
            "params": {}, "utility_preserving": False,
            "clinical_impact": "No clinical signal retained.",
            "reference_paper": "45 CFR 164.514(b)(2)(i)(F)",
            "sources": [_SAFE_HARBOR_SOURCE],
        },
        "G": {
            "name": "drop", "how_to_apply": "Remove the Social Security number column.",
            "why": "Social Security numbers carry no clinical signal and Safe Harbor removes them.",
            "params": {}, "utility_preserving": False,
            "clinical_impact": "No clinical signal retained.",
            "reference_paper": "45 CFR 164.514(b)(2)(i)(G)",
            "sources": [_SAFE_HARBOR_SOURCE],
        },
        "H": {
            "name": "pseudonymize",
            "how_to_apply": "Replace each value with a stable session-scoped pseudonym.",
            "why": "Preserves cross-file linkage while removing the record number.",
            "params": {"salt_source": "session_id", "hash": "sha256", "output_len": 8},
            "utility_preserving": True,
            "clinical_impact": "Cross-file linkage retained.",
            "reference_paper": "45 CFR 164.514(b)(2)(i)(H)",
            "sources": [_SAFE_HARBOR_SOURCE],
        },
    }
    _DETERMINISTIC_CATEGORIES = {"A", "D", "F", "G"}

    @classmethod
    def _fallback(cls, category: str) -> dict[str, Any]:
        method = cls._DETERMINISTIC_METHODS.get(category, {
            "name": "remove",
            "how_to_apply": "Remove the identifier column from the shared data.",
            "why": "No researched method was available; remove the identifier pending review.",
            "params": {},
            "utility_preserving": False,
            "clinical_impact": "Unknown.",
            "reference_paper": "",
            "sources": [],
        })
        return {
            "category": category,
            "methods": [method.copy()],
            "as_of": "deterministic-fallback",
        }

    async def method_for(self, category: str) -> dict[str, Any]:
        cache_topic = f"phi_method_v2:{category}"
        cached = await cache_get(self.db, cache_topic, "generic")
        if cached:
            payload = _json.loads(cached["content"])
            await self._log(
                f"praxis.cache_hit:{category}", "info",
                {"methods": len(payload.get("methods") or []),
                 "source": cached.get("source", "cache")},
            )
            return payload

        if category in self._DETERMINISTIC_CATEGORIES:
            reply = self._fallback(category)
            await self._log(
                f"praxis.deterministic:{category}", "info",
                {"method": reply["methods"][0]["name"]},
            )
            await cache_put(self.db, cache_topic, "generic", _json.dumps(reply),
                            source="deterministic")
            return reply

        pack = get_pack("us")
        description = pack.identifier_categories.get(category, category)
        prompt = (
            f"Category: {category} ({description}), under HIPAA Safe Harbor "
            f"(45 CFR 164.514(b)(2)(i)). Web-search and list the current "
            f"methods used to transform this category of PHI so the data "
            f"stays usable for research (e.g. linkable, analyzable, "
            f"comparable) without exposing the real value. For each method "
            f"return how to apply it, why it preserves utility, and its "
            f"params. Stay within Safe Harbor-compatible techniques unless "
            f"a method requires Expert Determination -- if so, say so "
            f"explicitly in that method's ``why`` field."
        )
        fallback = self._fallback(category)
        cache_source = "llm"
        try:
            reply, citations = await self.call_json_with_web_search(
                prompt, phase=f"praxis.web_search:{category}", default=fallback,
                max_uses=3, expect_key="methods", min_items=1,
                status_text=f"Researching PHI transformation methods for {description} online",
            )
            if not reply.get("methods"):
                reply = fallback
                cache_source = "deterministic"
            elif citations:
                for method in reply["methods"]:
                    if not method.get("sources"):
                        method["sources"] = citations
            if any(method.get("sources") for method in reply["methods"]):
                cache_source = "web_search"
        except Exception as e:  # pragma: no cover
            await self._log(f"praxis.error:{category}", "info", {"error": str(e)})
            reply = fallback
            cache_source = "deterministic"

        reply["category"] = category
        reply.setdefault("as_of", "web-search")
        await cache_put(self.db, cache_topic, "generic", _json.dumps(reply),
                        source=cache_source)
        return reply

    async def run(self, categories: list[str]) -> dict[str, Any]:
        return {"methods": {c: await self.method_for(c) for c in categories}}
