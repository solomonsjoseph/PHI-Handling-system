"""Focused D6 contracts for egress and the provider tool boundary."""
from __future__ import annotations

from pathlib import Path

import pytest
from phi_core.control.egress import canonical_payload, egress_digest
from phi_core.control.gateway import GatewayRequest, ProviderGateway, ToolGateway
from phi_core.control.opaque import OpaqueLookupError, OpaqueMap
from phi_core.control.store import MemoryControlStore


def _request(*, provider: str = "anthropic") -> GatewayRequest:
    return GatewayRequest(
        session_id="session",
        run_id="run",
        task_id="task",
        agent="Statute",
        attempt=1,
        purpose="research",
        input_class="internal",
        grant_id="grant",
        provider=provider,
        model="claude-test" if provider == "anthropic" else "other-test",
        endpoint="",
        system_prompt="system",
        user_prompt="user",
        coaching_note=None,
        tool_results=(),
        allowed_tools={"web_search": 3},
        response_schema="research_evidence",
        timeout_s=30.0,
        max_tokens=100,
        max_cost_usd=0.01,
        policy_version="policy/1",
    )


def test_egress_digest_binds_identity_decision_messages_and_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHI_ENV", "dev")
    req = _request()
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "user"}]
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
    allowed = canonical_payload(request=req, decision={"status": "ok", "denial_reason": ""}, messages=messages, tools=tools)
    denied = canonical_payload(request=req, decision={"status": "denied", "denial_reason": "tool_denied"}, messages=messages, tools=tools)

    assert egress_digest(allowed) != egress_digest(denied)
    assert b'"egress_schema":2' in allowed


def test_opaque_tokens_are_run_scoped_and_fail_closed_for_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHI_ENV", "dev")
    mapping: dict[str, str] = {}
    opaque = OpaqueMap("run", mapping)
    token = opaque.to_opaque("file", "file-id")

    assert opaque.from_opaque(token) == "file-id"
    with pytest.raises(OpaqueLookupError):
        opaque.from_opaque("file_unknown")


@pytest.mark.asyncio
async def test_non_anthropic_search_is_denied_without_a_completion() -> None:
    tool_gateway = ToolGateway(ProviderGateway(MemoryControlStore()))

    result = await tool_gateway.search(req=_request(provider="openai"), query="search this")

    assert result.status == "denied"
    assert result.denial_reason == "tool_unsupported_by_provider"


def test_only_gateway_has_a_production_litellm_import() -> None:
    root = Path(__file__).resolve().parents[1] / "phi_core"
    imports = []
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "import litellm" in source or "from litellm" in source:
            imports.append(path.relative_to(root).as_posix())

    assert sorted(imports) == ["control/gateway.py"]
