"""The workflow state machine (D9): the fixed node table and transition map.

This is a pure data structure: no agent imports, no gateway calls, and no
import of ``orchestrator.py``. Importing ``orchestrator.py`` from here would
recreate exactly the import cycle ``control/gates.py`` already had to route
around with a function-local import inside ``orchestrator.py`` --
``orchestrator.py`` (and, from Phase 5 on, ``control/superorchestrator.py``)
is expected to import *this* module, never the reverse.

``NODES`` are the exact D9 literals: ``"charter"``, ``"research"``,
``"specialists"``, ``"decide"``, ``"gate_decisions"``,
``"human_review_decisions"``, ``"execute"``, ``"verify_operator"``,
``"verify_reviewer"``, ``"publish_guard"``, ``"audit"``,
``"human_review_audit"``, ``"report_ledger"``, ``"report_herald"``,
``"publish"``, and the terminals ``"complete"``, ``"partially_complete"``,
``"blocked"``, ``"failed"``, ``"cancelled"``.

``TRANSITIONS`` maps ``(node, outcome)`` to the next node; ``next_node``
raises ``WorkflowError`` on an unknown pair, so an unmodelled outcome fails
closed instead of silently skipping a gate. This is the structure that
removes the duplicate resume tail (Phase 4 step 4): ``human_review_decisions``
transitions to ``execute`` on ``"resolved"``, the exact same node the initial
path reaches from ``gate_decisions``, so a resumed run and an initial run
converge on one code path instead of two.

The table below is grounded in ``orchestrator.py``'s current control flow
(``run_pipeline``, ``orchestrator.py:98-829``), collapsing runtime
concurrency and retry loops into the fixed checkpoint sequence D9 asks for:

- ``research`` (Statute + Praxis, ``orchestrator.py:158-166``) and
  ``specialists`` (Lexicon + Schema + Instrument, ``orchestrator.py:168-176``)
  run concurrently today; the table orders them sequentially for
  checkpointing purposes only -- a checkpoint answers "what must have
  finished before this point," not "what ran at the same wall-clock time".
- ``decide`` collapses the Judge/Sentinel ``judge_iter``/``sentinel_iter``
  retry loop (``orchestrator.py:280-451``) into one node: the loop's
  internal iteration count is not part of the durable resume contract,
  only its converged result is.
- ``gate_decisions`` is ``control/gates.py::run_decision_gates``
  (``orchestrator.py:476-511``). Its ``"coverage_failed"`` outcome models
  today's ``DecisionGateFailure`` (currently an uncaught exception); its
  ``"human_review_needed"``/``"proceed"`` outcomes model today's
  ``human_needed`` branch (``orchestrator.py:524-568``).
- ``execute``'s ``"crashed"`` outcome models the Executor-crash
  escalation (``orchestrator.py::_escalate_to_human_review``, reason
  ``executor_crashed``, routed through
  ``SuperOrchestrator.request_human_review`` per D10); this table
  records it as returning to ``human_review_decisions`` so a resumed run
  re-enters ``execute`` exactly as D9 requires.
- ``publish_guard``'s ``"blocked"`` outcome is a terminal, matching
  today's `status="blocked"` short-circuit (``orchestrator.py:681-702``)
  exactly: a blocked export never proceeds to audit.
- ``audit``'s ``"escalate"`` outcome models the Auditor-confidence and
  advisory-coverage escalations (``orchestrator.py::_escalate_to_human_review``,
  the reviewer- and auditor-stage call sites), both funnelled onto the
  one ``human_review_audit`` node D9 names.
- ``publish``'s two non-terminal outcomes mirror today's ``final_status``
  computation (``orchestrator.py:651,789``): ``"complete"`` when neither
  Operator nor Reviewer excluded a file, ``"partially_complete"``
  otherwise.

Wiring an actual resume onto this table (deleting ``server.py``'s
``_run_tail`` and routing ``session_human_review`` through ``next_node``)
is Phase 4 step 4, out of scope here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

WORKFLOW_VERSION = "wf/1"

# Every terminal outcome a run can reach. Once here, no further transition
# applies -- ``next_node`` still resolves a terminal name as a valid lookup
# (so a caller can render its label), but ``TRANSITIONS`` never maps *from*
# a terminal.
TERMINAL_NODES: frozenset[str] = frozenset(
    {"complete", "partially_complete", "blocked", "failed", "cancelled"}
)

# The fifteen non-terminal D9 nodes, in their canonical checkpoint order.
NON_TERMINAL_NODES: tuple[str, ...] = (
    "charter",
    "research",
    "specialists",
    "decide",
    "gate_decisions",
    "human_review_decisions",
    "execute",
    "verify_operator",
    "verify_reviewer",
    "publish_guard",
    "audit",
    "human_review_audit",
    "report_ledger",
    "report_herald",
    "publish",
)

NODES: frozenset[str] = frozenset(NON_TERMINAL_NODES) | TERMINAL_NODES


class WorkflowError(RuntimeError):
    """Raised by ``next_node`` for an unmodelled ``(node, outcome)`` pair, or
    by ``node``/``is_terminal`` for a name absent from ``NODES``. An
    unmodelled outcome fails closed rather than silently advancing or
    skipping a gate."""


def is_terminal(name: str) -> bool:
    if name not in NODES:
        raise WorkflowError(f"unknown workflow node: {name!r}")
    return name in TERMINAL_NODES


# (node, outcome) -> next_node. Every non-terminal node has at least one
# "the happy path continues" outcome; nodes that can fail, be cancelled, or
# require a human also declare those outcomes explicitly -- there is no
# implicit fallthrough.
TRANSITIONS: Mapping[tuple[str, str], str] = {
    ("charter", "ok"): "research",
    ("charter", "failed"): "failed",
    ("charter", "cancelled"): "cancelled",
    ("research", "ok"): "specialists",
    ("research", "cancelled"): "cancelled",
    ("specialists", "ok"): "decide",
    ("specialists", "cancelled"): "cancelled",
    ("decide", "ok"): "gate_decisions",
    ("decide", "cancelled"): "cancelled",
    ("gate_decisions", "proceed"): "execute",
    ("gate_decisions", "human_review_needed"): "human_review_decisions",
    ("gate_decisions", "coverage_failed"): "failed",
    ("human_review_decisions", "resolved"): "execute",
    ("human_review_decisions", "cancelled"): "cancelled",
    ("execute", "ok"): "verify_operator",
    ("execute", "crashed"): "human_review_decisions",
    ("execute", "cancelled"): "cancelled",
    ("verify_operator", "ok"): "verify_reviewer",
    ("verify_operator", "cancelled"): "cancelled",
    ("verify_reviewer", "ok"): "publish_guard",
    ("verify_reviewer", "cancelled"): "cancelled",
    ("publish_guard", "clean"): "audit",
    ("publish_guard", "blocked"): "blocked",
    ("audit", "ok"): "report_ledger",
    ("audit", "escalate"): "human_review_audit",
    ("audit", "cancelled"): "cancelled",
    ("human_review_audit", "resolved"): "report_ledger",
    ("human_review_audit", "cancelled"): "cancelled",
    ("report_ledger", "ok"): "report_herald",
    ("report_ledger", "cancelled"): "cancelled",
    ("report_herald", "ok"): "publish",
    ("report_herald", "cancelled"): "cancelled",
    ("publish", "complete"): "complete",
    ("publish", "partially_complete"): "partially_complete",
}


def node(name: str) -> str:
    """Validate ``name`` against the node table, failing closed on an
    unmodelled name. Returns ``name`` unchanged (there is no per-node
    record beyond its identity in this pure table)."""
    if name not in NODES:
        raise WorkflowError(f"unknown workflow node: {name!r}")
    return name


def next_node(current: str, outcome: str) -> str:
    """The one node ``current`` advances to on ``outcome``.

    Raises ``WorkflowError`` on an unmodelled ``(current, outcome)`` pair
    (including a lookup from a terminal node, which has none) rather than
    guessing or falling through -- an unmodelled outcome must never skip,
    reorder, or silently redefine a required gate."""
    node(current)
    try:
        return TRANSITIONS[(current, outcome)]
    except KeyError as exc:
        raise WorkflowError(
            f"no transition modelled for node {current!r} with outcome {outcome!r}"
        ) from exc


def possible_outcomes(current: str) -> tuple[str, ...]:
    """Every outcome ``next_node`` accepts for ``current``, in table order."""
    node(current)
    return tuple(outcome for (from_node, outcome) in TRANSITIONS if from_node == current)


# The checkpoint every node commits to ``workflow_runs.checkpoint`` in the
# same update that records the node transition (D9). ``payload_refs`` holds
# artifact/evidence/decision identifiers only, never inline blobs -- see
# ``control/limits.py::MAX_CHECKPOINT_PAYLOAD_REFS`` (Phase 4 step 10).
CHECKPOINT_VERSION = 1


@dataclass(frozen=True)
class Checkpoint:
    node: str
    checkpoint_version: int = CHECKPOINT_VERSION
    payload_refs: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        node(self.node)


# The fail-closed resume default (D9): an unknown ``checkpoint_version`` --
# one this running code does not recognise, e.g. after a downgrade -- must
# never resume into a node whose semantics might have changed. It always
# falls back to the nearest human-reviewable checkpoint instead.
RESUME_FAILSAFE_NODE = "human_review_decisions"


def resume_node(checkpoint: Checkpoint) -> str:
    """The node a resume should re-enter for ``checkpoint``.

    Returns ``checkpoint.node`` when its ``checkpoint_version`` matches the
    version this code understands; otherwise fails closed to
    ``RESUME_FAILSAFE_NODE`` rather than resuming into a node whose
    semantics this build may not recognise."""
    if checkpoint.checkpoint_version != CHECKPOINT_VERSION:
        return RESUME_FAILSAFE_NODE
    return checkpoint.node
