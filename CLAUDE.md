# CLAUDE.md

**Address:** Sir. **Style:** no emojis, no em-dashes, cite authorities, minimal filler.

## Project

Dual-jurisdiction PHI handling system: synthetic IRB-review-ready corpus + Presidio + rule detectors + human review + export. HIPAA 45 CFR 164.514 (US) first, DPDPA 2023 planned.

## Structure

```
/app
  backend/           FastAPI service (server.py). Reads /app/authorities, imports phi_core.
    phi_core/        Consolidated library (generators, detectors, readers, pipeline, benchmark, llm).
    .env             MONGO_URL, DB_NAME, EMERGENT_LLM_KEY, DATA_DIR, CORPUS_SEED.
  frontend/          React operator console (dark, brutalist, JetBrains Mono).
  authorities/       Primary legal sources + AUTHORITY_MATRIX.md (single source of truth).
  data/              corpus/, uploads/, benchmarks/ artifacts.
  docs/              File-format authority notes.
```

## Constraints

1. Datasets (CSV, XLSX, Parquet) reveal ONLY column headers to LLM. Row values never leave the process for LLM inspection. All other files (PDF, DOCX, TXT, EML, MD) can be fully read.
2. Every span carries an `authority_citation` traceable to AUTHORITY_MATRIX.md.
3. Corpus generation is seeded and deterministic. Same seed produces identical output.
4. No real PHI ever committed or logged.
5. Every human review decision is recorded in the audit log with timestamp, user comment, and action.

## Run

```
sudo supervisorctl restart backend frontend
# backend on :8001, frontend on :3000
```

## Authority index

Start at `authorities/AUTHORITY_MATRIX.md`. Every generator, detector rule, and file-format handler must trace to a row.
