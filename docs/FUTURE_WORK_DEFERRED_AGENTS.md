# Future work: deferred agents

This file preserves, as concepts only, the components removed from code during the
agent-driven pipeline rewrite. Source was read from `backend/phi_core/agents/outward.py`
and `backend/phi_corpus/` before those files were deleted. Prompts and output schemas are
copied verbatim inside fenced blocks. Line numbers are from the pre-deletion tree.

For each component: purpose, exact inputs, exact output shape, pipeline attachment point,
why deferred, and what would have to exist for it to return.

---

## Scout

### Purpose
Compile a competitive landscape of PHI de-identification and PHI detection systems, with
an emphasis on each system's reading policy (header-only versus row-value reading).

### Inputs
None (self-contained). Uses `call_json_with_web_search` with `max_uses=3`. Reads and writes
a 7-day cache under the key `("competitor_landscape", "generic")`.

### Output
`{"systems": [...], "summary": str}`, where each system is
`{"name", "kind", "vendor", "strengths", "weaknesses", "reads_row_values", "citation"}`.
Citations are post-filtered through `_verify_citation` against the authoritative vendor
domain allowlist; a citation whose domain is absent is dropped rather than surfaced.

### Attachment point
Opt-in post-run report (`run_post_run_report`), reached from
`POST /api/sessions/{sid}/post-run-report` after a run had already completed.

### Why deferred
The pipeline becomes a single agent-driven run under the Manager. Competitive landscape and
benchmark publishing are out of scope for an end-to-end de-identification run.

### Return condition
A real post-run competitive-analysis step would need to be reinstated behind the Manager,
with a web-enabled agent grant, the `_verify_citation` allowlist, and a report surface to
write into.

### Verbatim prompt and output schema

```python
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
```

---

## Ledger (driver; no prompt)

`NAME = "Ledger"`. Runs `LedgerCompare` and `LedgerAggregate` as two smaller LLM calls and
merges their outputs. The driver itself issues no LLM call.

Constructor:
`Ledger(ctx, compare_ctx, aggregate_ctx, *, complete_and_accept=None)`.

`run(decisions, audit, scout, benchmark_result=None) -> dict`:

```python
{
    "headline": str,
    "our_system": {"decision_counts": dict},
    "comparisons": [...],
    "metrics_narrative": str,
    "recommendations": [str],
    "benchmark_result": None,
}
```

### Ledger.Compare

Purpose: per-competitor delta narrative. Output: `{"comparisons": [{"competitor",
"reads_row_values", "delta_notes"}]}`.

```python
    PROMPT = (
        "You are Ledger.Compare. Given (a) our system's Auditor metrics and (b) up to 8 "
        "competitor systems from Scout, produce ONE JSON array of delta notes. Return JSON: "
        '{"comparisons": [{"competitor": str, "reads_row_values": bool, "delta_notes": str}]}. '
        "Focus on the 'headers-only' privacy invariant vs each competitor. Keep each delta note "
        "under 40 words. Skip vendors that are duplicates."
    )
```

### Ledger.Aggregate

Purpose: rollup headline, narrative, and recommendations from decision counts plus Compare's
delta notes. Output:
`{"headline", "our_system": {"reads_row_values": false, "decision_counts", "advantages"},
"metrics_narrative", "recommendations"}`.

```python
    PROMPT = (
        "You are Ledger.Aggregate. Given our system's decision counts, Auditor metrics, and a "
        "list of competitor delta notes from Ledger.Compare, write the rollup. Return JSON: "
        '{"headline": str, "our_system": {"reads_row_values": false, '
        '"decision_counts": object, "advantages": [str]}, '
        '"metrics_narrative": str, "recommendations": [str]}. '
        "Keep metrics_narrative under 120 words. Cite 45 CFR 164.514 explicitly."
    )
```

### Why deferred
Same as Scout: benchmark and competitive publishing are not part of the single agent-driven
run. Kept as a concept because the headers-only invariant framing versus competitors remains
archivable value.

---

## Herald (driver; no prompt)

`NAME = "Herald"`. Runs `HeraldAbstract` and `HeraldSections` in true parallel (independent
LLM calls) and merges them. The driver issues no LLM call.

Constructor:
`Herald(ctx, abstract_ctx, sections_ctx, *, complete_and_accept=None)`.

