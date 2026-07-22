"""Symlink-only intake for the standalone PHI pipeline."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import phi_engine.config.config as config
from phi_engine.pipeline.dependencies import is_artifact_id, is_sha256
from phi_engine.pipeline.intake_preflight import _iter_source_files

__all__ = ["intake_add", "load_intake_manifest"]

# Centralized legacy schema identifier and deprecation message. intake-manifest/v2
# is a temporary lifecycle marker ahead of a clean v3 cutover; keep this the single
# source of truth for both the schema string and the warning text so the two never
# drift out of sync.
_LEGACY_MANIFEST_SCHEMA = "intake-manifest/v2"
_LEGACY_MANIFEST_DEPRECATION_MESSAGE = (
    f"phi_engine.pipeline.intake.intake_add: {_LEGACY_MANIFEST_SCHEMA} is deprecated "
    "and will be replaced by a future manifest schema; this call path is scheduled "
    "for removal."
)

_HASH_CHUNK_SIZE = 1 << 20
_ALLOWED_MANIFEST_KEYS = {"study", "source_root", "entries", "duplicates", "errors", "removals", "schema"}
_REQUIRED_ENTRY_KEYS = {
    "artifact_id",
    "link_name",
    "relative_path",
    "original_path",
    "sha256",
    "size",
    "mtime_ns",
    "device",
    "inode",
    "mode",
}


def _empty_manifest(study: str) -> dict[str, Any]:
    return {"schema": _LEGACY_MANIFEST_SCHEMA, "study": study, "source_root": None, "entries": {}, "duplicates": [], "errors": [], "removals": []}


def _sha256_stream(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _new_artifact_id() -> str:
    return "a_" + uuid.uuid4().hex


def _safe_rel(path: Path) -> str:
    rel = path.as_posix()
    if rel.startswith("/") or rel == ".." or rel.startswith("../") or "/../" in rel:
        raise ValueError(f"unsafe relative path: {rel!r}")
    return rel


def _validate_manifest(study: str, manifest: dict[str, Any], manifest_path: Path, *, check_links: bool = True) -> dict[str, Any]:
    unknown = set(manifest) - _ALLOWED_MANIFEST_KEYS
    if unknown:
        raise ValueError(f"unknown intake manifest keys: {sorted(unknown)}")
    if manifest.get("study") != study:
        raise ValueError("intake manifest study mismatch")
    source_root_raw = manifest.get("source_root")
    source_root = Path(source_root_raw).resolve(strict=True) if source_root_raw else None
    entries = manifest.get("entries")
    if not isinstance(entries, dict):
        raise ValueError("intake manifest entries must be an object")
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for link_name, entry in entries.items():
        if not isinstance(entry, dict):
            raise ValueError("intake manifest entry must be an object")
        unknown_entry = set(entry) - _REQUIRED_ENTRY_KEYS
        if unknown_entry:
            raise ValueError(f"unknown intake entry keys: {sorted(unknown_entry)}")
        if entry.get("link_name") != link_name:
            raise ValueError("intake manifest link name mismatch")
        artifact_id = entry.get("artifact_id")
        if not is_artifact_id(artifact_id):
            raise ValueError("invalid artifact_id in intake manifest")
        if artifact_id in seen_ids:
            raise ValueError("duplicate artifact_id in intake manifest")
        seen_ids.add(artifact_id)
        rel = _safe_rel(Path(str(entry.get("relative_path"))))
        if rel in seen_paths:
            raise ValueError("duplicate relative_path in intake manifest")
        seen_paths.add(rel)
        if not is_sha256(entry.get("sha256")):
            raise ValueError("invalid sha256 in intake manifest")
        if check_links:
            link_path = manifest_path.parent / link_name
            if not link_path.is_symlink():
                raise ValueError(f"broken intake link: {link_name}")
            try:
                target = link_path.resolve(strict=True)
            except OSError as exc:
                raise ValueError(f"broken intake link: {link_name}") from exc
            if source_root is None:
                raise ValueError("intake manifest has entries without source_root")
            try:
                target.relative_to(source_root)
            except ValueError as exc:
                raise ValueError("intake link target outside source_root") from exc
            expected = (source_root / rel).resolve(strict=True)
            if target != expected:
                raise ValueError("intake link target/path mismatch")
    for list_key in ("duplicates", "errors", "removals"):
        if not isinstance(manifest.get(list_key, []), list):
            raise ValueError(f"intake manifest {list_key} must be a list")
    return manifest


def load_intake_manifest(study: str) -> dict[str, Any]:
    manifest_path = Path(config.INTAKE_DIR) / study / "intake_manifest.json"
    if not manifest_path.is_file():
        return _empty_manifest(study)
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"invalid intake manifest: {exc}") from exc
    return _validate_manifest(study, raw, manifest_path, check_links=True)


def _load_existing_for_reconcile(study: str) -> dict[str, Any]:
    manifest_path = Path(config.INTAKE_DIR) / study / "intake_manifest.json"
    if not manifest_path.is_file():
        return _empty_manifest(study)
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    return _validate_manifest(study, raw, manifest_path, check_links=False)


def intake_add(source: Path, study: str) -> dict[str, Any]:
    warnings.warn(_LEGACY_MANIFEST_DEPRECATION_MESSAGE, DeprecationWarning, stacklevel=2)
    source = Path(source).resolve(strict=True)
    if not source.is_dir():
        raise ValueError(f"source root must be a directory: {source}")
    study_dir = Path(config.INTAKE_DIR) / study
    study_dir.mkdir(parents=True, exist_ok=True)

    existing = _load_existing_for_reconcile(study)
    existing_root = existing.get("source_root")
    if existing_root is not None and Path(existing_root).resolve() != source:
        raise ValueError(f"intake source_root mismatch: expected {existing_root}, got {source}")

    existing_by_rel = {entry["relative_path"]: entry for entry in (existing.get("entries") or {}).values()}
    entries: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    removals: list[dict[str, Any]] = list(existing.get("removals") or [])
    seen_rel: set[str] = set()
    content_first: dict[str, str] = {}

    for src_file in sorted(_iter_source_files(source), key=lambda p: p.relative_to(source).as_posix()):
        rel = _safe_rel(src_file.relative_to(source))
        if src_file.is_symlink() and not src_file.exists():
            errors.append({"path": rel, "reason": "broken-symlink-in-source"})
            continue
        try:
            resolved = src_file.resolve(strict=True)
            resolved.relative_to(source)
            st = resolved.stat()
            if not stat.S_ISREG(st.st_mode):
                continue
            content_sha = _sha256_stream(resolved)
        except OSError as exc:
            errors.append({"path": rel, "reason": f"unreadable: {exc}"})
            continue
        except ValueError:
            errors.append({"path": rel, "reason": "source-target-outside-root"})
            continue

        prior = existing_by_rel.get(rel)
        artifact_id = prior["artifact_id"] if prior is not None else _new_artifact_id()
        link_name = f"{artifact_id}__{Path(rel).name}"
        link_path = study_dir / link_name
        try:
            if link_path.exists() or link_path.is_symlink():
                link_path.unlink()
            os.symlink(resolved, link_path)
        except OSError as exc:
            errors.append({"path": rel, "reason": f"symlink-failed: {exc}"})
            continue
        seen_rel.add(rel)
        duplicate_of = content_first.get(content_sha)
        entry = {
            "artifact_id": artifact_id,
            "link_name": link_name,
            "relative_path": rel,
            "original_path": str(resolved),
            "sha256": content_sha,
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
            "device": st.st_dev,
            "inode": st.st_ino,
            "mode": stat.S_IMODE(st.st_mode),
        }
        entries[link_name] = entry
        content_first.setdefault(content_sha, artifact_id)
        if duplicate_of and duplicate_of != artifact_id:
            # Alias artifacts stay first-class entries; this note is provenance only.
            pass

    removed_rels = sorted(set(existing_by_rel) - seen_rel)
    removed_link_names: set[str] = set()
    for rel in removed_rels:
        old = existing_by_rel[rel]
        removed_link_names.add(str(old["link_name"]))
        removals.append(
            {
                "artifact_id": old["artifact_id"],
                "relative_path": rel,
                "sha256": old.get("sha256"),
                "removed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )
    for link_path in study_dir.iterdir():
        if link_path.name == "intake_manifest.json":
            continue
        if link_path.name not in entries:
            link_path.unlink(missing_ok=True)

    manifest = {
        "schema": _LEGACY_MANIFEST_SCHEMA,
        "study": study,
        "source_root": str(source),
        "entries": entries,
        "duplicates": [],
        "errors": errors,
        "removals": removals,
    }
    manifest_path = study_dir / "intake_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    manifest_path.chmod(0o600)
    return manifest
