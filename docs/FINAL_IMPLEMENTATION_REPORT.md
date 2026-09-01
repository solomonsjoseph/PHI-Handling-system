# PHI-Handling-system infrastructure rewrite: final implementation report

Repo: `PHI-Handling-system`, branch `feat/phi-infrastructure-v2`, HEAD `3a2e6e0`.
Authoritative phase-by-phase evidence: `docs/PHASE_STATUS.md` (2700+ lines, this whole
rewrite's append-only log; every claim below is a distillation of, and traceable to, an
entry there). Master architecture source: `docs/MASTER_ARCHITECTURE_V2.md` (gitignored,
written verbatim at Step 0). Execution plan: `local://phi-rewrite-execution-plan.md`.

## A. Repository baseline

Started from `feat/phi-infrastructure-v2` at `3bf66e8`, forked from `main`'s tip
(`e2f8d4b`, confirmed ancestor). 271 commits landed this rewrite; 220 files changed
(+37,700 / -5,093 lines: 172 Python, 22 JSX, 11 Markdown, plus config/test-data files).
Pre-rewrite Phases 0-3 were previously committed but verified **built, not integrated**
(control-plane modules existed and were unit-tested with zero call sites in the running
pipeline) — this finding is why Phase R (remediation) exists as this rewrite's first
substantial phase, and the same defect class recurred twice more later (Phase 11's
`FinalAssuranceGate`, Phase 11b's report-generation cluster — see AA/AB below).

## B. Branch and migration history

`feat/agent-design-docs` was inventoried (per the plan's explicit anti-blind-merge rule)
and found to be an older, superseded snapshot: every structural component it carried
already exists on `main` equal-or-better, and its deletion of `phi_engine`/`harness`
contradicts this rewrite's out-of-scope rule. Recorded in
`docs/BRANCH_MIGRATION_INVENTORY.md`. The branch was not merged or deleted (plan
instruction: never delete the source branch as part of this rewrite unless explicitly
requested).

## C. Instruction files reconciled

`CLAUDE.md`'s PHI Console half fully rewritten (Phase 18, commit `9a3db5c`) to describe
the delivered architecture; its "Migration status" section removed (the migration is
done). The `phi_engine` half (from `## phi_engine pipeline` onward) is explicitly
untouched throughout, per the plan's boundary. No other active instruction file exists in
this tree (`.claude/settings.json` unchanged; no nested `AGENTS.md`/`.cursorrules`).

## D. Architecture before

15-agent LLM pipeline (12 original agents plus Manager/Sentinel/Operator/Auditor as
top-level roles), Statute/Praxis as regulatory-research agent names, Scout/Ledger/Herald
running unconditionally as part of the mandatory path, a duplicate-schema state
(`ColumnDecision` unwired alongside the live `JudgeDecision`), and the section-84/85/86
security/handoff infrastructure built but not called from the live pipeline.

## E. Architecture after

Schema/Lexicon/Instrument specialists (parallel) → `StudyKnowledgePackage` → Judge triage
→ RegulationsExpert/PHIMethodsExpert (on-demand, post-triage, deduplicated,
timeout-bounded) → Reviewer Preview → Human Review (only when triggered) → Executor
(sandboxed raw-row operations) → DeterministicVerifier → Reviewer Final (five typed
rewind routes) → Publish Guard → terminal status. Sentinel/Operator/Auditor retired as
top-level roles (folded into Reviewer/DeterministicVerifier/deterministic gates, per the
master architecture's own legacy migration map, section 81). Scout/Ledger/Herald moved to
an opt-in, post-run-only `POST /api/sessions/{sid}/post-run-report`. Full detail:
`CLAUDE.md`'s "Agent roster and pipeline" section.

## F. Agent matrix

| Role | Input | Output | Tools | Prohibited access |
|---|---|---|---|---|
| Schema | dataset headers only | column names/types | none (deterministic + LLM interpretation) | never reads row values |
| Lexicon | dictionary/codebook rows (scrubbed) | column definitions/code maps | none | never reads dataset rows |
| Instrument | form PDF text (Tier 1 AcroForm / Tier 2 OCR+LLM) | field labels | pdf readers | never reads dataset rows |
| Judge | `StudyKnowledgePackage`, guardian broker, expert research | per-column `ColumnDecision` | guardian query broker (`ask_schema`/`ask_instrument`/`ask_lexicon`) | never sees raw row values |
| RegulationsExpert | Judge's triage category | `RegulatoryFinding` + evidence | web search (provider-hosted tool), evidence cache | no dataset row access |
| PHIMethodsExpert | Judge's triage category | `MethodFinding` + evidence | web search, evidence cache, `MethodRegistry` query | no dataset row access |
| Reviewer (Preview/Final) | Judge's decisions / execution results | pass/fail + correction/rewind signal | deterministic rule table (`deterministic_rules.py`) | no LLM call for the hard-rule pass; Final stage's own LLM call never sees raw dataset values |
| Executor | verified transformation plan | transformed dataset files | sandboxed subprocess (`SandboxManager`) | no network, credential env stripped, output-byte capped |
| DeterministicVerifier | Executor's output | `VerificationResult` | none (pure Python) | — |
| Publish Guard | export bytes | clean/blocked verdict | deterministic scan | — |
| Scout/Ledger/Herald (opt-in) | completed session | landscape/benchmark/publication draft | web search | never runs inside the mandatory path |

## G. Deterministic service matrix

`HandoffGateway` (deny-by-default topology, `ALLOWED_EDGES`), `SandboxManager` (isolated
raw-row execution), `DeterministicVerifier`, Publish Guard, `CleanupManager`
(`CleanupManifest` population, NIST SP 800-88 Rev. 2 rationale), the leak-canary harness
(`control/canary.py`, 13 live surfaces), `SecurityIncident`/`handle_security_boundary_
violation` (section 71), `TraceEventStore`/`trace_sanitizer` (sanitized, hash-chained
event stream). **Built, tested, but not wired into the live pipeline** (disclosed, not
silently missing — see AB): `FinalAssuranceGate`/`evaluate_final_assurance`,
`ReportGenerator`/`ZIPBuilder`/`IntegrityService`, `RunPrivacyPolicy` (never constructed),
artifact-lineage `invalidate_descendants`, `trace_projection`'s read-side projections,
`LearningService`'s human-governance approval layer, a large fraction of
`SuperOrchestrator`'s own published API (documented "forward-compatible hooks").

## H. Manager supervision model

Two separate, both-live authorities, neither replacing the other (a deliberate,
disclosed architectural reality, not an oversight — see `CLAUDE.md`'s "Supervision"
section for the full accounting): **Manager** (`ExecutionHealthSupervisor`) is a broker —
execution-health supervision (retries, timeouts, web-search grants, human-review
escalation) plus the guardian query broker (`ask_schema`/`ask_instrument`/`ask_lexicon`,
governed through `HandoffGateway`). **SuperOrchestrator** owns `workflow_runs` lifecycle
state (holds, run start/cancel, cleanup confirmation, opaque-map erasure, human-review
request creation, export-window bookkeeping, rewind routing — the last called live from
Reviewer Final's FAIL path).

## I. Handoff topology

`HandoffGateway`/`ALLOWED_EDGES` enforces a deny-by-default directed topology between
current agent names. Genuinely invoked today for Manager's guardian query broker (every
`ask_schema`/`ask_instrument`/`ask_lexicon` call is a real, governed handoff recorded as a
`TraceEvent`) — it has not yet become the mechanism routing the pipeline's own phase
transitions (a later, unscheduled phase, per `CLAUDE.md`'s own honest accounting).

## J. Raw-data security boundary

`SandboxManager` runs Executor's four raw-row-touching operations in an isolated
`multiprocessing` child: explicit env allowlist (not the pre-Phase-R broken substring
denylist), a drained result queue (fixes a 128 KiB+ payload deadlock), fail-closed memory
limits via `RLIMIT_AS` with an explicit override
(`PHI_SANDBOX_ALLOW_UNENFORCED_MEMORY`, required on macOS/Darwin, which cannot enforce
`RLIMIT_AS` at all), output byte caps, 0700-mode workspace. Disclosed limits (never
claimed otherwise): the socket monkeypatch stops accidental egress only, not a deliberate
bypass; same-uid filesystem means the sandboxed worker can still read `backend/.env`,
`~/.aws/credentials`, `~/.ssh/`.

## K. Provider boundary

`ProviderGateway` is the sole LLM egress point: secret-scan/block before dispatch,
data-class/capability-grant authorization (`AuthorizationService`/`CapabilityBroker`),
canary-scan against the live `CanarySet` before the provider call, sanitize-then-hash-chain
trace recording (`egress_digest`, never the raw payload). BYO-key model
(`APP_ENCRYPTION_KEY`-encrypted at rest); dev-mode auto-generates an ephemeral key,
disclosed as orphaning any prior ciphertext across a restart (`docs/RUNBOOK.md`'s
"Encryption-key rotation" section).

## L. Human Review flow

`POST /api/sessions/{sid}/human-review`: `client_event_id` (idempotency),
`actual_knowledge_ack` required except for a pure `defer` (exempt — deferring is not a
knowledge claim). Reviewer identity resolved server-side from `REVIEWER_PRINCIPALS`
(`reviewer`/`lead_reviewer`/`expert_determination` roles), never client-supplied; at least
one `expert_determination` principal required outside dev mode (45 CFR 164.514(b)(1)
cannot be self-authorized by any agent). The Auditor-era `confirm_auditor_confidence`
flow was removed Phase 17-C (dead after Auditor's retirement).

## M. Execution architecture

Executor applies the verified transformation plan (SEC-004 fail-closed: any column
present in source without a decision defaults to `drop`, override-able to `scrub_text` via
`PHI_UNMAPPED_COLUMN_ACTION`), sandboxed for raw-row operations, with a study-wide
`PseudonymRegistry` crossing the sandbox boundary as plain `(salt, map)` args.
`_neutralise_formula` prefixes spreadsheet-formula-shaped values so a downstream
spreadsheet application never executes an injected formula.

## N. Verification / rewind architecture

`DeterministicVerifier` runs between Executor and Reviewer Final (a plain class, not an
`Agent`). Reviewer Final may trigger one of five typed rewind routes
(`EXECUTION_ERROR`→`execute`, `METHOD_ERROR`/`REGULATION_ERROR`→`decide`,
`SEMANTIC_ERROR`→`specialists`, `UNRESOLVED_UNCERTAINTY`→stage-dependent human-review
target), routed via `RewindRouter`/`SuperOrchestrator.rewind`, escalated to Human Review
with the rewind decision recorded as context (automatic re-execution from an arbitrary
rewound checkpoint is explicitly out of scope, not implemented).

## O. Observability architecture

One sanitized `TraceEvent` stream (hash-chained, `status_text`/`retry_category` scrubbed
through `scrub_persisted_text`), with two intended read-side projections
(`user_agent_trace`/`maintainer_trace`) that are built and tested but have **zero live
endpoint caller today** — the live per-session trace surface
(`GET /api/sessions/{sid}/agent-trace`) reads `agent_log` directly, not through these
projections. Disclosed honestly in `CLAUDE.md`, not silently claimed as exposed.

## P. Reporting / export architecture

**The single most load-bearing finding of this entire rewrite, surfaced and independently
re-confirmed three separate times this session (Phase 17-C, Phase 18, and again while
building the Phase 20 harness):** the live export path
(`GET /api/sessions/{sid}/bundle`, `/export/{file_id}`, `/reversal-key`) is
`phi_core/bundle.py::build_bundle`, a self-contained ZIP assembler (own coverage
rendering, own attestation payload, own report generation, own hashing). **It is not
gated by `FinalAssuranceGate`.** `control/final_assurance.py::evaluate_final_assurance`
— the master-architecture-mandated (section 57), fifteen-condition, "model confidence
cannot override this gate" release gate — has zero live call sites anywhere in
`server.py`/`superorchestrator.py`/`agents/` (confirmed by direct grep every time this was
checked). This was disclosed by Phase 11a and Phase 12 at the time they built it
(deliberately deferred rather than force-wired, citing real regression risk across
~100+ existing download tests), never silently dropped, and remains open. `ReportGenerator`/
`ZIPBuilder`/`IntegrityService` (Phase 11b's own report-pipeline cluster, fully built,
"genuinely gate-verified" by its own extensive test suite) are downstream of the same gap
and are also not the live path. Publish Guard's residual-PHI scan **is** real and wired
into `build_bundle`'s own path — the gap is specifically the mandatory-per-spec
non-bypassable 15-condition gate, not an absence of every release-safety check.

## Q. Learning and cleanup architecture

Learning: two distinct halves. The automated candidate-generation pipeline (Phase 12:
`candidate → abstract → sanitize → PHI/PII scan → reconstruction check → policy
validation`, unsafe candidates deleted before reaching the safe store, no autonomous
self-modification — the runtime pipeline never imports `control.learning`) is live and
tested. `LearningService`'s human-governance approval/promotion layer
(`propose`/`approve`/`promote_rollout`) is built and tested but has zero endpoint caller —
no human can currently approve/promote a candidate through any API surface. Cleanup:
`CleanupManager` populates every `CleanupManifest` field, wired into `session_delete` and
every purge-loop step, never transitions to `SESSION_DESTROYED` until verification
succeeds or a cleanup incident is raised.

## R. Legacy components migrated

Sentinel's review logic → `Reviewer` + `deterministic_rules.py` (Phase 8). Operator's
deterministic verification → `DeterministicVerifier` (Phase 10). Statute/Praxis →
`RegulationsExpert`/`PHIMethodsExpert` (Phase 5/6, understood-then-renamed, not a
mechanical rename per the plan's explicit instruction). Publish Guard's release checks →
partially migrated into `ReportingSafetyGate` (Phase 11a), the rest still live in
`publish_guard.py` directly.

## S. Legacy components removed

`class Auditor` and its LLM re-derivation call (Phase 17-B); `confirm_auditor_confidence`
(the dead-end API field/UI panel it left behind, Phase 17-C); Scout/Ledger/Herald's
mandatory-path execution (relocated to opt-in, Phase 17-B); a duplicate SHA-256 hashing
helper, `MethodRegistry`-adjacent orphaned utilities, unused dependencies (`PyJWT`,
`python-jose`, `typer`, `typer-slim`, `passlib`, 4 unused frontend packages — Phase 17-C).
Full accounting with file:line evidence: `docs/PHASE_STATUS.md`'s Phase 17-C section.

## T. Files added / modified / deleted

271 commits, 220 files, +37,700/-5,093 lines across this rewrite (`main...HEAD`). Full
per-phase breakdown is `docs/PHASE_STATUS.md` itself — every phase section states its own
exact commit list and file set.

## U. Actual unit test results

Non-live backend suite (serial, canonical, this session's final state):
**3 failed, 1849 passed, 6 skipped, ~113s.** The 3 failures are the pre-existing,
documented `test_human_review_invariant.py` `client_event_id` schema-drift failures
(present since Step 0's baseline, out of scope for every phase after Phase 8 named them).
The 6th skip is the new `test_acceptance_phase20.py` module (correctly self-skipping
without `PHI_TEST_BASE_URL`). `ruff check .`: all checks passed.

## V. Actual integration test results

`test_agent_pair_integration.py` (11 tests, one per surviving agent-pair interaction,
built Phase 11b, the designated regression suite Phases 12-17 run against): passing.
Phase R's own architectural-invariant suite (`test_control_phaseR_integration.py`, 20
tests: 5 exclusivity scans + 5 behavioral, each with a positive control): passing. Live
suite (server up, `mongod` up, no provider key): **3 failed** (`test_agent_pipeline.py`'s
pipeline-completion-dependent tests — cannot pass without a real key, see AB) **+
`test_control_migrate.py`'s one independently-confirmed pre-existing, order-dependent
flake** (proven, this session, to reproduce identically against a brand-new database in
total isolation in the *working tree* too — not a fresh-clone or rewrite-introduced
defect; not this rewrite's code to fix). Full live baseline otherwise matches Step 0.

## W. Actual security/adversarial results

Phase 15b (9 adversarial categories, mapped to OWASP Top 10 for Agentic Applications 2026
and OWASP LLM Top 10 2025): 4 genuine defects found, each with a regression test recorded
under `KNOWN_XFAIL`, all 4 resolved Phase 17-A (verified passing, not suppressed). Phase
16 (evaluations against labeled synthetic cases): 9 harnesses, 4 genuine defects found and
recorded/resolved the same way. Section 71 (`SECURITY_BOUNDARY_VIOLATION` handling): 19
new tests, Phase 15a. This session's own Phase 17-C cleanup-audit independently
re-verified every one of these findings against live code before trusting any automated
report (the report's own methodology was shown to have real gaps: a live Pydantic safety
validator was nearly misclassified as dead code by the raw tool output — caught and
corrected before anything was applied).

## X. Leak-canary results

Wave R-d's harness (`control/canary.py`, 13 live surfaces: exports plus 8 non-export
surfaces — `trace_events`, `workflow_runs.opaque_map`, agent logs, `HandoffEnvelope`
payloads, learning store, research queries, errors, ZIP metadata): 21 tests, all passing,
zero false hits on a clean run. Phase 20's own harness extends this with
`scan_zip_contents_for_leaks` (ZIP member *content*, complementing the pre-existing
metadata-only scan) — built, ruff-clean, but **not yet run against a real completed
session** because no session can complete without a valid provider key (see AB).

## Y. Fresh-clone validation results

Phase 19, executed directly after two subagent-dispatch failures on this specific phase
(see AA): fresh `git clone`, fresh venv, fresh `npm install`, fresh never-before-used
database. Final clean run: **4 failed** (3 pre-existing baseline + 1 independently
reproduced pre-existing flake, matching U/V above) **, 1848 passed, 5 skipped**; root suite
85 failed/909 passed/3 skipped (exact Step 0 baseline, `phi_engine`, out of scope);
frontend 2 suites/5 passed. Two genuine, real gaps found and fixed in the working tree
during this phase (not artifacts of the clone): `.env.example` was missing 5 real
environment variables including the security-critical, boot-required
`REVIEWER_PRINCIPALS` (commit `bb90cac`); a sandbox-path test compared an unresolved path
string against a resolved one, passing only by coincidence when `DATA_DIR` has no symlink
component, failing under macOS's `/tmp → /private/tmp` (commit `0ed79c7`, production code
was never wrong, only the test's own assertion). All five explicit Phase 19 "verify"
checks (no hidden local dependency, no committed secret, no committed real PHI, no
generated junk, no obsolete runtime architecture) passed clean.

## Z. Authoritative standards sources used

45 CFR 164.514(b)(2)(i) (HIPAA Safe Harbor, the 18-category identifier list and the two
worked de-identification examples); 45 CFR 164.514(b)(1) (Expert Determination); 45 CFR
46 (Common Rule); 42 CFR Part 2 (SUD records); FERPA; 5 U.S.C. § 552a (Privacy Act); NIST
SP 800-88 Rev. 2 (September 2025, cryptographic-erase/sanitization guidance, superseding
Rev. 1 — verified as current at the time Phase 12 cited it); OWASP Top 10 for Agentic
Applications 2026 and OWASP LLM Top 10 2025 (Phase 15b's adversarial-category mapping).

## AA. Known limitations

1. **No valid LLM provider API key exists anywhere in this environment.** Directly
   verified via real API calls, not inferred: the one configured `OPENAI_API_KEY` returns
   HTTP 401 "Incorrect API key provided" from `api.openai.com`; `ANTHROPIC_API_KEY`,
   `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `OPENROUTER_API_KEY` are all empty. This is the
   **sole reason Phase 20's "live system test... an actual run, not a unit test" per the
   plan's own explicit framing cannot be completed in this environment.** Every symptom
   observed all session (`"pipeline failed"`, `"0 decisions"`, `RegulationsExpert` absent
   from trace) traces to this single root cause, confirmed via backend process logs during
   the Phase 20 harness's proof run: every LLM-dependent phase raises `RuntimeError`
   (the provider gateway rejecting the call), producing the deterministic downstream
   `ResultAcceptanceError`. **This requires the user to supply a valid key; it cannot be
   resolved by further engineering effort.** Everything reachable without a live key is
   complete: the corpus is extended exactly per the plan's three items (forms, prompt
   injection, dictionary PHI/semantic conflict — commits `2a91c5e`, `fb4f055`, `736b389`),
   and the full acceptance harness (`backend/tests/test_acceptance_phase20.py`, commit
   `3a2e6e0`) is built, ruff-clean, and proven to run correctly all the way to the exact
   provider-key boundary with zero bugs in the harness itself.
2. **`FinalAssuranceGate` does not gate the live export path** (see P above). Fully built,
   fully tested, genuinely a required section-57 deliverable — never wired into
   `build_bundle`. Disclosed at the time by the phases that built the surrounding
   infrastructure; independently re-confirmed three times this session.
3. Two subagent dispatches failed outright on long, open-ended, sequential tasks (Phase 18's
   first attempt: 1h35m pure investigation then crashed with zero commits; Phase 19's first
   two attempts: similar pattern) before this pattern was recognized and those two phases
   were completed by decomposing into smaller bounded dispatches (Phase 18) or executed
   directly (Phase 19). No data loss or corruption resulted; every retry started from a
   clean, verified git state.
4. The macOS/Darwin platform cannot enforce `RLIMIT_AS`; every sandbox-dependent test and
   live run in this environment ran with `PHI_SANDBOX_ALLOW_UNENFORCED_MEMORY=1`. Per the
   plan's own explicit instruction, **this alone binds the ceiling verdict to
   `IMPLEMENTATION_COMPLETE_WITH_LIMITATIONS` at best, never `IMPLEMENTATION_COMPLETE`,
   even independent of the provider-key blocker.**
5. `test_control_migrate.py::test_backfill_export_artifacts_round_trips_a_legacy_export`:
   a pre-existing, order-dependent flake in the full-suite run, independently proven this
   session (via isolated runs against a brand-new database, in both the fresh clone and
   the working tree) to be unrelated to this rewrite's changes — not touched, not this
   rewrite's defect.
6. `test_security_paths.py`/`test_security_llm_and_auth.py` always make real HTTP calls to
   whatever is listening at `PHI_TEST_BASE_URL`, regardless of "live"/"non-live" suite
   selection, and can show spurious rate-limit/decrypt failures if the long-running
   `phi-backend` process accumulates state across many verification passes in one session —
   root-caused, documented, and the fix is procedural (restart before a canonical gate),
   not a code change.

## AB. Residual risks

Every item below was investigated in depth this session (primarily Phase 17-C), is
explicitly disclosed rather than silently present, and represents a genuine architecture
or product decision outside this rewrite's own scope to resolve unilaterally — recorded
in full, with file:line evidence, in `docs/PHASE_STATUS.md`'s Phase 17-C `RESIDUAL_RISK`
section:

- `FinalAssuranceGate` not wired into the live export path (AA.2, P above) — the single
  highest-priority open item for whoever owns this repo next.
- `ReportGenerator`/`ZIPBuilder`/`IntegrityService` (Phase 11b's cluster) downstream of
  the same gap.
- `SuperOrchestrator`'s dormant API surface (`resume`, `dependencies_satisfied`,
  `observe_handoff`, `evaluate_handoff_budget`, `authorize_execution`, `begin_export`,
  `confirm_export`, `authorize_publication`) — confirmed, via Phase 4's own historical
  record, to be *deliberately designed* forward-compatible hooks, not accidental dead
  code; whether to finish wiring them or trim them back is a real architecture call.
- `LearningService`'s human-governance approval/promotion layer — no endpoint caller
  exists; a genuinely unstarted feature, not a regression.
- Artifact-lineage `invalidate_descendants` — built, tested, safety-relevant, zero
  production caller; a disclosed gap present since before this rewrite began (row 108 of
  the original pre-implementation audit) and never actually closed by any later phase.
- `trace_projection`'s read-side (`user_agent_trace`/`maintainer_trace`) — tested, zero
  live endpoint caller.
- `RunPrivacyPolicy` — a typed, frozen contract with zero live constructor call site.
- Naming leftovers in live code (not architecture): `control/policy.py`'s `Operator`
  manifest/`TEAMS` entry, `manager.py`'s `ROLES['Operator']` key, and `agent_log`'s
  `"operator"` phase-key label all survive despite the `Operator` *class* being fully
  retired Phase 10 — harmless (label only, no behavior), found and left alone twice
  (Phase 17-C, Phase 18), a genuine low-priority cleanup candidate for later.

---

## Final status

**`IMPLEMENTATION_BLOCKED`**

Every phase from Step 0 through Phase 19 is genuinely complete, independently verified
against live code and a live/fresh-clone environment, and recorded with full evidence in
`docs/PHASE_STATUS.md`. Phase 20 is complete for everything reachable without a live
provider key: the corpus is extended exactly per the plan's three named items, and a
complete, correct, ruff-clean acceptance harness is built and proven — by an actual run
against the live backend — to execute flawlessly through corpus generation, intake,
manifest linking, and session creation, failing at exactly and only the one point that
requires a real, valid LLM provider API key, which does not exist anywhere in this
environment (independently confirmed via direct provider API calls, not inferred from
symptoms).

**This blocker cannot be resolved by further engineering in this environment.** To reach
a final verdict:

1. Supply a genuinely valid API key for at least one provider (Anthropic, OpenAI, Gemini,
   or OpenRouter) via `backend/.env`'s `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`/
   `GEMINI_API_KEY`/`OPENROUTER_API_KEY`, or via the running app's own `Settings` UI
   (`POST /api/settings/llm`).
2. Restart `phi-backend` so it picks up the new key.
3. Run: `cd backend && DATA_DIR=<repo>/data PHI_TEST_BASE_URL=http://127.0.0.1:8001
   .venv/bin/python -m pytest tests/test_acceptance_phase20.py -q -p no:cacheprovider -p
   no:xdist -v --tb=long`.
4. Even a fully green run on this macOS development machine is bound to
   `IMPLEMENTATION_COMPLETE_WITH_LIMITATIONS`, never `IMPLEMENTATION_COMPLETE`, per the
   plan's own explicit rule (`PHI_SANDBOX_ALLOW_UNENFORCED_MEMORY=1` is required here; the
   memory ceiling cannot be enforced on Darwin — AA.4 above).
5. Independently, resolving `FinalAssuranceGate`'s non-integration into the live export
   path (AB above) is the most consequential remaining architecture decision for whoever
   owns this repo's continued development, regardless of the acceptance run's outcome.
