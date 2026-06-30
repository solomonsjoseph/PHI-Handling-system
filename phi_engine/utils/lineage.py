"""Per-run lineage manifest for the RePORT AI Portal pipeline.

Regulators auditing a clinical de-identification pipeline want a single
artifact that ties every input file to every output file with hashes and
timestamps at each transformation step. This module produces that
artifact as ``output/{STUDY}/audit/lineage_manifest.json``.

The manifest records:

* **Run metadata** — pipeline version, extraction engine, UTC timestamp,
  compliance posture, study name.
* **Inputs** — every raw file that entered the pipeline this run, with
  SHA-256 + size + mtime.
* **Outputs** — every file in the published ``llm_source/`` + every
  audit report, with SHA-256 + size.
* **Steps** — per-leg (datasets / dictionary / pdfs) timestamps and
  rule-action counts (read from existing audit reports; this module
  does NOT re-compute scrub events).

The manifest carries only counts and hashes — never raw PHI values.
Caller must ensure :func:`emit_lineage_manifest` runs AFTER
``_publish_staging`` so the llm_source bundle exists and AFTER all audit
reports are on disk.

IRB-grade benchmark anchors:
    * NIST SP 800-188 §7 governance + audit
    * FDA 21 CFR Part 11 §11.10(e) audit record requirements
    * ICMR 2017 §11.5 audit + confidentiality
    * CDISC ODM origin/source traceability
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
UTC = timezone.utc  # py3.9 compat
from pathlib import Path
from typing import Any

from phi_engine.utils._extraction_io import atomic_write_json
from phi_engine.security.secure_env import assert_output_zone
from phi_engine.utils.integrity import hash_file as hash_path
from phi_engine.utils.logging_system import get_logger

logger = get_logger(__name__)

__all__ = [
    "LineageManifestError",
    "emit_lineage_manifest",
    "hash_path",
]


class LineageManifestError(Exception):
    """Raised when the lineage manifest cannot be assembled."""


def _file_metadata(path: Path) -> tuple[dict[str, Any], str]:
    """Return ``({sha256, size}, mtime_utc_str)`` for a regular file.

    The ``mtime_utc`` string is returned separately so callers can route it
    to the per-run timing sidecar rather than baking it into the primary
    manifest.  This keeps the primary manifest content-only and byte-identical
    across consecutive runs on identical input.
    """
    stat_result = path.stat()
    mtime_utc = datetime.fromtimestamp(stat_result.st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    content_meta: dict[str, Any] = {
        "sha256": hash_path(path),
        "size_bytes": stat_result.st_size,
    }
    return content_meta, mtime_utc


def _collect_files(
    root: Path,
    *,
    recursive: bool = True,
    mtime_map: dict[str, str] | None = None,
    exclude: Path | None = None,
    exclude_timing_sidecars: bool = False,
) -> list[dict[str, Any]]:
    """Return content-only file-metadata records for every regular file below *root*.

    Files are sorted by POSIX path for deterministic manifest output. Dot-
    files (``.*``) and temp-write artifacts (``*.tmp``) are skipped.

    Parameters
    ----------
    root:
        Directory to walk.
    recursive:
        If True, recurse into sub-directories.
    mtime_map:
        When provided, per-file ``mtime_utc`` strings are inserted into this
        dict (keyed by relative POSIX path) rather than into the returned
        records.  This keeps the returned records content-only.
    exclude:
        Optional single file path to skip (used to exclude the manifest file
        itself from the audit listing so consecutive runs are idempotent).
    exclude_timing_sidecars:
        When True, files whose names end with ``_timing.json`` are excluded
        from the content hash.  Timing sidecars carry wall-clock timestamps by
        design and must not enter the reproducible content manifest.
    """
    if not root.is_dir():
        return []
    records: list[dict[str, Any]] = []
    iterator = root.rglob("*") if recursive else root.iterdir()
    for entry in sorted(iterator):
        if not entry.is_file():
            continue
        if entry.name.startswith("."):
            continue
        if entry.suffix == ".tmp":
            continue
        if exclude is not None and entry.resolve() == exclude.resolve():
            continue
        if exclude_timing_sidecars and entry.name.endswith("_timing.json"):
            continue
        try:
            meta, mtime_utc = _file_metadata(entry)
        except OSError as exc:
            logger.warning("lineage: could not stat %s: %s", entry, exc)
            continue
        rel_path = str(entry.relative_to(root))
        meta["path"] = rel_path
        if mtime_map is not None:
            mtime_map[rel_path] = mtime_utc
        records.append(meta)
    return records


def _load_audit_counts(audit_path: Path) -> dict[str, Any] | None:
    """Return the audit payload at *audit_path*, or None if absent / malformed."""
    if not audit_path.is_file():
        return None
    try:
        parsed = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("lineage: could not read audit %s: %s", audit_path, exc)
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def emit_lineage_manifest(
    *,
    study_name: str,
    raw_datasets_dir: Path,
    raw_dictionary_dir: Path | None,
    raw_pdfs_dir: Path | None,
    llm_source_dir: Path,
    audit_dir: Path,
    pipeline_version: str,
    compliance_posture: str,
    manifest_path: Path,
    phi_key_fingerprint: str | None = None,
    run_id: str | None = None,
    runs_dir: Path | None = None,
) -> dict[str, Any]:
    """Assemble + atomically write the lineage manifest for this run.

    The primary manifest is content-only: it carries hashes and structural
    references but no timestamps.  When *run_id* and *runs_dir* are supplied,
    a ``lineage_timing.json`` sidecar is written to
    ``{runs_dir}/{run_id}/`` capturing ``generated_utc`` and a per-file
    ``mtime_utc`` map.  Callers that do not need the sidecar (e.g. tests that
    only assert on manifest structure) may omit both parameters.

    Returns the manifest payload (dict) so callers may log a summary.

    Zone guard: *manifest_path* is asserted against the output zone so a
    mis-configured audit dir fails fast.
    """
    from phi_engine.utils.run_context import write_lineage_timing_sidecar

    assert_output_zone(manifest_path.parent)

    # Collect mtime_utc into a side-channel dict rather than embedding in records.
    mtime_map: dict[str, str] = {}

    inputs = {
        "datasets": _collect_files(raw_datasets_dir, mtime_map=mtime_map),
    }
    if raw_dictionary_dir is not None:
        inputs["dictionary"] = _collect_files(raw_dictionary_dir, mtime_map=mtime_map)
    if raw_pdfs_dir is not None:
        inputs["pdfs"] = _collect_files(raw_pdfs_dir, mtime_map=mtime_map)

    outputs = {
        "llm_source": _collect_files(llm_source_dir, mtime_map=mtime_map),
        # Exclude the manifest itself so the audit listing is idempotent:
        # the manifest is written after collection, and a pre-existing one
        # from a prior run must not change the listing for the current run.
        "audit": _collect_files(
            audit_dir, recursive=False, mtime_map=mtime_map, exclude=manifest_path
        ),
        # Per-dataset PHI and cleanup ledgers live under audit/datasets/<stem>/.
        # Timing sidecars (*_timing.json) are excluded: they carry wall-clock
        # timestamps and must not enter the reproducible content hash.
        "audit_datasets": _collect_files(
            audit_dir / "datasets",
            recursive=True,
            mtime_map=mtime_map,
            exclude_timing_sidecars=True,
        ),
    }

    steps: dict[str, Any] = {}
    for leg, audit_filename in (
        ("phi_scrub", "phi_scrub_report.json"),
        ("dataset_cleanup", "dataset_cleanup_report.json"),
        ("dictionary_cleanup", "dictionary_cleanup_report.json"),
        ("pdfs_cleanup", "pdfs_cleanup_report.json"),
    ):
        payload = _load_audit_counts(audit_dir / audit_filename)
        if payload is None:
            continue
        steps[leg] = {
            "audit_file": audit_filename,
            "posture": payload.get("compliance_posture"),
            "event_count": len(payload.get("scrubbed", []))
            if isinstance(payload.get("scrubbed"), list)
            else None,
        }

    # Primary manifest: content-only, no timestamps.
    manifest: dict[str, Any] = {
        "study": study_name,
        "pipeline_version": pipeline_version,
        "compliance_posture": compliance_posture,
        "inputs": inputs,
        "outputs": outputs,
        "steps": steps,
    }
    # run_id is a stable identifier (not a timestamp) — safe for the
    # content-only manifest.  Lets an auditor tie this manifest to its run.
    if run_id is not None:
        manifest["run_id"] = run_id
    # The PHI key fingerprint (SHA-256 of the HMAC key bytes) lets an IRB
    # reviewer verify that the pseudonyms in llm_source/ were generated
    # with the claimed key — without exposing the key itself. Optional so
    # legacy callers without a key still emit a valid manifest.
    if phi_key_fingerprint is not None:
        manifest["phi_key_fingerprint"] = phi_key_fingerprint

    atomic_write_json(manifest_path, manifest)
    logger.info(
        "lineage manifest: %d input files, %d llm_source output files, %d dataset ledgers, %d steps",
        sum(len(v) for v in inputs.values()),
        len(outputs["llm_source"]),
        len(outputs["audit_datasets"]),
        len(steps),
    )

    # Write timing sidecar when run_id + runs_dir are provided.
    if run_id is not None and runs_dir is not None:
        generated_utc = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            write_lineage_timing_sidecar(
                runs_dir=runs_dir,
                run_id=run_id,
                study=study_name,
                generated_utc=generated_utc,
                mtime_utc=mtime_map,
            )
        except OSError as exc:
            logger.warning("lineage: could not write timing sidecar: %s", exc)

    return manifest
