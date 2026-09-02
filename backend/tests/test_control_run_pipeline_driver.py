"""Wave 4b: ``run_pipeline`` thin-driver invariants.

The plan's own note: "SuperOrchestrator is the only writer of
workflow_runs.node" already passes today and cannot detect this phase's
failure (nothing ever asserted *how* ``run_pipeline`` decided what ran
next). These three tests replace it with invariants that genuinely fail
before Wave 4b and pass after:

1. dispatch order is dictated by ``SuperOrchestrator.advance``, not chosen
   by ``run_pipeline`` itself;
2. a terminal/blocked ``advance`` result on the very first call dispatches
   zero agents;
3. ``run_pipeline``'s own function body never constructs a known agent
   class directly -- every dispatch goes through a registry (AST scan,
   modeled on ``test_architecture_boundaries.py``'s pattern, with a
   positive control so a scan that matches nothing fails loudly instead
   of silently passing).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from phi_core.agents import orchestrator
from phi_core.control.records import WorkflowRun
from phi_core.control.superorchestrator import SuperOrchestrator

BACKEND_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATOR_PATH = BACKEND_ROOT / "phi_core" / "agents" / "orchestrator.py"

# The set of agent classes `run_pipeline` must never construct inline --
# the same 7 names its pre-Wave-4b body constructed directly (Lexicon,
# Schema, Instrument, Judge, RegulationsExpert, PHIMethodsExpert, Sentinel), plus the wider
# section-13 primary-runtime-agent vocabulary the task names explicitly
# (Reviewer, Executor) so a future re-inlining of the execute-tail dispatch
# agent directly into `run_pipeline` is caught too.
_FORBIDDEN_AGENT_CLASS_NAMES = frozenset({
    "Schema", "Lexicon", "Instrument", "Judge", "RegulationsExpert", "PHIMethodsExpert",
    "Sentinel", "Reviewer", "Executor",
})


async def _noop_on_phase(_phase: str, _payload: dict) -> None:
    return None


async def _noop_emit(_message) -> None:
    return None


# ---- test 1: dispatch order is dictated by advance, not chosen ------------


@pytest.mark.asyncio
async def test_dispatch_order_follows_advance_not_choice(monkeypatch) -> None:
    """``advance`` is monkeypatched to return a fixed, deliberately
    non-obvious node sequence (Instrument before Schema -- the reverse of
    the real pipeline's own dependency-free ordering). ``run_pipeline`` is
    given stub agents that just record their own name; the dispatch order
    must equal the injected sequence exactly, proving `run_pipeline` never
    reorders or chooses -- it only executes what `advance` hands it."""
    injected_nodes = ["Instrument", "Schema", "complete"]
    calls = iter(injected_nodes)

    async def fake_advance(self, *, run_id: str, outcome: str) -> WorkflowRun:
        node = next(calls)
        return WorkflowRun(run_id=run_id, session_id="sid", state="running", node=node)

    monkeypatch.setattr(SuperOrchestrator, "advance", fake_advance)

    dispatched: list[str] = []

    async def stub_instrument(_state):
        dispatched.append("Instrument")
        return "ok"

    async def stub_schema(_state):
        dispatched.append("Schema")
        return "ok"

    registry = {"Instrument": stub_instrument, "Schema": stub_schema}

    result = await orchestrator.run_pipeline(
        {"id": "sid", "files": []},
        db=None,
        llm_cfg=None,
        emit=_noop_emit,
        on_phase=_noop_on_phase,
        run_id="sid",
        dispatch_registry=registry,
    )

    assert dispatched == ["Instrument", "Schema"]
    assert result["status"] == "complete"


# ---- test 2: nothing runs unbidden -----------------------------------------


@pytest.mark.asyncio
async def test_nothing_dispatched_when_advance_returns_terminal_first(monkeypatch) -> None:
    """``advance`` returns a terminal (``blocked``) node on the very first
    call. `run_pipeline` must not dispatch any agent -- it only acts on
    what `advance` authorizes, never in anticipation of it."""

    async def fake_advance(self, *, run_id: str, outcome: str) -> WorkflowRun:
        return WorkflowRun(run_id=run_id, session_id="sid", state="blocked",
                          node="blocked", terminal_outcome="blocked")

    monkeypatch.setattr(SuperOrchestrator, "advance", fake_advance)

    dispatched: list[str] = []

    async def stub_anything(_state):
        dispatched.append("should-never-run")
        return "ok"

    registry = {"Instrument": stub_anything, "Schema": stub_anything}

    result = await orchestrator.run_pipeline(
        {"id": "sid", "files": []},
        db=None,
        llm_cfg=None,
        emit=_noop_emit,
        on_phase=_noop_on_phase,
        run_id="sid",
        dispatch_registry=registry,
    )

    assert dispatched == []
    assert result["status"] == "blocked"


# ---- test 3: run_pipeline never constructs an agent class directly --------


def _run_pipeline_function_def() -> ast.FunctionDef:
    tree = ast.parse(ORCHESTRATOR_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_pipeline":
            return node
    raise AssertionError("run_pipeline function definition not found in orchestrator.py")


def _agent_class_calls_in(func_def: ast.AST) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for node in ast.walk(func_def):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        name = None
        if isinstance(callee, ast.Name):
            name = callee.id
        elif isinstance(callee, ast.Attribute):
            name = callee.attr
        if name in _FORBIDDEN_AGENT_CLASS_NAMES:
            hits.append((node.lineno, name))
    return hits


def test_run_pipeline_never_constructs_agent_classes_directly() -> None:
    """AST scan (modeled on test_architecture_boundaries.py's pattern):
    walk `run_pipeline`'s own FunctionDef body and assert no `ast.Call`
    targets a known agent class by name -- every dispatch must go
    through the registry instead. Positive control: the scan must
    actually find `run_pipeline` and walk a non-trivial number of Call
    nodes, so a scan that silently matches nothing (e.g. a typo'd
    function name) fails loudly rather than vacuously passing."""
    func_def = _run_pipeline_function_def()

    all_calls = [n for n in ast.walk(func_def) if isinstance(n, ast.Call)]
    assert len(all_calls) >= 3, (
        "positive control failed: the scan found fewer than 3 Call nodes inside "
        "run_pipeline -- the scan itself is not walking real code"
    )

    hits = _agent_class_calls_in(func_def)
    assert hits == [], f"run_pipeline constructs an agent class directly: {hits}"
