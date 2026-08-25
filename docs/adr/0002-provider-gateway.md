# 0002: Provider gateway as the sole production inference boundary

## Status

Accepted

## Context

Before this work, `backend/phi_core/agents/llm.py` called `litellm.completion` directly from two sites, and `Agent.call*` logged a separately scrubbed prompt surrogate distinct from the raw prompt actually sent to the provider. Web-search escalation defaulted to allowed for every agent. Non-Anthropic providers silently fell back to a plain completion when a caller requested `web_search`, so a denied capability could still return an answer with no citations and no record that research had not actually happened.

## Decision

`backend/phi_core/control/gateway.py` is the only production module that imports `litellm`, aside from the allow-listed `chatgpt_auth.py` OAuth device-code constants. `ProviderGateway.complete` implements the fixed twelve-step sequence in D6: grant load and triple revalidation, lease and cancellation check, single-DataClass input resolution with `restricted_phi` refused unconditionally, provider/model/endpoint/tool revalidation, canonical message construction, `scrub_for_prompt` over every string leaf, budget enforcement, response provider/model cross-check, keyed egress digest storage, error-text scrubbing, and a late-result fence check.

`control/egress.py` computes `egress_digest` as an HMAC-SHA256 over the canonical JSON payload under a key derived by `crypto.egress_digest_key()`, so a stored digest cannot be forged from guessed content and binds the exact sanitized payload, not a separately reconstructed surrogate.

`control/opaque.py` maps canonical identifiers (file id, column, artifact id, task id) to per-run opaque tokens with `OpaqueMap.to_opaque`/`from_opaque`; a sanitized display label never carries decision identity.

`ToolGateway.search` has exactly two outcomes: `provider == "anthropic"` uses the native `web_search_20250305` tool, and every other provider returns `status="denied"`, `denial_reason="tool_unsupported_by_provider"`. The two silent completion fallbacks previously in `call_llm_with_web_search` are deleted.

`agents/llm.py` keeps `LlmConfig`, `from_dict`, `_default_provider`, `_chatgpt_account_connected`, and `parse_json`; it no longer imports `litellm` or exposes `call_llm`/`call_llm_with_web_search`.

`Agent.__init__` takes `AgentContext` (session, run, task, attempt, grant, gateway, tools, trace, cache) instead of `(session_id, llm, db, emit, manager)`. `self.db` and `self.llm` no longer exist on `Agent`. `control/testing.py::make_ctx` is the single test factory, backed by `FakeGateway` and in-memory trace/cache implementations, replacing the eleven test files that previously passed `db=`.

`control/context.py::StoreTraceWriter` preserves the legacy `agent_log` write on every call for exactly one phase (Phase 7 removes it) while also emitting the new `TraceEvent` record.

`control/runs.py::RunStore` and `control/tasks.py::TaskService` provide run and task identity in their near-final form so every gateway request carries a real `run_id`, `task_id`, `grant_id`, and policy version instead of a placeholder. `control/activation.py::ActivationFactory` is the single call site that resolves a manifest, issues a `CapabilityGrant`, claims a `WorkItem`, and builds an `AgentContext` together; it is not named in D1's module list but is an ordinary implementation choice under D1's existing `tasks.py`/`policy.py`/`context.py` boundary, not a new architectural surface, so it does not warrant its own ADR.

## Consequences

- Every provider call is traceable to a persisted grant, run, and task; a call with no grant or a mismatched provider/model/endpoint is refused before any network request is built.
- Tests that previously constructed agents with a raw Motor database handle now construct a task-scoped `AgentContext`; a database handle is structurally unreachable from agent code.
- The two-phase "log a scrubbed surrogate, send the raw prompt" pattern is gone: the digest binds the payload that is actually sent.
- `Manager.run_supervised`'s escalated-attempt closure is now built only when the manifest grants the tool; the previous default-allow web-search escalation is a deny-by-default capability check instead.
