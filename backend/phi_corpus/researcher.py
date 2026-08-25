"""Corpus Research agent — reverse-engineer real study patterns (Phase C3).

Sir's original spec:

    "corpus generator [must] research the web for different kinds of study,
    what kind of data they have, most data you find would have been
    deidentified but you have to reverse engg and find out what got
    deidentified."

The CorpusResearcher agent web-searches ClinicalTrials.gov, PubMed, and
Nature Data catalogs for a real de-identified public dataset in the
requested study domain. From the study description it reverse-engineers
the ORIGINAL raw columns that would have existed pre-Safe-Harbor (e.g. a
public dataset shows ``age_at_diagnosis`` and ``diagnosis_year`` — before
de-identification the raw columns were ``date_of_diagnosis`` (full date)
and ``date_of_birth``). It returns a ``Scenario``-shaped JSON so the
existing planter can synthesise realistic corpora on top of it.

The agent uses the same ``call_with_web_search`` plumbing Statute +
Praxis already have. Cache-first (7-day TTL) so the same domain is not
re-searched twice. Falls back cleanly if web-search fails: returns the
error rather than a hallucinated scenario (never plant PHI we have not
grounded in a real study description).
"""
from __future__ import annotations

import json as _json
from typing import Any

from phi_core.agents.base import Agent
from phi_core.agents.experts import _verify_research_reply

# Real public-dataset repositories the researcher's own prompt instructs
# it to search; see D12 in phi_core.agents.experts for why these five
# dimensions are the deterministic proxy used in place of a full content
# re-fetch, and why an unverified reply falls back to a refusal here
# exactly as an unsourced reply always has.
_AUTHORITATIVE_DATASET_DOMAINS: frozenset[str] = frozenset({
    "clinicaltrials.gov", "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov",
    "nature.com", "zenodo.org", "hdruk.ac.uk", "ukbiobank.ac.uk", "physionet.org",
})


class CorpusResearcher(Agent):
    """Discover a real-life scenario for a study domain."""

    NAME = "CorpusResearcher"
    PROMPT = (
        "You are a corpus research assistant. Your job is to reverse-"
        "engineer a realistic PHI Console study scenario from a real "
        "public de-identified dataset. When asked about a study domain "
        "(e.g. 'cardiology outcomes', 'diabetes cohort', 'oncology "
        "screening'), you MUST search the web at ClinicalTrials.gov, "
        "PubMed, HDR-UK, Nature Data catalogs, or Zenodo, find ONE "
        "concrete de-identified public dataset, then extrapolate the "
        "raw PHI columns that would have existed BEFORE Safe Harbor was "
        "applied.\n\n"
        "Return JSON ONLY with this exact schema:\n"
        "{\n"
        '  "scenario_id": "snake_case_id",\n'
        '  "label": "Human readable label",\n'
        '  "jurisdictions": ["us"],\n'
        '  "source_study": {"title": str, "sponsor": str, "url": str, '
        '"nct_id": str|null, "accessed_at": "YYYY-MM-DD"},\n'
        '  "datasets": [{\n'
        '    "filename": "enrollment.csv",\n'
        '    "columns": [\n'
        '      {"name": "patient_id", "hipaa_category": "H", '
        '"expected_action": "pseudonymize", '
        '"generator_hint": "MRN or study-scoped identifier"},\n'
        '      ...\n'
        '    ]\n'
        '  }],\n'
        '  "dictionary": [{"column_name": str, "description": str, '
        '"type": "string"|"int"|"float"|"date"}],\n'
        '  "sources": [{"url": str, "title": str}]\n'
        "}\n\n"
        "HIPAA categories: A=Names, B=Geo(ZIP), C=Dates+AgeOver89, "
        "D=Phone, E=Fax, F=Email, G=SSN, H=MRN, I=Beneficiary#, "
        "J=Account#, K=License/NPI, L=Vehicle, M=Device serial, "
        "N=URL, O=IP, P=Biometric, Q=Photo, R=Any other identifier. "
        "Use 'NONE' for legitimate clinical variables.\n\n"
        "Expected actions: drop | keep | year_only | zip3_truncate | "
        "cap_age_90 | pseudonymize | scrub_text.\n\n"
        "Rules:\n"
        "* At least 8 columns, at most 15\n"
        "* Include both PHI and clinical variables in realistic ratio\n"
        "* Every column MUST cite a real column in the source study OR "
        "state 'inferred from context' in the dictionary description\n"
        "* NEVER invent a study; if the search returns no result, return "
        "{\"error\": \"no source study found for <domain>\", \"sources\": []}"
    )

    async def research(self, domain: str) -> dict[str, Any]:
        """Return a Scenario-shaped JSON for the requested study domain."""
        cache_key = domain.strip().lower()
        cached = await self.ctx.cache.get("corpus_scenario", cache_key) if self.ctx.cache else None
        if cached:
            await self._log(
                f"researcher.cache_hit:{cache_key}", "info",
                {"topic": "corpus_scenario"},
            )
            return _json.loads(cached["content"])

        prompt = (
            f"Study domain: {domain}.\n"
            "Task: Web-search for ONE real de-identified public dataset "
            "in this domain. Then reverse-engineer the raw PHI columns "
            "that would have existed before Safe Harbor was applied. "
            "Return JSON per the schema. Include source citations."
        )
        try:
            reply, citations = await self.call_json_with_web_search(
                prompt, phase=f"researcher.web_search:{cache_key}",
                default={"error": "web_search returned no result",
                         "sources": []},
                max_uses=3,
            )
            if isinstance(reply, dict) and not reply.get("error"):
                verified, verified_sources = _verify_research_reply(
                    run_id=self.ctx.run_id, task_id=self.ctx.task_id,
                    subject=f"corpus_researcher:{cache_key}",
                    statement=f"real de-identified public dataset for study domain {domain!r}",
                    reply_sources=reply.get("sources"), citations=citations,
                    allow_list=_AUTHORITATIVE_DATASET_DOMAINS,
                )
                # Grounding gate: refuse to return a scenario with no
                # tool-backed, verified source -- a model-authored URL
                # absent from the response's own citations is never
                # accepted as grounding (F-EVID-001). This is strictly
                # tighter than the previous "has any sources" check.
                if not verified:
                    reply = {
                        "error": (
                            "researcher refused: no verified, tool-backed web "
                            "citation attached to the returned scenario. Corpus "
                            "MUST be grounded in a real study."
                        ),
                        "sources": [],
                        "candidate": reply,
                    }
                else:
                    reply["sources"] = verified_sources
        except Exception as e:  # pragma: no cover
            await self._log(f"researcher.error:{cache_key}", "info", {"error": str(e)})
            return {"error": f"{type(e).__name__}: {e}", "sources": []}

        if self.ctx.cache:
            await self.ctx.cache.put(
                "corpus_scenario", cache_key,
                _json.dumps(reply),
                source="web_search" if reply.get("sources") else "error",
            )
        return reply
