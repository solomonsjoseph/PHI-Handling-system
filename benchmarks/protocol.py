"""Benchmark protocol metadata for PHI detector evaluations."""
from __future__ import annotations

BENCHMARK_PROTOCOL_VERSION = "2026-07-06.strict-v1"
PRIMARY_SCORING_PROFILE = "strict_all_span"
SECONDARY_SCORING_PROFILE = "legacy_overlap_coverable"


def protocol_dict() -> dict[str, str]:
    return {
        "version": BENCHMARK_PROTOCOL_VERSION,
        "primary_scoring_profile": PRIMARY_SCORING_PROFILE,
        "secondary_scoring_profile": SECONDARY_SCORING_PROFILE,
        "statement": "structural gaps count as misses for end-to-end detector claims",
    }