`run(ledger, audit, target_venue="JAMIA Open") -> dict`:

```python
{
    "title": str,
    "abstract": str,
    "sections": [{"heading": "Methods", "body": str}, ...],
    "references": [str],
    "target_venue": str,
    "alt_venues": [{"venue": str, "rationale": str}],
}
```

### Herald.Abstract

Purpose: first half of a manuscript, title, abstract (250-word max), and methods. Output:
`{"title", "abstract", "methods": {"heading": "Methods", "body"}, "references"}`.

```python
    PROMPT = (
        "You are Herald.Abstract, a senior scientific writer for medical informatics. "
        "Draft the FIRST HALF of a manuscript: title, abstract (250-word max), and methods "
        "section. Return JSON: "
        '{"title": str, "abstract": str, "methods": {"heading": "Methods", "body": str}, '
        '"references": [str]}. '
        "Follow JAMIA Open / npj Digital Medicine style. Cite 45 CFR 164.514 and NIST SP 800-188."
    )
```

### Herald.Sections

Purpose: second half of a manuscript, results, discussion, limitations, conclusion. Output:
`{"sections": [{"heading", "body"}], "alt_venues": [{"venue", "rationale"}]}`.

```python
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
```

### Why deferred
Same as Scout and Ledger: publication drafting is out of scope for the single agent-driven run.

---

## `run_post_run_report` and the synthesized audit summary

`run_post_run_report` guarded the opt-in publication path. Ledger and Herald historically
consumed Auditor's LLM-derived `audit["metrics"]` and `audit["summary"]`; Auditor is retired,
so `run_post_run_report` synthesized an Auditor-shaped `audit_summary` deterministically from
the decision list Executor actually ran, using `_count_actions` (a pure, no-LLM roll-up). The
synthesized shape:

```python
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
```

If any of these components returns, it must not rely on a real Auditor agent; use the
same deterministic synthesis (header-safe, no row values).

---

## CorpusResearcher

### Purpose
Reverse-engineer a realistic study scenario from a real public de-identified dataset. The
agent web-searches ClinicalTrials.gov, PubMed, HDR-UK, Nature Data catalogs, and Zenodo for
one concrete public dataset, then extrapolates the raw PHI columns that would have existed
before Safe Harbor was applied. Cache-first with a 7-day TTL; a failed search returns an
error rather than a hallucinated scenario.

### Inputs
`research(domain: str)`.

### Output
A `Scenario`-shaped JSON; see the schema below.

### Attachment point
`backend/phi_corpus/` corpus generation, replaced by the study picker over `data/test_data/`.

### Why deferred
Synthetic corpus research and generation are replaced by real, study-team-shipped datasets
selected from the UI.

### Return condition
A real corpus generator would need a web-enabled grant, the `_verify_research_reply` reply
check, the 7-day cache, and a planter downstream of it.

### Verbatim prompt and output schema

```python
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
```

---

## Corpus generator

Modules under `backend/phi_corpus/` (pre-deletion):

| Module | Role |
| --- | --- |
| `benchmark.py` | Build benchmark reports from ground truth versus decisions |
| `campaign.py` | Multi-study benchmark campaign orchestration |
| `edge_cases.py` | Edge-case tag catalogue for adversarial fixtures |
| `generate.py` | Corpus generation entry point |
| `instruments.py` | Form and instrument synthesis |
| `planters.py` | Plant synthetic datasets from a Scenario (see below) |
| `realism.py` | Value-realism shaping for planted fields |
| `replay.py` | Replay of prior runs / fixtures |
| `report.py` | Corpus report output |
| `researcher.py` | CorpusResearcher agent (see above) |
| `scenarios.py` | Scenario definitions |
| `study_data/__init__.py` | Study-data loader (repointed to `data/test_data/`, kept) |
| `tiers.py` | Corpus difficulty tiers |
| `verify.py` | Post-export leak scan (see below) |

Three capabilities worth rebuilding:

- `planters.plant(scenario_id, jurisdiction="us", edge_case_tags=None)` — synthesize a
  synthetic corpus on top of a researched scenario, consuming edge-case tags.
- `verify.scan_exports_for_leaks(ground_truth, export_paths, file_name_map=None)` — scan the
  produced exports for planted PHI that must have been removed.
- `benchmark.build_report(*, ground_truth, decisions, ...)` — build a benchmark report
  comparing decisions against ground truth.

