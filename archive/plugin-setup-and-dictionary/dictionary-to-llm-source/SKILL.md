---
name: dictionary-to-llm-source
description: Extract the study data dictionary into staging JSONL so a later publish step can promote it into llm_source/dictionary_mapping/. The dictionary leg carries no PHI row values — only codelist/variable metadata, with reference URLs masked at publish time. Use as the dictionary publish leg of the pipeline.
---

# Dictionary to LLM Source

## Core Rule

**GR-1: no LLM — including Claude — may read dataset row values at any time.**
Column headers (row 1) and dictionary metadata are the only permitted inputs.
This skill handles the study **data dictionary**, not dataset row values. The
dictionary is variable/codelist metadata (column definitions, code lists,
help-text) — it carries no PHI row values. Reference URLs in staff-authored
codelist help-text are masked to `<URL_REMOVED>` at publish time, so the leak
gate stays maximally broad.

## What This Skill Does

The dictionary leg covers **both** steps the pipeline needs (Note 1), selected
with `--leg`:

- **`--leg extract`** (default, orchestrator phase P1c): wraps
  `scripts.extraction.load_dictionary.load_study_dictionary`, loading the study's
  data dictionary into staging JSONL (`tmp/<study>/dictionary/`).
- **`--leg publish`**: promotes the staging tree into
  `llm_source/dictionary_mapping/jsonl/` via the shared `scripts/` primitive
  `scripts.pipeline.host_pipeline.publish_dictionary_leg` (the single source of
  truth, also consumed in-lock by the dataset publish supervisor at Step 2).

**Hard ordering rule:** `--leg publish` must run **only after** dataset
cleanup-propagation has pruned dropped/force-dropped columns from staging —
otherwise dropped-column references would leak into
`llm_source/dictionary_mapping/`. In the orchestrated run the publish therefore
happens in-lock inside the dataset publish supervisor (after propagation), via
that shared primitive; `--leg publish` as a standalone phase is a maintainer/test
surface only.

By default missing-data NA tokens are **preserved** (a documented codelist value
such as a defined "not applicable" entry is meaningful metadata); pass
`--no-preserve-na` to drop them instead.

## CLI

```bash
uv run --all-groups python \
  plugins/report-ai-study-pipeline/skills/dictionary-to-llm-source/scripts/run.py \
  --study <STUDY> --run-id <RUN_ID> --run-dir <output/<STUDY>/runs/<RUN_ID>> \
  --leg extract
```

Flags: `--leg {extract,publish}` (default `extract`); `--no-preserve-na` (drop NA
tokens instead of preserving them).

Exit `0` on success (`extract` → staging written; `publish` → promoted, or
skipped when staging is empty); `1` on extraction failure.

## Result Contract

Emits a single `RPLN_SKILL_RESULT:` marker line (the shared skill contract,
`scripts/utils/skill_protocol.py`): value-free — study name and the ok/failed
outcome only, never a dictionary value or a dataset row value.

## Portability

Pure host-side Python; no LLM call, no network. Invoked by the orchestrator as a
file-path subprocess and runnable from any LLM host the same way.

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | Success — `--leg extract` wrote staging, or `--leg publish` promoted the tree (or skipped when staging was empty). |
| `1` | Dictionary extraction failed (`--leg extract`). |
| `2` | Argparse usage error (e.g. missing `--study`, or an invalid `--leg` value). |

## What This Skill Does NOT Do

- **Does not read dataset rows** — handles only the data-dictionary metadata (variable/codelist definitions, help-text), never dataset row values (GR-1).
- **Masks reference URLs at publish** — codelist help-text URLs are rewritten to `<URL_REMOVED>` so the leak gate stays maximally broad; it does not emit raw URLs.
- **Does not publish out of order** — `--leg publish` must run only after dataset cleanup-propagation has pruned dropped columns from staging; otherwise dropped-column references would leak.
- **Does not scrub or classify PHI** — those are separate phases; the dictionary leg carries no PHI to scrub.
