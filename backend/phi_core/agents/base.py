"""Agent base class + inter-agent message bus + persistence.

Every agent inherits from `Agent` and implements `run(input) -> output`.
Every call is timed, logged, and persisted to Mongo under `agent_log`.
"""
from __future__ import annotations

import asyncio
import dataclasses
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional
from uuid import uuid4

from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from .llm import LlmConfig, call_llm, call_llm_with_web_search, parse_json
from ..anonymizer import scrub_for_prompt

if TYPE_CHECKING:                       # runtime import would be circular:
    from .manager import Manager        # manager.py imports Agent from here


PLAIN_TIMEOUT_S = 90.0
PLAIN_EXTENDED_BUMP_S = 30.0
WEB_SEARCH_TIMEOUT_S = 180.0
WEB_SEARCH_EXTENDED_BUMP_S = 45.0


def _json_validator(expect_key: str | None = None, min_items: int = 0):
    """Build the reply check used by the *_json call paths.

    Returns None when the reply is acceptable, else a small dict naming the
    failure. The dict carries counts only -- it is the sole thing derived from
    a reply that the Manager is ever shown, which is what keeps the Manager
    content-blind while still letting it see that an agent under-delivered.
    """
    _unset = object()

    def _check(reply: str) -> dict[str, Any] | None:
        parsed = parse_json(reply, _unset)
        if parsed is _unset:
            return {"kind": "invalid_output"}
        if expect_key is not None:
            items = parsed.get(expect_key) if isinstance(parsed, dict) else None
            got = len(items) if isinstance(items, list) else 0
            if got < min_items:
                return {"kind": "off_task", "owed": min_items, "delivered": got}
        return None

    return _check


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentMessage(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    session_id: str
    agent: str
    phase: str            # e.g. "classify.judge", "classify.sentinel.review"
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    direction: str        # "in" | "out" | "info"
    payload: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float = 0.0
    parent_id: str | None = None   # id of the AgentMessage that caused this call
    status_text: str = ""          # short plain-language narration for tier-1 live status


class Agent:
    """Base agent. Subclasses set NAME and PROMPT and implement `run`."""
    NAME: str = "agent"
    PROMPT: str = ""

    def __init__(
        self,
        session_id: str,
        llm: LlmConfig,
        db: AsyncIOMotorDatabase,
        emit: Optional[Callable[[AgentMessage], Awaitable[None]]] = None,
        manager: Optional["Manager"] = None,
    ):
        self.session_id = session_id
        self.llm = llm
        self.db = db
        self.emit = emit
        self.call_failures = 0
        self.manager = manager
        # id of the most recent completed ("out") AgentMessage this agent
        # logged -- callers read this explicitly to set `parent_id` on a
        # call they are about to make that was provoked by this one. Never
        # an ambient/contextvar pointer: concurrent calls (asyncio.gather)
        # would misattribute parentage under an ambient scheme.
        self.last_message_id: str | None = None

    async def _log(self, phase: str, direction: str, payload: dict[str, Any], duration_ms: float = 0.0,
                   *, parent_id: str | None = None, status_text: str = "") -> None:
        msg = AgentMessage(
            session_id=self.session_id, agent=self.NAME, phase=phase,
            direction=direction, payload=payload, duration_ms=duration_ms,
            parent_id=parent_id, status_text=status_text,
        )
        await self.db.agent_log.insert_one(msg.model_dump())
        if self.emit:
            await self.emit(msg)
        if direction == "out":
            self.last_message_id = msg.id

    async def call(
        self, user_prompt: str, phase: str, *,
        timeout_s: float | None = None,
        validate: Optional[Callable[[str], dict[str, Any] | None]] = None,
        allow_web_search_escalation: bool = True,
        web_search_max_uses: int = 3,
        parent_id: str | None = None,
        status_text: str = "",
        max_tokens: int | None = None,
    ) -> str:
        """LLM call with logging, hard timeout, and -- when self.manager is set --
        Manager-supervised recovery on timeout / exception / empty / invalid /
        off-task replies.

        ``max_tokens``, when given, overrides ``self.llm.max_tokens`` for this
        call only -- a large per-column reply (e.g. Judge deciding 50+
        columns) otherwise truncates against the session's fixed default and
        silently fails JSON validation."""
        base_timeout = PLAIN_TIMEOUT_S if timeout_s is None else timeout_s
        call_cfg = self.llm if max_tokens is None else dataclasses.replace(self.llm, max_tokens=max_tokens)
        # Full untruncated text always persisted (tier-3 requirement); a
        # write-time scrub pass is defense-in-depth on top of the scrubbing
        # each call site already does to its own inputs before this point.
        # Rule-only detectors here (not presidio+rule): this layer sees every
        # agent's full prompt/reply, most of which is ordinary reasoning
        # prose, and presidio's NER false-positived heavily on it (flagged
        # "Patient", "US", "years", ethnicity words, and ICD-10 codes as PHI
        # on real corpus text with zero actual identifiers present). Rule
        # regex still catches genuine SSN/phone/email/date-shaped leaks.
        in_payload: dict[str, Any] = {"prompt_text": scrub_for_prompt(user_prompt, detectors=("rule",))[0]}
        await self._log(phase, "in", in_payload, parent_id=parent_id, status_text=status_text)

        async def attempt_plain(system_prompt: str, extended: bool) -> str:
            return await asyncio.wait_for(
                asyncio.to_thread(call_llm, system_prompt, user_prompt, call_cfg),
                timeout=base_timeout + (PLAIN_EXTENDED_BUMP_S if extended else 0.0))

        async def attempt_web_search(system_prompt: str, extended: bool) -> str:
            reply, _cites = await asyncio.wait_for(
                asyncio.to_thread(call_llm_with_web_search, system_prompt, user_prompt,
                                  call_cfg, web_search_max_uses),
                timeout=WEB_SEARCH_TIMEOUT_S + (WEB_SEARCH_EXTENDED_BUMP_S if extended else 0.0))
            return reply

        t0 = time.perf_counter()
        if self.manager is None:
            try:
                reply = await attempt_plain(self.PROMPT, False)
            except asyncio.TimeoutError:
                self.call_failures += 1
                dur = (time.perf_counter() - t0) * 1000
                await self._log(phase, "out", {"error": f"llm timeout after {base_timeout:.0f}s"}, dur, parent_id=parent_id)
                return ""
            dur = (time.perf_counter() - t0) * 1000
            await self._log(phase, "out", {"reply_text": scrub_for_prompt(reply, detectors=("rule",))[0]}, dur, parent_id=parent_id)
            return reply

        reply, ok, error_kind = await self.manager.run_supervised(
            agent_name=self.NAME, phase=phase, base_system_prompt=self.PROMPT,
            primary_attempt=attempt_plain,
            escalated_attempt=attempt_web_search if allow_web_search_escalation else None,
            validate=validate,
        )
        dur = (time.perf_counter() - t0) * 1000
        if ok:
            await self._log(phase, "out", {"reply_text": scrub_for_prompt(reply, detectors=("rule",))[0]}, dur, parent_id=parent_id)
            return reply
        self.call_failures += 1
        await self._log(phase, "out", {"error": error_kind}, dur, parent_id=parent_id)
        return ""

    async def call_json(self, user_prompt: str, phase: str, default: Any = None, *,
                        timeout_s: float | None = None,
                        expect_key: str | None = None, min_items: int = 0,
                        parent_id: str | None = None, status_text: str = "",
                        max_tokens: int | None = None) -> Any:
        reply = await self.call(user_prompt, phase, timeout_s=timeout_s,
                                validate=_json_validator(expect_key, min_items),
                                parent_id=parent_id, status_text=status_text,
                                max_tokens=max_tokens)
        return parse_json(reply, default)

    async def call_with_web_search(
        self, user_prompt: str, phase: str, max_uses: int = 3,
        *, validate: Optional[Callable[[str], dict[str, Any] | None]] = None,
        parent_id: str | None = None, status_text: str = "",
    ) -> tuple[str, list[dict[str, Any]]]:
        """LLM call with Claude's provider-hosted web_search tool.

        Anthropic executes the search server-side; the tool loop is closed
        inside the provider so we get back a final text answer with inline
        citations. Falls back gracefully to plain ``call`` for non-Claude
        providers (returns ``([], "")``-shape empty citations).

        Wrapping this at the base-agent layer keeps Statute + Praxis
        boilerplate-free: they only supply the prompt and the phase; the
        agent takes care of logging, timeout, citation capture, and
        Mongo persistence.
        """
        in_payload = {
            "prompt_text": scrub_for_prompt(user_prompt, detectors=("rule",))[0],
            "tool": "web_search_20250305",
            "max_uses": max_uses,
        }
        await self._log(phase, "in", in_payload, parent_id=parent_id, status_text=status_text)
        citations_box: list[list[dict[str, Any]]] = [[]]

        async def attempt(system_prompt: str, extended: bool) -> str:
            reply, cites = await asyncio.wait_for(
                asyncio.to_thread(call_llm_with_web_search, system_prompt, user_prompt,
                                  self.llm, max_uses),
                timeout=WEB_SEARCH_TIMEOUT_S + (WEB_SEARCH_EXTENDED_BUMP_S if extended else 0.0))
            citations_box[0] = cites
            return reply

        t0 = time.perf_counter()
        if self.manager is None:
            try:
                reply = await attempt(self.PROMPT, False)
            except asyncio.TimeoutError:
                self.call_failures += 1
                dur = (time.perf_counter() - t0) * 1000
                await self._log(phase, "out",
                                {"error": "web_search timeout after 180s"}, dur, parent_id=parent_id)
                return "", []
            ok, error_kind = True, None
        else:
            reply, ok, error_kind = await self.manager.run_supervised(
                agent_name=self.NAME, phase=phase, base_system_prompt=self.PROMPT,
                primary_attempt=attempt, escalated_attempt=None, validate=validate)
        dur = (time.perf_counter() - t0) * 1000
        if ok:
            await self._log(phase, "out", {
                "reply_text": scrub_for_prompt(reply, detectors=("rule",))[0],
                "citations_count": len(citations_box[0]),
                "citations": citations_box[0][:20],
            }, dur, parent_id=parent_id)
            return reply, citations_box[0]
        self.call_failures += 1
        await self._log(phase, "out", {"error": error_kind}, dur, parent_id=parent_id)
        return "", []

    async def call_json_with_web_search(
        self, user_prompt: str, phase: str,
        default: Any = None, max_uses: int = 3,
        *, expect_key: str | None = None, min_items: int = 0,
        parent_id: str | None = None, status_text: str = "",
    ) -> tuple[Any, list[dict[str, Any]]]:
        reply, citations = await self.call_with_web_search(
            user_prompt, phase, max_uses, validate=_json_validator(expect_key, min_items),
            parent_id=parent_id, status_text=status_text)
        return parse_json(reply, default), citations

    async def run(self, **kwargs) -> dict[str, Any]:
        raise NotImplementedError


ITERATION_CAP = 3
