"""Phase 2 mandatory acceptance tests for `CapabilityPolicy` (F-CAP-001).

Written during the Phase 8 adversarial pass after discovering this file
was never actually created when Phase 2 closed, despite being named in
the master plan's acceptance-test table. Covers 4 of the plan's 5 named
tests against real, existing code; the 5th
(`test_untrusted_text_cannot_change_grants_tools_evidence_gates_publication_or_workflow_state`)
is not attempted here -- it needs a concrete enumeration of "untrusted
text" injection points across five distinct sources this pass did not
have grounds to construct honestly, and is recorded as a residual gap in
`docs/assurance/FINDINGS.md` (F-CAP-001) rather than faked.
"""
from __future__ import annotations

import dataclasses

import pytest
from motor.motor_asyncio import AsyncIOMotorDatabase
from phi_core.control.context import AgentContext
from phi_core.control.policy import MANIFESTS, CapabilityDenied, CapabilityPolicy
from phi_core.control.testing import _TestLlmConfig
from pydantic import ValidationError

_NON_RESEARCH_AGENTS = ["Judge", "Sentinel", "Auditor", "Ledger", "Herald", "Lexicon", "Instrument", "Manager"]


def _issue(agent: str):
    policy = CapabilityPolicy(_TestLlmConfig())
    manifest = MANIFESTS[agent]
    task_type = next(iter(manifest.task_types))
    grant = policy.issue_grant(run_id="r" * 32, task_id="t" * 32, agent=agent, task_type=task_type)
    return policy, grant, manifest


@pytest.mark.parametrize("agent", _NON_RESEARCH_AGENTS)
def test_nonresearch_agent_denied_web_search(agent: str) -> None:
    policy, grant, manifest = _issue(agent)
    assert "web_search" not in manifest.allowed_tools, f"{agent} manifest unexpectedly grants web_search"
    with pytest.raises(CapabilityDenied):
        policy.check_tool(grant, "web_search")


def test_agent_cannot_widen_its_own_grant() -> None:
    policy, grant, manifest = _issue("Judge")

    # Reassigning a field on a frozen record raises.
    with pytest.raises(ValidationError):
        grant.tools = {"web_search": 3}  # type: ignore[misc]

    # The frozen mapping itself cannot be mutated in place either.
    with pytest.raises(TypeError):
        grant.tools["web_search"] = 3  # type: ignore[index]

    # A hand-built grant claiming a wider tool set than the manifest is
    # rejected -- the policy re-derives authority from MANIFESTS, it does
    # not trust whatever the grant object itself claims.
    widened = grant.model_copy(update={"tools": {**dict(manifest.allowed_tools), "web_search": 3}})
    with pytest.raises(CapabilityDenied):
        policy.check_tool(widened, "web_search")


def test_restricted_phi_is_never_sendable() -> None:
    """Every manifest, no exception, is denied for `restricted_phi`."""
    policy = CapabilityPolicy(_TestLlmConfig())
    for agent, manifest in MANIFESTS.items():
        task_type = next(iter(manifest.task_types))
        grant = policy.issue_grant(run_id="r" * 32, task_id="t" * 32, agent=agent, task_type=task_type)
        with pytest.raises(CapabilityDenied):
            policy.check_data_class(grant, "restricted_phi")


def test_agent_cannot_widen_its_accepted_input_class_ceiling() -> None:
    """`check_data_class` refuses a class above the *grant's* ceiling
    regardless of what a caller passes -- there is no argument that lets
    a caller assert a narrower classification than the ceiling the
    manifest actually derived and have that override accepted. Uses the
    narrowest-ceiling manifest so a class above it is guaranteed to
    exist."""
    policy = CapabilityPolicy(_TestLlmConfig())
    narrowest_agent = min(MANIFESTS, key=lambda name: {
        "public": 0, "internal": 1, "restricted_metadata": 2, "restricted_phi": 3,
    }[MANIFESTS[name].data_class_ceiling])
    manifest = MANIFESTS[narrowest_agent]
    assert manifest.data_class_ceiling != "restricted_phi", "test needs an agent with room above its ceiling"
    above_ceiling = {"public": "internal", "internal": "restricted_metadata",
                      "restricted_metadata": "restricted_phi"}[manifest.data_class_ceiling]
    task_type = next(iter(manifest.task_types))
    grant = policy.issue_grant(run_id="r" * 32, task_id="t" * 32, agent=narrowest_agent, task_type=task_type)

    with pytest.raises(CapabilityDenied):
        policy.check_data_class(grant, above_ceiling)


def test_agents_receive_no_database_handle() -> None:
    """No `AgentContext` field and no `Agent` attribute is an
    `AsyncIOMotorDatabase` -- agents reach the provider, tools, cache,
    artifacts, and evidence only through the narrow facades `AgentContext`
    exposes, never a raw Mongo handle."""
    for f in dataclasses.fields(AgentContext):
        assert f.type is not AsyncIOMotorDatabase, f"AgentContext.{f.name} is typed as a raw database handle"

    from phi_core.control.testing import make_ctx

    ctx = make_ctx("Judge")
    for name in dir(ctx):
        if name.startswith("_"):
            continue
        value = getattr(ctx, name, None)
        assert not isinstance(value, AsyncIOMotorDatabase), f"AgentContext.{name} holds a raw database handle"
