#!/usr/bin/env python3
"""Minimal compatibility shim for the forms-manifest gate.

``phi_engine.security.phi_scrub.run_scrub`` unconditionally imports
``scripts.extraction.forms_manifest.check_forms_manifest`` to resolve
per-column date-locale overrides. The full ``scripts/extraction/`` pipeline
(dataset extraction, tabular file discovery, locale-consistency checking)
that the original RePORT AI Portal plugin ships was never ported into this
repo (see ``docs/JURISDICTION_EVIDENCE_REPORT_IN.md`` "Porting gaps" and
Ground truth Note 51 in the evidence plan) — only ``phi_engine``'s
security/audit/config/skills layers are.

This module provides ONLY the one function ``run_scrub`` needs, with the
same documented contract as the original
``scripts/extraction/forms_manifest.py`` (archived at
``tmp/reportal-phi-plugin.zip:phi-plugin-export/scripts/extraction/forms_manifest.py``),
so ``run_scrub`` can execute standalone against pre-staged JSONL without
pulling in the rest of the un-ported extraction pipeline. Two deliberate
deviations from the archived original:

1. Imports ``phi_engine.config.config`` instead of a bare ``import config``
   — the bare form resolves to nothing in this repo layout (config.py now
   lives at ``phi_engine/config/config.py``, not the repo root).
2. Degrades gracefully (returns an empty result) when *datasets_dir* is not
   a directory on disk, rather than requiring it to exist — this repo's
   harness drives ``phi_scrub`` directly against staged JSONL
   (``tmp/<STUDY>/datasets/``) and never populates the raw
   ``data/raw/<STUDY>/datasets/`` tree ``check_forms_manifest`` was written
   to gate, so that directory legitimately never exists here.
"""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path
from typing import NamedTuple

import yaml

# Logger name pinned to match the archived original so any log-filter
# configuration targeting it keeps working.
_gate_log = logging.getLogger("scripts.extraction.dataset_pipeline")

# File extensions recognised as tabular datasets by the archived gate
# (scripts.extraction.io.file_discovery.SUPPORTED_TABULAR_EXTENSIONS).
SUPPORTED_EXTENSIONS: tuple[str, ...] = (".xlsx", ".xls", ".csv")


class ManifestMismatchError(Exception):
    """Raised when the datasets directory does not match the forms manifest."""


class ManifestCheckResult(NamedTuple):
    """Return value of :func:`check_forms_manifest`.

    Attributes
    ----------
    date_locales:
        Per-column date-locale overrides (column name -> "DMY"/"MDY").
        Empty dict when the manifest is absent or the key is missing.
    rejected_files:
        Filenames present in ``datasets/`` that matched a ``reject:`` entry
        or fnmatch glob.
    """

    date_locales: dict[str, str]
    rejected_files: frozenset[str]


def check_forms_manifest(datasets_dir: Path | str) -> ManifestCheckResult:
    """Validate *datasets_dir* against its study's ``_forms_manifest.yaml``.

    See the module docstring for the two deliberate deviations from the
    archived original. Manifest format and required/optional/reject/
    date_locales semantics are otherwise identical.
    """
    from phi_engine.config import config

    datasets_dir = Path(datasets_dir)
    study_name = datasets_dir.parent.name
    manifest_path = config.study_config_path("_forms_manifest.yaml", study=study_name)

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
    date_locales: dict[str, str] = {
        k.upper(): v for k, v in (raw.get("date_locales") or {}).items()
    }

    if not datasets_dir.is_dir():
        # Manifest present but no raw datasets dir on disk — this repo's
        # harness drives phi_scrub directly against pre-staged JSONL
        # (see harness/run_phi_system.py), so the file-presence gate below
        # (which requires datasets_dir.iterdir()) does not apply. Keep the
        # declared date_locales.
        return ManifestCheckResult(date_locales=date_locales, rejected_files=frozenset())

    actual_files: list[str] = sorted(
        p.name
        for p in datasets_dir.iterdir()
        if p.is_file()
        and not p.name.startswith(".")
        and not p.name.startswith("~$")
        and p.suffix.lower() in SUPPORTED_EXTENSIONS
    )

    required_set: frozenset[str] = frozenset(required)
    optional_set: frozenset[str] = frozenset(optional)

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
                break

    actual_set: frozenset[str] = frozenset(actual_files)
    for required_form in required:
        if required_form not in actual_set:
            raise ManifestMismatchError(
                f"required form missing: {required_form!r} not found in {datasets_dir}"
            )
        if required_form in rejected:
            raise ManifestMismatchError(
                f"manifest conflict: {required_form!r} appears in both "
                "required: and reject: — fix _forms_manifest.yaml"
            )

    for fname in actual_files:
        if fname in required_set or fname in optional_set or fname in rejected:
            continue
        raise ManifestMismatchError(
            f"unknown form (not in manifest): {fname!r}; "
            "add to required/optional/reject in _forms_manifest.yaml"
        )

    for opt_form in optional:
        if opt_form not in actual_set:
            _gate_log.info("Optional form not present (skipped): %s", opt_form)

    return ManifestCheckResult(
        date_locales=date_locales,
        rejected_files=frozenset(rejected),
    )


__all__ = [
    "ManifestCheckResult",
    "ManifestMismatchError",
    "check_forms_manifest",
]
