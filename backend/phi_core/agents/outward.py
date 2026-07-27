"""External-facing agents: Scout (research), Ledger (benchmark), Herald (publishing)."""
from __future__ import annotations

import json
from typing import Any

from ..benchmark import run_benchmark
from ..models import CorpusRecord
from .base import Agent
from .cache import cache_get, cache_put


class Scout(Agent):
    NAME = "Scout"
    PROMPT = (
        "You are Scout. Compile a competitive landscape of PHI de-identification and PHI "
        "detection systems, both open-source (Presidio, spaCy scrubadub, philter, deid, "
        "MITRE-scrubber, DEID-GPT) and commercial (AWS Comprehend Medical, Azure Health "
        "de-identification, John Snow Labs, iSchemaView, etc.). Return JSON: "
        '{"systems": [{"name": str, "kind": "open|commercial", "vendor": str, '
        '"strengths": [str], "weaknesses": [str], "reads_row_values": bool, "citation": str}], '
        '"summary": str}. Focus on their READING policy (rows vs headers only).'
    )

    async def run(self) -> dict[str, Any]:
        cached = await cache_get(self.db, "competitor_landscape", "generic")
        if cached:
            await self._log("scout.cache_hit", "info", {})
            return json.loads(cached["content"])
        reply = await self.call_json(
            "Compile the competitive landscape of PHI detection and de-identification systems. JSON only.",
            phase="scout.compile", default={"systems": [], "summary": ""},
        )
        await cache_put(self.db, "competitor_landscape", "generic", json.dumps(reply), source="llm")
        return reply


class Ledger(Agent):
    NAME = "Ledger"
    PROMPT = (
        "You are Ledger. Given (a) our system's per-file decision counts and (b) a competitor "
        "landscape from Scout, produce a comparative benchmark report. Return JSON: "
        '{"headline": str, "our_system": {"reads_row_values": false, '
        '"decision_counts": object, "advantages": [str]}, '
        '"comparisons": [{"competitor": str, "reads_row_values": bool, '
        '"delta_notes": str}], "metrics_narrative": str, "recommendations": [str]}. '
        "Be specific about the 'headers-only' privacy invariant."
    )

    async def run(self, decisions: list[dict[str, Any]], audit: dict[str, Any], scout: dict[str, Any],
                  benchmark_result: dict[str, Any] | None = None) -> dict[str, Any]:
        prompt = (
            f"Our decisions summary (from Auditor): {audit.get('metrics', {})}\n\n"
            f"Benchmark against synthetic corpus: {benchmark_result}\n\n"
            f"Competitor landscape: {scout.get('systems', [])[:10]}\n"
            "Respond with JSON only."
        )
        return await self.call_json(prompt, phase="ledger.compare",
                                    default={"headline": "", "comparisons": [], "metrics_narrative": ""})


class Herald(Agent):
    NAME = "Herald"
    PROMPT = (
        "You are Herald, a senior scientific writer for medical informatics. Given a Ledger "
        "benchmark report and Auditor metrics, draft a research manuscript SECTION-BY-SECTION. "
        "Return JSON: "
        '{"title": str, "abstract": str, "sections": [{"heading": str, "body": str}], '
        '"references": [str], "target_venue": str, "alt_venues": [{"venue": str, "rationale": str}]}. '
        "Follow JAMIA Open / npj Digital Medicine style. Cite 45 CFR 164.514 and any relevant NIST SP 800-188."
    )

    async def run(self, ledger: dict[str, Any], audit: dict[str, Any], target_venue: str = "JAMIA Open") -> dict[str, Any]:
        prompt = (
            f"Target venue: {target_venue}\n\n"
            f"Ledger report: {ledger}\n\n"
            f"Auditor summary: {audit}\n"
            "Draft the manuscript. JSON only."
        )
        return await self.call_json(prompt, phase="herald.draft",
                                    default={"title": "", "abstract": "", "sections": [], "references": [],
                                             "target_venue": target_venue, "alt_venues": []})
