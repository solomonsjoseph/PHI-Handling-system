"""Agent base class, task-scoped gateway calls, and legacy progress messages."""
from __future__ import annotations

import asyncio
import functools
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from phi_core.control.context import AgentContext
from phi_core.control.gateway import GatewayRequest, GatewayResult

from .llm import parse_json

if TYPE_CHECKING:
    from .manager import Manager


PLAIN_TIMEOUT_S = 90.0
PLAIN_EXTENDED_BUMP_S = 30.0
WEB_SEARCH_TIMEOUT_S = 180.0
WEB_SEARCH_EXTENDED_BUMP_S = 45.0


def _json_validator(expect_key: str | None = None, min_items: int = 0):
    """Build the reply check used by the JSON call paths.

    Returns a validator following ``Manager.run_supervised``'s contract:
    ``None`` means the reply is acceptable, a dict means it fell short and
    carries ``kind`` plus the counts a caller needs to log (never content).
    """

    def _check(reply: str) -> dict[str, Any] | None:
        parsed = parse_json(reply, None)
        if not isinstance(parsed, (dict, list)):
            return {"kind": "off_task", "owed": min_items, "delivered": 0}
        if expect_key is not None:
            if not isinstance(parsed, dict) or expect_key not in parsed:
                return {"kind": "off_task", "owed": min_items, "delivered": 0}
            value = parsed[expect_key]
            if not isinstance(value, list):
                return {"kind": "off_task", "owed": min_items, "delivered": 0}
            if len(value) < min_items:
                return {"kind": "off_task", "owed": min_items, "delivered": len(value)}
        elif min_items and (not isinstance(parsed, list) or len(parsed) < min_items):
            delivered = len(parsed) if isinstance(parsed, list) else 0
            return {"kind": "off_task", "owed": min_items, "delivered": delivered}
        return None

    return _check


