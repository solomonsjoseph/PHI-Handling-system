"""Wave R-d: the run-scoped, in-process leak-canary gate (spec section 72).

Section 72 requires scanning every forbidden surface for a synthetic
planted-literal canary. Twelve of the thirteen surfaces (exports, trace
events, workflow_runs.opaque_map, the learning store, ...) are persisted
artifacts an acceptance run can query after the fact -- ``phi_corpus.verify``'s
``scan_exports_for_leaks`` and ``scan_run_surfaces_for_leaks`` cover those.

The remaining surface -- the outbound provider payload ``gateway.py`` sends
-- is, by design, never persisted: ``egress.canonical_payload`` builds it,
``egress.egress_digest`` hashes it, and the raw bytes are dropped
immediately after. A leak on that surface can only be caught in-process, at
the moment the payload is assembled and before it leaves the process. This
module is that in-process gate: a run-scoped ``CanarySet`` a caller
registers at run start, a scan primitive ``ProviderGateway.complete``
(``gateway.py``) runs against every outbound payload, and the
:class:`SecurityBoundaryViolation` it raises on a hit.

``CanarySet`` is genuinely process-local: a plain module-level dict keyed
by ``run_id``, populated by :func:`activate_canary_set` and cleared by
:func:`deactivate_canary_set`. It is never written to any ``ControlStore``
collection, never logged, and never placed inside a ``ControlRecord`` --
only the verdict (clean/violation, an opaque ``canary_id``, and a
``hit_count``) is ever persisted, and only by the caller that scans against
it (``gateway.py``, via a ``TraceEvent``).
"""
from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from phi_core.crypto import egress_digest_key

# Matches phi_corpus.planters._enforce_canary_uniqueness's own floor: a
# literal shorter than 4 characters collides with innocuous content too
# often to be a meaningful signal.
_MIN_LITERAL_LENGTH = 4
_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9@.'\-]+")


class SecurityBoundaryViolation(Exception):
    """Raised the moment a planted canary literal is detected in an
    outbound payload, immediately before that payload would leave the
    process (spec section 71: ``SECURITY_BOUNDARY_VIOLATION``).

    Deliberately a plain ``Exception``, not a ``RuntimeError``/``ValueError``/
    ``CapabilityDenied`` subclass: ``ProviderGateway.complete``'s existing
    ``except (CapabilityDenied, HTTPException, RuntimeError, ValueError)``
    clause converts those into an ordinary ``denied`` ``GatewayResult`` --
    correct for a budget or capability refusal a caller may reasonably
    retry past, wrong here. Section 71 requires STOP / BLOCK / ISOLATE /
    ESCALATE and explicitly "DO NOT automatically resume"; an exception
    that is not silently swallowed into a routine denial is what makes
    that true. Carries only ``canary_id`` (opaque) and ``hit_count`` --
    never the matched literal or any surrounding context.
    """

    failure_class = "SECURITY_BOUNDARY_VIOLATION"

    def __init__(self, canary_id: str, hit_count: int) -> None:
        self.canary_id = canary_id
        self.hit_count = hit_count
        super().__init__(f"canary literal detected in outbound payload (hit_count={hit_count})")


@dataclass(frozen=True)
class CanaryScanResult:
    """The verdict of one scan. Never carries the matched literal itself --
    only whether one was found, how many, and an opaque id for the first
    match (see :meth:`CanarySet._canary_id`)."""

    hit: bool
    hit_count: int
    canary_id: str  # "" when hit is False


def _partition(literals: Iterable[str]) -> tuple[dict[str, str], list[str]]:
    """Same discipline ``phi_corpus.verify``'s export scanner uses: a
    single-token literal goes into a lowercase set for O(1) membership
    tests; a multi-token literal (a name, a note fragment) is matched by
    substring. Both are case-insensitive."""
    single: dict[str, str] = {}
    multi: list[str] = []
    seen_multi: set[str] = set()
    for lit in literals:
        if not lit or len(lit) < _MIN_LITERAL_LENGTH:
            continue
        lowered = lit.lower()
        tokens = [t for t in _TOKEN_SPLIT.split(lowered) if t]
        if len(tokens) <= 1:
            single[lowered] = lowered
        elif lowered not in seen_multi:
            seen_multi.add(lowered)
            multi.append(lowered)
    return single, multi


