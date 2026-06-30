---
name: sot-lean-generator
description: Use when creating or auditing RePORT-AI Source-of-Truth policy YAML from clinical PDFs and dataset row-1 headers only, especially when PDF-authoritative clinical meaning, header-only dataset binding, no row values, PHI hints, duplicate reconciliation, and derived joined query views are required.
---

# SoT Policy Generator

> **Global Rule (GR-1):** No LLM — including Claude — may read dataset row values at any time, under any circumstance. Column headers (row 1) are the only permitted LLM dataset input. Failure reports carry pattern + column + count only, never a value.

## Core Rule

Generate SoT policy YAML from scratch using only:

1. The printed PDF/page renders for clinical meaning: form title, questions, options, layout, instructions, arrows, widgets, sections, units, and visible annotation conflicts.
2. Dataset row 1 headers for binding only: variable keys and variable order.
3. No dataset row 2+ values. Do not inspect or mention sample values.

The dataset is not the clinical authority. The printed form is the authority. PDF annotation labels help locate fields but can be wrong; record real conflicts in `discrepancies`.

Keep PDF Source Truth and dataset schema as separate authority files. Do not paste dataset schema blocks into the policy YAML. When the LLM needs one queryable object that combines clinical PDF meaning with dataset binding metadata, use the derived joined query view from the assistant runtime instead. That view is generated from the policy YAML plus the matching per-form `*_schema.json`, keys variables once by variable id, and omits `llm_status`, `published_in_jsonl`, and `source_order`. Because the joined view is read directly by LLMs, keep it concise but context-rich: preserve clinical labels, questions, descriptions, relationships, section, type, and PHI action, but avoid escaped Unicode, duplicate variable blocks, row values, source-order noise, and runtime-only noise. Prefer plain ASCII relationship markers such as `->` over encoded arrows.

When a dataset row-1 header has no visible printed widget on the rendered PDF, do not invent a PDF question. Keep the header in `variables` for binding, set `pdf_question: null`, use a widget phrase that says no visible printed widget was found, place it in `unmatched_dataset` unless it is clearly system-generated, and add a top-level `discrepancies` entry. The same applies to misnumbered annotations and visible printed widgets that have no matching dataset header: preserve the signal as a discrepancy instead of silently dropping it.

Duplicate labels need classification, not a blanket failure. Repeated PDF annotation labels are sometimes expected when the same printed option marker appears in multiple rows or table cells, such as repeated `0`/`1`/`2` option labels. Treat those as `pdf_annotation_repeated_option_label` when they explain the PDF, or drop them as non-signal if they do not change variable meaning. Treat duplicate annotation labels as `pdf_annotation_duplicate_or_mislabel` only when they look like duplicated field bindings, shifted row labels, or annotation mistakes. Duplicate dataset row-1 headers are different: source-level names or column positions may be needed during Stage 1/2 compilation so each source column can be inspected. In the final policy YAML, combine duplicate source columns into one variable only when the printed PDF and column context prove they are the same concept; document that collapse as `dataset_duplicate_header_combined_binding`. If duplicate headers have different meanings, stop instead of guessing.

Every variable-like PDF annotation must be reconciled before a final policy file is called good. It must be one of: an exact dataset row-1 header; a documented `pdf_annotation_alias_to_dataset_header`; a documented `pdf_annotation_non_variable_label` / `pdf_annotation_repeated_option_label`; or a documented `printed_widget_without_dataset_header`. The checker fails variable-like annotation labels that are neither exact nor reconciled, but a real printed widget with no dataset header can pass only when it is explicitly listed as a discrepancy and no policy variable is invented for it.

Source names and source-column positions are compile-time scaffolding, not final policy signal. It is acceptable to carry them in `/tmp/<form>_exhaustive.yaml`, unresolved notes, or review notes while reconciling duplicate/ambiguous inputs. The final policy YAML should contain the combined PDF/header truth, not raw source-name dumps, except for concise source attribution inside `discrepancies` when needed to explain a conflict or combined binding.

