# PHI Handling System

**License:** MIT (see `LICENSE`)

This repository holds two codebases sharing one git history from a common fork point: the
`backend`/`frontend` PHI Console service ("PHI Console" below) and the standalone `phi_engine`
pipeline package ("phi_engine pipeline" below). Both are real, both ship from this tree, and
this file documents both.

## PHI Console (backend / frontend)

**Version:** 2.0.0
**Address:** Sir. **Style:** no emojis, no em-dashes, cite authorities.

A study team drops in a ZIP (datasets + at least one of forms / data_dictionary /
mappings), a 15-agent pipeline (12 original agents plus Manager, Operator, and
Reviewer) classifies every column, applies HIPAA §164.514 Safe Harbor
transformations deterministically, and emits an IRB-grade bundle with
attestation, benchmark, and manuscript draft. See `CLAUDE.md`'s "PHI Console"
section for the full agent roster, structure, and API surface -- this file
gives the north star and operational summary; `CLAUDE.md` is the source of
truth for the running implementation.

This service is mid-migration to a new target architecture; see `CLAUDE.md`'s
"Migration status" note. Everything below describes the current, running
implementation.

### Core PHI safety constraint

Datasets (CSV, XLSX, Parquet) expose ONLY column headers to the LLM. Row values
are read exclusively by the deterministic Executor and Publish Guard. One
named exception: Lexicon sends data-dictionary/codebook row text (column
labels and descriptions, not patient dataset rows) to the LLM, after
`scrub_for_prompt` redacts identifiers first. All other files (PDF, DOCX)
feed Instrument/Lexicon extraction, never raw dataset rows.

### Stack

- **Backend:** FastAPI + Motor (MongoDB) + LiteLLM against a direct provider
  key (Claude Sonnet 4.5 default; OpenRouter/OpenAI/Gemini also supported).
- **Frontend:** React operator console (Wizard, SessionDetail live trace,
  Settings, Corpus runner).
- **Corpus:** Deterministic HIPAA Safe Harbor (A-R) + quasi-identifier
  generator, seeded, reproducible; datasets and dictionaries only.

### Layout

```
/app
  backend/
    server.py               FastAPI service on :8001
    phi_core/
      agents/                15-agent pipeline: specialists, experts, reasoning core, manager/operator/reviewer, outward
      intake.py, file_readers.py, anonymizer.py, publish_guard.py, bundle.py, crypto.py, jurisdictions.py
    phi_corpus/              adversarial corpus generator + benchmark report
    tests/                   340+ regression tests
    .env                    MONGO_URL, DB_NAME, ANTHROPIC_API_KEY, APP_ENCRYPTION_KEY, ATTESTATION_SIGNING_KEY
  frontend/
    src/                    React operator console on :3000
  authorities/              Primary legal sources + AUTHORITY_MATRIX.md (single source of truth for IRB)
  data/uploads/<sid>/        intake.zip + unpacked/ (never leaves the pod)
  data/exports/               handled outputs
```

See `CLAUDE.md`'s "Structure" block for the authoritative, current file-level
layout.

### Run

```
sudo supervisorctl restart backend frontend
# backend  :8001
# frontend :3000
```

### Deploy with Docker

```
cp backend/.env.example backend/.env
cp frontend/.env.example frontend/.env
# edit backend/.env: set PHI_ENV=production, API_TOKENS, CORS_ALLOWED_ORIGINS,
# APP_ENCRYPTION_KEY, ATTESTATION_SIGNING_KEY, and one LLM provider key
# (backend/.env.example documents how to generate each key)
export MONGO_ROOT_PASSWORD=<a strong password>
docker compose up --build
```

Open `http://localhost:8001`. The container serves both the API and the
built React console from one process on one port; `backend/.env` supplies
everything except `MONGO_URL`, which compose points at the bundled,
authenticated `mongo` service. See `backend/.env.example` for every
variable and what refuses to boot without it (4.1).

### Workflow

1. Create session, upload the intake ZIP.
2. Specialists (Lexicon, Schema, Instrument) and experts (Statute, Praxis)
   run in parallel: headers, dictionary text, and form fields only, never
   dataset rows.
3. Judge<->Sentinel reasoning loop decides a per-column action (keep, drop,
   cap_age_90, year_only, zip3_truncate, hash, pseudonymize, scrub_text,
   human_review), tagged with 45 CFR 164.514 categories A-R.
4. Human review checkpoint for blocking cases; advisory issues log only.
5. Executor applies approved actions to dataset rows (pure Python, no LLM);
   Operator and Reviewer deterministically verify the output; Publish Guard
   scans every export byte before any download is authorized.
6. Auditor produces the precision/recall/F1 audit-of-record; Scout/Ledger/
   Herald produce the competitor landscape and manuscript draft.

### Benchmark

`phi_corpus/benchmark.py` builds a per-run report (precision/recall/F1 per
HIPAA category, completeness narrative) from the adversarial corpus, attached
to each session and downloadable alongside the publication bundle. See
`CLAUDE.md`'s Auditor description for what the audit-of-record covers.

