"""k-anonymity / small-cell suppression gate for agent-tool responses.

At the trio-bundle -> agent boundary, row-level queries can surface
equivalence classes (age-band x sex x district x outcome) with very
small sample sizes. A response returning one matched row with all
sensitive attributes visible defeats the whole scrub — the scrub
guarantees de-identification at rest, but k-anon defends against
re-identification at query time.

This module provides two utilities:

* :func:`kanon_check` — given a list of equivalence-class records and
  a *k* threshold, returns a :class:`KAnonResult` with ``blocked`` set
  when any class has fewer than *k* members.
* :func:`suppress_small_cells` — given aggregate counts, replaces any
  count < *k* with the string ``"<5"`` (or equivalent) so the agent
  surface never reveals an exact small-cell value.

IRB-grade benchmark anchor: Pillar 1.7 — k-anonymity ≥ 5 enforced on
quasi-identifier combos surfaced to the agent; l-diversity ≥ 2 is a
tracked design gap (see references.rst).
Reference: ICMR 2017 §11.7; NIST SP 800-188 §5.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from phi_engine.utils.logging_system import get_logger

logger = get_logger(__name__)

__all__ = [
    "KAnonResult",
    "LDiversityResult",
    "kanon_check",
    "l_diversity_check",
    "mask_small_cell",
    "suppress_small_cells",
]


_DEFAULT_K = 5


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
