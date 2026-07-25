# PRD - PHI Handling Console

**Version:** 2.0.0 - 2026-04-20
**Address:** Sir. **Style:** no emojis, no em-dashes, cite authorities.

## Problem statement (verbatim)

> I want to refine the existing system and increase accuracy of the result. Find out all the improvements we can make to this project.

Follow-ups:
- Full end-to-end PHI handling from file upload to export.
- LLM/AI reads only column headers for datasets, full content for other files.
- Human review checkpoint with feedback/comments, iterative loop.
- Multi-jurisdiction ready, start with US.
- Install Presidio + spaCy locally, skip paid tools.
- CLAUDE.md was bloated; slim it.
- Remove stale files that do not serve the purpose.
- Web app runnable locally, secure upload, real-time progress visible to user.

## User personas

- **IRB reviewer**: needs traceability of every span to a primary legal source.
- **Clinical trial data engineer**: needs to run PHI scrubbing on a batch of files with confidence.
- **Compliance auditor**: needs the audit log and export artifacts to sign off.
- **Privacy/security engineer**: needs benchmark numbers on the detector stack to certify accuracy.

## Core requirements (static)

1. Datasets (CSV, XLSX, Parquet): LLM sees ONLY column headers.
2. Narratives (PDF, DOCX, TXT, EML, MD): LLM may read full content.
3. Every span carries a 45 CFR 164.514 authority citation.
4. Human review is a mandatory blocking gate before export.
5. Corpus generation is seeded and deterministic.
6. No real PHI committed to code or logs.
7. Multi-jurisdiction architecture, US-first.

## Architecture

- **Backend** (FastAPI on :8001) with modules:
  - `phi_core/generators.py` - jurisdiction-registered corpus generator (US HIPAA A-R + quasi-identifier).
  - `phi_core/detectors.py` - Presidio + rule detectors, merged with tie-breaker.
  - `phi_core/file_readers.py` - dataset vs narrative dispatch, headers-only for datasets.
  - `phi_core/llm_classifier.py` - Claude Sonnet 4.5 via Emergent Universal Key, JSON classification.
  - `phi_core/anonymizer.py` - CSV/XLSX/text redaction with review decisions.
  - `phi_core/benchmark.py` - precision/recall/F1 per HIPAA category and detector.
  - `phi_core/pipeline.py` - orchestrator, emits ProgressEvents to SSE queue.
  - `server.py` - REST + SSE. All routes under /api.
- **Frontend** (React on :3000) pages: Sessions, NewSession, SessionDetail, Corpus, Benchmark. Sidebar shows Authority Matrix.
- **Storage**: MongoDB (sessions, corpora, benchmarks). Local disk for uploaded and exported files.

## Whats implemented (2026-04-20)

- Consolidated `phi_core` library replacing the scattered `/app/generators`, `/app/harness`, `/app/validators` directories.
- Bug fixes vs prior generators:
  - NPI now Luhn-checked over `80840` + body per CMS spec.
  - Ages over 89 aggregated to "90+" per 164.514(b)(2)(i)(C).
  - MBI alphabet excludes S, L, O, I, B, Z exactly.
- Presidio + rule stack: baseline F1 approx 0.78 on the shipping seeded corpus, 17 of 19 HIPAA categories at 1.0 recall.
- Rule detectors close known Presidio gaps: MRN, MBI, NPI, VIN, UDI, device serial, biometric templates, photo file refs, clinical trial IDs, labeled fax, ZIP with strict lookbehind.
- LLM classifier returns JSON with content type, likely PHI domains, risk tier, notes with legal citation.
- Human review checkpoint UI with accept, reject, reclassify, comment, iterate.
- Anonymizer produces `[REDACTED:<HIPAA_CATEGORY>:<ENTITY_TYPE>]` tags in exports.
- SSE stream on `/api/sessions/{id}/stream` for real-time progress.
- Benchmark page with recharts bar chart, per-category recall breakdown.
- Slimmed CLAUDE.md from 335 lines to 40 lines.
- Removed stale scaffolding: `/app/generators`, `/app/harness`, `/app/validators`, `/app/tests`, `/app/requirements.txt`, `/app/setup-claude-code.sh`.

## Backlog (prioritized)

### P0 (must-have before IRB)
- Additional jurisdictions: India DPDPA Rule 14 generator (Aadhaar, PAN, ABHA, CTRI, UAN, ESI, CGHS, BPL, GSTIN, state-specific ration/DL).
- File format expansion: PDF form fields, XFDF, DOCX comments/track changes, DICOM headers, FHIR bundles, EXIF, HL7 v2.
- MANIFEST.json with per-run corpus hash, generator version pins.

### P1 (accuracy)
- Custom Presidio recognizers for the 2 weak categories (J account numbers, R internal codes).
- Post-detection LLM pass on narratives to catch missed quasi-identifiers (per 164.514(b)(2)(ii)).
- Membership inference (MIA) shadow-model framework per Nature Sci Rep 2024.

### P2 (operations)
- Per-user auth (JWT), audit log persistence beyond in-memory.
- Folder upload with zip streaming.
- OpenAPI docs at /docs already exposed by FastAPI; add human-readable operator guide.

## Environment

- `MONGO_URL` - local MongoDB.
- `DB_NAME` - phi_handling.
- `EMERGENT_LLM_KEY` - Emergent Universal Key for Claude Sonnet 4.5 (`claude-sonnet-4-5-20250929`).
- `DATA_DIR` - /app/data (uploads + exports).
- `CORPUS_SEED` - 20260420.

## Enhancement suggestion

Would Sir like to add a small "Attestation" page that renders a signed PDF summarising each session (corpus hash, detector versions, F1 baseline, review iterations, span counts, reviewer comments)? That is the single artefact an IRB reviewer would want to file and would turn this from an operator console into a submittable evidence packet.