### Authority

All corpus records, detector rules, and file format handlers trace to `authorities/AUTHORITY_MATRIX.md`. Every span in the API carries an `authority` citation.

## phi_engine pipeline

A standalone PHI intake, classification, scrubbing, review, and publish
pipeline (`phi_engine`). It plugs into any project's own data via
`PHI_WORKSPACE` with zero code changes.

### Purpose and non-certification boundary

This repository provides a fail-closed pipeline that symlink-ingests a
source data tree, classifies headers against pinned USA/HIPAA rules,
synthesizes and applies a per-study scrub configuration, runs a residual
PHI guard before publish, and routes anything uncertain to human review.

This repository does not certify HIPAA or any other regulatory compliance.
It does not substitute for counsel or clinician review. Jurisdiction
coverage is USA/HIPAA only -- pinned regulation rules exist solely for USA
(`phi_engine/security/phi_review.py`); see `authorities/01_hipaa_164_514_full.md`
and `authorities/AUTHORITY_MATRIX.md` for the grounding authority text.

### Installation

```bash
python -m pip install -r requirements.txt
```

Requires Python 3.10+ (`pyproject.toml` `requires-python = ">=3.10"`).

### CLI usage

Every subcommand accepts `--workspace` (sets `PHI_WORKSPACE`). `--study`
(sets `STUDY_NAME`) is REQUIRED for every subcommand except `intake`, where
it is optional. Both env vars are set before `phi_engine.config.config` is
imported, since that module resolves workspace/study paths at import time.

```bash
# Symlink-ingest a source tree (never copies/modifies/deletes source bytes).
# The source root MUST hold the mandatory intake-manifest/v3 component
# package: datasets/ (always required), plus at least one of forms/ or
# dictionary_mapping/ (an alternative group, not both mandatory).
python -m phi_engine intake --study MyStudy --source /path/to/raw/data --workspace /path/to/workspace

# --study is optional for intake ONLY. When omitted, intake resolves the
# study name itself: a local-only, support-content-only AI inference (a
# loopback, attested, digest-pinned local client, never an external
# provider), permitted only by the positive attestation
# --support-confirmed-no-phi; otherwise, or when no name is inferred, intake
# assigns a random study-<8hex> name and reuses/promotes it for the same
# source on a later intake. There is no negative "may contain PHI" flag; with
# neither --study nor --support-confirmed-no-phi, intake performs zero
# naming-content extraction and zero model calls.
python -m phi_engine intake --source /path/to/raw/data --support-confirmed-no-phi --workspace /path/to/workspace

# intake prints ONLY a redacted receipt to stdout and maps its status to the
# process exit code (ready -> 0, review_required -> 8, failed -> 2):
#   {"study": <name>, "status": <status>, "linked": <N>, "review": <N>,
#    "errors": <N>, "manifest": <protected-manifest-path>}
# It never prints entry paths, review/error detail, or raw exception text.

# Accepted formats per component (intake_preflight._COMPONENT_SUFFIXES):
#   datasets/ .csv .xls .xlsx (single-sheet)   forms/ .pdf only
#   dictionary_mapping/ .csv .xlsx
# .json/.jsonl are NOT accepted datasets; an unsupported suffix, invalid
# workbook, multi-sheet dataset xlsx, cross-component hardlink, or source
# symlink lands in an _unclassified review bucket recording only
# filename/reason metadata (never row values). Nested subdirectories and
# duplicate content are preserved as distinct entries.

# Route intake into normalized dataset JSONL, purely by the component each
# entry was assigned at intake (component-authoritative, no path re-guessing).
# Runs automatically on `run` if skipped, but can be run standalone for
# inspection. Refuses to organize an intake that is not `ready`, printing only
# `intake_review_required` (exit 8) or `intake_failed` (exit 2).
python -m phi_engine organize --study MyStudy --workspace /path/to/workspace

# Full pipeline: organize (if stale) -> classify (pinned jurisdiction rules,
# headers-only) -> synthesize the scrub config from the classification ->
# scrub -> residual PHI guard -> publish. Exit codes: 0 clean, 8 partial
# (held forms or a non-empty review queue), 5 guard failure, 1 scrub raised,
# 2 config/input error. No source-of-truth tree is generated automatically;
# standalone SoT remains available only to callers that explicitly maintain
# its legacy annotated-PDF layout.
python -m phi_engine run --study MyStudy --jurisdiction us --workspace /path/to/workspace

# List everything awaiting human review (organizer bucket, held forms, LLM
# uncertain queue, dependency recommendations) and record a decision. To
# correct a held study, inspect the protected manifest, fix the package or
# pass --study, and rerun; decisions apply on the NEXT run.
python -m phi_engine review --study MyStudy --workspace /path/to/workspace list
python -m phi_engine review --study MyStudy --workspace /path/to/workspace decide --header NOTES --decision override --action drop
# --decision is one of keep|drop|override ([--action ...] required only for override;
# action is one of cap|drop|jitter_date|pseudonymize|keep|suppress|generalize).

# Latest run status for a study.
python -m phi_engine status --study MyStudy --workspace /path/to/workspace
```

