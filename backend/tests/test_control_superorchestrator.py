"""D9 contracts for control/superorchestrator.py: the exclusive writer of
``workflow_runs.state``/``.node``, the exclusive caller of
``TaskService.enqueue`` (through ``create_child_work``), and the exclusive
issuer/consumer/acceptor of a human-review request, event, and material
child result.
"""
from __future__ import annotations

from types import MappingProxyType

import pytest
from phi_core.control import policy as policy_module
from phi_core.control import superorchestrator as so_module
from phi_core.control.policy import MANIFESTS, CapabilityDenied, CapabilityPolicy
from phi_core.control.records import HumanReviewEvent, ResourceBudget
from phi_core.control.store import MemoryControlStore
from phi_core.control.superorchestrator import SuperOrchestrator
from phi_core.control.tasks import TaskService
from phi_core.control.workflow import WorkflowError

SESSION_ID = "a" * 32


def _rig() -> tuple[SuperOrchestrator, TaskService, MemoryControlStore]:
    store = MemoryControlStore()
    tasks = TaskService(store, CapabilityPolicy(None))
    return SuperOrchestrator(store, tasks), tasks, store


async def _started_run(orch: SuperOrchestrator):
    return await orch.start_run(session_id=SESSION_ID, principal="operator-1")


async def _root_task(tasks: TaskService, run_id: str):
    return await tasks.enqueue(run_id=run_id, session_id=SESSION_ID, worker="Pipeline", task_type="pipeline_run")


# ---- start_run --------------------------------------------------------


@pytest.mark.asyncio
async def test_start_run_opens_at_charter_with_a_committed_checkpoint() -> None:
    orch, _tasks, store = _rig()
    run = await orch.start_run(session_id=SESSION_ID, principal="operator-1", run_type="study", iteration_cap=2)

    assert run.state == "running"
    assert run.node == "charter"
    assert run.checkpoint["node"] == "charter"
    assert run.session_id == SESSION_ID
    stored = await store.get_one("workflow_runs", {"run_id": run.run_id})
    assert stored["node"] == "charter"

    root_tasks = await store.find_many("work_items", {"run_id": run.run_id})
    assert len(root_tasks) == 1
    assert root_tasks[0]["worker"] == "Pipeline"
    assert root_tasks[0]["task_type"] == "pipeline_run"
    assert root_tasks[0]["parent_task_id"] == ""


@pytest.mark.asyncio
async def test_start_run_uses_a_route_claimed_run_id_for_its_run_and_root_task() -> None:
    orch, _tasks, store = _rig()
    claimed_run_id = "b" * 32

    run = await orch.start_run(
        session_id=SESSION_ID,
        principal="operator-1",
        correlation_id="handle-command",
        run_id=claimed_run_id,
    )

    assert run.run_id == claimed_run_id
    tasks = await store.find_many("work_items", {"run_id": claimed_run_id})
    assert len(tasks) == 1
    assert tasks[0]["correlation_id"] == "handle-command"


@pytest.mark.asyncio
async def test_start_run_can_enqueue_a_pipeline_resume_root_task() -> None:
    orch, _tasks, store = _rig()

    run = await orch.start_run(
        session_id=SESSION_ID,
        principal="operator-1",
        root_task_type="pipeline_resume",
    )

    tasks = await store.find_many("work_items", {"run_id": run.run_id})
    assert len(tasks) == 1
    assert tasks[0]["task_type"] == "pipeline_resume"


@pytest.mark.asyncio
async def test_start_run_refuses_an_anonymous_principal() -> None:
    orch, _tasks, _store = _rig()
    with pytest.raises(CapabilityDenied):
        await orch.start_run(session_id=SESSION_ID, principal="")


@pytest.mark.asyncio
async def test_start_run_refuses_a_negative_iteration_cap() -> None:
    orch, _tasks, _store = _rig()
    with pytest.raises(ValueError):
        await orch.start_run(session_id=SESSION_ID, principal="operator-1", iteration_cap=-1)


# ---- cancel_run ---------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_run_sets_the_flag_and_is_idempotent() -> None:
    orch, _tasks, _store = _rig()
    run = await _started_run(orch)

    first = await orch.cancel_run(session_id=SESSION_ID, run_id=run.run_id, principal="operator-1", reason="stop")
    assert first.cancel_requested is True

    second = await orch.cancel_run(session_id=SESSION_ID, run_id=run.run_id, principal="operator-1", reason="stop again")
    assert second.cancel_requested is True
    assert second.cancel_requested_at == first.cancel_requested_at  # no second write


@pytest.mark.asyncio
async def test_cancel_run_on_a_terminal_run_is_a_no_op() -> None:
    orch, _tasks, _store = _rig()
    run = await _started_run(orch)
    from phi_core.control.workflow import TRANSITIONS, is_terminal

    while not is_terminal(run.node):
        candidate = next((o for (n, o) in TRANSITIONS if n == run.node and o == "cancelled"), None)
        if candidate is None:
            candidate = next(o for (n, o) in TRANSITIONS if n == run.node)
        run = await orch.advance(run_id=run.run_id, outcome=candidate)
    assert is_terminal(run.node)

    unchanged = await orch.cancel_run(session_id=SESSION_ID, run_id=run.run_id, principal="op", reason="x")
    assert unchanged.cancel_requested is False
    assert unchanged.node == run.node


@pytest.mark.asyncio
async def test_cancel_run_refuses_a_run_from_another_session() -> None:
    orch, _tasks, _store = _rig()
    run = await _started_run(orch)
    with pytest.raises(WorkflowError):
        await orch.cancel_run(session_id="someone-else", run_id=run.run_id, principal="op", reason="x")


# ---- advance ------------------------------------------------------------


@pytest.mark.asyncio
async def test_advance_moves_to_the_transition_tables_target_and_commits_a_checkpoint() -> None:
    orch, _tasks, store = _rig()
    run = await _started_run(orch)

    advanced = await orch.advance(run_id=run.run_id, outcome="ok")

    assert advanced.node == "research"
    assert advanced.checkpoint["node"] == "research"
    stored = await store.get_one("workflow_runs", {"run_id": run.run_id})
    assert stored["node"] == "research"


@pytest.mark.asyncio
async def test_advance_rejects_an_outcome_the_transition_table_does_not_recognise() -> None:
    orch, _tasks, _store = _rig()
    run = await _started_run(orch)

    with pytest.raises(WorkflowError):
        await orch.advance(run_id=run.run_id, outcome="not_a_real_outcome")


@pytest.mark.asyncio
async def test_advance_refuses_once_the_run_is_already_terminal() -> None:
    orch, _tasks, _store = _rig()
    run = await _started_run(orch)
    from phi_core.control.workflow import TRANSITIONS, is_terminal

    while not is_terminal(run.node):
        candidate = next((o for (n, o) in TRANSITIONS if n == run.node and o == "cancelled"), None)
        if candidate is None:
            candidate = next(o for (n, o) in TRANSITIONS if n == run.node)
        run = await orch.advance(run_id=run.run_id, outcome=candidate)
    assert is_terminal(run.node)

    with pytest.raises(WorkflowError):
        await orch.advance(run_id=run.run_id, outcome="ok")


@pytest.mark.asyncio
async def test_advance_stamps_state_and_terminal_outcome_on_a_terminal_target() -> None:
    orch, _tasks, _store = _rig()
    run = await _started_run(orch)
    from phi_core.control.workflow import TRANSITIONS, is_terminal

    while not is_terminal(run.node):
        candidate = next((o for (n, o) in TRANSITIONS if n == run.node and o == "cancelled"), None)
        if candidate is None:
            candidate = next(o for (n, o) in TRANSITIONS if n == run.node)
        run = await orch.advance(run_id=run.run_id, outcome=candidate)

    assert run.state == run.node
    assert run.terminal_outcome == run.node
    assert run.completed_at


# ---- create_child_work --------------------------------------------------


@pytest.mark.asyncio
async def test_create_child_work_refuses_a_task_type_the_parent_manifest_does_not_grant() -> None:
    orch, tasks, _store = _rig()
    run = await _started_run(orch)
    parent = await _root_task(tasks, run.run_id)

    with pytest.raises(CapabilityDenied):
        await orch.create_child_work(
            run_id=run.run_id, parent_task_id=parent.task_id, task_type="executor",
            input_ref={}, budget=ResourceBudget(),
        )


@pytest.mark.asyncio
async def test_create_child_work_refuses_an_unknown_parent_task() -> None:
    orch, _tasks, _store = _rig()
    run = await _started_run(orch)

    with pytest.raises(WorkflowError):
        await orch.create_child_work(
            run_id=run.run_id, parent_task_id="does-not-exist", task_type="executor",
            input_ref={}, budget=ResourceBudget(),
        )


@pytest.mark.asyncio
async def test_create_child_work_enqueues_a_granted_child_within_depth_and_budget(monkeypatch) -> None:
    orch, tasks, store = _rig()
    run = await _started_run(orch)
    parent = await _root_task(tasks, run.run_id)

    # Grant "Pipeline" a test-only child task type, mirroring what step 5
    # will do for real for Ledger/Herald once it lands. Patched on both
    # modules: policy.py's own CapabilityPolicy.check_child reads the
    # module-level MANIFESTS name directly, and superorchestrator.py
    # bound its own reference at import time. "executor" (not "ledger")
    # because "Ledger"'s manifest restricts allowed_providers, which
    # CapabilityPolicy(None)'s empty provider/model would fail regardless
    # of the child-task grant this test is actually exercising.
    patched = MappingProxyType(
        {
            **MANIFESTS,
            "Pipeline": MANIFESTS["Pipeline"].model_copy(
                update={"allowed_child_task_types": frozenset({"executor"})}
            ),
        }
    )
    monkeypatch.setattr(policy_module, "MANIFESTS", patched)
    monkeypatch.setattr(so_module, "MANIFESTS", patched)

    child = await orch.create_child_work(
        run_id=run.run_id, parent_task_id=parent.task_id, task_type="executor",
        input_ref={"section": "compare"}, budget=ResourceBudget(),
    )

    assert child.parent_task_id == parent.task_id
    assert child.depth == parent.depth + 1
    assert child.worker == "Executor"
    assert child.state == "ready"
    stored = await store.get_one("work_items", {"task_id": child.task_id})
    assert stored["parent_task_id"] == parent.task_id


@pytest.mark.asyncio
async def test_create_child_work_refuses_a_budget_that_widens_the_targets_manifest(monkeypatch) -> None:
    orch, tasks, _store = _rig()
    run = await _started_run(orch)
    parent = await _root_task(tasks, run.run_id)
    patched = MappingProxyType(
        {
            **MANIFESTS,
            "Pipeline": MANIFESTS["Pipeline"].model_copy(
                update={"allowed_child_task_types": frozenset({"executor"})}
            ),
        }
    )
    monkeypatch.setattr(policy_module, "MANIFESTS", patched)
    monkeypatch.setattr(so_module, "MANIFESTS", patched)

    with pytest.raises(CapabilityDenied):
        await orch.create_child_work(
            run_id=run.run_id, parent_task_id=parent.task_id, task_type="executor",
            input_ref={}, budget=ResourceBudget(max_tool_calls=10_000_000),
        )


# ---- request_human_review / consume_review_event -------------------------


@pytest.mark.asyncio
async def test_request_human_review_opens_a_request_and_pauses_the_run() -> None:
    orch, _tasks, store = _rig()
    run = await _started_run(orch)

    request = await orch.request_human_review(
        run_id=run.run_id, node="human_review_decisions", reason_codes=["blocking_issue"],
        decision_version=1,
    )

    assert request.run_id == run.run_id
    assert request.node == "human_review_decisions"
    assert request.state == "open"
    stored = await store.get_one("workflow_runs", {"run_id": run.run_id})
    assert stored["state"] == "awaiting_human_review"
    assert stored["node"] == "human_review_decisions"


@pytest.mark.asyncio
async def test_consume_review_event_resolves_the_request_and_resumes_the_run() -> None:
    orch, _tasks, store = _rig()
    run = await _started_run(orch)
    request = await orch.request_human_review(
        run_id=run.run_id, node="human_review_decisions", reason_codes=["blocking_issue"],
        decision_version=1,
    )
    event = HumanReviewEvent(
        request_id=request.request_id, run_id=run.run_id, session_id=SESSION_ID,
        workflow_version="wf/1", task_id="", seq=1, client_event_id="c1",
        principal="reviewer-1", kind="resolution", body_hash="deadbeef",
    )

    resumed = await orch.consume_review_event(run_id=run.run_id, event=event)

    assert resumed.state == "running"
    stored_request = await store.get_one("human_review_requests", {"request_id": request.request_id})
    assert stored_request["state"] == "resolved"
    stored_events = await store.find_many("human_review_events", {"request_id": request.request_id})
    assert len(stored_events) == 1


@pytest.mark.asyncio
async def test_consume_review_event_refuses_a_mismatched_run_id() -> None:
    orch, _tasks, _store = _rig()
    run = await _started_run(orch)
    request = await orch.request_human_review(
        run_id=run.run_id, node="human_review_decisions", reason_codes=[], decision_version=1,
    )
    event = HumanReviewEvent(
        request_id=request.request_id, run_id="some-other-run", session_id=SESSION_ID,
        workflow_version="wf/1", task_id="", seq=1, client_event_id="c1",
        principal="reviewer-1", kind="resolution", body_hash="deadbeef",
    )
    with pytest.raises(WorkflowError):
        await orch.consume_review_event(run_id=run.run_id, event=event)


# ---- accept_result --------------------------------------------------------


@pytest.mark.asyncio
async def test_accept_result_accepts_a_succeeded_tasks_own_output(monkeypatch) -> None:
    orch, tasks, store = _rig()
    run = await _started_run(orch)
    task = await _root_task(tasks, run.run_id)
    claimed = await tasks.claim(task_id=task.task_id, lease_owner="worker-1")
    assert claimed is not None
    outcome = await tasks.complete(
        task_id=task.task_id, lease_owner="worker-1", fence=claimed.fence, output_ref={"status": "ok"},
    )
    assert outcome.ok

    accepted = await orch.accept_result(run_id=run.run_id, task_id=task.task_id, result={"status": "ok"})
    assert accepted is True
    stored = await store.get_one("workflow_runs", {"run_id": run.run_id})
    assert task.task_id in stored["checkpoint"]["accepted_task_ids"]

    # Idempotent: accepting again returns True without changing anything else.
    again = await orch.accept_result(run_id=run.run_id, task_id=task.task_id, result={"status": "ok"})
    assert again is True


@pytest.mark.asyncio
async def test_accept_result_refuses_a_result_a_task_did_not_actually_produce() -> None:
    orch, tasks, _store = _rig()
    run = await _started_run(orch)
    task = await _root_task(tasks, run.run_id)
    claimed = await tasks.claim(task_id=task.task_id, lease_owner="worker-1")
    await tasks.complete(task_id=task.task_id, lease_owner="worker-1", fence=claimed.fence, output_ref={"status": "ok"})

    forged = await orch.accept_result(run_id=run.run_id, task_id=task.task_id, result={"status": "forged"})
    assert forged is False


@pytest.mark.asyncio
async def test_accept_result_refuses_a_task_that_has_not_succeeded_yet() -> None:
    orch, tasks, _store = _rig()
    run = await _started_run(orch)
    task = await _root_task(tasks, run.run_id)

    accepted = await orch.accept_result(run_id=run.run_id, task_id=task.task_id, result={})
    assert accepted is False


# ---- recover --------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_reenters_the_persisted_checkpoint_node() -> None:
    orch, _tasks, store = _rig()
    run = await _started_run(orch)
    run = await orch.advance(run_id=run.run_id, outcome="ok")  # -> research

    recovered = await orch.recover(run_id=run.run_id, cause="process_restart")

    assert recovered.node == "research"
    assert recovered.checkpoint["recovery_cause"] == "process_restart"


@pytest.mark.asyncio
async def test_recover_fails_closed_to_the_resume_failsafe_on_an_unknown_checkpoint_version() -> None:
    orch, _tasks, store = _rig()
    run = await _started_run(orch)
    run = await orch.advance(run_id=run.run_id, outcome="ok")  # -> research
    stale = (await store.get_one("workflow_runs", {"run_id": run.run_id}))
    stale["checkpoint_version"] = 999
    await store.replace_one("workflow_runs", {"run_id": run.run_id}, stale)

    recovered = await orch.recover(run_id=run.run_id, cause="downgrade")

    assert recovered.node == "human_review_decisions"


@pytest.mark.asyncio
async def test_recover_on_a_terminal_run_is_a_no_op() -> None:
    orch, _tasks, _store = _rig()
    run = await _started_run(orch)
    from phi_core.control.workflow import TRANSITIONS, is_terminal

    while not is_terminal(run.node):
        candidate = next((o for (n, o) in TRANSITIONS if n == run.node and o == "cancelled"), None)
        if candidate is None:
            candidate = next(o for (n, o) in TRANSITIONS if n == run.node)
        run = await orch.advance(run_id=run.run_id, outcome=candidate)

    recovered = await orch.recover(run_id=run.run_id, cause="noop")
    assert recovered.node == run.node
    assert recovered.resumed_at == run.resumed_at


# ---- authorize_publication --------------------------------------------


@pytest.mark.asyncio
async def test_authorize_publication_with_no_artifact_ids_is_query_only() -> None:
    orch, _tasks, _store = _rig()
    run = await _started_run(orch)

    generation = await orch.authorize_publication(run_id=run.run_id)
    assert generation == 0


@pytest.mark.asyncio
async def test_authorize_publication_refuses_while_a_review_request_is_open() -> None:
    orch, _tasks, _store = _rig()
    run = await _started_run(orch)
    await orch.request_human_review(
        run_id=run.run_id, node="human_review_decisions", reason_codes=[], decision_version=1,
    )

    with pytest.raises(WorkflowError):
        await orch.authorize_publication(run_id=run.run_id, artifact_ids=["a1"])


@pytest.mark.asyncio
async def test_authorize_publication_certifies_a_new_generation() -> None:
    from phi_core.control.artifacts import ArtifactService

    orch, _tasks, store = _rig()
    run = await _started_run(orch)
    service = ArtifactService(store, session_id=SESSION_ID, run_id=run.run_id)
    artifact_id, tmp_path = await service.stage("export", "study.csv", "internal", "standard")
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path.write_bytes(b"clean export")
    await service.finalize(artifact_id)

    generation = await orch.authorize_publication(run_id=run.run_id, artifact_ids=[artifact_id])

    assert generation == 1
    path = await service.open_for_download(SESSION_ID, artifact_id)
    assert path.is_file()
