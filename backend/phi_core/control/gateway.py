"""The sole production LiteLLM inference and research-tool boundary."""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Literal, Mapping

import litellm
from fastapi import HTTPException

from phi_core.anonymizer import scrub_for_prompt
from phi_core.security import scrub_persisted_text, validate_llm_base_url, validate_llm_provider

from .egress import _STRUCTURAL_KEYS, canonical_payload, egress_digest
from .policy import CapabilityDenied, CapabilityPolicy
from .records import CapabilityGrant, DataClass
from .store import ControlStore

litellm.suppress_debug_info = True
litellm.drop_params = True

_URL_RE = re.compile(r"https?://[^\s\)\]\"']+")


@dataclass(frozen=True)
class ToolResult:
    tool: str
    tool_request_id: str
    content: str
    status: str
    citations: tuple[str, ...] = ()
    denial_reason: str = ""


@dataclass(frozen=True)
class GatewayRequest:
    session_id: str
    run_id: str
    task_id: str
    agent: str
    attempt: int
    purpose: str
    input_class: DataClass
    grant_id: str
    provider: str
    model: str
    endpoint: str
    system_prompt: str
    user_prompt: str
    coaching_note: str | None
    tool_results: tuple[ToolResult, ...]
    allowed_tools: Mapping[str, int]
    response_schema: str
    timeout_s: float
    max_tokens: int
    max_cost_usd: float
    policy_version: str


@dataclass(frozen=True)
class ToolEvent:
    tool: str
    requested: bool
    executed: bool
    status: str
    citations: tuple[str, ...]
    tool_request_id: str


@dataclass(frozen=True)
class GatewayResult:
    text: str
    tool_events: tuple[ToolEvent, ...]
    provider: str
    model: str
    provider_request_id: str
    usage: Mapping[str, int]
    cost_usd: float
    latency_ms: int
    status: Literal["ok", "denied", "timeout", "provider_error", "fenced"]
    denial_reason: str
    egress_digest: str


def _model_supports_custom_temperature(model: str) -> bool:
    """Whether LiteLLM may receive a non-default temperature for this model."""
    return not model.rsplit("/", 1)[-1].lower().startswith(("o1", "o3", "o4", "gpt-5"))


def _require_chatgpt_connected() -> None:
    """Refuse unbounded LiteLLM device-code polling when OAuth is absent or stale."""
    from phi_core.chatgpt_auth import read_auth

    auth = read_auth()
    if auth is None or (auth.get("expires_at") is not None and time.time() >= auth["expires_at"] - 60):
        raise RuntimeError("ChatGPT account not connected")


def _text_content(content: Any) -> str:
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content)
    return str(content or "")


def _citation_urls(value: Any) -> tuple[str, ...]:
    """Extract URLs only from provider-native citation fields, never model prose."""
    urls: list[str] = []

    def visit(item: Any, in_citations: bool = False) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                is_citation = in_citations or key == "citations"
                if key == "url" and is_citation and isinstance(child, str):
                    urls.extend(_URL_RE.findall(child))
                else:
                    visit(child, is_citation)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child, in_citations)

    visit(value)
    return tuple(dict.fromkeys(urls))


def _usage(response: Any) -> Mapping[str, int]:
    raw = getattr(response, "usage", None) or {}
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): int(value) for key, value in raw.items() if isinstance(value, (int, float))}


def _scrub(value: Any) -> tuple[Any, int]:
    if isinstance(value, str):
        return scrub_for_prompt(value, detectors=("presidio", "rule"))
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        findings = 0
        for key, child in value.items():
            clean_key = str(key)
            if clean_key not in _STRUCTURAL_KEYS:
                clean_key, count = scrub_for_prompt(clean_key, detectors=("presidio", "rule"))
                findings += count
            clean_child, count = _scrub(child)
            result[clean_key] = clean_child
            findings += count
        return result, findings
    if isinstance(value, (list, tuple)):
        result: list[Any] = []
        findings = 0
        for child in value:
            clean_child, count = _scrub(child)
            result.append(clean_child)
            findings += count
        return result, findings
    return value, 0


def _contains_restricted_content(value: Any) -> bool:
    if isinstance(value, str):
        return scrub_for_prompt(value, detectors=("presidio", "rule"))[1] > 0
    if isinstance(value, Mapping):
        return any(
            (key not in _STRUCTURAL_KEYS and _contains_restricted_content(str(key))) or _contains_restricted_content(child)
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_restricted_content(child) for child in value)
    return False