Do not use generic annotation placeholders as signal. Phrases like `"Visible printed field associated with PDF annotation X"` or `"visible printed widget associated with PDF annotation X"` are not acceptable policy `pdf_question`, `pdf_label`, or `widget` values. Either transcribe the printed PDF wording/widget shape, or use `pdf_question: null` with a discrepancy when the printed widget cannot be verified.

## What This Skill Does

Builds and audits Source Truth policy YAML from printed clinical PDFs plus dataset
row-1 headers, under the authority order **printed PDF > annotation label > dataset
header > dataset rows (forbidden)**. The end product the LLM ever reads is the
derived **joined query view** (`llm_source/SoT/<pair>/joined/`); the policy YAML +
dataset schema construction material is fenced into the audit zone.

The orchestrator invokes this skill at conceptual phase 3 (SoT leg) as a file-path
subprocess, once per form. The subprocess entry (`scripts/run.py`) is a thin
passthrough to the Stage-0 per-form intake CLI (`study_intake.main`): it resolves
the annotated PDF + dataset for one form and produces the deterministic source pack
JSON + 600-DPI page renders, or — when sources are missing/ambiguous — a
PHI-metadata-only human-review note under `audit/human_review/<form>/`. Stages 1–6
(exhaustive YAML write, visual sweep, policy trim, verify, promote, joined-view
generation) are the LLM-driven authoring loop documented below; the LLM reads only
the joined query view and page renders — never dataset row values (GR-1).

## Pipeline (6 stages; Stage 6 is derived query output)

This skill runs as a 5-stage Source Truth pipeline plus a derived Stage 6 query-view step. The temp YAMLs at every intermediate Source Truth stage live under `/tmp/`. The policy YAML is promoted only after every Source Truth stage passes. The Stage 6 joined query view is not an authority file; it is a generated LLM-facing view built from the final policy YAML plus the matching per-form dataset schema JSON.

Use this generated review layout. Do not create a skill-owned folder named `dataset_schema`; that name belongs to the existing runtime JSONL pipeline, not to this SoT review output.

```text
tmp/SoT/<sot-pair-name>/
  pdf/<form>_policy.yaml
  dataset/<form>_schema.json
  joined/<form>_joined_query_view.yaml
```

The SoT pair folder name is the canonical associated form name, sanitized with underscores. Strip file extensions, version suffixes such as `v1.0`, and spacing differences before comparing the PDF and dataset names. If the PDF and dataset normalize to the same form, use that one form name only. Use `<pdf-form>__<dataset-form>` only when the normalized PDF form and dataset form are genuinely different and both names are needed to disambiguate.

Example for `6 HIV v1.0.pdf` and `6_HIV.xlsx`:

```text
tmp/SoT/6_HIV/
```

Policy filenames must not contain the word `lean`; use `<form>_policy.yaml`.

### Stage 0 — Source pack (deterministic)

Use the exact dataset-backed form id (for example, `18_NonConsent`), not a bare numeric form code, when more than one dataset shares a leading form number.

Normal Stage 0 runs through the repo wrapper so missing or ambiguous source pairs are handled as audit-review outcomes instead of dead-end failures:

```bash
uv run --all-groups python -m scripts.source_truth.study_intake \
  --study <study> \
  --form <form> \
  --repo-root .
```

When both sources are available, the wrapper prints `source_pack=/tmp/sot_source_pack_<form>.json` and one `render=...` line per 600-DPI page render.

When the PDF is missing, the dataset is missing, or the dataset match is ambiguous, do not author YAML and do not invent a partial SoT. The wrapper writes a human-review audit file and prints its path:

```text
output/<study>/audit/human_review/<form>/review_report.md
```

The review report title must be `Sot_review: Source Truth Human Review`. It records file/path availability, the missing/ambiguous source classification, and the required next step. It must not contain dataset row values.

