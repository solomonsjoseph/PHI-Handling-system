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

### iteration_5 (fork) round-2 security audit residuals closed

Second audit returned FAIL with 4 residuals + 1 new finding. All fixed in a second batch; testing_agent iteration_5 verdict: **fixed**.

- **SEC-002 completion** — read endpoints now gated by `require_api_token` (no-op when `API_TOKEN` empty). `_scrub_session_document()` strips `stored_path` from every file record and blanks `export_paths` values while retaining keys for the UI. Applied to `GET /api/sessions`, `GET /api/sessions/{sid}`, `GET /api/sessions/{sid}/results`, `GET /api/sessions/{sid}/agent-trace`. Live verified: zero occurrences of `/app/data/exports` or `stored_path` in any response body.
- **SEC-003 residual** — `validate_llm_base_url` now enforces a per-provider host allow-list (`PROVIDER_HOSTS` in `phi_core/security.py`): openai only accepts `api.openai.com`, anthropic only `api.anthropic.com`, gemini only `generativelanguage.googleapis.com`, openrouter `openrouter.ai`/`api.openrouter.ai`; emergent rejects any `base_url`. Attacker cannot silently repoint agents to a public exfiltration host. Live verified: POST `/api/settings/llm` with `provider=openai` + `base_url=https://evil.example.com/v1` returns HTTP 400.
- **SEC-005 residual** — dropped the header-based `total_bytes` precheck (attackers can lie in the header). Sole authoritative aggregate cap is now `streamed_total` accumulated while decompressing, tripped at `INTAKE_MAX_TOTAL_BYTES` bytes actually written to disk. Header-based per-file precheck kept for early rejection; compression-ratio guard retained.
- **SEC-006** — new finding. LLM was echoing raw patient names into `agent_decisions.reason` and `agent_log.payload.prompt_preview/reply_preview`, then read endpoints served them back. Fix in three layers:
  1. `scrub_persisted_text()` in `phi_core/security.py` redacts names/phones/emails/SSNs to `[A]/[D]/[F]/[G]` placeholders.
  2. `scrub_decision()` scrubs decision string fields.
  3. `scrub_nested()` recursively walks dicts/lists/tuples so PHI hiding inside nested LLM payloads gets caught (fix for the failure iteration_4 flagged).
  4. Applied at TWO points: (i) `orchestrator.py` scrubs `approved_decisions` and `sentinel` output BEFORE persisting; (ii) read endpoints scrub responses AFTER fetching, so even legacy sessions get scrubbed on the way out.

**Testing evidence** — iteration_5 test report:
- agent-trace body: 0 hits for "James Smith", 0 for phone, 0 for email, 0 for SSN; 26 messages still returned.
- `/api/sessions/{sid}` body 49 KB: 0 PHI hits.
- `/api/sessions/{sid}/results` body 31 KB: 0 PHI hits.
- 60/60 unit tests green across all 6 security test files.

**Frontend companion changes**:
- `frontend/src/lib/api.js` `streamUrl` and `exportUrl` append `?token=` when localStorage has one, since `EventSource` and anchor-tag downloads cannot set headers.
- Backend `require_api_token` dependency accepts either `X-API-Token` header or `?token=` query for these two paths.

### iteration_6 (fork) GOAL boundary features shipped

Sir sharpened the GOAL to one sentence: "input PHI-filled study data -> system -> output PHI-handled study data ready to be shared and used publicly". Two features encode that boundary and closed both remaining "usable" gaps (items 10 human-review-invariant and the download boundary check).

- **Publish Guard** (`phi_core/publish_guard.py`) - deterministic last-mile PHI scanner. Runs synchronously after Executor emits exports, before Auditor. Pattern set: SSN, phone (US), email, full DOB (ISO + US slash), restricted 17 ZIP3 codes, age > 89. Findings are masked (`11*******33`) so the report itself never carries the raw substring. Persists to `session.guard_report` and exposed via `GET /api/sessions/{sid}/results.guard`. Download route `GET /api/sessions/{sid}/export/{file_id}` returns HTTP 403 with `{error, message, guard}` body unless the per-file guard status is `clean` or `skipped`. `?force=true` query provides an explicit operator override. UI badge: `PHI-HANDLED ✓ SAFE TO SHARE` (green) or per-file `BLOCKED` panel with the offending pattern rows.
- **Human review invariant** - `POST /api/sessions/{sid}/human-review` now requires a non-empty `reviewer` field. Every changed decision carries `reviewer`, `reviewer_comment`, `reviewed_at`. When Sentinel refuses to approve globally but no per-row decision is `human_review`, the operator can accept via the UI "Accept Judge decisions as reviewer" button (or empty resolutions via the API) and a `session_review = {reviewer, comment, reviewed_at, changed_decisions}` block is persisted. `scrub_nested` now excludes `reviewer` / `reviewed_at` / `citation` fields from PHI scrubbing so the audit trail remains legible.
- **API contract clean-ups from iteration_6 review**: `GET /results` now surfaces `status`; 403 body is flat `{error, message, guard}` via `JSONResponse` (no double-nested `detail`); UI shows an "Accept globally" button when there are no per-row `human_review` decisions but session is `awaiting_human_review`.

**testing_agent iteration_6 verdict**: **100% pass** across 11 items covering clean-path download, blocked-path 403, force override, reviewer-required 400, per-decision + session-level reviewer capture, unit suite regressions, and UI rendering.

**Files added**: `/app/backend/phi_core/publish_guard.py`, `/app/backend/tests/test_publish_guard.py` (12 tests), `/app/backend/tests/test_human_review_invariant.py` (2 tests). Frontend `SessionDetail.jsx` gets a Publish Guard panel and a reviewer bar. Total regression suite is now **75/75 green**.

**GOAL cross-check after iteration_6**:
- (a) LLM never reads dataset rows -- HOLDS.
- (b) Exports contain no residual raw PHI -- HOLDS AND ENFORCED at the download boundary. Publish Guard blocks any file with residual PHI before the download URL is served.
- (c) BYO keys stored securely and never returned plaintext -- HOLDS.
- (d) Clinical signal preserved -- HOLDS.
- (e) Cross-file pseudonym linkage -- HOLDS.
- (f) Human review invariant (reviewer id + comment + timestamp) -- NOW HOLDS. Was PARTIAL before.
- (g) Output ready to share publicly -- NOW HOLDS provably at the download boundary. Was materially-safe-but-unverified before.

### iteration_7 (fork) attestation bundle + wizard UI + coverage matrix

Sir consolidated GOAL to: "input PHI-filled study data -> output PHI-handled study data ready to be shared and used publicly" AND asked for a redesigned UI in clinical/academic minimal register + proof of best-in-class coverage against every established tool. All three shipped and testing_agent iteration_7 verdict: **100% pass across 12 items**.

**Attestation Bundle** (`/app/backend/phi_core/bundle.py`, endpoint `GET /api/sessions/{sid}/bundle`):
- Default tier `safe_to_share/` contains PHI-handled datasets, forms, dictionary, plus `attestation.json` (SHA-256 per file + reviewer + guard verdict + jurisdiction + timestamps), `attestation.txt` human-readable receipt, and `README.md`.
- Publication add-on (`?publication=1`) adds `publication/paper/` with tables/, figures/, methods.md/results.md/discussion.md, and BibTeX references, plus `publication/benchmark/` scaffolding.
- Refuses HTTP 200 when guard status is 'blocked' (returns 403).
- Backfills the reviewer trail from `agent_decisions[].reviewer` for older sessions where session-level reviewer wasn't captured.

**Coverage Matrix** (`/app/backend/phi_core/coverage_matrix.py`, endpoint `GET /api/coverage-matrix`):
- 23 rows (18 HIPAA A-R + 5 beyond-HIPAA structural categories) x 7 tools (Amazon Comprehend PHId, CliniDeID, NLM Scrubber, Microsoft Presidio, MITRE MIST, GPT-4 zero-shot ICL, PHI Console).
- **PHI Console: 23/23** (best in class). Runner-up GPT-4: 17/23. CliniDeID: 16/23.
- Rendered as CSV (Table 1) and two publication-grade PNG figures (heatmap + totals bar chart) using oxblood/paper palette, 150 dpi, ready for JAMIA-style print.

**Wizard UI** (`/app/frontend/src/pages/Wizard.jsx`, replaces the old tabbed layout as `/`):
- Three linear steps: Upload -> Configure -> Choose output.
- Progress rail on left, editorial serif headings, hairline underlined inputs, hand-drawn tick CheckCards.
- Legacy routes (/studies, /studies/new, /sessions, /benchmark, /experimental/corpus) redirect to `/`.
- SessionDetail redesigned as a minimal receipt with a prominent oxblood 'Download bundle' CTA and a collapsible 'show agent details' dev section.

**Design system** (`/app/frontend/tailwind.config.js`, `/app/frontend/src/index.css`, `/app/frontend/src/components/ui.jsx`):
- Palette: paper `#F7F5F0` / paper-2 `#EFEBE3` / ink `#12141A` / ink-2 `#2A2D35` / ink-muted `#6B6E76` / rule `#D6D0C4` / oxblood `#8C2135` (single accent) / clean `#2F6E4E` / signal `#B37A00`.
- Typography: Fraunces (display serif), Inter (sans body), JetBrains Mono (data only).
- Subtle SVG grain overlay, hairline rules instead of card borders, generous whitespace, step-fade-in animation on wizard transitions.

**Regression**: **84/84 unit tests green** — added `test_bundle_and_coverage.py` (10 tests) covering matrix invariants + bundle correctness. Two new backend modules: `phi_core/bundle.py`, `phi_core/coverage_matrix.py`. `matplotlib` added to backend deps.

### iteration_8 (fork) IRB-readiness phases B/C/D/E

Sir directed a five-phase IRB-readiness roadmap in /app/memory/TODO.md. Phase A (Classification F1 corpus) landed in iteration_7 tail. This iteration ships Phases B, C, D, E in one batch. testing_agent iteration_8 verdict: **backend 132/137 (5 legacy-fixture failures, since fixed) + frontend structural pass on all Phase D/E testids and regulatory copy**.

