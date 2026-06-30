---
name: report-ai-study-pipeline
description: Use when a user wants the full RePORT-AI study preparation workflow as one portable LLM plugin. The orchestrator IS the pipeline — a 10-phase state machine that drives header/dictionary extraction, deduplication, Source Truth, PHI classification + scrubbing, audit verification, the PHI guard gate, promotion, the cleanup verifier, and the immutable snapshot, end to end.
---

# RePORT-AI Study Pipeline (Orchestrator)

> **Global Rule (GR-1):** No LLM — including Claude — may read dataset row values at any time, under any circumstance. Column headers (row 1) are the only permitted LLM dataset input. Failure reports carry pattern + column + count only, never a value.

## Core Rule

This is the platform-neutral **orchestrator** for the bundled RePORT-AI skills.
The plugin IS the pipeline: a single 10-phase state machine
(`skills/report-ai-study-pipeline/scripts/run.py`) holds the per-study pipeline
lock for the whole run and drives every phase to completion. Operators launch it
with:

```bash
make study STUDY=<name>
```

`make study` exports `STUDY_NAME` and delegates to the orchestrator. Do not run
the host publish engine (`scripts.pipeline.host_pipeline`) or the publish
supervisor directly for a normal build — the orchestrator owns lock acquisition,
phase ordering, the redundant-run short-circuit, and the snapshot commit.

**Do not read raw dataset row values into the agent context.** Dataset row 2+
values may only be handled inside trusted repo code paths that scrub, merge,
clean, audit, or publish data and expose metadata-only reports. Skills read row-1
column NAMES and metadata only.

## What This Skill Does

Drives the entire raw → `llm_source/` + `audit/` study build as one 10-phase
state machine (`scripts/run.py`). It acquires the per-study pipeline lock once for
the whole run, records a durable value-free `run_state.json` (schema 2, with the
per-form state map), and invokes the child pipeline skills as file-path
subprocesses under a validated lock baton — dictionary extraction, raw-file
deduplication, shared header extraction, Source Truth, the bundled
classify→extract→scrub→guard-gate→promote publish leg, the audit verifier, the
cleanup verifier, and the immutable snapshot commit. It owns the redundant-run
short-circuit (input-fingerprint match → repoint `current.json` at the existing
clean snapshot), crash-recovery readback, and the maintainer `--resume-held`
loop. The orchestrator refuses to run if `STUDY_NAME` resolves to a different
study than `--study`.

## CLI

Normal build (the only command operators need):

```bash
make study STUDY=<name>            # build/publish a study via the orchestrator
make study STUDY=<name> FORCE=1    # ignore the redundant-run short-circuit
make study STUDY=<name> STRICT=1   # abort on the first un-scrubbable row
```

`make study` exports `STUDY_NAME` and delegates to the orchestrator. The
underlying subprocess entry (used by `make` and by the maintainer resume loop) is:

```bash
uv run --all-groups python \
  plugins/report-ai-study-pipeline/skills/report-ai-study-pipeline/scripts/run.py \
  --study <name> [--resume-held] [--skip-header-extraction]
```

Do not run the host publish engine (`scripts.pipeline.host_pipeline`) or the
publish supervisor directly for a normal build — the orchestrator owns lock
acquisition, phase ordering, the redundant-run short-circuit, and the snapshot
commit.

## The 10 Phases

The conceptual 10 runtime phases map onto the supervisory steps `run.py` drives.
The contiguous publish phases 2–7 are executed by the proven
`$dataset-to-llm-source` publish supervisor in one locked subprocess (which in
turn invokes the host publish engine `scripts.pipeline.host_pipeline`), rather
than being re-decomposed into separate subprocesses.

### Runtime vs conceptual phase labels

`run_state.json` records **runtime** phase names (what `run.py` emits). The table
above uses **conceptual** phase numbers (0–10, including `2b` and `3b`) for
operator docs and `plugin.yaml`. Both refer to the same pipeline.

