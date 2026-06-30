# Exhaustive YAML Rules

These rules govern **Stage 1 (Exhaustive YAML write)** and **Stage 2 (Visual sweep loop)** of the `sot-lean-generator` pipeline. The output of these stages is `/tmp/<form>_exhaustive.yaml` — a verbose, capture-everything YAML that will later be trimmed in Stage 3 against `policy_yaml_rules.md`.

**Core principle:** Stage 1/2 catches everything. Stage 3 trims. Do not skip ahead — if you trim during Stage 1, you cannot recover what was dropped, and the visual sweep in Stage 2 cannot verify a YAML that already chose to omit.

## What "Exhaustive" Means

For Stage 1, the YAML must include every visible element on the printed form AND every dataset row-1 header. "Visible" means anything a human reader would point at, including:

- Form metadata: study name, form number, form title (verbatim, including any double-spaces or typos), version, revision date, page count.
- Subject-ID layout/format: the full segmented mask if the form prints one (e.g., `[1][0][2] - [0][_][_][_][_] - [_]`).
- Sections: every printed section banner, full printed label (not abbreviated). Add a `system` section for dataset-only columns with no printed section.
- Per-variable:
  - The full printed question text including question number (e.g., `"3a. CD4 Test Date"`).
  - Any separate row/cell label distinct from the question (e.g., `"3. CD4 Testing"` left-cell label + `"Was a CD4 test done?"` right-cell question).
  - Subsection headings inside larger rows.
  - Widget shape with explicit visual structure: count of character-boxes, layout pattern (`Day(2) / Month(2) / Year(4)`), inline-vs-stacked checkbox arrangement, fixed masks like `[X][X].[X] %`, free-text underline positions.
  - Every option label verbatim, in printed order.
  - Type, format, units (verbatim from the printed form; preserve Unicode only when the PDF prints Unicode), precision.
  - Skip logic — both printed prose and any logic inferable from drawn arrows (mark `(inferred from arrow)` or `(inferred)`).
  - Every printed marker beside the field, even if undefined (e.g., a raised numeral `1` beside a "Not Done" checkbox). Record as `printed_markers:` on the variable — do NOT call it `footnote` or `superscript`.
- Instructions: every standalone printed instruction block, verbatim, with location and downstream effect.
- Arrows: every visibly drawn directional arrow on the form. Record `from` (source variable + option), `to` (target variable or instruction), and a short note describing what the arrow looks like.
- Discrepancies: every annotation-vs-printed-form conflict observed, every dataset row-1 header without a visible printed widget, every safely combined duplicate dataset header, every duplicate dataset header binding conflict, every annotation label that is not a dataset header, every expected repeated annotation label that explains the PDF, and every visible printed widget without a dataset header.
- Footer artifacts: pagination, batch glyphs, alignment squares, footer-band text. Capture under a top-level `footer_artifacts:` list for Stage 3 to drop.
- `unresolved:` block: anything the visual sweep flagged but could not resolve in 5 iterations.

## Required Top-Level Shape

