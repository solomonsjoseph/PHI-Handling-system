# CLAUDE.md

**Address:** Sir. **Style:** no emojis, no em-dashes, cite authorities, minimal filler.

## Project

PHI Console. A study team drops in a ZIP (datasets + at least one of forms /
data_dictionary / mappings), a 12-agent pipeline classifies every column, applies
HIPAA §164.514 Safe Harbor transformations deterministically, and emits an
IRB-grade bundle with attestation, benchmark, and manuscript draft.

The zero-row-read invariant is the whole product: the LLM sees dataset
**headers** only. Row values are read exclusively by the deterministic Executor
and the deterministic Publish Guard.

Jurisdiction scope is **US-HIPAA only** until end-to-end runs are consistently
green. EU / UK / IN / CA / BR stubs live in `phi_core/jurisdictions.py` and stay
disabled at the wizard level until Sir clears expansion.

See `/app/memory/VISION.md` for the north-star, `/app/memory/GOAL.md` for the
operational spec, `/app/memory/PRD.md` for the delivery log.

## Structure

```
/app
  backend/
    server.py                     FastAPI on :8001. All /api/* routes.
    phi_core/
      agents/                     12-agent pipeline (LiteLLM via Emergent Key)
        base.py                     Agent base class, AgentMessage, ITERATION_CAP
        orchestrator.py             run_pipeline(): parallel launch, phase timings, iteration_cap
        specialists.py              Lexicon, Schema, Instrument (parallel)
        experts.py                  Statute (jurisdiction rules), Praxis (transformation methods)
        reasoning.py                Judge, Sentinel, Executor, Auditor, apply_sentinel_hard_rules
        outward.py                  Scout, Ledger, Herald
        llm.py                      LiteLLM adapter with web_search_20250305 tool
        cache.py                    Weekly-refresh Mongo cache for Statute/Praxis
      intake.py                    ZIP unpack + manifest v3 validator (fail-closed)
      file_readers.py              CSV/XLSX/DOCX/PDF; headers-only for datasets
      docx_safe.py                 defusedxml + zip-bomb guards for .docx
      anonymizer.py                Per-file span application, HIPAA-tagged tokens
      publish_guard.py             Deterministic residual-PHI scan at download boundary
      security.py                  scrub_decision, scrub_persisted_text, providers allow-list
      bundle.py                    Publication + attestation bundles
      benchmark.py                 Precision / recall / F1 per HIPAA category
      jurisdictions.py             Rulebook packs. US-HIPAA active; others = stubs.
      llm_catalog.py               Multi-provider model catalog for /settings
      db.py                        Motor MongoDB access
      models.py                    Pydantic Session, ProgressEvent
    phi_corpus/                    Adversarial corpus (datasets + dictionaries only)
      planters.py, scenarios.py, edge_cases.py, verify.py
    tests/                         210+ regression tests
    .env                           MONGO_URL, DB_NAME, EMERGENT_LLM_KEY
  frontend/
    src/pages/
      Wizard.jsx                   3-step intake + rigor selector (iteration_cap)
      SessionDetail.jsx            Progress bar, live trace with agent meta + deep-links, review, downloads
      Settings.jsx                 BYO-key config + cold-cache warmup
      Corpus.jsx                   Adversarial corpus runner + verifier
  memory/
    VISION.md, GOAL.md, PRD.md, test_credentials.md
  authorities/                     HIPAA 164.514 primary sources
  data/uploads/<sid>/              intake.zip + unpacked/ (never leaves pod)
  data/exports/                    handled outputs
```

## The 12 agents

**Specialists (parallel with Statute + Praxis at t=0):**
- **Lexicon** — dictionary / mapping (xlsx / csv / docx). Extracts column definitions and code maps.
- **Schema** — dataset headers only. Row values never enter the LLM path.
- **Instrument** — collection form PDFs. Extracts field labels, groupings, instructions.

**Experts (cache-first, weekly refresh, fired in parallel with specialists):**
- **Statute** — jurisdictional rulebook (HIPAA §164.514(b)(2)(i) for US).
- **Praxis** — best-practice transformation technique per HIPAA identifier A-R. Categories A/B/C/D/F/G/H use deterministic fallbacks; E/I..R hit web-search.

**Reasoning (Judge<->Sentinel loop, cap 1..3 per run):**
- **Judge** — per-column action: keep, drop, cap_age_90, year_only, zip3_truncate, hash, pseudonymize, scrub_text, human_review.
- **Sentinel** — deterministic hard-rules pass first (dob/ssn/mrn/phone/email/name/zip/age/address/url/ip), then LLM Sentinel for the non-obvious. Blocking issues trigger revision; advisory issues log only.
- **Executor** — pure Python, no LLM. Applies approved actions to dataset rows.
- **Auditor** — precision/recall/F1 per HIPAA category, completeness narrative.

**External / publishing:**
- **Scout** — competitor landscape (Presidio, Comprehend Medical, Azure, JSL).
- **Ledger** — Compare (per-competitor delta) + Aggregate (headline + recommendations). Split so no LLM call exceeds the 90 s timeout.
- **Herald** — Abstract (title + methods + refs) + Sections (results + discussion + limitations).

**Deterministic gate (no LLM):**
- **Publish Guard** — scans every export byte for residual PHI before authorising any download.

