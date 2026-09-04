"""Pre-execution deterministic validators (docs #52): the fixed set of
checks an :class:`~.records.ExecutionTask` must clear before the
raw-data worker (``control.sandbox.run_isolated``) is allowed to touch a
single row. Every validator here is a plain deterministic function --
none of them ever makes an LLM call, and none of them ever runs the
candidate operation to see what it does; they inspect declared state
(dataset paths, decision actions, method references, sandbox
configuration) and reject outright on a fixed rule.

Each validator below owns a subset of docs #52's twelve rejection
categories (network calls, shell use, unexpected subprocess, dynamic
dependency installation, host filesystem access, path escape, credential
access, unapproved import, unknown operation, unapproved method,
unbounded runtime, unbounded memory); several categories are covered by
more than one validator on purpose (static-analysis and runtime-config
layers checking the same threat is defense in depth, not redundancy to
prune).

``run_pre_execution_validators`` runs the full set and raises
:class:`ExecutionValidationRejected` naming every violation found (not
just the first), matching ``control/gates.py``'s "report the complete
picture in one shot" convention.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from ..paths import DATA_DIR
from . import limits
from .records import CapabilityGrant, MethodRecord, SandboxRecord

# StaticCodeValidator/CapabilityBroker: absolute top-level module names
# the raw-data worker's dispatch targets are allowed to import (stdlib
# plus the two vetted third-party packages ``reasoning.py`` already
# depends on). A *relative* import (``from .base import Agent``,
# ``from ..anonymizer import apply_to_text``) is always approved
# regardless of this set -- it names another module inside this same
# reviewed package, not a new external dependency; the allowlist's job
# is catching an unexpected new *external* package showing up in a
# worker module later, not re-enumerating every internal sibling.
_APPROVED_WORKER_IMPORTS = frozenset({
    "__future__", "asyncio", "csv", "hashlib", "hmac", "json", "os", "re",
    "pathlib", "tempfile", "typing", "uuid", "openpyxl", "pydantic",
})

# StaticCodeValidator: import names that are always forbidden regardless
# of the allowlist above -- network, shell, subprocess, and dynamic
# dependency installation each have an unambiguous module name to key on.
_FORBIDDEN_IMPORT_CATEGORIES: dict[str, str] = {
    "socket": "network_call", "http": "network_call", "http.client": "network_call",
    "urllib": "network_call", "requests": "network_call", "aiohttp": "network_call",
    "subprocess": "unexpected_subprocess",
    "shlex": "shell_use",
    "pip": "dynamic_dependency_installation",
}
_FORBIDDEN_CALL_CATEGORIES: dict[str, str] = {
    "os.system": "shell_use", "os.popen": "shell_use", "os.spawnl": "shell_use",
    "subprocess.run": "unexpected_subprocess", "subprocess.Popen": "unexpected_subprocess",
    "subprocess.call": "unexpected_subprocess", "subprocess.check_call": "unexpected_subprocess",
    "socket.socket": "network_call",
}


class ExecutionValidationRejected(RuntimeError):
    """Raised by ``run_pre_execution_validators`` naming every rejection
    category and its detail. ``categories`` is the sorted, deduplicated
    set of docs #52 categories triggered, for a caller that only needs
    to know which class of problem was found."""

    def __init__(self, violations: list[tuple[str, str]]) -> None:
        self.violations = violations
        self.categories = sorted({category for category, _detail in violations})
        detail = "; ".join(f"{category}: {reason}" for category, reason in violations)
        super().__init__(f"pre-execution validation rejected: {detail}")


def _iter_module_sources(module_paths: list[Path]) -> list[tuple[Path, ast.Module]]:
    parsed = []
    for path in module_paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        parsed.append((path, tree))
    return parsed


def _call_dotted_name(node: ast.Call) -> str:
    func = node.func
    parts: list[str] = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(func.id)
    return ".".join(reversed(parts))


class StaticCodeValidator:
    """Rejects: network calls, shell use, unexpected subprocess, dynamic
    dependency installation. AST-scans the raw-data worker's own module
    sources (never a user-submitted script -- Executor has no such
    surface; the fixed set of modules the sandboxed dispatch targets
    live in and call into) for a forbidden import or a forbidden dotted
    call name."""

    def validate(self, module_paths: list[Path]) -> list[tuple[str, str]]:
        violations: list[tuple[str, str]] = []
        for path, tree in _iter_module_sources(module_paths):
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        category = _FORBIDDEN_IMPORT_CATEGORIES.get(alias.name)
                        if category:
                            violations.append((category, f"{path.name}: import {alias.name!r}"))
                elif isinstance(node, ast.ImportFrom) and node.module:
                    category = _FORBIDDEN_IMPORT_CATEGORIES.get(node.module)
                    if category:
                        violations.append((category, f"{path.name}: from {node.module!r} import ..."))
                elif isinstance(node, ast.Call):
                    dotted = _call_dotted_name(node)
                    category = _FORBIDDEN_CALL_CATEGORIES.get(dotted)
                    if category:
                        violations.append((category, f"{path.name}: call {dotted!r}"))
        return violations


class OperationAllowlistValidator:
    """Rejects: unknown operation. Every decision's ``action`` must be a
    member of ``allowed_operations`` (the caller's ``ACTION_TYPES``,
    passed in rather than imported to keep this module independent of
    any PHI-specific action vocabulary)."""

    def validate(self, decisions: list[dict[str, Any]], allowed_operations: set[str]) -> list[tuple[str, str]]:
        violations = []
        for d in decisions:
            action = d.get("action")
            if action not in allowed_operations:
                violations.append((
                    "unknown_operation",
                    f"file_id={d.get('file_id')!r} column={d.get('column')!r} action={action!r}",
                ))
        return violations


class MethodRegistryValidator:
    """Rejects: unapproved method. A decision that names a
    ``method_id`` must resolve to a :class:`~.records.MethodRecord` at
    lifecycle ``"approved"`` -- research or candidate status alone never
    grants execution permission (docs #38, the same rule
    ``PHIMethodsExpert.method_for`` already enforces one stage earlier)."""

    def validate(
        self, decisions: list[dict[str, Any]], approved_methods: list[MethodRecord],
    ) -> list[tuple[str, str]]:
        approved_ids = {m.method_id for m in approved_methods if m.lifecycle == "approved"}
        violations = []
        for d in decisions:
            method_id = d.get("method_id")
            if method_id and method_id not in approved_ids:
                violations.append((
                    "unapproved_method",
                    f"file_id={d.get('file_id')!r} column={d.get('column')!r} method_id={method_id!r}",
                ))
        return violations


class CapabilityBroker:
    """Rejects: unapproved import, credential access. Confirms the
    worker modules import only from :data:`_APPROVED_WORKER_IMPORTS`
    (the positive-allowlist counterpart to ``StaticCodeValidator``'s
    negative denylist), and that the run's own
    :class:`~.records.CapabilityGrant` never lists a tool whose name is
    credential-shaped (``*_key``, ``*_secret``, ``*_token``,
    ``*_password``, ``*_credential``) -- Executor's raw-data work has no
    legitimate reason to hold such a grant at all."""

    _CREDENTIAL_SUFFIXES = ("_key", "_secret", "_token", "_password", "_credential")

    def validate(
        self, module_paths: list[Path], grant: CapabilityGrant | None = None,
    ) -> list[tuple[str, str]]:
        violations: list[tuple[str, str]] = []
        for path, tree in _iter_module_sources(module_paths):
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0]
                        full = alias.name
                        if top not in _APPROVED_WORKER_IMPORTS and full not in _APPROVED_WORKER_IMPORTS:
                            violations.append(("unapproved_import", f"{path.name}: import {alias.name!r}"))
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    top = node.module.split(".")[0]
                    if top not in _APPROVED_WORKER_IMPORTS and node.module not in _APPROVED_WORKER_IMPORTS:
                        violations.append(("unapproved_import", f"{path.name}: from {node.module!r} import ..."))
        if grant is not None:
            for tool_name in grant.tools:
                lowered = tool_name.lower()
                if any(lowered.endswith(suffix) for suffix in self._CREDENTIAL_SUFFIXES):
                    violations.append(("credential_access", f"grant tool {tool_name!r} is credential-shaped"))
        return violations


class PathPolicyValidator:
    """Rejects: host filesystem access, path escape. Refuses any dataset
    path outside ``phi_core.paths.DATA_DIR`` (the same "refuse any path
    outside the record's workspace" concept ``control/sandbox.py``
    documents at its module level for the sandbox workspace itself,
    applied here to the *input* dataset paths Executor is about to
    hand the worker) and any path containing a ``..`` traversal
    component."""

    def validate(self, files: list[dict[str, Any]]) -> list[tuple[str, str]]:
        violations: list[tuple[str, str]] = []
        root = DATA_DIR.resolve()
        for f in files:
            raw = f.get("stored_path")
            if not raw:
                continue
            candidate = Path(raw)
            if ".." in candidate.parts:
                violations.append(("path_escape", f"file_id={f.get('file_id')!r} path contains '..': {raw!r}"))
                continue
            resolved = candidate.resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                violations.append((
                    "host_filesystem_access",
                    f"file_id={f.get('file_id')!r} path {raw!r} is outside {root}",
                ))
        return violations


class ResourceLimitValidator:
    """Rejects: unbounded runtime, unbounded memory. A
    :class:`~.records.SandboxRecord` whose CPU/wall-clock/memory
    ceiling is zero, negative, or above ``control/limits.py``'s
    configured ``MAX_SANDBOX_*`` ceiling is refused -- the sandbox
    process must always run under a finite, policy-bounded ceiling."""

    def validate(self, sandbox: SandboxRecord | None) -> list[tuple[str, str]]:
        if sandbox is None:
            return []
        violations: list[tuple[str, str]] = []
        if sandbox.max_cpu_seconds <= 0 or sandbox.max_cpu_seconds > limits.MAX_SANDBOX_CPU_SECONDS:
            violations.append(("unbounded_runtime", f"max_cpu_seconds={sandbox.max_cpu_seconds}"))
        if sandbox.max_wall_seconds <= 0 or sandbox.max_wall_seconds > limits.MAX_SANDBOX_WALL_SECONDS:
            violations.append(("unbounded_runtime", f"max_wall_seconds={sandbox.max_wall_seconds}"))
        if sandbox.max_memory_bytes <= 0 or sandbox.max_memory_bytes > limits.MAX_SANDBOX_MEMORY_BYTES:
            violations.append(("unbounded_memory", f"max_memory_bytes={sandbox.max_memory_bytes}"))
        return violations


class SandboxPolicyValidator:
    """Rejects: network calls. Runtime-config counterpart to
    ``StaticCodeValidator``'s static-analysis network check: confirms
    the :class:`~.records.SandboxRecord` itself is actually configured
    to deny network access (``network_denied``), rather than only
    proving the worker's own source never calls a networking function --
    a monkeypatch removed from ``control/sandbox.py`` in the future
    would flip this from "also true" to "the only thing catching it"."""

    def validate(self, sandbox: SandboxRecord | None) -> list[tuple[str, str]]:
        if sandbox is None:
            return []
        if not sandbox.network_denied:
            return [("network_call", "sandbox record does not have network_denied set")]
        return []


def run_pre_execution_validators(
    *,
    decisions: list[dict[str, Any]],
    files: list[dict[str, Any]],
    allowed_operations: set[str],
    worker_module_paths: list[Path],
    approved_methods: list[MethodRecord] | None = None,
    grant: CapabilityGrant | None = None,
    sandbox: SandboxRecord | None = None,
) -> None:
    """Run all seven validators and raise :class:`ExecutionValidationRejected`
    naming every violation found across all of them, or return silently
    when every check passes."""
    violations: list[tuple[str, str]] = []
    violations += StaticCodeValidator().validate(worker_module_paths)
    violations += OperationAllowlistValidator().validate(decisions, allowed_operations)
    violations += MethodRegistryValidator().validate(decisions, approved_methods or [])
    violations += CapabilityBroker().validate(worker_module_paths, grant)
    violations += PathPolicyValidator().validate(files)
    violations += ResourceLimitValidator().validate(sandbox)
    violations += SandboxPolicyValidator().validate(sandbox)
    if violations:
        raise ExecutionValidationRejected(violations)
