"""Regulation and PHI-methods expert agents.

Statute - jurisdiction regulation expert (HIPAA, DPDPA, GDPR, LGPD, ...).
Praxis  - PHI transformation methods expert (jittering, generalisation, hashing).

Both use cache-first LLM knowledge with optional web-search fallback via the
underlying LLM's browsing capability.
"""
from __future__ import annotations

from typing import Any

from .base import Agent
from .cache import cache_get, cache_put


class Statute(Agent):
    NAME = "Statute"
    PROMPT = (
        "You are Statute, an expert on data-protection regulations. When asked about a "
        "jurisdiction and a data category, return JSON: "
        '{"jurisdiction": str, "regulation": str, "citation": str, "handling_rules": '
        '[{"category": str, "rule": str, "recommended_transform": str}], "as_of": str}. '
        "For USA use 45 CFR 164.514 HIPAA Safe Harbor and cite each subsection. "
        "For India use DPDPA 2023 + Rules 2025. Prefer primary law citations."
    )

    async def rules_for(self, jurisdiction: str) -> dict[str, Any]:
        cached = await cache_get(self.db, "regulation_rules", jurisdiction)
        if cached:
            await self._log(f"statute.cache_hit:{jurisdiction}", "info", {"topic": "regulation_rules"})
            import json as _json
            return _json.loads(cached["content"])
        reply = await self.call_json(
            f"Jurisdiction: {jurisdiction}. List the direct-identifier categories and the "
            "recommended handling for each, with primary-law citations. JSON only.",
            phase=f"statute.rules_for:{jurisdiction}",
            default={"jurisdiction": jurisdiction, "handling_rules": []},
        )
        import json as _json
        await cache_put(self.db, "regulation_rules", jurisdiction, _json.dumps(reply), source="llm")
        return reply

    async def run(self, jurisdiction: str) -> dict[str, Any]:
        return await self.rules_for(jurisdiction)


class Praxis(Agent):
    NAME = "Praxis"
    PROMPT = (
        "You are Praxis, an expert in PHI transformation techniques (date jittering, "
        "k-anonymity generalisation, cryptographic hashing, tokenisation, ZIP truncation). "
        "When asked for a technique for a variable category, return JSON: "
        '{"category": str, "technique": str, "params": object, "utility_preserving": bool, '
        '"citation": str}. Prefer techniques that preserve clinical utility (e.g., age -> '
        "'90+' cap, date -> year only, ZIP -> first 3 digits with 17-code deny list)."
    )

    async def method_for(self, category: str) -> dict[str, Any]:
        cached = await cache_get(self.db, f"phi_method:{category}", "generic")
        if cached:
            import json as _json
            return _json.loads(cached["content"])
        reply = await self.call_json(
            f"Category: {category}. Return the best PHI transformation. JSON only.",
            phase=f"praxis.method_for:{category}",
            default={"category": category, "technique": "remove", "params": {}},
        )
        import json as _json
        await cache_put(self.db, f"phi_method:{category}", "generic", _json.dumps(reply), source="llm")
        return reply

    async def run(self, categories: list[str]) -> dict[str, Any]:
        return {"methods": {c: await self.method_for(c) for c in categories}}
