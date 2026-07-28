# PRD - PHI Handling Console

**Version:** 3.0.0 - 2026-07-26 (12-agent architecture)
**Address:** Sir. **Style:** no emojis, no em-dashes, cite authorities.

## Standing goal

See `/app/memory/GOAL.md`. In short: input a study package (datasets + at least one of forms/dictionary/mappings), route it through a 12-agent LLM pipeline, output PHI-handled data safe to share with any AI while preserving clinical and epidemiological signal. LLM/AI never reads dataset row values, only column headers.

## Architecture (12 agents)

Every agent is a Claude Sonnet 4.5 call (Emergent Universal Key by default; BYO-key via LiteLLM for Anthropic, OpenAI, Gemini, OpenRouter, or any OpenAI-compatible endpoint). Every input, output, and duration is persisted to Mongo `agent_log`.

**Specialists (parallel):**
- Lexicon - dictionary/mapping specialist (xlsx/csv codebooks)
- Schema - dataset headers only, never rows
- Instrument - PDF collection forms

**Experts (cache-first, week-refresh):**
- Statute - jurisdiction regulation rulebook (HIPAA, DPDPA, etc.)
- Praxis - PHI transformation techniques (age-cap, year-truncate, ZIP3, hash, pseudonymize)

**Reasoning (Judge <-> Sentinel loop up to 3 iterations, then human review):**
- Judge - per-column decision (keep, drop, cap_age_90, year_only, zip3_truncate, hash, pseudonymize, human_review)
- Sentinel - preview reviewer enforcing 0% PHI leak and 100% accuracy
- Executor - deterministic applier of decisions (no LLM)
- Auditor - verifies Executor output and produces compliance metrics

**External / paper-publishing:**
- Scout - competitor landscape (Presidio, Comprehend Medical, Azure, JSL, etc.)
- Ledger - comparative benchmark (headers-only advantage, metrics narrative)
- Herald - manuscript draft (title, abstract, sections, target venue + alt venues)

## Key endpoints

- `POST /api/sessions/{sid}/intake` - v3 manifest ZIP intake, fail-closed
- `POST /api/sessions/{sid}/handle` - launch 12-agent pipeline
- `POST /api/sessions/{sid}/human-review` - resolve human_review decisions and resume tail
- `GET  /api/sessions/{sid}/results` - decisions, audit, ledger, herald
- `GET  /api/sessions/{sid}/agent-trace` - full agent audit log
- `GET  /api/settings/llm` and `POST /api/settings/llm` - BYO LLM config

## Verified behaviours (testing_agent iteration_3 + iteration_4 fork verification)

- All 26 intake edge cases pass (regression clean).
- Full /handle completes end-to-end: intake -> classifying -> awaiting_human_review -> resolutions -> anonymizing -> complete.
- Dataset export CSV shows correct per-column redactions: `drop` empties the column, `year_only` truncates dates to YYYY, `pseudonymize` yields deterministic tokens, `keep` preserves data as-is.
- Columns are populated on session.files before Schema runs.
- Agent trace shows messages from 10 of 12 agents (Herald optionally times out at 90s, acceptable).
- All 12 agent tiles render on /studies/{sid} with the Run Agent Pipeline button.
- Human review UX: unresolved decisions rendered with dropdown, submit resumes the tail.

### iteration_4 (fork) additional live verification (session 27e9059741e64d05acd633ddd65b8e0c)

- **scrub_text on free-text columns** works end to end: notes containing name/phone/email/SSN produced `[A] ... [D] ... [F] ... [G]` category tags while clinical content ("headache", "acetaminophen", "Blood pressure", "medication list", "UCSF") preserved.
- **Cross-file pseudonym exact-match linkage** verified: same real patient_id `P001` produced identical pseudonym `P4f455ade` in both enrollment.csv AND visits.csv exports. Distinct real values produced distinct pseudonyms. Cross-study salt isolation confirmed.
- **Subject tagging (participant/staff/specimen/site/study)** now populated on decisions and rendered in SessionDetail Subject column.
- **Sentinel deterministic hard-rules** installed: known direct-identifier column names (dob, ssn, mrn, phone, email, fax, patient_name, address, zip, age, url, ip) are forced off `human_review` into safe HIPAA-cited actions before the LLM Sentinel runs.
- **SessionDetail runtime crash fixed**: `getSession(sid)` now catches 404 and renders a clean "study not found" state instead of an uncaught Promise rejection.
- **Regression suite added** at `/app/backend/tests/test_scrub_and_pseudonym.py` (7 tests) and `/app/backend/tests/test_sentinel_hard_rules.py` (10 tests). All 17 green.

### iteration_5 (fork) security audit findings closed

Security audit returned FAIL with SEC-001..005. All fixed in this batch and covered by 47 green regression tests (`/app/backend/tests/test_security_*.py`).

**Goal alignment for each fix** (per `/app/memory/GOAL.md`):

