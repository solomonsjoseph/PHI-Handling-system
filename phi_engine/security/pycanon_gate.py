"""pyCANON k-anonymity / l-diversity engine (Wave 3 C3).

**Publish gate status (2026-06): DEFERRED.** The live publish path uses
classification + scrub + Presidio/residual pattern scan only. This module
remains for agent-query-time measurement and future discussion — not invoked
at promotion in ``host_pipeline``.

Decision D2 (historical): pyCANON was intended as a publish-gate engine measuring
residual re-identification risk over quasi-identifier columns. The lightweight
:mod:`scripts.security.kanon_gate` stays the **agent-query-time** gate;
this module provides formal k-anonymity metrics when explicitly invoked.

Why a second engine: ``kanon_gate.kanon_check`` counts equivalence classes with a
plain dict — fine for a handful of query rows. pyCANON implements the formal
anonymity metrics (k-anonymity, l-diversity, t-closeness, …) used in the privacy
literature, giving an audit-credible publish-time number rather than a bespoke
count.

Value-free
----------
The result reports the computed integer k (the size of the smallest
quasi-identifier equivalence class), the threshold, the QI/sensitive column
*names*, and a record count — never a row value and never a violating class key
(a small-cell QI combination, even if generalized, is the one thing we are trying
not to surface).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from phi_engine.utils.logging_system import get_logger

__all__ = [
    "PyCanonGateResult",
    "check_publish_anonymity",
]

_logger = get_logger(__name__)

_DEFAULT_K = 5
# Sentinel for missing QI values — pyCANON/pandas sort mixed str/None otherwise.
_NULL_QI_SENTINEL = "<NULL>"


@dataclass(frozen=True)
class PyCanonGateResult:
    """Outcome of a pyCANON publish-gate anonymity measurement (value-free).

    ``l_value`` is the measured l-diversity (named ``l_value`` rather than ``l``
    to avoid the ambiguous single-letter identifier); ``None`` when l-diversity
    was not requested.
    """

    ok: bool
    k: int
    k_threshold: int
    quasi_identifiers: tuple[str, ...]
    n_records: int
    l_value: int | None = None
    l_threshold: int | None = None
    sensitive_attributes: tuple[str, ...] = ()
    reason: str = ""


def _normalize_qi_columns(df: Any, columns: Sequence[str]) -> Any:
    """Coerce null/empty QI values to a string sentinel so pyCANON can sort safely."""
    import pandas as pd

    out = df.copy()

    def _to_qi_str(v: Any) -> str:
        if v is None:
            return _NULL_QI_SENTINEL
        try:
            if pd.isna(v):
                return _NULL_QI_SENTINEL
        except (TypeError, ValueError):
            pass
        if isinstance(v, str) and not v.strip():
            return _NULL_QI_SENTINEL
        return str(v)

    for col in columns:
        if col not in out.columns:
            continue
        out[col] = out[col].map(_to_qi_str)
    return out


def check_publish_anonymity(
    records: Sequence[Mapping[str, Any]],
    *,
    quasi_identifiers: Sequence[str],
    k_threshold: int = _DEFAULT_K,
    sensitive_attributes: Sequence[str] | None = None,
    l_threshold: int | None = None,
) -> PyCanonGateResult:
    """Measure k-anonymity (and optional l-diversity) of *records* via pyCANON.

    Args:
        records: already-scrubbed published rows (list of dict-like).
        quasi_identifiers: QI column names to measure k over (must be non-empty
            and present in the records).
        k_threshold: minimum acceptable k; ``ok`` is False when ``k < k_threshold``.
        sensitive_attributes: when given with ``l_threshold``, also measure
            l-diversity over these columns.
        l_threshold: minimum acceptable l; when set (and sensitive_attributes
            given) ``ok`` additionally requires ``l >= l_threshold``.

    An empty record set is vacuously anonymous (``ok=True``, ``k=0``) — there is
    nothing to re-identify. Raises ``ValueError`` on an empty QI list or a
    threshold < 1.
    """
    if k_threshold < 1:
        raise ValueError(f"k_threshold must be >= 1, got {k_threshold}")
    qi = tuple(quasi_identifiers)
    if not qi:
        raise ValueError("quasi_identifiers must be non-empty")
    sens = tuple(sensitive_attributes or ())
    measure_l = l_threshold is not None and bool(sens)

    if not records:
        return PyCanonGateResult(
            ok=True,
            k=0,
            k_threshold=k_threshold,
            quasi_identifiers=qi,
            n_records=0,
            l_value=0 if measure_l else None,
            l_threshold=l_threshold if measure_l else None,
            sensitive_attributes=sens,
            reason="no records (vacuously anonymous)",
        )

    # Lazy heavy imports — pandas + pycanon only loaded when the gate runs.
    import pandas as pd
    from pycanon import anonymity

    df = pd.DataFrame.from_records(list(records))
    missing = [c for c in (*qi, *sens) if c not in df.columns]
    if missing:
        raise ValueError(f"columns absent from records: {sorted(missing)}")

    qi_cols = list(dict.fromkeys((*qi, *sens)))
    df = _normalize_qi_columns(df, qi_cols)

    k = int(anonymity.k_anonymity(df, list(qi)))
    l_val: int | None = None
    if measure_l:
        l_val = int(anonymity.l_diversity(df, list(qi), list(sens)))

    ok = k >= k_threshold and (l_val is None or l_val >= int(l_threshold))  # type: ignore[arg-type]
    if not ok:
        _logger.warning(
            "pycanon publish gate FAILED: k=%d (threshold %d)%s over %d QI columns, %d records",
            k,
            k_threshold,
            f", l={l_val} (threshold {l_threshold})" if measure_l else "",
            len(qi),
            len(df),
        )
    return PyCanonGateResult(
        ok=ok,
        k=k,
        k_threshold=k_threshold,
        quasi_identifiers=qi,
        n_records=len(df),
        l_value=l_val,
        l_threshold=l_threshold if measure_l else None,
        sensitive_attributes=sens,
        reason="" if ok else "k/l below threshold",
    )