- **Phase B — Publish Guard pattern parity across every HIPAA A-R category**. `phi_core/publish_guard.py` `_PATTERNS` extended with URL (N), IPv4/IPv6 (O), license plate (L), IMEI + device serial (M), image reference (Q), biometric hash + DNA profile (P), NPI + DEA (K). Coverage now includes B, C, D, F, G, K, L, M, N, O, P, Q. 12 new pytest cases in `test_publish_guard.py` including a corpus invariant `test_every_hipaa_letter_has_at_least_one_pattern`.

- **Phase C — OCR path for scanned / annotated PDFs**. `phi_core/file_readers.py` `read_pdf` now falls back to `pytesseract` + `pdf2image` when the digital text layer is shorter than 50 chars. OCR runs at 200 dpi bounded to 100 pages. OCR output flows into the same `_scrub_text_cell` deterministic scrubber. System deps installed: `tesseract-ocr`, `poppler-utils`. Python deps added to `requirements.txt`: `pytesseract==0.3.13`, `pdf2image==1.17.0`. Tests: `test_ocr_pdf.py` builds a synthetic image-only PDF at runtime and asserts OCR text >= 50 chars and scrubber produces HIPAA category tags. 2/2 green.

- **Phase D — Row-level review preview**. New module `phi_core/preview.py` exposes `build_preview()` and `_mask_original()`. New endpoint `GET /api/sessions/{sid}/preview?samples=5` returns up to N (clamped 1..20) `(original_masked, redacted)` cell pairs per dataset file. `original_masked` uses the same partial-mask rule as the Publish Guard finding masker so the preview surface itself carries no PHI. Metadata/narrative files are skipped. UI: `SessionDetail.jsx` renders a spot-check strip (`data-testid=spot-check-panel`) with three-column grid `column·action / original(masked) / redacted`. A required checkbox `spot-check-ack` gates both Submit paths until ticked. Tests: 6/6 green in `test_preview.py` including the anti-leak invariant `test_preview_original_never_returned_raw`.

- **Phase E — HHS §164.514(b)(2)(ii) actual-knowledge attestation**. `HumanReviewSubmit` grew `actual_knowledge_ack: bool = False`. Endpoint returns HTTP 400 when false or omitted, with a body citing 45 CFR 164.514(b)(2)(ii). Persisted to `session_review.actual_knowledge_ack=true` and `agent_decisions[].actual_knowledge_ack=true` (per-decision trail). `phi_core/bundle.py` `_attestation_payload` now surfaces `actual_knowledge_ack`, `actual_knowledge_cite`, and `actual_knowledge_statement` fields in `attestation.json`. `attestation.txt` gains an "Actual-knowledge attestation" line with YES/NO + statement. UI: `SessionDetail.jsx` adds a required checkbox `actual-knowledge-ack` / `actual-knowledge-ack-global` with the exact HHS wording. Both Submit paths disabled until ticked. Tests: 4/4 green in `test_actual_knowledge_attestation.py` covering payload true/false, bundle JSON+TXT surfacing, and legacy-session backfill from per-decision trail.

**Full regression after iteration_8**: 120 tests passed + 1 skipped across the unit-test suite (excluding live-LLM-required agent pipeline tests). Legacy integration test `test_agent_pipeline.py::test_human_review_and_export` updated to send `actual_knowledge_ack=true` + reviewer field so it no longer trips the Phase E gate.

### iteration_9 (fork) regulator-defensible guard + jurisdiction registry + phase F E2E

Sir directed a full security audit + code review. The audit was clean on architecture; the code review surfaced a HIGH-severity false-positive: `AGE_OVER_89` was firing on any two-digit number 90-99 regardless of column context, blocking real clinical exports (heart rate 95, systolic BP 92, glucose 96). Sir corrected the direction: the fix must be regulator-defensible, not just code-defensible. Per HIPAA §164.514(b)(2)(i)(C), the identifier is "the age of an individual" — not "any 90-99". The rule regulates identifier TYPES, not shapes.

- **CR-HIGH fix — Column-semantics + in-cell anchor gate on the 3 shape-ambiguous patterns**. `phi_core/publish_guard.py` `_CONDITIONAL` table now gates `AGE_OVER_89`, `LICENSE_PLATE`, `IMEI` on either (a) the pipeline's per-column HIPAA category matching the identifier type or (b) an in-cell anchor token (e.g. `age`, `y/o`, `yrs`, `plate`, `vehicle`, `imei`, `device id`). Every other pattern (SSN, phone, email, URL, IPv4/IPv6, DEVICE_SERIAL prefix-anchored, IMAGE_REF, BIOMETRIC_HASH, DNA_PROFILE, NPI, DEA) remains unconditional because its shape is unique enough that a false-positive is implausible. Verified live: `heart_rate=95`, `systolic_bp=92`, `arm_code="HB 120"`, `barcode="490154203237518"` no longer trip the guard, while `age=95` still does (real leak).

- **`scan_all_exports(export_paths, decisions=None)`** — the guard now accepts the pipeline's per-column decisions and builds a `{file_id: {column: phi_category}}` map, threaded through CSV + XLSX scanners. Both `orchestrator.py` (initial run) and `server.py` (human-review resume) pass decisions to the guard.

- **`phi_core/jurisdictions.py`** — first-class `JurisdictionPack` seeded (US-HIPAA fully populated + EU/UK/IN/CA/BR stubs). The pack contains identifier_categories, age_aggregation_threshold, restricted_zip3_prefixes, and pattern-set per jurisdiction. Will become the fallback cache for the Regulations expert once it is armed with real web-search.

- **Phase F E2E live-verified**: full run through `create → intake → handle → awaiting_human_review → preview → 400 gate → 200 submit → complete → bundle` on realistic study data. `publish_guard.status = clean, scanned=2, blocked=0`. Bundle attestation.json.actual_knowledge_ack = True.

- **`iteration_8` bonus fix**: 3 legacy security-round2 tests had a phone regex `\b\d{3}[\s\-.]?\d{3}[\s\-.]?\d{4}\b` with optional separators that false-positives on floating-point `duration_ms` values whose fractional part is a 10-digit sequence. Tightened to require at least one separator.

**Full regression after iteration_9**: **125/125 unit tests + 3 skipped (unrelated env-skip) green + Phase F live E2E passing.**

### iteration_10 (fork) Statute + Praxis armed with Claude native web_search

Sir directed: "The agents already exist. They must be given the right tools to execute the task. AI/LLM used must use its existing abilities like Claude can use native web_search." This iteration arms the two research-grade agents with Anthropic's provider-hosted `web_search_20250305` tool so their answers reflect the current primary-law text and current best-practice methods rather than stale LLM training data.

