---
name: dataset-deduplication
description: Raw-file dataset deduplication (Note 4) — filename normalization, header/row-count tiers, no cell reads in the automated path. Orchestrator phase 2. Also hosts the maintainer-only lossless workbook merge resolution arm (folded in from the retired excel-duplicate-handler, Note 18).
---

# Dataset Deduplication

## Core Rule (GR-1 + Note 4)

**No LLM and no dedup logic may read dataset row values** in the automated
detect arm — column headers (row 1) and row **counts** only. The maintainer
*merge* arm is trusted host code (same class as `phi_scrub.py`) that may read raw
cell values **internally** to build a merged workbook; it operates on raw,
pre-scrub files, writes back only to the raw `datasets/` path (never
`llm_source/`), and emits count/header/provenance-only reports. GR-1 forbids
*LLM* value reads; it does not forbid trusted deterministic code from touching
raw workbooks. Lock/temp files (``~$*.xlsx``) are ignored automatically.

## What This Skill Does

This skill has **two arms** with different roles and PHI postures:

| Arm | When | Reads cell values? | Wired into the DAG? |
|---|---|---|---|
| **Detect** (automated) | Orchestrator phase 2, every run | **No** — row-1 headers + row **counts** only | Yes — `invoke_skill("dataset-deduplication")` |
| **Resolve-by-merge** (maintainer) | Only when a human resolves a held duplicate group | Yes — trusted, deterministic, **pre-scrub, never LLM-exposed** | **No** — run by hand, never auto-invoked |

### Arm 1 — Detect (automated, orchestrator phase 2)

Deduplicates **raw** Excel/CSV files under ``data/raw/<study>/datasets/`` via
``scripts/extraction/raw_file_dedup.py`` **before** the shared header-extraction
store (Note 6) is built. Reads row-1 column headers internally when comparing
duplicate candidates — it does not consume the Note 6 temp store.

1. **Normalize** filenames (strip underscores, numeric suffixes, case).
2. **Group** files sharing the same normalized base (2+ = duplicate candidate set).
3. **Tier 1 — perfect column match:** identical header name/count/order;
   row counts equal → auto-resolve (archive duplicates); row counts differ →
   human review.
4. **Tier 2 — header superset:** one file's columns are a strict superset of
   another's → keep the file with the maximum column count; archive others.
5. **All other cases** → human review (count-only note under
   ``audit/human_review/``).

JSONL-level duplicate merging (``clean_trio_datasets`` pair merge) is **retired**
from the production path (legacy unit tests only).

### Arm 2 — Resolve-by-merge (maintainer-only, NOT on the DAG)

When the detect arm routes a candidate group to human review because two files
are **complementary** (each holds rows or columns the other lacks), a maintainer
may resolve it with a **lossless workbook merge** using
``scripts/merge_excel_duplicates.py`` (folded in from the retired
``excel-duplicate-handler`` skill, Note 18). This arm is **never auto-invoked** —
auto-merging clinical data is forbidden; a complementary merge is a clinical-data
decision that requires human judgment (Note 18 "escalate, don't auto-merge"). It
builds the merge in a temp workbook first, preserves the main workbook as the
base, appends valid branch rows not already present, adds branch-only columns,
snapshots the full original ``datasets/`` folder to ``data/raw/<study>/_dataset/``
(never overwriting an existing snapshot), and writes count/header/provenance-only
reports under the audit folder. Unsafe/ambiguous candidates get a
count/header-only human-review note instead of a merge.

## CLI

Automated detect arm (the orchestrator phase-2 entrypoint):

```bash
uv run --all-groups python \
  plugins/report-ai-study-pipeline/skills/dataset-deduplication/scripts/run.py \
  --study <STUDY> --run-id <RUN_ID>
```

Maintainer merge arm (manual resolution of a held complementary-duplicate group):

```bash
uv run --all-groups python \
  plugins/report-ai-study-pipeline/skills/dataset-deduplication/scripts/merge_excel_duplicates.py \
  --study <STUDY> --dataset <DATASET> \
  --main <path/to/main.xlsx> --branch <path/to/branch.xlsx>
```

Merge reports land under ``output/<study>/audit/datasets/<dataset>/merge_report.md``
+ ``merge_provenance.csv``; human-review cases under
``output/<study>/audit/human_review/<candidate_group>/duplicate_review_report.md``.

## Result Contract

The automated detect arm emits one ``RPLN_SKILL_RESULT:`` marker:
auto-resolved / held / archived / error **counts** only — never a row value. The
maintainer merge arm writes count/header/provenance-only reports to the audit
folder and prints merged/appended **counts** only.

## Portability

Pure host-side Python; no LLM call, no network. The merge arm depends only on
``openpyxl`` + ``scripts/audit/review_paths.py`` (the shared audit-path helper).

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | Dedup completed — including groups routed to held-for-review (a held group is a normal, non-error outcome). |
| `1` | Dedup failed on an I/O or manifest error (the exception type NAME only is reported). |
| `2` | Argparse usage error (e.g. missing `--study`). |

## What This Skill Does NOT Do

- **The automated arm never reads dataset row values** — filename normalization,
  header NAMES, and row **counts** only (GR-1 + Note 4).
- **Does not auto-resolve ambiguous groups** — anything beyond Tier 1 (exact-header)
  / Tier 2 (superset) routes to count-only human review.
- **Does not auto-merge** — the merge arm is maintainer-invoked only; the
  orchestrator never calls it.
- **Does not delete originals destructively** — removed duplicates are archived;
  merges snapshot the full original ``datasets/`` folder first.
- **Never writes to ``llm_source/``** — both arms write only to raw/audit paths.
- **Does not merge at the JSONL row level** — the legacy ``clean_trio_datasets``
  pair merge is retired from the production path.
