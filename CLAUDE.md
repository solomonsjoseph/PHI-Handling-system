# CLAUDE.md — Claude Code Handoff Document

**Purpose:** This file tells Claude Code everything it needs to know to resume the PHI-Handling-IRB-approval-ready build in Sir's terminal. Read this first, in its entirety, before touching any other file.

**Session origin:** Built by Claude Opus 4.7 in claude.ai web chat on 2026-04-20. Full chat transcripts are accessible via Claude's conversation search if running in claude.ai, but not in Claude Code. This document is the authoritative handoff.

---

## About Sir (read carefully)

- **Preferred address:** "Sir"
- **Role:** Scientific Programmer / Research Teaching Specialist III
- **Primary interests:** AI, cybersecurity, quantum computing, science
- **Formatting rules:** NO emojis. NO em-dashes. Clear, minimal, high-signal output.
- **Communication style:** Apply the Truth Protocol — cite sources for factual claims, explicitly state "I cannot confirm this" when uncertain, never fabricate data, always stress-test ideas aggressively rather than validating them. Sir wants bulletproof thinking, not reassurance.
- **Principle:** "Less is More" — outputs should be minimal, efficient, implementation-ready. No filler.
- **Security posture:** Extreme emphasis on security-first design. PHI protection is non-negotiable.

## The project context

**Repository target:** `https://github.com/brucebanner010198-commits/PHI-Handling-IRB-approval-ready.git`