Every agent input/output/duration persists to Mongo `agent_log`. Every phase transition persists to `session.phase_timings` for wallclock analysis.

## Key API endpoints

- `POST /api/sessions` — create session (jurisdiction).
- `POST /api/sessions/{sid}/upload` — chunked ZIP upload.
- `POST /api/sessions/{sid}/intake` — manifest v3 validation, fail-closed.
- `POST /api/sessions/{sid}/handle?iteration_cap={1|2|3}` — launch 12-agent pipeline. Rigor selector; default 2.
- `POST /api/sessions/{sid}/cancel` — operator STOP.
- `POST /api/sessions/{sid}/human-review` — resolve blocking cases, resume tail.
- `GET  /api/sessions/{sid}/stream` — SSE progress + agent trace.
- `GET  /api/sessions/{sid}/agent-trace` — full agent audit log.
- `GET  /api/sessions/{sid}/results` — decisions, audit, ledger, herald, phase_timings.
- `GET  /api/sessions/{sid}/bundle` — publication + attestation download (Publish Guard clean only).
- `POST /api/corpus/study/run` — adversarial red-team run.
- `GET  /api/corpus/study/verify/{sid}` — per-plant grading.
- `GET  /api/settings/llm`, `POST /api/settings/llm` — BYO LLM config.
- `POST /api/settings/warmup` — pre-run Statute + all 17 Praxis categories to prime cache.

## Recent (Feb 2026)

- **Pipeline speedup:** Statute + Praxis launch in parallel with Specialists at t=0. On cold Praxis cache (biggest single wallclock cost), 10+ web searches overlap with file parsing rather than serialising after it.
- **Live wallclock:** `session.phase_timings` (start_s, end_s, duration_ms per phase) + `run_elapsed_s` persisted at pipeline exit. Rendered under the progress bar and inline on the current phase.
- **Rigor selector:** wizard step 2 exposes Fast (cap=1) / Balanced (cap=2, default) / Thorough (cap=3). Persisted on the session, honoured by the Judge<->Sentinel loop.
- **Trace deep-links:** every agent row has `id="trace-{Agent}"` + a "# copy link" affordance in the meta panel. `#trace-Judge` on the URL auto-expands and scrolls to that row.
- **Cold-cache warmup:** `POST /api/settings/warmup` (button on `/settings`) pre-primes Statute + all 17 Praxis methods with an ephemeral session id.
- **Agent trace meta:** every expanded row shows `role · what · why · how` for the 13 agents (Lexicon, Schema, Instrument, Statute, Praxis, Judge, Sentinel, Executor, Publish Guard, Auditor, Scout, Ledger, Herald).
- **PipelineProgressBar:** phase %, current-phase label + blurb, elapsed seconds, expandable per-phase timing table.

## Non-negotiables

1. Datasets never expose row values to the LLM. Static agent-input checks planned; currently reviewed manually.
2. Every span carries a 45 CFR 164.514 authority citation.
3. Every human-review decision is recorded with timestamp and reviewer identity.
4. No real PHI committed or logged. Sessions are transient; the bundle is the receipt.
5. Corpus generation is datasets-and-dictionaries only. **Do not reintroduce PDF/form generation** to the corpus (removed by direction).
6. Ignore "Code Quality Analysis Reports" that flag `localStorage` (intended BYO-key), `random` in deterministic seeding, or React exhaustive-deps warnings on single-run effects — audited false positives.
7. Any authentication or credential change goes through `integration_playbook_expert_v2` first. Not in scope right now.

## Environment

Backend on :8001, frontend on :3000, both supervisor-managed with hot reload.
Kubernetes ingress routes `/api/*` to :8001. Frontend uses
`process.env.REACT_APP_BACKEND_URL`; backend uses `os.environ["MONGO_URL"]`
and `os.environ["DB_NAME"]`. Never edit these two.

## Portability -- runs anywhere

The console is deliberately not locked to any hosting platform. Copy
`backend/.env.example` to `backend/.env` and fill in whichever LLM
credential you have. The backend auto-detects the default provider in
this order:

1. `EMERGENT_LLM_KEY`   -> Emergent Universal Key (Emergent platform only)
2. `ANTHROPIC_API_KEY`  -> Anthropic direct (recommended for self-hosted)
3. `OPENAI_API_KEY`     -> OpenAI direct
4. `GEMINI_API_KEY` / `GOOGLE_API_KEY` -> Google Gemini
5. `OPENROUTER_API_KEY` -> OpenRouter

The `emergent` provider option is only advertised to the UI when
`EMERGENT_LLM_KEY` is present, so self-hosted deploys don't see paths
they can't use. `emergentintegrations` is a soft/lazy import; the app
works cleanly without it. Statute + Praxis (web-search agents) work
end-to-end with a plain `ANTHROPIC_API_KEY` via LiteLLM's native
`web_search_20250305` tool -- no Emergent library required.

## Run / redeploy

```
sudo supervisorctl restart backend frontend
```

Emergent Universal Key powers Statute, Praxis, and every LLM agent when
running on the Emergent platform. Anywhere else, a plain Anthropic key
(or any BYO key configured through `/settings`) covers the same paths.
Web-search-capable models (Claude Sonnet 4.5 default) are required for
Statute + Praxis first-fetch; deterministic fallbacks kick in otherwise.
