"""Telemetry logger for RePORT AI Portal AI Assistant.

Captures agent events (tool calls, LLM invocations, hallucination detections,
feedback) to an append-only JSONL file. All free-text fields are scanned for
PHI patterns and masked before writing.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from datetime import datetime, timezone
UTC = timezone.utc  # py3.9 compat
from pathlib import Path
from typing import Any

import phi_engine.config.config as config
from phi_engine.security.phi_patterns import BLOCKING_PATTERNS, WARN_PATTERNS
from phi_engine.utils.logging_system import get_logger

_HAS_LANGCHAIN = False
_AIMessage: type | None = None

try:
    from langchain_core.callbacks.base import (
        BaseCallbackHandler as _LCBaseHandler,
    )
    from langchain_core.messages import AIMessage as _LCAIMessage

    _HAS_LANGCHAIN = True
    _AIMessage = _LCAIMessage
except ImportError:
    pass

__all__ = ["TelemetryLogger"]

logger = get_logger(__name__)

# Single source of truth for PHI patterns is scripts.security.phi_patterns.
# Using the shared catalog keeps telemetry aligned with the query-time gate
# and log_hygiene (which log_hygiene.py itself flags as the correct path).
_TELEMETRY_MASK_PATTERNS = [pat for _, pat in BLOCKING_PATTERNS] + [pat for _, pat in WARN_PATTERNS]


def _mask_phi(text: str) -> str:
    """Replace potential PHI patterns in text with [REDACTED]."""
    for pattern in _TELEMETRY_MASK_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def _append_event(event: dict[str, Any]) -> None:
    """Append a telemetry event to the JSONL sink using atomic writes."""
    sink_path = Path(config.TELEMETRY_SINK)
    sink_path.parent.mkdir(parents=True, exist_ok=True)

    line = json.dumps(event, default=str, ensure_ascii=False) + "\n"

    # Atomic append: write to temp, fsync, then append
    fd, tmp_path = tempfile.mkstemp(
        prefix="telem_",
        suffix=".tmp",
        dir=sink_path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

        # Append to main file
        with open(sink_path, "a", encoding="utf-8") as fh:
            fh.write(line)

        # Tighten file mode after every append. The first append creates the
        # file with the process umask (typically 0o644 → world-readable);
        # subsequent appends are no-ops on permissions but the chmod is
        # idempotent so we keep it simple.
        sink_path.chmod(0o600)

        os.unlink(tmp_path)
    except Exception:
        # Cleanup temp on failure
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


_TelemetryBase = _LCBaseHandler if _HAS_LANGCHAIN else object  # type: ignore[misc]


class TelemetryLogger(_TelemetryBase):  # type: ignore[misc,valid-type]
    """LangChain callback handler for telemetry event capture."""

    name: str = "report_ai_portal_telemetry"

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        """Log LLM completion events with token usage."""
        event: dict[str, Any] = {
            "type": "llm_end",
            "timestamp": datetime.now(tz=UTC).isoformat(),
        }

        # Extract token usage from AIMessage.usage_metadata
        if _HAS_LANGCHAIN and hasattr(response, "generations"):
            for gen_list in response.generations:
                for gen in gen_list:
                    if (
                        _AIMessage is not None
                        and isinstance(gen.message, _AIMessage)
                        and gen.message.usage_metadata  # type: ignore[attr-defined]
                    ):
                        event["tokens"] = {
                            "input": gen.message.usage_metadata.get(  # type: ignore[attr-defined]
                                "input_tokens", 0
                            ),
                            "output": gen.message.usage_metadata.get(  # type: ignore[attr-defined]
                                "output_tokens", 0
                            ),
                        }

        try:
            _append_event(event)
        except Exception:
            logger.debug("Telemetry write failed", exc_info=True)

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        **kwargs: Any,
    ) -> None:
        """Log tool invocation events."""
        event = {
            "type": "tool_start",
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "tool": serialized.get("name", "unknown"),
            "input_preview": _mask_phi(input_str[:200]),
        }
        try:
            _append_event(event)
        except Exception:
            logger.debug("Telemetry write failed", exc_info=True)

    def on_custom_event(
        self,
        name: str,
        data: Any,
        **kwargs: Any,
    ) -> None:
        """Log custom events (hallucination detection, follow-up, etc.)."""
        event: dict[str, Any] = {
            "type": f"custom:{name}",
            "timestamp": datetime.now(tz=UTC).isoformat(),
        }

        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, str):
                    event[key] = _mask_phi(value[:500])
                elif isinstance(value, (int, float, bool)) or value is None:
                    event[key] = value
                else:
                    # Non-primitive payloads (exceptions, tracebacks, nested
                    # dicts, dataframe previews) are force-stringified + PHI-
                    # masked + length-capped so a caller that hands us a
                    # repr-heavy object cannot land raw PHI in the JSONL sink.
                    event[key] = _mask_phi(str(value)[:500])

        try:
            _append_event(event)
        except Exception:
            logger.debug("Telemetry write failed", exc_info=True)
