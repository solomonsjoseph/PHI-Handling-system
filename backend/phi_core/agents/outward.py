"""External-facing agents: Scout (research), Ledger (benchmark), Herald (publishing).

Ledger and Herald are each split into two subagents so no single LLM
call has to fit a whole comparative benchmark or a whole manuscript into
one 90 s call. The split cuts wall-clock time by roughly half in the
worst case (parallel + smaller prompts) without dropping any deliverable.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from phi_core.control.context import AgentContext

from .base import Agent


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
        cached = await self.ctx.cache.get("competitor_landscape", "generic") if self.ctx.cache else None
        if cached:
            await self._log("scout.cache_hit", "info", {})
            return json.loads(cached["content"])
        reply = await self.call_json(
            "Compile the competitive landscape of PHI detection and de-identification systems. JSON only.",
            phase="scout.compile", default={"systems": [], "summary": ""},
            status_text="Compiling the competitive landscape",
        )
        if self.ctx.cache:
            await self.ctx.cache.put("competitor_landscape", "generic", json.dumps(reply), source="llm")
        return reply


# ---------------- LEDGER (split into Compare + Aggregate) ----------------


class LedgerCompare(Agent):
    """Subagent: per-competitor delta narrative (short, one call ~15-20 s)."""
    NAME = "Ledger.Compare"
    PROMPT = (
        "You are Ledger.Compare. Given (a) our system's Auditor metrics and (b) up to 8 "
        "competitor systems from Scout, produce ONE JSON array of delta notes. Return JSON: "
        '{"comparisons": [{"competitor": str, "reads_row_values": bool, "delta_notes": str}]}. '
        "Focus on the 'headers-only' privacy invariant vs each competitor. Keep each delta note "
        "under 40 words. Skip vendors that are duplicates."
    )

    async def run(self, audit_metrics: dict[str, Any], competitors: list[dict[str, Any]]) -> dict[str, Any]:
        prompt = (
            f"Our metrics: {audit_metrics}\n\n"
            f"Competitor systems ({len(competitors)}): {competitors}\n"
            "Respond with JSON only."
        )
        return await self.call_json(prompt, phase="ledger.compare",
                                    default={"comparisons": []},
                                    status_text="Comparing against competitor systems")


class LedgerAggregate(Agent):
    """Subagent: rollup headline + narrative + recommendations (~15-20 s)."""
    NAME = "Ledger.Aggregate"
    PROMPT = (
        "You are Ledger.Aggregate. Given our system's decision counts, Auditor metrics, and a "
        "list of competitor delta notes from Ledger.Compare, write the rollup. Return JSON: "
        '{"headline": str, "our_system": {"reads_row_values": false, '
        '"decision_counts": object, "advantages": [str]}, '
        '"metrics_narrative": str, "recommendations": [str]}. '
        "Keep metrics_narrative under 120 words. Cite 45 CFR 164.514 explicitly."
    )

    async def run(self, decision_counts: dict[str, int], audit_metrics: dict[str, Any],
                  comparisons: list[dict[str, Any]]) -> dict[str, Any]:
        prompt = (
            f"Our decision counts: {decision_counts}\n\n"
            f"Our metrics: {audit_metrics}\n\n"
            f"Delta notes from Ledger.Compare ({len(comparisons)}): {comparisons}\n"
            "Respond with JSON only."
        )
        return await self.call_json(prompt, phase="ledger.aggregate",
                                    default={"headline": "", "our_system": {}, "metrics_narrative": "",
                                             "recommendations": []},
                                    status_text="Rolling up benchmark metrics")

class Ledger:
    """Combined Ledger driver. Runs Compare + Aggregate as two smaller LLM
    calls (parallel where dependency allows) and merges the outputs into
    the same shape the old monolithic Ledger returned."""
    NAME = "Ledger"

    def __init__(self, ctx: AgentContext, compare_ctx: AgentContext, aggregate_ctx: AgentContext) -> None:
        if ctx.agent != self.NAME:
            raise ValueError(f"agent context is for {ctx.agent!r}, not {self.NAME!r}")
        self.ctx = ctx
        self._compare_ctx = compare_ctx
        self._aggregate_ctx = aggregate_ctx

    async def run(self, decisions: list[dict[str, Any]], audit: dict[str, Any],
                  scout: dict[str, Any], benchmark_result: dict[str, Any] | None = None) -> dict[str, Any]:
        audit_metrics = audit.get("metrics", {}) if isinstance(audit, dict) else {}
        competitors = (scout.get("systems") or [])[:8] if isinstance(scout, dict) else []

        # Ledger.Compare depends only on audit metrics + competitor list, so
        # it can run in parallel with a decision-count roll-up.
        compare_task = LedgerCompare(self._compare_ctx).run(audit_metrics, competitors)
        counts = _count_actions(decisions)
        compare_out = await compare_task

        # Ledger.Aggregate needs Compare's output.
        aggregate_out = await LedgerAggregate(self._aggregate_ctx).run(
            decision_counts=counts, audit_metrics=audit_metrics,
            comparisons=compare_out.get("comparisons", []),
        )

        # Merge into the legacy Ledger schema.
        return {
            "headline": aggregate_out.get("headline", ""),
            "our_system": aggregate_out.get("our_system", {"decision_counts": counts}),
            "comparisons": compare_out.get("comparisons", []),
            "metrics_narrative": aggregate_out.get("metrics_narrative", ""),
            "recommendations": aggregate_out.get("recommendations", []),
            "benchmark_result": benchmark_result,
        }


def _count_actions(decisions: list[dict[str, Any]]) -> dict[str, int]:
    """Deterministic decision-count roll-up. No LLM call needed."""
    out: dict[str, int] = {}
    for d in decisions or []:
        a = str(d.get("action", "unknown"))
        out[a] = out.get(a, 0) + 1
    return out


# ---------------- HERALD (split into Abstract + Full) --------------------


class HeraldAbstract(Agent):
    """Subagent: title, abstract, methods, references. Small, ~30-45 s."""
    NAME = "Herald.Abstract"
    PROMPT = (
        "You are Herald.Abstract, a senior scientific writer for medical informatics. "
        "Draft the FIRST HALF of a manuscript: title, abstract (250-word max), and methods "
        "section. Return JSON: "
        '{"title": str, "abstract": str, "methods": {"heading": "Methods", "body": str}, '
        '"references": [str]}. '
        "Follow JAMIA Open / npj Digital Medicine style. Cite 45 CFR 164.514 and NIST SP 800-188."
    )

    async def run(self, ledger: dict[str, Any], audit: dict[str, Any],
                  target_venue: str) -> dict[str, Any]:
        prompt = (
            f"Target venue: {target_venue}\n\n"
            f"Ledger headline + narrative: {ledger.get('headline')}. {ledger.get('metrics_narrative')}\n\n"
            f"Auditor summary: {audit}\n"
            "Draft title, abstract, methods, references. JSON only."
        )
        return await self.call_json(prompt, phase="herald.abstract",
                                    default={"title": "", "abstract": "",
                                             "methods": {"heading": "Methods", "body": ""},
                                             "references": []},
                                    status_text="Drafting the publication abstract")


class HeraldSections(Agent):
    """Subagent: results, discussion, limitations, conclusion. ~30-45 s."""
    NAME = "Herald.Sections"
    PROMPT = (
        "You are Herald.Sections. Draft the SECOND HALF of a manuscript: results, discussion, "
        "limitations, and conclusion sections. Return JSON: "
        '{"sections": [{"heading": str, "body": str}], '
        '"alt_venues": [{"venue": str, "rationale": str}]}. '
        "Order sections as Results, Discussion, Limitations, Conclusion. Each body 120-220 words. "
        "Do NOT restate the study aim, methodology overview, or dataset summary that belongs in "
        "the abstract or methods; assume those exist verbatim upstream and start each section "
        "from the numeric or thematic point of interest."
    )

    async def run(self, ledger: dict[str, Any], audit: dict[str, Any],
                  target_venue: str) -> dict[str, Any]:
        prompt = (
            f"Target venue: {target_venue}\n\n"
            f"Ledger comparisons: {ledger.get('comparisons', [])[:8]}\n\n"
            f"Auditor metrics: {audit.get('metrics', {}) if isinstance(audit, dict) else {}}\n"
            "Draft results, discussion, limitations, conclusion. JSON only."
        )
        return await self.call_json(prompt, phase="herald.sections",
                                    default={"sections": [], "alt_venues": []},
                                    status_text="Drafting results, discussion, and limitations")


class Herald:
    """Combined Herald driver. Runs Abstract + Sections in TRUE parallel
    (both are independent LLM calls now that the Sections prompt is
    instructed to skip restating the aim/methodology). Saves ~30-50 s of
    wallclock on a typical run without losing manuscript coherence, since
    human authors edit the merged draft anyway.

    If either subagent times out, the other's output is still returned so
    the publication bundle is never empty."""
    NAME = "Herald"

    def __init__(self, ctx: AgentContext, abstract_ctx: AgentContext, sections_ctx: AgentContext) -> None:
        if ctx.agent != self.NAME:
            raise ValueError(f"agent context is for {ctx.agent!r}, not {self.NAME!r}")
        self.ctx = ctx
        self._abstract_ctx = abstract_ctx
        self._sections_ctx = sections_ctx

    async def run(self, ledger: dict[str, Any], audit: dict[str, Any],
                  target_venue: str = "JAMIA Open") -> dict[str, Any]:
        abstract_agent = HeraldAbstract(self._abstract_ctx)
        sections_agent = HeraldSections(self._sections_ctx)
        abstract_out, sections_out = await asyncio.gather(
            abstract_agent.run(ledger, audit, target_venue),
            sections_agent.run(ledger, audit, target_venue),
            return_exceptions=True,
        )
        # LLM timeouts are already handled inside call_json (returns a
        # default dict, never raises); an exception surviving to here is an
        # unexpected bug in one subagent and must not blank out the whole
        # manuscript draft when the other subagent succeeded.
        if isinstance(abstract_out, Exception):
            await abstract_agent._log("herald.abstract_crashed", "info",
                                       {"error": f"{type(abstract_out).__name__}: {abstract_out}"})
            abstract_out = {"title": "", "abstract": "", "methods": None, "references": []}
        if isinstance(sections_out, Exception):
            await sections_agent._log("herald.sections_crashed", "info",
                                       {"error": f"{type(sections_out).__name__}: {sections_out}"})
            sections_out = {"sections": [], "alt_venues": []}

        methods = abstract_out.get("methods") or {"heading": "Methods", "body": ""}
        sections = [methods] + (sections_out.get("sections") or [])
        return {
            "title": abstract_out.get("title", ""),
            "abstract": abstract_out.get("abstract", ""),
            "sections": sections,
            "references": abstract_out.get("references", []),
            "target_venue": target_venue,
            "alt_venues": sections_out.get("alt_venues", []),
        }
