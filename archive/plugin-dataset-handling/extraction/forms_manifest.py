#!/usr/bin/env python3
"""Shared forms-manifest gate.

Lives in scripts/ as a shared utility (Note 20 Gap B); imported by extraction,
the header-extraction skill, and the orchestrator. Never imports from plugins/.

The gate validates the contents of a study's ``datasets/`` directory against
its sibling ``_forms_manifest.yaml`` before any file is opened for extraction:
it raises :class:`ManifestMismatchError` on a missing-required or unknown file,
auto-skips ``reject:``-listed files, and surfaces per-column ``date_locales:``
overrides — degrading to a warning when the manifest is absent.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import NamedTuple

import yaml

from scripts.extraction.io.clinical_dates import check_locale_consistency
from scripts.extraction.io.file_discovery import SUPPORTED_TABULAR_EXTENSIONS
from scripts.utils.logging_system import get_logger

# Standard library logger for the gate — used by check_forms_manifest so
# that pytest's caplog fixture can capture messages during tests. Logger name
# is pinned to the historical "scripts.extraction.dataset_pipeline" channel so
# existing tests (and operator log filters) that target that logger keep
# capturing the gate's messages after the move.
_gate_log = get_logger("scripts.extraction.dataset_pipeline")

# File extensions recognised as tabular datasets.
#
# Imported from the canonical source rather than aliased through
# dataset_pipeline so this shared module has no back-dependency on the
# extraction pipeline that re-exports it.
SUPPORTED_EXTENSIONS: tuple[str, ...] = SUPPORTED_TABULAR_EXTENSIONS
"""File extensions recognised as tabular datasets.

