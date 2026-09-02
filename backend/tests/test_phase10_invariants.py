"""Phase 10 invariants (item 7): cross-cutting checks that don't belong to
any single item's own test file.

Invariant A (item 3's raw-reader allowlist tightening): codified again
here, standalone, so it is obvious from this file alone -- without
cross-referencing ``test_control_phaseR_integration.py``'s own AST-scan
test -- that ``agents/operator.py`` is genuinely gone and that the raw-
reader call-site scan it used to be allowlisted in still finds real call
sites (never vacuous).

Invariant B (item 6's five rewind routes): the actual route()-level
proof already lives in ``test_control_rewind.py`` (each category tested
individually against a real advanced ``WorkflowRun``); this file adds
one summary test that exercises all five in a single pass and asserts
the resulting nodes match the exact expected mapping, collapsing onto 4
distinct values (METHOD_ERROR and REGULATION_ERROR share `decide`).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from phi_core.control.manager import Manager
from phi_core.control.policy import CapabilityPolicy
from phi_core.control.rewind import RewindRouter
from phi_core.control.store import MemoryControlStore
from phi_core.control.tasks import TaskService

_PHI_CORE_ROOT = Path(__file__).resolve().parent.parent / "phi_core"
_RAW_READER_TARGETS = {
    "_read_dataset_headers", "read_narrative",
    "_redact_metadata_file", "apply_column_actions_to_dataset",
    "verify_keep_decisions",
}


def _call_sites(root: Path, target_names: set[str]) -> list[tuple[Path, int]]:
    """Standalone copy of test_control_phaseR_integration.py's own
    ``_call_sites`` helper -- deliberately not imported from that test
    module, so this invariant test has no coupling to that file's own
    internals and would still catch a regression even if that file's
    helper were ever renamed or changed."""
    import ast

    sites: list[tuple[Path, int]] = []
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in target_names:
                sites.append((path, node.lineno))
            elif isinstance(func, ast.Attribute) and func.attr in target_names:
                sites.append((path, node.lineno))
    return sites


def test_operator_module_is_gone():
    """The retired module file itself does not exist -- not merely that
    nothing imports it."""
    assert not (_PHI_CORE_ROOT / "agents" / "operator.py").exists()


def test_operator_module_cannot_be_imported():
    with pytest.raises(ModuleNotFoundError):
        __import__("phi_core.agents.operator")


def test_raw_reader_scan_is_non_vacuous_and_confined_to_reasoning_py():
    """The tightened allowlist (reasoning.py only, operator.py's
    exemption removed) is substantive: real call sites exist, and every
    single one is inside reasoning.py."""
    sites = _call_sites(_PHI_CORE_ROOT, _RAW_READER_TARGETS)
    assert sites, "the scan itself found nothing -- it would be vacuous"
    reasoning_py = _PHI_CORE_ROOT / "agents" / "reasoning.py"
    offenders = [(p, ln) for p, ln in sites if p != reasoning_py]
    assert offenders == [], f"raw reader called outside reasoning.py: {offenders}"


@pytest.mark.asyncio
async def test_all_five_rewind_categories_route_to_their_expected_nodes():
    """Summary proof (item 6, cross-referenced): every one of the five
    FailureCategory values routes to its own earliest-affected node --
    a fresh run per category (never chaining rewinds on one shared run,
    which would need to reason about `rewind()`'s own "strictly earlier"
    check across each successive call) -- and the resulting five nodes
    collapse onto exactly 4 distinct values (METHOD_ERROR and
    REGULATION_ERROR legitimately share `decide`, per section 56's own
    diagram), never all 5 the same.
    """
    store = MemoryControlStore()
    orch = Manager(store, TaskService(store, CapabilityPolicy(None)))

    async def _routed_node(signal) -> str:
        run = await orch.start_run(session_id="b" * 32, principal="operator-1")
        for outcome in ("ok", "ok", "ok", "ok", "proceed", "ok", "ok", "ok", "clean", "ok"):
            run = await orch.advance(run_id=run.run_id, outcome=outcome)
        assert run.node == "report_ledger"
        _decision, rewound = await RewindRouter.route(
            super_orchestrator=orch, run_id=run.run_id, signal=signal,
        )
        return rewound.node

    nodes_by_category = {
        "EXECUTION_ERROR": await _routed_node({"failure_class": "EXECUTION_ERROR"}),
        "METHOD_ERROR": await _routed_node({"failure_class": "METHOD_ERROR"}),
        "REGULATION_ERROR": await _routed_node({"failure_class": "REGULATION_ERROR"}),
        "SEMANTIC_ERROR": await _routed_node("SPECIALIST_INTERPRETATION_ERROR"),
        "UNRESOLVED_UNCERTAINTY": await _routed_node({"failure_class": "HUMAN_REVIEW_REQUIRED"}),
    }

    assert nodes_by_category == {
        "EXECUTION_ERROR": "execute",
        "METHOD_ERROR": "decide",
        "REGULATION_ERROR": "decide",
        "SEMANTIC_ERROR": "specialists",
        "UNRESOLVED_UNCERTAINTY": "human_review_audit",
    }
    assert len(set(nodes_by_category.values())) == 4, (
        "5 categories collapse onto 4 distinct nodes -- METHOD_ERROR and "
        "REGULATION_ERROR legitimately share 'decide', every other category is distinct"
    )
