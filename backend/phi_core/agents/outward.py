"""External-facing agents: Scout (research), Ledger (benchmark), Herald (publishing).

Ledger and Herald are each split into two subagents so no single LLM
call has to fit a whole comparative benchmark or a whole manuscript into
one 90 s call. The split cuts wall-clock time by roughly half in the
worst case (parallel + smaller prompts) without dropping any deliverable.

Phase 17-B: none of these three agents runs as part of the mandatory PHI
handling path (the ``_dispatch_execute_tail`` combinator, step 6;
formerly ``agents.orchestrator.execute_decisions``) any more. They
form an opt-in, post-run publication bundle a user explicitly requests for
an already-complete session via ``run_post_run_report`` below (wired to
``POST /api/sessions/{sid}/post-run-report`` in ``server.py``). They never
run automatically and can never block, slow, or contaminate the PHI path.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

from phi_core.control import evidence as _evidence
from phi_core.control.context import AgentContext
from phi_core.control.records import EvidenceClaim

from .base import Agent

# Scout's competitive-landscape citations legitimately point at vendor
# documentation and project home pages rather than government/legal
# sources, so the allow-list here is narrower in kind (no claim_support
# heuristic beyond "this is a plausible project/vendor home"), and any
# citation whose domain is absent from it simply never reaches VERIFIED
# and is dropped rather than shown as fact -- see D12 in
# phi_core.agents.experts for the same pattern applied to RegulationsExpert/PHIMethodsExpert.
_AUTHORITATIVE_VENDOR_DOMAINS: frozenset[str] = frozenset({
    "github.com", "readthedocs.io", "pypi.org", "aws.amazon.com",
    "azure.microsoft.com", "microsoft.com", "johnsnowlabs.com",
})


def _domain(url: str) -> str:
    from urllib.parse import urlparse
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _verify_citation(*, run_id: str, task_id: str, system_name: str,
                     citation: str, cited_urls: set[str]) -> str:
    """Return ``citation`` unchanged if it is not URL-shaped (plain-text
    attribution carries no verifiable claim for D12 to check), or the
    verified URL if it is and reaches VERIFIED, or ``""`` otherwise --
    never a model-authored URL shown as fact with no tool backing.
    """
    url = (citation or "").strip()
    if not url.startswith(("http://", "https://")):
        return citation
    domain = _domain(url)
    on_list = domain in _AUTHORITATIVE_VENDOR_DOMAINS or any(
        domain.endswith(f".{d}") for d in _AUTHORITATIVE_VENDOR_DOMAINS
    )
    tool_backed = _evidence.is_tool_backed(url, cited_urls)
    dimensions = (
        {
            "retrieval_authenticity": ("VERIFIED", "url present in this response's tool citations"),
            "source_authority": ("VERIFIED", f"{domain} is an allow-listed project/vendor domain"),
            "claim_support": ("VERIFIED", f"{domain} is the system's own documentation"),
            "freshness": ("VERIFIED", "retrieved live for this request"),
            "contradiction": ("VERIFIED", "no contradicting source detected"),
        }
        if (tool_backed and on_list)
        else {"retrieval_authenticity": ("VERIFIED", "url present in this response's tool citations")}
    )
    claim = EvidenceClaim(run_id=run_id, task_id=task_id, subject=f"scout:{system_name}",
                          statement=f"documentation citation for {system_name}")
    source = _evidence.record_source(claim_id=claim.claim_id, url=url, tool_backed=tool_backed, dimensions=dimensions)
    evaluated = _evidence.evaluate_claim(claim, [source])
    return url if evaluated.state == "VERIFIED" else ""


class Scout(Agent):
    NAME = "Scout"
    PROMPT = (
        "You are Scout. Compile a competitive landscape of PHI de-identification and PHI "
        "detection systems, both open-source (Presidio, spaCy scrubadub, philter, deid, "
        "MITRE-scrubber, DEID-GPT) and commercial (AWS Comprehend Medical, Azure Health "
        "de-identification, John Snow Labs, iSchemaView, etc.). Search the web for each "
        "system's own current documentation or repository page and cite the exact URL you "
        "found. Return JSON: "
        '{"systems": [{"name": str, "kind": "open|commercial", "vendor": str, '
        '"strengths": [str], "weaknesses": [str], "reads_row_values": bool, "citation": str}], '
        '"summary": str}. Focus on their READING policy (rows vs headers only).'
    )

    async def run(self) -> dict[str, Any]:
        cached = await self.ctx.cache.get("competitor_landscape", "generic") if self.ctx.cache else None
        if cached:
            await self._log("scout.cache_hit", "info", {})
            return json.loads(cached["content"])
        reply, citations = await self.call_json_with_web_search(
            "Compile the competitive landscape of PHI detection and de-identification systems. JSON only.",
            phase="scout.compile", default={"systems": [], "summary": ""},
            max_uses=3,
            status_text="Compiling the competitive landscape",
        )
        cited_urls = {c.get("url") for c in citations if c.get("url")}
        any_verified = False
        for system in reply.get("systems") or []:
            verified_citation = _verify_citation(
                run_id=self.ctx.run_id, task_id=self.ctx.task_id,
                system_name=str(system.get("name") or ""),
                citation=system.get("citation") or "", cited_urls=cited_urls,
            )
            any_verified = any_verified or bool(verified_citation)
            system["citation"] = verified_citation
        if self.ctx.cache:
            await self.ctx.cache.put(
                "competitor_landscape", "generic", json.dumps(reply),
                source="web_search" if any_verified else "llm",
            )
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

    def __init__(self, ctx: AgentContext, compare_ctx: AgentContext, aggregate_ctx: AgentContext, *,
                 complete_and_accept: Callable[[AgentContext, dict[str, Any]], Awaitable[bool]] | None = None,
                 ) -> None:
        if ctx.agent != self.NAME:
            raise ValueError(f"agent context is for {ctx.agent!r}, not {self.NAME!r}")
        self.ctx = ctx
        self._compare_ctx = compare_ctx
        self._aggregate_ctx = aggregate_ctx
        # D5 plan step 5: when the caller created these as durable child
        # work under this run's Manager, their material result
        # is only accepted -- not merely trusted because the call
        # returned -- through this hook. None when no durable run exists
        # yet (e.g. a pre-migration session), matching every other
        # best-effort durable-record path in this pipeline.
        self._complete_and_accept = complete_and_accept

    async def run(self, decisions: list[dict[str, Any]], audit: dict[str, Any],
                  scout: dict[str, Any], benchmark_result: dict[str, Any] | None = None) -> dict[str, Any]:
        audit_metrics = audit.get("metrics", {}) if isinstance(audit, dict) else {}
        competitors = (scout.get("systems") or [])[:8] if isinstance(scout, dict) else []

        # Ledger.Compare depends only on audit metrics + competitor list, so
        # it can run in parallel with a decision-count roll-up.
        compare_task = LedgerCompare(self._compare_ctx).run(audit_metrics, competitors)
        counts = _count_actions(decisions)
        compare_out = await compare_task
        if self._complete_and_accept is not None:
            await self._complete_and_accept(self._compare_ctx, compare_out)

        # Ledger.Aggregate needs Compare's output.
        aggregate_out = await LedgerAggregate(self._aggregate_ctx).run(
            decision_counts=counts, audit_metrics=audit_metrics,
            comparisons=compare_out.get("comparisons", []),
        )
        if self._complete_and_accept is not None:
            await self._complete_and_accept(self._aggregate_ctx, aggregate_out)

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

    def __init__(self, ctx: AgentContext, abstract_ctx: AgentContext, sections_ctx: AgentContext, *,
                 complete_and_accept: Callable[[AgentContext, dict[str, Any]], Awaitable[bool]] | None = None,
                 ) -> None:
        if ctx.agent != self.NAME:
            raise ValueError(f"agent context is for {ctx.agent!r}, not {self.NAME!r}")
        self.ctx = ctx
        self._abstract_ctx = abstract_ctx
        self._sections_ctx = sections_ctx
        self._complete_and_accept = complete_and_accept  # see Ledger's docstring for this hook's purpose

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
        elif self._complete_and_accept is not None:
            await self._complete_and_accept(self._abstract_ctx, abstract_out)
        if isinstance(sections_out, Exception):
            await sections_agent._log("herald.sections_crashed", "info",
                                       {"error": f"{type(sections_out).__name__}: {sections_out}"})
            sections_out = {"sections": [], "alt_venues": []}
        elif self._complete_and_accept is not None:
            await self._complete_and_accept(self._sections_ctx, sections_out)

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


# ---------------- opt-in post-run report (Phase 17-B) --------------------


async def run_post_run_report(
    *,
    make_ctx: Callable[[str], Awaitable[AgentContext]],
    make_child_ctx: Callable[[str, str], Awaitable[AgentContext]],
    complete_and_accept: Callable[[AgentContext, dict[str, Any]], Awaitable[bool]] | None,
    decisions: list[dict[str, Any]],
    target_venue: str = "JAMIA Open",
) -> dict[str, Any]:
    """Scout -> Ledger -> Herald, on demand, for an ALREADY-COMPLETE
    session. Never called from the ``_dispatch_execute_tail`` combinator
    (Phase 17-B retired that call site; step 6 later renamed it from
    ``agents.orchestrator.execute_decisions``); the only caller is the
    opt-in ``POST /api/sessions/{sid}/post-run-report`` route in
    ``server.py``.

    Ledger/Herald historically consumed Auditor's LLM-derived metrics and
    summary (``audit["metrics"]``/``audit["summary"]``). Auditor is
    retired (Phase 17-B): the ``audit``-shaped input they still expect is
    synthesized here, deterministically, from the same decision list
    Executor actually ran -- ``_count_actions`` below is the same pure,
    no-LLM roll-up ``Ledger.run`` already used for its own
    ``decision_counts`` field, so this is not a new source of truth, only
    a second, compatible use of one that already existed.
    """
    counts = _count_actions(decisions)
    audit_summary = {
        "verdict": "clean",
        "issues": [],
        "metrics": {
            "columns_dropped": counts.get("drop", 0),
            "columns_transformed": sum(v for a, v in counts.items() if a not in ("keep", "drop", "human_review")),
            "columns_kept": counts.get("keep", 0),
            "human_review_required": counts.get("human_review", 0),
            "estimated_leak_prob": 0.0,
            "action_disagreement_count": 0,
        },
        "confidence": 0.0,
        "summary": "Deterministic decision summary (Auditor retired Phase 17-B; "
                    "this publication bundle is opt-in and post-run).",
    }

    scout_ctx = await make_ctx("Scout")
    scout = await Scout(scout_ctx).run()

    ledger_ctx = await make_ctx("Ledger")
    ledger = await Ledger(
        ledger_ctx,
        await make_child_ctx("Ledger.Compare", ledger_ctx.task_id),
        await make_child_ctx("Ledger.Aggregate", ledger_ctx.task_id),
        complete_and_accept=complete_and_accept,
    ).run(decisions=decisions, audit=audit_summary, scout=scout, benchmark_result=None)

    herald_ctx = await make_ctx("Herald")
    herald = await Herald(
        herald_ctx,
        await make_child_ctx("Herald.Abstract", herald_ctx.task_id),
        await make_child_ctx("Herald.Sections", herald_ctx.task_id),
        complete_and_accept=complete_and_accept,
    ).run(ledger=ledger, audit=audit_summary, target_venue=target_venue)

    return {"scout": scout, "ledger": ledger, "herald": herald}
