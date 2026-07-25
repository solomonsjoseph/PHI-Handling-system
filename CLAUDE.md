# CLAUDE.md

**Address:** Sir. **Style:** no emojis, no em-dashes, cite authorities, minimal filler.

## Project

USA-only HIPAA PHI handling system aligned with the `feat/v2-multi-jurisdiction`
intake-manifest/v3 convention. Default flow: ZIP intake, headers-only classification
for datasets, full-content classification for forms and metadata, human review,
export. Corpus generation is experimental and optional.

## Structure

```
/app
  backend/
    server.py               FastAPI on :8001. Intake, sessions, corpus (exp), benchmark (exp).
    phi_core/
      intake.py             ZIP unpack + manifest v3 validator (datasets/, forms/, dict OR mappings/).
      generators.py         US HIPAA A-R + quasi-identifier corpus (experimental).
      detectors.py          Presidio + rule stack, cross-category merge policy.
      file_readers.py       Dataset (headers only) vs narrative vs metadata.
      llm_classifier.py     Claude Sonnet 4.5 via Emergent Universal Key.
      anonymizer.py         Per-file span application with HIPAA-tagged tokens.
      benchmark.py          Precision/recall/F1 per HIPAA category (experimental).
      pipeline.py           Intake-aware orchestrator.
      models.py             Pydantic models incl. Session with intake_status.
      db.py                 Motor MongoDB access.
    .env                    MONGO_URL, DB_NAME, EMERGENT_LLM_KEY, DATA_DIR.
  frontend/                 React operator console on :3000.
  authorities/              HIPAA 164.514 primary sources + AUTHORITY_MATRIX.md.
  docs/file_formats/        DICOM PS3.15 + FHIR R4 notes.
  data/uploads/<sid>/       intake.zip + unpacked/ (never leaves the pod).
  data/exports/             anonymized outputs.
```

## Intake manifest v3 (mandatory for default flow)

Top-level components inside the ZIP:
- `datasets/` (mandatory) - `.csv`, `.xls`, `.xlsx` single-sheet. LLM sees column headers only.
- At least ONE of the following must accompany `datasets/`:
  - `forms/` - `.pdf`. LLM reads full content.
  - `data_dictionary/` - `.csv`, `.xlsx`. Metadata; classified but skipped for PHI scan.
  - `mappings/` - `.csv`, `.xlsx`. Metadata; classified but skipped for PHI scan.

Fail-closed: unsupported extensions or missing components land in `_unclassified`
and block the study. Exit codes: 0 ready, 8 review_required, 2 failed.

## Constraints

1. Datasets never expose row values to the LLM.
2. Every span carries a 45 CFR 164.514 authority citation.
3. Every human review decision is recorded with timestamp and comment.
4. No real PHI committed or logged.
5. Corpus generation is behind the `/experimental/*` routes only.

## Run

```
sudo supervisorctl restart backend frontend
# backend  :8001
# frontend :3000
```

## Alignment

Structure mirrors `feat/v2-multi-jurisdiction` phi_engine intake conventions
(datasets/forms/data_dictionary|mappings, single-sheet dataset, .json/.jsonl
rejected, fail-closed on missing components, headers-only classification).
