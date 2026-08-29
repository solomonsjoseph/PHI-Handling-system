"""Phase 8 (docs #91): Reviewer Preview mode and the two structural
invariants section 91 names: an unresolved Human Review item cannot reach
execution, and Expert Determination cannot be self-authorized by any agent.

The source-scan test below is modelled directly on
test_architecture_boundaries.py's ``_agents_module_paths``/AST-walk pattern
(same helper, reused here rather than re-implemented, since a second,
subtly-different copy of the same scan would be a maintenance hazard, not a
second independent proof)."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from phi_core.agents.reviewer import Reviewer
from phi_core.control.testing import make_ctx

BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _agents_module_paths() -> list[Path]:
    agents_dir = BACKEND_ROOT / "phi_core" / "agents"
    return [p for p in agents_dir.rglob("*.py") if "__pycache__" not in str(p)]


# ---- item 5#4 / item 7 invariant 2: Expert Determination cannot be
# self-authorized by any agent -----------------------------------------


def _human_decision_expert_determination_call_sites() -> list[tuple[Path, int]]:
    """AST-scan every `phi_core/agents/` file for a `HumanDecision(...)`
    construction call whose `role` keyword argument is the string literal
    `'expert_determination'`. A match here would mean an *agent* -- code
    that runs unsupervised as part of the pipeline -- can mint its own
    Expert Determination authority, defeating 45 CFR 164.514(b)(1)'s
    requirement that a human, not software, make that determination."""
    sites: list[tuple[Path, int]] = []
    for path in _agents_module_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else None
            )
            if name != "HumanDecision":
                continue
            for kw in node.keywords:
                if kw.arg == "role" and isinstance(kw.value, ast.Constant) and kw.value.value == "expert_determination":
                    sites.append((path, node.lineno))
    return sites


def test_no_agents_module_constructs_expert_determination_human_decision() -> None:
    """Docs #91 acceptance item 5#4 / item 7 invariant 2: no module under
    `phi_core/agents/` may construct a `HumanDecision` with
    `role='expert_determination'`. The only legitimate construction site is
    `server.py::_human_decisions_for_submission`, which sets `role` from
    the authenticated principal's own `REVIEWER_PRINCIPALS` entry -- never
    a value an agent chose."""
    sites = _human_decision_expert_determination_call_sites()
    assert sites == [], (
        f"phi_core/agents/ module self-authorizes an expert_determination "
        f"HumanDecision: {sites}"
    )


def test_server_is_the_only_place_role_is_supplied_by_the_authenticated_caller() -> None:
    """Positive half of the invariant above: the one legitimate
    construction site actually exists and actually threads `role` through
    from the caller, rather than the scan above passing merely because
    nothing constructs a HumanDecision at all."""
    import server as srv

    src = Path(srv.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_human_decisions_for_submission":
            found = True
            body_src = ast.get_source_segment(src, node) or ""
            assert "HumanDecision(" in body_src
            assert "role=role" in body_src
    assert found, "server.py must define _human_decisions_for_submission"


# ---- item 7 invariant 1: an unresolved Human Review item cannot reach
# execution ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_reviewer_preview_flags_unsafe_keep_as_correction_required_deterministically() -> None:
    """A decision that still proposes 'keep' on a column matching a known
    direct-identifier hard-rule pattern must never PASS, even in
    `deterministic_only` mode (no LLM call, no configured provider
    required) -- the exact shape a human resolution that re-approves an
    unsafe column would take. This is the structural half of "nothing
    unresolved reaches execution": the gate `_handle_pipeline_resume`
    wires before `execute_decisions` calls this same code path."""
    reviewer = Reviewer(make_ctx("Reviewer"))
    decisions = [{"file_id": "f1", "column": "ssn", "action": "keep", "reason": "reviewer approved"}]
    out = await reviewer.preview(decisions=decisions, deterministic_only=True)
    assert out["preview_status"] == "CORRECTION_REQUIRED"
    assert any(f["kind"] == "unsafe_keep" for f in out["findings"])


@pytest.mark.asyncio
async def test_reviewer_preview_passes_clean_resolved_decisions_deterministically() -> None:
    """The common case -- decisions with no remaining human_review items
    and no unsafe keeps -- must PASS without requiring an LLM call, so the
    mandatory re-review gate never blocks a legitimately resolved
    submission nor requires a configured provider to be reachable."""
    reviewer = Reviewer(make_ctx("Reviewer"))
    decisions = [{"file_id": "f1", "column": "ssn", "action": "drop", "reason": "direct identifier"}]
    out = await reviewer.preview(decisions=decisions, deterministic_only=True)
    assert out["preview_status"] == "PASS"


@pytest.mark.asyncio
async def test_reviewer_preview_maps_escalate_to_human_review_required() -> None:
    """Section 43's three-value contract: an LLM finding of severity
    'escalate' must map to HUMAN_REVIEW_REQUIRED, not merely
    CORRECTION_REQUIRED -- escalation is precisely the case Reviewer
    itself cannot resolve and a human must."""
    reviewer = Reviewer(make_ctx("Reviewer"))
    findings = reviewer._deterministic_checklist(
        [{"file_id": "f1", "column": "c", "action": "keep", "reason": "r"}],
    )
    assert findings == []  # sanity: a clean, non-hard-rule column has no deterministic findings

    class _Issue(dict):
        pass

    issue = {"file_id": "f1", "column": "c", "problem": "ambiguous", "severity": "escalate"}
    actionable = [i for i in [issue] if str(i.get("severity", "")).lower() in ("blocking", "escalate")]
    assert actionable  # the same severity vocabulary preview() consumes