```yaml
study: "<full study name verbatim>"
form:
  number: Form N
  title: "<printed title verbatim, including any printing quirks>"
  version: v1.0
  revision_date: YYYY-MM-DD
  page_count: N
  subjid_layout: "<segmented mask if printed, else null>"
  subjid_format: "<format string if printed, else null>"

sections:
  header: { label: null, note: "<descriptive note>" }
  <section_key>: { label: "<printed banner label>" }
  system: { label: null, note: "dataset-only system-generated columns with no printed section" }

instructions:
  - id: I1
    text: "<verbatim printed instruction>"
    location: "<where on the form>"
    effect: "<which variables this gates>"

arrows:
  - from: { variable: <SRC_VAR>, option: "<option label>" }
    to:   { variable: <TGT_VAR>, target: "<e.g., Day box, instruction I2>" }
    note: "<visual description of the arrow>"

discrepancies:
  - kind: pdf_annotation_label_typo
    where: "<location of annotation>"
    pdf_annotation_says: <ANNOT_VALUE>
    printed_form_truth: "<verbatim printed text>"
    dataset_column_binding: <DATASET_HEADER>
    resolution: "<short prose>"
  - kind: dataset_header_without_visible_pdf_widget
    where: "<form region or repeated-row group>"
    pdf_annotation_says: null
    printed_form_truth: "No visible printed question/widget found on rendered PDF for these row-1 headers"
    dataset_column_binding: [<HEADER_A>, <HEADER_B>]
    resolution: "Retain variables for binding only with pdf_question: null; do not invent printed meaning"

footer_artifacts:
  - "<verbatim footer-band text or glyph description>"

variables:
  <DATASET_HEADER>:
    section: <section_key>
    pdf_label: "<row/cell label or null>"
    pdf_question: "<full printed prompt including question number, or null for dataset-only>"
    pdf_subsection: "<subsection heading or omit>"
    widget: "<visual structure with counts>"
    options:
      - "<verbatim option 1>"
      - "<verbatim option 2>"
    type: <identifier|code|date|time|datetime|decimal|integer|free_text|signature|initials>
    format: "<mask if printed, else omit>"
    units: "<verbatim with Unicode, else omit>"
    precision: "<from printed mask, else omit>"
    skip_logic: "<printed gating, and (inferred from arrow) where applicable>"
    printed_markers:
      - "<verbatim marker description, e.g., raised numeral '1' beside checkbox>"
    notes: "<context such as annotation typo; omit if none>"
    phi: <pseudonymize|drop|jitter_date|omit>

unresolved:
  - "<anything Stage 2 flagged but could not resolve in 5 iterations>"
```

## Verbatim Capture Rules

- **Preserve every printing quirk** in Stage 1: double-spaces in titles, irregular capitalization, exact punctuation. Stage 3 may normalize later if the policy rules call for it.
- **Preserve Unicode glyphs** verbatim (`³`, `²`, `±`, `≥`, `≤`, `µ`, em-dash `—`, en-dash `–`). Do not transliterate to ASCII.
- **Record raised numerals / superscript markers as `printed_markers:`** on the variable they sit beside. Use phrasings like `"raised numeral '1' beside the Not Done checkbox (no printed definition on form)"`. Never use the literal words `footnote` or `superscript` — those are forbidden by the Stage 4 verifier and any trace will survive into the policy trim.

## Widget Specificity (mandatory in Stage 1)

Every `widget:` value must include:

- A **count** when there are discrete elements: `"3 character-boxes"`, `"4 mutually exclusive checkboxes (stacked)"`, `"2 character-boxes (side by side)"`.
- A **structural layout** when applicable: `"Day(2) / Month(2) / Year(4)"`, `"[X][X].[X] %"`, `"single-line free-text underline beside option N"`, `"9 character-boxes laid out as [1][0][2] - [0][_][_][_][_] - [_]"`.
- A **relative position** when relevant: `"single checkbox right of <SIBLING_VAR> date boxes"`, `"single checkbox under <SIBLING_VAR> in the Part I cell"`.

Banned vague widgets in Stage 1: `"date box"`, `"checkbox"`, `"checkbox group"`, `"numeric text box"`, `"width not legible"`. If the screenshot resolution genuinely prevents counting boxes, write `widget: "<best-effort description>"` AND append the variable to the top-level `unresolved:` block with status `widget_box_count_uncertain`.

For a dataset row-1 header with no visible printed widget after a full screenshot sweep, do not force a vague widget. Use the explicit header-only wording: `"no visible printed widget found on rendered PDF; dataset row-1 header retained for binding only"`, set `pdf_question: null`, and add `dataset_header_without_visible_pdf_widget` to `discrepancies`.

Do not write generic annotation placeholders such as `"Visible printed field associated with PDF annotation X"` or `"visible printed widget associated with PDF annotation X"`. Annotation labels are locator hints only. Capture the actual printed prompt/widget from the screenshot, or mark the variable as unmatched/header-only with a discrepancy when the printed widget cannot be verified.

