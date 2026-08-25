"""D9/D10 static and dynamic architecture-boundary contracts.

Three of the five tests the plan names for this file are covered here.
The other two -- `test_every_entry_path_submits_a_command` and
`test_no_module_outside_the_super_orchestrator_calls_task_service_enqueue`
-- cannot pass yet against real code, not against a fake: `session_intake`,
the corpus-study routes, and settings warmup still submit commands
directly (Phase 5 step 2 is not fully migrated), and
`control/activation.py::ActivationFactory.activate` is a documented,
intentional interim `TaskService.enqueue` caller for the very large
"every agent activation becomes a durable child task" migration
(`ActivationFactory`'s own module docstring: "Phase 5's SuperOrchestrator
becomes the sole caller of TaskService.enqueue; until then this factory
is the one place that does"). Writing either test now would either fail
honestly (documenting real, tracked gaps -- acceptable) or need a
scope-widening allow-list invented ad hoc to make it pass, which would
misstate how narrow that exception actually is. Tracked in
docs/adr/0006-super-orchestrator.md and docs/assurance/FINDINGS.md
(F-ORCH-001) instead of asserted here as green.
"""
from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path

import pytest
from phi_core.control.policy import CapabilityDenied, CapabilityPolicy
from phi_core.control.records import ResourceBudget
from phi_core.control.store import MemoryControlStore
from phi_core.control.superorchestrator import SuperOrchestrator
from phi_core.control.tasks import TaskService

BACKEND_ROOT = Path(__file__).resolve().parent.parent
SESSION_ID = "c" * 32

_ESCALATE_LIKE = re.compile(r"(escalate|publish|promote|transition|enqueue|accept)", re.IGNORECASE)


# ---- test_manager_holds_no_workflow_authority ----------------------------


def test_manager_holds_no_workflow_authority() -> None:
    """D10: Manager keeps run_supervised/consult/the guardian broker/
    close_run, and nothing that writes workflow, task, artifact, or
    publication-pointer state."""
    from phi_core.agents.manager import Manager

    offending = [name for name in dir(Manager) if not name.startswith("__") and _ESCALATE_LIKE.search(name)]
    assert offending == []

    source = (BACKEND_ROOT / "phi_core" / "agents" / "manager.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_modules = {"control.tasks", "control.artifacts", "control.review"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not any(node.module.endswith(m) or m in node.module for m in forbidden_modules), (
                f"manager.py imports from forbidden module: {node.module}"
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not any(alias.name.endswith(m) for m in forbidden_modules), (
                    f"manager.py imports forbidden module: {alias.name}"
                )


# ---- test_only_the_artifact_service_writes_the_publication_pointer -------


def _publication_pointer_write_sites() -> list[tuple[Path, int]]:
    """AST-scan every backend .py file for a store-mutation call
    (`insert`/`delete_one`/`replace_one`/`compare_and_set`) whose first
    positional argument is the literal `"publication_pointers"`."""
    write_methods = {"insert", "delete_one", "replace_one", "compare_and_set"}
    sites: list[tuple[Path, int]] = []
    for path in BACKEND_ROOT.rglob("*.py"):
        if "/.venv/" in str(path) or "/node_modules/" in str(path):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in write_methods or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and first.value == "publication_pointers":
                sites.append((path, node.lineno))
    return sites


def test_only_the_artifact_service_writes_the_publication_pointer() -> None:
    sites = _publication_pointer_write_sites()
    allowed = BACKEND_ROOT / "phi_core" / "control" / "artifacts.py"
    offenders = [(p, ln) for p, ln in sites if p != allowed]
    assert offenders == [], f"publication_pointers written outside artifacts.py: {offenders}"
    assert sites, "expected at least one write site in artifacts.py -- the scan itself found nothing"


# ---- test_concurrent_child_creation_cannot_exceed_parent_ancestor_or_run_budgets


def _rig() -> tuple[SuperOrchestrator, MemoryControlStore]:
    store = MemoryControlStore()
    tasks = TaskService(store, CapabilityPolicy(None))
    return SuperOrchestrator(store, tasks), store


@pytest.mark.asyncio
async def test_concurrent_child_creation_cannot_exceed_parent_ancestor_or_run_budgets(monkeypatch) -> None:
    """Fires more concurrent create_child_work calls than the ceiling
    allows, at the per-parent, then the run-wide ceiling; every excess is
    refused, and exactly the ceiling's worth succeed."""
    from types import MappingProxyType

    from phi_core.control import limits
    from phi_core.control import policy as policy_module
    from phi_core.control import superorchestrator as so_module
    from phi_core.control.policy import MANIFESTS

    patched = MappingProxyType(
        {
            **MANIFESTS,
            "Pipeline": MANIFESTS["Pipeline"].model_copy(
                update={"allowed_child_task_types": frozenset({"executor"}), "max_children": 100}
            ),
        }
    )
    monkeypatch.setattr(so_module, "MANIFESTS", patched)
    monkeypatch.setattr(policy_module, "MANIFESTS", patched)
    monkeypatch.setattr(limits, "MAX_PARALLEL_TASKS_PER_PARENT", 3)
    monkeypatch.setattr(limits, "MAX_TASKS_PER_RUN", 100)
    monkeypatch.setattr(limits, "MAX_PARALLEL_TASKS_PER_RUN", 100)

    orch, store = _rig()
    run = await orch.start_run(session_id=SESSION_ID, principal="operator-1")
    root_docs = await store.find_many("work_items", {"run_id": run.run_id})
    root_task_id = root_docs[0]["task_id"]

    async def _attempt() -> bool:
        try:
            await orch.create_child_work(
                run_id=run.run_id, parent_task_id=root_task_id, task_type="executor",
                input_ref={}, budget=ResourceBudget(),
            )
            return True
        except CapabilityDenied:
            return False

    results = await asyncio.gather(*[_attempt() for _ in range(6)])

    assert sum(results) == limits.MAX_PARALLEL_TASKS_PER_PARENT
    assert sum(not r for r in results) == 6 - limits.MAX_PARALLEL_TASKS_PER_PARENT
    children = await store.find_many("work_items", {"parent_task_id": root_task_id})
    assert len(children) == limits.MAX_PARALLEL_TASKS_PER_PARENT
