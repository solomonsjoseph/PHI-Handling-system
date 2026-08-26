# CLAUDE.md

**Address:** Sir. **Style:** no emojis, no em-dashes, cite authorities, minimal filler.

This repository holds two codebases sharing one git history from a common fork point: the
`backend`/`frontend` PHI Console service (this document's first half), and the standalone
`phi_engine` pipeline package (this document's second half, "phi_engine pipeline"). Both
statements below are true of this tree; read the half that matches the code you are touching.

## PHI Console (backend / frontend)

### Project

PHI Console. A study team drops in a ZIP (datasets + at least one of forms /
data_dictionary / mappings), a 15-agent pipeline (12 original agents plus Manager,
Operator, and Reviewer) classifies every column, applies HIPAA §164.514 Safe Harbor
transformations deterministically, and emits an IRB-grade bundle with attestation,
benchmark, and manuscript draft.

The zero-row-read invariant is the whole product: the LLM sees dataset
**headers** only. Row values are read exclusively by the deterministic Executor
and the deterministic Publish Guard.

Jurisdiction scope is **US-only** until end-to-end runs are consistently
green. Within `us`, Statute researches HIPAA Safe Harbor (45 CFR 164.514) plus
adjacent PHI/PII regimes: the Common Rule (45 CFR 46), 42 CFR Part 2 (SUD
records), FERPA, and the federal Privacy Act (5 U.S.C. § 552a), with a
non-exhaustive state-law advisory note. EU / UK / IN / CA / BR stubs live in
`phi_core/jurisdictions.py` and stay disabled at the wizard level until Sir
clears expansion.

See `/app/memory/VISION.md` for the north-star, `/app/memory/GOAL.md` for the
operational spec, `/app/memory/PRD.md` for the delivery log.

### Structure

```
/app
  backend/
    server.py                     FastAPI on :8001. All /api/* routes.
    phi_core/
      agents/                     15-agent pipeline (LiteLLM, direct provider keys)
        base.py                     Agent base class, AgentMessage, ITERATION_CAP
        orchestrator.py             run_pipeline(): parallel launch, phase timings, iteration_cap
        specialists.py              Lexicon, Schema, Instrument (parallel)
        experts.py                  Statute (jurisdiction rules), Praxis (transformation methods)
        reasoning.py                Judge, Sentinel, Executor, Auditor, apply_sentinel_hard_rules
        manager.py                  Manager: supervises every LLM call (retry/extend/web-search/escalate); deterministic guardian query broker (attach_*/ask_*)
        operator.py                 Operator: deterministic self-verification of Executor's writes against decisions
        reviewer.py                 Reviewer: deterministic confirmation Operator covered every decision
        batching.py                 run_batched(): bounded worker-pool batching shared by Operator/Reviewer
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
      crypto.py                    Fernet key/BYO-key encryption, Ed25519 attestation signing
      jurisdictions.py             Rulebook packs. US-HIPAA active; others = stubs.
      llm_catalog.py               Multi-provider model catalog for /settings
      db.py                        Motor MongoDB access
      models.py                    Pydantic Session, ProgressEvent
    phi_corpus/                    Adversarial corpus (datasets + dictionaries only)
      planters.py, scenarios.py, edge_cases.py, generate.py, verify.py
      benchmark.py                  Per-run benchmark report: build_report, to_json/markdown/csv, render_figures
    tests/                         340+ regression tests
    .env                           MONGO_URL, DB_NAME, ANTHROPIC_API_KEY, APP_ENCRYPTION_KEY, ATTESTATION_SIGNING_KEY
  frontend/
    src/pages/
      Wizard.jsx                   3-step intake + rigor selector (iteration_cap)
      SessionDetail.jsx            Progress bar, live trace with agent meta + deep-links, review, downloads
      Settings.jsx                 LLM provider / model / temperature / max tokens
      Corpus.jsx                   Adversarial corpus runner + verifier
  memory/
    VISION.md, GOAL.md, PRD.md, test_credentials.md
  authorities/                     HIPAA 164.514 primary sources
  data/uploads/<sid>/              intake.zip + unpacked/ (never leaves pod)
  data/exports/                    handled outputs
```

### The 15 agents

**Supervisor (spans the whole run):**
- **Manager** — supervises execution health only (attempt counts, error kinds, elapsed seconds), never content: retries, extends a timeout, grants the web-search tool, or escalates to human review, with coaching notes reused across the run. Also owns the deterministic guardian query broker (`attach_schema`/`ask_schema`, `attach_instrument`/`ask_instrument`, `attach_lexicon`/`ask_lexicon`) so Judge/Sentinel can ask a specialist a targeted question instead of relying only on the broadcast summary.

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
- **Operator** — deterministic. Self-verifies Executor's written output against the approved decisions: completeness first, then a shape check per transform action. Never calls an LLM.
- **Reviewer** — deterministic. Confirms Operator covered every decision, closing the one gap Operator's own completeness pass can't see (an `omit_by_file` column with no matching decision). Runs before Auditor.
- **Auditor** — precision/recall/F1 per HIPAA category, completeness narrative, independent re-derivation with self-reported confidence (escalates a second human-review gate below `AUDITOR_CONFIDENCE_FLOOR`). Runs after Publish Guard by design: Publish Guard is the deterministic download gate, and Auditor's verdict is the audit-of-record/recommendation layer on top, not a second gate.

**External / publishing:**
- **Scout** — competitor landscape (Presidio, Comprehend Medical, Azure, JSL).
- **Ledger** — Compare (per-competitor delta) + Aggregate (headline + recommendations). Split so no LLM call exceeds the 90 s timeout.
- **Herald** — Abstract (title + methods + refs) + Sections (results + discussion + limitations).

**Deterministic gate (no LLM):**
- **Publish Guard** — scans every export byte for residual PHI before authorising any download.

Every agent input/output/duration persists to Mongo `agent_log`. Every phase transition persists to `session.phase_timings` for wallclock analysis.

### Key API endpoints

- `POST /api/auth/session`, `POST /api/auth/logout`, `GET /api/auth/whoami` -- cookie auth (SEC 4.3).
- `POST /api/sessions` -- create session (jurisdiction).
- `DELETE /api/sessions/{sid}` -- right-to-erasure: session, files, agent_log rows.
- `POST /api/sessions/{sid}/intake` -- manifest v3 validation, fail-closed.
- `POST /api/sessions/{sid}/handle?iteration_cap={1|2|3}` -- launch 12-agent pipeline. Rigor selector; default 2.
- `POST /api/sessions/{sid}/cancel` -- operator STOP.
- `POST /api/sessions/{sid}/human-review` -- resolve blocking cases, resume tail.
- `GET  /api/sessions/{sid}/stream` -- SSE progress + agent trace.
- `GET  /api/sessions/{sid}/agent-trace` -- full agent audit log.
- `GET  /api/sessions/{sid}/results` -- decisions, audit, ledger, herald, phase_timings.
- `GET  /api/sessions/{sid}/bundle` -- publication + attestation download (Publish Guard clean only).
- `POST /api/corpus/study/generate`, `POST /api/corpus/study/run` -- synthetic corpus, attached to a session.
- `GET  /api/corpus/study/{sid}/zip` -- download the generated/run intake ZIP.
- `GET  /api/corpus/study/verify/{sid}` -- per-plant grading against ground truth.
- `GET  /api/corpus/study/benchmark/{sid}`, `GET .../benchmark/{sid}/download` -- per-dataset benchmark report + artefact ZIP.
- `GET  /api/settings/llm`, `POST /api/settings/llm` -- LLM provider/model/temperature/max_tokens.
- `GET  /api/settings/llm/catalog` -- curated Open Router / ChatGPT / Claude / Gemini model menu.
- `GET  /api/corpus/study-data`, `GET /api/corpus/study-data/{id}/zip` -- curated static study packages.
- `POST /api/settings/warmup` -- pre-run Statute + all 17 Praxis categories to prime cache (API; not on Settings UI).
- `GET  /api/health` -- mongo/llm/tesseract/signing-key readiness. `GET /api/version` -- service banner.

### Recent (Feb 2026)

- **Pipeline speedup:** Statute + Praxis launch in parallel with Specialists at t=0. On cold Praxis cache (biggest single wallclock cost), 10+ web searches overlap with file parsing rather than serialising after it.
- **Live wallclock:** `session.phase_timings` (start_s, end_s, duration_ms per phase) + `run_elapsed_s` persisted at pipeline exit. Rendered under the progress bar and inline on the current phase.
- **Rigor selector:** wizard step 2 exposes Fast (cap=1) / Balanced (cap=2, default) / Thorough (cap=3). Persisted on the session, honoured by the Judge<->Sentinel loop.
- **Trace deep-links:** every agent row has `id="trace-{Agent}"` + a "# copy link" affordance in the meta panel. `#trace-Judge` on the URL auto-expands and scrolls to that row.
- **Cold-cache warmup:** `POST /api/settings/warmup` pre-primes Statute + all 17 Praxis methods with an ephemeral session id (API / campaign; Settings UI is provider/model only).
- **Agent trace meta:** every expanded row shows `role · what · why · how` for the 13 agents (Lexicon, Schema, Instrument, Statute, Praxis, Judge, Sentinel, Executor, Publish Guard, Auditor, Scout, Ledger, Herald).
- **PipelineProgressBar:** phase %, current-phase label + blurb, elapsed seconds, expandable per-phase timing table.

### Non-negotiables

1. Datasets never expose row values to the LLM. Static agent-input checks planned; currently reviewed manually.
2. Every span carries a 45 CFR 164.514 authority citation.
3. Every human-review decision is recorded with timestamp and reviewer identity.
4. No real PHI committed or logged. Sessions are transient; the bundle is the receipt.
5. Corpus generation is datasets-and-dictionaries only. **Do not reintroduce PDF/form generation** to the corpus (removed by direction).
6. Ignore "Code Quality Analysis Reports" that flag `localStorage` (UI-preference keys only --
   `phi_reviewer_id`, `phi_reviewer_comment`, `phi_output_options`; the operator credential
   moved to an httponly `phi_session` cookie in 4.3 and is never stored client-side), `random`
   in deterministic seeding, or React exhaustive-deps warnings on single-run effects --
   audited false positives.
7. Any authentication or credential change goes through `integration_playbook_expert_v2` first. Not in scope right now.
8. Nothing a command can regenerate is ever committed. Generated output is
   git-ignored and swept after every task. See "Generated artifacts and cleanup".

### Generated artifacts and cleanup

The generator is worth keeping. Its output is not. Anything a command can
recreate is git-ignored, is never committed, and is deleted once the work it
supported is finished. This holds for output that exists today and for output
new tooling starts producing later: when a new artifact appears, add its pattern
to `.gitignore` in the same change that creates it.

Never committed, now or in the future:

- `test_reports/`, including pytest JUnit XML, iteration JSON, and
  `corpus/<run>/campaign_report.{json,md}`
- corpus generator output of every kind: campaign reports, planted ZIPs,
  ground-truth JSON
- runtime session artifacts under `data/uploads/`, `data/exports/`, `data/corpus/`
- caches and bytecode: `__pycache__/`, `.pytest_cache/`, `.coverage`,
  `.ruff_cache/`, `.mypy_cache/`
- build output and dependency trees: `frontend/build/`, `node_modules/`,
  `dist/`, `build/`
- agent and harness scratch: `.superpowers/`, `.impeccable/`,
  `.phi-build-status`, `tmp/`, `intake-v3-final`

`git add -f` on any of these is prohibited. When a generated result looks like it
has to be shared, record the finding in `memory/PRD.md` and leave the artifact on
disk. Working notes never get a new directory in the tree; they go to scratch
space, which is `/tmp` here.

The repo holds four things and nothing else: codebase (`backend/`, `frontend/`),
documentation (`memory/`, `authorities/`, `docs/file_formats/`, `README.md`,
`CLAUDE.md`, `LICENSE`), config (`.claude/`, `.gitignore`,
`design_guidelines.json`, frontend lint config), and test inputs that a test
actually reads (`backend/tests/`). A file that fits none of those four does not
belong in the repo.

#### Garbage collection after every task

Every task ends with a sweep, so the tree holds the work that was done and
nothing that was used to produce it:

```
python scripts/cleanup.py            # dry run, lists what would go
python scripts/cleanup.py --apply    # remove it
```

The sweep is `git clean -X` scoped by the ignore list, so tracked files and
untracked-but-unignored files are never at risk. Local credentials
(`backend/.env`, `*.pem`, `*.key`) and `.vscode/` are protected at every scope.
`node_modules/`, `.venv/` and `frontend/build/` survive the default sweep and go
only with `--all`, which is the `make distclean` case.

Run the sweep when no pipeline is in flight: it removes session directories under
`data/uploads/` and `data/exports/`.

### Environment

Backend on :8001, frontend on :3000, both supervisor-managed with hot reload.
Kubernetes ingress routes `/api/*` to :8001. Frontend uses
`process.env.REACT_APP_BACKEND_URL`; backend uses `os.environ["MONGO_URL"]`
and `os.environ["DB_NAME"]`. Never edit these two.

### Portability -- runs anywhere

The console is deliberately not locked to any hosting platform. Copy
`backend/.env.example` to `backend/.env` and fill in whichever LLM
credential you have. The backend auto-detects the default provider in
this order (`phi_core/agents/llm.py::_default_provider`):

1. `OPENROUTER_API_KEY` -> OpenRouter
2. `OPENAI_API_KEY`     -> OpenAI direct
3. `ANTHROPIC_API_KEY`  -> Anthropic direct (recommended for self-hosted)
4. `GEMINI_API_KEY` / `GOOGLE_API_KEY` -> Google Gemini

Direct provider keys are the only supported path -- there is no universal-key
proxy. Statute + Praxis (web-search agents) work end-to-end with a plain
`ANTHROPIC_API_KEY` via LiteLLM's native `web_search_20250305` tool.

### Run / redeploy

```
sudo supervisorctl restart backend frontend
```

A plain Anthropic key (or any BYO key configured through `/settings`) powers
Statute, Praxis, and every LLM agent. Web-search-capable models (Claude
Sonnet 4.5 default) are required for Statute + Praxis first-fetch;
deterministic fallbacks kick in otherwise.

## phi_engine pipeline (agent handoff document)

This section tells an agent everything it needs to know to work on the
`phi_engine` package. Read it in full before touching any file under
`phi_engine/`, `tests/`, `harness/`, or `authorities/`.

### Scope

USA/HIPAA only. Pinned de-identification rules exist solely for USA
(`phi_engine/security/phi_review.py` `_PINNED_RULE_SPECS`), grounded in
`authorities/01_hipaa_164_514_full.md` (45 CFR 164.514 primary text) and
`authorities/AUTHORITY_MATRIX.md` (identifier-category mapping). Extending
to another jurisdiction needs its own pinned rule-spec entries grounded in
that jurisdiction's own authority document set.

This repository does not certify HIPAA or any other regulatory compliance.

### What phi_engine is

A standalone PHI intake, classification, scrubbing, review, and publish
pipeline (`phi_engine`), runnable via `python -m phi_engine`. It is a
portable package: point it at any project's own data with `PHI_WORKSPACE`
and zero code changes.

### Invariants an agent must never break

- **Source immutability.** `phi_engine/pipeline/intake.py::intake_add`
  links files into `<workspace>/intake/<study>/` via `os.symlink` only --
  never `shutil.copy*`. Intake never opens a source file for write and
  never deletes a source file.
- **Symlink-only intake.** Every entry under `<workspace>/intake/<study>/`
  is either a symlink or the `intake_manifest.json` bookkeeping file.
- **Never move/modify/copy source data.** The organizer
  (`phi_engine/pipeline/organize.py::organize`) reads normalized dataset
  content only through intake symlinks and writes derived artifacts under
  `<workspace>/organized/<study>/` -- never back into the source tree
  passed to `intake_add`. It also performs a direct metadata-only read of
  an optional forms manifest from the external source root, separate from
  the row-data path.
- **Fail-closed review routing.** Any raw variable/dataset that cannot be
  normalized (unrecognized suffix, broken intake symlink, unreadable
  `.xls`, unparseable `.pdf`) lands in the review bucket with a record
  retaining filename, link name, reason, and diagnostic metadata -- never
  row values, never silently dropped, never silently parsed as garbage.
- **Residual guard before publish, with a disclosed fallback gap.** The
  published `llm_source/datasets/` tree passes
  `phi_engine/security/phi_guard_gate.py::run_phi_guard_gate` (Presidio AND
  a legacy regex scanner) when the gate runs cleanly. On a guard exception
  (`phi_engine/pipeline/run.py`'s residual-guard `except Exception:`
  block), the pipeline currently falls back to the legacy regex scanner
  ALONE and can still publish on that scanner's result -- a known,
  disclosed weak-fallback path, not yet closed (see
  `docs/PRIVACY_GATEWAY_RECOMMENDATION.md` §"Weak points wrapped or
  replaced"). Do not describe this as an unconditional two-scanner
  guarantee.
- **LLM egress controls; read-path wrapper not yet a production
  chokepoint.** Header classification prompts (`llm_detector.py`) are
  headers-only -- never a row value. `LLMClient.complete` runs
  `phi_gate_check` (`phi_engine/security/phi_gate.py`) on the outbound
  prompt before provider dispatch and raises `PHIEgressBlockedError` on a
  match. `guard_llm_output` screens serialized tool output through the PHI
  gate and IS called from `llm_detector.py` and
  `phi_engine/tools/regulation_fetcher.py` provider-response paths; the
  generic `llm_safe_tool` decorator has ZERO production `@llm_safe_tool`
  usages, so arbitrary tool returns are not routed through it.
  `llm_tool_guard.py`'s `validate_llm_read_path` is defined and exported
  but currently has NO production caller anywhere in `phi_engine` --
  available, not yet wired as an enforced read-side chokepoint. Audit
  stores beyond `phi_gate`/log-hygiene blocking-path logs -- the
  organizer review-bucket record (`organize.py`, explicitly
  `chmod(0o600)`) and `phi_scrub_report.json` -- retain filenames, link
  names, header/field names, reasons, counts, and diagnostic metadata:
  sensitive metadata, not row values, but not "category tags only"
  either. `llm_uncertain.jsonl` (`llm_detector.py::_write_review_queue`)
  retains the same class of metadata (including the raw column header)
  but is written via plain `Path.open("a")` with NO explicit chmod --
  do not claim it is 0600-guaranteed; an empirical tempfile check
  produced mode 0644 / parent 0755.
- **Human review feedback loop.** A `keep`/`drop`/`override <action>`
  decision (`python -m phi_engine review --study S decide ...`) is
  persisted and applied on the NEXT `run` -- not merely logged.

### Intake contract (intake-manifest/v3)

- **Required package.** A source root MUST provide `datasets/` (always
  required), plus at least one of `forms/` or `dictionary_mapping/` (an
  alternative group, not both mandatory). Missing/empty required
  components -- or a shortfall in the alternative group -- are blocking
  review items.
- **Closed accepted-format matrix** (`intake_preflight._COMPONENT_SUFFIXES`):
  `datasets/` = `.csv`/`.xls`/`.xlsx` (dataset `.xlsx` must be single-sheet);
  `forms/` = `.pdf` only (annotated and non-annotated are not distinguished,
  no `annotated_pdfs` alias); `dictionary_mapping/` =
  `.csv`/`.xlsx`. `.json`/`.jsonl` are NOT accepted datasets. Any
  unsupported suffix, invalid/multi-sheet workbook, or cross-component
  hardlink becomes an `_unclassified` review item. Nested subdirectories and
  duplicate content are preserved as distinct entries.
- **Source symlink rejection.** The whole source subtree is opened
  `O_NOFOLLOW`; a symlink anywhere yields `source-symlink-not-allowed` and is
  never followed.
- **Study naming.** `--study` (source `user`) is required for every
  subcommand except `intake`. Omitted at intake: local-only, support-content
  -only AI naming (source `ai`, loopback/attested/digest-pinned client, never
  `config.get_llm_client()`) is permitted ONLY by the positive attestation
  `--support-confirmed-no-phi`; otherwise a random `study-<8hex>` name
  (source `generated`) is assigned and reused/promoted for the same source.
  There is no negative "may contain PHI" flag.
- **Manifest.** `intake-manifest/v3` keys `schema`/`study`/
  `study_name_source`/`status`/`source_root`/`entries`/`review_items`/
  `errors`/`removals`; statuses `ready`/`review_required`/`failed` map to
  exit `0`/`8`/`2`. Clean cutover: a missing/malformed/v2 manifest fails with
  a fixed public code, no legacy reader or shim.
- **Redacted output.** `intake` prints only `{"study", "status", "linked",
  "review", "errors", "manifest"}` to stdout, never entry paths, review/error
  detail, or raw exceptions.
- **Organize is component-authoritative.** The organizer routes each entry by
  the `component` intake assigned, never by re-guessing from path/suffix;
  `_unclassified` is never parsed. `run_pipeline` generates no source-of-truth
  tree automatically; standalone SoT is available only to callers that
  explicitly maintain the legacy annotated-PDF layout.

### Authority grounding

Every classification/action claim in code comments or documentation should
trace to `authorities/01_hipaa_164_514_full.md` or
`authorities/AUTHORITY_MATRIX.md`. Do not add jurisdiction, identifier
category, or benchmark claims without a grounding authority citation or a
`file:line` reference into surviving `phi_engine`/`harness` code.

### Runtime paths

- `phi_engine/cli/main.py` -- `python -m phi_engine {intake,organize,run,review,status}`
  entry point; module docstring is the source of truth for exact CLI syntax.
- `phi_engine/pipeline/{intake,organize,run,review,dependencies}.py` -- pipeline stages.
- `phi_engine/security/{phi_review,phi_scrub,phi_guard_gate,phi_gate,llm_tool_guard,presidio_gate,kanon_gate}.py` -- classification, scrub, and guard controls.
- `phi_engine/config/config.py`, `phi_engine/config/config.yaml`, `phi_engine/config/_defaults/` -- static configuration; per-study config is synthesized fresh each run (`phi_engine/pipeline/synthesize_config.py`) and is not source of truth.
- `harness/make_stress_fixtures.py`, `harness/make_privacy_gateway_fixtures.py` -- deterministic fixture builders used by the stress and privacy-gateway test suites.
- `harness/spec_check.py` -- post-run invariant checker (`intake_symlink_invariant`, `llm_boundary_canary`, `source_immutability`).
- `harness/validate_privacy_research.py` -- validates `research/privacy_gateway/{evidence_ledger,candidate_registry,dispositions,search_log}` against `docs/PRIVACY_GATEWAY_RESEARCH.md`.

### Verification commands

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider \
  tests/test_phi_engine_integration.py \
  tests/test_stress_standalone.py \
  tests/test_phase3_run_pipeline_integration.py \
  tests/test_phase3_run_review_integration.py \
  tests/test_pipeline_lock.py \
  tests/test_phi_llm_safety.py \
  tests/test_llm_egress_gate.py \
  tests/test_validate_privacy_research.py

PYTHONDONTWRITEBYTECODE=1 python -m harness.validate_privacy_research

PYTHONDONTWRITEBYTECODE=1 python -m phi_engine --help
PYTHONDONTWRITEBYTECODE=1 python -c "from phi_engine.security.presidio_gate import analyze_text; assert analyze_text('SSN 123-45-6789')"
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
```

Before claiming any change complete: run the relevant command(s) above and
report the actual observed output. Never claim a test passed without
running it.

### Working conventions

- No em-dashes, no emojis in generated files (repository convention).
- Never commit real PHI. Never commit AWS/API or LLM provider credentials.
- Every classification/action rule change must cite its authority source.
- Commit messages follow Conventional Commits.
