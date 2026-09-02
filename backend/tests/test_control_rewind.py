"""Tests for RewindRouter (Phase 10, docs #56): the root-cause classifier
and rewind-routing layer on top of the existing
``Manager.rewind`` (built, untested-from-a-live-caller, in Wave
R-b). Each of the five root-cause categories is tested individually:
the classifier must pick the right category AND the router must call
``rewind()`` targeting the correct earliest-affected node -- not merely
that something fails.
"""
from __future__ import annotations

import pytest
from phi_core.control.manager import Manager
from phi_core.control.policy import CapabilityPolicy
from phi_core.control.rewind import RewindDecision, RewindRouter, UnroutableFailure, record_rewind_decision
from phi_core.control.store import MemoryControlStore
from phi_core.control.tasks import TaskService
from phi_core.control.workflow import WorkflowError

SESSION_ID = "a" * 32

# Advances a run to "report_ledger" (index 12 in NON_TERMINAL_NODES) --
# strictly later than every one of the five categories' rewind targets
# (execute=6, decide=3, specialists=2, human_review_audit=10,
# human_review_decisions=5), so every category's `rewind()` call below is
# a genuine "earlier than current" transition, never refused on that
# ground alone.
_ADVANCE_TO_REPORT_LEDGER = ("ok", "ok", "ok", "ok", "proceed", "ok", "ok", "ok", "clean", "ok")


def _rig() -> tuple[Manager, MemoryControlStore]:
    store = MemoryControlStore()
    return Manager(store, TaskService(store, CapabilityPolicy(None))), store


async def _run_at_report_ledger(orch: Manager):
    run = await orch.start_run(session_id=SESSION_ID, principal="operator-1")
    for outcome in _ADVANCE_TO_REPORT_LEDGER:
        run = await orch.advance(run_id=run.run_id, outcome=outcome)
    assert run.node == "report_ledger"
    return run


# ---- classify(): each of the 5 categories picks the right node/failure_class --


def test_classify_execution_error_targets_execute():
    decision = RewindRouter.classify({"failure_class": "EXECUTION_ERROR"})
    assert decision.category == "EXECUTION_ERROR"
    assert decision.to_node == "execute"
    assert decision.failure_class == "EXECUTION_ERROR"


def test_classify_method_error_targets_decide():
    decision = RewindRouter.classify({"failure_class": "METHOD_ERROR"})
    assert decision.category == "METHOD_ERROR"
    assert decision.to_node == "decide"
    assert decision.failure_class == "METHOD_ERROR"


def test_classify_regulation_error_targets_decide():
    decision = RewindRouter.classify({"failure_class": "REGULATION_ERROR"})
    assert decision.category == "REGULATION_ERROR"
    assert decision.to_node == "decide"
    assert decision.failure_class == "REGULATION_ERROR"


def test_classify_semantic_error_targets_specialists_and_maps_failure_class():
    """SEMANTIC_ERROR is not a records.FailureClass member -- the
    incoming signal names the mapped FailureClass directly
    (SPECIALIST_INTERPRETATION_ERROR), and the classifier must recognize
    it as the SEMANTIC_ERROR category."""
    decision = RewindRouter.classify("SPECIALIST_INTERPRETATION_ERROR")
    assert decision.category == "SEMANTIC_ERROR"
    assert decision.to_node == "specialists"
    assert decision.failure_class == "SPECIALIST_INTERPRETATION_ERROR"


def test_classify_unresolved_uncertainty_post_execution_targets_human_review_audit():
    decision = RewindRouter.classify({"failure_class": "HUMAN_REVIEW_REQUIRED"})
    assert decision.category == "UNRESOLVED_UNCERTAINTY"
    assert decision.to_node == "human_review_audit"
    assert decision.failure_class == "HUMAN_REVIEW_REQUIRED"


def test_classify_unresolved_uncertainty_pre_execution_targets_human_review_decisions():
    decision = RewindRouter.classify({"failure_class": "HUMAN_REVIEW_REQUIRED"}, stage="pre_execution")
    assert decision.category == "UNRESOLVED_UNCERTAINTY"
    assert decision.to_node == "human_review_decisions"


def test_classify_raises_on_unroutable_failure_class():
    """A FailureClass this router genuinely has no rewind route for
    (section 56's disclosed structurally-blocked case) fails closed
    rather than guessing a stage."""
    with pytest.raises(UnroutableFailure):
        RewindRouter.classify({"failure_class": "SECURITY_BOUNDARY_VIOLATION"})


def test_classify_raises_on_signal_with_no_failure_class():
    with pytest.raises(UnroutableFailure):
        RewindRouter.classify({"detail": "no failure_class key at all"})


# ---- route(): each of the 5 categories actually calls rewind() to the ---
# ---- correct node -----------------------------------------------------


