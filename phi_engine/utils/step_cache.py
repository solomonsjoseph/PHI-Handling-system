"""Pipeline step caching for fast incremental re-runs.

This module provides file-based caching so that each pipeline step can be
skipped when its outputs already exist **and** its inputs have not changed
since the last successful run.

How it works:

1. Before running a step, ``is_step_fresh()`` checks for a manifest file
   (``.<step_name>.manifest.json``) stored inside the step's output
   directory.  The manifest records:
   - SHA-256 content hashes of every input file
   - Artifact version strings that were current when the step ran
   - A timestamp for human convenience

2. If the manifest exists and every recorded input hash still matches the
   file on disk (and artifact versions haven't changed), the step is
   considered *fresh* and can be skipped.

3. After a step completes successfully, ``save_step_manifest()`` writes a
   new manifest capturing the current state of its inputs.

This gives deterministic, content-based cache invalidation with no need for
external databases or lock files.

Design rules:
- Pure-function hashing: only file contents matter, not timestamps.
- Manifests are hidden dotfiles so they don't pollute ``ls`` output.
- ``--force`` in the CLI always bypasses the cache.
- Missing output directories always mean "not fresh".
- If the manifest itself is corrupt or missing, the step runs.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from datetime import datetime, timezone
UTC = timezone.utc  # py3.9 compat
from pathlib import Path
from typing import Any

from phi_engine.utils.logging_system import get_logger

from .integrity import hash_file

__all__ = [
    "MANIFEST_VERSION",
    "hash_directory",
    "hash_file",
    "is_step_fresh",
    "save_step_manifest",
]

logger = get_logger(__name__)

MANIFEST_VERSION = "1.0.0"
"""Schema version of the manifest file itself."""

_BUF_SIZE = 1 << 16  # 64 KiB read buffer for hashing


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------


def hash_directory(
    directory: Path,
    *,
    extensions: frozenset[str] | None = None,
) -> dict[str, str]:
    """Recursively SHA-256 every file in *directory* and return ``{rel_path: hex}``.

    Hidden files, ``__pycache__`` dirs, and ``.pyc`` files are excluded;
    *extensions* (if given) restricts the walk to those suffixes.
    """
    if not directory.is_dir():
        return {}

    hashes: dict[str, str] = {}
    for fpath in directory.rglob("*"):
        if not fpath.is_file():
            continue

        # Check path parts relative to ensure we skip hidden dirs and pycache
        rel = str(fpath.relative_to(directory))
        parts = Path(rel).parts
        if any(p.startswith(".") or p == "__pycache__" for p in parts):
            continue

        if fpath.name.startswith(".") or fpath.name.endswith(".pyc"):
            continue

        if extensions and fpath.suffix not in extensions:
            continue

        hashes[rel] = hash_file(fpath)

    return dict(sorted(hashes.items()))


# ---------------------------------------------------------------------------
# Manifest I/O
# ---------------------------------------------------------------------------


def _manifest_path(output_dir: Path, step_name: str) -> Path:
    """Return the hidden manifest file path for a given step."""
    return output_dir / f".{step_name}.manifest.json"


def save_step_manifest(
    step_name: str,
    output_dir: Path,
    input_hashes: dict[str, str],
    *,
    artifact_versions: dict[str, str] | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> Path:
    """Persist a cache manifest after a successful step run.

    Args:
        step_name: Short identifier for the pipeline step (e.g. ``"dictionary"``).
        output_dir: Directory where the step wrote its outputs.
        input_hashes: ``{relative_path: sha256}`` of every input file.
        artifact_versions: Optional artifact version strings to record.
        extra_metadata: Optional extra data to store (e.g. counts, flags).

    Returns:
        Path to the written manifest file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    mpath = _manifest_path(output_dir, step_name)

    payload: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "step_name": step_name,
        "completed_at": datetime.now(UTC).isoformat(),
        "input_hashes": input_hashes,
    }
    if artifact_versions:
        payload["artifact_versions"] = artifact_versions
    if extra_metadata:
        payload["extra"] = extra_metadata

    # Atomic write to avoid partial manifest files on crash/interruption
    mpath.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=mpath.parent, delete=False, encoding="utf-8") as tmp:
        json.dump(payload, tmp, indent=2, ensure_ascii=False, sort_keys=True)
        tmp.write("\n")
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_name = tmp.name

    try:
        os.replace(tmp_name, mpath)
    except Exception:
        with contextlib.suppress(OSError):
            os.remove(tmp_name)
        raise

    logger.debug("Step cache manifest saved: %s", mpath)
    return mpath


