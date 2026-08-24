# PHI agent architecture flowchart checklist

No external tracker is designated for this documentation effort.

- [ ] Step 1: Create `docs/AGENT_ARCHITECTURE.md` with the fixed 13-section order and a code-anchor table populated before prose.
  - Acceptance criteria: The document has all required sections, every later claim can point to a code-anchor row, and it records that no checkpoint object exists.
  - Verification: Confirm each anchor path and symbol resolves in the repository.

- [ ] Step 2: Add Levels 0 through 2 for system context, orchestration, and generic agent calls.
  - Acceptance criteria: Level 0 shows transport and the untrusted provider boundary, Level 1 shows all pipeline phases and real loop predicates, and Level 2 includes per-agent contracts for the roster, Manager, Operator, Reviewer, and four subagents.
  - Verification: Render the three Mermaid blocks and compare every loop, exit, and escalation edge with its cited code anchor.

- [ ] Step 3: Add Level 3 Manager supervision.
  - Acceptance criteria: The state diagram includes retries, 2 s and 5 s backoff, legal timeout and web-search transitions, coaching reuse, recovery, escalation, and attempt-3 exhaustion without another Manager LLM decision.
  - Verification: Check the diagram and supervision prose against `backend/phi_core/agents/manager.py` and `backend/phi_core/agents/base.py`.

- [ ] Step 4: Add Level 4 Operator and Reviewer coverage audit.
  - Acceptance criteria: The flow explains completeness, action-shape checks, Reviewer batching, dropped exports, and the `omit_by_file` header gap that only Reviewer detects.
  - Verification: Check the diagram and claims against `backend/phi_core/agents/operator.py`, `backend/phi_core/agents/reviewer.py`, and `backend/phi_core/agents/orchestrator.py`.

- [ ] Step 5: Add Level 5 human review, PHI boundary, failure matrix, and tunables.
  - Acceptance criteria: The sequence diagram covers SSE-triggered refetch, accepted modes, comment interpretation, deterministic re-gating, hard-rule precedence, paused review, and tail-only resume. The PHI diagram states both documented caveats. The matrix contains all required failure paths and the tunables table distinguishes session settings from hardcoded values.
  - Verification: Trace the five reader-test scenarios through the completed document, then confirm review routes, status strings, and bounds against source.

- [ ] Step 6: Reconcile `memory/ARCHITECTURE.md`.
  - Acceptance criteria: The first paragraph states the 12 roster agents under Manager with Operator and Reviewer verification stages and links to the detailed document. The loop uses `ITERATION_CAP = 3` and `max(iteration_cap, BLOCKING_ISSUE_FLOOR)`. The census includes Manager, Operator, and Reviewer while excluding the four subagents.
  - Verification: Inspect the three edited regions and confirm no other section of `memory/ARCHITECTURE.md` changed.
