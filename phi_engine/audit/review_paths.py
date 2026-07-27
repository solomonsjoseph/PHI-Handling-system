"""Canonical paths for maintainer-facing human-review artifacts.

All count-only review reports live under ``output/{study}/audit/human_review/``,
organized **by form** (Note 22): every holding producer deposits a distinctly-named
note into ``human_review/{key}/`` so a reviewer sees everything for one form in a
single directory. *key* is the form id for form-scoped producers, and the dedup
group / run id for the inherently cross-form producers (dedup, publish gate).
This module is the single source of truth; writers and readers import helpers
from here rather than assembling paths ad hoc.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = [
    "HUMAN_REVIEW_ROOT",
    "LEGACY_SOT_REVIEW_DIR",
    "classification_review_path",
    "excel_duplicate_review_path",
    "organizer_review_path",
    "form_review_dir",
    "human_review_root",
    "intake_review_path",
    "is_sot_review_report_path",
    "legacy_sot_review_report_path",
    "presidio_failure_md_path",
    "publish_sot_joined_gate_md_path",
    "pycanon_report_md_path",
    "resolve_sot_review_report_path",
    "safe_review_slug",
    "scrub_quarantine_review_path",
    "sot_review_report_path",
    "verifier_review_path",
]

HUMAN_REVIEW_ROOT = "human_review"
LEGACY_SOT_REVIEW_DIR = "Sot_review"


def safe_review_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-")
    return slug or "unknown_form"


def human_review_root(audit_dir: Path) -> Path:
    return Path(audit_dir) / HUMAN_REVIEW_ROOT


def form_review_dir(audit_dir: Path, key: str) -> Path:
    """The single per-form (or per-key) review directory (Note 22)."""
    return human_review_root(audit_dir) / safe_review_slug(key)


def intake_review_path(audit_dir: Path) -> Path:
    """Count-only note for skill-0 quarantined files (Note 22, key='intake')."""
    return form_review_dir(audit_dir, "intake") / "intake_review.md"


def sot_review_report_path(audit_dir: Path, form: str) -> Path:
    return form_review_dir(audit_dir, form) / "review_report.md"


def legacy_sot_review_report_path(audit_dir: Path, form: str) -> Path:
    return Path(audit_dir) / LEGACY_SOT_REVIEW_DIR / safe_review_slug(form) / "review_report.md"


def resolve_sot_review_report_path(audit_dir: Path, form: str) -> Path:
    """Return the canonical path, or the legacy file when only that exists."""
    canonical = sot_review_report_path(audit_dir, form)
    if canonical.is_file():
        return canonical
    legacy = legacy_sot_review_report_path(audit_dir, form)
    if legacy.is_file():
        return legacy
    return canonical


def is_sot_review_report_path(path: Path) -> bool:
    # The SoT review is the only producer that writes ``review_report.md``; it
    # lives under the per-form dir (human_review/{form}/) or the legacy tree.
    if path.name != "review_report.md":
        return False
    parts = path.parts
    return HUMAN_REVIEW_ROOT in parts or LEGACY_SOT_REVIEW_DIR in parts


def dataset_jsonl_union_review_path(audit_dir: Path, stem: str) -> Path:
    return form_review_dir(audit_dir, stem) / "jsonl_union_review.md"


def excel_duplicate_review_path(audit_dir: Path, group: str) -> Path:
    return form_review_dir(audit_dir, group) / "duplicate_review_report.md"


def publish_sot_joined_gate_md_path(audit_dir: Path, run_id: str) -> Path:
    return form_review_dir(audit_dir, run_id) / "sot_joined_gate.md"


def presidio_failure_md_path(audit_dir: Path, form: str) -> Path:
    """Pre-promotion PHI guard-gate failure report (pattern + column + count only)."""
    return form_review_dir(audit_dir, form) / "presidio_failure.md"


def pycanon_report_md_path(audit_dir: Path, form: str) -> Path:
    """Publish-time pyCANON k-anonymity report (k, threshold, QI names, counts only)."""
    return form_review_dir(audit_dir, form) / "pycanon_report.md"


def classification_review_path(audit_dir: Path, form: str) -> Path:
    """PHI-classification hold note (Note 22): which columns/jurisdiction rule were
    ambiguous + the config/policy to edit. Column NAMES + counts only."""
    return form_review_dir(audit_dir, form) / "classification_review.md"


def scrub_quarantine_review_path(audit_dir: Path, form: str) -> Path:
    """PHI-scrub quarantine / 'elevated' hold note (Note 22): held-row count +
    reason codes + which config to fix. Counts only, never a row value."""
    return form_review_dir(audit_dir, form) / "scrub_quarantine_review.md"


def verifier_review_path(audit_dir: Path, form: str) -> Path:
    """Audit-verifier failure note (Note 22): failed assertion id + the form/column
    it concerns + the ledger/config to fix. Counts/ids only."""
    return form_review_dir(audit_dir, form) / "verifier_review.md"


def organizer_review_path(audit_dir: Path) -> Path:
    """Organizer review-bucket JSONL (Standalone refactor, Note 22): messy
    intake files the organizer could not route (unrecognized format, parse
    failure, broken intake symlink). One JSON record per entry --
    ``{file, link_name, reason, ...}`` — never a row value. Cross-file, not
    form-scoped, so it lives directly under ``human_review/`` rather than a
    per-form subdirectory (mirrors how dedup/publish-gate producers use a
    cross-form key, per this module's docstring)."""
    return human_review_root(audit_dir) / "organizer_review.jsonl"