class AgentMessage(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    session_id: str
    agent: str
    phase: str
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    direction: str
    payload: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float = 0.0
    parent_id: str | None = None
    status_text: str = ""


class Agent:
    """Base agent. Subclasses set ``NAME`` and ``PROMPT`` and implement ``run``."""

    NAME: str = "agent"
    PROMPT: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Wrap a leaf subclass's own ``run`` so its ``WorkItem`` is
        completed (or failed) the moment it returns, against whatever
        ``ctx.tasks`` the activation that built it supplied. Every
        production entry path (`ActivationFactory`) wires a real one;
        ``ctx.tasks is None`` -- most unit tests, built via
        ``control.testing.make_ctx`` -- is a silent no-op, unchanged from
        before this hook existed.

        Exists because `Agent.run`'s signature varies per subclass
        (``Ledger``/``Herald`` are not `Agent` subclasses and never had a
        single template method to hook); wrapping here, once, is the only
        way every subclass's completion stays correct without touching
        each of their `run` bodies individually. An exception other than
        cancellation is recorded as a failed task and always re-raised
        unchanged -- this hook only ever adds bookkeeping around the
        original call, never changes its outcome."""
        super().__init_subclass__(**kwargs)
        original_run = cls.__dict__.get("run")
        if original_run is None:
            return

        @functools.wraps(original_run)
        async def _completing_run(self: "Agent", *args: Any, **run_kwargs: Any) -> Any:
            try:
                result = await original_run(self, *args, **run_kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.ctx.tasks is not None:
                    await self.ctx.tasks.fail(f"agent_crashed:{type(exc).__name__}")
                raise
            if self.ctx.tasks is not None:
                await self.ctx.tasks.complete(result if isinstance(result, dict) else {})
            return result

        cls.run = _completing_run

    def __init__(self, ctx: AgentContext):
        if ctx.agent != self.NAME:
            raise ValueError(f"agent context is for {ctx.agent!r}, not {self.NAME!r}")
        self.ctx = ctx
        self.session_id = ctx.session_id
        self.call_failures = 0
        self.last_message_id: str | None = None
        self._last_gateway_result: GatewayResult | None = None

    @property
    def manager(self) -> "Manager | None":
        return self.ctx.manager

    @staticmethod
    def _error_from_result(result: GatewayResult) -> RuntimeError:
        return RuntimeError(result.denial_reason or result.status)

    def _request(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        timeout_s: float,
        max_tokens: int,
        allowed_tools: dict[str, int] | None = None,
    ) -> GatewayRequest:
        grant = self.ctx.grant
        return GatewayRequest(
            session_id=self.ctx.session_id,
            run_id=self.ctx.run_id,
            task_id=self.ctx.task_id,
            agent=self.NAME,
            attempt=self.ctx.attempt,
            purpose=grant.agent,
            input_class=grant.data_class_ceiling,
            grant_id=grant.grant_id,
            provider=grant.provider,
            model=grant.model,
            endpoint=grant.endpoint,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            coaching_note=None,
            tool_results=(),
            allowed_tools=allowed_tools or {},
            response_schema="",
            timeout_s=timeout_s,
            max_tokens=max_tokens,
            max_cost_usd=grant.budget.max_cost_usd,
            policy_version=grant.policy_version,
        )

    async def _log(
        self,
        phase: str,
        direction: str,
        payload: dict[str, Any],
        duration_ms: float = 0.0,
        *,
        parent_id: str | None = None,
        status_text: str = "",
    ) -> None:
        msg = AgentMessage(
            session_id=self.session_id,
            agent=self.NAME,
            phase=phase,
            direction=direction,
            payload=payload,
            duration_ms=duration_ms,
            parent_id=parent_id,
            status_text=status_text,
        )
        result = self._last_gateway_result
        tools = result.tool_events if result else ()
        await self.ctx.trace.emit(
            event_id=msg.id,
            task_id=self.ctx.task_id,
            parent_task_id="",
            parent_msg_id=parent_id or "",
            phase=phase,
            direction=direction,
            payload=payload,
            depth=0,
            attempt=self.ctx.attempt,
            agent=self.NAME,
            manifest_version=self.ctx.grant.manifest_version,
            policy_version=self.ctx.grant.policy_version,
            provider=result.provider if result else self.ctx.grant.provider,
            model=result.model if result else self.ctx.grant.model,
            endpoint=self.ctx.grant.endpoint,
            provider_request_id=result.provider_request_id if result else "",
            usage=dict(result.usage) if result else {},
            cost_usd=result.cost_usd if result else 0.0,
            latency_ms=result.latency_ms if result else int(duration_ms),
            tool_requested=",".join(event.tool for event in tools if event.requested),
            tool_policy_decision=(result.status if result else "not_requested"),
            tool_executed=",".join(event.tool for event in tools if event.executed),
            tool_result_status=",".join(event.status for event in tools),
            input_class=self.ctx.grant.data_class_ceiling,
            output_class="internal",
            outcome="ok" if direction == "out" and not payload.get("error") else (payload.get("error") or ""),
            retry_category="" if not payload.get("error") else str(payload["error"]),
            status_text=status_text,
            egress_digest=result.egress_digest if result else "",
            gateway_decision=result.status if result else "",
        )
        if self.ctx.emit:
            await self.ctx.emit(msg)
        if direction == "out":
            self.last_message_id = msg.id

    async def call(
        self,
        user_prompt: str,
        phase: str,
        *,
        timeout_s: float | None = None,
        validate: Optional[Callable[[str], dict[str, Any] | None]] = None,
        allow_web_search_escalation: bool = True,
        web_search_max_uses: int = 3,
        parent_id: str | None = None,
        status_text: str = "",
        max_tokens: int | None = None,
    ) -> str:
        base_timeout = PLAIN_TIMEOUT_S if timeout_s is None else timeout_s
        token_limit = self.ctx.grant.budget.max_tokens if max_tokens is None else max_tokens
        async def attempt_plain(system_prompt: str, extended: bool) -> str:
            request = self._request(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                timeout_s=base_timeout + (PLAIN_EXTENDED_BUMP_S if extended else 0.0),
                max_tokens=token_limit,
            )
            result = await self.ctx.gateway.complete(request)
            self._last_gateway_result = result
            if result.status == "timeout":
                raise asyncio.TimeoutError
            if result.status != "ok":
                raise self._error_from_result(result)
            return result.text

        async def attempt_web_search(system_prompt: str, extended: bool) -> str:
            allowed = min(web_search_max_uses, int(self.ctx.grant.tools.get("web_search", 0)))
            if allowed <= 0:
                raise RuntimeError("web_search is not granted")
            request = self._request(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                timeout_s=WEB_SEARCH_TIMEOUT_S + (WEB_SEARCH_EXTENDED_BUMP_S if extended else 0.0),
                max_tokens=token_limit,
                allowed_tools={"web_search": allowed},
            )
            tool_result = await self.ctx.tools.search(req=request, query=user_prompt)
            self._last_gateway_result = getattr(self.ctx.tools, "last_result", None)
            if tool_result.status != "ok":
                raise RuntimeError(tool_result.denial_reason or tool_result.status)
            return tool_result.content

        t0 = time.perf_counter()
        if self.manager is None:
            try:
                reply = await attempt_plain(self.PROMPT, False)
            except asyncio.TimeoutError:
                self.call_failures += 1
                await self._log(
                    phase,
                    "out",
                    {"error": f"llm timeout after {base_timeout:.0f}s"},
                    (time.perf_counter() - t0) * 1000,
                    parent_id=parent_id,
                )
                return ""
            await self._log(phase, "out", {}, (time.perf_counter() - t0) * 1000, parent_id=parent_id)
            return reply

        reply, ok, error_kind = await self.manager.run_supervised(
            agent_name=self.NAME,
            phase=phase,
            base_system_prompt=self.PROMPT,
            primary_attempt=attempt_plain,
            escalated_attempt=attempt_web_search if allow_web_search_escalation and self.ctx.grant.tools.get("web_search") else None,
            validate=validate,
        )
        duration_ms = (time.perf_counter() - t0) * 1000
        if ok:
            await self._log(phase, "out", {}, duration_ms, parent_id=parent_id)
            return reply
        self.call_failures += 1
        await self._log(phase, "out", {"error": error_kind}, duration_ms, parent_id=parent_id)
        return ""

    async def call_json(self, user_prompt: str, phase: str, default: Any = None, **kwargs: Any) -> Any:
        reply = await self.call(user_prompt, phase, validate=_json_validator(kwargs.pop("expect_key", None), kwargs.pop("min_items", 0)), **kwargs)
        return parse_json(reply, default)

    async def call_with_web_search(
        self,
        user_prompt: str,
        phase: str,
        max_uses: int = 3,
        *,
        validate: Optional[Callable[[str], dict[str, Any] | None]] = None,
        parent_id: str | None = None,
        status_text: str = "",
    ) -> tuple[str, list[dict[str, Any]]]:
        allowed = min(max_uses, int(self.ctx.grant.tools.get("web_search", 0)))
        await self._log(phase, "in", {"tool": "web_search", "max_uses": allowed}, parent_id=parent_id, status_text=status_text)

        async def attempt(system_prompt: str, extended: bool) -> str:
            if allowed <= 0:
                raise RuntimeError("web_search is not granted")
            request = self._request(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                timeout_s=WEB_SEARCH_TIMEOUT_S + (WEB_SEARCH_EXTENDED_BUMP_S if extended else 0.0),
                max_tokens=self.ctx.grant.budget.max_tokens,
                allowed_tools={"web_search": allowed},
            )
            tool_result = await self.ctx.tools.search(req=request, query=user_prompt)
            self._last_gateway_result = getattr(self.ctx.tools, "last_result", None)
            if tool_result.status != "ok":
                raise RuntimeError(tool_result.denial_reason or tool_result.status)
            if self._last_gateway_result is None:
                self._last_gateway_result = GatewayResult(
                    tool_result.content, (), request.provider, request.model, "", {}, 0.0, 0, "ok", "", ""
                )
            return tool_result.content

        t0 = time.perf_counter()
        try:
            if self.manager is None:
                reply = await attempt(self.PROMPT, False)
                ok, error_kind = True, ""
            else:
                reply, ok, error_kind = await self.manager.run_supervised(
                    agent_name=self.NAME, phase=phase, base_system_prompt=self.PROMPT,
                    primary_attempt=attempt, escalated_attempt=None, validate=validate,
                )
        except asyncio.TimeoutError:
            reply, ok, error_kind = "", False, "web_search timeout after 180s"
        duration_ms = (time.perf_counter() - t0) * 1000
        if ok:
            citations = [
                {"url": citation}
                for event in (self._last_gateway_result.tool_events if self._last_gateway_result else ())
                for citation in event.citations
            ]
            await self._log(phase, "out", {"citations_count": len(citations)}, duration_ms, parent_id=parent_id)
            return reply, citations
        self.call_failures += 1
        await self._log(phase, "out", {"error": error_kind}, duration_ms, parent_id=parent_id)
        return "", []

    async def call_json_with_web_search(
        self, user_prompt: str, phase: str, default: Any = None, max_uses: int = 3, **kwargs: Any
    ) -> tuple[Any, list[dict[str, Any]]]:
        reply, citations = await self.call_with_web_search(user_prompt, phase, max_uses, **kwargs)
        return parse_json(reply, default), citations

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError


ITERATION_CAP = 3
