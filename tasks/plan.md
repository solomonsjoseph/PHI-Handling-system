# PHI agent architecture flowchart implementation plan

> For agentic workers: REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a code-anchored architectural explanation and six Mermaid diagrams for the PHI agent pipeline, then reconcile the existing architecture reference.

**Architecture:** `docs/AGENT_ARCHITECTURE.md` is the detailed authority for agent interactions, supervision, review, and PHI boundaries. It uses Mermaid because the terminal renders it and it diffs as text. Executable code is authoritative when it differs from `memory/ARCHITECTURE.md`.

**Tech Stack:** Markdown, Mermaid, Python source anchors, FastAPI, React.

**Spec:** `local://agent-architecture-flowchart-plan.md`

## Overview

The deliverable documents the existing pipeline without changing behavior. It covers the transport boundary, the 12-agent roster, Manager supervision, deterministic verification, human review, failure paths, and tunables. Claims must resolve to current symbols and routes.

## Global constraints

- Do not change application behavior, tests, configuration, or generated assets.
- Keep the six diagrams in Mermaid: Levels 0 through 5.
- Cite repository paths and executable symbols. Do not infer mechanics absent from the source.
- State the two PHI caveats accurately: base-layer prompt scrubbing protects persisted logs rather than provider traffic, and Sentinel's raw approved default is closed by the subsequent call-failure gate.
- Use sentence-case headings, straight quotes, and no em dashes or en dashes.
- Do not modify `memory/ARCHITECTURE.md` beyond Task 6's three scoped corrections.

---

## Architecture decisions

- Mermaid is the diagram format because it renders in the terminal and remains reviewable as text.
- `docs/AGENT_ARCHITECTURE.md` is the detailed interaction map. `memory/ARCHITECTURE.md` remains the concise reference and links to the detailed document.
- Six diagram levels separate system transport, orchestration, generic agent calls, Manager supervision, deterministic coverage checks, and human review. This prevents one diagram from obscuring failure and resume paths.

## Task list

### Task 1: Create the architecture document skeleton and code-anchor table

**Files:**
- Create: `docs/AGENT_ARCHITECTURE.md`

**Interfaces:**
- Produces the fixed section order and the code-anchor table consumed by every later section.

- [ ] Create the 13 required sections in their fixed order: scope, system context, census, orchestration, generic agent contract, Manager, coverage audit, human review, PHI boundary, failure matrix, tunables, code anchors, and code divergences.
- [ ] Fill the code-anchor table from the approved module and symbol inventory before writing explanatory prose.
- [ ] Record that no checkpoint object exists in `backend/`, rather than diagramming a checkpoint.
- [ ] Verify every anchor names an existing path and symbol.

### Task 2: Document Levels 0 through 2

**Files:**
- Modify: `docs/AGENT_ARCHITECTURE.md`

**Interfaces:**
- Consumes the Task 1 anchor table.
- Produces the system context, orchestration-loop, and generic-agent-call diagrams with agent contracts.

- [ ] Add Level 0 as a `flowchart LR` covering Wizard, SessionDetail, FastAPI, `run_pipeline`, the 12-agent pool, Manager, untrusted providers, Mongo collections, export files, and bundle transport.
- [ ] Add Level 1 as a `flowchart TB` covering the parallel opening wave, Judge and Sentinel loop, human-review gate, Executor through Reviewer, Publish Guard, Auditor and Scout, Ledger, Herald, and closeout.
- [ ] Label Level 1 loop, exit, escalation, dropped-export, publish-block, and resumed-tail edges with the approved predicates and source anchors.
- [ ] Add Level 2 as a `flowchart TB` for scoped prompt construction, call-site scrubbing, `Agent.call`, parsing, validation, and declared defaults. Add the per-agent contracts for the roster, Manager, Operator, Reviewer, and four subagents.
- [ ] Verify Mermaid syntax and compare every Level 0 through 2 edge predicate with its cited code anchor.

## Checkpoint after Task 2

Review the first three diagrams against the source before adding supervisory and review detail. Confirm the agent census, transport boundaries, loop exits, and fallback paths do not assert undocumented behavior.

