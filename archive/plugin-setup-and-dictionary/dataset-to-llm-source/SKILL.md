---
name: dataset-to-llm-source
description: Run, verify, or audit the PHI-safe RePORT-AI dataset extraction skill that turns raw study workbooks into published llm_source dataset JSONL using scripts/skills/extract_to_llm_source.py. Use when the user asks for the dataset skill, dataset extraction, raw workbook to llm_source, PHI-safe dataset processing, one-form dataset pilots, or extract_to_llm_source operations.
---

# Dataset to LLM Source

> **Global Rule (GR-1):** No LLM — including Claude — may read dataset row values at any time, under any circumstance. Column headers (row 1) are the only permitted LLM dataset input. Failure reports carry pattern + column + count only, never a value.

## Core Rule

Do not read raw or staged dataset values into the agent context.

Allowed inputs for agent reasoning:

- File names and directory shape under `data/raw/{STUDY}/`.
- `_forms_manifest.yaml` and `_study_privacy.yaml`.
- Row-1 headers only when the CLI approval path reads them.
- Code, docs, tests, status files, verifier reports, ledgers, and approval artifacts that contain headers/actions only.
- Published `output/{STUDY}/llm_source/` artifacts only after the run has passed the verifier.

Real row values may be opened only inside the trusted extraction, scrub, cleanup, and publish pipeline driven by `scripts/skills/extract_to_llm_source.py`. Do not bypass the CLI by manually opening raw workbooks, staged JSONL, quarantine files, or audit payloads that may contain values.

Use `$sot-lean-generator` instead when the task is Source Truth YAML creation from printed PDFs plus dataset row-1 headers.

## What This Skill Does

The durable entry point is the cross-LLM CLI:

```bash
uv run --all-groups python scripts/skills/extract_to_llm_source.py
```

It drives one study through:

```text
data/raw/{STUDY}/datasets/*.{xlsx,csv}
  -> tmp/{STUDY}/datasets/*.jsonl
  -> scripts.security.phi_scrub.run_scrub
  -> dataset cleanup and cleanup propagation
  -> output/{STUDY}/llm_source/dataset_schema/files/*.jsonl
  -> verifier report and staging destruction attestation
```

For a full study build this skill runs under the `report-ai-study-pipeline` orchestrator (`make study STUDY=<name>`), which holds the pipeline lock and drives every phase. This CLI is the publish supervisor the orchestrator invokes; prefer it (or `make study`) over invoking the host publish engine (`scripts.pipeline.host_pipeline`) directly, because it includes the manifest gate, privacy approval, pipeline lock, verifier, and destruction attestation.

## CLI

Two surfaces drive the same supervisor:

- **Durable cross-LLM CLI** (`status` / `run` / `verify`) — the operator entry
  point used directly and documented under **Preflight**, **Run**, and **Verify**
  below:

  ```bash
  uv run --all-groups python scripts/skills/extract_to_llm_source.py run \
    --study Indo-VAP
  ```

- **Orchestrator subprocess entry** (`scripts/run.py`) — forwards `argv` verbatim
  to the CLI above and adds the value-free SkillResult marker the orchestrator
  reads; this is how the contiguous publish phases 2–7 run in one locked
  subprocess under the lock baton:

  ```bash
  uv run --all-groups python \
    plugins/report-ai-study-pipeline/skills/dataset-to-llm-source/scripts/run.py \
    run --study Indo-VAP
  ```

Both expose the same `run`/`verify`/`status` subcommands, the same flags
(`--form`, `--run`, `--max-workers`), and the same exit-code contract (below).

## PHI guard gate (pre-promotion)

Before any scrubbed file is promoted into `llm_source/`, the supervisor runs the
**OR-combined PHI guard gate** (Note 5) — it fails closed if *either* layer finds
PHI: **Presidio** (model-free PatternRecognizers) **and** `scan_tree_for_phi`
(the shared residual-pattern scanner). Publish-time pyCANON k-anonymity is
**deferred** (session notes 2026-06). A gate failure writes a value-free report
to the per-form human-review queue (`presidio_failure.md` — pattern + column +
counts only) and blocks promotion. Post-consolidation, this supervisor (not a
standalone CLI) is the single owner of the classify → extract → scrub →
guard-gate → promote → verify sequence.

## Key Boundary

Do not read, print, hash, stat, permission-check, or existence-check PHI HMAC keys or encryption keys from an agent workflow. The key is operator-managed secret material outside the repo. The only code path allowed to load it is the trusted PHI scrubber at the point where it rewrites staged values.

If a run fails because a key is missing or invalid, report the CLI failure stage and stop. Do not inspect the key file yourself.

## Preflight

1. Start at the repo root.
2. Check `git status --short` and preserve unrelated user changes.
3. Print the contract before a first run or when the scope is unclear:

```bash
uv run --all-groups python scripts/skills/extract_to_llm_source.py status
```

4. Confirm required inputs exist without opening dataset values:
   - `data/raw/{STUDY}/_forms_manifest.yaml`
   - `data/raw/{STUDY}/_study_privacy.yaml`
   - `data/raw/{STUDY}/datasets/`
   - `config/_defaults/phi_scrub.yaml` (per-study override: `config/{STUDY}/phi_scrub.yaml`)

Do not set `REPORTALIN_ALLOW_DISABLED_SCRUB`. The CLI fails closed when that variable is present.

## Run

Run all manifest-approved forms:

