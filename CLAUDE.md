# CLAUDE.md — Agent Handoff Document

This file tells an agent everything it needs to know to work on this
repository. Read it in full before touching any other file.

## Scope

USA/HIPAA only. Pinned de-identification rules exist solely for USA
(`phi_engine/security/phi_review.py` `_PINNED_RULE_SPECS`), grounded in
`authorities/01_hipaa_164_514_full.md` (45 CFR 164.514 primary text) and
`authorities/AUTHORITY_MATRIX.md` (identifier-category mapping). Extending
to another jurisdiction needs its own pinned rule-spec entries grounded in
that jurisdiction's own authority document set.

This repository does not certify HIPAA or any other regulatory compliance.

## What this repository is

A standalone PHI intake, classification, scrubbing, review, and publish
pipeline (`phi_engine`), runnable via `python -m phi_engine`. It is a
portable package: point it at any project's own data with `PHI_WORKSPACE`
and zero code changes.

## Invariants an agent must never break

- **Source immutability.** `phi_engine/pipeline/intake.py::intake_add`
  links files into `<workspace>/intake/<study>/` via `os.symlink` only --
  never `shutil.copy*`. Intake never opens a source file for write and
  never deletes a source file.
- **Symlink-only intake.** Every entry under `<workspace>/intake/<study>/`
  is either a symlink or the `intake_manifest.json` bookkeeping file.
- **Never move/modify/copy source data.** The organizer
  (`phi_engine/pipeline/organize.py::organize`) reads normalized dataset
  content only through intake symlinks and writes derived artifacts under
  `<workspace>/organized/<study>/` -- never back into the source tree
  passed to `intake_add`. It also performs a direct metadata-only read of
  an optional forms manifest from the external source root, separate from
  the row-data path.
- **Fail-closed review routing.** Any raw variable/dataset that cannot be
  normalized (unrecognized suffix, broken intake symlink, unreadable
  `.xls`, unparseable `.pdf`) lands in the review bucket with a record
  retaining filename, link name, reason, and diagnostic metadata -- never
  row values, never silently dropped, never silently parsed as garbage.
- **Residual guard before publish, with a disclosed fallback gap.** The
  published `llm_source/datasets/` tree passes
  `phi_engine/security/phi_guard_gate.py::run_phi_guard_gate` (Presidio AND
  a legacy regex scanner) when the gate runs cleanly. On a guard exception
  (`phi_engine/pipeline/run.py`'s residual-guard `except Exception:`
  block), the pipeline currently falls back to the legacy regex scanner
  ALONE and can still publish on that scanner's result -- a known,
  disclosed weak-fallback path, not yet closed (see
  `docs/PRIVACY_GATEWAY_RECOMMENDATION.md` §"Weak points wrapped or
  replaced"). Do not describe this as an unconditional two-scanner
  guarantee.
- **LLM egress controls; read-path wrapper not yet a production
  chokepoint.** Header classification prompts (`llm_detector.py`) are
  headers-only -- never a row value. `LLMClient.complete` runs
  `phi_gate_check` (`phi_engine/security/phi_gate.py`) on the outbound
  prompt before provider dispatch and raises `PHIEgressBlockedError` on a
  match. `guard_llm_output` screens serialized tool output through the PHI
  gate and IS called from `llm_detector.py` and
  `phi_engine/tools/regulation_fetcher.py` provider-response paths; the
  generic `llm_safe_tool` decorator has ZERO production `@llm_safe_tool`
  usages, so arbitrary tool returns are not routed through it.
  `llm_tool_guard.py`'s `validate_llm_read_path` is defined and exported
  but currently has NO production caller anywhere in `phi_engine` --
  available, not yet wired as an enforced read-side chokepoint. Audit
  stores beyond `phi_gate`/log-hygiene blocking-path logs -- the
  organizer review-bucket record (`organize.py`, explicitly
  `chmod(0o600)`) and `phi_scrub_report.json` -- retain filenames, link
  names, header/field names, reasons, counts, and diagnostic metadata:
  sensitive metadata, not row values, but not "category tags only"
  either. `llm_uncertain.jsonl` (`llm_detector.py::_write_review_queue`)
  retains the same class of metadata (including the raw column header)
  but is written via plain `Path.open("a")` with NO explicit chmod --
  do not claim it is 0600-guaranteed; an empirical tempfile check
  produced mode 0644 / parent 0755.
- **Human review feedback loop.** A `keep`/`drop`/`override <action>`
  decision (`python -m phi_engine review --study S decide ...`) is
  persisted and applied on the NEXT `run` -- not merely logged.

## Authority grounding

Every classification/action claim in code comments or documentation should
trace to `authorities/01_hipaa_164_514_full.md` or
`authorities/AUTHORITY_MATRIX.md`. Do not add jurisdiction, identifier
category, or benchmark claims without a grounding authority citation or a
`file:line` reference into surviving `phi_engine`/`harness` code.

## Runtime paths

- `phi_engine/cli/main.py` -- `python -m phi_engine {intake,organize,run,review,status}`
  entry point; module docstring is the source of truth for exact CLI syntax.
- `phi_engine/pipeline/{intake,organize,run,review,dependencies}.py` -- pipeline stages.
- `phi_engine/security/{phi_review,phi_scrub,phi_guard_gate,phi_gate,llm_tool_guard,presidio_gate,kanon_gate}.py` -- classification, scrub, and guard controls.
- `phi_engine/config/config.py`, `phi_engine/config/config.yaml`, `phi_engine/config/_defaults/` -- static configuration; per-study config is synthesized fresh each run (`phi_engine/pipeline/synthesize_config.py`) and is not source of truth.
- `harness/make_stress_fixtures.py`, `harness/make_privacy_gateway_fixtures.py` -- deterministic fixture builders used by the stress and privacy-gateway test suites.
- `harness/spec_check.py` -- post-run invariant checker (`intake_symlink_invariant`, `llm_boundary_canary`, `source_immutability`).
- `harness/validate_privacy_research.py` -- validates `research/privacy_gateway/{evidence_ledger,candidate_registry,dispositions,search_log}` against `docs/PRIVACY_GATEWAY_RESEARCH.md`.

## Verification commands

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

Before claiming any change complete: run the relevant command(s) above and
report the actual observed output. Never claim a test passed without
running it.

## Working conventions

- No em-dashes, no emojis in generated files (repository convention).
- Never commit real PHI. Never commit AWS/API or LLM provider credentials.
- Every classification/action rule change must cite its authority source.
- Commit messages follow Conventional Commits.
