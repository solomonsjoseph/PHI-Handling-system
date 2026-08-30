"""ReportArtifacts (Phase 11b, docs #58): the shared shape ReportGeneration's
``ReportGenerator`` produces and Packaging/Integration's ``ZIPBuilder`` and
``IntegrityService`` consume.

Every field is a real, on-disk file path ReportGenerator actually wrote --
never a placeholder, never bytes held only in memory. ``human_review_summary_pdf``
is the one field allowed to stay ``None``: docs #61's canonical ZIP structure
omits the whole ``04_Human_Review/`` folder for a run where no human review
ever occurred, so ReportGenerator has nothing to write there and must say so
honestly rather than fabricating an empty PDF.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# The seven fields every run must populate regardless of whether human
# review occurred. Kept as a tuple of attribute names (not hand-duplicated
# in `is_report_package_complete`) so the two can never silently drift.
_ALWAYS_REQUIRED_FIELDS: tuple[str, ...] = (
    "audit_report_pdf",
    "column_ledger_xlsx",
    "technical_appendix_pdf",
    "evidence_manifest_json",
    "verification_manifest_json",
    "run_manifest_json",
    "checksums_sha256",
)


@dataclass(frozen=True)
class ReportArtifacts:
    """One run's full section-58 report bundle. Every field is
    ``Path | None`` to a real written file on disk; ``None`` is only ever
    correct for ``human_review_summary_pdf`` on a run with no human review."""

    audit_report_pdf: Path | None = None
    column_ledger_xlsx: Path | None = None
    technical_appendix_pdf: Path | None = None
    human_review_summary_pdf: Path | None = None
    evidence_manifest_json: Path | None = None
    verification_manifest_json: Path | None = None
    run_manifest_json: Path | None = None
    checksums_sha256: Path | None = None


def is_report_package_complete(artifacts: ReportArtifacts, *, human_review_occurred: bool) -> bool:
    """docs #57's ``report_package_complete`` condition, computed from a
    real :class:`ReportArtifacts` instance rather than accepted as an
    opaque flag -- closes the gap Phase 11a explicitly disclosed
    (``control/final_assurance.py``'s own docstring: "no live code in this
    session computes that boolean from an actually-generated report
    bundle").

    Every field in :data:`_ALWAYS_REQUIRED_FIELDS` must be populated
    (non-``None``). ``human_review_summary_pdf`` is required only when
    ``human_review_occurred`` is true for this run -- a run with no human
    review is genuinely complete without it, matching docs #61's "omit
    the whole 04_Human_Review/ folder" instruction.
    """
    if any(getattr(artifacts, name) is None for name in _ALWAYS_REQUIRED_FIELDS):
        return False
    if human_review_occurred and artifacts.human_review_summary_pdf is None:
        return False
    return True
