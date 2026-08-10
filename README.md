# PHI Handling Console

**Version:** 2.0.0
**License:** MIT
**Address:** Sir. **Style:** no emojis, no em-dashes, cite authorities.

An end-to-end PHI detection, human review, and redaction pipeline for HIPAA (US) with a Presidio + rule detector stack, human-in-the-loop review gate, and a synthetic IRB-review-ready corpus for pre-flight benchmarking.

## Core PHI safety constraint

Datasets (CSV, XLSX, Parquet) expose ONLY column headers to the LLM. Row values never leave the process boundary for LLM inspection. All other files (PDF, DOCX, TXT, EML, MD) may be read fully by the LLM.

## Stack

- **Backend:** FastAPI + Presidio + spaCy + Motor (MongoDB) + Emergent LLM Key (Claude Sonnet 4.5).
- **Frontend:** React operator console. Dark theme, brutalist grid, JetBrains Mono.
- **Corpus:** Deterministic HIPAA Safe Harbor (A-R) + quasi-identifier generator, seeded, reproducible.

## Layout

```
/app
  backend/
    server.py               FastAPI service on :8001
    phi_core/               generators, detectors, file_readers, llm_classifier, anonymizer, benchmark, pipeline
    .env                    MONGO_URL, DB_NAME, EMERGENT_LLM_KEY, DATA_DIR, CORPUS_SEED
    requirements.txt
  frontend/
    src/                    React operator console on :3000
  authorities/              Primary legal sources + AUTHORITY_MATRIX.md (single source of truth for IRB)
  docs/file_formats/        DICOM PS3.15 + FHIR R4 authority notes
  data/
    uploads/{session_id}/   uploaded files (local disk only, never leaves the pod)
    exports/                anonymized outputs
```

## Run

```
sudo supervisorctl restart backend frontend
# backend  :8001
# frontend :3000
```

## Workflow

1. Create session, upload files.
2. Backend reads: headers for datasets, full text for narratives.
3. LLM classifies content type (Claude Sonnet 4.5, headers only for datasets).
4. Presidio + rule detectors flag PHI spans, tagged with 45 CFR 164.514 categories A-R.
5. Human review checkpoint: accept, reject, reclassify, comment. Iterate as needed.
6. Anonymize and export scrubbed files.

## Benchmark

Runs any detector combination against a seeded corpus. Precision, recall, F1 per HIPAA category. Baseline (Presidio + rule) on the shipping corpus: F1 approx 0.78, with 17 of 19 categories at 1.0 recall. Category J (account numbers) and R (internal codes) plus the quasi-identifier layer are the residual gaps that require human judgement per 164.514(b)(2)(ii).

## Authority

All corpus records, detector rules, and file format handlers trace to `authorities/AUTHORITY_MATRIX.md`. Every span in the API carries an `authority` citation.
