"""OR-combined PHI residual guard gate (Wave 3 C3, decision D2).

``run_phi_guard_gate(root)`` runs **both** structured-PHI scanners over an
LLM-visible tree and fails if **either** finds PHI:

* primary  — :func:`scripts.security.presidio_gate.scan_tree_with_presidio`
* secondary — :func:`scripts.security.llm_source_gate.scan_tree_for_phi`
  (the study-calibrated structured-identifier catalog, retained as a safety floor)

OR-combination means adopting Presidio never *weakens* the gate: a leak the
legacy scanner caught still fails the gate even if Presidio's recognizer set ever
regressed, and vice-versa. Both scanners are value-free, so the combined result
is too. This is the single entry point wired into all three gate points (publish,
snapshot activation, session start), replacing the bare ``scan_tree_for_phi``
call so every surface inherits the primary scanner.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from phi_engine.security.llm_source_gate import LeakScanResult, scan_tree_for_phi
from phi_engine.security.presidio_gate import PresidioScanResult, scan_tree_with_presidio
from phi_engine.utils.logging_system import get_logger

__all__ = [
    "PHIGuardResult",
    "run_phi_guard_gate",
]

_logger = get_logger(__name__)


@dataclass(frozen=True)
class PHIGuardResult:
    """Combined outcome of the primary + secondary residual scanners."""

    ok: bool
    presidio: PresidioScanResult
    legacy: LeakScanResult
    triggered_by: tuple[str, ...] = field(default_factory=tuple)

    @property
    def detail(self) -> str:
        """First failing scanner's value-free detail (empty when ``ok``)."""
        if self.ok:
            return ""
        parts = []
        if not self.presidio.ok:
            parts.append(f"presidio: {self.presidio.detail}")
        if not self.legacy.ok:
            parts.append(f"legacy: {self.legacy.detail}")
        return " | ".join(parts)


def run_phi_guard_gate(root) -> PHIGuardResult:
    """Run both residual scanners over *root*; fail if either finds PHI."""
    presidio = scan_tree_with_presidio(root)
    legacy = scan_tree_for_phi(root)
    triggered = []
    if not presidio.ok:
        triggered.append("presidio")
    if not legacy.ok:
        triggered.append("legacy")
    ok = presidio.ok and legacy.ok
    if not ok:
        _logger.warning("PHI guard gate FAILED (triggered by: %s)", ", ".join(triggered))
    return PHIGuardResult(
        ok=ok,
        presidio=presidio,
        legacy=legacy,
        triggered_by=tuple(triggered),
    )