- **SEC-001 (CRITICAL) closed** — serves "fail closed on unsafe input". Introduced `phi_core.paths.safe_join()` and `sanitise_filename()`. `/api/sessions/{sid}/upload` and `/api/sessions/{sid}/intake` now reject any filename that is absolute, contains a separator, resolves outside the session dir, or has NUL. Live curl with `../../../.env` filename returns HTTP 400.
- **SEC-002 (HIGH) closed** — serves "the result can be shared without PHI leak". Mutating endpoints (`POST /api/sessions*`, `/upload`, `/intake`, `/handle`, `/run`, `/review`, `/finalize`, `/human-review`, `/corpus/generate`, `/benchmark/run`, `/settings/llm`) now require `X-API-Token: <API_TOKEN>` when the env var is set. Empty `API_TOKEN` keeps the preview open for local dev. Frontend attaches the token via axios interceptor sourced from `localStorage.phi_api_token`. `/api/sessions` list scrubbed of internal `stored_path`.
- **SEC-003 (HIGH) closed** — serves "LLM never reads rows" (pipeline integrity). Default provider allow-list is `emergent | anthropic | openai | gemini | openrouter`. `openai_compatible` is only enabled when `ALLOWED_LLM_BASE_URL_HOSTS` is set explicitly, and `base_url` must be `https://` with a public IP (rejects loopback, RFC-1918, link-local, `169.254.169.254`, etc.). BYO API keys encrypted at rest via Fernet (`phi_core.crypto`) keyed by `APP_ENCRYPTION_KEY`.
- **SEC-004 (HIGH) closed** — serves "zero PHI slips through, ever". Executor now runs the deterministic detector over `metadata` (dictionary/mapping) files instead of copying them verbatim; `apply_column_actions_to_dataset` fails closed by defaulting unmapped dataset columns to `drop` (or `scrub_text` when `PHI_UNMAPPED_COLUMN_ACTION=scrub_text`). Verified live: `columns.csv` export now contains `[A]`/`[D]`/`[X]` tags instead of raw "James Smith"/phone.
- **SEC-005 (MED) closed** — serves "preview stays usable / fail closed on malformed ZIP". `unpack_zip` enforces total-uncompressed-size cap (`INTAKE_MAX_TOTAL_BYTES=1 GiB`), entry count cap (`INTAKE_MAX_ENTRIES=500`), per-entry compression-ratio cap (`INTAKE_MAX_RATIO=100`), and streams every entry through a 200 MB per-file cap. `/upload` and `/intake` bodies capped at `MAX_UPLOAD_BYTES=250 MB` via chunked streaming.
- **Hardening**: `CORS_ALLOWED_ORIGINS` env pin (falls back to `*` for local dev only); BYO API key encrypted at rest with Fernet (`APP_ENCRYPTION_KEY` auto-generated on first save and persisted to `backend/.env`).

**GOAL invariants after this batch** (`/app/memory/GOAL.md` cross-check):
- (a) LLM never sees dataset row values — HOLDS (Schema headers-only + deterministic scrub_text + SEC-003 SSRF closure).
- (b) Exports contain no residual raw PHI — HOLDS (SEC-004 fail-closed dictionary redaction + unmapped column default = drop).
- (c) BYO API keys stored securely and never returned plaintext — HOLDS (SEC-003 Fernet encryption at rest; GET returns `api_key_set` boolean only).
- (d) Clinical / epidemiological signal preserved — HOLDS (age <=89 kept, year retained, non-PHI text preserved in scrub_text output).
- (e) Cross-file pseudonym linkage on exact value match — HOLDS (verified live: `P001` -> same pseudonym in enrollment.csv AND visits.csv).

## Minor items (from iteration_3, non-blocking)

- Herald sometimes hits the 90s LLM timeout on the full manuscript draft. When it does, pipeline still completes; results.herald is empty and Sir can rerun.
- Judge occasionally sends `dob` to human_review even though the safe answer is `year_only`. LLM prefers the safer route.
- `pseudonymize` output uses a `P<hex8>` prefix (readable, deterministic).

## Backlog (prioritized)

### P0
- Corpus generator revamp with `expected_handling` gold annotations (deferred per Sir).
- India DPDPA jurisdiction pack.

### P1
- Herald: split into abstract-first / sections-later two-call flow so a 90s timeout still gives Sir at least the abstract.
- Publish guard on exports (residual PHI check via detectors before download URL is served).

### P2
- Signed attestation PDF per completed study.
- Additional jurisdictions after DPDPA: UK/GDPR-UK, EU/GDPR, Canada/PIPEDA, Brazil/LGPD.

### Recently DONE (this fork)
- Sentinel hard-rule table (dob->year_only, ssn->drop, mrn->pseudonymize, phone/email/fax->drop, name->drop, zip->zip3_truncate, age->cap_age_90, address/url/ip->drop). Runs deterministically before the LLM Sentinel.
- Multi-provider settings UI page at /settings (BYO API key for Anthropic, OpenAI, Gemini, OpenRouter, OpenAI-compatible; Emergent Universal Key by default).

## Enhancement (would Sir like this next?)

Would Sir like a **Sentinel Hard-Rule table** where I encode: `if column matches known direct-identifier pattern (dob, ssn, mrn, phone, email, address, name) then Sentinel forces Judge to pick from a small allow-list of actions (drop, year_only for dob, zip3_truncate for zip, pseudonymize for name/mrn)`? This closes the "LLM being cautious" pattern where Judge routes obvious PHI to human_review unnecessarily, which is the last remaining accuracy gap.