**This repository is SEPARATE from RePORTaLiN-RAG** (Sir's primary work project). Do not conflate them. RePORTaLiN-RAG is a privacy-first, air-gapped, local-first, HPC-deployable agentic RAG system for clinical trial data. This repository is a dual-jurisdiction (HIPAA + DPDPA) PHI test corpus and benchmark framework designed to validate systems like RePORTaLiN-RAG. The corpus is general-purpose.

**The directive (verbatim from Sir):**
> "Do full research no matter how much time it takes, search the web, research and gather all that you need to make it IRB ready to show that our system will handle all PHI cases and result in PHI free IRB audit ready results. Don't stop until you get everything you need to make it IRB approval ready system and corpus."

Sir explicitly requested:
1. All PHI edge cases for USA and India, with a common/universal layer and strictly separated country-specific layers (never mixed)
2. All file formats including Excel, CSV, PDF, DICOM, FHIR, email, DOCX, image EXIF, Parquet — "anything presently not constituted include that as well for worst case scenarios"
3. Benchmark comparisons vs Presidio, Amazon Comprehend Medical, John Snow Labs, Azure Health, i2b2
4. GitHub delivery via tarball workflow, separate from RePORTaLiN

---

## What is already complete (v0.1 skeleton)

### Research phase — 8 primary authorities consulted and analyzed

All saved to `authorities/`:

1. `01_hipaa_164_514_full.md` — Full 45 CFR 164.514 text with three findings that change corpus design:
   - **Limited Data Set (LDS) is a THIRD de-identification tier** at 164.514(e) that I had missed in prior corpus work. Excludes 16 direct identifiers, permits dates and general geography (town/city, state, ZIP). Still PHI. Requires Data Use Agreement per (e)(4).
   - **Re-identification codes** at 164.514(c) — permitted if not derived from individual AND mechanism not disclosed. Hash-with-salt permitted; hash-of-SSN forbidden.
   - **Fundraising context** at 164.514(f) — name/DOB/address permitted without authorization for fundraising. Context-aware detection required.

2. `02_dpdp_rules_2025.md` — Official Gazette G.S.R. 846(E), notified 2025-11-13. Critical findings:
   - Phased commencement: Rules 1,2,17-21 immediate; Rule 4 at 2026-11-13; Rules 3,5-16,22-23 at 2027-05-13
   - Rule 6 security safeguards with 1-year minimum log retention
   - Rule 7 breach notification: 72-hour Board notification
   - Rule 13(3) algorithmic due diligence (direct regulatory hook for RAG/LLM audits)
   - Rule 14 identifier vocabulary: customer ID file number, acquisition form number, application reference number, enrolment ID, email, mobile, licence number
   - Rule 16 research exemption via Second Schedule (8 compliance conditions)
   - Fourth Schedule Part A: pediatric clinical/educational exemption

3. `03_icmr_2017.md` — ICMR National Ethical Guidelines 2017. Critical findings:
   - Section 1.1.5: right to life supersedes right to privacy in limited cases (suicidal ideation, homicidal tendency, HIV status, court order)
   - Table 2.1: four-tier risk categorization (less than minimal / minimal / minor increase / more than minimal)
   - Section 2.3.5: explicit coding/anonymization mandate
   - Section 3.3.2: institutions are custodians, not owners
   - Section 3.8.3: HMSC approval required for international collaboration
   - Table 4.2: review tiers (exempt / expedited / full)

4. `04_spdi_rules_2011.md` — IT Act SPDI Rules 2011 Rule 3 eight categories:
   - Password, financial, physical/physiological/mental health, sexual orientation, medical records, biometric, any detail related, any info received

5. `benchmarks/01_presidio_entities.md` — Microsoft Presidio full entity list across 13 jurisdictions (62 entities) with gap analysis against HIPAA 18 and DPDPA Rule 14.

6. `benchmarks/02_aws_comprehend_medical.md` — AWS Comprehend Medical DetectPHI's 9 entity types mapped to HIPAA categories. Key finding: their "ID" mega-category collapses 9 HIPAA categories into one.

7. `docs/file_formats/01_dicom_ps3_15.md` — DICOM PS3.15 Annex E Basic Confidentiality Profile with all 11 options and specific tags.

8. `docs/file_formats/02_hl7_fhir_r4.md` — FHIR R4 Patient resource PHI field analysis.

### The single most important deliverable so far

**`authorities/AUTHORITY_MATRIX.md`** — consolidated matrix with five tables:
- Table A: 62 identifier categories mapped across HIPAA / DPDPA / ICMR / SPDI / DICOM / FHIR / Presidio / AWS Comprehend Medical
- Table B: complete legal citation list (US + India + EU + standards + research)
- Table C: benchmark tool coverage gaps
- Table D: 25 file formats with coverage status
- Table E: OWASP LLM Top 10 + MIA attack surface

This is the document IRB reviewers should read first. Every corpus claim must trace to a row in this matrix.

### Repo skeleton files

- `README.md` — IRB-reviewer-first structure with explicit Known Limitations
- `LICENSE` — MIT with no-real-PHI notice
- Directory layout matches README specification

---

## What remains (the build plan)

Execute in this order. Each step is idempotent — it can be re-run without breaking prior work.

### Phase 1 — Import prior corpus and reconcile taxonomy
If Sir has the prior `phi-test-corpus-v1.0.1.tar.gz` in `/mnt/user-data/uploads/` or similar, extract it first. It contains 5,942 records with 1,990 gold spans and 10 layers. The `MANIFEST.json` hash starts with `bd13d8c0f324b890`.

Prior corpus layers (reuse):
- structured (10 records)
- short_queries (140)
- narratives (160)
- statistical (8 fixtures, 4,626 rows)
- injection (100)
- reidentification (5 scenarios)
- unit_identifiers (850)
- dpdpa_strict (50)
- mia_context (6)

Tasks:
- Copy prior corpus into `corpus/legacy_v1.0.1/`
- Verify offset validation still passes
- Update taxonomy doc to reference new authorities

### Phase 2 — Gap-closure generators (the research identified these)

Each generator goes in `generators/`. Each must:
- Cite its primary authority in a docstring
- Emit test cases with an `authority_citation` field
- Be seeded (deterministic)

Required new generators:

**From HIPAA research:**
- `hipaa_lds.py` — Limited Data Set tier (16 identifiers excluded, dates+geography retained). Cite: 45 CFR 164.514(e)
- `hipaa_reid_codes.py` — Re-identification code test cases (permitted vs forbidden patterns). Cite: 164.514(c)
- `hipaa_fundraising.py` — Fundraising context cases. Cite: 164.514(f)
- `hipaa_verification.py` — Disclosure verification audit log cases. Cite: 164.514(h)
- `hipaa_biometric.py` — Biometric identifier fixtures (fingerprint, voice, retinal, DNA references). Cite: 164.514(b)(2)(i)(P)
- `hipaa_device.py` — Device identifier fixtures (UDI-DI patterns). Cite: (M)
- `hipaa_fax.py` — Fax numbers as distinct from phone. Cite: (E)
- `hipaa_vehicle.py` — VIN (17 char) patterns distinct from license plates. Cite: (L)

**From DPDPA research:**
- `dpdpa_second_schedule.py` — 8-condition compliance fixtures per Second Schedule
- `dpdpa_pediatric_exemption.py` — Fourth Schedule Part A clinical/educational exemption cases
- `dpdpa_algorithmic_dd.py` — Rule 13(3) algorithmic due diligence scenarios
- `dpdpa_breach_timing.py` — 72-hour breach notification fixtures
- `dpdpa_consent_manager.py` — Token-forwarding with encrypted content cases
- `dpdpa_rule14_identifiers.py` — customer ID file number, acquisition form, application reference, enrolment ID

**From ICMR research:**
- `icmr_risk_categorization.py` — Four-tier risk tags
- `icmr_vulnerability.py` — Three vulnerability categories (legal/clinical/situational)
- `icmr_emergency_disclosure.py` — Suicidal ideation, HIV, court order override cases
- `icmr_hmsc_international.py` — HMSC approval metadata

**Indian identifiers (missing from Presidio):**
- `in_abha.py` — ABHA 14-digit + ABHA Address (user@abdm)
- `in_ctri.py` — CTRI registration ID
- `in_ration_card.py` — 29 state-variant formats
- `in_uan_esi_cghs_bpl.py` — EPF UAN, ESI, CGHS, BPL numbers
- `in_driving_license_state.py` — 30+ state-variant driving license formats

**Quasi-identifier layer:**
- `quasi_identifier_combinations.py` — Sweeney 2002 k-anonymity violations (DOB + gender + ZIP; rare disease + small geography; profession + ZIP)

### Phase 3 — File format generators (the "worst-case" layer Sir demanded)

Each goes in `generators/file_formats/`:

- `xlsx_gen.py` — Excel with PHI in cell values + authors + sheet names + defined names + custom XML properties. Use `openpyxl`.
- `csv_gen.py` — Clean RFC 4180 + dirty-CSV edge cases (unquoted commas, embedded newlines, Windows/Unix line endings, BOM, trailing whitespace)
- `pdf_gen.py` — PDFs with PHI in text + author metadata + form fields + XFDF. Use `pypdf` or `reportlab`.
- `docx_gen.py` — DOCX with track changes, comments, author metadata, embedded OLE objects. Use `python-docx`.
- `dicom_header_gen.py` — Synthetic DICOM headers covering all tags in `docs/file_formats/01_dicom_ps3_15.md`. Use `pydicom`.
- `fhir_gen.py` — FHIR R4 Patient + Practitioner + Encounter + Observation bundles (JSON and XML). Use `fhir.resources`.
- `cda_gen.py` — HL7 CDA documents with PHI in narrative
- `hl7v2_gen.py` — HL7 v2.x messages with PID, NK1, IN1 segments
- `eml_gen.py` — RFC 5322 emails with PHI in headers + body + attachments
- `exif_gen.py` — JPEG/TIFF with GPS + artist + comment fields (synthetic images). Use `Pillow` + `piexif`.
- `parquet_gen.py` — Parquet with sensitive column names + row-level PHI. Use `pyarrow`.
- `sqlite_gen.py` — SQLite files with PHI in rows

### Phase 4 — Benchmark adapters

Each goes in `benchmarks/`:

- `presidio_adapter.py` — Runs Presidio against corpus, emits Presidio's results in our format. Uses `presidio_analyzer`.
- `comprehend_medical_adapter.py` — Optional (requires AWS credentials). Uses `boto3.client('comprehendmedical')`.
- `azure_health_adapter.py` — Optional. Uses Azure Health Data Services De-ID API.
- `jsl_adapter.py` — John Snow Labs Healthcare NLP (requires license).
- `metrics.py` — Precision, recall, F1, gap detection rate, per-jurisdiction breakdown.

### Phase 5 — Validation harness

Each goes in `harness/`:

- `generate_corpus.py` — Single command to rebuild entire corpus from seeded generators
- `run_all_validations.py` — Offset validator + hash validator + taxonomy closure
- `mia_framework.py` — Shadow-model MIA per Nature Sci Rep 2024 methodology
- `clinical_plausibility_review.py` — ASQ-PHI-style review harness for clinician sign-off

### Phase 6 — Review documents

Each goes in `docs/`:

- `VALIDATION_PROTOCOL.md` — Overall validation process
- `COUNSEL_REVIEW_CHECKLIST.md` — Per-item legal sign-off template (one row per identifier category in the matrix)
- `CLINICIAN_REVIEW_PROTOCOL.md` — Medical plausibility review (ASQ-PHI n=300, 3 reviewers recommended)
- `THREAT_MODEL.md` — OWASP LLM Top 10 2025 + MITRE ATLAS mapped to corpus layers
- `REPRODUCIBILITY.md` — Exact rebuild steps, seed, hash
- `KNOWN_LIMITATIONS.md` — Everything the corpus does NOT cover (expand the README section)
- `ATTESTATION_TEMPLATE.md` — Per-release attestation template

### Phase 7 — Repository infrastructure

- `CONTRIBUTING.md` — How to contribute (including authority citation requirement)
- `CODE_OF_CONDUCT.md` — Contributor Covenant 2.1
- `SECURITY.md` — Private security disclosure policy (Sir's email or a security@ alias)
- `CHANGELOG.md` — Keep-a-Changelog format
- `MANIFEST.json` — Hash, record count, span count, generator versions
- `requirements.txt` — Pinned Python dependencies
- `.github/workflows/ci.yml` — Run validations + benchmarks on push
- `.github/ISSUE_TEMPLATE/bug_report.md`
- `.github/ISSUE_TEMPLATE/authority_citation.md` — For submitting new authority references
- `.github/ISSUE_TEMPLATE/new_identifier.md` — For proposing new identifier categories
- `.gitignore` — Standard Python + corpus artifacts

### Phase 8 — Final packaging

- Run full validation (must pass)
- Generate MANIFEST.json
- Create `phi-corpus-v2.0.0.tar.gz`
- Verify with fresh extract + validate
- Hand to Sir for `git push`

---

## How to run this in Claude Code

### Initial setup (Sir does this once)

```bash
# Create working directory outside RePORTaLiN-RAG
mkdir -p ~/dev && cd ~/dev

# Extract the handoff tarball
tar -xzf ~/Downloads/phi-handling-handoff-v0.1.tar.gz
cd PHI-Handling-IRB-approval-ready

# Start Claude Code in this directory
claude
```

### Give Claude Code this exact prompt

```
Read CLAUDE.md in full before doing anything else. Then read 
authorities/AUTHORITY_MATRIX.md to understand scope. Then execute 
the build plan in Phase 1 through Phase 8 in order. Stop after 
each phase and report status. Use the Truth Protocol.
```

### Claude Code conventions for this project

- Truth Protocol is mandatory. Cite sources.
- No em-dashes, no emojis in any generated file.
- Every generator must have a docstring citing its authority.
- Every test case must include `authority_citation` field.
- Seeded randomness — use `random.Random(seed)` pattern, never unseeded `random.*`
- Deterministic output — given same seed, bitwise identical output
- Test with `pytest` before committing
- Commit messages follow Conventional Commits
- Never commit real PHI. Never commit AWS/API credentials.
- Before pushing: run `harness/run_all_validations.py` and include output in PR

### Workflow Sir uses

1. Claude Code builds a phase
2. Sir reviews the output, runs validations, tests generators
3. If good: `git add . && git commit -m "feat: phase N complete"`
4. Sir runs `git push origin main` (credentials are Sir's, not Claude's)
5. Next phase begins

### Critical: Claude Code must NEVER

- Request GitHub credentials or Personal Access Tokens
- Push directly to GitHub — Sir always does this with his own credentials
- Use real PHI for testing — always synthetic, seeded
- Skip authority citations
- Claim completeness without running validation

---

## Resuming work efficiently

When Sir runs Claude Code in this directory, the first thing Claude Code should do is:

1. Read this entire `CLAUDE.md`
2. Read `authorities/AUTHORITY_MATRIX.md` (all five tables)
3. Read `README.md`
4. Run `git log --oneline` to see commit history
5. Check `.phi-build-status` (see below) to see what phase is current
6. Report status to Sir and ask which phase to execute next

### Phase tracking

Create a file `.phi-build-status` in the repo root with the current phase:

```
phase: 1
last_completed_phase: 0
last_updated: 2026-04-20T04:00:00Z
```

Update this file after each phase completion. This is the single source of truth for build state.

---

## Open questions for Sir to answer when resuming

These were not resolved in the claude.ai session and need Sir's input:

1. **AWS credentials for Comprehend Medical benchmarking** — will Sir provide a dedicated AWS account with billing limits, or should this benchmark be optional/skipped?
2. **Azure Health Data Services** — same question, optional?
3. **John Snow Labs** — has a paid license; can Sir arrange or skip?
4. **Clinician reviewers for ASQ-PHI** — does Sir have access to clinicians for the plausibility review protocol?
5. **Counsel review** — will Sir's institution's legal counsel review the authority matrix, or does this need external arrangement?
6. **Corpus size target** — prior v1.0.1 was 5,942 records. Does Sir want a larger v2.0 corpus (e.g., 15,000 records) or keep size similar with expanded taxonomy coverage?
7. **Pediatric clinical trial data** — does Sir's RePORTaLiN-RAG actually handle pediatric data? This affects whether DPDPA Fourth Schedule fixtures need production density or just sample coverage.

---

## Final note

Sir explicitly said: "Don't stop until you get everything you need to make it IRB approval ready system and corpus." Honor this. Completeness matters more than speed. Stress-test your own work. If a generator feels inadequate, say so and rebuild it.

When in doubt, cite the authority matrix.

---

**End of handoff document. Proceed to `authorities/AUTHORITY_MATRIX.md` next.**
