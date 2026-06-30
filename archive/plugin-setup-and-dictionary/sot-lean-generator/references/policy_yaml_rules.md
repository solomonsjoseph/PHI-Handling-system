# Policy YAML Rules

These rules are study- and form-agnostic. They apply to any clinical CRF where a printed PDF form is the authority and dataset row-1 headers supply the binding.

## Authority Order

1. Printed PDF form: clinical truth.
2. PDF annotation labels: field-locating hints; can be wrong.
3. Dataset row-1 headers: binding names and order only.
4. Dataset row 2+: forbidden.

When sources disagree, the variable key stays the dataset header, but `pdf_question`, `pdf_label`, `widget`, `options`, `section`, `instructions`, and `arrows` follow the printed PDF. Put conflicts in top-level `discrepancies`.

If a dataset row-1 header has no visible printed widget/question on the rendered PDF, keep it as a header-only variable instead of inventing PDF meaning:

```yaml
sections:
  unmatched_dataset:
    label: null
    note: "dataset row-1 headers with no visible printed PDF widget; see discrepancies"
variables:
  SOME_HEADER:
    section: unmatched_dataset
    pdf_question: null
    widget: "no visible printed widget found on rendered PDF; dataset row-1 header retained for binding only"
    notes: "See discrepancies"
```

Use `system` instead of `unmatched_dataset` only when the column is clearly system-generated, such as `Time_Stamp`. Do not assign `type`, `options`, `units`, `phi`, or a clinical section to an unmatched header unless the printed PDF or an already-approved rule supports it.

Do not use generic annotation placeholders as policy signal. Values such as `"Visible printed field associated with PDF annotation X"` or `"visible printed widget associated with PDF annotation X"` are forbidden because they repeat a locator hint instead of the printed form truth. If the printed question/widget cannot be verified, use `pdf_question: null`, put the variable in `unmatched_dataset` unless it is system-generated, and document the issue in `discrepancies`.

## Required Shape

```yaml
study: "<full study name>"
form:
  number: Form N
  title: "<printed title>"
  version: v1.0
  revision_date: YYYY-MM-DD
  page_count: 1
  # Include subjid_layout / subjid_format only when the form prints a
  # non-trivial subject-ID mask (e.g., segmented character-boxes).
sections:
  header: { label: null, note: "form-header band (no printed section label)" }
variables:
  COLUMN_NAME:
    section: header
    pdf_question: "<printed prompt>"
    widget: "<visual widget shape>"
```

Optional top-level keys:

- `instructions`: standalone printed instructions that change completion. Each entry has ONLY `id`, `text` (verbatim printed), and `location`. Do NOT include an `effect:` field in policy YAML — that is interpretive (lists which downstream variables the instruction gates) and lives in the exhaustive stage only. The per-variable `skip_logic:` already captures the gating from each dependent variable's side; an `effect:` on the instruction is redundant and inferential.
- `arrows`: visibly drawn directional arrows.
- `discrepancies`: printed-form-vs-annotation/header conflicts, including header-only variables with no visible printed widget, safely combined duplicate dataset headers, annotations not in the dataset headers, duplicate/misnumbered annotations, expected repeated annotation labels, and visible printed widgets without a dataset header.

Do not include `footnote_markers` for unresolved or clinically irrelevant superscripts. If the page prints a marker such as `1` or a superscript next to a checkbox but does not print useful explanatory text that affects clinical variables, exclude it as noise.

## Sections Block Naming

Section keys are derived from the printed section/banner label, not abbreviated:

- Lowercased, words joined by `_`, using the *full* printed phrase — not a shortened form.
  - Printed `"HIV Treatment Regimen"` → `hiv_treatment_regimen` (NOT `hiv_treatment`).
  - Printed `"Form Completion"` → `completion`.
- Dataset-only / system-generated columns with no printed section live in a `system` section (NOT `footer`). Header-only columns that are not clearly system-generated and have no visible printed widget live in `unmatched_dataset`. Reserve `footer` only when the printed form actually has a labeled footer band.
- Use `header` when the column sits in the top form-header band with no printed section label.

