---
name: raw-data-intake
description: Skill 0 (setup, NOT a publish phase) — sort an unorganized study delivery (flat dump and/or zips) into the canonical data/raw/<study>/ four-bucket layout (annotated_pdfs, datasets, data_dictionary, _unclassified) and draft config/<study>/_forms_manifest.yaml. Classification is filename + extension ONLY; no workbook is ever opened. Idempotent: a no-op on an already-organized tree unless forced.
---

# Raw Data Intake (Skill 0)

> **Global Rule (GR-1):** No LLM may read dataset row values at any time. This skill classifies on filenames + extensions only — it never opens a workbook. The review note carries file name + bucket-guess + reason code only, never file contents.

## Core Rule

Intake is **not** a publish phase and never touches the per-study pipeline lock.
It runs once, *before* `make study`, turning an unorganized delivery into the
inputs the 10-phase orchestrator already assumes exist. It is **non-destructive**
(copies, never moves the source) and **idempotent** (a no-op on an
already-organized tree unless `FORCE=1`).

## What This Skill Does

1. **Stage** — copy `SRC` (a dir or a `.zip`) into a temp working dir; extract
   any zips. The source is never modified. A directory `SRC` is walked
   recursively, so files may sit loose in `SRC` or in subfolders. Pipeline-managed
   subdirs (`snapshots/`, `output/`, `tmp/`, `.git/`, …), hidden/junk files
   (`.DS_Store`), and the destination `data/raw/` tree itself are skipped — so
   `SRC` can safely be `data/` (which already contains `data/raw/`) without
   re-ingesting the study's own organized files.
2. **Classify** each staged file by name + extension (case-insensitive):
   - `*.pdf` -> `annotated_pdfs/`
   - `*.xlsx`/`*.csv` whose name contains `mapping`/`dictionary`/`deb`/`codebook` -> `data_dictionary/`
   - other `*.xlsx`/`*.csv` -> `datasets/`
   - everything else -> `_unclassified/`
3. **Place** into `data/raw/<study>/<bucket>/` -- unless the tree is already
   organized (bucket dirs present and `datasets/` non-empty), in which case it
   no-ops, unless `--force`.
4. **Draft** `config/<study>/_forms_manifest.yaml` listing every `datasets/`
   file under `required:` (empty `optional:`/`reject:`), only if no manifest
   exists (a hand-tuned one is never clobbered).
5. **Review note** -- if any file landed in `_unclassified/`, write one count-only
   note to `output/<study>/audit/human_review/intake/` (Note 22).

Duplicate / collision-pair resolution stays with `dataset-deduplication` (skill 2).

## CLI

```bash
make organize STUDY=<name> SRC=<dir-or-zip>
make organize SRC=<dir-or-zip>                        # STUDY auto-detected (refuses if none)
make organize STUDY=<name> SRC=<dir-or-zip> FORCE=1   # rebuild an organized tree
make organize STUDY=<name> SRC=<inbox-dir> ADD=1      # file NEW files into an organized study
make organize STUDY=<name> SRC=data ADD=1 PRUNE=1     # file new files, then delete them from SRC

python plugins/report-ai-study-pipeline/skills/raw-data-intake/scripts/run.py \
  [--study <STUDY>] --src <dir-or-zip> [--force | --add] [--prune-source]
```

**Study name resolution.** `STUDY` / `--study` is optional. The target folder
name is resolved and validated *before* anything is filed, in this order: an
explicit name (`--study`/`STUDY=`) → an intentional `STUDY_NAME` env var → an
auto-detected existing `data/raw/<x>/datasets` study. If none of these resolves,
the skill **refuses** (exit 2, nothing filed) rather than inventing a generic
default — so a brand-new study is never silently filed into another study's
folder; name it explicitly with `STUDY=<name>`. The name must be a plain folder
name — a path-injected name (`../evil`, `a/b`) is also rejected up front. The
resolved name and how it was resolved (`explicit` / `detected`) are reported in
the result so the operator can confirm files went to the right study folder.

`PRUNE=1` (`--prune-source`) deletes the loose source files from `SRC` after
they are filed into the raw tree (the skill is copy-by-default; pruning is
opt-in). It only removes files it actually ingested — never anything under the
destination `data/raw/` tree or a managed subdir — and best-effort removes
emptied leftover subfolders.

`ADD=1` (`--add`) files new files from `SRC` into an already-organized study
without the `FORCE` rebuild semantics: it never overwrites an existing file
(reports it as `already_present` instead). `SRC` may be `data/` directly — the
study's own `raw/` and `snapshots/` are skipped automatically, so only the loose
new files (in `data/` or its subfolders) are filed.

**Manifest-gap auto-append.** When a manifest already exists, any newly placed
dataset it does not yet list (under `required`/`optional`/`reject`) is
auto-appended to its `required:` list — append-only, preserving existing entries,
ordering, and comments — so a new form can't silently trip
`ManifestMismatchError` at `make study`. Each appended form is also flagged in
the count-only review note (`manifest_gap_appended`) and reported in the result
(`manifest_gaps` / `manifest_appended=N`).

Emits a value-free `RPLN_SKILL_RESULT:` line with per-bucket counts.

## Result Contract

`RPLN_SKILL_RESULT:` JSON with:

- `ok` — `true` on success (including a no-op skip), `false` on error (missing SRC, etc.)
- `exit_code` — 0 on success, 2 on error
- `summary` — per-bucket counts (`datasets=N; annotated_pdfs=N; ...`) or `"already organized — skipping"`
- `data.counts` — `{datasets, annotated_pdfs, data_dictionary, _unclassified}` (all int)
- `data.unclassified` — list of filenames that landed in `_unclassified/`
- `data.manifest_written` — `true` if a new draft manifest was created
- `data.skipped` — `true` if the tree was already organized and `--force` was not passed
- `data.review_note` — absolute path to the intake review note, or `null` if no unclassified files

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success (organized, or no-op skip) |
| 2 | Error — missing SRC dir or invalid arguments |

## Portability

- Python 3.11+; no external dependencies beyond the stdlib and the repo's `scripts/` package.
- Path roots (`data/raw/`, `config/`, `output/`) may be overridden via env vars for testing:
  `RPLN_INTAKE_RAW_ROOT`, `RPLN_INTAKE_CONFIG_ROOT`, `RPLN_INTAKE_AUDIT_DIR`.

## What This Skill Does NOT Do

- Does NOT open any workbook or read dataset row values (GR-1).
- Does NOT resolve duplicate files — that is `dataset-deduplication` (skill 2).
- Does NOT fuzzy-match filenames or make reject decisions.
- Does NOT touch the per-study pipeline lock (never a DAG phase).
- Does NOT clobber a hand-tuned `_forms_manifest.yaml`.
