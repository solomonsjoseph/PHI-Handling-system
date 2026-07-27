"""Agent base class + inter-agent message bus + persistence.

Every agent inherits from `Agent` and implements `run(input) -> output`.
Every call is timed, logged, and persisted to Mongo under `agent_log`.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional
from uuid import uuid4

from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from .llm import LlmConfig, call_llm, parse_json


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentMessage(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    session_id: str
    agent: str
    phase: str            # e.g. "classify.judge", "classify.sentinel.review"
    ts: str = Field(default_factory=_now)
    direction: str        # "in" | "out" | "info"
    payload: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float = 0.0


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
    ):
        self.session_id = session_id
        self.llm = llm
        self.db = db
        self.emit = emit

    async def _log(self, phase: str, direction: str, payload: dict[str, Any], duration_ms: float = 0.0) -> None:
        msg = AgentMessage(
            session_id=self.session_id, agent=self.NAME, phase=phase,
            direction=direction, payload=payload, duration_ms=duration_ms,
        )
        await self.db.agent_log.insert_one(msg.model_dump())
        if self.emit:
            await self.emit(msg)

    async def call(self, user_prompt: str, phase: str) -> str:
        """LLM call with logging and hard timeout."""
        await self._log(phase, "in", {"prompt_preview": user_prompt[:400]})
        t0 = time.perf_counter()
        try:
            reply = await asyncio.wait_for(
                asyncio.to_thread(call_llm, self.PROMPT, user_prompt, self.llm),
                timeout=90.0,
            )
        except asyncio.TimeoutError:
            dur = (time.perf_counter() - t0) * 1000
            await self._log(phase, "out", {"error": "llm timeout after 90s"}, dur)
            return ""
        dur = (time.perf_counter() - t0) * 1000
        await self._log(phase, "out", {"reply_preview": reply[:400]}, dur)
        return reply

    async def call_json(self, user_prompt: str, phase: str, default: Any = None) -> Any:
        reply = await self.call(user_prompt, phase)
        return parse_json(reply, default)

    async def run(self, **kwargs) -> dict[str, Any]:
        raise NotImplementedError


ITERATION_CAP = 3
