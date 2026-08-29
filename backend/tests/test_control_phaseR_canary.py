"""Wave R-d: the egress leak-canary gate (spec sections 71/72).

``CanarySet``/``activate_canary_set`` unit contracts, then the full
``ProviderGateway.complete`` integration: a canary literal embedded in the
outbound payload must block the send (the provider is never reached),
raise ``canary.SecurityBoundaryViolation``, and record a ``TraceEvent``
carrying only ``{canary_scan: "violation", canary_id: ..., hit_count: N}``
-- never the matched literal. A clean payload records
``{canary_scan: "clean"}`` alongside the same event's ``egress_digest``.
``ToolGateway.search`` gets the identical treatment via the same
``ProviderGateway.complete`` choke point its ``query`` argument flows
through.

Follows ``test_control_bounds.py``'s gateway rig (``_open_gateway_run`` /
``_issue_gateway_grant`` / ``_gateway_request``) so a real grant and run
pass ``_validate_request`` before the canary scan point is reached.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from phi_core.control import canary
from phi_core.control.gateway import GatewayRequest, ProviderGateway, ToolGateway
from phi_core.control.policy import POLICY_VERSION, CapabilityPolicy
from phi_core.control.records import ResourceBudget, ResourceUsage, WorkflowRun
from phi_core.control.store import MemoryControlStore

RUN_ID = "canary-run-" + "r" * 20
TASK_ID = "canary-task-" + "t" * 20
SESSION_ID = "canary-session-" + "s" * 16


# ---------------------------------------------------------------------------
# CanarySet / activate_canary_set unit contracts
# ---------------------------------------------------------------------------


def test_canaryset_detects_single_and_multi_token_literals_case_insensitively() -> None:
    cs = canary.CanarySet("run", ["MRN-99887766", "Jane Doe", "ab"])

    assert cs.scan_text("nothing relevant here").hit is False
    hit = cs.scan_text("patient mrn-99887766 admitted")
    assert hit.hit is True and hit.hit_count == 1
    hit2 = cs.scan_text("seen by Jane Doe today")
    assert hit2.hit is True and hit2.hit_count == 1
    # Below the 4-character uniqueness floor: never matches.
    assert cs.scan_text("ab").hit is False


def test_canaryset_scan_result_never_carries_the_raw_literal() -> None:
    cs = canary.CanarySet("run", ["SUPERSECRETCANARY1234"])
    result = cs.scan_text("leaked: SUPERSECRETCANARY1234")

    assert result.hit is True
    assert "SUPERSECRETCANARY1234".lower() not in result.canary_id.lower()
    assert result.canary_id and result.canary_id != "SUPERSECRETCANARY1234"


def test_canaryset_id_is_deterministic_per_run_and_differs_across_runs() -> None:
    cs_a1 = canary.CanarySet("run-a", ["ZZZDETERMINISTIC1"])
    cs_a2 = canary.CanarySet("run-a", ["ZZZDETERMINISTIC1"])
    cs_b = canary.CanarySet("run-b", ["ZZZDETERMINISTIC1"])

    id_a1 = cs_a1.scan_text("x ZZZDETERMINISTIC1 x").canary_id
    id_a2 = cs_a2.scan_text("x ZZZDETERMINISTIC1 x").canary_id
    id_b = cs_b.scan_text("x ZZZDETERMINISTIC1 x").canary_id

    assert id_a1 == id_a2
    assert id_a1 != id_b


def test_activate_canary_set_builds_from_ground_truth_planted_leak_literals() -> None:
    ground_truth = {
        "planted": [
            {"plant_id": "p1", "leak_literals": ["ZZZGTLITERAL9999"]},
            {"plant_id": "p2", "leak_literals": []},
        ]
    }
    try:
        cs = canary.activate_canary_set("gt-run", ground_truth)
        assert canary.active_canary_set("gt-run") is cs
        assert cs.scan_text("contains ZZZGTLITERAL9999 here").hit is True
    finally:
        canary.deactivate_canary_set("gt-run")
    assert canary.active_canary_set("gt-run") is None


# ---------------------------------------------------------------------------
# ProviderGateway.complete integration rig (mirrors test_control_bounds.py)
# ---------------------------------------------------------------------------

def _llm_cfg() -> SimpleNamespace:
    return SimpleNamespace(provider="anthropic", model="claude-test", base_url="")


async def _issue_gateway_grant(store: MemoryControlStore) -> object:
    policy = CapabilityPolicy(_llm_cfg())
    grant = policy.issue_grant(run_id=RUN_ID, task_id=TASK_ID, agent="Statute", task_type="statute")
    await store.insert("capability_grants", grant)
    return grant


async def _open_gateway_run(store: MemoryControlStore) -> WorkflowRun:
    run = WorkflowRun(
        run_id=RUN_ID, session_id=SESSION_ID, workflow_version="wf/1", policy_version=POLICY_VERSION,
        run_type="study", state="running", node="charter",
        started_at=datetime.now(timezone.utc).isoformat(),
        budget=ResourceBudget(), usage=ResourceUsage(),
    )
    await store.insert("workflow_runs", run)
    return run


def _gateway_request(*, grant_id: str, **overrides) -> GatewayRequest:
    fields: dict = dict(
        session_id=SESSION_ID, run_id=RUN_ID, task_id=TASK_ID, agent="Statute", attempt=1,
        purpose="research", input_class="internal", grant_id=grant_id,
        provider="anthropic", model="claude-test", endpoint="",
        system_prompt="system", user_prompt="user", coaching_note=None, tool_results=(),
        allowed_tools={}, response_schema="research_evidence",
        timeout_s=30.0, max_tokens=100, max_cost_usd=0.01, policy_version=POLICY_VERSION,
    )
    fields.update(overrides)
    return GatewayRequest(**fields)


class _FakeResponse:
    choices = [SimpleNamespace(message=SimpleNamespace(content="a clean, ordinary reply"))]
    usage = {"total_tokens": 7}
    provider = "anthropic"
    model = "claude-test"
    id = "resp-1"


def _never_called_completion(**kwargs):
    raise AssertionError("litellm.completion must never be called once a canary hit is detected")


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_canary_in_outbound_payload_blocks_send_and_raises_security_boundary_violation(monkeypatch) -> None:
    from phi_core.control import gateway as gateway_module

    monkeypatch.setattr(gateway_module.litellm, "completion", _never_called_completion)
    store = MemoryControlStore()
    await _open_gateway_run(store)
    grant = await _issue_gateway_grant(store)
    req = _gateway_request(grant_id=grant.grant_id, user_prompt="patient record MRN-CANARY-LEAK-0001 attached")
    canary.activate_canary_set(RUN_ID, {"planted": [{"plant_id": "p1", "leak_literals": ["MRN-CANARY-LEAK-0001"]}]})

    try:
        with pytest.raises(canary.SecurityBoundaryViolation) as excinfo:
            await ProviderGateway(store).complete(req)
        assert excinfo.value.hit_count >= 1
        assert excinfo.value.canary_id
        assert "MRN-CANARY-LEAK-0001" not in str(excinfo.value)

        events = await store.find_many("trace_events", {"run_id": RUN_ID})
        violation_events = [e for e in events if e.get("payload", {}).get("canary_scan") == "violation"]
        assert len(violation_events) == 1
        event = violation_events[0]
        assert event["payload"] == {
            "canary_scan": "violation",
            "canary_id": excinfo.value.canary_id,
            "hit_count": excinfo.value.hit_count,
        }
        assert event["failure_class"] == "SECURITY_BOUNDARY_VIOLATION"
        assert event["egress_digest"]
        # Never the raw literal or a reconstructable fragment of it.
        assert "MRN-CANARY-LEAK-0001" not in str(event)
        assert "mrn-canary-leak-0001" not in str(event).lower()
    finally:
        canary.deactivate_canary_set(RUN_ID)


@pytest.mark.asyncio
async def test_clean_run_records_canary_scan_clean_alongside_egress_digest(monkeypatch) -> None:
    from phi_core.control import gateway as gateway_module

    monkeypatch.setattr(gateway_module.litellm, "completion", lambda **kwargs: _FakeResponse())
    store = MemoryControlStore()
    await _open_gateway_run(store)
    grant = await _issue_gateway_grant(store)
    req = _gateway_request(grant_id=grant.grant_id, user_prompt="ordinary, canary-free request")
    canary.activate_canary_set(RUN_ID, {"planted": [{"plant_id": "p1", "leak_literals": ["MRN-CANARY-LEAK-0001"]}]})

    try:
        result = await ProviderGateway(store).complete(req)
        assert result.status == "ok"
        assert result.egress_digest

        events = await store.find_many("trace_events", {"run_id": RUN_ID})
        clean_events = [e for e in events if e.get("payload", {}).get("canary_scan") == "clean"]
        assert len(clean_events) == 1
        assert clean_events[0]["egress_digest"] == result.egress_digest
        assert clean_events[0]["payload"] == {"canary_scan": "clean"}
    finally:
        canary.deactivate_canary_set(RUN_ID)


@pytest.mark.asyncio
async def test_no_active_canary_set_never_scans_and_never_records_a_verdict_event(monkeypatch) -> None:
    """The overwhelming common case (no acceptance harness registered a
    CanarySet for this run_id): zero behavioral change from before this
    wave -- no scan runs, no extra TraceEvent is written."""
    from phi_core.control import gateway as gateway_module

    monkeypatch.setattr(gateway_module.litellm, "completion", lambda **kwargs: _FakeResponse())
    store = MemoryControlStore()
    await _open_gateway_run(store)
    grant = await _issue_gateway_grant(store)
    req = _gateway_request(grant_id=grant.grant_id, user_prompt="MRN-CANARY-LEAK-0001 would match if scanned")
    assert canary.active_canary_set(RUN_ID) is None

    result = await ProviderGateway(store).complete(req)

    assert result.status == "ok"
    events = await store.find_many("trace_events", {"run_id": RUN_ID})
    assert all("canary_scan" not in e.get("payload", {}) for e in events)


@pytest.mark.asyncio
async def test_toolgateway_search_with_canary_query_is_blocked_via_shared_gateway_path(monkeypatch) -> None:
    """Section 36's surface: `ToolGateway.search`'s ``query`` argument
    becomes ``GatewayRequest.user_prompt`` (via ``dataclasses.replace``)
    and flows through the exact same ``ProviderGateway.complete`` payload
    assembly and canary scan the direct-completion path above already
    proves -- no separate scan call is needed inside ``search`` itself."""
    from phi_core.control import gateway as gateway_module

    monkeypatch.setattr(gateway_module.litellm, "completion", _never_called_completion)
    store = MemoryControlStore()
    await _open_gateway_run(store)
    grant = await _issue_gateway_grant(store)
    req = _gateway_request(grant_id=grant.grant_id, allowed_tools={"web_search": 2})
    tool_gateway = ToolGateway(ProviderGateway(store))
    canary.activate_canary_set(RUN_ID, {"planted": [{"plant_id": "p1", "leak_literals": ["MRN-CANARY-LEAK-0001"]}]})

    try:
        with pytest.raises(canary.SecurityBoundaryViolation):
            await tool_gateway.search(req=req, query="find records for MRN-CANARY-LEAK-0001")

        events = await store.find_many("trace_events", {"run_id": RUN_ID})
        violation_events = [e for e in events if e.get("payload", {}).get("canary_scan") == "violation"]
        assert len(violation_events) == 1
    finally:
        canary.deactivate_canary_set(RUN_ID)