| Runtime label (`run_state.json`) | Conceptual phase | What runs |
|---|---|---|
| `P0:preflight` | 0 | Config validation, rulebook drift, input-fingerprint redundant-run check, lock |
| `P1c:dictionary-extract` | 1 | Dictionary extraction (column NAMES only) |
| `P2:dataset-deduplication` | 2 | Per-form raw-file deduplication |
| `P1:header-extraction` | 2b | Shared header extraction on deduplicated file set |
| `P1b:sot-lean-generate` | 3 (SoT leg) | Source Truth lean outputs (joined views → `llm_source/SoT/`) |
| `P2:publish` | 3–7 (bundled) | `dataset-to-llm-source run`: classify → extract → scrub → inline verifier → PHI guard gate (Presidio + residual scan; pyCANON deferred) → promote → cleanup/destroy |
| `P8:cleanup-verifier` | 8 | Cleanup verifier over published tree + cleanup ledgers |
| `P9:verify` | 9 | Idempotent 17-assertion re-verify |
| `P10:finalize` | 10 | Input fingerprint, snapshot commit, `current.json`, lock release |

Conceptual phase 3b (cross-form PHI-classification barrier) executes inside
`P2:publish` before per-form scrub; it does not get a separate runtime record.

| Phase | Action | Skill(s) |
|---|---|---|
| 0 | Config validation ∥ rulebook fetch/drift · input-fingerprint redundant-run check · dir pre-creation · acquire lock | (shared modules) |
| 1 | Dictionary extraction (column NAMES only; ∥ rulebook in P0) | `$dictionary-to-llm-source` |
| 2 | Per-form deduplication (scrub-first; provably-safe merges only; internal row-1 header reads) | `$dataset-deduplication` |
| 2b | Shared header extraction on deduplicated file set (column NAMES only; gates classification) | `$header-extraction` |
| 3 | Source Truth ∥ PHI classification ∥ full data extraction | `$sot-lean-generator` ∥ `$phi-classification` ∥ `$dataset-to-llm-source` |
| 3b | Cross-form PHI-classification consistency barrier | `$phi-classification` |
| 4 | Per-form PHI scrub (fail-closed) | `$phi-scrubbing` |
| 5 | 17-assertion audit verification | `$audit-verification` |
| 6 | PHI guard gate (Presidio + residual scan, OR-combined; pyCANON deferred at publish) → atomic promotion | `$dataset-to-llm-source` |
| 7 | Cleanup propagation ∥ staging destruction + attestation ∥ key zero | `$dataset-to-llm-source` + orchestrator |
| 8 | Cleanup verifier over published tree + cleanup ledgers | orchestrator module |
| 9 | Idempotent 17-assertion re-verify | `$audit-verification` |
| 10 | Snapshot → current pointer → status.json → lock release | orchestrator module |

`$phi-rulebook` is a shared-module skill consumed in phase 0 and by
`$phi-classification` (not a DAG node). `$study-setup` is interactive scaffolding
and is **not** an orchestrator phase. The legacy `excel-duplicate-handler` skill
was retired (Note 18); its lossless workbook-merge engine is folded into
`$dataset-deduplication` as a maintainer-only resolution arm (never auto-invoked).

## Execution Unit

The atomic unit after duplicate preflight is a **raw-file set**: one canonical
form/work unit containing the associated raw dataset workbook or CSV, matching
printed PDF when Source Truth is required, manifest/privacy context, duplicate
variants already resolved or held for review, and the per-set output/audit
status.

Valid set statuses:

- `ready`
- `held_duplicate_review`
- `held_sot_review`
- `held_publish_review`
- `complete`

A held raw-file set blocks only itself, not unrelated ready sets.

## Maintainer Resume Loop

After a partial run holds forms for human review, a maintainer resolves the
inputs and re-runs:

```bash
make study STUDY=<name>            # first build (commits a snapshot if clean)
# orchestrator passthrough for the human-review resume cycle:
uv run --all-groups python plugins/report-ai-study-pipeline/skills/report-ai-study-pipeline/scripts/run.py \
  --study <name> --resume-held
```

`--resume-held` re-publishes the **full surviving set** (approved ∪ held), never
the held subset alone (promotion is a whole-leg atomic replace). A clean pass
commits an immutable snapshot under `output/<STUDY>/snapshots/<id>/` and points
`current.json` at it. The retry loop is a CLI/maintainer workflow only and is
never triggered from the Load Study UI.

## Result Contract

The orchestrator is the marker **consumer**, not a producer: it invokes each child
skill via `scripts/utils/skill_protocol.py:invoke_skill` and reads their
`RPLN_SKILL_RESULT:` markers, recording per-phase status / exit code / detail into
a durable, value-free `run_state.json` (schema 2: phase records plus the per-form
`forms` map and per-form fingerprints). The run-level evidence is the file set —
`run_state.json`, `run_recovery.json`, `phi_handling_approval.json`,
`verifier_report.json`, `status.json` (with `held_forms`), the lineage manifest,
and the committed snapshot. Every field is a phase name, status, exit code, form
NAME, fingerprint hash, or count — never a dataset row value.