Alias of :data:`scripts.extraction.io.file_discovery.SUPPORTED_TABULAR_EXTENSIONS`.
"""


# ============================================================================
# Forms-manifest gate
# ============================================================================


class ManifestMismatchError(Exception):
    """Raised when the datasets directory does not match the forms manifest.

    Possible causes:
        - A ``required:`` form is absent from the directory.
        - A file in the directory is not listed in required/optional/reject.

    Reject-matched files are *not* an error: they are auto-skipped, recorded
    in :attr:`ManifestCheckResult.rejected_files`, and an info-level log line
    is emitted for each so the audit trail records the skip explicitly.
    """


class ManifestCheckResult(NamedTuple):
    """Return value of :func:`check_forms_manifest`.

    Attributes
    ----------
    date_locales:
        Per-column date-locale overrides (column name → ``"DMY"``/``"MDY"``).
        Empty dict when the manifest is absent or the key is missing.
    rejected_files:
        Filenames present in ``datasets/`` that matched a ``reject:`` entry
        or fnmatch glob.  Callers must drop these from the extraction set;
        the gate itself only logs and collects them, never raises.
    """

    date_locales: dict[str, str]
    rejected_files: frozenset[str]


def check_forms_manifest(datasets_dir: Path | str) -> ManifestCheckResult:
    """Validate the contents of *datasets_dir* against its study's forms manifest.

    The manifest is expected at ``config/<study>/_forms_manifest.yaml`` (Note 11),
    where ``<study>`` is derived from the folder one level above the datasets
    directory (i.e. ``data/raw/<study>/datasets``) and resolved through
    :func:`config.study_config_path`.

    Manifest format (YAML, all keys optional but ``required``/``optional``/
    ``reject`` are the only recognised keys; ``date_locales`` is also loaded
    and returned for use by the date-parsing pipeline)::

        required:
          - form_name.xlsx
        optional:
          - form_name.xlsx
        reject:
          - "*_1.xlsx"   # fnmatch-style glob patterns are supported
        date_locales:    # per-column date locale overrides (case-insensitive keys)
          MY_DATE_COL: DMY

    Behaviour
    ---------
    Manifest absent:
        Logs a WARNING and returns an empty result without raising.  Keeps
        behaviour unchanged for studies that do not yet have a manifest.

    Reject pattern matched:
        File is auto-skipped: included in
        :attr:`ManifestCheckResult.rejected_files` and an INFO log line is
        emitted.  Does **not** raise — the reject list is the operator's
        declaration that those files are known junk/duplicates.

    Required form missing:
        Raises :exc:`ManifestMismatchError` listing the first missing form.

    Unknown file (not in required/optional/reject):
        Raises :exc:`ManifestMismatchError` listing the unknown file.

    Optional form missing:
        Logs a single INFO-level message; does **not** raise.

    Parameters
    ----------
    datasets_dir:
        Path to ``data/raw/{STUDY}/datasets/``.

    Returns
    -------
    ManifestCheckResult
        ``(date_locales, rejected_files)``.  Callers MUST filter discovered
        files by ``rejected_files`` before extraction — the gate only
        records the skip; it does not remove the files from disk.
    """
    import config

    datasets_dir = Path(datasets_dir)
    # The manifest now lives under config/<study>/ (Note 11), separate from the
    # raw data tree. Derive the study name from the {STUDY} folder one level
    # above datasets/ (i.e. data/raw/{STUDY}/datasets) and resolve through the
    # single config chokepoint.
    study_name = datasets_dir.parent.name
    manifest_path = config.study_config_path("_forms_manifest.yaml", study=study_name)

    # --- Manifest absent: warn + continue ---
    if not manifest_path.exists():
        _gate_log.warning(
            "No forms manifest found at %s; extraction proceeds without "
            "form-level gate (add _forms_manifest.yaml to enable it)",
            manifest_path,
        )
        return ManifestCheckResult(date_locales={}, rejected_files=frozenset())

    with manifest_path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    required: list[str] = raw.get("required") or []
    optional: list[str] = raw.get("optional") or []
    reject: list[str] = raw.get("reject") or []
    # Normalise keys to UPPER-CASE once here so all downstream callers
    # (including _resolve_locale in clinical_dates) can do O(1) dict.get()
    # without repeated field_name.upper() conversions per row.
    date_locales: dict[str, str] = {
        k.upper(): v for k, v in (raw.get("date_locales") or {}).items()
    }
    # Warn if any manifest entry conflicts with the hardcoded DMY_VARIABLES allowlist.
    check_locale_consistency(date_locales)

    # Collect actual .xlsx/.csv filenames present in the directory
    actual_files: list[str] = sorted(
        p.name
        for p in datasets_dir.iterdir()
        if p.is_file()
        and not p.name.startswith(".")
        and not p.name.startswith("~$")
        and p.suffix.lower() in SUPPORTED_EXTENSIONS
    )

    # Build lookup sets for fast membership tests
    required_set: frozenset[str] = frozenset(required)
    optional_set: frozenset[str] = frozenset(optional)

    # --- Step 1: classify reject matches (auto-skip, never raise) ---
    rejected: set[str] = set()
    for fname in actual_files:
        for pattern in reject:
            if fname == pattern or fnmatch.fnmatch(fname, pattern):
                rejected.add(fname)
                _gate_log.info(
                    "Reject-listed form auto-skipped: %s (matched pattern %r)",
                    fname,
                    pattern,
                )
                break  # first matching pattern is sufficient

    # --- Step 2: required forms must all be present ---
    actual_set: frozenset[str] = frozenset(actual_files)
    for required_form in required:
        if required_form not in actual_set:
            raise ManifestMismatchError(
                f"required form missing: {required_form!r} not found in {datasets_dir}"
            )
        # A required form cannot also be reject-listed; that is a manifest
        # authoring error and we surface it loudly rather than silently
        # dropping the file.
        if required_form in rejected:
            raise ManifestMismatchError(
                f"manifest conflict: {required_form!r} appears in both "
                "required: and reject: — fix _forms_manifest.yaml"
            )

    # --- Step 3: every actual file must be in required/optional or rejected ---
    for fname in actual_files:
        if fname in required_set or fname in optional_set or fname in rejected:
            continue
        raise ManifestMismatchError(
            f"unknown form (not in manifest): {fname!r}; "
            "add to required/optional/reject in _forms_manifest.yaml"
        )

    # --- Step 4: optional forms missing → info log only, no raise ---
    for opt_form in optional:
        if opt_form not in actual_set:
            _gate_log.info(
                "Optional form not present (skipped): %s",
                opt_form,
            )

    return ManifestCheckResult(
        date_locales=date_locales,
        rejected_files=frozenset(rejected),
    )


__all__ = [
    "ManifestCheckResult",
    "ManifestMismatchError",
    "check_forms_manifest",
]