Every variable's `section:` must match a key declared in the top-level `sections:` mapping.

## Arrows vs Skip Logic

These are TWO different signals — record both when both are visible:

- `arrows` (top-level block): the visibly drawn directional arrow graphic on the printed form. Records what the form *draws* (source variable + option, target variable, optional note). Use when the form has a printed arrow connecting an answer to a downstream field.
- `skip_logic` (per variable): the *gating rule* expressed in prose on the dependent variable. Records when this variable is/isn't completed, based on the upstream value.

If the printed form has an arrow AND a written skip rule, include both. If the form only draws the arrow with no written rule, still add `skip_logic:` on the dependent variable expressing the gating inferred from the arrow (label it `(inferred from arrow)`).

Do not collapse an arrow into a skip_logic-only encoding.

### Inferred clinical-logic relationships on `skip_logic`

Two page-layout patterns encode real clinical logic that must survive into the policy YAML on per-variable `skip_logic`:

- **Terminal skip on a specific option value.** When a printed instruction says "If <option>, skip to <terminal section>" (e.g., "If negative, skip to bottom of form, sign and enter date"), anchor the skip on the variable whose value triggers it. The instruction block itself stays text-only (`id`, `text`, `location`); the downstream gating goes on the trigger variable's `skip_logic` in the form `if '<option label verbatim>', skip to <terminal target> per instruction Ik`.
- **"Not Done" / "Not Done/Unknown" mutex with value fields.** When a single `Not Done` / `Not Done/Unknown` / `Unknown` checkbox sits in the same row/cell as a date/count/percentage widget, the two are alternatives. Record the mutex reciprocally on BOTH variables' `skip_logic` as `mutually exclusive with <SIBLING_VAR>` (label `(inferred)` because the relationship is conveyed by spatial adjacency, not printed prose). If the form uses a row-level `Not Done` column for a repeated table, that checkbox is mutually exclusive with every same-row value/dependent field it suppresses, including free-text "Other, specify" fields.

See `exhaustive_yaml_rules.md` § "Inferred clinical-logic relationships" for detection guidance and worked examples.

## Variable Fields

Use only fields with signal:

- `section`: required, must match a top-level section key.
- `pdf_question`: the full printed prompt as it appears on the form, including any question number (e.g., `"1a. HIV Test Date:"`, `"3. CD4 Testing — Was a CD4 test done?"`). Use `null` only for system-only/free-line fields with no printed prompt (e.g., a dataset-only timestamp column) or row-1 headers with no visible printed widget documented in `discrepancies`.
- `pdf_label`: use ONLY when the printed form has a distinct row/cell label *separate from* the question text — e.g., a left-column label `"3. CD4 Testing"` with a separate prompt `"Was a CD4 test done?"` in the data cell. If the printed text is a single prompt with no separate row label, put it all in `pdf_question` and omit `pdf_label`. Do NOT merge a row label + prompt into `pdf_question` with an em-dash unless the printed form itself uses that punctuation.
- `pdf_subsection`: printed subheading inside a larger row.
- `widget`: required. Must describe the visible control shape with enough specificity that a reader can picture it without the PDF. Use visual counts and structure, not generic labels.
  - Good: `"4 character-boxes (integer)"`, `"Day(2) / Month(2) / Year(4)"`, `"3 mutually exclusive checkboxes (stacked)"`, `"single-line free-text underline beside option N"`, `"9 character-boxes laid out as [X][X][X] - [X][_][_][_][_] - [_]"`.
  - Avoid: `"date box"`, `"checkbox"`, `"checkbox group"`, `"numeric text box"` — these strip the visual structure the reader needs.
  - **Positional accuracy** (verified at 600 DPI): when describing a single checkbox adjacent to a date widget (e.g., a "Not Done" checkbox sitting on the same row as the Day/Month/Year boxes), the widget description MUST identify the spatially adjacent element, not the row-1 question. Example: `"single checkbox 'Not Done' right of HIV_ARTDAT date boxes (Q2a row, far right)"` — NOT `"right of the Yes/No checkboxes in Q2"`. Likewise for mutually exclusive checkbox groups where the first option is inline with the question, do not call the layout `"stacked vertically"` (the first option is at the question line, not below it). Use the precise variant: `"3 mutually exclusive checkboxes — 'Yes' inline right of question; 'No' options below"`. See `exhaustive_yaml_rules.md` § "Positional accuracy traps" for the full list.