### Task 3: Document Level 3 Manager supervision

**Files:**
- Modify: `docs/AGENT_ARCHITECTURE.md`

**Interfaces:**
- Consumes Manager, `Agent.call`, and guardian-broker anchors.
- Produces the supervised-call state diagram and the supervision contract.

- [ ] Add Level 3 as a `stateDiagram-v2` with attempts, failure kinds, legal actions, retry backoff, recovery, escalation, and attempts-exhausted behavior.
- [ ] Put the timeout-extension and web-search legality conditions on transitions.
- [ ] Document coaching-note insertion, successful-note reuse, and the fact that attempt 3 skips the Manager LLM decision.
- [ ] Describe all three advisory `consult` sites, their fail-open behavior, the deterministic Auditor escalation path, and the count-and-enum supervision payload with the guardian-broker exception.
- [ ] Verify state transitions and all cited constants against `manager.py` and `base.py`.

### Task 4: Document Level 4 deterministic coverage audit

**Files:**
- Modify: `docs/AGENT_ARCHITECTURE.md`

**Interfaces:**
- Consumes Executor, Operator, Reviewer, and batching anchors.
- Produces the execution-verification diagram and coverage-gap explanation.

- [ ] Add Level 4 as a `flowchart TB` from Executor output through Operator completeness and shape checks to Reviewer coverage checks.
- [ ] State Operator's per-action shape checks and per-column verdict output.
- [ ] State Reviewer batching as `run_batched(..., batch_size=8, pool_size=6)` and its `clean` or `issues` result.
- [ ] Explain that Reviewer detects an `omit_by_file` column left in a written header because Operator skips omitted columns, and state that the current orchestrator passes `omit_by_file=None`.
- [ ] Verify the diagram's order and coverage-gap claim against `operator.py`, `reviewer.py`, and `orchestrator.py`.

### Task 5: Document Level 5, PHI boundary, failures, and tunables

**Files:**
- Modify: `docs/AGENT_ARCHITECTURE.md`

**Interfaces:**
- Consumes server, review UI, deterministic-gate, and configuration anchors.
- Produces the human-review and PHI-boundary diagrams, the failure matrix, and tunables reference.

- [ ] Add Level 5 as a `sequenceDiagram` for SSE refresh, dataset-file glance, approve, comment, defer, comment interpretation, re-gating, still-awaiting behavior, and tail-only resume with a new `_pipeline_run_id`.
- [ ] State that approve and defer invoke no agent, comment invokes one `Judge.resolve_comment` per commented column, no reject mode exists, and resumed review does not rerun Judge or Sentinel.
- [ ] Add the PHI-boundary `flowchart LR` with header and metadata prompt projections, in-process value readers, call-site scrubbing, and both documented caveats.
- [ ] Add the required failure and escalation matrix plus the session and hardcoded tunables reference.
- [ ] Verify review routes, resolution modes, deterministic hard-rule precedence, failure status strings, and tunable bounds against current source.

## Checkpoint after Task 5

Use the completed document to trace the five required reader-test scenarios. Confirm each is an unambiguous diagram path and reconcile any wording that contradicts the code before touching the concise reference.

### Task 6: Reconcile the concise architecture reference

**Files:**
- Modify: `memory/ARCHITECTURE.md`

**Interfaces:**
- Consumes the completed detailed document and the source-backed census and loop-bound facts.
- Produces a concise reference that does not contradict the detailed architecture document.

- [ ] Change the first end-to-end-flow paragraph to describe 12 roster agents under a Manager with Operator and Reviewer as deterministic verification stages, and link to `docs/AGENT_ARCHITECTURE.md`.
- [ ] Correct the Judge and Sentinel loop to `ITERATION_CAP = 3` with effective bound `max(iteration_cap, BLOCKING_ISSUE_FLOOR)`.
- [ ] Correct the census to the 12 roster agents plus Manager, Operator, and Reviewer, while keeping the four Ledger and Herald subagents outside the census.
- [ ] Verify only those three scoped corrections change `memory/ARCHITECTURE.md`.