Batch generation follows the same rule. If `scripts.source_truth.generate_lean_outputs` discovers a PDF-backed form with a missing or ambiguous dataset, or a selected form is missing its PDF/dataset pair, it writes the same human-review report under `audit/human_review/<form>/` and continues with forms that have complete source pairs. A SoT review report is a handled audit outcome, not a policy file and not a partial source pack.

Use the lower-level extractor only when the exact PDF and exact dataset path are already known:

```bash
uv run --all-groups python skills/sot-lean-generator/scripts/extract_sources.py \
  --repo-root . \
  --pdf "data/raw/<study>/annotated_pdfs/<form>.pdf" \
  --dataset "data/raw/<study>/datasets/<form>.xlsx" \
  --out /tmp/sot_source_pack_<form>.json \
  --render-dir /tmp/sot_render_<form>
```

The extractor falls back to a self-contained xlsx/csv header reader and a pdfplumber-based page reader when `scripts.source_truth.study_intake` is unavailable, so it works whether or not the legacy intake helpers are present.

**PDF render resolution**: every page is rendered at 600 DPI via ghostscript (cross-platform). Lower resolutions have been observed to mislead visual sweeps on small details: 2-vs-3 character-box counts on Initials/ICTC, title double-spaces (e.g., `"HIV Treatment and  CD4 Enumeration"`), and raised numerals beside checkboxes. Do NOT downgrade the resolution; do not work from the pdfplumber text output alone for these details (pdfplumber normalizes whitespace and cannot count boxes).

### Stage 1 — Exhaustive YAML write

**Goal:** capture EVERYTHING visible on the printed form + every dataset row-1 header. No trimming, no signal-vs-noise judgment yet.

1. Read `references/exhaustive_yaml_rules.md` in full.
2. Read `/tmp/sot_source_pack_<form>.json` and view every path listed in its `renders` array, such as `/tmp/sot_render_<form>/<pdf-name>.page-001.png`.
3. Write a verbose, capture-everything YAML to `/tmp/<form>_exhaustive.yaml`. Include every printed marker, footer artifact, annotation, box count, arrow, instruction, layout cue, and visible glyph — even if it looks like noise. This is the wide net.

### Stage 2 — Visual sweep loop (up to 5 iterations)

**Goal:** iterate the exhaustive YAML against every page render until nothing more on the pages is missing from the YAML, or 5 iterations have run — whichever comes first.

Repeat up to 5 times:

1. Open every render listed in `/tmp/sot_source_pack_<form>.json` AND the current `/tmp/<form>_exhaustive.yaml`.
2. Sweep each page render top-to-bottom, left-to-right. For every visible element (text, box, checkbox, arrow, instruction, marker, footer, header), confirm it has a representation in the YAML. Build a delta list:
   - Items on the page but not in the YAML (ADD).
   - Items in the YAML but not justified by the page (REMOVE — but be conservative; only remove if clearly invented).
   - Items in the YAML where the printed form contradicts the encoding (FIX).
3. Apply the deltas to `/tmp/<form>_exhaustive.yaml`.
4. **Termination check:** if iteration N reports zero ADD/FIX deltas, stop. Otherwise continue. After iteration 5, stop unconditionally and record any unresolved items in a top-level `unresolved:` block in the exhaustive YAML.

**Iteration discipline:** each iteration MUST inspect every page render — do not skip the visual sweep in favor of re-reading the source pack JSON. Annotations in the JSON are a helper, not a substitute for looking at the page. The renders are 600 DPI (≈ 5000×7000 for US Letter); if you cannot count discrete character-boxes at this resolution, crop the relevant region with PIL/sips and re-read the crop rather than guessing.

**When to stop early (before 5):** zero deltas on a full sweep. Do not stop early on partial sweeps.

