"""Phase 2B (authorization and provider control) genuine-gap coverage:
secret scan/blocking wired into ``ProviderGateway``, the named
``AuthorizationService``/``AgentContractRegistry`` re-exposure of
``CapabilityPolicy``/``MANIFESTS``, unwired response-schema validation,
and the ``prompt_version`` egress field.
"""
from __future__ import annotations

import pytest
from phi_core.agents.llm import LlmConfig
from phi_core.control.authorization import authorize_capability, get_contract
from phi_core.control.egress import canonical_payload
from phi_core.control.gateway import GatewayRequest, ProviderGateway
from phi_core.control.policy import MANIFESTS, CapabilityDenied, CapabilityPolicy
from phi_core.control.schema_validation import ResponseSchemaError, validate_response_schema
from phi_core.control.secrets_scan import contains_secret, find_secrets
from phi_core.control.store import MemoryControlStore


def _request(**overrides: object) -> GatewayRequest:
    fields: dict[str, object] = dict(
        session_id="session",
        run_id="run",
        task_id="task",
        agent="RegulationsExpert",
        attempt=1,
        purpose="research",
        input_class="internal",
        grant_id="grant",
        provider="anthropic",
        model="claude-test",
        endpoint="",
        system_prompt="system",
        user_prompt="user",
        coaching_note=None,
        tool_results=(),
        allowed_tools={},
        response_schema="research_evidence",
        timeout_s=30.0,
        max_tokens=100,
        max_cost_usd=0.01,
        policy_version="policy/1",
    )
    fields.update(overrides)
    return GatewayRequest(**fields)


# --- secret scan -----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "AKIAABCDEFGHIJKLMNOP",
        "sk-ant-api03-abcdefghijklmnopqrstuvwx",
        "ghp_abcdefghijklmnopqrstuvwxyz012345",
        "xoxb-1234567890-abcdefghij",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop",
        "-----BEGIN RSA PRIVATE KEY-----",
    ],
)
def test_find_secrets_matches_known_credential_shapes(text: str) -> None:
    assert find_secrets(text)


def test_find_secrets_ignores_ordinary_text() -> None:
    assert find_secrets("a study of column headers and HIPAA identifiers") == ()
    assert find_secrets("") == ()


def test_contains_secret_recurses_through_gateway_payload_shape() -> None:
    messages = [{"role": "user", "content": "here is my key AKIAABCDEFGHIJKLMNOP please use it"}]
    assert contains_secret(messages) is True
    assert contains_secret([{"role": "user", "content": "nothing sensitive here"}]) is False


@pytest.mark.asyncio
async def test_gateway_denies_a_secret_bearing_prompt_before_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHI_ENV", "dev")
    # Presidio's spaCy/thinc/numpy chain can be ABI-broken in a given local
    # interpreter independent of this repo's code; stub it out so this test
    # exercises the rule detector plus the secret scan, not that install.
    import phi_core.detectors as detectors

    monkeypatch.setattr(detectors, "presidio_detect", lambda text: [])
    store = MemoryControlStore()
    grant = CapabilityPolicy(LlmConfig(provider="anthropic", model="claude-test")).issue_grant(run_id="run", task_id="task", agent="RegulationsExpert", task_type="regulationsexpert")
    await store.insert("capability_grants", grant.model_dump())
    gateway = ProviderGateway(store)

    req = _request(
        grant_id=grant.grant_id,
        model="claude-test",
        user_prompt="use this key sk-ant-api03-abcdefghijklmnopqrstuvwx to call the api",
    )
    result = await gateway.complete(req)

    assert result.status == "denied"
    assert result.denial_reason == "secret_detected"