### Why deferred
Real study-team datasets replace generated corpora. The generator and benchmark were the
Phase 20 synthetic acceptance harness, not a production path.

### Return condition
A reinstated generator needs the researcher, the planter, the verify scanner, and a benchmark
reporting surface, all behind the study picker.

---

## `phi_engine` CLI (deleted package)

The standalone `phi_engine` package was the pre-Console pipeline. Its CLI surface:

- Console scripts declared in root `pyproject.toml`:
  - `phi-review` -> `phi_engine.cli.phi_review:_main` (interactive PHI review queue CLI with
    `--queue` and `--jurisdiction`).
  - `phi-authority` -> `phi_engine.tools.regulation_fetcher:_main` (regulation authority
    fetcher).
- `python -m phi_engine` -> `phi_engine/__main__.py` -> `phi_engine/cli/main.py`, with
  subcommands `intake`, `organize`, `run`, `review`, `status`, each taking `--study` and
  `--workspace`.

Its one unique capability that was not lost: `phi_engine/security/kanon_gate.py` and
`pycanon_gate.py` provided the k-anonymity and l-diversity gate. That gate was lifted into
the backend as `backend/phi_core/control/reidentification.py` (see ADR and tests), so the capability survives even though the package is gone.

---

## FinalAssuranceGate disposition table (pre-deletion record)

`control/final_assurance.py` was split and the gate itself deleted. All fifteen
`FINAL_ASSURANCE_CONDITIONS` are accounted for below: each survives as a tool inside
`OutputVerifier`, is subsumed by another check, or is intentionally dropped.

| # | Condition | Disposition | Detail |
| --- | --- | --- | --- |
| 1 | `input_inventory_complete` | Subsumed | `control/gates.py::assert_exact_coverage` proves every file and column has exactly one decision; OutputVerifier re-proves it post-execution. |
| 2 | `all_logical_columns_accounted` | Subsumed | Same coverage proof, computed from the verified decision set rather than a boolean input. |
| 3 | `reviewer_preview_pass` | Survives as a gate, restructured | The `review_classification` workflow node: ClassificationReviewer must return `ok` before `code_generate` can run. |
| 4 | `no_unresolved_human_review` | Subsumed | Workflow sequencing: `execute` is unreachable while a `human_review` node is open. |
| 5 | `manifest_current` | Subsumed | `_next_decision_version` CAS counter on the run record invalidates stale decision sets. |
| 6 | `manifest_frozen` | Subsumed | The decision set is version-locked by `control/gates.py` before `review_classification` can pass. |
| 7 | `executor_complete` | Survives, restructured | The `execute` node's `ok` outcome plus `workspace_diff_check` (generated code produced exactly its declared outputs). |
| 8 | `deterministic_verifier_pass` | Survives as OutputVerifier tool | DeterministicVerifier's row-transform checks are folded into OutputVerifier. |
| 9 | `reviewer_final_pass` | Survives as OutputVerifier tool | Reviewer.finalize's section-55 gate is folded into OutputVerifier. |
| 10 | `no_unresolved_privacy_finding` | Subsumed | Hardened Publish Guard byte scan plus the reporting-safety gate, both OutputVerifier tools. |
| 11 | `no_unresolved_security_incident` | Survives as OutputVerifier check | OutputVerifier reads `security_incident_active` from the durable `security_incidents` store before yielding `ok`. |
| 12 | `report_package_complete` | Subsumed | The `package` node structurally cannot ship an incomplete bundle; `ReportingSafetyRefused` is a refusal path. |
| 13 | `reporting_safety_gate_pass` | Survives as OutputVerifier tool | `run_reporting_safety_gate` runs over generated reports before packaging. |
| 14 | `integrity_checks_pass` | Subsumed | `IntegrityService`/`zip_builder` sha256-bind every artifact at the download boundary. |
| 15 | `no_unresolved_audit_finding` | Intentionally dropped | It existed to stop a model's self-reported confidence from promoting itself. The new pipeline has no self-reported-confidence channel at all (the loop-exit gate is computed from reviewer tool outcomes), so the condition has nothing left to gate. Its companion `reasoning.auditor_escalation_reason` was deleted with it. |

`run_integrity_checks` (the producer for condition 14's own hash re-computation) was deleted
with the gate; hash re-verification lives in `IntegrityService` at the boundary.