**When to continue with discrepancies instead of stopping:**
- A dataset row-1 header has no visible printed widget/question on the rendered PDF.
- A PDF annotation is misnumbered, shifted to the wrong row, or absent from the dataset headers.
- A duplicate PDF annotation label is an expected repeated option/score marker rather than a duplicated field binding.
- Duplicate dataset headers can be safely combined into one final variable after source-level compile review.
- A visible printed widget appears to have no dataset header.

For these cases, keep `variables.keys()` equal to row-1 headers, encode header-only variables with `pdf_question: null`, and add a top-level `discrepancies` entry. Do not invent printed wording, options, type, units, or clinical meaning for the unmatched header.

**When to stop and ask the human** (do not loop further):
- The dataset header sequence cannot be preserved without inventing variables.
- Duplicate dataset headers have different printed meanings, or no source-level names/positions are available to prove they can be safely combined.
- A widget shape is genuinely ambiguous from the page renders (rendering cropped or low-resolution).
- A visible printed widget has clinical meaning but cannot be reconciled to any dataset header even as a discrepancy.
- A date column cannot be confidently classified as administrative vs clinical for PHI purposes.

Record the unresolved item under `unresolved:` and stop. Do not invent.

### Stage 3 — Policy trim

**Goal:** apply signal-vs-noise rules to the exhaustive YAML and produce the policy YAML.

1. Read `references/policy_yaml_rules.md` in full.
2. Read `/tmp/<form>_exhaustive.yaml`.
3. Write the trimmed result to `tmp/SoT/<sot-pair-name>/pdf/<form>_policy.yaml`:
   - Drop noise: footer/pagination glyphs, unresolved markers without printed text, evidence dumps, coordinates, raw visible text dumps, governance/runtime/source plumbing.
   - Keep signal: form metadata, sections, instructions, arrows, discrepancies, and per-variable section/pdf_label/pdf_question/widget/options/type/format/units/precision/skip_logic/notes/phi as defined by the policy rules.
   - Apply the PHI catalog (`pseudonymize` / `drop` / `jitter_date`) per the policy rules.
   - Use `phi`, not `pii`.
   - Key `variables` exactly by dataset header names, in row-1 order.

### Stage 4 — Verify policy

```bash
uv run --all-groups python skills/sot-lean-generator/scripts/check_lean_policy.py \
  --policy tmp/SoT/<sot-pair-name>/pdf/<form>_policy.yaml \
  --source-pack /tmp/sot_source_pack_<form>.json
```

When a benchmark file is intentionally supplied for a calibration run, add:

```bash
--benchmark /path/to/benchmark.yaml
```

If the verifier fails: fix the policy YAML in place and re-run (up to 5 fix iterations). If the benchmark differs from the PDF/header truth, keep the PDF/header truth and report the benchmark discrepancy.

### Stage 5 — Promote to output

After Stage 4 passes, the **joined query view is the sole LLM-facing SoT file**
(N2/N3/N17). The construction material (policy YAML + dataset schema) goes to the
AUDIT zone — fenced from the LLM by `deny_if_audit_zone` — and ONLY the joined
view is promoted into `llm_source/`:

```bash
# construction material -> AUDIT zone (NOT llm_source)
cp tmp/SoT/<sot-pair-name>/pdf/<form>_policy.yaml \
  output/<study>/audit/SoT_construction/<sot-pair-name>/pdf/<form>_policy.yaml
cp tmp/SoT/<sot-pair-name>/dataset/<form>_schema.json \
  output/<study>/audit/SoT_construction/<sot-pair-name>/dataset/<form>_schema.json
```

Do not write to `output/` before Stage 4 passes. `llm_source/SoT/<sot-pair-name>/`
holds ONLY `joined/` — the policy YAML + dataset schema never enter the LLM read
zone. (The automated pipeline does exactly this in
`generate_lean_outputs._publish_verified_sot_outputs`, and the per-form `tmp/`
intermediates are destroyed after promotion.)