class CanarySet:
    """A run-scoped set of planted canary literals, held only in this
    process's memory for the lifetime of one run. Construct directly from
    a flat literal iterable, or via :func:`activate_canary_set` from a
    ``CorpusArtifact.ground_truth`` dict. Never persisted."""

    def __init__(self, run_id: str, literals: Iterable[str]) -> None:
        self._run_id = run_id
        self._single, self._multi = _partition(literals)
        # Same HMAC-of-run-scoped-key discipline as opaque.OpaqueMap: a
        # deterministic id derived from the run and the matched value,
        # never the value itself, and never reversible without the
        # process's own encryption key.
        self._run_key = hmac.new(
            egress_digest_key(), f"canary-v1\0{run_id}".encode("utf-8"), hashlib.sha256
        ).digest()

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def is_empty(self) -> bool:
        return not self._single and not self._multi

    def _canary_id(self, matched_literal_lower: str) -> str:
        return hmac.new(
            self._run_key, matched_literal_lower.encode("utf-8"), hashlib.sha256
        ).hexdigest()[:16]

    def scan_text(self, text: str) -> CanaryScanResult:
        """Scan one string for any planted literal. Returns a clean result
        immediately (no work) when the set is empty or ``text`` is empty."""
        if not text or self.is_empty:
            return CanaryScanResult(False, 0, "")
        lower = text.lower()
        hit_count = 0
        first_match = ""
        for tok in set(t for t in _TOKEN_SPLIT.split(lower) if t):
            if tok in self._single:
                hit_count += 1
                first_match = first_match or tok
        for lit in self._multi:
            if lit in lower:
                hit_count += 1
                first_match = first_match or lit
        if hit_count == 0:
            return CanaryScanResult(False, 0, "")
        return CanaryScanResult(True, hit_count, self._canary_id(first_match))

    def scan_payload(self, payload: bytes | str) -> CanaryScanResult:
        """Scan a ``canonical_payload``-shaped byte string (or already
        text) for any planted literal."""
        if isinstance(payload, (bytes, bytearray)):
            text = payload.decode("utf-8", errors="replace")
        else:
            text = str(payload)
        return self.scan_text(text)


# ---- process-local, run-scoped registry ------------------------------------
# A plain module-level dict: lives only in this process's memory, for
# exactly the interval between activate_canary_set() and
# deactivate_canary_set(). Never a ControlStore collection, never
# serialized to disk, never logged.
_ACTIVE: dict[str, CanarySet] = {}


def activate_canary_set(run_id: str, ground_truth: Mapping[str, Any]) -> CanarySet:
    """Build and register a :class:`CanarySet` from a
    ``phi_corpus.planters.CorpusArtifact.ground_truth`` dict's ``planted``
    cells (each cell's ``leak_literals``). Duck-typed on that dict shape --
    this module never imports ``phi_corpus`` (``phi_corpus`` already
    imports ``phi_core``; the reverse would be a circular, backward
    dependency)."""
    literals: list[str] = []
    for cell in ground_truth.get("planted") or []:
        literals.extend(cell.get("leak_literals") or [])
    canary_set = CanarySet(run_id, literals)
    _ACTIVE[run_id] = canary_set
    return canary_set


def active_canary_set(run_id: str) -> CanarySet | None:
    """The registered :class:`CanarySet` for ``run_id``, or ``None`` when
    no canary harness activated one -- the common case for every
    production run. Callers must treat ``None`` as "do not scan", not as
    an empty/clean canary set."""
    return _ACTIVE.get(run_id)


def deactivate_canary_set(run_id: str) -> None:
    """Drop the registered :class:`CanarySet` for ``run_id``, if any."""
    _ACTIVE.pop(run_id, None)