## Exit Codes

The orchestrator reuses the publish supervisor's exit-code contract and propagates
a failing child code:

| Code | Meaning |
|---|---|
| `0` | Full clean run — all forms published, verifier passed, snapshot committed (`EXIT_OK`); also the redundant-run short-circuit (`current.json` repointed at the existing clean snapshot). |
| `1` | A child skill subprocess failed (dictionary / dedup / header / SoT / verifier leg). |
| `2` | Preflight/config failure — e.g. `STUDY_NAME` mismatch, invalid config, or lock unavailable. |
| `5` | Verifier-failure family — an assertion failed (the cleanup/scrub interrupt token is left in place for recovery). |
| `6` | Needs advice — a phase paused for human input (`EXIT_NEEDS_ADVICE`). |
| `8` | Partial review — approved forms published, held forms carried for human review (`EXIT_PARTIAL_REVIEW`); resolve and re-run with `--resume-held`. |

## Per-Form State Machine & Crash Recovery (Note 16)

`run_state.json` (schema 2) records a value-free per-form `forms` map — every
form in exactly one of `not_started → running → complete | held_for_review |
re_running | failed_pipeline_level`, written on every transition. Each form
carries a per-form input fingerprint. On restart, preflight reads any prior
`run_state.json` left `in_progress` (the per-study lock proves it is dead),
classifies its forms (running→re-run, held→carried, complete→re-validated by
fingerprint), writes a value-free `output/<STUDY>/runs/<RUN_ID>/run_recovery.json`,
and marks the crashed run recovered. This is detection + observability: the
re-run still re-publishes the full surviving set (whole-leg atomic promotion).
Per-form fingerprints do **not** drive partial promotion — an accepted deviation
(always re-scrub from raw, fail-closed).

## When To Use Only One Child Skill

If the user asks only about duplicate files, use `$dataset-deduplication`
(orchestrator phase 2). Its maintainer-only merge arm (`merge_excel_duplicates.py`,
folded in from the retired `excel-duplicate-handler`) resolves complementary
duplicates by lossless workbook merge and is never invoked by the publish path.
If the user asks only about PDF/header Source Truth policy YAML, use
`$sot-lean-generator`. If the user asks only to run or verify PHI-safe dataset
publishing, use `$dataset-to-llm-source`. Use this orchestrator when the request
spans more than one phase, when the phase is unclear, or when the user asks for
the full repo/plugin study build.

## Portability

This plugin is not Codex-only. Any LLM platform can use it by reading
`plugin.yaml`, this orchestrator, and the child `SKILL.md` files. Codex can also
load `.codex-plugin/plugin.json`, but that file is an adapter, not the source of
truth. Bundled skill agent metadata uses `agents/llm.yaml` as the
platform-neutral filename; adapters may map it into their native discovery
surface, but the plugin should not depend on vendor-specific filenames.

When porting to another repo, verify the host repo provides equivalent CLI paths
before running the workflow. If a required path is missing, report the missing
contract and do not invent a substitute that weakens the PHI boundary.

## What This Skill Does NOT Do

- **Never reads raw dataset row values into the agent context** — child skills read row-1 column NAMES and metadata only; row 2+ values are touched only inside trusted publish/scrub/cleanup code paths that emit metadata-only reports (GR-1).
- **Does not partial-promote on recovery** — per-form fingerprints drive readback classification + observability + the redundant-run check, but the publish leg is a whole-leg atomic `rename()` that always re-scrubs from raw; `--resume-held` re-publishes the full surviving set (accepted deviation D4).
- **Does not run the publish engine directly for a normal build** — `make study` is the entry point; the orchestrator owns the lock, phase ordering, the redundant-run short-circuit, and the snapshot commit.
- **Does not let a stale lock baton disable the lock** — the handed `REPORTAL_PIPELINE_LOCK_PARENT_PID` baton is validated against `os.getppid()`, so a leftover env var cannot silently skip lock acquisition.
- **Does not run `$study-setup` as a phase, and never auto-runs the merge resolution arm** — `$study-setup` is interactive scaffolding; the `$dataset-deduplication` maintainer merge arm (`merge_excel_duplicates.py`, folded in from the retired `excel-duplicate-handler`, Note 18) is run by a maintainer to resolve a held group, never by the orchestrator.
