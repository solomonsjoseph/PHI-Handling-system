# Pre-implementation audit

Scope: PHI Console (`backend/`/`frontend`) on branch `chore/pre-implementation-audit`, forked
from `main` at commit `dcec23a`. This establishes repository/architecture truth before any
production migration, per the pre-implementation process the user requested. It does not change
agent behavior, orchestration, or execution logic; the only source edit is one clarifying
sentence in `CLAUDE.md` (see Section 5).

## 1. Repository identity

- Repository: `github.com/solomonsjoseph/PHI-Handling-system`
- Base branch: `main`, HEAD `dcec23a` (clean working tree, no untracked files, no submodules)
- Audit branch: `chore/pre-implementation-audit`, forked from that HEAD

A second branch, `feat/agent-design-docs`, exists with a genuinely divergent history from the
same ancestor (`c7ab628`): 199 commits not on `main`, deleting `phi_engine/` entirely and
rewriting `CLAUDE.md`/`README.md`/`AUTHORITY_MATRIX.md`, against `main`'s 62 commits not on that
branch (including the `phi_engine` package and the PR #15 control-plane merge). Per direction,
`main` is the confirmed baseline; `feat/agent-design-docs` is treated as stale/unmerged and is not
reconciled in this pass.

## 2. Instruction hierarchy

Only root-level instruction files exist: `CLAUDE.md`, `README.md`, `SECURITY.md`, plus
`docs/{THREAT_MODEL,RUNBOOK,MIGRATION,adr/0001-0008-*}.md`. No nested `CLAUDE.md`/`AGENTS.md`
exists anywhere under `backend/`, `frontend/`, or `phi_engine/`. `.claude/settings.json` only pins
a model, no instruction content. `.github/` has only `workflows/ci.yml`, no Copilot/agent
instruction files. `docs/AGENT_ARCHITECTURE.md` exists only on `feat/agent-design-docs`, never
merged to `main`.

No conflicting or stale instruction files were found on `main`.

`INSTRUCTION_BASELINE_STATUS = ALIGNED`

## 3. Current executable architecture

```
Browser (Wizard.jsx / SessionDetail.jsx)
  -> POST /api/sessions/{sid}/intake      (manifest v3 validation, fail-closed)
  -> POST /api/sessions/{sid}/handle      (launch pipeline)
       -> orchestrator.py::run_pipeline()
            t=0 parallel: Statute + Praxis(17 categories) + [Lexicon | Schema | Instrument]
            Judge <-> Sentinel loop (1..3 iterations, LLM + deterministic hard-rule gates)
            execute_decisions():
              Executor (deterministic, sole writer)
              -> Operator (deterministic, verify-only)
              -> Reviewer (deterministic, verify-only)
              -> Publish Guard (deterministic residual-PHI scan)
              -> [Scout backgrounded from here] -> Auditor (LLM, re-derivation + confidence gate)
              -> Ledger (LLM, benchmark rollup)
              -> Herald (LLM, manuscript draft)
  -> GET /api/sessions/{sid}/bundle       (download; only if Publish Guard clean)
  -> DELETE /api/sessions/{sid}           (right-to-erasure)
```

One `Manager` instance wraps the whole run as a supervisory sidecar: LLM-backed for
execution-health decisions only (retry/extend/web-search/escalate), with its own `Agent` prompt
explicitly walling it off from PHI/decision content. Agent lifecycle (activation, completion,
acceptance) is owned by `ActivationFactory`, called from the orchestrator, not by Manager or the
agents themselves. There is one lifecycle owner (`orchestrator.py::run_pipeline` /
`execute_decisions`), not competing orchestrators.

## 4. Agent inventory

| Agent | File | LLM-backed? | Trigger / concurrency |
|---|---|---|---|
| Manager | `agents/manager.py` | Yes (health decisions only) | Created first, threaded through whole run |
| Lexicon | `agents/specialists.py` | Yes | Parallel gather w/ Schema+Instrument at t=0, if `dict_files` present |
| Schema | `agents/specialists.py` | No (`PROMPT=""`) | Same parallel gather, if `dataset_files` present |
| Instrument | `agents/specialists.py` | Yes | Same parallel gather, if `form_files` present |
| Statute | `agents/experts.py` | Yes (web-search + deterministic fallback) | `asyncio.create_task` at t=0, awaited after specialists |
| Praxis | `agents/experts.py` | Yes (per-category, deterministic fallback) | `asyncio.gather` over 17 categories at t=0 |
| Judge | `agents/reasoning.py` | Yes | Sequential, inside iteration loop |
| Sentinel | `agents/reasoning.py` | Yes | Sequential, right after Judge each iteration |
| Executor | `agents/reasoning.py` | No (`PROMPT=""`) | Sequential, first in `execute_decisions`; sole file-writer |
| Operator | `agents/operator.py` | No | Sequential, right after Executor; verify-only |
| Reviewer | `agents/reviewer.py` | No | Sequential, right after Operator; verify-only |
| Auditor | `agents/reasoning.py` | Yes | Parallel-gathered with Scout after Publish Guard clears |
| Scout | `agents/outward.py` | Yes (web search) | Backgrounded right after Executor, joined with Auditor |
| Ledger | `agents/outward.py` (Compare+Aggregate) | Yes | Sequential, after Auditor/Scout resolve |
| Herald | `agents/outward.py` (Abstract+Sections) | Yes | Sequential, last; split to stay under 90s timeout |

Deterministic gate (no LLM, not an "agent" per se): Publish Guard, scans every export byte before
authorizing download.

## 5. Data-flow / raw-row-read findings

The zero-row-read invariant holds for patient dataset rows: no confirmed path from a patient
dataset row value to an LLM prompt. `file_readers.py`'s header/count-only readers
(`read_csv_columns`, `read_xlsx_columns`, `read_parquet_columns`, `column_value_stats`) feed
Schema, which never calls an LLM. `iter_dataset_rows` (the one function that reads full row
values) has exactly three callers, all deterministic/non-LLM: Operator's `_read_columns`, a
deterministic `verify_keep_decisions` function in `reasoning.py`, and Publish Guard.

One named, already-mitigated exception: `Lexicon` (`backend/phi_core/agents/specialists.py:84-147`)
sends data-dictionary/codebook row text (column labels and descriptions from the dictionary file,
not the patient dataset) to the LLM, after `scrub_for_prompt(raw_row, detectors=("rule",))`
redacts identifiers. `CLAUDE.md`'s invariant paragraph has been updated with one sentence
documenting this exception (this audit's only source-code-adjacent edit).

## 6. Human review, execution, cleanup

**Human review is real, not LLM-simulated.** `POST /api/sessions/{sid}/human-review`
(`backend/server.py:3011-3072`) requires an authenticated principal with a reviewer role (403
otherwise); the reviewer identity is always the resolved principal, never client-supplied text
(comment at `server.py:2950-2952`). A server-generated timestamp is stamped as `reviewed_at` on
every resolved decision, and durably recorded on a `HumanReviewEvent`
(`phi_core/control/records.py:372-388`) with required, non-optional `principal`/`submitted_at`
fields. Resumption re-enters the same `execute_decisions` function used for a fresh run
(`orchestrator.py:198-230`), not a separate simulated path.

**Executor is the sole writer.** Only `Executor.run()` (`reasoning.py:1178`) calls
`tmp_path.write_text`, staged through `ArtifactWriter`/`ArtifactRecord`. Operator
(`operator.py:104,130`) and Reviewer (`reviewer.py:41,71,135`) contain no write calls: they only
read Executor's output and verify it against approved decisions.

**Cleanup covers both explicit deletion and crash/abandonment.** `DELETE /api/sessions/{sid}`
(`server.py:1014-1080`) tombstones the session, cancels any running workflow, erases registered
artifacts, removes the raw upload/export filesystem tree, and only then deletes the corresponding
Mongo rows (`agent_log`, `trace_events`, `sessions`). If filesystem erasure fails, the session is
marked `erasure_pending` with an attempt count, retried by a background loop rather than silently
succeeding. That same loop also purges sessions independent of any explicit delete: terminal
sessions past `RETENTION_DAYS`, and stalled `awaiting_human_review` sessions past
`REVIEW_RETENTION_DAYS` (raw PHI removed via `shutil.rmtree` even though never explicitly
deleted). Cleanup is not happy-path-only.

## 7. Provider / egress inventory

All LLM inference funnels through exactly one call site: `ProviderGateway.complete` in
`backend/phi_core/control/gateway.py` (`litellm.completion`, line 397). `agents/llm.py` is
config/parsing only, its own docstring states inference is "intentionally implemented only by
`phi_core.control.gateway`." Every agent reaches the gateway through the shared base class
(`agents/base.py:245`, `Agent.call` -> `self.ctx.gateway.complete`). A repo-wide search for direct
`litellm`/`openai.`/`anthropic.Client` usage outside `control/gateway.py` found no hits in
`experts.py`, `reasoning.py`, `specialists.py`, `outward.py`, `manager.py`, `operator.py`,
`orchestrator.py`, or `reviewer.py`.

## 8. Trace/logging privacy

The legacy `agent_log` collection is retired: `control/migrate.py:274-321` migrates any remaining
rows into `trace_events` and deletes the source. Current writes go through `Agent._log`
(`base.py:164-219`) into `TraceEvent` (`control/records.py:447-499`), whose schema has no field for
raw prompt/completion text, only provider/model/endpoint, usage/cost/latency, tool
request/exec/result status, data-class, outcome, egress digest, and gateway decision. Every
`payload=` call site scanned across `agents/*.py` carries only counts, error strings/types, and
identifiers (e.g. `{"identifiers_removed": n}`, `{"header_count": len(headers)}`), never raw LLM
text. One caveat: a comment in `records.py:457-465` references future intent for these fields to
support reconstructing a full prompt/reply trace panel; no such implementation exists today, so
this is aspirational, not a present leak.

## 9. Standards verification (external sources, retrieved 2026-08-26)

- **45 CFR 164.514 Safe Harbor / Expert Determination**: current guidance confirms the two-method
  structure (Safe Harbor = remove 18 identifier categories + no actual knowledge of
  re-identifiability; Expert Determination = statistical/scientific determination of very small
  re-identification risk). No indication the repo's authority citations
  (`authorities/01_hipaa_164_514_full.md`, `authorities/AUTHORITY_MATRIX.md`) are stale against
  this. (eCFR's direct page blocked automated fetch behind a bot-check redirect; used indirect
  search-result corroboration instead of following that redirect.)
- **OWASP Top 10 for LLM Applications (2025)**: Sensitive Information Disclosure moved to rank 2
  (LLM02:2025) and Excessive Agency is ranked 6 (LLM06:2025). Relevant to this pipeline: the
  Manager's explicit wall-off from PHI/decision content and the single-gateway egress choke point
  (Section 7) are structural mitigations already in place against both; nothing found in this
  audit suggests either risk is currently unmitigated in the reviewed code paths.

Sources:
- [OWASP Top 10 for LLM Applications 2025](https://genai.owasp.org/resource/owasp-top-10-for-llm-applications-2025/)
- [45 CFR 164.514 Explained](https://www.accountablehq.com/post/45-cfr-164-514-explained-hipaa-s-rules-on-de-identification-re-identification-and-limited-data-sets)

## 10. Baseline test results

Locally: NOT RUN. `PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider tests/` was
launched with a 600s timeout and produced zero output before being killed (exit 143). A direct
`pymongo` ping against the configured `MONGO_URL` failed with `ServerSelectionTimeoutError`,
confirming the suite was hung waiting on a database connection, not failing on logic. This audit
did not fabricate pass/fail counts in place of that unavailable evidence.

Via CI (`.github/workflows/ci.yml`, which provisions a real Mongo service), on this PR
(`gh pr checks 16`, run `33029652662`, all jobs green):

- `backend` job (`Test` step): 884 passed, 28 skipped, 4 warnings, in 88.95s
- `test` job (`Run test suite`): 996 passed, in 71.28s
- `xls-isolation` job, pandas 2.2.3: 54 passed, in 13.03s
- `xls-isolation` job, pandas 3.0.5: 54 passed, in 12.09s
- `credentialed` and `frontend` jobs: pass

No failures across any job. This is the real baseline evidence the pre-implementation prompt's
Section 57 requires, superseding the local NOT RUN result above.

## 11. Gap list against target architecture

The target architecture described in the pre-implementation prompt (single lifecycle owner,
header-only specialists, Judge classifier, on-demand research experts, real human review, sole
Executor, deterministic residual guard, sanitized trace, session destruction) is already largely
satisfied by `main` as audited above. Real gaps found:

- **CLAUDE.md wording gap (fixed this pass)**: the zero-row-read invariant paragraph didn't
  mention the Lexicon/dictionary-row exception, even though the exception is scrubbed and
  intentional. One sentence added (Section 5).
- **Trace-panel intent vs. implementation gap**: `control/records.py` comments describe future
  reconstruction of a full prompt/reply trace view; no such view exists today. Not a leak, but
  worth resolving explicitly (delete the comment, or implement it deliberately with its own
  redaction pass) before anyone builds against the comment's stated intent.
- **No local Mongo available in this sandbox**: blocked running the baseline suite directly here;
  resolved by pulling real pass/fail counts from this PR's CI run instead (Section 10). Not a
  repository defect, an environment gap for this audit session specifically.

No agent-role duplication, no competing orchestrators, no raw-PHI trace leakage, and no provider
bypass were found. The corresponding sections of the pre-implementation prompt (raw-row access,
provider inventory, trace privacy, human-review authenticity, cleanup/retention) are satisfied as
implemented.

## 12. Status

`PRE_IMPLEMENTATION_STATUS = READY`

No blocking unknowns remain for the items audited in this pass (instruction alignment, agent
inventory, raw-row-read invariant, human review authenticity, execution/write ownership,
cleanup/retention, provider egress, trace privacy, applicable standards). The one open item
(trace-panel comment vs. implementation, Section 11) is a documentation/decision gap, not a
blocker: it doesn't contradict current behavior, it describes unbuilt future behavior.