def is_step_fresh(
    step_name: str,
    output_dir: Path,
    current_input_hashes: dict[str, str],
    *,
    artifact_versions: dict[str, str] | None = None,
    required_outputs: list[str] | None = None,
) -> bool:
    """Check whether a pipeline step can be skipped.

    A step is *fresh* when ALL of the following hold:

    1. The output directory exists.
    2. A valid manifest file exists inside it.
    3. Every input file hash in the manifest matches the current hash.
    4. No new input files have appeared that weren't in the manifest.
    5. If *artifact_versions* is provided, every recorded version matches.
    6. If *required_outputs* is provided, each named file exists under
       *output_dir*.

    Args:
        step_name: Pipeline step identifier.
        output_dir: Directory where the step writes outputs.
        current_input_hashes: Live hashes of current input files.
        artifact_versions: If provided, must match recorded versions.
        required_outputs: Optional list of filenames/globs that must exist
            under *output_dir* for the step to be considered complete.

    Returns:
        ``True`` if the step is fresh and can be safely skipped.
    """
    mpath = _manifest_path(output_dir, step_name)

    # ── 1. Output dir and manifest must exist ──
    if not output_dir.is_dir():
        logger.debug("Cache miss [%s]: output dir does not exist", step_name)
        return False

    if not mpath.is_file():
        logger.debug("Cache miss [%s]: manifest not found", step_name)
        return False

    # ── 2. Parse manifest ──
    try:
        with open(mpath, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("Cache miss [%s]: manifest corrupt: %s", step_name, exc)
        return False

    if not isinstance(manifest, dict):
        logger.debug("Cache miss [%s]: manifest is not a JSON object", step_name)
        return False

    recorded_hashes = manifest.get("input_hashes", {})
    if not isinstance(recorded_hashes, dict):
        logger.debug("Cache miss [%s]: input_hashes not a dict", step_name)
        return False

    # ── 3. Compare input hashes (both directions) ──
    if set(recorded_hashes.keys()) != set(current_input_hashes.keys()):
        added = set(current_input_hashes.keys()) - set(recorded_hashes.keys())
        removed = set(recorded_hashes.keys()) - set(current_input_hashes.keys())
        logger.debug(
            "Cache miss [%s]: input file set changed (added=%s, removed=%s)",
            step_name,
            added or "∅",
            removed or "∅",
        )
        return False

    for rel_path, current_hash in current_input_hashes.items():
        if recorded_hashes.get(rel_path) != current_hash:
            logger.debug("Cache miss [%s]: hash mismatch for %s", step_name, rel_path)
            return False

    # ── 4. Artifact version check ──
    if artifact_versions:
        recorded_versions = manifest.get("artifact_versions", {})
        for key, expected in artifact_versions.items():
            if recorded_versions.get(key) != expected:
                logger.debug(
                    "Cache miss [%s]: artifact version changed: %s (%s → %s)",
                    step_name,
                    key,
                    recorded_versions.get(key),
                    expected,
                )
                return False

    # ── 5. Required output files ──
    if required_outputs:
        for name in required_outputs:
            if not (output_dir / name).exists():
                logger.debug("Cache miss [%s]: required output missing: %s", step_name, name)
                return False

    logger.debug("Cache hit [%s]: step is fresh, skipping", step_name)
    return True