@pytest.mark.asyncio
async def test_route_execution_error_rewinds_to_execute():
    orch, store = _rig()
    run = await _run_at_report_ledger(orch)

    decision, rewound = await RewindRouter.route(
        super_orchestrator=orch, run_id=run.run_id, signal={"failure_class": "EXECUTION_ERROR"},
    )

    assert decision.category == "EXECUTION_ERROR"
    assert rewound.node == "execute"
    assert rewound.state == "running"
    stored = await store.get_one("workflow_runs", {"run_id": run.run_id})
    assert stored["node"] == "execute"
    assert "root_cause=EXECUTION_ERROR" in stored["checkpoint"]["rewind_reason"]


@pytest.mark.asyncio
async def test_route_method_error_rewinds_to_decide():
    orch, _store = _rig()
    run = await _run_at_report_ledger(orch)

    decision, rewound = await RewindRouter.route(
        super_orchestrator=orch, run_id=run.run_id, signal={"failure_class": "METHOD_ERROR"},
    )

    assert decision.category == "METHOD_ERROR"
    assert rewound.node == "decide"


@pytest.mark.asyncio
async def test_route_regulation_error_rewinds_to_decide():
    orch, _store = _rig()
    run = await _run_at_report_ledger(orch)

    decision, rewound = await RewindRouter.route(
        super_orchestrator=orch, run_id=run.run_id, signal={"failure_class": "REGULATION_ERROR"},
    )

    assert decision.category == "REGULATION_ERROR"
    assert rewound.node == "decide"


@pytest.mark.asyncio
async def test_route_semantic_error_rewinds_to_specialists():
    orch, _store = _rig()
    run = await _run_at_report_ledger(orch)

    decision, rewound = await RewindRouter.route(
        super_orchestrator=orch, run_id=run.run_id, signal="SPECIALIST_INTERPRETATION_ERROR",
    )

    assert decision.category == "SEMANTIC_ERROR"
    assert rewound.node == "specialists"


@pytest.mark.asyncio
async def test_route_unresolved_uncertainty_rewinds_to_human_review_audit_by_default():
    orch, _store = _rig()
    run = await _run_at_report_ledger(orch)

    decision, rewound = await RewindRouter.route(
        super_orchestrator=orch, run_id=run.run_id, signal={"failure_class": "HUMAN_REVIEW_REQUIRED"},
    )

    assert decision.category == "UNRESOLVED_UNCERTAINTY"
    assert rewound.node == "human_review_audit"


@pytest.mark.asyncio
async def test_route_unresolved_uncertainty_rewinds_to_human_review_decisions_pre_execution():
    orch, _store = _rig()
    run = await _run_at_report_ledger(orch)

    decision, rewound = await RewindRouter.route(
        super_orchestrator=orch, run_id=run.run_id,
        signal={"failure_class": "HUMAN_REVIEW_REQUIRED"}, stage="pre_execution",
    )

    assert decision.category == "UNRESOLVED_UNCERTAINTY"
    assert rewound.node == "human_review_decisions"


# ---- route() propagates the underlying rewind()'s own WorkflowError ----


@pytest.mark.asyncio
async def test_route_propagates_workflow_error_when_target_is_not_earlier_than_current_node():
    """`route` never builds a second rewind mechanism: when the resolved
    target is not strictly earlier than the run's current node,
    `Manager.rewind`'s own refusal propagates unchanged."""
    orch, _store = _rig()
    run = await orch.start_run(session_id=SESSION_ID, principal="operator-1")
    for outcome in ("ok", "ok", "ok"):  # lands at "decide"
        run = await orch.advance(run_id=run.run_id, outcome=outcome)
    assert run.node == "decide"

    with pytest.raises(WorkflowError):
        # EXECUTION_ERROR -> "execute", which is LATER than "decide" --
        # not earlier, so rewind() itself must refuse.
        await RewindRouter.route(
            super_orchestrator=orch, run_id=run.run_id, signal={"failure_class": "EXECUTION_ERROR"},
        )


# ---- record_rewind_decision persists the mapped FailureClass ----------


@pytest.mark.asyncio
async def test_record_rewind_decision_persists_mapped_failure_class():
    store = MemoryControlStore()
    decision = RewindDecision(
        category="SEMANTIC_ERROR", failure_class="SPECIALIST_INTERPRETATION_ERROR",
        to_node="specialists", reason="root_cause=SEMANTIC_ERROR; original_failure_class=x",
    )

    await record_rewind_decision(store, run_id="r1", decision=decision)

    stored = await store.find_many("rewind_decisions", {"run_id": "r1"})
    assert len(stored) == 1
    assert stored[0]["failure_class"] == "SPECIALIST_INTERPRETATION_ERROR"
    assert stored[0]["to_node"] == "specialists"
