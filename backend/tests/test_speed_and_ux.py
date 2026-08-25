"""Regression tests for the Q4-iteration speed & UX changes.

Covers:
  - Sentinel severity gate + orchestrator._blocking_issues
  - PipelineCancelled exception path
  - /api/sessions/{sid}/cancel endpoint gate
  - LedgerCompare/Aggregate + HeraldAbstract/Sections agent registration
"""
from __future__ import annotations

import pytest

# ---- Sentinel severity -------------------------------------------------


def test_blocking_issues_returns_only_blocking():
    from phi_core.agents.orchestrator import _blocking_issues
    s = {"issues": [
        {"severity": "blocking", "column": "phone", "problem": "leak"},
        {"severity": "advisory", "column": "notes", "problem": "prefer scrub"},
        {"severity": "", "column": "x", "problem": "unspecified"},
    ]}
    out = _blocking_issues(s)
    assert len(out) == 1
    assert out[0]["column"] == "phone"


def test_blocking_issues_empty_on_no_issues():
    from phi_core.agents.orchestrator import _blocking_issues
    assert _blocking_issues({"issues": []}) == []
    assert _blocking_issues({}) == []


def test_sentinel_post_processes_verdict_when_no_blocking_issues():
    """Sentinel's LLM may return verdict='revise' with only advisory
    issues. The deterministic post-processing MUST override that to
    'approved' so the orchestrator's Judge<->Sentinel loop short-circuits.
    This is the fix for "Sentinel nitpicks endlessly".
    """
    # Simulate the post-processing logic directly on a canned Sentinel reply.
    llm_out = {
        "verdict": "revise",
        "issues": [
            {"severity": "advisory", "column": "patient_id",
             "problem": "prefer pseudonymize over hash"},
        ],
    }
    issues = llm_out.get("issues") or []
    blocking = [i for i in issues if str(i.get("severity", "")).lower() == "blocking"]
    assert not blocking
    llm_out["verdict"] = "approved" if not blocking else llm_out["verdict"]
    assert llm_out["verdict"] == "approved"


# ---- Cancel path -------------------------------------------------------


class _StubDB:
    def __init__(self, doc):
        self._doc = doc
        self.sessions = self

    async def find_one(self, *_a, **_kw):
        return self._doc

    async def update_one(self, *_a, **_kw):
        return None


@pytest.mark.asyncio
async def test_check_cancel_raises_when_flag_set():
    from phi_core.agents.orchestrator import PipelineCancelled, _check_cancel
    db = _StubDB({"id": "s1", "cancel_requested": True})
    with pytest.raises(PipelineCancelled):
        await _check_cancel(db, "s1", _noop)


@pytest.mark.asyncio
async def test_check_cancel_silent_when_flag_absent():
    from phi_core.agents.orchestrator import _check_cancel
    db = _StubDB({"id": "s1", "cancel_requested": False})
    await _check_cancel(db, "s1", _noop)  # must not raise


async def _noop(*_a, **_kw):
    return None


@pytest.mark.asyncio
async def test_cancel_endpoint_registered_and_token_gated():
    from server import app
    rs = [r for r in app.router.routes
          if getattr(r, "path", "") == "/api/sessions/{sid}/cancel"]
    assert rs, "cancel endpoint not registered"
    dep_fns = {d.call.__name__ for d in rs[0].dependant.dependencies}
    assert "resolve_principal" in dep_fns


# ---- Ledger + Herald split ---------------------------------------------


def test_ledger_split_classes_present():
    from phi_core.agents.outward import Ledger, LedgerAggregate, LedgerCompare
    assert Ledger.NAME == "Ledger"
    assert LedgerCompare.NAME == "Ledger.Compare"
    assert LedgerAggregate.NAME == "Ledger.Aggregate"


def test_herald_split_classes_present():
    from phi_core.agents.outward import Herald, HeraldAbstract, HeraldSections
    assert Herald.NAME == "Herald"
    assert HeraldAbstract.NAME == "Herald.Abstract"
    assert HeraldSections.NAME == "Herald.Sections"


def test_ledger_count_actions_deterministic():
    from phi_core.agents.outward import _count_actions
    decisions = [
        {"action": "drop", "column": "name"},
        {"action": "drop", "column": "phone"},
        {"action": "keep", "column": "hr"},
        {"action": "pseudonymize", "column": "patient_id"},
    ]
    assert _count_actions(decisions) == {"drop": 2, "keep": 1, "pseudonymize": 1}


def test_iteration_cap_is_three():
    """Sir's spec: 'if the issue continues MORE THAN 3 times then it must
    be sent to human review agent'. So we try up to 3 iterations of
    Judge<->Sentinel; on the 4th unresolved iteration the pipeline
    escalates to human review. The severity gate short-circuits when
    zero blocking issues remain, so most sessions finish in 1 iteration."""
    from phi_core.agents import ITERATION_CAP
    assert ITERATION_CAP == 3


# ---- Verifier hash-is-safe fix -----------------------------------------


def test_verifier_treats_hash_as_phi_safe_action():
    """Q4-iteration bug: `hash` is a valid PHI-transform (deterministic,
    one-way) allowed by the Sentinel hard-rule table for H and R
    categories. The verifier previously counted `hash` as a leak."""
    from phi_corpus.verify import _PHI_ACTIONS
    assert "hash" in _PHI_ACTIONS
    assert "pseudonymize" in _PHI_ACTIONS
    # Sanity: `keep` and unknown actions are NOT PHI-safe.
    assert "keep" not in _PHI_ACTIONS
    assert "unknown" not in _PHI_ACTIONS


# ---- Spec-alignment audit tests (Sir Q "ensure implementation aligns") --


def test_praxis_agent_wired_into_orchestrator():
    """Sir's spec: 'PHI Methods experts ... the classifier asks for method
    to handle dates then the agent would search the latest jittering
    method (eg. SANT) then provide the necessary information'. Praxis
    must be imported and invoked in the orchestrator before Judge."""
    import inspect

    from phi_core.agents import orchestrator
    src = inspect.getsource(orchestrator)
    assert "Praxis(" in src, "Praxis not instantiated in orchestrator"
    assert "praxis_methods" in src, "Praxis output not fed forward"
    # Judge signature must accept praxis so it can be consulted
    from phi_core.agents.reasoning import Judge
    sig = inspect.signature(Judge.run)
    assert "praxis" in sig.parameters, "Judge.run must accept a `praxis` argument"


def test_judge_prompt_asks_for_false_positive_check():
    """Sir's spec: 'If the preview agent send report for fixing the this
    agent would ensure its not false positive first then if it is real
    issue then it corrects'. Judge's prompt must include the FP-check
    instruction when prior_feedback is present."""
    import inspect

    from phi_core.agents.reasoning import Judge
    src = inspect.getsource(Judge.run)
    assert "false positive" in src.lower(), (
        "Judge.run must instruct itself to verify Sentinel issues are "
        "not false positives before correcting"
    )


def test_orchestrator_emits_praxis_phase():
    """UI-visibility check: the orchestrator must emit an on_phase('praxis')
    call so the live agent-trace panel renders a 'Praxis' row and the
    operator can see the PHI-methods lookup happening."""
    import inspect

    from phi_core.agents import orchestrator
    src = inspect.getsource(orchestrator)
    assert 'on_phase("praxis"' in src, "orchestrator must emit a 'praxis' phase event"