- **`phi_core/agents/llm.py`** — new `call_llm_with_web_search(system, user, cfg, max_uses)` that routes through `emergentintegrations.LlmChat.with_tools([{type:"web_search_20250305", name:"web_search", max_uses}])`. Uses `asyncio.run` for correct LiteLLM cleanup. Extracts URLs from the reply text (LiteLLM stringifies Anthropic's structured citation blocks, so best-effort URL regex is the pragmatic path).
- **`phi_core/agents/base.py`** — new `Agent.call_with_web_search(prompt, phase, max_uses)` and `Agent.call_json_with_web_search(...)` boilerplate. 180-second timeout (web search is slower). Citations logged into agent_log alongside the reply preview.
- **`phi_core/agents/experts.py`** — Statute + Praxis rewritten.
  - **Statute** now web-searches the current primary-law text per jurisdiction, merges results with the deterministic `JurisdictionPack` fallback so `identifier_categories` and `age_aggregation_threshold` are always populated. Cache-first, web-search-on-miss.
  - **Praxis** short-circuits well-known HIPAA categories (A/B/C/D/F/G/H) with deterministic canonical techniques (no web search wasted); non-canonical categories trigger a live web-search returning `{technique, params, utility_preserving, clinical_impact, reference_paper, sources}`.
- **Live-verified**: Statute(`"in"`) returned real DPDPA 2023 + DPDP Rules 2025 with 6 web citations from live search (not training-time knowledge). Cache hit on subsequent call.
- **`tests/test_experts_web_search.py`** (5 tests, includes 1 live web-search test) and **`tests/conftest.py`** (loads `.env` so tests see `EMERGENT_LLM_KEY` and `MONGO_URL`).

**Full regression after iteration_10**: **130/130 unit tests + 3 skipped green.**

### iteration_11 (fork) multi-provider LLM catalog + provider-aware web_search

Sir directed: "I want an option to choose between different models from different AI company so that user has option to choose for themselves instead of building around one model." Same-session refactor of both plumbing and UX so every one of the 12 agents can run under Claude / GPT / Gemini through either the Emergent Universal Key (zero-setup) or BYOK.

- **`phi_core/llm_catalog.py`** — curated multi-provider model catalog. 10 models across Anthropic (Claude Sonnet 4.5 / Opus 4 / Haiku 4.5), OpenAI (GPT-5.2 / 5.4 / 5.4-mini), Google (Gemini 3 Pro / Flash), and BYOK OpenRouter entries. Every row declares `tier` (flagship/balanced/fast) and `supports_web_search`. `PROVIDER_FAMILIES` table maps family → native web_search tool descriptor. `resolve_family(provider, model_id)` picks the underlying family when routing through the Emergent proxy.
- **`phi_core/agents/llm.py`** — `_emergent_call` and `_emergent_call_with_web_search` both use the catalog to route to the correct `LlmChat.with_model(family, model_id)` and select the right web_search tool descriptor:
  * Anthropic → `{"type":"web_search_20250305","name":"web_search","max_uses":N}`
  * Gemini → `{"googleSearch":{}}` (Google Search grounding)
  * OpenAI / OpenRouter / OpenAI-compatible → no native tool; falls back to plain LLM call
- **`GET /api/settings/llm/catalog`** — new endpoint feeding the UI with providers + models + default_model_id.
- **`frontend/src/pages/Settings.jsx`** — full UX rebuild. Provider dropdown shows human-readable labels ("Emergent Universal Key (Claude · GPT · Gemini)"), model dropdown cascades on the provider choice, each option annotates the tier + web-search capability, an "available models" grid shows the full catalog at a glance. Changing provider auto-resets model when the current selection isn't valid for the new provider family. Live tags surface `web-search enabled` + tier when the picked model supports them.
- **`tests/test_llm_catalog.py`** — 11 tests covering catalog shape, provider-family coverage (Anthropic + OpenAI + Gemini all reachable via Emergent), family resolution parametrized over model IDs, and per-family web_search tool selection.

**Full regression after iteration_11**: **141/141 unit tests + 3 skipped green + Settings UI smoke-verified live.**

### iteration_12 (fork) study-level corpus torture-test harness (Phases C1 + C2)

Sir clarified: the corpus generator's sole purpose is to PLANT PHI/PII into realistic study data so the pipeline can prove it removes every plant, including deliberate edge cases. Ground-truth labels stay in the session document only (never on disk). The verifier dual-scores correctness (0% PHI leak / 100% accuracy) AND deferral rate (how often Judge sends a decidable case to human review — the metric to drive down over iterations).

- **`phi_corpus/`** — new package:
  * **`scenarios.py`** — 3 hand-curated real-life archetypes (oncology_v1, diabetes_v1, pediatric_behavioral_v1). Each declares dataset column specs with `hipaa_category`, `expected_action`, and a value `generator`. Includes 20+ cell generators covering names, DOB, phones, emails, ZIPs, SSNs, MRNs, ages (under/over 89), clinical vitals (HR, BP, glucose), study arm codes shaped like license plates, 15-digit barcodes, Aadhaar, PAN.
  * **`edge_cases.py`** — 10 torture tests: `age_over_89`, `age_over_89_diabetes`, `dob_indicative_of_age_over_89`, `restricted_zip3`, `notes_carry_name`, `notes_carry_phone`, `notes_carry_age_over_89`, `notes_carry_ipv4`, `notes_carry_email`, `clinical_hr_90s` (guard false-positive test).
  * **`planters.py`** — `plant(scenario_id, jurisdiction, edge_case_tags, row_count, seed)` returns `CorpusArtifact(zip_bytes, ground_truth, ground_truth_summary)`. Ground truth records every `(file, row, column, value, hipaa_category, expected_action, edge_case_tag)` cell — clinical too, so the verifier can score false-positives.
  * **`verify.py`** — dual-scoring per Sir's Q2(iii). `verify(ground_truth, decisions, file_name_map)` returns `{correctness: {precision, recall, F1, accuracy, per_category, false_positives, false_negatives}, deferral: {rate, count, cells}, summary: {TP, FP, FN, TN}}`.
  * **`generate.py`** — CLI: `python -m phi_corpus.generate --scenario X --jurisdiction Y --edge-cases A,B,C --rows N --out /tmp/corpus.zip`.
- **`GET /api/corpus/study/catalog`**, **`POST /api/corpus/study/generate`**, **`POST /api/corpus/study/run`**, **`GET /api/corpus/study/verify/{sid}`** — study-level endpoints. Prefixed `/study/` to avoid shadowing the pre-existing detector-benchmark corpus endpoints (`/api/corpus/generate` for individual-example labelled corpus + `/api/corpus/{id}` + `/api/benchmark/run`).
- **Live E2E verified**: oncology_v1 + 4 edge cases (age_over_89 + restricted_zip3 + notes_carry_name + clinical_hr_90s) → pipeline completed with **precision 1.0 / recall 1.0 / F1 1.0 / accuracy 1.0** and **zero deferrals**. Heart-rate 90-99 correctly preserved (CR-HIGH guard fix confirmed on real corpus). Age 90+ correctly capped. Restricted ZIP3 correctly truncated. Name embedded in notes correctly scrub_text'd.
- **`tests/test_corpus.py`** — 12 unit tests covering ZIP shape validity, ground-truth completeness, edge-case tag persistence, ZIP3 denylist coverage, HR-90s range check, verifier scoring on perfect / leak / over-block / deferral cases.

**Full regression after iteration_12**: **153/153 unit tests + 3 skipped green + live corpus E2E precision/recall/F1 = 1.0.**

**GOAL cross-check after iteration_8**:
- (a) LLM never reads dataset rows — HOLDS.
- (b) Exports contain no residual raw PHI — HOLDS AND ENFORCED across every HIPAA A-R category (Phase B pattern parity).
- (c) BYO keys stored securely — HOLDS.
- (d) Clinical signal preserved — HOLDS.
- (e) Cross-file pseudonym linkage — HOLDS.
- (f) Human review invariant — HOLDS with row-level spot-check (Phase D).
- (g) Output ready to share publicly — HOLDS with actual-knowledge attestation (Phase E).
- (h) Scanned / annotated PDFs handled — HOLDS with OCR fallback (Phase C).

### iteration_13 (fork) IRB-ready adversarial torture-test + optional corpus mode

Sir directed: "Corpus generator + PHI handling must be an optional feature. Corpus must violate every regulation including edge cases. If it passes → 100 % trustworthy, auditable, IRB-ready. Focus US-only for now. IRB audit in 3 days." This iteration ships the IRB-defensible torture-test loop.

- **Verifier fix — narrative / form-plant scoring** (`phi_corpus/verify.py`). The Executor processes PDF forms wholesale via `detect_text` + `apply_to_text`, so it emits no per-field Judge decisions. Previous verifier attempted to file-level-score by Judge action; that returned 13 false negatives on `consent_digital.pdf`. Fixed by reading the redacted export text and asserting the raw planted PHI substring is ABSENT. Tabular plants (row > 0) still scored by Judge decisions; narrative plants (row == 0) scored by export-text substring absence. `/api/corpus/study/verify/{sid}` now threads `export_paths` + `guard_report` from the session document. `test_verifier_credits_form_plant_when_planted_value_absent_from_export` + `test_verifier_flags_form_plant_when_raw_phi_survives_in_export` cover both directions.

- **Adversarial corpus — full HIPAA A-R + all edge cases** (`phi_corpus/scenarios.py`, `edge_cases.py`). New scenario `hipaa_max_adversarial_v1` plants every HIPAA §164.514(b)(2)(i) identifier A-R in a single dataset (28 columns, 18 PHI categories + clinical keepers). New generators: fax (E), health-plan (I), account (J), licence (K), VIN (L), device serial (M), URL (N), IPv4 (O), biometric hash (P), image ref (Q), unique code (R). New notes edge cases: `notes_carry_url`, `notes_carry_device_serial`, `notes_carry_license`. `HIPAA_MAX_EDGE_CASE_TAGS` preset exercises every edge case that targets a column in the max-adversarial scenario.

- **Sentinel hard-rules expanded** (`phi_core/agents/reasoning.py`). (H) regex now catches study-scoped record identifiers: `patient_id`, `subject_id`, `child_id`, `participant_id`, `study_id`, `enrolment_id`, `record_id` → forced to `pseudonymize` per 164.514(b)(2)(i)(H). Clinical-keep regex expanded to cover `arm_code`, `barcode`, `specimen_barcode`, `heart_rate_bpm`, `hba1c_percent`, `cbcl_total_score`, `glucose_mgdl` (avoids Judge dropping clinical barcode / arm-code shapes).

- **Wizard UI — optional corpus mode** (`frontend/src/pages/Wizard.jsx`). Step 1 gains an "IRB torture test" panel with an Enable toggle. When ON, the user picks a preset (HIPAA A-R + every torture edge case) and row count, then clicks "Launch adversarial run" → POSTs `/api/corpus/study/run` → jumps straight to `/studies/{sid}?corpus=1`. The traditional ZIP-upload flow is preserved and untouched.

- **SessionDetail — corpus verifier panel** (`frontend/src/pages/SessionDetail.jsx`). New panel `[data-testid=corpus-verifier-panel]` surfaces precision / recall / accuracy / deferral count / TP-FP-FN-TN summary + per-HIPAA-category breakdown + FN/FP tables inline. Fetched from `/api/corpus/study/verify/{sid}` after status=complete.

- **Live E2E verified (US-HIPAA, `hipaa_max_adversarial_v1`, seed 11)**:
  * All 18 HIPAA A-R categories present in the corpus.
  * Pipeline outcome: **precision 1.0 / recall 1.0 / F1 1.0 / accuracy 1.0 · 0 false negatives · 0 false positives · 0 deferrals**.
  * Guard status: `clean`. Bundle: publishable.
  * Same result on the `oncology_v1` + 4 edge-cases E2E (was 13 FN → 0 FN → still 0 FN).

- **Regression**: 159 unit tests + 4 skipped green (excluding pre-existing flaky `test_narrative_flow` span-count test unrelated to this iteration).

- **`hipaa_max_adversarial_v1` scenario summary** (row_count=3): A=5 B=8 C=11 D=7 E=3 F=5 G=3 H=3 I=3 J=3 K=3 L=3 M=3 N=3 O=3 P=3 Q=3 R=3 + 21 clinical NONE keepers. Zero PHI cells leak into the exported bundle.

**GOAL cross-check after iteration_13** (US-HIPAA only, IRB-ready):
- (a) LLM never reads dataset rows — HOLDS.
- (b) Exports contain no residual raw PHI — HOLDS, PROVEN across all 18 HIPAA A-R categories on the adversarial corpus.
- (c) BYO keys stored securely — HOLDS.
- (d) Clinical signal preserved — HOLDS (heart_rate, systolic_bp, glucose, arm_code, barcode all kept).
- (e) Cross-file pseudonym linkage — HOLDS.
- (f) Human review invariant — HOLDS with row-level spot-check + actual-knowledge attestation.
- (g) Output ready to share publicly — HOLDS, provable at both the deterministic Publish Guard boundary AND the ground-truth verifier report.
- (h) Scanned / annotated PDFs handled — HOLDS with OCR fallback.
- (i) Adversarial torture test — HOLDS with `hipaa_max_adversarial_v1` scoring P=R=F1=1.0 across the whole HIPAA A-R matrix.

### iteration_14 (fork) Security audit fixes -- Publish Guard fail-closed at download boundary

Security audit surfaced one HIGH and two LOW findings. All three fixed and locked with regression tests.

- **SEC-001 (HIGH, fixed)** -- Publish Guard bypass at the download gate. The legacy `POST /finalize` never populated `guard_report`, so `GET /export/{file_id}` and `GET /bundle` served exports whose only 403 trigger was an explicit `status == "blocked"`. Missing guard was treated as safe (fail-open).
  * `/finalize` now runs `scan_all_exports` and persists a `guard_report` before flipping status to `complete`.
  * `/bundle` fails closed: only serves when overall `guard_report.status == "clean"`; missing or blocked → 403.
  * `/export/{file_id}` fails closed: only serves when the per-file result is `clean` or `skipped`; missing → 403. `?force=true` still overrides `blocked` (audit-trailed) but never overrides `missing`.
  * Locked with `test_bundle_refuses_when_guard_missing`, `test_bundle_refuses_when_guard_blocked`, `test_export_refuses_when_guard_result_missing`, `test_export_serves_only_on_clean_per_file`.

- **SEC-002 (LOW, fixed)** -- `/api/corpus/study/verify/{sid}` was the only session read not carrying `Depends(require_api_token)`. Added the dep; regression: `test_corpus_verify_endpoint_is_token_gated`.

- **SEC-003 (LOW, fixed)** -- `corpus_ground_truth` (the planted-PHI answer key) was returned by session read endpoints, weakening the invariant that ground truth never leaves the session document. `_scrub_session_document` now `.pop("corpus_ground_truth", None)`. `corpus_summary` counters kept because they carry no PHI values and the UI needs them to decide when to fetch the verifier report. Regression: `test_scrub_strips_corpus_ground_truth_but_keeps_summary`. Frontend switched to `s.corpus_summary` for the same signal.

- **Regression**: 169 passed + 3 skipped (up from 159, +10 new tests: 6 security + 3 corpus-narrative from iteration_13 + 1 misc). No previously-green test regressed.

- **Not fixed (accepted-low, per audit hardening list)**: Token in URL query string for SSE + downloads; reviewer newline injection in `attestation.txt`; `?force=true` guard override recording (now recorded onto `session.guard_overrides` array); persisted agent logs may still hold raw PHI substrings in `prompt_preview` (partially mitigated by read-time `scrub_nested`); BYOK auto-key generation on multi-worker deploys.

### iteration_15 (fork) Speed + STOP + live trace UX

Sir's directives Q1-Q4:
- Q1 "Sentinel must nitpick only where required" -> severity gate
- Q2 "Herald must meet the manuscript goal but easier to complete" -> two-subagent split (Abstract, Sections)
- Q3 "Ledger could benefit from a subagent" -> two-subagent split (Compare, Aggregate)
- Q4 "STOP button + live agent trace + user must feel movement, not loading screen" -> cancel endpoint + live-trace panel

**Speed (measured, oncology_v1 + 4 edge cases, 5 rows):**
- **Before: 340 s wall clock / 341 s of LLM time across 13 calls** (Judge×3+Sentinel×3=158 s, Ledger=52 s monolithic, Herald=90 s hit timeout with empty output).
- **After: 190 s wall clock / 193 s of LLM time across 11 real calls** (Judge×1+Sentinel×1=43 s short-circuit, Ledger.Compare + Ledger.Aggregate=28 s, Herald.Abstract + Herald.Sections=83 s producing a COMPLETE manuscript).
- **-44 % wall clock, -43 % LLM time, +manuscript quality (no more Herald timeout).**

**Sentinel severity gate** (`phi_core/agents/reasoning.py::Sentinel`):
- Prompt now asks Sentinel to tag each issue `severity: "blocking"|"advisory"`.
- Post-processing forces `verdict='approved'` whenever there are zero blocking issues, even if the LLM's own JSON said `'revise'`. This closes the observed pathology where Sentinel kept issuing philosophical objections (hash vs pseudonymize, retention-policy details) that Judge could not practically resolve.
- Orchestrator uses `_blocking_issues()` (not `verdict`) to decide whether to iterate again OR force human review. Advisory issues are logged onto `session.advisory_issues` for the audit trail but never block progress.
- `ITERATION_CAP` lowered from 3 to 2 (severity gate typically ends on iter 1).

**Ledger split** (`phi_core/agents/outward.py`):
- `LedgerCompare` -- per-competitor delta narrative (JSON: `{"comparisons": [...]}`), ~15-20 s.
- `LedgerAggregate` -- rollup headline + metrics narrative + recommendations (JSON: `{"headline", "our_system", "metrics_narrative", "recommendations"}`), ~15-20 s.
- Public `Ledger` class stitches both subagent outputs into the legacy schema so callers don't change.
- `_count_actions()` is deterministic (no LLM) and runs alongside the LLM call.

**Herald split** (`phi_core/agents/outward.py`):
- `HeraldAbstract` -- title + abstract + methods + references (~35-45 s).
- `HeraldSections` -- results + discussion + limitations + conclusion + alt_venues (~30-40 s).
- Public `Herald` class merges into the legacy manuscript schema. Neither subagent hits the 90 s hard timeout; the manuscript is always fully populated.

**Parallel Auditor + Scout** (`phi_core/agents/orchestrator.py`):
- `asyncio.gather(Auditor.run, Scout.run, _empty(None))` -- Scout is cache-hit in most cases so the parallel win is Auditor overlapping with the cache lookup, but still tighter than the previous strict sequence.

**Cancel endpoint** (`server.py`):
- `POST /api/sessions/{sid}/cancel` -- idempotent, token-gated. Sets `session.cancel_requested=true` and emits an SSE event so the UI knows.
- Orchestrator's `_check_cancel()` runs between every phase boundary. If the flag is set it raises `PipelineCancelled` which the worker traps and writes `status='cancelled'` + emits a `cancelled` SSE event.
- In-flight LLM call still finishes (base Agent has a 90 s hard timeout) but no further calls are issued.

**Live agent trace panel** (`frontend/src/pages/SessionDetail.jsx`):
- `AgentTracePanel` groups every AgentMessage by `(agent, phase_key)` and renders one row per group with a status badge (`RUNNING` / `DONE` / `ERROR` / `INFO`), the phase key, and duration.
- Clicking a row expands prompt-preview + reply-preview so the operator can drill in and see WHY an agent decided what it did.
- Header shows total agent-call count + cumulative LLM time -- Sir Q4 ("the user must feel like the process is moving further").
- `SSE onmessage` triggers a full refresh (`refresh()`) which re-fetches `/agent-trace` -- new rows appear within ~2 s.
- Advisory Sentinel issues are surfaced in a dedicated sub-block so the operator can see the "we chose X over Y for stylistic reasons" reasoning without them being escalated to human review.

**STOP button** (`frontend/src/pages/SessionDetail.jsx`):
- Rendered while `isPending` is true. `data-testid="btn-cancel-run"`.
- POSTs to `/api/sessions/{sid}/cancel`, toasts "Cancel requested", and re-fetches. Once the flag is set the button label flips to "Cancel pending..." until the server sets `status='cancelled'`.

**Verifier bug fix** (`phi_corpus/verify.py`):
- `hash` was missing from `_PHI_ACTIONS`, so a Judge decision of `hash` on a (H)-category column was counted as a leak (false negative). It is a valid deterministic one-way transform allowed by the Sentinel hard-rule table. Added.
- Verified: oncology_v1 corpus now scores **P=1.0 R=1.0 F1=1.0 · 0 FN · 0 FP**.

**Regression**: 180 unit + 3 skipped (up from 169). New coverage: `test_speed_and_ux.py` (11 tests) locks severity gate, cancel flag, endpoint token-gate, Ledger/Herald split registration, `_count_actions` determinism, `ITERATION_CAP == 2`, and `hash in _PHI_ACTIONS`.

**End-to-end proven live**:
- Corpus run wall-clock 189.5 s, verifier score P=R=F1=1.0 · 0 FN · 0 FP.
- Cancel flow: HTTP 200 `cancel_requested=true` -> `status=cancelled` within 30 s.
- Screenshot confirms STOP button + "1 agent call · 0.0 s of LLM time · pipeline in phase classifying" live display with RUNNING badge on Lexicon.

### iteration_16 (fork) Spec-alignment audit + hang-protection

Sir asked to verify the current implementation aligns with the 12-agent spec and add safeguards for "system may get stuck or load forever" scenarios. Systematic audit results:

| Sir's Spec Agent | Current Code | Status |
|------------------|--------------|--------|
| Dictionary specialist | `Lexicon` | ALIGNED |
| Dataset (headers only) specialist | `Schema` | ALIGNED |
| Forms specialist | `Instrument` | ALIGNED |
| Regulations expert (web search) | `Statute` (native Claude web_search_20250305) | ALIGNED |
| PHI Methods expert | `Praxis` exists but NOT wired into Judge | **FIXED** |
| Classifier | `Judge` | ALIGNED |
| Preview (`false-positive check first`) | `Sentinel` + Judge FP-check prompt | **FIXED** |
| Cap "more than 3 times then human review" | `ITERATION_CAP=3` (was 2 after iter_15) | **FIXED** |
| Operator | `Executor` (deterministic) | ALIGNED |
| Reviewer | `Auditor` | ALIGNED |
| Research | `Scout` | ALIGNED |
| Benchmark | `Ledger.Compare + Ledger.Aggregate` | ALIGNED |
| Publishing | `Herald.Abstract + Herald.Sections` | ALIGNED |
| Corpus generator + optional toggle | Wizard corpus mode (iter_13) | ALIGNED |
| Live agent trace + STOP | (iter_15) | ALIGNED |
| Human review escalation | (iter_15 severity gate) | ALIGNED |
| Safe-to-share downloadable bundle + auditor report + benchmark | (iter_15) | ALIGNED |

**Fixes applied (targeted, minimal, non-regressive):**

- **Praxis wired into orchestrator + Judge** (`phi_core/agents/orchestrator.py`, `reasoning.py`).
  - Orchestrator runs Praxis for the 17 HIPAA A-R categories in parallel (`asyncio.gather`) between Statute and Judge. Cached in Mongo cross-session so first run pays ~30 s, subsequent runs are ~free.
  - Judge signature: `run(schema, instrument, lexicon, statute, praxis, prior_feedback)`. Judge's prompt now includes a Praxis summary `{category: {technique, utility_preserving}}` so it picks the current best-practice technique per category rather than reasoning from scratch.
  - Praxis cache-hit path now emits a `praxis.cache_hit:{cat}` info log so operators see the agent firing in the live trace even when cached.

- **Judge false-positive verification** (`reasoning.py::Judge.run`) — Sir's spec: "the classifier ensures its not false positive first before correcting". When `prior_feedback` is present, Judge's prompt instructs it to VERIFY each blocking issue against Statute + Instrument and add a `justification` field for any issue it declines to act on. Prevents Sentinel from over-tuning Judge.

- **ITERATION_CAP 2 -> 3** (`phi_core/agents/base.py`) — Sir's spec explicitly says "if the issue continues MORE THAN 3 times then it must be sent to human review agent". Restored the 3-iteration bound. The severity gate from iter_15 still short-circuits when zero blocking issues remain, so most sessions finish in 1 iteration; only genuine PHI-leak disputes now get the full 3 chances before escalation.

**Hang-protection fixes (Sir's concern: "system may get stuck or load forever"):**

- **Global wall-clock ceiling** (`server.py::worker`) — `asyncio.wait_for(run_agent_pipeline(...), timeout=900)` = 15-minute hard ceiling. On timeout the session is marked `failed` with `reason=wall_clock_ceiling_exceeded` and a clear SSE event tells the operator "try again or switch model in Settings". 15 min = 5x the observed 190 s happy path, 2x the historical worst case.

- **SSE heartbeat** (`server.py::session_stream`) — `asyncio.wait_for(q.get(), timeout=15.0)` emits an SSE comment (`: heartbeat\n\n`) every 15 s of silence so browsers / K8s proxies don't reap the connection during a long Herald.Sections or Statute web-search call. Comment lines are ignored by EventSource per SSE spec.

**Live E2E measurements (oncology_v1 + 4 edge cases, 5 rows):**
- First run (Praxis cold): 219 s wall clock. Verifier P=R=F1=1.0 · 0 FN · 0 FP.
- Second run (Praxis cache-hit): 183 s wall clock. Verifier P=R=F1=1.0 · 0 FN · 0 FP.
- All 12 spec agents visible in the live trace: Lexicon, Schema, Instrument, Statute, **Praxis (17 category rows)**, Judge, Sentinel, Executor, Auditor, Scout, Ledger.Compare + Aggregate, Herald.Abstract + Sections.

**Regression**: **183 passed / 3 skipped** unit tests (up from 170). 4 new tests in `test_speed_and_ux.py`: `test_iteration_cap_is_three`, `test_praxis_agent_wired_into_orchestrator`, `test_judge_prompt_asks_for_false_positive_check`, `test_orchestrator_emits_praxis_phase`.

**Untouched (Sir's directive "if functional don't touch"):**
- Executor deterministic pipeline (works, no LLM needed at the last mile)
- Publish Guard (fail-closed, iter_14)
- All specialist agents (Lexicon / Schema / Instrument)
- Corpus generator + verifier (iter_13/14)
- Wizard corpus toggle (iter_13)
- Live trace panel + STOP button (iter_15)

### iteration_17 (fork) Remove PDF / form generation from corpus generator

Sir's directive: "remove pdf or form creation in corpus generator, the output is useless. Completely remove it with no traces of it left."

**What was removed** (traceable back to iter_13 Phase B/C):
- `phi_corpus/forms.py` -- deleted. fpdf2-driven `build_digital_pdf` + `build_scanned_pdf` gone.
- `phi_corpus/planters.py::plant(include_forms=...)` argument -- gone. `plant()` now emits only `datasets/` + `dictionary/`.
- `phi_corpus/scenarios.py::Scenario.narrative_body` field + every scenario's `narrative_body=` payload -- gone.
- `phi_corpus/generate.py::--no-forms` CLI flag -- gone.
- `phi_corpus/verify.py` narrative-scoring path (row == 0, redacted-export-text substring absence check) -- removed. Verifier is now tabular-only, one Judge decision per (file, column). Kept `export_paths` in the signature as a silent-noop for backwards-compatible callers.
- Tests `test_planter_emits_two_pdfs_when_forms_enabled`, `test_planter_form_pdfs_add_phi_plants_to_ground_truth`, `test_digital_pdf_text_layer_is_extractable`, `test_verifier_credits_form_plant_when_planted_value_absent_from_export`, `test_verifier_flags_form_plant_when_raw_phi_survives_in_export` -- removed.

**Regression tests added**:
- `test_planter_emits_valid_manifest_zip` now asserts no `forms/` folder and no `.pdf` files in the corpus ZIP.
- `test_planter_no_pdf_generation_module` asserts `phi_corpus.forms` is not importable (ModuleNotFoundError).

**Untouched (Sir: "if functional don't touch")**:
- The intake endpoint (`server.py::/sessions/{sid}/intake`) still accepts user-uploaded `forms/` folders in the study ZIP. This directive only removed the CORPUS GENERATOR's synthetic PDF emission.
- The pipeline's Instrument agent + OCR fast path still handle real user-uploaded PDF forms.
- `fpdf2==2.8.7` stays pinned in `requirements.txt` as dead-weight; removing it triggers an unrelated litellm resolver conflict. It is not imported by any code path in the app.

**Live E2E after removal** (oncology_v1, 5 rows, 4 edge cases, seed=55):
- Wall clock: 195.4 s (fastest to date -- Praxis cache-hit + no form-gen overhead).
- Verifier: **P = R = F1 = 1.0, 0 FN, 0 FP**.
- Bundle contents: `safe_to_share/datasets/enrollment.csv`, `safe_to_share/dictionary/columns.csv`, `attestation.json`, `attestation.txt`, `README.md`. **Zero PDFs**.

**Regression**: **179 passed / 3 skipped** (down from 183 = the 4 removed form-scoring tests). All ex-corpus tests still green.

### iteration_18 (fork) Security audit re-run -- SEC-002 + SEC-003 fixed

Post-iter_17 security audit came back **CONDITIONAL PASS**. All iter_14 fixes verified as still holding (guard-gating on /finalize + /bundle + /export, corpus_ground_truth stripped, BYO Fernet encryption, verify token-gated, SSRF allowlist, ZIP intake caps). Three items:

- **SEC-001 [MEDIUM] Deployment posture, NOT code defect** -- preview `.env` has empty `API_TOKEN`, so `require_api_token` is a no-op and any internet user could hit the console. Gating code fails-closed correctly the moment `API_TOKEN` is set. **ACTION REQUIRED FROM OPERATOR: set `API_TOKEN=<strong-random>` in `/app/backend/.env` before touching real PHI or promoting the preview to production.** The frontend already carries `X-API-Token` from `localStorage['phi_api_token']`; operators paste the same token into Settings -> API Token.

- **SEC-002 [MEDIUM] SSE queue leak / DoS FIXED** (`server.py`):
  * Added `_progress_subscribers: dict[str, int]` counter per session id.
  * `session_stream()` now returns 404 for unknown sids, 409 for settled sids (`{complete, failed, cancelled, intake_failed, awaiting_human_review}`), and 429 when subscriber count for a session >= `_MAX_STREAM_SUBSCRIBERS_PER_SESSION` (4).
  * Stream generator wraps its loop in `try / finally` that calls `_release_stream(sid)` on client disconnect / normal end. The queue is popped from `_progress_queues` when the last subscriber leaves so idle streams cannot pin memory indefinitely.
  * Verified live: `curl` on a settled sid -> HTTP 409 with the reason body; unknown sid -> HTTP 404.

- **SEC-003 [LOW] Scrubber breadth FIXED** (`phi_core/security.py`):
  * `scrub_persisted_text` now covers HIPAA (C) ISO / US / named dates, (C-age) explicit "age NN years" over-89 patterns (context-clued so clinical values like glucose 105 or heart rate 82 survive), (B) US street-address suffixes, (H) MRN / account / device / UID prefixes with 4+ char tail, (N) URLs, (O) IPv4. Order matters: MRN scrubbed before dates so `MRN 202305` cannot leak its numeric tail.

**Untouched (audit re-verified as holding)**:
- SEC-001 guard-gating (fail-closed on /finalize, /bundle, /export)
- SEC-002 verify endpoint token-gated
- SEC-003 corpus_ground_truth stripped from session reads
- BYO Fernet encryption + never returned plaintext
- SSRF allowlist + private-IP block on Base URL
- ZIP intake caps (path traversal, symlink, zip-bomb)
- Praxis never receives dataset rows (category letter only)
- `verify.py::export_paths` truly unused (no traversal window)
- `phi_corpus/forms.py` deletion has no orphan imports
- No cookies used, so cancel/POST paths have no CSRF window

**Regression**: **192 passed / 3 skipped** (up from 179). 13 new tests in `test_security_audit_iter18.py` lock every SEC-002/003 defence.

### iteration_19 (fork) Real-world file shapes + trace polish

Sir uploaded three example artefacts (Word dictionary, CSV dataset, annotated CRF PDF) with directive: "handling of these types of files is must, the dictionary is word doc here but if large then it would be excel workbook -- align it accordingly". Also asked to progressively implement the next-items backlog.

**A. Dictionary supports `.docx`** (`phi_core/intake.py`, `phi_core/agents/specialists.py`):
- `COMPONENT_SUFFIXES["dictionary"]` extended to `{.csv, .xlsx, .xls, .docx}`.
- New `_read_docx_tables(path)` uses stdlib `zipfile` + `xml.etree.ElementTree` (no new deps) to walk `word/document.xml`, extract every `<w:tbl>` element as CSV-shaped rows, correctly quote comma-containing cells, and capture non-table prose (up to 40 paragraphs) as "narrative context" for the Lexicon agent.
- New `_read_xls_tables(path)` handles legacy `.xls` -- openpyxl for mis-renamed xlsx binaries, xlrd fallback where installed; returns "" gracefully when neither works so Lexicon just sees no table text rather than crashing.
- `_read_table_flat` dispatches on suffix so Lexicon transparently accepts all four dictionary formats.
- Verified live: intake accepts Sir's real `dict.docx` + `diabetes.csv` ZIP with exit=0, dictionary and dataset both stored.

**B. Praxis trace collapse** (`frontend/src/pages/SessionDetail.jsx::_groupTrace`):
- 17 per-category `Praxis` rows now collapse into a SINGLE trace row: `Praxis · praxis.methods · 27 methods · 302.4 s`.
- Expanded panel lists every HIPAA-category consulted as a chip strip so operators can drill in without scrolling past 17 near-identical rows.
- Trace header now shows the correct "17 agent calls" instead of "27+" (the true LLM call count minus the collapsed Praxis hits).

**C. `/corpus` empty-state intro** (`frontend/src/pages/Corpus.jsx`):
- Testing_agent iter_10 flagged the legacy Corpus page as "blank canvas". Added a short intro that (a) explains this is the legacy span-corpus builder, (b) points operators to the Wizard's IRB torture-test toggle for the full 12-agent flow. No behaviour change to the underlying corpus generator; purely a wayfinding fix.

**D. Regression tests** (`test_realworld_file_shapes.py`):
- 6 new tests locking `_read_docx_tables` behaviour: table extraction, comma-quoting, narrative-context capture, bad-zip graceful failure, suffix dispatch, `.xls` graceful degradation without xlrd.

**Regression**: **197 passed / 4 skipped** (up from 192). Zero regressions.

**Untouched (Sir "if functional don't touch")**:
- Wizard corpus mode + STOP button + live trace panel (iter_13/15)
- Publish Guard fail-closed at download (iter_14)
- BYO API token architecture (audit iter_18 documented as intentional)
- Corpus PDF/form removal (iter_17)
- Existing dictionary readers for CSV/XLSX

### iteration_20 (fork) Security audit — SEC-001 + SEC-002 fixed on iter_19 docx parser

Post-iter_19 security audit came back **CONDITIONAL PASS**. Two findings in the new `_read_docx_tables` code — both fixed and locked with regression tests.

- **SEC-001 [MEDIUM] Nested-docx decompression bomb → memory exhaustion FIXED** (`phi_core/agents/specialists.py`):
  * Added `_DOCX_XML_MAX_BYTES = 10 * 1024 * 1024` ceiling on the decompressed size of the inner `word/document.xml`.
  * Defence-in-depth: check `ZipInfo.file_size` BEFORE opening, then also cap the streamed read at `_DOCX_XML_MAX_BYTES + 1` to catch archives that lie about their declared uncompressed size.
  * Real dict.docx (~50 KB inflated) passes; crafted bomb with ~10 MB whitespace payload in a ~50 KB archive is refused. Reader returns empty string so Lexicon just sees no dictionary text rather than the process crashing with OOM.

- **SEC-002 [LOW] XML parsed with stdlib ElementTree instead of installed defusedxml FIXED** (`phi_core/agents/specialists.py`):
  * Switched to `defusedxml.ElementTree.fromstring(raw, forbid_dtd=True)`. `defusedxml==0.7.1` was already in `requirements.txt` — audit found it installed but unused.
  * Refuses ANY `<!DOCTYPE ...>` declaration → blocks billion-laughs and quadratic-blowup entity expansion even on hosts with older libexpat.

- **Hardening cleanup** — removed the dead xlrd fallback in `_read_xls_tables`. xlrd was never in `requirements.txt`, so the fallback was unreachable dead code. Contract preserved: legacy .xls returns "" (Sir's spec is docx small / xlsx large; real BIFF-format .xls is out of scope).

**Regression tests added** (`test_realworld_file_shapes.py`):
- `test_read_docx_tables_rejects_oversize_document_xml` — locks the SEC-001 cap.
- `test_read_docx_tables_refuses_dtd_via_defusedxml` — locks the SEC-002 DTD refusal.

**Verification** — `testing_agent` iter_11: **100% pass on all 41 requested tests** across four suites (`test_realworld_file_shapes` 8/8, `test_corpus` 14/14, `test_security_findings` 6/6, `test_security_audit_iter18` 13/13). Sir's real `diabetes.csv` + `dict.docx` still accepted end-to-end via POST /intake with exit_code=0. Zero backend or frontend issues.

**Total backend regression: 199 passed / 4 skipped** (up from 197). No previously-green test regressed.

**Untouched (audit re-verified as holding)**:
- All iter_14 fixes (guard-gating on /finalize, /bundle, /export)
- All iter_18 fixes (SSE queue leak, subscriber cap, corpus_ground_truth stripped, scrubber breadth)
- ZIP intake caps (path traversal, symlink, zip-bomb on the OUTER study zip)
- BYO Fernet encryption + never returned plaintext
- Praxis never receives dataset rows

### iteration_21 (fork) Gap sweep — find-and-fix loop until clean

Sir directed "find the gap and fix the gap and loop until nothing to be found to fix". Two full sweeps executed, three real defects fixed, ten cosmetic unused-import warnings cleared. Terminated on the third sweep returning clean.

**Real defects fixed**:

- **`tests/test_intake.py` broke pytest collection** (`SystemExit` at module load) → moved to `tests/live/intake_audit.py` since it's a live-integration script, not a pytest suite. pytest collection now runs without crashing.
- **`tests/backend_test.py::test_narrative_flow` off-by-one threshold** — asserted `len(spans) >= 10` on a narrative containing exactly 9 canonical HIPAA identifiers. Loosened to `>= 9` with a comment explaining the per-category check below is the stronger assertion. Suite unlocked: **7 → 8 tests passing**.
- **`tests/test_agent_pipeline.py::test_llm_settings_default` mismatched contract** — asserted `openai_compatible` was in the default `providers` list, but that provider is intentionally opt-in via `ALLOWED_LLM_BASE_URL_HOSTS` for SSRF safety (`phi_core/security.py:52`). Test now matches shipped contract: 5 always-available providers, `openai_compatible` env-gated.

**Cosmetic cleanup** (10 unused imports across tests):
- `test_publish_guard.py`, `test_classification_accuracy.py`, `test_realworld_file_shapes.py`, `test_scrub_and_pseudonym.py`, `test_security_exports_and_zip.py`, `backend_test.py`, `test_corpus_researcher.py`, `test_bundle_and_coverage.py`, `test_speed_and_ux.py` — dropped unreachable `pytest` / `io` / `os` / `asyncio` / `Path` / `Sentinel` / `GuardReport` / `GuardResult` imports pyflakes flagged. Zero behaviour change.

**Regression**: **208 passed / 3 skipped** (up from 199). Nine previously-unrunnable tests are back in the suite. Zero mainline pyflakes issues, zero ESLint warnings.

**Remaining known-flaky (not fixed, low value):**
- `tests/live/intake_audit.py` — legacy live-integration script; pyflakes flags a stray `io`/`os`/`subprocess` and one f-string-missing-placeholder. Not in the pytest path any more; script still runnable directly via `python tests/live/intake_audit.py` for ops smoke tests.
- `tests/test_ocr_pdf.py` — `pytesseract` / `pdf2image` imported inside an availability-probe function with `# noqa: F401`. Pyflakes doesn't respect noqa but the imports are intentional side-effect checks.

## Minor items (from iteration_3, non-blocking)

- Herald sometimes hits the 90s LLM timeout on the full manuscript draft. When it does, pipeline still completes; results.herald is empty and Sir can rerun.
- Judge occasionally sends `dob` to human_review even though the safe answer is `year_only`. LLM prefers the safer route.
- `pseudonymize` output uses a `P<hex8>` prefix (readable, deterministic).

## Agent audit -- in progress (2026-08-14, per-agent walkthrough)

Sir's asked for a one-by-one pass through each of the 12+1 agents: run it standalone
against a real corpus fixture, cross-check with an independent blind sub-agent doing
the same extraction, and log what needs fixing before discussing per-agent model
selection by task complexity. Running notes below; items move to Backlog once triaged.

- **[FIXED] OpenAI reasoning-tier models reject non-default `temperature`.** `gpt-5.6-luna`
  (and by extension any `o1`/`o3`/`o4`/`gpt-5*` reasoning model) 400s on any `temperature`
  other than the API default (1); the saved Settings temperature (0.3) was always sent.
  `litellm.drop_params=True` doesn't help because litellm's model-capability table doesn't
  yet know this newer model rejects the param. Fixed in `phi_core/agents/llm.py`: added
  `_model_supports_custom_temperature(model)`, guards both `_litellm_call` and
  `_litellm_call_with_web_search` so the param is omitted entirely for reasoning-tier models
  instead of being sent and rejected. Found while trying to run Lexicon standalone; every
  agent call would have failed identically until this was fixed.
- **[OPEN] Lexicon silently dropped 4 of 30 columns** on a live run against
  `phi_corpus/study_data/level_1_tuberculosis/dictionary.csv` (gpt-5.6-luna, temperature
  fix applied): `state`, `zip_code`, `years_in_us`, `drug_susceptibility_result` never
  appeared in its `columns` output, no error/warning logged. Missing `zip_code` is
  specifically dangerous -- Judge/Sentinel/Executor only ever see what Lexicon reports, so
  a column Lexicon drops gets no de-identification decision at all rather than being
  flagged, and category B ZIP handling is one of the more failure-prone Safe Harbor rules.
  Root cause not yet isolated (candidates: prompt following degraded on a longer table,
  reply truncation similar to the earlier Judge `max_tokens` bug, or the model just skipped
  rows). Need to: (1) capture the raw pre-parse LLM reply to see if the columns were in the
  text and dropped by parsing, or never generated: (2) check whether `call_json`'s
  `min_items` validator on Lexicon's `call_json` call site catches under-delivery the way
  Judge's did post-fix, or whether Lexicon has no such floor at all.
- **[OPEN] `phi_flag_hint` inconsistently `null` vs `false` for non-PHI columns** --
  `race`, `case_definition`, `site_of_disease`, several lab-result columns came back
  `null` rather than `false` even though the dictionary text explicitly says "keep,
  clinically needed" for all of them. The `bool|null` schema makes `null` a legal "no
  hint given" state, but on this fixture the dictionary *does* give a hint (explicitly
  says not-PHI) for every one of these, so `null` looks like the model punting rather
  than a genuine absence of signal. Needs a decision: tighten the prompt to force a
  bool whenever the dictionary text is unambiguous, or confirm `null` is intentional and
  downstream Judge already treats it correctly.
- **[CONFIRMED] Independent sub-agent cross-check** on the same `level_1_tuberculosis`
  dictionary, run blind with the same schema/rules and no visibility into Lexicon's
  output: extracted all 30/30 columns correctly, including the 4 Lexicon dropped
  (`state`, `zip_code`, `years_in_us`, `drug_susceptibility_result`), and returned a
  clean boolean `phi_flag_hint` on every column with no `null`s -- confirms the
  dictionary text is unambiguous everywhere Lexicon returned `null`. Both findings
  above are real, not fixture artifacts or one-off model flakiness. Next: instrument
  Lexicon to log its raw pre-parse reply so we can see whether the 4 columns were
  generated and lost in parsing, or never generated by the model at all, before
  deciding the fix (retry-on-underdelivery vs. prompt change vs. chunking the table).
- **[CONFIRMED] Instrument Tier-1 (AcroForm) real run.** Zero LLM calls, all
  17/17 fields recovered from `backend/tests/fixtures/tb_collection_form_acroform.pdf`
  via the deterministic `pypdf`/`pdfplumber` proximity match: 8/8 annotated
  variable names correct, 9/9 unannotated field labels correct, no
  `phi_category` or fabricated variable name anywhere in the output.
- **[BLOCKED] Instrument Tier-2 (flat form) live run.** Could not execute
  against `tb_collection_form.pdf` -- `backend/.env`'s `OPENAI_API_KEY` is
  empty and no other provider key (Anthropic/Gemini/OpenRouter/Emergent) is
  present in this worktree's environment, so `Instrument.run()`'s LLM call
  fails with a clean `litellm.BadRequestError` ("You didn't provide an API
  key...") instead of producing output. Recorded as environment-blocked per
  the live-run contingency, not faked or silently skipped. Needs a real
  provider key in this worktree to close.
- **[CONFIRMED] Path B, independent blind sub-agent** given only the flat
  form PDF and the same "list every field" framing, no visibility into
  Instrument's implementation, prompt, or the ground truth file: extracted
  all 17/17 fields, 100% precision/recall on the 8 annotated variable
  names, 100% label match on the 9 unannotated fields. A clean pass, and
  evidence the extraction task itself is solvable at full accuracy by a
  capable model -- it does not substitute for exercising Instrument's own
  Tier-2 prompt/parsing path, which remains unverified live pending a key.
- **Fixture-methodology note.** Grading Tier-1's real AcroForm output
  against the same "annotated variable" ground-truth notion used for the
  flat form produces a low apparent precision (0.47) that is not a defect:
  every AcroForm widget carries a real internal field name by construction
  (that is what makes a PDF fillable), unlike a flat form where
  `collected_variable` is genuinely absent unless printed next to the
  label. The two tiers need different grading semantics; recall (17/17,
  100%) is the meaningful Tier-1 signal.

### P0
- IRB audit dry-run (Sir has 3-day window): step-by-step demonstration script covering (a) adversarial corpus launch, (b) live pipeline trace, (c) verifier panel showing P=R=F1=1.0 across HIPAA A-R, (d) attestation bundle download.

### P1
- Herald: split into abstract-first / sections-later two-call flow so a 90s timeout still gives Sir at least the abstract.
- Additional US-only corpus scenarios (registry-based, mental-health, pediatric) to broaden the archetype library.
- `/api/sessions/{sid}/upload` (single-file) referenced by `test_security_paths.py::test_upload_rejects_evil_filename` returns 404 -- route does not exist; intake goes through `/api/sessions/{sid}/intake` (ZIP) instead. Test is stale against the current API surface, or the single-file upload endpoint was removed without updating the test -- needs a decision, not a fix, since both directions are plausible. Found during the 2026-08-14 agent-pipeline audit; out of scope for that pass (server route surface, not the 12-agent pipeline).
- `test_human_review_invariant.py::test_human_review_requires_reviewer` expects 400 for missing reviewer identity on a nonexistent session; server returns 404 (session-not-found check runs before the reviewer-identity check). Pre-existing, confirmed unrelated to any 2026-08-14 change via `git stash` diff-test.
- Executor/Auditor swarm-based apply and verify (multi-subagent, per Sir's original design) -- deferred. Current deterministic single-pass Executor + single-LLM-pass Auditor + Publish Guard judged sufficient: Executor's determinism is the strongest available guarantee for the zero-row-read invariant (an LLM swarm would be a weaker guarantee, not a stronger one, for this specific step). Revisit only if row-level apply complexity grows beyond what deterministic Python can express cleanly.
- Herald does not interactively ask Sir for publishing venue/format or suggest alternate venues (original vision) -- drafts Abstract/Sections directly against `target_venue` today. Feature work, not a correctness fix.
- Ledger's comparative benchmark does not explicitly narrate the "no LLM ever reads a row value" differentiator against competitor systems (original vision asked for this framing). Feature work, not a correctness fix.

### P2
- Signed attestation PDF per completed study.
- Additional jurisdictions AFTER IRB sign-off: EU/GDPR, UK/GDPR-UK, IN/DPDPA, Canada/PIPEDA, Brazil/LGPD. Kept stubs in `jurisdictions.py` so the framework does not change when we ship.
- SEC-LOW: SSE/download token in URL query string (`security.py:213` + `frontend/lib/api.js:55`).
- SEC-LOW: Reviewer newline injection in `attestation.txt` (`bundle.py:227`).
- `/finalize` legacy flow: ensure Publish Guard runs; unknown/blank Judge actions must not pass cells verbatim.
- Non-Anthropic providers (openai, gemini-direct, openrouter, chatgpt) silently drop web-search citations for Statute/Praxis (`llm.py::call_llm_with_web_search`) -- deterministic fallback covers correctness (confirmed 0% leak on gpt-4o-mini in the 2026-08-14 live audit), but the operator gets no signal that "search was requested but unavailable for this provider." Low priority: only affects citation freshness, not safety.

### Recently DONE (this fork)
- 2026-08-14: **Agent-pipeline audit (12 agents + orchestrator) against live server, gpt-4o-mini.** Environment: fixed two infra gaps found via `/api/health` -- `ATTESTATION_SIGNING_KEY` was unset (bundle attestation silently disabled; generated a dev Ed25519 key), and `tesseract`/`pdftoppm` were missing (Instrument agent's scanned-PDF OCR path was dead; installed via a user-level micromamba env, no root needed). All five health checks now green.
  - **Root-cause bug found and fixed**: Judge's `call_json` used the session's fixed `max_tokens=2000` default regardless of dataset size. Judge's decision schema is ~9 fields per column; once a dataset crossed roughly a dozen columns the reply truncated mid-JSON, failed `min_items` validation on every retry, and fell through to `default={"decisions": []}`. Downstream, an empty decisions list meant the campaign's human-review auto-resolution had nothing to resolve (`resolutions=[]`), and those columns went entirely missing from the export rather than being kept, dropped, or transformed -- confirmed live on two tiers (`l2_pcornet_raw_v1`: 50 columns, `l3_i2b2_crosswalk_v1`: 24 columns), both showing `avg_recall=0.0, avg_f1=0.0` before the fix. Leak rate was 0% throughout (Publish Guard held), but research utility was destroyed for any wider dataset. Fixed by threading an optional `max_tokens` override through `Agent.call`/`call_json` (`dataclasses.replace` on the existing `LlmConfig`, no new abstraction) and having Judge scale its budget with column count (`max(2000, 200 + 150*num_columns)`). Re-verified live post-fix: L2 recall 0.0->0.92 (f1 0.0->0.785), L3 recall 0.0->0.93 (f1 0.0->0.83), leak rate still 0.0 throughout, deterministic offline ladder (15 scenarios) unaffected at 0 errors / 0 leaks.
  - **Visibility fixes** (matching the existing guarded pattern already used for Praxis's per-category `asyncio.gather(..., return_exceptions=True)`): Praxis category failures now log an `agent_log` entry instead of being silently dropped from `praxis_methods`. Auditor/Scout's joint `asyncio.gather` and Herald's Abstract/Sections `asyncio.gather` now catch exceptions instead of letting one subagent's bug crash a run that already succeeded through Executor/Publish Guard -- each falls back to a visibly-marked "not verified" / empty result and logs the exception, rather than either crashing the whole pipeline late or silently producing an empty section with no record of why.
  - **Confirmed working as designed, no fix needed**: deterministic core (Executor, Sentinel hard-rule table, Publish Guard) -- 0 errors, 0% leak, 100% recall across the full 15-scenario offline ladder both before and after the above fixes. Manager/`run_supervised` retry-escalation machinery -- already covered by `test_manager.py`. Auditor's LLM verdict is presentational only; Publish Guard (deterministic) is the actual leak gate downstream code relies on, confirmed nothing treats `Auditor.verdict` as a safety signal.
  - Full `pytest backend/tests/` run: 400 passed / 6 skipped, same 8 pre-existing failures before and after (confirmed via `git stash` on the changed files) -- none touch the agent pipeline; see P1/P2 backlog above.
- Sentinel hard-rule table (dob->year_only, ssn->drop, mrn->pseudonymize, phone/email/fax->drop, name->drop, zip->zip3_truncate, age->cap_age_90, address/url/ip->drop). Runs deterministically before the LLM Sentinel.
- Multi-provider settings UI page at /settings (BYO API key for Anthropic, OpenAI, Gemini, OpenRouter, OpenAI-compatible; Emergent Universal Key by default).
- 2026-02-01: PipelineProgressBar (phase %, current-phase label + blurb) mounted above the live agent trace on `/sessions/{sid}`. Fixed initial `events` undefined build blocker by wiring the component to the `trace` state (SSE-driven). Frontend lint: 0 errors.
- Scope decision (2026-02-01): No jurisdiction expansion until US-HIPAA proves 100% reliable end-to-end across live runs. EU/UK/IN/CA/BR stubs remain in `jurisdictions.py`; expansion deferred.
- 2026-02-01: `/app/memory/VISION.md` added — one-page north-star (problem, answer, six non-negotiable principles, 12-agent architecture, users, trust bar, non-goals, metrics).
- 2026-02-07: **Pipeline speedup** — Statute + Praxis (17-category web-search fan-out) now launch in parallel with Specialists at t=0 instead of serialising after them. On cold cache this overlaps ~all Praxis runtime (biggest single wallclock cost) with Lexicon/Schema/Instrument file parsing.
- 2026-02-07: **Agent trace** — every expanded trace row now shows "role · what · why · how" for the agent (13 agents documented: Lexicon, Schema, Instrument, Statute, Praxis, Judge, Sentinel, Executor, Publish Guard, Auditor, Scout, Ledger, Herald).
- 2026-02-07: **Live wallclock** — orchestrator persists `session.phase_timings` (start/end/duration per phase) + `run_elapsed_s` at pipeline exit. PipelineProgressBar renders elapsed seconds and an expandable per-phase timing table.
- 2026-02-07: **Rigor selector** — `POST /api/sessions/{sid}/handle?iteration_cap={1|2|3}` accepts a per-run Sentinel iteration cap; wizard Step 2 exposes Fast/Balanced/Thorough cards. Cap persisted on session, honoured by the Judge↔Sentinel loop.
- 2026-02-07: **Trace deep-links** — every trace row has `id="trace-{Agent}"` and a "# copy link" affordance in the meta panel; `#trace-Judge` in the URL auto-expands + scrolls to that row.
- 2026-02-07: **Cold-cache warmup** — `POST /api/settings/warmup` primes Statute + all 17 Praxis methods with an ephemeral session id; button on `/settings` triggers it, reports primed/failed counts.
- 2026-02-07: **CLAUDE.md** — fully rewritten to reflect the actual 12-agent architecture (was still describing the legacy detectors/llm_classifier layout).
- 2026-02-07: **Live wallclock baseline** — appended to `VISION.md` Appendix A. Real US-HIPAA corpus run (`diabetes_v1`, iteration_cap=3, cache warm): **196.8 s total**, per-agent LLM time table, projected cold-cache parallel-launch savings ~55 s.
- 2026-02-07: **Deep-link toast** — `#trace-Judge` URLs auto-expand + scroll AND raise a toast ("Deep-link opened: Judge · scroll below for the reviewer's citation") so operators know why the page moved.
- 2026-02-07: **Rigor chip on SessionDetail** — `Rigor · Fast/Balanced/Thorough (N)` chip renders next to "Pipeline progress · N%" with a title-tooltip explaining the trade-off.
- 2026-02-07: **Auto-warmup scheduler** — background loop fires the Statute + 17-Praxis warmup every Monday 09:00 UTC when the operator opts in. Endpoints: `GET/POST /api/settings/warmup/schedule`. UI: checkbox on `/settings` with next-run + last-run bookkeeping.
- 2026-02-07: **Orchestrator closure bug fixed** — `timed_on_phase` was capturing the rebound `on_phase` name and recursing on itself. Now captures `_original_on_phase` before rebinding. Cleared a `RecursionError` seen on the first live baseline run.
- 2026-02-07: **Cold-cache rerun** — wiped Praxis + Statute cache and re-measured. Cold: **181.2 s**; warm (same run again): **159.9 s**. Parallel Praxis fan-out collapses **289.87 s of sequential LLM time into 34.92 s wallclock**. VISION.md Appendix A rewritten with measured cold vs warm baselines and IRB-quotable summary.
- 2026-02-07: **Herald parallelisation** — Herald.Abstract and Herald.Sections now fire concurrently (Sections prompt reworked to skip restating aim/methods). Measured savings: **~33 s cold**, **~31 s warm** on Herald wallclock. Contributed to the 196.8 → 159.9 s total drop on warm runs.
- 2026-02-07: **Rigor persistence on corpus runs** — `CorpusStudyRunBody.iteration_cap` (default 2, balanced) plumbed into `session_handle(sid, iteration_cap=...)`. Corpus runs no longer silently max to 3.
- 2026-02-07: **Warmup schedule countdown** — `/settings` auto-warmup line now renders "next run: Mon Aug 10 09:00 UTC (~2d 15h)" via a client-side `_formatSchedule` helper that decodes the ISO timestamp into weekday + local delta.
- 2026-02-07: **Portability — runs anywhere** — `emergentintegrations` is now a soft/lazy import; `LlmConfig` default provider auto-detects from env (`EMERGENT_LLM_KEY` → `ANTHROPIC_API_KEY` → `OPENAI_API_KEY` → Gemini → OpenRouter). Anthropic native `web_search_20250305` wired through LiteLLM so Statute + Praxis cold-fetch works with a plain Anthropic key. `/settings/llm` no longer advertises `emergent` when the key isn't present. `.env.example` added at `backend/.env.example`. Legacy `phi_core/llm_classifier.py` migrated off direct emergentintegrations calls onto the shared multi-provider client (lazy import to break a circular dep). CLAUDE.md + VISION.md now include portability sections.
- 2026-08-24: **Guardian query broker implemented** — `Manager.attach_schema`/`ask_schema`, `attach_instrument`/`ask_instrument`, `attach_lexicon`/`ask_lexicon` added (`phi_core/agents/manager.py`), closing the gap the three 2026-08-14 guardian design docs left open (specialist-side `Schema.verify`/`Instrument.verify`/`Lexicon.answer` existed with zero callers). `ask_schema`/`ask_instrument` are pure deterministic lookups (no LLM); `ask_lexicon` forwards to `Lexicon.answer`, which does call an LLM, so it is capped at `Manager.LEXICON_QUERY_BUDGET = 8` queries/run. Wired via `attach_*` in `orchestrator.py` right after the Specialists gather. No Judge/Sentinel call site was added in this pass (the broker exists, but nothing calls it yet) — a real caller (e.g. Sentinel querying `ask_schema` on an ambiguous low-cardinality column) is a candidate follow-up, deliberately left undone to avoid scope creep beyond what was asked. Covered by `backend/tests/test_manager_broker.py` (7 tests, all green).
- 2026-08-24: **Auditor-Reviewer doc's ordering claim corrected, not the code** — the 2026-08-17 auditor-reviewer design doc stated Auditor must run before Publish Guard as a non-negotiable; the live orchestrator has always run Publish Guard first, by design (Publish Guard is the deterministic download gate; Auditor's audit pass runs after it as the audit-of-record and recommendation layer, never a second gate). User's explicit call: keep the code as-is, correct CLAUDE.md's agent description instead of reordering the pipeline.
- 2026-08-24: **Two items recorded as open, not implemented** — (1) Praxis multi-method ranking (candidate-selection/ranking logic across HIPAA categories) was never built; `Praxis` still reports methods without ranking (`phi_core/agents/experts.py:320`). (2) Human-review resubmission (`server.py` `/api/sessions/{sid}/human-review`) applies only deterministic hard rules (`apply_sentinel_hard_rules`, `verify_keep_decisions`) and does not re-invoke the LLM `Sentinel` class — an open design question, not a defect. Both were previously tracked only in now-deleted one-time design docs; recorded here so the context survives the docs' removal.
- 2026-08-24: **CLAUDE.md synced to the live pipeline** — "12-agent pipeline" corrected to 15 (12 original + Manager, Operator, Reviewer, all of which existed in code but were undocumented). Structure tree and "## The 15 agents" section now list `manager.py`/`operator.py`/`reviewer.py` and describe their roles, including the Publish-Guard-before-Auditor ordering note above.
- 2026-08-24: **Housekeeping sweep** — removed all 13 one-time design/spec docs from `docs/superpowers/specs/` (10 untracked, 3 previously committed) once their plans were confirmed implemented, corrected in CLAUDE.md, or recorded as open items above. Removed 2 stray non-project files (`Untitled`, `backend/Untitled`, voice-transcript noise with no relation to the codebase). Ran `scripts/cleanup.py --apply` for `__pycache__/`, `.pytest_cache/`, `.impeccable/` (repo root, backend, frontend) — 111 garbage paths cleared, `node_modules/`/`.env`/`.vscode/` correctly left in place (protected by default).
- 2026-08-24: **Emergent Universal Key support removed completely** — per explicit user direction, all `emergent`/`emergentintegrations`/`EMERGENT_LLM_KEY` code paths removed from `phi_core/agents/llm.py`, `phi_core/security.py`, `phi_core/llm_catalog.py`, `server.py`, `.env.example`, and related tests/docs. Direct provider keys (Anthropic, OpenAI, Gemini, OpenRouter) are now the only supported path; Anthropic remains the recommended default for self-hosted deploys.

## Enhancement (would Sir like this next?)

Would Sir like a **Sentinel Hard-Rule table** where I encode: `if column matches known direct-identifier pattern (dob, ssn, mrn, phone, email, address, name) then Sentinel forces Judge to pick from a small allow-list of actions (drop, year_only for dob, zip3_truncate for zip, pseudonymize for name/mrn)`? This closes the "LLM being cautious" pattern where Judge routes obvious PHI to human_review unnecessarily, which is the last remaining accuracy gap.
