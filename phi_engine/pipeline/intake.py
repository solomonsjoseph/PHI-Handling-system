"""Symlink-only intake for the standalone PHI pipeline.

``intake_add`` NEVER copies, moves, or modifies source bytes: every file
under a study's intake tree is either an ``os.symlink`` pointing at the
resolved absolute source path, or the ``intake_manifest.json`` bookkeeping
file. Content is only ever opened for a streamed read (sha256 hashing) --
never for write, and the walk never deletes a source file.

This is the single ingestion door for the standalone pipeline: everything
under a project's own data tree (raw variables/datasets, PDFs, xlsx/xls/csv,
whatever else) is linked in here, unfiltered by extension -- the organizer
(``organize.py``) is what routes by file type, not intake.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import phi_engine.config.config as config
from phi_engine.utils._extraction_io.file_discovery import DEFAULT_JUNK_FILENAMES

__all__ = ["intake_add", "load_intake_manifest"]

_HASH_CHUNK_SIZE = 1 << 20  # 1 MiB streamed-read chunks


def _sha256_stream(path: Path) -> str:
    """Stream-hash *path*'s content. Read-only; never buffers the whole file."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha8_of_path(resolved_path: Path) -> str:
    """First 8 hex chars of sha256(resolved absolute path) -- deterministic,
    collision-safe for same-named files living in different source directories."""
    return hashlib.sha256(str(resolved_path).encode("utf-8")).hexdigest()[:8]


def _iter_source_files(source: Path):
    """Yield every non-hidden, non-junk entry under *source* (recursive).

    Directory symlinks are NOT followed (avoids intake loops / escaping the
    declared source root); a dangling file symlink IS yielded (so the caller
    can record it under ``errors``) because ``os.walk`` classifies it by
    ``lstat``, not by whether it resolves.
    """
    for root, dirnames, filenames in os.walk(source, followlinks=False):
        root_path = Path(root)
        dirnames[:] = [
            d for d in dirnames if not d.startswith(".") and d not in DEFAULT_JUNK_FILENAMES
        ]
        for name in filenames:
            if name.startswith(".") or name in DEFAULT_JUNK_FILENAMES:
                continue
            yield root_path / name


def load_intake_manifest(study: str) -> dict[str, Any]:
    """Return the current ``intake_manifest.json`` for *study*, or an empty shell."""
    manifest_path = Path(config.INTAKE_DIR) / study / "intake_manifest.json"
    if not manifest_path.is_file():
        return {"study": study, "source_root": None, "entries": {}, "duplicates": [], "errors": []}
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"study": study, "source_root": None, "entries": {}, "duplicates": [], "errors": []}


def intake_add(source: Path, study: str) -> dict[str, Any]:
    """Symlink every file under *source* into ``INTAKE_DIR/<study>/``.

    Idempotent: a link already pointing at the same resolved target is left
    untouched. Content-duplicate files (same sha256, different source paths)
    link only the first occurrence and record the rest under ``duplicates``.
    Unreadable files and dangling symlinks found while walking *source* are
    recorded under ``errors``; the walk always continues (never raises for a
    single bad entry).

    Hard rule: this function never opens a SOURCE file for write, never
    copies bytes (hashing is a streamed read), and never deletes anything
    under *source*. Only the intake-side symlink (and the manifest file) are
    ever created/replaced.
    """
    source = Path(source).resolve()
    study_dir = Path(config.INTAKE_DIR) / study
    study_dir.mkdir(parents=True, exist_ok=True)

    existing = load_intake_manifest(study)
    entries: dict[str, dict[str, Any]] = dict(existing.get("entries") or {})
    duplicates: list[dict[str, Any]] = list(existing.get("duplicates") or [])
    seen_content_hashes: dict[str, str] = {
        e["sha256"]: name for name, e in entries.items() if "sha256" in e
    }

    errors: list[dict[str, Any]] = []

    for src_file in _iter_source_files(source):
        if src_file.is_symlink() and not src_file.exists():
            errors.append({"path": str(src_file), "reason": "broken-symlink-in-source"})
            continue
        try:
            if not src_file.is_file():
                continue
            resolved = src_file.resolve()
        except OSError as exc:
            errors.append({"path": str(src_file), "reason": f"unreadable: {exc}"})
            continue

        try:
            content_sha = _sha256_stream(resolved)
        except OSError as exc:
            errors.append({"path": str(src_file), "reason": f"unreadable: {exc}"})
            continue

        sha8 = _sha8_of_path(resolved)
        link_name = f"{sha8}__{resolved.name}"
        link_path = study_dir / link_name

        prior_link_for_content = seen_content_hashes.get(content_sha)
        if prior_link_for_content is not None and prior_link_for_content != link_name:
            dup_record = {
                "path": str(resolved),
                "sha256": content_sha,
                "duplicate_of": prior_link_for_content,
            }
            if dup_record not in duplicates:
                duplicates.append(dup_record)
            continue

        if link_path.is_symlink():
            try:
                if link_path.resolve() == resolved:
                    seen_content_hashes.setdefault(content_sha, link_name)
                    continue  # idempotent -- already linked to this exact target
            except OSError:
                pass  # stale/broken intake-side link -- fall through and relink

        try:
            if link_path.exists() or link_path.is_symlink():
                link_path.unlink()
            os.symlink(resolved, link_path)
        except OSError as exc:
            errors.append({"path": str(resolved), "reason": f"symlink-failed: {exc}"})
            continue

        stat = resolved.stat()
        entries[link_name] = {
            "link_name": link_name,
            "original_path": str(resolved),
            "sha256": content_sha,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        }
        seen_content_hashes.setdefault(content_sha, link_name)

    manifest = {
        "study": study,
        "source_root": str(source),
        "entries": entries,
        "duplicates": duplicates,
        "errors": errors,
    }
    manifest_path = study_dir / "intake_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest
