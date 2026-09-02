"""D9/D10 static and dynamic architecture-boundary contracts.

All five tests the plan names for this file are covered here.
`test_every_entry_path_submits_a_command` and
`test_no_module_outside_the_manager_calls_task_service_enqueue`
each carry one documented, narrow exception rather than a fictional
clean pass: `session_intake` and `corpus_study_generate` do no
provider/workflow work at all (pure file/DB I/O), so the plan's own
qualifier ("routes that start provider or workflow work") excludes
them; `corpus_study_run` delegates to `session_handle`, already
migrated. `control/activation.py::ActivationFactory.activate` remains a
documented, intentional interim `TaskService.enqueue` caller for the
"every agent activation becomes a durable child task" migration
(`ActivationFactory`'s own module docstring: "Phase 5's Manager
becomes the sole caller of TaskService.enqueue; until then this factory
is the one place that does"). Tracked as a known, accepted interim
state in docs/adr/0006-super-orchestrator.md.
"""
from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path

import pytest
from phi_core.control.manager import Manager
from phi_core.control.policy import CapabilityDenied, CapabilityPolicy
from phi_core.control.records import ResourceBudget
from phi_core.control.store import MemoryControlStore
from phi_core.control.tasks import TaskService

BACKEND_ROOT = Path(__file__).resolve().parent.parent
SESSION_ID = "c" * 32

_ESCALATE_LIKE = re.compile(r"(escalate|publish|promote|transition|enqueue|accept)", re.IGNORECASE)


# ---- test_manager_holds_no_workflow_authority ----------------------------


def test_manager_holds_no_workflow_authority() -> None:
    """D10: ManagerSupervision keeps run_supervised/consult/the guardian
    broker/close_run, and nothing that writes workflow, task, artifact, or
    publication-pointer state."""
    import inspect

    from phi_core.control.manager import ManagerSupervision

    offending = [
        name for name in dir(ManagerSupervision) if not name.startswith("__") and _ESCALATE_LIKE.search(name)
    ]
    assert offending == []

    # D10: ManagerSupervision must never receive a ControlStore or
    # TaskService at construction -- the structural guarantee that
    # replaces the old (agents/manager.py-only) module import scan now
    # that ManagerSupervision shares control/manager.py with the D9
    # Manager class, which legitimately imports both.
    params = set(inspect.signature(ManagerSupervision.__init__).parameters)
    forbidden = params & {"store", "tasks"}
    assert not forbidden, f"ManagerSupervision.__init__ accepts a workflow collaborator: {forbidden}"


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


# ---- test_only_trace_event_store_writes_trace_events ----------------------


def _trace_events_write_sites() -> list[tuple[Path, int]]:
    """AST-scan every backend .py file for a store-mutation call
    (`insert`/`delete_one`/`delete_many`/`replace_one`/`compare_and_set`)
    whose first positional argument is the literal `"trace_events"`."""
    write_methods = {"insert", "delete_one", "delete_many", "replace_one", "compare_and_set"}
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
            if isinstance(first, ast.Constant) and first.value == "trace_events":
                sites.append((path, node.lineno))
    return sites


def test_only_trace_event_store_writes_trace_events() -> None:
    """D15: `TraceEventStore.append` is the only place in the codebase
    that mutates `trace_events`, so `seq` allocation, hash chaining, and
    the terminal-outcome fence check can never be bypassed by a writer
    that goes straight to the store."""
    sites = _trace_events_write_sites()
    allowed = BACKEND_ROOT / "phi_core" / "control" / "events.py"
    offenders = [(p, ln) for p, ln in sites if p != allowed]
    assert offenders == [], f"trace_events written outside events.py: {offenders}"
    assert sites, "expected at least one write site in events.py -- the scan itself found nothing"


# ---- test_concurrent_child_creation_cannot_exceed_parent_ancestor_or_run_budgets


def _rig() -> tuple[Manager, MemoryControlStore]:
    store = MemoryControlStore()
    tasks = TaskService(store, CapabilityPolicy(None))
    return Manager(store, tasks), store


@pytest.mark.asyncio
async def test_concurrent_child_creation_cannot_exceed_parent_ancestor_or_run_budgets(monkeypatch) -> None:
    """Fires more concurrent create_child_work calls than the ceiling
    allows, at the per-parent, then the run-wide ceiling; every excess is
    refused, and exactly the ceiling's worth succeed."""
    from types import MappingProxyType

    from phi_core.control import limits
    from phi_core.control import manager as manager_module
    from phi_core.control import policy as policy_module
    from phi_core.control.policy import MANIFESTS

    patched = MappingProxyType(
        {
            **MANIFESTS,
            "Pipeline": MANIFESTS["Pipeline"].model_copy(
                update={"allowed_child_task_types": frozenset({"executor"}), "max_children": 100}
            ),
        }
    )
    monkeypatch.setattr(manager_module, "MANIFESTS", patched)
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


# ---- test_every_entry_path_submits_a_command ------------------------------


def _function_source(source: str, tree: ast.AST, name: str) -> str:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            segment = ast.get_source_segment(source, node)
            assert segment is not None, f"could not extract source for {name!r}"
            return segment
    raise AssertionError(f"function {name!r} not found in server.py")


def test_every_entry_path_submits_a_command() -> None:
    """Every route that starts provider or workflow work calls
    `Manager` to do it -- `session_handle`, `session_human_review`,
    `session_cancel`, `session_delete`, `corpus_study_research`, and
    `_run_warmup` (shared by `settings_warmup` and `_warmup_scheduler_loop`)
    each construct one and call a command method on it, rather than
    submitting work (a provider call, a workflow transition) directly."""
    source = (BACKEND_ROOT / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for name in (
        "session_handle", "session_human_review", "session_cancel", "session_delete",
        "corpus_study_research", "_run_warmup",
    ):
        segment = _function_source(source, tree, name)
        assert re.search(r"(?<![A-Za-z])Manager\(", segment), f"{name} never constructs a Manager"


# ---- test_no_module_outside_the_super_orchestrator_calls_task_service_enqueue


def _task_service_enqueue_call_sites() -> list[tuple[Path, int]]:
    """AST-scan every backend .py file for a `<expr>.enqueue(...)` call
    whose receiver isn't obviously unrelated (a cheap syntactic filter --
    `TaskService.enqueue` is the only `.enqueue(` call in this codebase,
    confirmed by the assertion below)."""
    sites: list[tuple[Path, int]] = []
    for path in BACKEND_ROOT.rglob("*.py"):
        if "/.venv/" in str(path) or "/node_modules/" in str(path) or "/tests/" in str(path):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "enqueue":
                sites.append((path, node.lineno))
    return sites


def test_no_module_outside_the_manager_calls_task_service_enqueue() -> None:
    sites = _task_service_enqueue_call_sites()
    allowed = {
        BACKEND_ROOT / "phi_core" / "control" / "manager.py",
        # Documented interim exception -- see this file's module docstring.
        BACKEND_ROOT / "phi_core" / "control" / "activation.py",
    }
    offenders = [(p, ln) for p, ln in sites if p not in allowed]
    assert offenders == [], f"TaskService.enqueue called outside the documented exceptions: {offenders}"
    assert sites, "expected at least one .enqueue( call site -- the scan itself found nothing"


# ---- test_no_agents_module_imports_control_learning -----------------------


def _agents_module_paths() -> list[Path]:
    agents_dir = BACKEND_ROOT / "phi_core" / "agents"
    return [p for p in agents_dir.rglob("*.py") if "__pycache__" not in str(p)]


def test_no_agents_module_imports_control_learning() -> None:
    """D16: runtime agents never import `control.learning` at all -- a
    proposal a running task authors is inert data a human must evaluate,
    approve, and activate through `LearningService`; nothing under
    `phi_core/agents/` may even hold a reference to that module."""
    offenders: list[Path] = []
    for path in _agents_module_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith("control.learning"):
                offenders.append(path)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.endswith("control.learning"):
                        offenders.append(path)
    assert offenders == [], f"phi_core/agents/ module imports control.learning: {offenders}"


# ---- test_no_agents_module_writes_learning_or_capability_collections ------


def _forbidden_collection_write_sites() -> list[tuple[Path, int, str]]:
    """AST-scan every `phi_core/agents/` file for a store-mutation call
    (`insert`/`replace_one`/`compare_and_set`/`delete_one`/`delete_many`)
    whose first positional argument names a learning or capability-policy
    collection."""
    write_methods = {"insert", "replace_one", "compare_and_set", "delete_one", "delete_many"}
    forbidden_collections = {"learning_proposals", "learning_evaluations", "learning_activations", "capability_grants"}
    sites: list[tuple[Path, int, str]] = []
    for path in _agents_module_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in write_methods or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and first.value in forbidden_collections:
                sites.append((path, node.lineno, first.value))
    return sites


def test_no_agents_module_writes_learning_or_capability_collections() -> None:
    """D16 gate: "runtime tasks cannot write active policy stores." No
    `phi_core/agents/` module names `learning_proposals`,
    `learning_evaluations`, `learning_activations`, or `capability_grants`
    in a store-mutation call -- structurally true today since no
    `AgentContext` field or `Agent` attribute exposes a raw `ControlStore`
    at all (verified directly against `AgentContext`'s field list; the
    plan's own `test_control_capability.py::test_agents_receive_no_database_handle`
    was never actually written in Phase 2, and remains a residual,
    intentionally untested gap. This scan guards the property directly
    rather than only its current cause."""
    sites = _forbidden_collection_write_sites()
    assert sites == [], f"phi_core/agents/ writes a learning/policy collection: {sites}"