class ProviderGateway:
    """Validates and sends the exact, sanitized payload approved by a grant."""

    def __init__(self, store: ControlStore, policy: CapabilityPolicy | None = None) -> None:
        self._store = store
        self._policy = policy or CapabilityPolicy()

    async def complete(self, req: GatewayRequest) -> GatewayResult:
        try:
            grant = await self._grant_for(req)
            self._validate_request(req, grant)
            messages, tools = self._build_messages(req)
            messages, _ = _scrub(messages)
            tools, _ = _scrub(tools)
            if _contains_restricted_content(messages) or _contains_restricted_content(tools):
                return self._denied(req, "unresolved_restricted_content")
        except (CapabilityDenied, HTTPException, RuntimeError, ValueError) as exc:
            return self._denied(req, scrub_persisted_text(str(exc)))

        payload = canonical_payload(
            request=req, decision={"status": "ok", "denial_reason": ""}, messages=messages, tools=tools
        )
        digest = egress_digest(payload)
        started = time.monotonic()
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(self._completion, req, messages, tools), timeout=req.timeout_s
            )
        except TimeoutError:
            return GatewayResult("", (), req.provider, req.model, "", {}, 0.0, self._latency(started), "timeout", "timeout", digest)
        except Exception as exc:
            return GatewayResult(
                scrub_persisted_text(str(exc)), (), req.provider, req.model, "", {}, 0.0,
                self._latency(started), "provider_error", "provider_error", digest,
            )

        actual_provider = str(getattr(response, "provider", "") or getattr(response, "provider_name", "") or req.provider)
        actual_model = str(getattr(response, "model", "") or req.model)
        if actual_provider != req.provider or actual_model != req.model:
            mismatch_payload = canonical_payload(
                request=req,
                decision={"status": "denied", "denial_reason": "provider_mismatch"},
                messages=messages,
                tools=tools,
            )
            return GatewayResult(
                "", (), actual_provider, actual_model, str(getattr(response, "id", "")), _usage(response), 0.0,
                self._latency(started), "denied", "provider_mismatch", egress_digest(mismatch_payload),
            )

        content = _text_content(response.choices[0].message.content)
        events = tuple(
            ToolEvent(
                tool=str(tool["name"]), requested=True, executed=True, status="ok",
                citations=_citation_urls(response), tool_request_id=f"{req.task_id}:{tool['name']}",
            )
            for tool in tools
        )
        return GatewayResult(
            content, events, actual_provider, actual_model, str(getattr(response, "id", "")), _usage(response), 0.0,
            self._latency(started), "ok", "", digest,
        )

    async def _grant_for(self, req: GatewayRequest) -> CapabilityGrant:
        stored = await self._store.get_one("capability_grants", {"grant_id": req.grant_id})
        if stored is None:
            raise CapabilityDenied("grant is missing")
        grant = CapabilityGrant.model_validate(stored)
        if grant.run_id != req.run_id or grant.task_id != req.task_id:
            raise CapabilityDenied("grant is not owned by this run and task")
        try:
            expires_at = datetime.fromisoformat(grant.expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CapabilityDenied("grant expiry is invalid") from exc
        if expires_at <= datetime.now(timezone.utc):
            raise CapabilityDenied("grant has expired")
        return grant

    def _validate_request(self, req: GatewayRequest, grant: CapabilityGrant) -> None:
        if req.policy_version != grant.policy_version or req.agent != grant.agent:
            raise CapabilityDenied("request identity does not match grant")
        self._policy.check_provider(grant, req.provider, req.model, req.endpoint)
        self._policy.check_data_class(grant, req.input_class)
        validate_llm_provider(req.provider)
        validate_llm_base_url(req.endpoint, req.provider)
        if req.max_tokens <= 0 or req.max_tokens > grant.budget.max_tokens:
            raise CapabilityDenied("token budget exceeds grant")
        if req.max_cost_usd < 0 or req.max_cost_usd > grant.budget.max_cost_usd:
            raise CapabilityDenied("cost budget exceeds grant")
        if req.timeout_s <= 0 or req.timeout_s > grant.budget.wall_seconds:
            raise CapabilityDenied("timeout exceeds grant")
        if set(req.allowed_tools) - set(grant.tools):
            raise CapabilityDenied("request includes an ungranted tool")
        if req.allowed_tools and req.provider != "anthropic":
            raise CapabilityDenied("tool_unsupported_by_provider")
        for tool, uses in req.allowed_tools.items():
            self._policy.check_tool(grant, tool, uses=uses)
            if tool != "web_search":
                raise CapabilityDenied(f"unsupported tool {tool!r}")

    @staticmethod
    def _build_messages(req: GatewayRequest) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": req.system_prompt}]
        if req.coaching_note:
            messages.append({"role": "system", "source": "manager_coaching", "content": req.coaching_note})
        messages.append({"role": "user", "content": req.user_prompt})
        messages.extend(
            {"role": "tool", "tool_request_id": result.tool_request_id, "content": result.content}
            for result in req.tool_results
        )
        tools = [
            {"type": "web_search_20250305", "name": tool, "max_uses": uses}
            for tool, uses in req.allowed_tools.items()
        ]
        return messages, tools

    @staticmethod
    def _completion(req: GatewayRequest, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        if req.provider == "chatgpt":
            _require_chatgpt_connected()
        model = req.model
        if req.provider == "openrouter" and not model.startswith("openrouter/"):
            model = f"openrouter/{model}"
        kwargs: dict[str, Any] = {"model": model, "max_tokens": req.max_tokens, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        if req.endpoint:
            kwargs["api_base"] = req.endpoint
        return litellm.completion(**kwargs)

    @staticmethod
    def _latency(started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    @staticmethod
    def _denied(req: GatewayRequest, reason: str) -> GatewayResult:
        return GatewayResult("", (), req.provider, req.model, "", {}, 0.0, 0, "denied", reason, "")


class ToolGateway:
    """Research tools that never downgrade a denied search to plain completion."""

    def __init__(self, provider_gateway: ProviderGateway) -> None:
        self._provider_gateway = provider_gateway
        self.last_result: GatewayResult | None = None

    async def search(self, *, req: GatewayRequest, query: str) -> ToolResult:
        if req.provider != "anthropic":
            return ToolResult("web_search", "", "", "denied", (), "tool_unsupported_by_provider")
        uses = req.allowed_tools.get("web_search", 0)
        if uses <= 0:
            return ToolResult("web_search", "", "", "denied", (), "tool_not_granted")
        result = await self._provider_gateway.complete(
            replace(req, user_prompt=query, tool_results=(), allowed_tools={"web_search": uses})
        )
        self.last_result = result
        citations = tuple(url for event in result.tool_events for url in event.citations)
        return ToolResult(
            "web_search", f"{req.task_id}:web_search", result.text, result.status, citations, result.denial_reason
        )