```bash
uv run --all-groups python scripts/skills/extract_to_llm_source.py run \
  --study Indo-VAP
```

Run one dataset pilot:

```bash
uv run --all-groups python scripts/skills/extract_to_llm_source.py run \
  --study Indo-VAP --form 6_HIV
```

Limit header-review parallelism when needed:

```bash
uv run --all-groups python scripts/skills/extract_to_llm_source.py run \
  --study Indo-VAP --max-workers 2
```

The `--form` value may be a manifest-declared filename such as `6_HIV.xlsx` or a stem such as `6_HIV`. Repeat `--form` for a small explicit set.

## Verify

Always verify after a run before claiming the dataset publish is complete.

Verify the latest successful or partial-safe run:

```bash
uv run --all-groups python scripts/skills/extract_to_llm_source.py verify \
  --study Indo-VAP
```

Verify a specific run:

```bash
uv run --all-groups python scripts/skills/extract_to_llm_source.py verify \
  --study Indo-VAP --run RUN_ID
```

The verifier writes `output/{STUDY}/runs/{RUN_ID}/verifier_report.json` and updates `status.json` with `verifier_passed: true` on full pass.

## Result Contract

The orchestrator subprocess entry (`scripts/run.py`) emits one
`RPLN_SKILL_RESULT:` marker line (the shared skill contract,
`scripts/utils/skill_protocol.py`): the subcommand (`run`/`verify`/`status`), the
study name, the ok/failed outcome, and the exit code only — never a dataset row
value, a quarantine value, or an audit payload. The machine-readable run evidence
lives in files (`status.json`, `verifier_report.json`,
`destruction_attestation.json`); the exit code (below) is the publish/verify
signal. Gate-failure detail is written value-free to the per-form human-review
queue (`presidio_failure.md` — pattern + column + counts only).

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | Success. Report the run id, verifier status, published path, and attestation path. |
| `2` | Manifest mismatch. Compare manifest entries to dataset filenames only; do not open dataset values. |
| `3` | Ledger hash or no-LLM sentinel failed. Stop and report the failing assertion. |
| `4` | Quarantine is non-empty. Stop and preserve artifacts for operator review. |
| `5` | Verifier assertion failed. Use the verifier report to identify the failing invariant. |
| `6` | Needs advice. Stop and report the exact pause reason. |
| `7` | Destruction incomplete. Do not claim operational untraceability. |
| `8` | Partial review. Approved forms may be published; held forms need human review. Report names/status only, not raw values. |

Do not rerun with weaker privacy controls to force success.

## Portability

The supervisor and the orchestrator subprocess entry are pure host-side Python; no
LLM call, no network. Invoked by the orchestrator as a file-path subprocess under
the lock baton, and runnable directly from any LLM host the same way (read this
`SKILL.md`; `agents/llm.yaml` carries the adapter metadata). The trusted scrub /
guard-gate / promote sequence is the only code allowed to read row values, and it
does so only inside the locked publish leg.

## Evidence to Report

For a completed run, report these paths when present:

- `output/{STUDY}/runs/{RUN_ID}/status.json`
- `output/{STUDY}/runs/{RUN_ID}/phi_handling_approval.json`
- `output/{STUDY}/runs/{RUN_ID}/verifier_report.json`
- `output/{STUDY}/runs/{RUN_ID}/destruction_attestation.json`
- `output/{STUDY}/llm_source/dataset_schema/files/`
- `output/{STUDY}/audit/datasets/{DATASET}/phi_handling_ledger.as_written.json`
- `output/{STUDY}/audit/datasets/{DATASET}/dataset_cleanup_ledger.as_written.json`
- `output/{STUDY}/audit/dataset_cleanup_report.json`

If a file may contain dataset values, do not paste its contents into chat. Summarize pass/fail status, counts, filenames, hashes, and assertion names instead.

## Skill Maintenance

When editing this skill or the dataset CLI, keep these sources aligned:

- `scripts/skills/extract_to_llm_source.py`
- `docs/sphinx/developer_guide/extract_to_llm_source.rst`
- `docs/sphinx/developer_guide/data_extraction_datasets.rst`
- `docs/sphinx/developer_guide/architecture.rst`
- `tests/skills/test_extract_to_llm_source_cli.py`
- `tests/skills/test_extract_to_llm_source_verify.py`

Focused validation after skill-only edits:

```bash
uv run --all-groups python -m pytest \
  tests/skills/test_dataset_to_llm_source_skill.py \
  tests/skills/test_extract_to_llm_source_cli.py \
  tests/skills/test_extract_to_llm_source_verify.py -q
```

## What This Skill Does NOT Do

- **Never reads raw or staged dataset values into the agent context** — only file names, manifests, row-1 headers (on the approval path), counts, ledgers, and verifier/status reports; real row values are touched only inside the trusted scrub/cleanup/publish pipeline (GR-1).
- **Does not promote PHI-bearing output** — the OR-combined PHI guard gate (Presidio + `scan_tree_for_phi`) fails closed before promotion if either layer finds PHI; an un-scrubbable form is held, never published.
- **Does not inspect key material** — it does not read, print, hash, or stat the PHI HMAC / encryption keys from an agent workflow; only the trusted scrubber loads the key, at the point it rewrites staged values.
- **Does not weaken privacy to force success** — `REPORTALIN_ALLOW_DISABLED_SCRUB` is refused (the CLI fails closed when it is present), and a failing run is reported by stage, not retried with weaker controls.
