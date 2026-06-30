"""Structured error envelope for RePORT AI Portal.

A single ``RePORTError`` dataclass carries enough context (stage, operation,
cause, path, hint, traceback) to diagnose failures without trawling logs.
Pipeline legs, agent tools, and the UI all wrap raised exceptions through the
``wrap`` helper so callers get a uniform, JSON-serialisable envelope.

Public API
----------
- :class:`RePORTError` — frozen dataclass with ``to_dict`` / ``to_json`` / human formatter.
- :func:`wrap` — turn any ``BaseException`` into a ``RePORTError``.
- :func:`format_for_user` — short, operator-facing one-liner.
- :func:`format_for_log` — verbose multi-line block (includes traceback).
"""

from __future__ import annotations

import json
import traceback as _tb
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
# datetime.UTC was added in Python 3.11; use timezone.utc for 3.9 compatibility
UTC = timezone.utc
from pathlib import Path
from typing import Any

__all__ = [
    "RePORTError",
    "format_for_log",
    "format_for_user",
    "wrap",
]


@dataclass(frozen=True)
class RePORTError:
    """Structured failure envelope.

    Attributes
    ----------
    stage:
        High-level phase (e.g., ``"pipeline.dataset"``, ``"agent.tool"``, ``"ui.load_study"``).
    operation:
        Specific operation that failed (e.g., ``"query_dataset"``, ``"publish_staging"``).
    cause:
        The exception class name (e.g., ``"FileNotFoundError"``).
    message:
        Short human description (the first line of ``str(exc)``).
    path:
        Optional path the error relates to. Stored as a string.
    hint:
        Optional operator-facing fix suggestion.
    traceback:
        Optional multi-line traceback for logs. Not surfaced to end users.
    timestamp:
        ISO-8601 UTC timestamp the envelope was created.
    """

    stage: str
    operation: str
    cause: str
    message: str
    path: str | None = None
    hint: str | None = None
    traceback: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""

        return asdict(self)

    def to_json(self) -> str:
        """Serialise to a compact JSON string."""

        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    def as_user_message(self) -> str:
        """Short, single-line message safe to show end users."""

        return format_for_user(self)

    def as_log_block(self) -> str:
        """Verbose multi-line block suitable for logs."""

        return format_for_log(self)


def wrap(
    exc: BaseException,
    *,
    stage: str,
    operation: str,
    path: str | Path | None = None,
    hint: str | None = None,
    include_traceback: bool = True,
) -> RePORTError:
    """Wrap a raised exception as a :class:`RePORTError`.

    This is the single entry point other modules should use so the envelope
    stays consistent. The caller supplies ``stage`` and ``operation``; the
    exception's class and first message line are pulled automatically.
    """

    msg = str(exc).strip().splitlines()
    first = msg[0] if msg else exc.__class__.__name__
    tb = None
    if include_traceback:
        tb = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__)).strip()
    return RePORTError(
        stage=stage,
        operation=operation,
        cause=exc.__class__.__name__,
        message=first,
        path=str(path) if path is not None else None,
        hint=hint,
        traceback=tb,
    )


def format_for_user(err: RePORTError) -> str:
    """Render a short operator-facing one-liner."""

    base = f"[{err.stage} · {err.operation}] {err.cause}: {err.message}"
    if err.path:
        base += f" (path: {err.path})"
    if err.hint:
        base += f" — hint: {err.hint}"
    return base


def format_for_log(err: RePORTError) -> str:
    """Render a multi-line block including traceback for logs/audit."""

    lines = [
        f"RePORTError @ {err.timestamp}",
        f"  stage     : {err.stage}",
        f"  operation : {err.operation}",
        f"  cause     : {err.cause}",
        f"  message   : {err.message}",
    ]
    if err.path:
        lines.append(f"  path      : {err.path}")
    if err.hint:
        lines.append(f"  hint      : {err.hint}")
    if err.traceback:
        lines.append("  traceback :")
        lines.extend("    " + line for line in err.traceback.splitlines())
    return "\n".join(lines)
