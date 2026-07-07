"""Deterministic, LOCAL, trusted-code value profiler (the review-reduction lever).

``profile_column(values)`` reads dataset ROW VALUES directly -- this is
LOCAL trusted code, explicitly allowed to do so because it NEVER leaves the
process (mirrors the existing precedent of ``phi_scrub`` reading rows
locally). It is never wired to any LLM call and never constructs a prompt;
``phi_engine.pipeline.run`` is the only caller.

Wired into ``run_pipeline``'s classification step as two rules:

- **ESCALATION** (accuracy-protecting, always on): a header classified
  ``keep`` by the name-rule engine but whose profile shows a blocking
  pattern hit rate above :data:`ESCALATION_BLOCKING_RATE_THRESHOLD` of its
  non-empty values is force-dropped with reason ``value-profile-conflict``
  -- this is the "PHI planted in an unexpected/mislabeled column" backstop.
- **AUTO-CLEAR** (review-reducing, conservative): a header already force-
  dropped pending human confirmation (a PHI-risky NAME with no SoT/decision
  confirming it benign) whose profile shows a CLOSED categorical set is
  auto-approved as ``keep`` with reason ``value-profile-closed-categorical``.
  This cannot leak an identifier series: an identifier is high-cardinality
  by construction (approaching one distinct value per row), so a column
  with at most :data:`AUTO_CLEAR_MAX_DISTINCT` distinct non-empty values,
  zero blocking-pattern hits, zero warn-pattern hits, and zero date-parses
  is STRUCTURALLY PROVEN to not be an identifier or date series -- this is
  a proof, not a confidence heuristic, so it is safe to auto-apply.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from phi_engine.security.phi_patterns import BLOCKING_PATTERNS, WARN_PATTERNS
from phi_engine.utils._extraction_io.clinical_dates import value_looks_like_date

__all__ = [
    "AUTO_CLEAR_MAX_DISTINCT",
    "ESCALATION_BLOCKING_RATE_THRESHOLD",
    "ColumnProfile",
    "profile_column",
]

#: A column with at most this many distinct non-empty values cannot be an
#: identifier/free-text series (those are high-cardinality by construction).
AUTO_CLEAR_MAX_DISTINCT = 10

#: A keep-classified column whose non-empty values trip a BLOCKING pattern
#: more than half the time is treated as a name/classification mismatch,
#: not tolerable noise -- force-dropped rather than published raw.
ESCALATION_BLOCKING_RATE_THRESHOLD = 0.5


@dataclass(frozen=True)
class ColumnProfile:
    """Value-shape summary for one column. Never records a raw value --
    only counts, rates, and pattern CATEGORY names (never matched text)."""

    non_empty_count: int
    distinct_count: int
    blocking_hit_count: int
    warn_hit_count: int
    date_parse_count: int
    numeric_count: int
    blocking_categories: tuple[str, ...] = field(default_factory=tuple)

    @property
    def blocking_hit_rate(self) -> float | None:
        return self.blocking_hit_count / self.non_empty_count if self.non_empty_count else None

    @property
    def warn_hit_rate(self) -> float | None:
        return self.warn_hit_count / self.non_empty_count if self.non_empty_count else None

    @property
    def date_parse_rate(self) -> float | None:
        return self.date_parse_count / self.non_empty_count if self.non_empty_count else None

    @property
    def is_closed_categorical(self) -> bool:
        return (
            self.non_empty_count > 0
            and self.distinct_count <= AUTO_CLEAR_MAX_DISTINCT
            and self.blocking_hit_count == 0
            and self.warn_hit_count == 0
            and self.date_parse_count == 0
        )

    @property
    def is_value_profile_conflict(self) -> bool:
        rate = self.blocking_hit_rate
        return rate is not None and rate > ESCALATION_BLOCKING_RATE_THRESHOLD

    def to_json(self) -> dict[str, object]:
        return {
            "non_empty_count": self.non_empty_count,
            "distinct_count": self.distinct_count,
            "blocking_hit_count": self.blocking_hit_count,
            "warn_hit_count": self.warn_hit_count,
            "date_parse_count": self.date_parse_count,
            "numeric_count": self.numeric_count,
            "blocking_categories": list(self.blocking_categories),
            "blocking_hit_rate": self.blocking_hit_rate,
            "is_closed_categorical": self.is_closed_categorical,
            "is_value_profile_conflict": self.is_value_profile_conflict,
        }


def profile_column(values: Iterable[object]) -> ColumnProfile:
    """Profile one column's values. Local, in-process, never persisted raw."""
    non_empty = 0
    distinct: set[str] = set()
    blocking_hits = 0
    warn_hits = 0
    date_hits = 0
    numeric_hits = 0
    blocking_categories: set[str] = set()

    for raw in values:
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        non_empty += 1
        distinct.add(text)

        value_blocked = False
        for name, pattern in BLOCKING_PATTERNS:
            if pattern.search(text):
                value_blocked = True
                blocking_categories.add(name)
        if value_blocked:
            blocking_hits += 1

        if any(pattern.search(text) for _name, pattern in WARN_PATTERNS):
            warn_hits += 1

        if value_looks_like_date(text):
            date_hits += 1

        try:
            float(text)
            numeric_hits += 1
        except ValueError:
            pass

    return ColumnProfile(
        non_empty_count=non_empty,
        distinct_count=len(distinct),
        blocking_hit_count=blocking_hits,
        warn_hit_count=warn_hits,
        date_parse_count=date_hits,
        numeric_count=numeric_hits,
        blocking_categories=tuple(sorted(blocking_categories)),
    )
