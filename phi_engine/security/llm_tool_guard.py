"""PHI-safe guards for LLM-visible tool inputs and outputs."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from functools import wraps
from pathlib import Path
from typing import Any, TypeVar

try:
    from typing import ParamSpec
except ImportError:  # pragma: no cover - Python < 3.10 compatibility
    from typing_extensions import ParamSpec

from phi_engine.audit.zone_guards import deny_if_audit_zone, deny_if_snapshot_root
from phi_engine.security.phi_gate import phi_gate_check
from phi_engine.security.secure_env import assert_clean_zone

P = ParamSpec("P")
R = TypeVar("R")


class LLMToolOutputBlocked(PermissionError):
    pass


class LLMToolPathDenied(PermissionError):
    pass


@dataclass(frozen=True)
class LLMToolGuardResult:
    ok: bool
    findings: tuple[str, ...] = ()


__all__ = [
    "LLMToolOutputBlocked",
    "LLMToolPathDenied",
    "LLMToolGuardResult",
    "validate_llm_read_path",
    "guard_llm_output",
    "llm_safe_tool",
]


def validate_llm_read_path(path: str | Path) -> Path:
    """Return a resolved path only if it is safe for LLM tool reads."""

    resolved = Path(path).resolve()
    try:
        deny_if_audit_zone(resolved)
        deny_if_snapshot_root(resolved)
        assert_clean_zone(resolved)
    except PermissionError as exc:
        raise LLMToolPathDenied(str(exc)) from exc
    return resolved


def _payload_to_scan_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if is_dataclass(payload) and not isinstance(payload, type):
        payload = asdict(payload)
    elif isinstance(payload, Mapping):
        payload = dict(payload)
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        payload = list(payload)
    return json.dumps(payload, default=str, sort_keys=True)


def guard_llm_output(payload: Any) -> LLMToolGuardResult:
    """Fail closed when a tool result contains PHI before it reaches an LLM."""

    result = phi_gate_check(_payload_to_scan_text(payload))
    findings = tuple(result.findings)
    if result.blocked:
        detail = ", ".join(findings) if findings else "PHI"
        raise LLMToolOutputBlocked(f"LLM tool output blocked by PHI gate: {detail}")
    return LLMToolGuardResult(ok=True, findings=findings)


def llm_safe_tool(fn: Callable[P, R]) -> Callable[P, R]:
    """Decorate a tool so returned data is PHI-gated before LLM exposure."""

    @wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        result = fn(*args, **kwargs)
        guard_llm_output(result)
        return result

    setattr(wrapper, "__phi_llm_safe__", True)
    return wrapper
