# 0001: Workflow node table as the single source of transition truth

## Status

Accepted (partial: the node table is defined and tested; nothing yet drives execution through it — see Consequences)

## Context

`orchestrator.py::run_pipeline` and `server.py::session_human_review`'s `_run_tail` closure each implement the same fixed pipeline sequence independently: an initial run walks specialists through Judge/Sentinel, execute, verify, publish-guard, audit, and reporting; a resumed run re-implements the tail of that sequence from a human-review checkpoint, with real divergences from the initial path (missing `try/except` wrappers, absent `_check_cancel` calls, absent Manager consults). Neither path is expressed as data a resume routine can look up; both are Python control flow that must be read and kept in sync by hand.

## Decision

`backend/phi_core/control/workflow.py` defines the D9 state machine as pure data: `WORKFLOW_VERSION = "wf/1"`, the fifteen non-terminal node literals plus five terminals, and `TRANSITIONS: Mapping[tuple[str, str], str]` mapping `(node, outcome)` to the next node. `next_node(node, outcome)` raises `WorkflowError` on any pair not in `TRANSITIONS`, so an unmodelled outcome fails closed rather than silently advancing past a gate. The table is grounded in `orchestrator.py`'s current control flow (see the module docstring for the exact `path:line` justification of every non-obvious transition), collapsing the Judge/Sentinel retry loop into one `decide` node and the concurrent specialists/research fan-out into a fixed checkpoint order, since a checkpoint records "what must have finished," not "what ran concurrently."

The load-bearing property is `("gate_decisions", "proceed") -> "execute"` and `("human_review_decisions", "resolved") -> "execute"` both resolving to the identical `execute` node: an initial run and a resumed run converge on one path, which is what lets `server.py`'s duplicate `_run_tail` be deleted once resume is wired onto this table (tracked separately, not yet done).

`Checkpoint` (`node`, `checkpoint_version`, `payload_refs`) is the record every node is meant to commit to `workflow_runs.checkpoint` alongside its transition. `resume_node(checkpoint)` implements the fail-closed resume rule: an unrecognized `checkpoint_version` never resumes into a node whose semantics this build might not understand; it always falls back to `RESUME_FAILSAFE_NODE = "human_review_decisions"`.

## Consequences

- `control/workflow.py` has no `orchestrator.py` import and no agent/gateway import (enforced by a static AST scan in `test_control_workflow.py`), so it can be imported from `orchestrator.py`, `control/tasks.py`'s handlers, and the future `control/superorchestrator.py` without a cycle.
- Nothing in the codebase drives execution through `TRANSITIONS` yet. `orchestrator.py::run_pipeline` and `server.py`'s `_run_tail` still implement the same logic in Python control flow, unchanged. This ADR records the target state machine; the migration that makes `orchestrator.py` and `_run_tail` actually consult `next_node` and deletes the duplicate resume path is separate, larger work, tracked as the remaining scope of the durability effort this ADR belongs to.
- The exact transition set is an interpretation of current behavior, not a literal spec in the plan text; several transitions (e.g. `("execute", "crashed") -> "human_review_decisions"`) describe what today's `Manager.escalate_to_human_review` call sites do, which itself is scheduled for replacement by `SuperOrchestrator.request_human_review`. When that replacement lands, this table is the reference for what each call site's outcome should map to; it does not need to change shape, only gain real callers.
