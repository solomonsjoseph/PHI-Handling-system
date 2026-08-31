# CLAUDE.md

**Address:** Sir. **Style:** no emojis, no em-dashes, cite authorities, minimal filler.

This repository holds two codebases sharing one git history from a common fork point: the
`backend`/`frontend` PHI Console service (this document's first half), and the standalone
`phi_engine` pipeline package (this document's second half, "phi_engine pipeline"). Both
statements below are true of this tree; read the half that matches the code you are touching.

## PHI Console (backend / frontend)

### Project

PHI Console. A study team drops in a ZIP (datasets + at least one of forms /
data_dictionary / mappings). The pipeline classifies every column, applies HIPAA
§164.514 Safe Harbor transformations deterministically, and emits an IRB-grade bundle
with attestation, benchmark, and manuscript draft.

The zero-row-read invariant is the whole product: the LLM sees dataset **headers**
only. Row values are read exclusively by the deterministic Executor and the
deterministic Publish Guard. One named exception: Lexicon
(`backend/phi_core/agents/specialists.py:121-...`) sends data-dictionary/codebook row
text (column labels and descriptions, not patient dataset rows) to the LLM, after
`scrub_for_prompt` redacts identifiers first.

Jurisdiction scope is **US-only** until end-to-end runs are consistently green. Within
`us`, RegulationsExpert researches HIPAA Safe Harbor (45 CFR 164.514) plus adjacent
PHI/PII regimes: the Common Rule (45 CFR 46), 42 CFR Part 2 (SUD records), FERPA, and
the federal Privacy Act (5 U.S.C. § 552a), with a non-exhaustive state-law advisory
note. EU / UK / IN / CA / BR stubs live in `phi_core/jurisdictions.py` and stay
disabled at the wizard level until Sir clears expansion.

See `README.md` for the north-star, operational spec, and delivery status.

### Structure

```
/app
  backend/
    server.py                     FastAPI on :8001. All /api/* routes.
    phi_core/
      agents/                     LLM pipeline (LiteLLM, direct provider keys)
        base.py                     Agent base class, AgentMessage, ITERATION_CAP
        orchestrator.py             run_pipeline(): phased launch, phase timings, iteration_cap, rewind routing
        specialists.py              Lexicon, Schema, Instrument (parallel at t=0)
        experts.py                  RegulationsExpert, PHIMethodsExpert (on demand, after Judge's triage pass)
        reasoning.py                Judge, Executor, plus deterministic decision-shaping helpers
        deterministic_rules.py      Hard-rule table forcing obvious direct identifiers off human_review
        reviewer.py                 Reviewer (Preview stage before Executor; Final stage after DeterministicVerifier)
        manager.py                  ExecutionHealthSupervisor (NAME="Manager"): health supervision + guardian query broker
        batching.py                 run_batched(): bounded worker-pool batching
        outward.py                  Scout, Ledger, Herald (opt-in post-run report only, never on the mandatory path)
        llm.py                      LiteLLM adapter with web_search_20250305 tool
        cache.py                    Weekly-refresh Mongo cache for RegulationsExpert / PHIMethodsExpert
      control/                     Control-plane services (among others); some fully wired into the
                                    live pipeline, some built and tested but not yet called anywhere
                                    live -- see the sections below for which is which
        handoff.py                   HandoffGateway, ALLOWED_EDGES
        superorchestrator.py         SuperOrchestrator: workflow_runs lifecycle authority, rewind routing
        sandbox.py                   SandboxManager: isolated raw-row execution
        records.py                   RunPrivacyPolicy, ColumnDecision, StudyKnowledgePackage, and other shared contracts
        artifacts.py                 Artifact lineage: invalidate_descendants, export_expires_at
        events.py                    TraceEventStore, the sanitized TraceEvent stream
        trace_projection.py          user_agent_trace / maintainer_trace read-side projections
        rewind.py                    Five-category rewind router (FailureCategory)
        deterministic_verifier.py    DeterministicVerifier: post-execution verification layer
        final_assurance.py           FinalAssuranceGate: the 15-condition release gate (see "Export")
        learning.py                  LearningService / LearningCaseService approval layer
        cleanup_manager.py           CleanupManager: CleanupManifest population
        canary.py                    Leak-canary harness (13 live surfaces)
        security_incident.py         SecurityIncident (a ControlRecord), one FinalAssuranceGate condition
      intake.py                    ZIP unpack + manifest v3 validator (fail-closed)
      file_readers.py              CSV/XLSX/DOCX/PDF; headers-only for datasets
      docx_safe.py                 defusedxml + zip-bomb guards for .docx
      anonymizer.py                Per-file span application, HIPAA-tagged tokens
      publish_guard.py             Deterministic residual-PHI scan at download boundary
      security.py                  scrub_decision, scrub_persisted_text, providers allow-list
      bundle.py                    build_bundle(): the live export ZIP assembler (see "Export")
      crypto.py                    Fernet key/BYO-key encryption, Ed25519 attestation signing
      jurisdictions.py             Rulebook packs. US-HIPAA active; others = stubs.
      llm_catalog.py               Multi-provider model catalog for /settings
      db.py                        Motor MongoDB access
      models.py                    Pydantic Session, ProgressEvent
    phi_corpus/                    Adversarial corpus (datasets + dictionaries only)
      planters.py, scenarios.py, edge_cases.py, generate.py, verify.py
      benchmark.py                  Per-run benchmark report: build_report, to_json/markdown/csv, render_figures
    tests/                         Regression test suite
    .env                           MONGO_URL, DB_NAME, ANTHROPIC_API_KEY, APP_ENCRYPTION_KEY, ATTESTATION_SIGNING_KEY
  frontend/
    src/pages/
      Wizard.jsx                   3-step intake + rigor selector (iteration_cap)
      SessionDetail.jsx            Progress bar, live trace, review, downloads
      Settings.jsx                 LLM provider / model / temperature / max tokens
      Corpus.jsx                   Adversarial corpus runner + verifier
  authorities/                     HIPAA 164.514 primary sources
  data/uploads/<sid>/              intake.zip + unpacked/ (never leaves pod)
  data/exports/                    handled outputs
```

### Agent roster and pipeline

**Manager** (`agents/manager.py`, class `ExecutionHealthSupervisor`, `NAME = "Manager"`)
spans the whole run and does two distinct jobs. First, execution-health supervision
only (attempt counts, error kinds, elapsed seconds), never content: retries, extends a
timeout, grants the web-search tool, or escalates to human review. Second, the
deterministic guardian query broker (`attach_schema`/`ask_schema`,
`attach_instrument`/`ask_instrument`, `attach_lexicon`/`ask_lexicon`) that lets Judge
ask a specialist a targeted question instead of relying only on the broadcast summary;
see "Handoff topology" below for how this broker relates to `HandoffGateway`.

**Specialists** (parallel at t=0): **Lexicon** reads the data dictionary/mapping
(xlsx/csv/docx) and extracts column definitions and code maps. **Schema** reads
dataset headers only; row values never enter the LLM path. **Instrument** reads
collection-form PDFs and extracts field labels, groupings, and instructions.

**Experts** (cache-first, weekly refresh): **RegulationsExpert** researches the
jurisdictional rulebook (HIPAA §164.514(b)(2)(i) for US) and **PHIMethodsExpert**
researches the best-practice transformation technique per HIPAA identifier category
A-R. Unlike the specialists, neither expert launches at run start: `orchestrator.py`
launches them on demand, once each, from the decide loop, right after Judge's first
(triage) pass names which HIPAA categories the dataset actually contains.

**Reasoning:** **Judge** decides a per-column action (keep, drop, cap_age_90,
year_only, zip3_truncate, hash, pseudonymize, scrub_text, human_review), consulting
the guardian query broker and the on-demand expert research as needed. **Executor** is
pure Python, no LLM: it applies the approved actions to dataset rows inside the
sandbox boundary (below).

**Review (deterministic, no LLM):** **Reviewer** runs at two distinct stages.
Reviewer Preview runs before Executor. Reviewer Final runs after
`DeterministicVerifier` and may trigger one of five typed rewind routes (see "Rewind
routes" below). **DeterministicVerifier** (`control/deterministic_verifier.py`) is the
post-execution verification layer between Executor and Reviewer Final; it is a plain
class, not an `Agent` subclass.

**Deterministic gate (no LLM):** **Publish Guard** scans every export byte for
residual PHI before authorising any download.

**External / opt-in only:** **Scout**, **Ledger**, and **Herald** (competitor
landscape, benchmark ledger, publication draft) are not part of the mandatory
PHI-handling path. A completed session (`complete` or `partially_complete`) can
trigger `POST /api/sessions/{sid}/post-run-report`, which runs them synchronously and
returns their output. They can never block, slow, or run inside the mandatory path.

The delivered pipeline, end to end: Schema / Lexicon / Instrument (parallel) build a
`StudyKnowledgePackage` -> Judge classifies columns -> RegulationsExpert /
PHIMethodsExpert run targeted, deduplicated, timeout-bounded research on demand ->
Reviewer Preview -> Human Review (only when triggered: unresolved uncertainty, low
confidence, or a policy-required checkpoint) -> Executor applies the verified
transformation plan inside the sandbox -> DeterministicVerifier -> Reviewer Final (may
route to one of five rewind targets) -> Publish Guard -> the session reaches
`complete` / `partially_complete` / `failed` / `awaiting_human_review` / `blocked`.

Every agent input/output/duration persists to Mongo `agent_log`. Every phase
transition persists to `session.phase_timings` for wallclock analysis.

### Supervision: Manager and SuperOrchestrator

Two separate authorities are both live at once; neither replaces the other.

**Manager** is the broker described above: `ask_schema`/`ask_instrument`/`ask_lexicon`
let Judge query a specialist through a governed handoff, each query recorded as a
(Judge, Schema)/etc. handoff via `HandoffGateway` (see below). Manager also owns
`ROLES`/`BUDGET_S` bookkeeping and writes a per-run charter record to `agent_log`.

**SuperOrchestrator** (`control/superorchestrator.py`) is a separate authority owning
`workflow_runs` lifecycle state: hold/clear-hold, run start/cancel, cleanup
confirmation, opaque-map erasure, human-review-request creation, export-window
bookkeeping, and rewind routing (`SuperOrchestrator.rewind`, called live from Reviewer
Final's FAIL path via `RewindRouter`; see "Rewind routes" below). `server.py` calls
the lifecycle methods above live; the rewind path is called from `orchestrator.py`
instead.

Disclosed gap, not resolved: a large fraction of `SuperOrchestrator`'s own published
API (`resume`, `dependencies_satisfied`, `observe_handoff`,
`require_artifacts_current`, `evaluate_handoff_budget`, `route_budget_exceeded`,
`authorize_execution`, `begin_export`, `confirm_export`, `authorize_publication`) has
zero production caller today. These are deliberately-designed, individually tested
forward-compatible hooks, not accidental dead code, but they do not fire in the live
pipeline yet. Do not describe `SuperOrchestrator`'s full published surface as live;
some of it is, some of it is built and tested but dormant.

### Handoff topology

`HandoffGateway` (`control/handoff.py`) enforces `ALLOWED_EDGES`, a deny-by-default
directed topology between the current agent names (Schema, Lexicon, Instrument, Judge,
RegulationsExpert, PHIMethodsExpert, Reviewer, Executor, DeterministicVerifier,
Manager). `handoff()` records every governed exchange as a `TraceEvent`; any edge not
explicitly declared is refused.

It is invoked today for Manager's guardian query broker: every `ask_schema`/
`ask_instrument`/`ask_lexicon` call records a Judge -> specialist attempt through
`HandoffGateway.handoff` when the broker's handoff facade is attached. For that
specific call path it is a validating audit rail alongside the broker's own
already-authorized answer: a denied verdict is written to the trace store but does not
gate what the broker returns to Judge. It has not yet become the mechanism that routes
the pipeline's own phase transitions; that arrives with a later phase.

### Raw-data / sandbox boundary

`SandboxManager` (`control/sandbox.py`) runs Executor's raw-row-touching operations
(`apply_column_actions_to_dataset`, `_redact_metadata_file`, `_read_dataset_headers`,
`read_narrative`) in an isolated `multiprocessing` child: env allowlisting, a drained
result queue, fail-closed memory limits via `RLIMIT_AS` (with an explicit override,
`PHI_SANDBOX_ALLOW_UNENFORCED_MEMORY`, required on platforms such as macOS that cannot
enforce `RLIMIT_AS` at all), output byte caps, and a 0700-mode working directory.
Every unit-test path with `ctx.sandbox=None` calls the underlying functions
in-process directly; that is a documented, permanent compatibility path, not a bypass
of the boundary in production.

### RunPrivacyPolicy

A typed contract in `control/records.py`. It currently has no live constructor call
site: nothing in the running pipeline creates one yet. Describe it as designed and
typed, not as an enforced, active policy today.

### Human Review

`POST /api/sessions/{sid}/human-review`. Requires `client_event_id` (idempotency) and
`actual_knowledge_ack` for any non-defer resolution (a pure `defer` is exempt:
deferring is not itself a knowledge claim). Reviewer identity comes from
`REVIEWER_PRINCIPALS` (role must be `reviewer`, `lead_reviewer`, or
`expert_determination`; at least one `expert_determination` principal is required at
startup, since 45 CFR 164.514(b)(1) Expert Determination cannot be self-authorized by
any agent), never a client-supplied field. `GET
/api/sessions/{sid}/human-review/source/{file_id}` is the protected source-inspection
endpoint available to any authorized reviewer.

### Artifact lineage

`control/artifacts.py`'s `invalidate_descendants(artifact_id)` is built and tested:
cycle-safe, idempotent, run-scoped, flips descendants to `superseded` and any linked
`VerifiedClassificationManifest` to `invalidated`; a read guard in `open_for_download`
refuses a superseded artifact. Disclosed gap, still open: zero production caller
invokes `invalidate_descendants` today. Describe the mechanism as built, tested, and
ready, but not yet triggered by any live event.

### Rewind routes

`control/rewind.py` defines five typed failure categories (`EXECUTION_ERROR`,
`METHOD_ERROR`, `REGULATION_ERROR`, `SEMANTIC_ERROR`, `UNRESOLVED_UNCERTAINTY`), each
mapped to its earliest-correct stage: `EXECUTION_ERROR` targets `execute`;
`METHOD_ERROR` and `REGULATION_ERROR` both loop back through Judge to `decide`;
`SEMANTIC_ERROR` targets `specialists` (a specialist's own finding was wrong, so the
fix starts there, not merely a Judge re-run over the same bad specialist output);
`UNRESOLVED_UNCERTAINTY`'s target depends on stage (`human_review_audit`
post-execution, `human_review_decisions` pre-execution).

When Reviewer Final returns a FAIL verdict with a signal, `orchestrator.py` calls
`RewindRouter.route` (`control/rewind.py`), which classifies the failure and calls the
existing `SuperOrchestrator.rewind` for the routing decision; that decision is
persisted via `record_rewind_decision` and attached to the run. The run is then
escalated to human review with the rewind decision recorded as context: the actual
automatic re-execution/resume from an arbitrary rewound checkpoint is explicitly out
of scope for this phase (deliberately not implemented). If the router structurally
cannot apply a decision (`WorkflowError`), that is caught and the escalation proceeds
anyway with a `..._rewind_unavailable` reason.

### Observability

One sanitized `TraceEvent` stream (`control/events.py`), with two intended read-side
projections in `control/trace_projection.py`: `user_agent_trace` (a friendly,
sequence-ordered view that never includes the free-form maintainer-only payload
field) and `maintainer_trace` (the full sanitized forensic record). Disclosed gap:
both projection functions are tested but have zero live endpoint caller today. Do not
describe either projection as exposed through a live API; the live per-session trace
surface is `GET /api/sessions/{sid}/agent-trace` and `GET /api/sessions/{sid}/stream`,
which read `agent_log` directly, not through these projections.

### Export -- the single most important accuracy point in this section

Live endpoints: `GET /api/sessions/{sid}/bundle`,
`GET /api/sessions/{sid}/export/{file_id}`, `GET /api/sessions/{sid}/reversal-key`,
`POST /api/sessions/{sid}/acknowledge`, `GET /api/sessions/{sid}/cleanup-status`. All
are gated on session status (`complete`/`partially_complete`), an
`EXPORT_RETENTION_WINDOW_DAYS` (default 14) 410-Gone check once elapsed, and
`export_expires_at` is surfaced on every session read.

The live bundle path is `phi_core/bundle.py::build_bundle`, a self-contained ZIP
assembler with its own coverage CSV/PNG rendering, its own attestation payload, its
own README/methods/results/discussion generation, and its own SHA-256 hashing.

**It is not gated by `FinalAssuranceGate`.** `control/final_assurance.py`'s
`evaluate_final_assurance`, the deterministic, non-bypassable release gate the target
architecture calls for, has zero live call sites in `server.py`,
`superorchestrator.py`, or `agents/`. This is a disclosed, open residual risk, not a
silently missed one: a session that reaches `complete`/`partially_complete` can be
downloaded via `build_bundle` without ever passing the 15-condition
`FinalAssuranceGate` check. `build_bundle` itself still applies its own independent
safety measures (Publish Guard's scan is real and wired into the export path), but the
specific mandatory-per-spec non-bypassable gate is not in that call path today.
`ReportGenerator`/`ZIPBuilder`/`IntegrityService` (a separate, fully built,
extensively tested report-pipeline cluster) are downstream of this same gap and are
also not the live export path. See `SECURITY.md` and
`docs/THREAT_MODEL_BACKEND.md` for the full security framing of this gap.

### Session cleanup

`CleanupManager` (`control/cleanup_manager.py`) populates every `CleanupManifest`
field: `destroyed_categories`, `retained_safe_categories`, `credentials_revoked` (a
real auditable record, never a bare default-true), `keys_destroyed` (NIST SP 800-88
Rev. 2 cryptographic-erase rationale: no independent per-run key to zero separately,
so irrecoverable ciphertext deletion is the documented equivalent),
`sandbox_destroyed`, `storage_sanitization_status`, `verification_status`. It is wired
into `session_delete` and every live purge-loop step. A session never transitions to
`SESSION_DESTROYED` until cleanup verification succeeds or a cleanup incident is
raised.

### Key API endpoints

- `POST /api/auth/session`, `POST /api/auth/logout`, `GET /api/auth/whoami` -- cookie auth.
- `POST /api/sessions` -- create session (jurisdiction).
- `DELETE /api/sessions/{sid}` -- right-to-erasure: session, files, agent_log rows.
- `POST /api/sessions/{sid}/intake` -- manifest v3 validation, fail-closed.
- `POST /api/sessions/{sid}/handle?iteration_cap={1|2|3}` -- launch the pipeline. Rigor selector; default 2.
- `POST /api/sessions/{sid}/cancel` -- operator STOP.
- `POST /api/sessions/{sid}/human-review` -- resolve blocking cases, resume the tail.
- `GET  /api/sessions/{sid}/human-review/source/{file_id}` -- protected source inspection for an authorized reviewer.
- `GET  /api/sessions/{sid}/stream` -- SSE progress + agent trace.
- `GET  /api/sessions/{sid}/agent-trace` -- full agent audit log.
- `GET  /api/sessions/{sid}/results` -- decisions, phase_timings, and other consolidated outputs.
- `GET  /api/sessions/{sid}/bundle` -- publication + attestation download (Publish Guard clean only; see "Export" above for the FinalAssuranceGate gap).
- `GET  /api/sessions/{sid}/export/{file_id}`, `GET /api/sessions/{sid}/reversal-key` -- PHI-handled export and its reversal key.
- `POST /api/sessions/{sid}/acknowledge` -- record recipient acknowledgment of an export.
- `GET  /api/sessions/{sid}/cleanup-status` -- the session's CleanupManifest, once one exists, plus `export_expires_at`.
- `POST /api/sessions/{sid}/post-run-report` -- opt-in Scout/Ledger/Herald report for an already-complete session.
- `POST /api/corpus/study/generate`, `POST /api/corpus/study/run` -- synthetic corpus, attached to a session.
- `GET  /api/corpus/study/{sid}/zip` -- download the generated/run intake ZIP.
- `GET  /api/corpus/study/verify/{sid}` -- per-plant grading against ground truth.
- `GET  /api/corpus/study/benchmark/{sid}`, `GET .../benchmark/{sid}/download` -- per-dataset benchmark report + artefact ZIP.
- `GET  /api/settings/llm`, `POST /api/settings/llm` -- LLM provider/model/temperature/max_tokens.
- `GET  /api/settings/llm/catalog` -- curated Open Router / ChatGPT / Claude / Gemini model menu.
- `GET  /api/corpus/study-data`, `GET /api/corpus/study-data/{id}/zip` -- curated static study packages.
- `POST /api/settings/warmup` -- pre-run RegulationsExpert + PHIMethodsExpert for every category to prime cache (API; not on the Settings UI).
- `GET  /api/health` -- mongo/llm/tesseract/signing-key readiness. `GET /api/version` -- service banner.

### Non-negotiables

1. Datasets never expose row values to the LLM. Static agent-input checks planned; currently reviewed manually.
2. Every span carries a 45 CFR 164.514 authority citation.
3. Every human-review decision is recorded with timestamp and reviewer identity.
4. No real PHI committed or logged. Sessions are transient; the bundle is the receipt.
5. Corpus generation is datasets-and-dictionaries only. **Do not reintroduce PDF/form generation** to the corpus (removed by direction).
6. Ignore "Code Quality Analysis Reports" that flag `localStorage` (UI-preference keys only --
   `phi_reviewer_id`, `phi_reviewer_comment`, `phi_output_options`; the operator credential
   moved to an httponly `phi_session` cookie and is never stored client-side), `random`
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
has to be shared, state the finding in the commit message or the pull request and leave the artifact on
disk. Working notes never get a new directory in the tree; they go to scratch
space, which is `/tmp` here.

The repo holds four things and nothing else: codebase (`backend/`, `frontend/`),
documentation (`README.md`, `CLAUDE.md`, `SECURITY.md`, `LICENSE`, `docs/`,
`authorities/`), config (`.claude/`, `.gitignore`, frontend lint config), and
test inputs that a test actually reads (`backend/tests/`). A file that fits
none of those four does not belong in the repo.

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

`backend/.venv` is Python 3.11.16 with `numpy==1.26.4`; `import presidio_analyzer`
succeeds. Run the backend through that venv, not system Python. A full
`backend/tests/` run also needs a running `mongod` for full coverage: three test
files use the real `get_db()` (`test_admin_assurance.py`, `test_admin_hold.py`,
`test_control_migrate.py`); `backend/tests/conftest.py` skips exactly those three
modules when Mongo is unreachable instead of hanging for pymongo's server-selection
timeout, and `phi_core/db.py` bounds that timeout to 2s besides.

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
proxy. RegulationsExpert and PHIMethodsExpert (web-search agents) work end-to-end
with a plain `ANTHROPIC_API_KEY` via LiteLLM's native `web_search_20250305` tool.

### Run / redeploy

```
sudo supervisorctl restart backend frontend
```

A plain Anthropic key (or any BYO key configured through `/settings`) powers
RegulationsExpert, PHIMethodsExpert, and every LLM agent. Web-search-capable models
(Claude Sonnet 4.5 default) are required for their first-fetch research;
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
  disclosed weak-fallback path, not yet closed: the legacy-scanner-alone
  path lacks the Presidio pass's coverage and can publish on a narrower
  guarantee than the two-scanner path provides. Do not describe this as
  an unconditional two-scanner guarantee.
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
- `harness/validate_privacy_research.py` -- validates `research/privacy_gateway/{evidence_ledger,candidate_registry,dispositions,search_log}` against the research report passed via its required `--report` flag.

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