- `options`: checkbox/radio labels in printed order, verbatim.
- `type`: `identifier`, `code`, `date`, `time`, `datetime`, `decimal`, `integer`, `free_text`, `signature`, or `initials`.
- `format`: include only when the printed mask carries unusual structure or deviates from convention. Routine date fields (`DD/MM/YYYY`) do NOT need `format`; two-digit-year masks such as `DD/MM/YY` DO need `format`. Identifier fields with a printed segmented layout (e.g., `"NNN-NNNNN-N"`) DO need `format`.
- `units`: verbatim from the printed form. Preserve Unicode only when the PDF prints Unicode; do not upgrade plain printed text such as `mm3` to `mm³`. When the printed unit is a symbol that benefits from expansion, write `"<word> (<symbol>)"` — e.g., `"percent (%)"`.
- `precision`: printed-mask precision.
- `skip_logic`: printed and inferred gating; label inferred logic as `(inferred from arrow)` or `(inferred)`.
- `notes`: only for useful context such as an annotation typo, a quasi-identifier reason, a PHI ambiguity, or the exact validator phrase `no PHI expected` for constrained free-text fields.
- `phi`: downstream handling hint only, not form truth. Allowed common values are `pseudonymize`, `drop`, and `jitter_date`. Apply per this catalog:
  - `pseudonymize` — subject identifiers (typically the SUBJID-equivalent column).
  - `pseudonymize` on `type: code` — allowed only when `notes:` explains why the code acts as a quasi-identifier, such as a site/clinic-style code.
  - `drop` — handwritten signature and initials fields (commonly `*_SIGN`, `*_INIT`).
  - `jitter_date` — *administrative* dates only: form-completion timestamps, visit dates, signature dates (commonly `*_COMPDAT`, `*_VISIT` when date-typed).
  - `free_text` variables — include `phi:` when the field may contain PHI, or `notes: "no PHI expected"` when the printed prompt constrains the field away from PHI.
  - Do NOT mark clinical event dates (test dates, diagnosis dates, treatment-start dates) with `phi:` — these are clinical observations, not administrative metadata, and downstream code handles them through clinical-data policy, not the PHI catalog.
  - If unsure whether a date is administrative or clinical, leave `phi:` off and note the ambiguity in `notes`.

## Forbidden In Policy

Do not add:

- `schema_version`, `policy_status`, `source`, `runtime_binding`, `source_presence`, `coverage`.
- Raw `pdf_visible_text`, coordinates, bounding boxes, pages, annotation dumps, or evidence packs.
- Raw source-name/source-column dumps from the compile stage; keep only the combined final truth plus concise discrepancy attribution.
- Dataset paths or row values.
- `pii`; use `phi`.
- Footnote/superscript residue that does not alter the clinical variable set or field meaning.
- Generic annotation placeholder wording in `pdf_question`, `pdf_label`, or `widget`.

## Discrepancy Patterns That Stay In Policy

Keep the following as signal in top-level `discrepancies`:

- `dataset_header_without_visible_pdf_widget`: a row-1 header exists but no matching printed question/widget is visible in the rendered PDF.
- `dataset_duplicate_header_combined_binding`: exact duplicate row-1 header names were inspected using source-level names/positions during compilation and safely collapsed into one final policy variable because the combined printed/header meaning is the same.
- `dataset_duplicate_header_binding_conflict`: exact duplicate row-1 header names are present but cannot be safely collapsed because the source columns differ in meaning or the source-level names/positions are not enough to prove equivalence.
- `pdf_annotation_alias_to_dataset_header`: a variable-like PDF annotation label is a typo, case mismatch, prefix omission, hyphen/underscore variant, or otherwise a locator alias for an existing dataset row-1 header.
- `pdf_annotation_non_variable_label`: a variable-like-looking annotation label is actually an option, artifact, or printed non-variable label and should not become a dataset variable.
- `pdf_annotation_not_in_dataset_headers`: a PDF annotation label exists but is not a dataset row-1 header.
- `pdf_annotation_duplicate_or_mislabel`: an annotation is duplicated, shifted, misnumbered, or points to the wrong printed widget.
- `pdf_annotation_repeated_option_label`: a duplicate annotation label is expected because the same printed option/score marker appears in multiple rows/cells and is not itself a dataset binding key.
- `printed_widget_without_dataset_header`: a visible printed widget has no matching dataset row-1 header.

For `dataset_header_without_visible_pdf_widget`, include the affected headers in `dataset_column_binding` as a list when several related headers share the same issue. The resolution should say that the variables were retained for binding only and that printed PDF meaning was not invented.

Do not treat every duplicate PDF annotation label as an error. First classify it:

- Expected repeated labels: small option/score labels such as `0`, `1`, `2`, repeated `Yes`/`No`, or table row markers that recur in separate rows/cells. These are not variable bindings unless the dataset header uses that exact label. Keep them only when useful as `pdf_annotation_repeated_option_label`; otherwise drop them as non-signal.
- Binding-like duplicates: labels that look like variable names, dataset headers, or unique field annotations. Keep these as `pdf_annotation_duplicate_or_mislabel` until visually resolved.
- Dataset duplicate headers: exact repeated row-1 header names. During Stage 1/2, use source-level names or column positions to inspect each duplicate column. In final policy YAML, collapse duplicates only when the columns share the same combined meaning and document `dataset_duplicate_header_combined_binding`; otherwise stop with `dataset_duplicate_header_binding_conflict`.

Source names are compile-time scaffolding. They can appear in exhaustive YAML, unresolved notes, or review notes while reconciling duplicate columns, but the final policy variable should carry the combined PDF/header truth. Do not emit raw source-column inventories in final policy YAML unless a short discrepancy field is needed to explain the collapse or conflict.

Before calling a form good, reconcile every variable-like PDF annotation label that is not an exact dataset header. Exact matches need no discrepancy. Non-exact labels must be documented as one of:

- `pdf_annotation_alias_to_dataset_header` with `dataset_column_binding` pointing to an existing variable key.
- `pdf_annotation_non_variable_label` or `pdf_annotation_repeated_option_label` when the label is not a data field.
- `printed_widget_without_dataset_header` when the PDF appears to contain a real data-entry field but the dataset has no row-1 binding. This can pass only as an explicit discrepancy; do not add a policy variable without a dataset binding key.

### Forbidden text (verifier-enforced, applies anywhere in the file)

The verifier rejects the literal words `pii`, `footnote`, `superscript`, `row-1 observation`, `sample value`, `sample row`, and `dataset is authoritative` ANYWHERE in the file — keys, values, notes, comments. To describe a marker on the form that has no explanatory text, use phrasings like:

- `"unlabeled '1' marker next to checkbox (no printed definition)"` instead of `"footnote '1' present but no text"`.
- `"raised numeral next to option"` instead of `"superscript '1' present"`.

## When to Stop and Ask

The skill should pause for human input — not guess — when:

- The dataset header sequence cannot be preserved without inventing variables.
- A widget shape is genuinely ambiguous from the screenshot (e.g., the rendering is cropped or low-resolution).
- A visible printed widget has clinical meaning but cannot be reconciled to any dataset header even as a discrepancy.
- A date column cannot be confidently classified as administrative vs clinical for PHI purposes.

Do not stop solely because a dataset row-1 header lacks a visible printed widget. Add `dataset_header_without_visible_pdf_widget` to `discrepancies`, retain the variable as header-only, and continue. Stop only when continuing would require invented printed wording or unsafe PHI/date classification.
