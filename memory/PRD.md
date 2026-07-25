# PRD - PHI Handling Console

**Version:** 2.1.0 - 2026-07-25 (intake-manifest/v3 alignment)
**Address:** Sir. **Style:** no emojis, no em-dashes, cite authorities.

## Problem statement (verbatim, in order)

> I want to refine the existing system and increase accuracy of the result. Find out all the improvements we can make to this project.
>
> [...] The user sees an interface for uploading files/folders. The classifier extracts dataset info (column headers only) and full extraction from every other source, classify with jurisdiction (HIPAA for USA), then goes to the next process. LLM/AI reads only column headers for datasets. Web app runnable locally, secure, user sees progress, human review, iterative loop, exportable data.
>
> Does the current implementation clearly create a corpus based on US jurisdiction rules considering all edge cases (optional experimental step) but the default option is upload data containing forms and/or dictionary (or mapping or both) and dataset. User arranges data within designated folders and uploads a ZIP file which gets unzipped and goes through classification after successful intake. The classifier extracts dataset information (column headers only) and extracts all other information from every other source. Then classify with jurisdiction regulation like HIPAA for USA. Then move to next process. Refine, aligning with feat/v2-multi-jurisdiction and conflict_250726_1829 branches.

## Alignment target

`feat/v2-multi-jurisdiction` intake-manifest/v3 conventions, plus Sir's clarification (2026-07-25):
- `datasets/` is the only fully-mandatory component (`.csv/.xls/.xlsx` single-sheet).
- Alongside `datasets/`, at least ONE of `forms/` (`.pdf`), `data_dictionary/`, or `mappings/` (`.csv/.xlsx`) must be present.
- `.json`, `.jsonl` NOT accepted as datasets.
- Unsupported suffix, multi-sheet xlsx dataset, or symlink lands in `_unclassified` and blocks the study.
- Headers-only classification for datasets; full content for forms and metadata.
- Exit codes: 0 ready, 8 review_required, 2 failed.
- USA-only jurisdiction end-to-end.

## User personas

- **IRB reviewer**: needs traceability of every span to a primary legal source.
- **Clinical trial data engineer**: needs to run PHI scrubbing on a study package with confidence.
- **Privacy engineer**: needs benchmark numbers to certify accuracy.

## Core requirements (static)

1. Default entry is a ZIP upload conforming to intake-manifest/v3.
2. Datasets (CSV, XLSX, Parquet single-sheet): LLM sees ONLY column headers.
3. Forms (PDF): LLM reads full content.
4. Data dictionary / mappings (CSV, XLSX): full read, classified as metadata, skipped for PHI scan, byte-copied to export.
5. Fail-closed intake: missing component or unsupported file blocks the study.
6. Every span carries a 45 CFR 164.514 authority citation.
7. Human review is mandatory before export. Iterative loop supported.
8. Corpus generation and benchmark are experimental, behind `/experimental/*` routes.

## Whats implemented (2026-07-25)

- **New `phi_core/intake.py`** implementing the v3 manifest validator:
  - ZIP unpack with security checks (no traversal, no symlinks, per-file 200 MB cap).
  - Single-root wrapper normalization (accepts both `study/datasets/...` and `datasets/...` layouts).
  - Component routing: datasets, forms, data_dictionary, mappings, `_unclassified`.
  - Explicit extension whitelists, single-sheet xlsx check, exit-code contract.
- **Server**: new `POST /api/sessions/{id}/intake` accepts a ZIP, returns a redacted receipt with counts and per-component file lists.
- **`GET /api/intake/spec`** exposes the manifest v3 spec to the UI.
- **Pipeline**: `ingest_file` now accepts and stores `component`; classifier and detectors read component to route work; metadata files skipped from PHI scan and byte-copied at export.
- **Bug fix**: spans now carry `file_id`; anonymizer filters per-file so dataset header-hint spans no longer leak into narrative outputs.
- **Frontend**: default route now `/studies/new` with a ZIP drop-zone and manifest v3 requirements table. Corpus and Benchmark demoted to `/experimental/corpus` and `/experimental/benchmark` with orange (exp) badges in the nav. Study detail page adds an Intake panel with unclassified entry list and per-component tags on each file row.

## Baseline accuracy (unchanged from v2.0.0 measurement)

Corpus (seed 20260420, 2 records/category = 40 records, 71 gold spans):
- **F1 = 0.781, precision = 0.738, recall = 0.831**
- 17 of 19 HIPAA categories at 1.0 recall
- Category J (accounts), R (internal codes), and QUASI are the residual gaps that 164.514(b)(2)(ii) requires human review to close.

End-to-end study test (patients.csv + consent.pdf + columns.csv):
- Intake status: ready, exit 0
- 11 spans detected (6 header hints, 5 form-content spans)
- Redacted CSV: every PHI column replaced with HIPAA-tagged tokens
- Redacted PDF text: NAME, DATE, MRN, PHONE, CLINICAL_TRIAL_ID redacted
- Dictionary: copied through as-is (no PHI in a schema table)

## Backlog

### P0
- Retro fit residual PHI guard gate (Presidio-AND-regex) on the export before publish, matching the `phi_engine` invariant.
- OpenAPI tags on all routes for a clean `/docs`.

### P1
- Post-detection LLM pass on forms to surface quasi-identifiers per 164.514(b)(2)(ii).
- Custom Presidio recognizers for weak categories J and R.

### P2
- Multi-jurisdiction re-enablement (India, EU, Brazil, Uganda, Australia) once the USA pipeline is signed off.
- Signed attestation PDF per completed study.

## Enhancement suggestion

Would Sir like a **Publish Guard** step that automatically runs Presidio + a regex sweep on the exported files and refuses the download link when residual PHI is detected? That is exactly the invariant the `feat/v2-multi-jurisdiction` branch enforces at `phi_engine.security.phi_guard_gate`, and it turns this pipeline from "operator-attested" into "gate-enforced" without additional user work.