### Positional accuracy traps (verified at 600 DPI required)

The Stage 0 render is 600 DPI. Below 400 DPI, the following details have been observed to mislead visual sweeps:

- **"Not Done" / "Not Done/Unknown" single checkboxes adjacent to date widgets**: the widget description MUST identify the *spatially adjacent element* (typically the date widget's boxes), NOT the row-1 question. Wrong: `"single checkbox to the right of the Yes/No checkboxes in Q2"`. Right: `"single checkbox right of HIV_ARTDAT date boxes (Q2a row, far right)"`. The "Not Done" sits on the same horizontal row as the date boxes, one row below the Yes/No question.
- **Mutually exclusive checkbox groups where the first option is inline with the question**: do NOT call this layout `"stacked vertically"`. The first option's vertical position is at the question line, not below it. Use phrasing like: `"3 mutually exclusive checkboxes — 'Yes' option inline right of the question; 'No' options below the 1a/1b widgets"`. The pattern is common on Q1-style "Was X done?" prompts where the affirmative option carries an inline instruction like "Yes, record …".
- **Initials / short-text character-box widgets** (e.g., 2 vs 3 boxes for "Initials:") are below the threshold where a sub-400-DPI render is reliable. Count at the 600 DPI render only.
- **Form title double/triple spaces**: per the verbatim capture rule, preserve `"HIV Treatment and  CD4 Enumeration"` exactly as printed. The pdfplumber text extraction NORMALIZES whitespace and will report a single space; trust the high-DPI render for the spacing, not the source pack text. Footer text on the same page may use a different (e.g., single-spaced) variant — that is a footer artifact, not the title.

## Arrows vs Skip Logic (Stage 1 captures BOTH)

If the form draws an arrow AND prints a skip rule, record both:

- The arrow goes in the top-level `arrows:` block (with from/to/note).
- The gating rule goes on the dependent variable's `skip_logic:`.

If the form draws an arrow with NO printed prose rule, still write `skip_logic:` on the dependent variable, labeled `(inferred from arrow)`.

If the form prints a rule with NO drawn arrow, only `skip_logic:` is populated; `arrows:` does not get an entry.

Never collapse an arrow into a skip_logic-only encoding.

### Inferred clinical-logic relationships (must be captured on per-variable `skip_logic`)

Beyond printed prose and drawn arrows, two recurring page-layout patterns encode real clinical logic that downstream LLMs need. Capture these on each affected variable's `skip_logic` field, labeled `(inferred)`:

**Pattern A — Terminal skip on a specific option value.** When a printed italic instruction says "If <option>, skip to <terminal section>" (e.g., "If negative, skip to bottom of form, sign and enter date"), anchor that skip on the variable whose *value* triggers it — not just the instruction block.

- Identify the trigger variable: the one whose options include the value referenced by the instruction (e.g., HIV_HIV has option `"Negative (-)"`, so I1 anchors on HIV_HIV).
- On that variable's `skip_logic`, append: `if '<option label verbatim>', skip to <terminal target> per instruction Ik`.
- Worked example for HIV_HIV gated by I1 "If negative, skip to bottom of form...":
  ```yaml
  HIV_HIV:
    skip_logic: "Complete only when HIV_HIVND == 'Yes, …'; if 'Negative (-)', skip to completion per instruction I1"
  ```
- The `instructions[]` block keeps the verbatim text and `location`. The downstream-variable gating belongs on `skip_logic`, not on an interpretive `effect:` field.

**Pattern B — "Not Done" / "Not Done/Unknown" sibling is mutually exclusive with adjacent or same-row value fields.** When a single checkbox labeled `"Not Done"`, `"Not Done/Unknown"`, `"Unknown"`, or similar sits in the same row/cell as a date/count/percentage widget, the two are alternatives — a participant ticks the checkbox *instead of* entering a value. When a repeated table has a row-level `Not Done` column, that checkbox suppresses every same-row value/dependent field in the test row, including free-text "Other, specify" fields.

- Record the mutex on BOTH variables, reciprocally, so an LLM querying either side sees the alternative.
- The relationship is not printed in prose on the form (it is conveyed by spatial adjacency), so mark `(inferred)`.
- Worked examples from Form 6 HIV:
  ```yaml
  HIV_ARTDAT:
    skip_logic: "Complete only when HIV_ARTTX = Yes (inferred from arrow); inferred mutually exclusive with HIV_ARTND"
  HIV_ARTND:
    skip_logic: "Complete when ART initiation date is not done (inferred); mutually exclusive with HIV_ARTDAT"

  HIV_CD4:
    skip_logic: "Complete only when HIV_CD4DONE = Yes (inferred); inferred mutually exclusive with HIV_CD4ND"
  HIV_CD4ND:
    skip_logic: "Complete only when HIV_CD4DONE = Yes (inferred); mutually exclusive with HIV_CD4"
  ```
- How to detect at Stage 2 visual sweep: scan each row containing a date/integer/decimal/free-text widget for a `Not Done` / `Not Done/Unknown` / `Unknown` checkbox sitting on the same horizontal band, directly below the value boxes, or in a repeated-table row-level `Not Done` column. If found, the pair is an alternative; record the mutex on both.

**Printed masks and units stay exact.** If the PDF prints a two-digit-year date mask (`DD/MM/YY`), include `format: "DD/MM/YY"`; do not silently treat it as the routine four-digit-year date. If the PDF prints `mm3`, keep `units: "cells/mm3"`; use `mm³` only when the rendered PDF itself prints the superscript character.

Both patterns must be present after Stage 2 (visual sweep) completes — they are not noise and Stage 3 (policy trim) must preserve them.

## PHI Catalog (apply in Stage 1, may be revised in Stage 3)

Apply `phi:` per this catalog. Stage 3 may revise if the policy rules narrow the scope.

- `pseudonymize` — subject identifiers (SUBJID-equivalent) AND site/clinic codes that act as quasi-identifiers (e.g., `ICTC`, `SITE`, `*_CODE` where the value identifies a small location). If `phi: pseudonymize` is used on `type: code`, add `notes:` explaining the quasi-identifier reason.
- `drop` — handwritten signature/initials fields (`*_SIGN`, `*_INIT`) AND system-generated timestamps (`Time_Stamp`, `*_TIMESTAMP`) that are not on the printed form.
- `jitter_date` — administrative dates only: form-completion timestamps, visit dates, signature dates (`*_COMPDAT`, `*_VISIT` when date-typed).
- `free_text` fields — add `phi:` when PHI may be entered, or add `notes: "no PHI expected"` when the printed prompt constrains the answer away from PHI.
- Do NOT mark clinical event dates (test dates, diagnosis dates, treatment-start dates) with `phi:`.
- If unsure whether a date is administrative or clinical, leave `phi:` off, mark `notes:` with the ambiguity, AND list the variable in `unresolved:` for human input.

## Stage 2 Visual Sweep Discipline

Each of the up-to-5 sweep iterations MUST:

1. Open the screenshot. Do not work from the source pack JSON alone.
2. Sweep top-to-bottom, left-to-right.
3. For every visible element on the page, point to its representation in the YAML — or add one.
4. Compare every YAML element to the page — flag any that look invented.
5. Compare every drawn arrow to the `arrows:` block.
6. Compare every printed marker (raised numerals, dashes, asterisks) to the relevant variable's `printed_markers:`.
7. Compare every section banner to the `sections:` mapping.
8. Compare every option label to the variable's `options:` list, verbatim and in order.

Terminate the loop on the first iteration that yields zero ADD/FIX deltas. If iteration 5 still has deltas, stop and record them under `unresolved:`. Do not silently continue.

## What NOT to Drop in Stage 1

Even if it looks like noise, capture in Stage 1:

- Footer-band text (pagination, version glyphs, batch codes) → into `footer_artifacts:`.
- Raised numerals next to checkboxes with no printed definition → into per-variable `printed_markers:`.
- Visible alignment squares in form corners → into `footer_artifacts:` with description.
- Annotation labels that disagree with the printed form → into `discrepancies:`.
- Dataset row-1 headers that have no visible printed widget → into `variables:` as header-only entries and into `discrepancies:`.
- PDF annotation labels that are not dataset row-1 headers, including duplicate/misnumbered row labels → into `discrepancies:`.

## Header/PDF Mismatch Handling

Some forms have dataset row-1 headers that are not visibly printed on the PDF, or annotations that are duplicated, shifted, or absent from the row-1 headers. These are discrepancies, not permission to invent.

Use these `kind` values:

- `dataset_header_without_visible_pdf_widget`: row-1 header exists, but the rendered PDF has no visible question/widget for it.
- `dataset_duplicate_header_combined_binding`: duplicate row-1 header names were inspected using source-level names/positions and safely combined into one final policy variable.
- `dataset_duplicate_header_binding_conflict`: duplicate row-1 header names cannot be safely combined because the source columns differ in meaning or source-level names/positions are insufficient.
- `pdf_annotation_alias_to_dataset_header`: annotation label is a locator alias for an existing dataset row-1 header.
- `pdf_annotation_non_variable_label`: annotation label is an option/artifact/non-variable printed label.
- `pdf_annotation_not_in_dataset_headers`: annotation label exists, but no row-1 header matches it.
- `pdf_annotation_duplicate_or_mislabel`: annotation is duplicated, shifted to the wrong row, misnumbered, or otherwise conflicts with the printed widget.
- `pdf_annotation_repeated_option_label`: annotation label repeats by design as a printed option/score/table marker and is not a unique dataset binding.
- `printed_widget_without_dataset_header`: visible printed widget exists, but no row-1 header matches it.

Rules:

- Keep every row-1 header in `variables`, in order.
- If row-1 headers contain exact duplicate names, use source-level names/positions during Stage 1/2 to inspect each source column. For the final policy YAML, combine duplicate source columns into one variable only when they have the same combined PDF/header meaning and record `dataset_duplicate_header_combined_binding`. Do not suffix or guess new final keys.
- For unmatched headers, set `pdf_question: null`, place them in `unmatched_dataset` unless clearly system-generated, and use the header-only widget wording above.
- For matched headers, `pdf_question` and `widget` must come from printed screenshot signal, not from a generic annotation-label placeholder.
- Do not assign options, units, type, PHI handling, or skip logic to an unmatched header unless the printed PDF or an already-approved rule supports it.
- Continue the generation when the mismatch can be represented this way. Stop only when continuing would require invented printed wording, unsafe PHI/date classification, or a clinical widget that cannot be represented even as a discrepancy.

Duplicate PDF annotation labels need classification:

- Expected duplicates: repeated small option labels (`0`, `1`, `2`, etc.), repeated `Yes`/`No`, or repeated row/table markers. These are not binding keys unless the dataset header uses that exact label. Keep only when they explain a visible repeated-option pattern; otherwise trim as noise.
- Problem duplicates: variable-like labels, labels matching dataset headers, or labels that appear to point at multiple different printed fields. Record as `pdf_annotation_duplicate_or_mislabel` and resolve visually where possible.

Source names are allowed in the exhaustive/compile artifact because they help reconcile duplicate source columns. They should be trimmed from final policy YAML unless a concise discrepancy field needs them to explain a combined binding or unresolved conflict.

Variable-like annotation labels that are not exact dataset headers must be reconciled during the visual sweep. Use aliases for obvious typos/prefix/case/underscore differences, non-variable labels for options/artifacts, and `printed_widget_without_dataset_header` for real printed fields missing from row-1 headers. Do not leave them as generic annotation-only noise.

Stage 3 will drop these per the policy rules. Stage 1 keeps them so Stage 2 can verify completeness against the screenshot.