@pytest.mark.asyncio
async def test_gateway_accepts_an_openrouter_upstream_vendor(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenRouter brokers the call, so its response names the upstream compute
    vendor that served it. That is not a substitution of the authorized
    provider and must not be refused after the tokens are paid for."""
    monkeypatch.setenv("PHI_ENV", "dev")
    import phi_core.detectors as detectors

    monkeypatch.setattr(detectors, "presidio_detect", lambda text: [])
    store = MemoryControlStore()
    policy = CapabilityPolicy(LlmConfig(provider="openrouter", model="minimax/minimax-m3:free"))
    grant = policy.issue_grant(run_id="run", task_id="task", agent="RegulationsExpert", task_type="regulationsexpert")
    await store.insert("capability_grants", grant.model_dump())
    gateway = ProviderGateway(store)

    class _Message:
        content = '{"ok": true}'
        tool_calls = None

    class _Choice:
        message = _Message()
        finish_reason = "stop"

    class _Response:
        id = "resp-1"
        provider = "GMICloud"
        model = "minimax/minimax-m3:free"
        choices = (_Choice(),)
        usage = None

    monkeypatch.setattr(ProviderGateway, "_completion", staticmethod(lambda *a, **k: _Response()))

    result = await gateway.complete(
        _request(
            grant_id=grant.grant_id, provider="openrouter", model="minimax/minimax-m3:free",
            response_schema="", user_prompt="classify these headers",
        )
    )

    assert result.denial_reason != "provider_mismatch"
    assert result.status == "ok"
    assert result.provider == "openrouter"


# --- authorization / contract registry --------------------------------------


def test_get_contract_returns_the_manifest_for_a_known_agent() -> None:
    assert get_contract("RegulationsExpert") is MANIFESTS["RegulationsExpert"]


def test_get_contract_denies_an_unknown_agent() -> None:
    with pytest.raises(CapabilityDenied):
        get_contract("NotAnAgent")


def test_authorize_capability_denies_a_provider_mismatch() -> None:
    policy = CapabilityPolicy(LlmConfig(provider="anthropic", model="claude-test"))
    grant = policy.issue_grant(run_id="run", task_id="task", agent="RegulationsExpert", task_type="regulationsexpert")
    with pytest.raises(CapabilityDenied):
        authorize_capability(policy, grant, provider="openai", model="x", endpoint="", data_class="internal")


def test_authorize_capability_denies_restricted_phi() -> None:
    policy = CapabilityPolicy(LlmConfig(provider="anthropic", model="claude-test"))
    grant = policy.issue_grant(run_id="run", task_id="task", agent="RegulationsExpert", task_type="regulationsexpert")
    with pytest.raises(CapabilityDenied):
        authorize_capability(
            policy, grant, provider=grant.provider, model=grant.model, endpoint=grant.endpoint,
            data_class="restricted_phi",
        )


def test_authorize_capability_allows_a_matching_grant() -> None:
    policy = CapabilityPolicy(LlmConfig(provider="anthropic", model="claude-test"))
    grant = policy.issue_grant(run_id="run", task_id="task", agent="RegulationsExpert", task_type="regulationsexpert")
    authorize_capability(
        policy, grant, provider=grant.provider, model=grant.model, endpoint=grant.endpoint, data_class="internal"
    )


# --- response schema validation (implemented, not wired into the gateway) --


def test_validate_response_schema_accepts_matching_shape() -> None:
    assert validate_response_schema("column_decision", '{"file_id": "f1"}') == {"file_id": "f1"}
    assert validate_response_schema("audit_report", '{"ok": true}') == {"ok": True}


def test_validate_response_schema_rejects_non_json() -> None:
    with pytest.raises(ResponseSchemaError):
        validate_response_schema("audit_report", "not json")


def test_validate_response_schema_rejects_wrong_top_level_shape() -> None:
    with pytest.raises(ResponseSchemaError):
        validate_response_schema("column_decision", '["not", "an", "object"]')


def test_validate_response_schema_rejects_unknown_schema_name() -> None:
    with pytest.raises(ResponseSchemaError):
        validate_response_schema("no_such_schema", "{}")


# --- prompt_version egress field --------------------------------------------


def test_gateway_request_prompt_version_defaults_empty() -> None:
    assert _request().prompt_version == ""


def test_canonical_payload_binds_prompt_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHI_ENV", "dev")
    req_a = _request(prompt_version="prompt/1")
    req_b = _request(prompt_version="prompt/2")
    messages = [{"role": "user", "content": "user"}]
    payload_a = canonical_payload(request=req_a, decision={"status": "ok", "denial_reason": ""}, messages=messages, tools=[])
    payload_b = canonical_payload(request=req_b, decision={"status": "ok", "denial_reason": ""}, messages=messages, tools=[])

    assert payload_a != payload_b
    assert b'"prompt_version":"prompt/1"' in payload_a
