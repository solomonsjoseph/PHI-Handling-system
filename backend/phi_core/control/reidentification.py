"""k-anonymity / l-diversity gate, lifted from the deleted ``phi_engine``.

Ported verbatim (names and signatures preserved) from
``phi_engine/security/kanon_gate.py`` and ``phi_engine/security/pycanon_gate.py``
before those sources were deleted. Only two mechanical changes: the
``phi_engine.utils.logging_system`` logger became the stdlib ``logging`` module,
and the pycanon import is guarded so ``check_publish_anonymity`` fails with an
explicit ``ImportError`` (rather than a bare module-resolution error) when
pycanon is not installed.

pycanon status (2026-09-01, this platform): ``pip install "pycanon>=1.0.1"``
does not install cleanly on Python 3.11 / arm64 — it resolves numpy 2.0.2
(breaking spacy's ``thinc 8.2.5`` numpy<2 pin), downgrades pandas to 2.3.3, and
its 1.3.6 surface no longer exports the ``anonymity`` module this code was
written against. Per the rewrite plan's stated contingency, the pure-Python
gate (:func:`kanon_check`, :func:`l_diversity_check`, :func:`mask_small_cell`,
:func:`suppress_small_cells`) is the operational one and its numeric
``smallest_class_size`` is the recorded-k acceptance number;
:func:`check_publish_anonymity` remains available behind
:data:`PYCANON_AVAILABLE` and reports its unavailability explicitly instead of
silently pretending to have run.

Value-free contract (unchanged from the originals): results report keys,
counts, sizes, and thresholds only — never a raw cell value. The number that
matters for acceptance criterion 9 is :attr:`KAnonResult.smallest_class_size`.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "PYCANON_AVAILABLE",
    "KAnonResult",
    "LDiversityResult",
    "PyCanonGateResult",
    "kanon_check",
    "l_diversity_check",
    "check_publish_anonymity",
    "mask_small_cell",
    "suppress_small_cells",
]

_DEFAULT_K = 5

# Sentinel for missing QI values — pyCANON/pandas sort mixed str/None otherwise.
_NULL_QI_SENTINEL = "<NULL>"

try:  # pycanon is only required by check_publish_anonymity; it fails lazily.
    _pycanon_anonymity = importlib.import_module("pycanon.anonymity")
    PYCANON_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised expressly on this platform
    _pycanon_anonymity = None
    PYCANON_AVAILABLE = False


# ---------------------------------------------------------------------------
# kanon_gate.py port
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KAnonResult:
    """Outcome of a k-anonymity check.

    ``blocked`` is ``True`` when at least one equivalence class is
    smaller than *k*. ``smallest_class_size`` reports the minimum
    class size observed (or 0 when no classes were supplied).
    ``violating_keys`` is a sorted tuple of equivalence-class keys
    whose size is below the threshold; each key is a string form of
    the quasi-identifier tuple, safe to log.
    """

    blocked: bool
    smallest_class_size: int
    violating_keys: tuple[str, ...]


def _key_to_str(key: tuple[Any, ...]) -> str:
    return "|".join("" if v is None else str(v) for v in key)


def kanon_check(
    rows: Iterable[Mapping[str, Any]],
    *,
    quasi_identifiers: tuple[str, ...],
    k: int = _DEFAULT_K,
) -> KAnonResult:
    """Return a :class:`KAnonResult` for the given rows + quasi-identifiers.

    Does NOT mutate *rows*. Counts equivalence classes by the tuple of
    quasi-identifier values; any class with size < *k* marks the result
    as ``blocked``. An empty input returns ``blocked=False`` with zero
    class size — caller decides whether empty is permitted.
    """
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")
    if not quasi_identifiers:
        raise ValueError("quasi_identifiers must be non-empty")

    counts: dict[tuple[Any, ...], int] = {}
    for row in rows:
        key = tuple(row.get(col) for col in quasi_identifiers)
        counts[key] = counts.get(key, 0) + 1

    if not counts:
        return KAnonResult(blocked=False, smallest_class_size=0, violating_keys=())

    smallest = min(counts.values())
    violating = sorted(_key_to_str(key) for key, size in counts.items() if size < k)
    blocked = smallest < k
    if blocked:
        logger.warning(
            "kanon_check: smallest class %d < k=%d (%d violating equivalence classes)",
            smallest,
            k,
            len(violating),
        )
    return KAnonResult(
        blocked=blocked,
        smallest_class_size=smallest,
        violating_keys=tuple(violating),
    )


@dataclass(frozen=True)
class LDiversityResult:
    """Outcome of an l-diversity check.

    A row set passes l-diversity (l ≥ 2) when every equivalence class
    (defined by the quasi-identifier tuple) contains at least *l*
    distinct values for each sensitive attribute. l = 2 is the
    smallest meaningful threshold; higher values resist homogeneity
    attacks more strongly.

    ``blocked`` is ``True`` when at least one (class, sensitive_attr)
    pair has fewer than *l* distinct values. ``violating_classes``
    enumerates which equivalence classes failed and on which attribute.
    """

    blocked: bool
    smallest_diversity: int
    violating_classes: tuple[tuple[str, str], ...]
    """Tuples of ``(equivalence_class_key, sensitive_attribute_name)``
    whose distinct-value count fell below *l*."""


def l_diversity_check(
    rows: Iterable[Mapping[str, Any]],
    *,
    quasi_identifiers: tuple[str, ...],
    sensitive_attributes: tuple[str, ...],
    l_threshold: int = 2,
) -> LDiversityResult:
    """Verify that every equivalence class has ≥ ``l_threshold`` distinct
    values for every sensitive attribute.

    Use AFTER :func:`kanon_check` — k-anonymity ensures classes are
    large enough; l-diversity ensures they aren't homogeneous on the
    outcomes that matter (e.g., all 5+ subjects in a class share
    ``outcome=DIED``). Empty input returns ``blocked=False``.

    Raises ``ValueError`` if either tuple is empty or ``l_threshold < 1``.
    """
    if l_threshold < 1:
        raise ValueError(f"l_threshold must be >= 1, got {l_threshold}")
    if not quasi_identifiers:
        raise ValueError("quasi_identifiers must be non-empty")
    if not sensitive_attributes:
        raise ValueError("sensitive_attributes must be non-empty")

    classes: dict[tuple[Any, ...], dict[str, set[Any]]] = {}
    for row in rows:
        key = tuple(row.get(col) for col in quasi_identifiers)
        bucket = classes.setdefault(key, {attr: set() for attr in sensitive_attributes})
        for attr in sensitive_attributes:
            bucket[attr].add(row.get(attr))

    if not classes:
        return LDiversityResult(blocked=False, smallest_diversity=0, violating_classes=())

    smallest = l_threshold
    violations: list[tuple[str, str]] = []
    for key, bucket in classes.items():
        for attr, values in bucket.items():
            div = len(values)
            if div < smallest:
                smallest = div
            if div < l_threshold:
                violations.append((_key_to_str(key), attr))

    blocked = bool(violations)
    if blocked:
        logger.warning(
            "l_diversity_check: smallest diversity %d < l=%d (%d violating "
            "(class, attribute) pairs)",
            smallest,
            l_threshold,
            len(violations),
        )
    return LDiversityResult(
        blocked=blocked,
        smallest_diversity=smallest,
        violating_classes=tuple(sorted(violations)),
    )


def mask_small_cell(
    count: int,
    *,
    k: int = _DEFAULT_K,
    label: str | None = None,
) -> Any:
    """Return *count* if ``count >= k``, else the suppression label.

    GAP-8: when *label* is not explicitly provided the label is derived from
    *k* as ``f'<{k}'`` so the suppression text always matches the threshold
    actually applied — previously the label was hardcoded to ``"<5"``
    regardless of the k value passed by the caller.  Passing an explicit
    *label* still overrides the default (backwards-compatible).

    Pair with :func:`suppress_small_cells` when aggregating cross-
    tabulations for the agent surface.
    """
    if count >= k:
        return count
    return label if label is not None else f"<{k}"


def suppress_small_cells(
    counts: Mapping[Any, int],
    *,
    k: int = _DEFAULT_K,
    label: str | None = None,
) -> dict[Any, Any]:
    """Return a new dict where values < *k* are replaced with the suppression label.

    Leaves keys untouched. Intended for cross-tab / frequency counts
    that a tool is about to return to the LLM. The default label is derived
    from *k* (see :func:`mask_small_cell`).
    """
    return {key: mask_small_cell(val, k=k, label=label) for key, val in counts.items()}


# ---------------------------------------------------------------------------
# pycanon_gate.py port
# ---------------------------------------------------------------------------


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

    Raises :class:`ImportError` when pycanon is not installed: the gate cannot
    silently no-op on non-empty records, and a fake "passed" result would hide
    the unavailability. Check :data:`PYCANON_AVAILABLE` first if the result can
    be tolerated as unavailable.
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

    if _pycanon_anonymity is None:
        raise ImportError(
            "pycanon is required for check_publish_anonymity on non-empty "
            "records but is not installed (PYCANON_AVAILABLE is False); the "
            "pure-Python kanon_check/l_diversity_check in this module remain "
            "available"
        )

    # Lazy heavy import — pandas only loaded when the gate runs on real records.
    import pandas as pd

    df = pd.DataFrame.from_records(list(records))
    missing = [c for c in (*qi, *sens) if c not in df.columns]
    if missing:
        raise ValueError(f"columns absent from records: {sorted(missing)}")

    qi_cols = list(dict.fromkeys((*qi, *sens)))
    df = _normalize_qi_columns(df, qi_cols)

    k = int(_pycanon_anonymity.k_anonymity(df, list(qi)))
    l_val: int | None = None
    if measure_l:
        l_val = int(_pycanon_anonymity.l_diversity(df, list(qi), list(sens)))

    ok = k >= k_threshold
    if l_val is not None and l_threshold is not None:
        ok = ok and l_val >= l_threshold
    if not ok:
        logger.warning(
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