See `python -m phi_engine --help` for the full argument reference, including
`review dependency-decide`.

### Invariants

- **Source immutability.** Intake links files into the workspace via
  `os.symlink` only, never `shutil.copy*`; source bytes are never opened for
  write and never deleted. The organizer reads normalized dataset content
  only through intake symlinks and writes derived artifacts under
  `<workspace>/organized/<study>/` -- never back into the source tree. It
  also performs a direct metadata-only read of an optional forms manifest
  from the external source root (`organize.py`), separate from the row-data
  path.
- **Fail-closed handling.** Any unsupported suffix, invalid or multi-sheet
  dataset `.xlsx`, unreadable `.xls`, cross-component hardlink, or source
  symlink lands in an `_unclassified` review bucket with a `{path, reason,
  blocking}` record retaining filename, link name, and reason -- never row
  values, never silently dropped or silently parsed as garbage. A missing
  or empty `datasets/`, or a shortfall in the `forms/`/`dictionary_mapping/`
  alternative group, also blocks. Any single blocking review
  item holds the whole study; a missing/malformed/v2 manifest fails with a
  fixed public code (clean v3 cutover, no legacy reader). An unavailable or
  weakened rulebook exits non-zero rather than running with a silently
  downgraded rule set.
- **Publish guard, with a disclosed fallback gap.** The published
  `llm_source/datasets/` tree passes the residual PHI guard gate
  (`phi_guard_gate.run_phi_guard_gate`: Presidio AND a legacy regex
  scanner) before publish when the gate runs cleanly. On a guard exception
  (e.g. Presidio unavailable), the pipeline currently falls back to the
  legacy regex scanner ALONE and can still publish on that scanner's
  result -- a known, disclosed weak-fallback path, not yet closed: the
  legacy-scanner-alone path lacks the Presidio pass's coverage. Header
  classification prompts are headers-only, never a row value, and
  `LLMClient.complete` runs a prompt egress gate (`phi_gate_check`) before
  provider dispatch. A separate read-path wrapper
  (`llm_tool_guard.validate_llm_read_path`) exists but currently has no
  production caller -- it is available, not yet a wired chokepoint.

### Directory layout

```text
PHI-Handling-system/
|-- README.md
|-- pyproject.toml
|-- requirements.txt
|
|-- phi_engine/                 # Runtime PHI pipeline
|   |-- cli/                    # `python -m phi_engine` entry point
|   |-- pipeline/                # intake, organize, run, review, dependencies
|   |-- security/                # classification, scrub, guard gates, patterns
|   |-- config/                  # config.py, config.yaml, per-study _defaults/
|   |-- audit/                   # audit-zone and snapshot-root read barriers
|   |-- sot/                     # source-of-truth study intake helpers
|   `-- tools/                   # regulation_fetcher (authority-document lookup)
|
|-- harness/                     # spec_check, stress/gateway fixture builders,
|                                 # privacy-gateway research validator
|-- authorities/                 # Primary legal source mapping (HIPAA 164.514)
|-- docs/                        # Threat model, operator runbook, migration guide, decision records
|-- research/                    # Local exploratory notes (gitignored)
`-- tests/
```

See `docs/RUNBOOK.md` for operator procedures and `docs/MIGRATION.md` for
control-plane migration steps. `python backend/scripts/export_openapi.py`
regenerates the API reference at `docs/API.md`, which is not committed.

### Configuration

- `--workspace` / `PHI_WORKSPACE`: workspace root. Relocates every
  workspace-relative path (`intake/`, `organized/`, `data/raw/<study>/`,
  `output/<study>/`, per-study `config/<study>/`). Running
  `python -m phi_engine ...` from a different `cwd` with a different
  `PHI_WORKSPACE`, against a foreign source tree, produces the same
  behavior with no repo-root dependence for data paths.
- `--study` / `STUDY_NAME`: study name (plain folder name), scoping intake,
  organized output, and per-study configuration.

### Verification

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider \
  tests/test_phi_engine_integration.py \
  tests/test_stress_standalone.py \
  tests/test_phase3_run_pipeline_integration.py \
  tests/test_phase3_run_review_integration.py \
  tests/test_pipeline_lock.py \
  tests/test_phi_llm_safety.py \
  tests/test_llm_egress_gate.py \
  tests/test_validate_privacy_research.py

PYTHONDONTWRITEBYTECODE=1 python -m harness.validate_privacy_research

PYTHONDONTWRITEBYTECODE=1 python -m phi_engine --help
PYTHONDONTWRITEBYTECODE=1 python -c "from phi_engine.security.presidio_gate import analyze_text; assert analyze_text('SSN 123-45-6789')"
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
```

### Security disclosure

Do not file suspected PHI or security leakage as a public GitHub issue. Use
a private maintainer contact configured by the project owner; if none is
configured, stop distribution and notify the repository owner out of band.
See `SECURITY.md`.

### License

MIT License. See `LICENSE` for full text. The authority documents under
`authorities/` reference statutory text, which is in the public domain.
