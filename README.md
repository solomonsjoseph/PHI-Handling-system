# PHI Handling System

A standalone PHI intake, classification, scrubbing, review, and publish
pipeline (`phi_engine`). It plugs into any project's own data via
`PHI_WORKSPACE` with zero code changes.

**License:** MIT (see `LICENSE`)

## Purpose and non-certification boundary

This repository provides a fail-closed pipeline that symlink-ingests a
source data tree, classifies headers against pinned USA/HIPAA rules,
synthesizes and applies a per-study scrub configuration, runs a residual
PHI guard before publish, and routes anything uncertain to human review.

This repository does not certify HIPAA or any other regulatory compliance.
It does not substitute for counsel or clinician review. Jurisdiction
coverage is USA/HIPAA only -- pinned regulation rules exist solely for USA
(`phi_engine/security/phi_review.py`); see `authorities/01_hipaa_164_514_full.md`
and `authorities/AUTHORITY_MATRIX.md` for the grounding authority text.

## Installation

```bash
python -m pip install -r requirements.txt
```

Requires Python 3.10+ (`pyproject.toml` `requires-python = ">=3.10"`).

## CLI usage

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

## Invariants

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
  result -- a known, disclosed weak-fallback path, not yet closed (see
  `docs/PRIVACY_GATEWAY_RECOMMENDATION.md` §"Weak points wrapped or
  replaced"). Header classification prompts are headers-only, never a row
  value, and `LLMClient.complete` runs a prompt egress gate
  (`phi_gate_check`) before provider dispatch. A separate read-path wrapper
  (`llm_tool_guard.validate_llm_read_path`) exists but currently has no
  production caller -- it is available, not yet a wired chokepoint.

## Directory layout

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
|-- docs/                        # Spec, threat model, and evidence/research reports
|-- research/                    # Local exploratory notes (gitignored)
`-- tests/
```

## Configuration

- `--workspace` / `PHI_WORKSPACE`: workspace root. Relocates every
  workspace-relative path (`intake/`, `organized/`, `data/raw/<study>/`,
  `output/<study>/`, per-study `config/<study>/`). Running
  `python -m phi_engine ...` from a different `cwd` with a different
  `PHI_WORKSPACE`, against a foreign source tree, produces the same
  behavior with no repo-root dependence for data paths.
- `--study` / `STUDY_NAME`: study name (plain folder name), scoping intake,
  organized output, and per-study configuration.

See `docs/STANDALONE_SPEC.md` for the full portability/security checklist.

## Verification

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

## Security disclosure

Do not file suspected PHI or security leakage as a public GitHub issue. Use
a private maintainer contact configured by the project owner; if none is
configured, stop distribution and notify the repository owner out of band.
See `SECURITY.md`.

## License

MIT License. See `LICENSE` for full text. The authority documents under
`authorities/` reference statutory text, which is in the public domain.
