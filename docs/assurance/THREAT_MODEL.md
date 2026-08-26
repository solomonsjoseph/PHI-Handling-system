# Threat model

Phase 9 step 3. Covers the six trust boundaries the plan names: provider,
tool, human-review, workflow, cache, and artifact. `memory/ARCHITECTURE.md`
section 6 covers five earlier, request-level boundaries (HTTP API, intake
ZIP, model output, model input, export) and remains accurate; this
document is the control-plane layer built on top of it.

## Provider boundary

**Untrusted party:** every LLM provider (Anthropic, OpenAI, Gemini,
OpenRouter, ChatGPT OAuth) and the network path to it.

**Assets at risk:** the exact payload sent (must never carry a raw
dataset cell value or original filename); the provider's identity claim
(must match the grant's pinned provider/model/endpoint).

**Controls:**
- `ProviderGateway.complete` (`backend/phi_core/control/gateway.py`) is
  the sole production `litellm.completion` boundary; `CapabilityPolicy`
  re-validates provider/model/endpoint against the calling agent's
  manifest-derived `CapabilityGrant` on every call (`check_provider`),
  and a reply reporting a different provider/model than requested is
  denied.
- `restricted_phi` is refused unconditionally for every manifest
  (`CapabilityPolicy.check_data_class`); no agent, no exception.
- The exact sanitized payload is what gets hashed into `egress_digest`
  and persisted on the matching `TraceEvent` (D15), so a captured-payload
  audit can prove after the fact what was actually sent, not merely what
  the code intended to send.
- **Residual risk (unchanged since Phase 2, tracked in
  `docs/assurance/RISK_REGISTER.md`): no network-layer egress control
  (firewall/proxy allow-list, sidecar, service mesh) exists.** Every
  control above is application-layer; a compromised or buggy dependency
  making its own raw HTTP call from inside the process is not stopped by
  anything in this list. `test_control_gateway_egress.py`'s
  `test_raw_http_alternate_sdk_and_subprocess_bypass_rejected` is a
  static/runtime check that the codebase itself never does this, not a
  network-layer enforcement of it.

## Tool boundary

**Untrusted party:** web-search results and any future tool a manifest
grants.

**Assets at risk:** the tool result content reaching a downstream prompt
unverified; a non-research agent invoking a tool at all.

**Controls:**
- `AgentManifest.allowed_tools` is a deny-by-default map (`{tool: max
  uses}`); `CapabilityPolicy.check_tool` refuses both an ungranted tool
  and a tool whose per-grant use budget is exhausted.
- A tool result's citations are matched against `ToolEvent.citations`
  before anything downstream can treat a claim as tool-derived
  (`tool_derived_citations`); a model's own `sources` claim with no
  matching tool citation never reaches `VERIFIED` (`control/evidence.py`,
  D12).
- `security._is_private_ip` refuses a retrieval whose `final_redirect_url`
  resolves to a private IP, closing the redirect/DNS-rebinding path from
  a tool result back into the deployment's own network.
- Residual risk: the tool surface today is exactly one tool
  (`web_search`); the boundary is proven for that one tool and has not
  been exercised against a second, differently-shaped tool.

## Human-review boundary

**Untrusted party:** the reviewer's own free-text comment; a stale or
racing review submission.

**Assets at risk:** an unauthenticated principal resolving a review; a
model's own interpretation of a comment silently becoming the operative
decision; a resubmission being applied twice or against a superseded
request.

**Controls:**
- `session_human_review` requires both session ownership and a
  configured reviewer role (`reviewer_role`, `REVIEWER_PRINCIPALS`) --
  D13 step 1.
- Comment-mode resolution never auto-applies regardless of model
  confidence; every interpretation requires a second, explicit reviewer
  confirmation (D13 step 6).
- `client_event_id` is required and idempotency-checked against the
  durable `HumanReviewRequest`/`HumanReviewEvent` records; `audit_version`
  is required and must match the open request's own value whenever
  `confirm_auditor_confidence` is true (409 on mismatch).
- `SuperOrchestrator.supersede_human_review` closes a stale open request
  automatically whenever a new escalation targets the same run, so at
  most one open request per run ever exists -- a rerun's audit cannot be
  resolved against a decision an earlier, already-superseded audit made.
- The reviewer identity displayed and recorded is the authenticated
  principal from `GET /api/auth/whoami`, never an operator-typed or
  localStorage-persisted string (D13 step 8, closed in Phase 6).
- Residual risk (F-HITL-001): `auditor_escalation_reason`'s
  evidence-sufficiency and deterministic-gate-result grounds are not yet
  wired to a live data path from `EvidenceClaim`/`GateResult` into
  Auditor's context. `HumanReviewRequest.superseded_by` is always
  `"system"`; there is no path for a human to explicitly supersede a
  request outside the automatic new-escalation case.

## Workflow boundary

**Untrusted party:** a superseded, crashed, or zombie worker still
running past its lease.

**Assets at risk:** a duplicate or out-of-order state transition; a
terminal audit event published by a worker that no longer owns the run.

**Controls:**
- Every `WorkItem` transition (`claim`/`heartbeat`/`complete`/`fail`) is
  a fenced compare-and-set (`TaskService`); a lease reconciler
  (`reconcile_leases`) returns an expired lease to `ready`, bumping the
  fence so a zombie worker's later completion attempt loses the race.
- `SuperOrchestrator.advance` fences `WorkflowRun` node transitions on
  `(updated_at, node)`; a losing racer gets `WorkflowError`, never a
  silent overwrite.
- `TraceEventStore.append` requires and checks a fence against the
  emitting `WorkItem`'s current fence for any terminal outcome
  (`complete`/`failed`/`cancelled`/etc.), so a worker whose lease was
  reconciled away out from under it cannot publish the winning terminal
  audit event for a run another worker has since completed (D15).
- `create_child_work` enforces depth, fanout, per-parent and per-run
  parallelism, total task count, and budget-widen checks before any
  child task is enqueued; every refusal is recorded as a `TraceEvent`
  (`outcome="budget_exceeded"`).
- Residual risk (F-ORCH-001): `control/activation.py::ActivationFactory.activate`
  remains a documented, interim direct `TaskService.enqueue` caller
  outside `SuperOrchestrator` -- the ~20-activation migration to make
  every individual agent activation itself a durable, fenced child task
  was not undertaken.

## Cache boundary

**Untrusted party:** a stale cache entry from a policy version that has
since changed; a malformed persisted document.

**Assets at risk:** research content served past the point its
provenance or policy basis is still valid.

**Controls:**
- `StoreResearchCache` (D16) stamps every entry with the `POLICY_VERSION`
  active when it was written and an `evidence_state` derived from
  `source` (`UNVERIFIED` for a tool-backed source, `UNKNOWN` otherwise --
  never a caller-asserted `VERIFIED`); `get` treats a policy-version
  mismatch, a missing/malformed `fetched_at`, or an entry older than
  `WEB_CACHE_REFRESH_DAYS` as a miss rather than serving it.
- `fetched_at` is a native BSON Date, giving `web_cache` a real Mongo TTL
  index (`expireAfterSeconds`) in addition to the application-level
  staleness check -- a stale entry cannot accumulate indefinitely even if
  the application-level check were ever bypassed.
- Residual risk: cache invalidation on a `POLICY_VERSION` bump is coarse
  (every entry misses at once, no selective invalidation by topic); this
  is a deliberate simplicity tradeoff, not yet exercised at production
  cache volume.

## Artifact boundary

**Untrusted party:** a crash between staging and finalizing a write; a
concurrent external filesystem change; a stale generation reference.

**Assets at risk:** a partially-written file being served; a download
served against a hash that no longer matches what is on disk; PHI
retained past its retention window; a failed erasure being silently
reported as successful.

**Controls:**
- Two-phase `stage()`/`finalize()` (D14): a crash before `finalize()`
  leaves the record `provisional` and no bytes at the final path -- there
  is nothing to promote.
- `open_for_download` refuses on a stale state, a generation mismatch
  against the current `PublicationPointer`, or an on-disk hash that no
  longer matches the recorded `sha256`.
- `ArtifactService.reconcile` (Phase 7) collects `rejected`/`superseded`
  records, stale `provisional` records past a staleness window, and
  dangling records whose file has already vanished -- a database
  tombstone is removed only after filesystem deletion is confirmed; a
  failure increments `delete_attempts` and is retried, never silently
  dropped.
- Every retention timer (terminal-session purge, review-retention
  expiry, artifact reconciliation) checks `ArtifactRecord.hold` /
  `WorkflowRun.hold` and skips while either is non-empty (D14 legal
  hold).
- `session_delete` and the terminal-state retention sweep both record
  `status="erasure_pending"` with the exact filesystem error on a failed
  deletion, rather than either deleting the session document with bytes
  still on disk or silently swallowing the failure -- retried
  automatically every hour.
- Residual risk: `rejected`/`superseded` `ArtifactRecord` states have no
  current producer (Operator, Reviewer, and Publish Guard reject
  *decisions*, not artifacts, today); `reconcile`'s handling of those
  states is exercised directly in tests against a hand-built record, not
  yet against a live production write path.

## Controlled-learning boundary (D16, inactive by default)

**Untrusted party:** a running task's own proposal input.

**Assets at risk:** runtime behavior being altered by content the system
itself produced, without human review.

**Controls:**
- `LEARNING_ENABLED` defaults `false`; every `LearningService` method
  refuses outright (`learning_disabled`) unless it is set.
- No `phi_core/agents/` module imports `control.learning` at all (static
  AST scan, `test_no_agents_module_imports_control_learning`); no
  `AgentContext` field or `Agent` attribute exposes a raw `ControlStore`
  that could write `learning_proposals`/`learning_activations`/
  `capability_grants` (`test_no_agents_module_writes_learning_or_capability_collections`,
  `test_agents_receive_no_database_handle`).
- Activation requires a recorded passing offline evaluation *and* a
  separate passing adversarial evaluation, plus an authorized
  `lead_reviewer` approval. A canary meeting its own criteria still needs
  the monitor's continued pass before promotion to `full`. A monitor trip
  halts and reverts without human action, restoring the prior good
  activation for the same target.
- `Manager` coaching (`_notes_that_worked`) is a plain per-instance
  dictionary, never seeded from a durable store; two `Manager` instances
  against the same session share nothing (`test_manager_coaching_state_is_never_seeded_from_a_durable_store`).

## Cross-cutting: audit-trail integrity

`TraceEventStore` (D15) is the sole writer of `trace_events`
(`test_only_trace_event_store_writes_trace_events`, a static AST scan).
Every event chains `prev_hash`/`hash`
(`hash = sha256(prev_hash + canonical_json(event minus hash))`), so
mutation, deletion, or sequence reuse in the underlying collection is
detectable by walking the chain. A sealed `trace_segments` range can only
be purged with a `trace_purge_tombstones` row recorded first, carrying
the segment's own hash -- a gap in the sequence with no matching
tombstone is evidence of tampering, not authorized retention.