### Stage 6 — Generate joined query view (the sole LLM-facing SoT file)

Use this only after the policy Source Truth and per-form dataset schema already exist. The joined view combines them for LLM querying without exposing the construction files. Build it FROM the audit-zone construction material, and write it INTO `llm_source/`:

```bash
uv run --all-groups python skills/sot-lean-generator/scripts/generate_joined_query_view.py \
  --policy output/<study>/audit/SoT_construction/<sot-pair-name>/pdf/<form>_policy.yaml \
  --schema output/<study>/audit/SoT_construction/<sot-pair-name>/dataset/<form>_schema.json \
  --out output/<study>/llm_source/SoT/<sot-pair-name>/joined/<form>_joined_query_view.yaml
```

For a scratch/calibration pair such as the 6_HIV benchmark:

```bash
uv run --all-groups python skills/sot-lean-generator/scripts/generate_joined_query_view.py \
  --policy tmp/SoT/6_HIV/pdf/6_HIV_policy.yaml \
  --schema tmp/SoT/6_HIV/dataset/6_HIV_schema.json \
  --out tmp/SoT/6_HIV/joined/6_HIV_joined_query_view.yaml
```

Stage 6 output rules:

- Key `variables` once by variable id.
- Put printed-form meaning under `pdf`: section, question, label/subsection when useful, type, description, options, relationships, units/format/precision, notes, and phi hints.
- Put dataset binding under `dataset`: PHI action and concise useful schema notes. Do not include source order in the joined query view.
- Include top-level dataset context: `source_dataset` and `record_count`. Include `jsonl_file` only when that exact JSONL artifact was produced by the same run and belongs to the same SoT output; do not carry old runtime paths into this review layout.
- Include runtime fields only when useful for query context, such as `source_file`.
- Do not include dataset row values.
- Do not include `llm_status`, `published_in_jsonl`, or `source_order`.
- Avoid escaped Unicode and symbol-heavy text. Use plain ASCII markers such as `->`, `-`, and `mm3`.
- If the dataset schema contains duplicate column names, stop and fix the schema or source review before generating the joined view.

## Header Store Lifecycle (Note 16 + Task B4)

The shared header store from Phase 2b (header-extraction) provides dataset column
NAMES only (row 1) for binding dataset columns to PDF form variables during Source
Truth policy creation. This skill may consume the store when available to validate
that row-1 headers match the dataset schema used in Stage 0 source pack generation.

The store is optional (SoT generation falls back to direct CSV/XLSX header reading
if unavailable). It is never serialized into the final policy YAML or joined query
view — it is a pipeline-internal signal used for validation and consistency checks
only (GR-1 compliance).

## Verification Bar (Stage 4 acceptance criteria)

Before claiming completion, confirm:

- YAML parses.
- `variables.keys()` exactly equals the dataset row-1 headers. If row-1 headers have exact duplicates, `variables.keys()` must equal the de-duplicated header sequence and the final file must include `dataset_duplicate_header_combined_binding`.
- Duplicate dataset headers are never silently merged; they are either source-reviewed and documented as a combined binding, or the run stops.
- No `pii` key or text remains; use `phi`.
- No `footnote`, `superscript`, or unresolved-marker residue remains when it is non-signal.
- No row-value language remains: no `sample value`, `sample row`, `row-1 observation`, or dataset-value notes.
- Every useful printed question/option/instruction/arrow from the PDF is represented (the Stage 2 sweep loop is what guarantees this).
- Known annotation-vs-printed-form conflicts are documented with printed-form truth winning clinical meaning and dataset header supplying binding only.
- Known header-only or annotation-only mismatches are documented in top-level `discrepancies`; unmatched headers stay in `variables` as header-only entries, not invented PDF questions.
- Duplicate PDF annotation labels are classified: expected repeated option labels are not treated as binding errors, while duplicate field-binding labels are recorded as duplicate/mislabel discrepancies.
- Variable-like PDF annotation labels are fully reconciled: exact dataset header, alias to a dataset header, non-variable/repeated-label classification, or documented `printed_widget_without_dataset_header`.
- No generic annotation placeholders remain in `pdf_question`, `pdf_label`, or `widget`; these fields must contain printed PDF signal or be null/header-only with a discrepancy.
- Units and date masks match the printed form exactly: do not convert plain printed unit text to Unicode notation, and include `format:` for non-routine printed masks such as `DD/MM/YY`.
- Row-level `Not Done` columns are mutually exclusive with every same-row value/dependent field they suppress, including adjacent free-text "Other, specify" fields.
- Property-validator policy notes are present: `free_text` variables have `phi:` or `notes: "no PHI expected"`, and `type: code` variables with `phi: pseudonymize` have `notes:` explaining the quasi-identifier reason.

