"""Evidence_Manifest.json / Verification_Manifest.json / Run_Manifest.json /
CHECKSUMS.sha256 (docs #58): plain JSON exports of the already-existing,
already-typed control records. No re-derivation, no new fields -- each
export is ``model_dump(mode="json")`` of the record(s) the caller already
built earlier in the run (``EvidenceRecord``, ``VerificationResult``,
``RunManifest`` -- the docs #63 reproducibility projection). Every one of
those records is already scoped to safe, non-PHI metadata by its own
schema (see each class's docstring in ``.records``), so this module never
performs its own safety filtering -- there is nothing raw to filter.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

from ..publish_guard import _sha256_of_file  # local import: reuse the one hashing routine, no duplicate
from .records import EvidenceRecord, RunManifest, VerificationResult


def export_evidence_manifest(records: Sequence[EvidenceRecord], path: Path) -> Path:
    """docs #58's ``Evidence_Manifest.json``: every ``EvidenceRecord`` the
    run's EvidenceRegistry composed, keyed under ``"evidence"``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"evidence": [r.model_dump(mode="json") for r in records]}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def export_verification_manifest(result: VerificationResult, path: Path) -> Path:
    """docs #58's ``Verification_Manifest.json``: DeterministicVerifier's
    typed post-execution verdict (docs #54), exported as-is."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True), encoding="utf-8")
    return path


def export_run_manifest(manifest: RunManifest, path: Path) -> Path:
    """docs #58's ``Run_Manifest.json``: the docs #63 reproducibility
    record (repository commit, workflow/agent/prompt/model/provider
    versions, RunPrivacyPolicy version, Method Registry/validator
    versions, transformation hashes, feature flags, trace root hash) --
    exported exactly as ``RunManifest`` already carries it. No secrets and
    no raw study values ever reach this record's schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True), encoding="utf-8")
    return path


def write_checksums(paths: Sequence[Path], output_path: Path) -> Path:
    """docs #58's ``CHECKSUMS.sha256``: one ``sha256  filename`` line per
    real file already written to ``output_path``'s directory, in a
    coreutils-``sha256sum``-compatible format. Reuses Publish Guard's own
    ``_sha256_of_file`` (same hashing routine, imported not duplicated)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{_sha256_of_file(p)}  {p.name}" for p in sorted(paths, key=lambda p: p.name)]
    output_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return output_path
