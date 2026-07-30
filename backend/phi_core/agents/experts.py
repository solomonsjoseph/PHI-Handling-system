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

    async def rules_for(self, jurisdiction: str) -> dict[str, Any]:
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
    """PHI transformation methods expert. Fetches the current recommended
    technique for a variable category with web citations."""

    NAME = "Praxis"
    PROMPT = (
        "You are Praxis, an expert in PHI transformation techniques. You "
        "MUST search the web for the current best-practice method for the "
        "requested category (date-jittering, k-anonymity generalisation, "
        "cryptographic hashing, tokenisation, ZIP truncation, differential "
        "privacy noise). Return JSON only with this schema: "
        '{"category": str, "technique": str, "params": object, '
        '"utility_preserving": bool, "clinical_impact": str, '
        '"reference_paper": str, "sources": [{"url": str, "title": str}]}. '
        "Prefer techniques that preserve clinical utility (e.g., age -> '90+' "
        "cap, date -> year only, ZIP -> first 3 digits with 17-code deny "
        "list). If a web search returns no result, still fill the schema from "
        "your training knowledge and mark ``sources`` empty."
    )

    _DETERMINISTIC_METHODS: dict[str, dict[str, Any]] = {
        # HIPAA-canonical, deterministic fallbacks. Only used when the LLM
        # is unreachable OR the category is well-defined without needing
        # a live web search (avoids paying for a search on trivia).
        "A": {"category": "A", "technique": "drop", "params": {},
              "utility_preserving": False, "clinical_impact": "none — names carry no clinical signal",
              "reference_paper": "HIPAA §164.514(b)(2)(i)(A)", "sources": []},
        "B": {"category": "B", "technique": "zip3_truncate", "params": {"length": 3, "restricted_prefixes_denylist": True},
              "utility_preserving": True, "clinical_impact": "coarse geographic banding retained",
              "reference_paper": "HIPAA §164.514(b)(2)(i)(B)", "sources": []},
        "C": {"category": "C", "technique": "date_year_only + cap_age_90", "params": {"age_cap": 90},
              "utility_preserving": True, "clinical_impact": "temporal signal preserved at year granularity",
              "reference_paper": "HIPAA §164.514(b)(2)(i)(C)", "sources": []},
        "D": {"category": "D", "technique": "drop", "params": {},
              "utility_preserving": False, "clinical_impact": "none — phone carries no clinical signal",
              "reference_paper": "HIPAA §164.514(b)(2)(i)(D)", "sources": []},
        "F": {"category": "F", "technique": "drop", "params": {},
              "utility_preserving": False, "clinical_impact": "none",
              "reference_paper": "HIPAA §164.514(b)(2)(i)(F)", "sources": []},
        "G": {"category": "G", "technique": "drop", "params": {},
              "utility_preserving": False, "clinical_impact": "none — SSN carries no clinical signal",
              "reference_paper": "HIPAA §164.514(b)(2)(i)(G)", "sources": []},
        "H": {"category": "H", "technique": "pseudonymize", "params": {"salt_source": "session_id", "hash": "sha256", "output_len": 8},
              "utility_preserving": True, "clinical_impact": "cross-file linkage preserved",
              "reference_paper": "HIPAA §164.514(b)(2)(i)(H)", "sources": []},
    }

    async def method_for(self, category: str) -> dict[str, Any]:
        cached = await cache_get(self.db, f"phi_method:{category}", "generic")
        if cached:
            # Log the cache hit so operators can see Praxis actually
            # consulted in the live agent-trace panel (otherwise the row
            # is missing on cached runs and it looks like Praxis was
            # skipped -- Sir Q "user must feel movement").
            payload = _json.loads(cached["content"])
            await self._log(f"praxis.cache_hit:{category}", "info",
                            {"technique": payload.get("technique"),
                             "source": cached.get("source", "cache")})
            return payload

        # For well-defined HIPAA categories with canonical techniques, skip
        # the web search (deterministic + free) and log the shortcut.
        if category in self._DETERMINISTIC_METHODS:
            reply = self._DETERMINISTIC_METHODS[category].copy()
            await self._log(f"praxis.deterministic:{category}", "info",
                            {"technique": reply["technique"]})
            await cache_put(self.db, f"phi_method:{category}", "generic",
                            _json.dumps(reply), source="deterministic")
            return reply

        prompt = (
            f"Category: {category}. Web-search the current best-practice PHI "
            "transformation for this category and return JSON per the schema. "
            "Include citations for the method AND the paper that proposed it."
        )
        try:
            reply, citations = await self.call_json_with_web_search(
                prompt, phase=f"praxis.web_search:{category}",
                default={
                    "category": category, "technique": "remove",
                    "params": {}, "utility_preserving": False,
                    "clinical_impact": "unknown — search failed",
                    "reference_paper": "", "sources": [],
                },
                max_uses=3,
            )
            if not reply.get("sources") and citations:
                reply["sources"] = citations
        except Exception as e:  # pragma: no cover
            await self._log(f"praxis.error:{category}", "info", {"error": str(e)})
            reply = {"category": category, "technique": "remove", "params": {},
                     "utility_preserving": False, "clinical_impact": "unknown",
                     "reference_paper": "", "sources": []}

        await cache_put(self.db, f"phi_method:{category}", "generic",
                        _json.dumps(reply),
                        source="web_search" if reply.get("sources") else "llm")
        return reply

    async def run(self, categories: list[str]) -> dict[str, Any]:
        return {"methods": {c: await self.method_for(c) for c in categories}}
