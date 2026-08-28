"""Focused D9 contracts for the workflow state machine (control/workflow.py)."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from phi_core.control import workflow


def test_workflow_module_imports_no_agent_or_gateway_or_orchestrator_code() -> None:
    """control/workflow.py is pure data: guard the "no agent imports, no
    gateway calls, no orchestrator.py import" contract with a static AST
    scan, so a future edit cannot silently reintroduce the import cycle
    control/gates.py already had to route around."""
    source = Path(workflow.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = ("phi_core.agents", "phi_core.control.gateway", "orchestrator")
    for stmt in ast.walk(tree):
        if isinstance(stmt, ast.Import):
            names = [alias.name for alias in stmt.names]
        elif isinstance(stmt, ast.ImportFrom):
            names = [stmt.module or ""]
        else:
            continue
        for module_name in names:
            for bad in forbidden:
                assert bad not in module_name, f"workflow.py imports {module_name!r}"


def test_exact_d9_node_literals() -> None:
    assert set(workflow.NON_TERMINAL_NODES) == {
        "charter", "research", "specialists", "decide", "gate_decisions",
        "human_review_decisions", "execute", "verify_operator", "verify_reviewer",
        "publish_guard", "audit", "human_review_audit", "report_ledger",
        "report_herald", "publish",
    }
    assert workflow.TERMINAL_NODES == {
        "complete", "partially_complete", "blocked", "failed", "cancelled",
    }
    assert workflow.NODES == set(workflow.NON_TERMINAL_NODES) | workflow.TERMINAL_NODES


def test_node_validates_and_fails_closed_on_unknown_name() -> None:
    assert workflow.node("execute") == "execute"
    with pytest.raises(workflow.WorkflowError):
        workflow.node("not_a_real_node")


def test_is_terminal() -> None:
    assert workflow.is_terminal("complete") is True
    assert workflow.is_terminal("execute") is False
    with pytest.raises(workflow.WorkflowError):
        workflow.is_terminal("not_a_real_node")


def test_next_node_follows_the_happy_path_start_to_finish() -> None:
    assert workflow.next_node("charter", "ok") == "research"
    assert workflow.next_node("research", "ok") == "specialists"
    assert workflow.next_node("specialists", "ok") == "decide"
    assert workflow.next_node("decide", "ok") == "gate_decisions"
    assert workflow.next_node("gate_decisions", "proceed") == "execute"
    assert workflow.next_node("execute", "ok") == "verify_operator"
    assert workflow.next_node("verify_operator", "ok") == "verify_reviewer"
    assert workflow.next_node("verify_reviewer", "ok") == "publish_guard"
    assert workflow.next_node("publish_guard", "clean") == "audit"
    assert workflow.next_node("audit", "ok") == "report_ledger"
    assert workflow.next_node("report_ledger", "ok") == "report_herald"
    assert workflow.next_node("report_herald", "ok") == "publish"
    assert workflow.next_node("publish", "complete") == "complete"
    assert workflow.next_node("publish", "partially_complete") == "partially_complete"


def test_human_review_decisions_converges_on_the_same_execute_node() -> None:
    """The exact D9 property that removes the duplicate resume tail: a
    fresh run reaches `execute` from `gate_decisions`; a resumed run
    reaches the identical `execute` node from `human_review_decisions`."""
    assert workflow.next_node("gate_decisions", "proceed") == "execute"
    assert workflow.next_node("human_review_decisions", "resolved") == "execute"


def test_gate_decisions_branches_on_coverage_and_human_review() -> None:
    assert workflow.next_node("gate_decisions", "human_review_needed") == "human_review_decisions"
    assert workflow.next_node("gate_decisions", "coverage_failed") == "failed"


def test_execute_crash_returns_to_human_review_decisions() -> None:
    assert workflow.next_node("execute", "crashed") == "human_review_decisions"


def test_publish_guard_blocked_is_terminal() -> None:
    assert workflow.next_node("publish_guard", "blocked") == "blocked"
    assert workflow.is_terminal(workflow.next_node("publish_guard", "blocked"))


def test_audit_escalation_reaches_human_review_audit_then_report_ledger() -> None:
    assert workflow.next_node("audit", "escalate") == "human_review_audit"
    assert workflow.next_node("human_review_audit", "resolved") == "report_ledger"


def test_unmodelled_outcome_fails_closed() -> None:
    with pytest.raises(workflow.WorkflowError):
        workflow.next_node("execute", "not_a_real_outcome")
    with pytest.raises(workflow.WorkflowError):
        workflow.next_node("not_a_real_node", "ok")


def test_no_transition_is_declared_from_a_terminal_node() -> None:
    for terminal in workflow.TERMINAL_NODES:
        assert workflow.possible_outcomes(terminal) == ()


def test_possible_outcomes_lists_every_declared_branch() -> None:
    assert set(workflow.possible_outcomes("gate_decisions")) == {
        "proceed", "human_review_needed", "coverage_failed",
    }


def test_checkpoint_validates_its_node_on_construction() -> None:
    workflow.Checkpoint(node="execute")
    with pytest.raises(workflow.WorkflowError):
        workflow.Checkpoint(node="not_a_real_node")


def test_resume_node_returns_the_checkpointed_node_for_the_current_version() -> None:
    checkpoint = workflow.Checkpoint(node="verify_operator", checkpoint_version=workflow.CHECKPOINT_VERSION)
    assert workflow.resume_node(checkpoint) == "verify_operator"


def test_resume_node_fails_closed_to_human_review_on_unknown_checkpoint_version() -> None:
    checkpoint = workflow.Checkpoint(node="publish", checkpoint_version=workflow.CHECKPOINT_VERSION + 999)
    assert workflow.resume_node(checkpoint) == workflow.RESUME_FAILSAFE_NODE == "human_review_decisions"


def test_workflow_version_and_checkpoint_version_are_stable_literals() -> None:
    assert workflow.WORKFLOW_VERSION == "wf/1"
    assert isinstance(workflow.CHECKPOINT_VERSION, int)


# --- Phase R-a: exhaustive state-transition coverage (Phase 1 exit
# criterion "state transitions tested", both directions). Data-driven over
# ``TRANSITIONS``/``NODES`` themselves so a future edit to the table stays
# exhaustively covered without hand-maintaining a parallel list here. ---

_ALL_DECLARED_OUTCOMES = sorted({outcome for (_node, outcome) in workflow.TRANSITIONS})


@pytest.mark.parametrize(
    "node,outcome,target",
    [(node, outcome, target) for (node, outcome), target in workflow.TRANSITIONS.items()],
)
def test_every_legal_transition_reaches_its_declared_target(node, outcome, target) -> None:
    assert workflow.next_node(node, outcome) == target


@pytest.mark.parametrize(
    "node,outcome",
    [
        (node, outcome)
        for node in sorted(workflow.NODES)
        for outcome in _ALL_DECLARED_OUTCOMES
        if (node, outcome) not in workflow.TRANSITIONS
    ],
)
def test_every_illegal_node_outcome_pair_fails_closed(node, outcome) -> None:
    with pytest.raises(workflow.WorkflowError):
        workflow.next_node(node, outcome)


def test_transition_coverage_is_exhaustive_over_declared_outcomes() -> None:
    """Sanity check on the two parametrized tests above: every (node,
    outcome) pair over the declared outcome vocabulary is classified as
    either legal (target lookup) or illegal (raises) -- none silently
    skipped."""
    legal = set(workflow.TRANSITIONS)
    all_pairs = {(node, outcome) for node in workflow.NODES for outcome in _ALL_DECLARED_OUTCOMES}
    illegal = all_pairs - legal
    assert legal | illegal == all_pairs
    assert legal & illegal == set()
    assert len(legal) == 34