## CLI

The orchestrator-facing subprocess entry is the per-form Stage-0 intake passthrough:

```bash
uv run --all-groups python \
  plugins/report-ai-study-pipeline/skills/sot-lean-generator/scripts/run.py \
  --study <STUDY> --form <FORM>
```

It resolves the form's annotated PDF + dataset and either prints `source_pack=…`
plus one `render=…` line per page, or writes a human-review note and prints
`status=human_review_required`. The individual authoring/verification stages each
have their own dev CLIs (see the per-stage commands above): `study_intake`
(Stage 0 source pack), `check_lean_policy.py` (Stage 4 verify), and
`generate_joined_query_view.py` (Stage 6 joined view).

## Result Contract

The subprocess entry emits one `RPLN_SKILL_RESULT:` marker line (the shared skill
contract, `scripts/utils/skill_protocol.py`): the study + form names and the
ok/failed outcome with the intake exit code only — never dataset row values, PDF
content dumps, or sample values. Stage outputs are file artifacts: the source pack
JSON + page renders (`/tmp/`), the policy YAML + dataset schema (audit zone), and
the joined query view (`llm_source/SoT/<pair>/joined/`). A missing/ambiguous source
pair is a handled audit outcome (human-review report), not an error.

## Portability

The authoring stages are LLM-driven and host-neutral — any LLM platform can run
them by reading this `SKILL.md` and the per-stage dev CLIs. The deterministic
Stage-0 intake + verifier + joined-view generator are pure host-side Python
(pdfplumber + ghostscript renders; no network, no LLM call). `agents/llm.yaml`
carries the platform-neutral adapter metadata.

## Exit Codes

The subprocess entry mirrors the Stage-0 intake (`study_intake.main`):

| Code | Meaning |
|---|---|
| `0` | Source pack + renders produced, **or** a missing/ambiguous source pair handled as a human-review audit outcome (both are non-error Stage-0 results). |
| `1` | Stage-0 intake failed (e.g. an unreadable source or an internal extraction error; the exception type NAME only is reported). |
| `2` | Argparse usage error (e.g. missing `--study`/`--form`). |

The downstream authoring stages report their own pass/fail through their dev CLIs
(`check_lean_policy.py` non-zero on a failing policy); fix the policy YAML in place
and re-run, up to the documented fix-iteration cap.

## What This Skill Does NOT Do

- **Never reads dataset row values** — uses printed PDF page renders + dataset row-1 headers only; the dataset is never the clinical authority, and row 2+ values are forbidden input (GR-1).
- **Does not publish construction material to the LLM zone** — only the joined query view enters `llm_source/`; the policy YAML + dataset schema are fenced into the audit zone (`audit/SoT_construction/`).
- **Does not author a partial SoT on missing/ambiguous sources** — it writes a count/path-only human-review note and continues with complete source pairs instead of inventing YAML.
- **Does not invent printed wording** — an unmatched header is kept for binding with `pdf_question: null` and a `discrepancies` entry; it never fabricates a PDF question, options, units, or clinical meaning.
