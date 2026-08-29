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

from . import authorization, canary, limits
from .egress import _STRUCTURAL_KEYS, canonical_payload, egress_digest
from .policy import BudgetExceeded, CapabilityDenied, CapabilityPolicy
from .records import CapabilityGrant, DataClass, TraceEvent
from .secrets_scan import contains_secret
from .runs import check_run_budget, record_grant_tool_usage, record_run_usage
from .store import ControlStore
from .workflow import WorkflowError

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
    prompt_version: str = ""


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


def _tool_was_used(value: Any) -> bool:
    """Whether the provider response actually invoked a tool, not merely
    whether one was offered in the request. Anthropic's server-side tool
    use surfaces as a content block whose ``type`` is ``server_tool_use``
    or a ``*_tool_result`` block; a model can answer without touching an
    offered tool, so ``tools`` being non-empty on the request proves
    nothing about execution."""

    def visit(item: Any) -> bool:
        if isinstance(item, Mapping):
            block_type = item.get("type")
            if isinstance(block_type, str) and ("tool_use" in block_type or "tool_result" in block_type):
                return True
            return any(visit(child) for child in item.values())
        if isinstance(item, (list, tuple)):
            return any(visit(child) for child in item)
        return False

    return visit(value)


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
            await self._validate_request(req, grant)
            messages, tools = self._build_messages(req)
            messages, _ = _scrub(messages)
            tools, _ = _scrub(tools)
            if _contains_restricted_content(messages) or _contains_restricted_content(tools):
                return self._denied(req, "unresolved_restricted_content")
            if contains_secret(messages) or contains_secret(tools):
                return self._denied(req, "secret_detected")
        except BudgetExceeded as exc:
            reason = scrub_persisted_text(str(exc))
            await self._record_budget_denial(req, reason)
            return self._denied(req, reason)
        except (CapabilityDenied, HTTPException, RuntimeError, ValueError) as exc:
            return self._denied(req, scrub_persisted_text(str(exc)))

        payload = canonical_payload(
            request=req, decision={"status": "ok", "denial_reason": ""}, messages=messages, tools=tools
        )
        digest = egress_digest(payload)

        # Wave R-d (spec sections 71/72): the outbound payload is never
        # persisted -- canonical_payload() builds it, egress_digest() hashes
        # it, and the raw bytes are dropped right after this call returns.
        # A leak canary planted in an acceptance run's ground truth can only
        # be caught here, in-process, immediately before the payload would
        # leave. `active_canary_set` is None for every ordinary production
        # run (no acceptance harness registered one), so this is a no-op
        # dict lookup on the hot path in the overwhelming common case.
        canary_set = canary.active_canary_set(req.run_id)
        if canary_set is not None:
            scan = canary_set.scan_payload(payload)
            await self._record_canary_scan(req, digest, scan)
            if scan.hit:
                # No provider consumption happened -- give the reservation
                # back, same as the timeout path below, before propagating
                # the violation. Deliberately not caught by the except
                # clause above (SecurityBoundaryViolation is not a
                # RuntimeError/ValueError/CapabilityDenied): a security
                # boundary violation must reach the caller as a raised
                # exception, never be silently folded into an ordinary
                # "denied" GatewayResult.
                await self._reconcile_run_budget(req)
                raise canary.SecurityBoundaryViolation(scan.canary_id, scan.hit_count)

        started = time.monotonic()
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(self._completion, req, messages, tools, self._api_key()), timeout=req.timeout_s
            )
        except TimeoutError:
            # No provider consumption happened -- give the whole reservation back.
            await self._reconcile_run_budget(req)
            return GatewayResult("", (), req.provider, req.model, "", {}, 0.0, self._latency(started), "timeout", "timeout", digest)
        except Exception as exc:
            await self._reconcile_run_budget(req)
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
            mismatch_usage = _usage(response)
            await self._reconcile_run_budget(
                req, tokens=int(mismatch_usage.get("total_tokens", 0)),
                cost_usd=int(mismatch_usage.get("total_tokens", 0)) / 1000 * limits.ASSUMED_USD_PER_1K_TOKENS,
            )
            return GatewayResult(
                "", (), actual_provider, actual_model, str(getattr(response, "id", "")), mismatch_usage, 0.0,
                self._latency(started), "denied", "provider_mismatch", egress_digest(mismatch_payload),
            )

        usage = _usage(response)
        total_tokens = int(usage.get("total_tokens", 0))
        cost_usd = total_tokens / 1000 * limits.ASSUMED_USD_PER_1K_TOKENS
        tool_calls = len(tools)
        # `check_run_budget` (in `_validate_request`) already reserved the
        # worst-case (max_tokens/max_cost_usd/aggregate tool uses) atomically
        # before the call went out. Real provider consumption happened
        # regardless of what happens next (an oversized output is still a
        # spent call) -- reconcile the reservation down to the actual amount
        # before either return below, best-effort: a usage-record race must
        # never fail an otherwise-successful/oversized call.
        await self._reconcile_run_budget(req, tokens=total_tokens, cost_usd=cost_usd, tool_calls=tool_calls)
        # The grant's own per-tool ceiling (checked pre-call in
        # ``_validate_request`` against ``grant.tools_used``) must be
        # persisted here too, or every repeated call against the same
        # grant is checked against an always-zero starting point.
        try:
            await record_grant_tool_usage(self._store, req.grant_id, req.allowed_tools)
        except WorkflowError:
            pass

        content = _text_content(response.choices[0].message.content)
        if len(content.encode("utf-8")) > limits.MAX_OUTPUT_BYTES:
            await self._record_budget_denial(req, "MAX_OUTPUT_BYTES exceeded")
            return GatewayResult(
                "", (), actual_provider, actual_model, str(getattr(response, "id", "")), usage, cost_usd,
                self._latency(started), "denied", "MAX_OUTPUT_BYTES exceeded", digest,
            )
        response_dump = response.model_dump() if hasattr(response, "model_dump") else response
        events = tuple(
            ToolEvent(
                tool=str(tool["name"]),
                requested=True,
                executed=_tool_was_used(response_dump),
                status="ok" if _tool_was_used(response_dump) else "not_used",
                citations=_citation_urls(response_dump),
                tool_request_id=f"{req.task_id}:{tool['name']}",
            )
            for tool in tools
        )
        return GatewayResult(
            content, events, actual_provider, actual_model, str(getattr(response, "id", "")), usage, cost_usd,
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

    async def _validate_request(self, req: GatewayRequest, grant: CapabilityGrant) -> None:
        if req.policy_version != grant.policy_version or req.agent != grant.agent:
            raise CapabilityDenied("request identity does not match grant")
        # Wave R-c Step 7: `authorization.authorize_capability` names the
        # spec's AuthorizationService boundary; it composes exactly the
        # same `policy.check_provider`/`policy.check_data_class` pair
        # this call site invoked directly before -- a pure rename with
        # no new security check.
        authorization.authorize_capability(
            self._policy, grant,
            provider=req.provider, model=req.model, endpoint=req.endpoint, data_class=req.input_class,
        )
        validate_llm_provider(req.provider)
        validate_llm_base_url(req.endpoint, req.provider)
        if req.max_tokens <= 0 or req.max_tokens > grant.budget.max_tokens:
            raise BudgetExceeded("MAX_TOKENS_PER_TASK: token budget exceeds grant")
        if req.max_cost_usd < 0 or req.max_cost_usd > grant.budget.max_cost_usd:
            raise BudgetExceeded("MAX_COST_PER_TASK_USD: cost budget exceeds grant")
        if req.timeout_s <= 0 or req.timeout_s > grant.budget.wall_seconds:
            raise BudgetExceeded("MAX_TASK_WALL_S: timeout exceeds grant")
        if set(req.allowed_tools) - set(grant.tools):
            raise CapabilityDenied("request includes an ungranted tool")
        if req.allowed_tools and req.provider != "anthropic":
            raise CapabilityDenied("tool_unsupported_by_provider")
        if sum(req.allowed_tools.values()) > grant.budget.max_tool_calls:
            raise BudgetExceeded("MAX_TOOL_CALLS_PER_TASK: aggregate tool-call budget exceeds grant")
        for tool, uses in req.allowed_tools.items():
            self._policy.check_tool(grant, tool, uses=uses)
            if tool != "web_search":
                raise CapabilityDenied(f"unsupported tool {tool!r}")
        input_bytes = (
            len(req.system_prompt.encode("utf-8")) + len(req.user_prompt.encode("utf-8"))
            + sum(len(result.content.encode("utf-8")) for result in req.tool_results)
        )
        if input_bytes > limits.MAX_INPUT_BYTES:
            raise BudgetExceeded("MAX_INPUT_BYTES exceeded")
        await check_run_budget(
            self._store, req.run_id, tokens=req.max_tokens, cost_usd=req.max_cost_usd,
            tool_calls=sum(req.allowed_tools.values()),
        )

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

    def _api_key(self) -> str:
        """The operator-configured BYO key for the active provider, decrypted
        by ``_current_llm_cfg`` and stashed on ``CapabilityPolicy`` at
        activation time. Never sourced from a request or grant field: those
        are persisted, and a raw key must never land in Mongo."""
        return str(getattr(getattr(self._policy, "_llm_config", None), "api_key", "") or "")

    @staticmethod
    def _completion(
        req: GatewayRequest, messages: list[dict[str, Any]], tools: list[dict[str, Any]], api_key: str = ""
    ) -> Any:
        if req.provider == "chatgpt":
            _require_chatgpt_connected()
        model = req.model
        if req.provider == "openrouter" and not model.startswith("openrouter/"):
            model = f"openrouter/{model}"
        kwargs: dict[str, Any] = {
            "model": model, "max_tokens": req.max_tokens, "messages": messages, "timeout": req.timeout_s,
        }
        if tools:
            kwargs["tools"] = tools
        if req.endpoint:
            kwargs["api_base"] = req.endpoint
        if api_key:
            kwargs["api_key"] = api_key
        return litellm.completion(**kwargs)

    @staticmethod
    def _latency(started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    async def _reconcile_run_budget(
        self, req: GatewayRequest, *, tokens: int = 0, cost_usd: float = 0.0, tool_calls: int = 0,
    ) -> None:
        """Give back the difference between what ``check_run_budget`` (via
        ``_validate_request``) reserved up front -- ``req.max_tokens``,
        ``req.max_cost_usd``, and the aggregate requested tool uses -- and
        what this call actually consumed. Called on every exit path after a
        successful reservation (timeout, provider error, provider mismatch,
        and the normal completion), so a call that reserved the worst case
        but consumed less -- or nothing, on error -- never leaves the run
        permanently charged for headroom it never used. Best-effort: a lost
        usage-record race must never turn a real result into a raised error."""
        reserved_tool_calls = sum(req.allowed_tools.values())
        try:
            await record_run_usage(
                self._store, req.run_id,
                tokens=tokens - req.max_tokens,
                cost_usd=cost_usd - req.max_cost_usd,
                tool_calls=tool_calls - reserved_tool_calls,
            )
        except WorkflowError:
            pass

    async def _record_budget_denial(self, req: GatewayRequest, reason: str) -> None:
        """Insert a ``TraceEvent`` with ``outcome="budget_exceeded"`` for a
        gateway-time D5 refusal, via the same ``TraceEventStore`` every
        other trace write goes through (D15: it is the sole writer of
        ``trace_events``, so this no longer duplicates its ``event_seq``
        CAS allocation inline -- ``events.py`` has no dependency on this
        module, so importing it here cannot cycle back through
        ``context.py``). Best-effort against a lost CAS race: a missed
        trace row must never turn a real denial into a raised exception."""
        from .events import EventAppendError, TraceEventStore

        event = TraceEvent(
            run_id=req.run_id, seq=0, session_id=req.session_id, task_id=req.task_id, agent=req.agent,
            input_class=req.input_class, output_class=req.input_class, outcome="budget_exceeded", status_text=reason,
        )
        try:
            await TraceEventStore(self._store, run_id=req.run_id, session_id=req.session_id).append(event)
        except EventAppendError:
            return

    async def _record_canary_scan(
        self, req: GatewayRequest, digest: str, scan: "canary.CanaryScanResult"
    ) -> None:
        """Insert the one ``TraceEvent`` that carries this outbound
        payload's ``egress_digest`` together with its canary-scan verdict
        (Wave R-d, spec section 72). On a hit the payload is deliberately
        never included -- ``payload`` carries only the opaque
        ``canary_id`` and ``hit_count`` (section 71: "Do not copy the
        leaked sensitive value into incident telemetry"). Best-effort like
        ``_record_budget_denial``: a lost trace write must never turn a
        real scan result into a raised exception of its own -- the caller
        still raises ``SecurityBoundaryViolation`` on a hit regardless."""
        from .events import EventAppendError, TraceEventStore

        if scan.hit:
            payload: dict[str, Any] = {
                "canary_scan": "violation", "canary_id": scan.canary_id, "hit_count": scan.hit_count,
            }
            outcome = "security_boundary_violation"
        else:
            payload = {"canary_scan": "clean"}
            outcome = "ok"
        event = TraceEvent(
            run_id=req.run_id, seq=0, session_id=req.session_id, task_id=req.task_id, agent=req.agent,
            input_class=req.input_class, output_class=req.input_class, outcome=outcome,
            failure_class=canary.SecurityBoundaryViolation.failure_class if scan.hit else "",
            egress_digest=digest, payload=payload,
        )
        try:
            await TraceEventStore(self._store, run_id=req.run_id, session_id=req.session_id).append(event)
        except EventAppendError:
            return

    @staticmethod
    def _denied(req: GatewayRequest, reason: str) -> GatewayResult:
        return GatewayResult("", (), req.provider, req.model, "", {}, 0.0, 0, "denied", reason, "")


class ToolGateway:
    """Research tools that never downgrade a denied search to plain completion."""

    def __init__(self, provider_gateway: ProviderGateway) -> None:
        self._provider_gateway = provider_gateway
        self.last_result: GatewayResult | None = None

    async def search(self, *, req: GatewayRequest, query: str) -> ToolResult:
        # Wave R-d / spec section 36: `query` becomes the delegated
        # request's `user_prompt` below, which flows through
        # `ProviderGateway.complete`'s own payload assembly and canary
        # scan (same run_id, via `replace`) -- a canary-bearing query is
        # blocked and raises `canary.SecurityBoundaryViolation` from
        # `complete()`, uncaught here, before this method logs or returns
        # anything. No separate scan call is needed on this path.
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
