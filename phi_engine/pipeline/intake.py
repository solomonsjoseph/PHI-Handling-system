"""Symlink-only intake for the standalone PHI pipeline (intake-manifest/v3).

Clean v3 cutover: no v2 schema, no deprecation shim, no legacy reader. Every
workspace path (INTAKE_DIR, OUTPUT_DIR, and everything beneath them) is
treated as hostile -- opened/created descriptor-relatively with
``O_NOFOLLOW``, verified to be a private ``0700`` directory (or a ``0600``
regular file for the manifest/review-note leaves), before it is ever
written to. ``pipeline_lock._create_dir_ancestry`` walks every ancestor
segment from the filesystem root by directory descriptor (never a
pathname-based ``mkdir(parents=True)``), so a symlinked/reparse-point
ancestor anywhere above a workspace directory fails closed instead of
being silently followed. Every source path is treated as hostile too:
intake never copies, moves, writes, chmods, or deletes a source artifact
-- it only creates symlinks that point at a descriptor-verified original,
after re-opening each candidate through :func:`open_verified_source` with
its preflight-computed identity immediately before the symlink is
created.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, NamedTuple

import phi_engine.config.config as config
import phi_engine.pipeline.intake_naming as intake_naming
import phi_engine.pipeline.intake_preflight as intake_preflight
from phi_engine.audit.review_paths import safe_review_slug
from phi_engine.pipeline.dependencies import is_artifact_id, is_sha256, is_timestamp_z, utc_now_z
from phi_engine.pipeline.intake_preflight import IntakeCandidate, IntakePreflight
from phi_engine.pipeline.verified_source import VerifiedSourceError, open_verified_source
from phi_engine.utils import pipeline_lock

__all__ = ["IntakeManifestError", "IntakeNotReadyError", "intake_add", "load_intake_manifest"]


class IntakeManifestError(Exception):
    """Typed, value-free intake-manifest failure. ``code`` is a fixed
    reason string; never raw path/content/exception text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class IntakeNotReadyError(Exception):
    """Typed, value-free failure: intake status is not ``ready``. ``status``
    is the fixed manifest status string. Reserved for downstream callers
    (organize/run) that gate on a completed intake."""

    def __init__(self, status: str) -> None:
        self.status = status
        super().__init__(status)


_MANIFEST_SCHEMA = "intake-manifest/v3"
_MANIFEST_FILENAME = "intake_manifest.json"

_ALLOWED_MANIFEST_KEYS = {
    "schema",
    "study",
    "study_name_source",
    "status",
    "source_root",
    "entries",
    "review_items",
    "errors",
    "removals",
}
_ENTRY_KEYS = {
    "artifact_id",
    "intake_path",
    "component",
    "relative_path",
    "original_path",
    "sha256",
    "size",
    "mtime_ns",
    "device",
    "inode",
    "mode",
}
_COMPONENTS = frozenset({"datasets", "forms", "data_dictionary", "mappings", "_unclassified"})
_STUDY_NAME_SOURCES = frozenset({"user", "ai", "generated"})
_STATUSES = frozenset({"ready", "review_required", "failed"})
_REVIEW_REQUIRED_KEYS = {"path", "reason", "blocking"}
_REVIEW_OPTIONAL_KEYS = {"artifact_id", "detail", "candidates"}
_ERROR_REQUIRED_KEYS = {"path", "reason"}
_ERROR_OPTIONAL_KEYS = {"detail"}
_REMOVAL_KEYS = {"artifact_id", "relative_path", "sha256", "removed_at"}

_REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_ROOT_PATH = ""  # fixed sentinel path for whole-source-root review/error records

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_OPEN_FLAGS = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC


# --- small pure predicates ---------------------------------------------------------------


def _is_safe_relative_posix(value: object) -> bool:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value or "\x00" in value:
        return False
    segments = value.split("/")
    return not any(segment in ("", ".", "..") for segment in segments)


def _is_reason_code(value: object) -> bool:
    return isinstance(value, str) and bool(_REASON_CODE_RE.fullmatch(value))


def _is_canonical_absolute_dir(value: object) -> bool:
    """Lexically canonical absolute directory: starts with ``/``, no
    trailing slash (except the root itself), and equal to its own
    ``os.path.normpath`` -- rejects ``..``, ``.``, and duplicate
    separators without ever touching the filesystem."""
    if not isinstance(value, str) or not value.startswith("/"):
        return False
    if value != "/" and value.endswith("/"):
        return False
    return os.path.normpath(value) == value


def _canonical_original_path(source_root: str, relative_path: str) -> str:
    """Canonical POSIX join of a canonical ``source_root`` (see
    :func:`_is_canonical_absolute_dir`) and a safe relative path.
    ``source_root`` is never empty and never ends in ``/`` except for
    the filesystem root itself, so a plain ``f"{source_root}/{rel}"``
    interpolation doubles the separator exactly when ``source_root ==
    "/"`` -- the one case this handles explicitly."""
    if source_root == "/":
        return f"/{relative_path}"
    return f"{source_root}/{relative_path}"


def _new_artifact_id() -> str:
    return "a_" + secrets.token_hex(16)


def _compute_intake_path(relative_path: str, component: str, artifact_id: str) -> str:
    parts = relative_path.split("/")
    basename = parts[-1]
    link_name = f"{artifact_id}__{basename}"
    if component == "_unclassified":
        parent_parts = parts[:-1]
        prefix = "_unclassified"
    else:
        parent_parts = parts[1:-1]
        prefix = component
    if parent_parts:
        return "/".join([prefix, *parent_parts, link_name])
    return f"{prefix}/{link_name}"


def _split_intake_path(intake_path: str) -> tuple[tuple[str, ...], str]:
    parts = intake_path.split("/")
    return tuple(parts[:-1]), parts[-1]


# --- descriptor-relative, hostile-workspace-safe directory primitives ---------------------


def _open_workspace_root_creating(path: Path) -> int:
    """Ensure ``path`` exists via :func:`pipeline_lock._create_dir_ancestry`
    (descriptor-walked from the filesystem root, never a pathname-based
    ``mkdir(parents=True)``) then open, verify, and force it to a private
    ``0700`` directory. This is the tool-owned root (``INTAKE_DIR``/
    ``OUTPUT_DIR``); everything *beneath* it is additionally walked
    NOFOLLOW per segment by :func:`_open_dir_creating`."""
    try:
        fd = pipeline_lock._create_dir_ancestry(path)
        if fd is None:
            fd = os.open(path, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC)
    except OSError:
        raise IntakeManifestError("intake-tree-unsafe") from None
    try:
        _verify_and_lock_down_dir(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_workspace_root_readonly(path: Path) -> int | None:
    try:
        fd = pipeline_lock._read_dir_ancestry(path)
    except OSError:
        raise IntakeManifestError("intake_manifest_invalid") from None
    if fd is None:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise IntakeManifestError("intake_manifest_invalid")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _verify_and_lock_down_dir(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise IntakeManifestError("intake-tree-unsafe")
    if not hasattr(os, "fchmod"):
        raise IntakeManifestError("intake-tree-unsafe")
    os.fchmod(fd, 0o700)
    info = os.fstat(fd)
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise IntakeManifestError("intake-tree-unsafe")


def _open_dir_creating(parent_fd: int, name: str) -> tuple[int, bool]:
    """Open (creating if absent) a single path SEGMENT directly under
    ``parent_fd``, NOFOLLOW, requiring it to be (or become) a private
    ``0700`` directory. A pre-existing symlink/reparse point or any other
    unexpected node type fails closed with ``intake-tree-unsafe``; never
    followed, never replaced. Returns ``(fd, created)`` where ``created``
    is ``True`` only when THIS call actually made the directory (not
    when it already existed), so callers can journal exactly what they
    themselves are responsible for undoing."""
    created = False
    for _attempt in range(2):
        try:
            fd = os.open(name, _DIR_OPEN_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            except OSError:
                raise IntakeManifestError("intake-tree-unsafe") from None
            created = True
            continue
        except OSError:
            raise IntakeManifestError("intake-tree-unsafe") from None
        try:
            _verify_and_lock_down_dir(fd)
        except BaseException:
            os.close(fd)
            raise
        return fd, created
    raise IntakeManifestError("intake-tree-unsafe")


def _open_existing_dir_strict(parent_fd: int, name: str) -> int | None:
    """Open an EXISTING directory segment NOFOLLOW. ``None`` if absent
    (caller decides what that means); raises ``intake-tree-unsafe`` for a
    symlink/reparse point or any other unexpected node -- never mkdir's,
    never follows, never replaces."""
    try:
        fd = os.open(name, _DIR_OPEN_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError:
        raise IntakeManifestError("intake-tree-unsafe") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise IntakeManifestError("intake-tree-unsafe")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_study_dir_creating(parent_fd: int, name: str) -> tuple[int, bool]:
    """Like :func:`_open_dir_creating` but reports the SAME kind of
    created-fresh flag for the study directory itself. Caller MUST
    already hold every lock that makes this race-free -- ``intake_add``
    holds the registry lock across its entire body, so no other caller
    can create ``name`` between the existence probe and the create."""
    existing = _open_existing_dir_strict(parent_fd, name)
    if existing is not None:
        return existing, False
    fd, _created = _open_dir_creating(parent_fd, name)
    return fd, True


def _descend(
    parent_fd: int,
    parts: tuple[str, ...],
    *,
    create: bool,
    on_created: Callable[[tuple[str, ...]], None] | None = None,
) -> int | None:
    """Return an owned fd for the directory chain ``parts`` relative to
    ``parent_fd``. ``create=True`` creates missing 0700 segments
    (``intake-tree-unsafe`` on any unsafe node), invoking ``on_created``
    with the cumulative path tuple for each segment THIS call actually
    created (so a caller can journal it for rollback); ``create=False``
    requires every segment to already exist as a verified real
    directory, returning ``None`` (not raising) the moment any segment
    in the chain is simply absent."""
    current = parent_fd
    owns_current = False
    walked: list[str] = []
    try:
        for part in parts:
            walked.append(part)
            if create:
                nxt, created = _open_dir_creating(current, part)
                if created and on_created is not None:
                    on_created(tuple(walked))
            else:
                nxt = _open_existing_dir_strict(current, part)
                if nxt is None:
                    return None
            if owns_current:
                os.close(current)
            current = nxt
            owns_current = True
        return current if owns_current else os.dup(current)
    except BaseException:
        if owns_current:
            os.close(current)
        raise


# --- atomic same-directory writes ----------------------------------------------------------


def _atomic_write_in_dir(dir_fd: int, filename: str, payload: bytes, mode: int) -> None:
    if os.rename not in os.supports_dir_fd or os.open not in os.supports_dir_fd:
        raise IntakeManifestError("intake-tree-unsafe")
    temp_name = f".{filename}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC
    fd = os.open(temp_name, flags, mode, dir_fd=dir_fd)
    try:
        try:
            written = 0
            while written < len(payload):
                written += os.write(fd, payload[written:])
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(temp_name, filename, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name, dir_fd=dir_fd)
        raise
    os.fsync(dir_fd)


def _read_regular_file_bytes(dir_fd: int, filename: str) -> bytes | None:
    """Descriptor-relative read of an EXISTING private regular file.
    ``None`` if absent; ``intake-tree-unsafe`` for a symlink or any
    other unexpected node type. Shared core for the manifest and review-
    note "prior bytes" snapshots the transactional rollback restores."""
    flags = os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC
    try:
        fd = os.open(filename, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError:
        raise IntakeManifestError("intake-tree-unsafe") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise IntakeManifestError("intake-tree-unsafe")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _read_manifest_json(study_fd: int) -> Any:
    try:
        raw = _read_regular_file_bytes(study_fd, _MANIFEST_FILENAME)
    except IntakeManifestError:
        raise IntakeManifestError("intake_manifest_invalid") from None
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise IntakeManifestError("intake_manifest_invalid") from None


def _read_manifest_bytes(study_fd: int) -> bytes | None:
    """Raw pre-mutation snapshot of the manifest file, or ``None`` if
    none existed yet. Used only to restore exact prior bytes on a failed
    reconcile attempt -- never parsed, never trusted as valid content."""
    with contextlib.suppress(IntakeManifestError):
        return _read_regular_file_bytes(study_fd, _MANIFEST_FILENAME)
    return None


def _restore_manifest_bytes(study_fd: int, prior_bytes: bytes | None) -> None:
    """Best-effort: never raises -- a rollback step failing must not
    mask the original error driving it."""
    with contextlib.suppress(Exception):
        if prior_bytes is None:
            with contextlib.suppress(OSError):
                os.unlink(_MANIFEST_FILENAME, dir_fd=study_fd)
        else:
            _atomic_write_in_dir(study_fd, _MANIFEST_FILENAME, prior_bytes, 0o600)


# --- v3 schema validation (pure; no I/O) ----------------------------------------------------


def _validate_entry(
    intake_path: str,
    entry: Any,
    source_root: str,
    seen_ids: set[str],
    seen_rel: set[str],
    seen_intake: set[str],
) -> dict[str, Any]:
    if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
        raise IntakeManifestError("intake_manifest_invalid")
    if not _is_safe_relative_posix(intake_path) or entry.get("intake_path") != intake_path:
        raise IntakeManifestError("intake_manifest_invalid")
    if intake_path in seen_intake:
        raise IntakeManifestError("intake_manifest_invalid")
    seen_intake.add(intake_path)

    artifact_id = entry.get("artifact_id")
    if not is_artifact_id(artifact_id) or artifact_id in seen_ids:
        raise IntakeManifestError("intake_manifest_invalid")
    seen_ids.add(artifact_id)

    component = entry.get("component")
    if component not in _COMPONENTS:
        raise IntakeManifestError("intake_manifest_invalid")

    relative_path = entry.get("relative_path")
    if not _is_safe_relative_posix(relative_path) or relative_path in seen_rel:
        raise IntakeManifestError("intake_manifest_invalid")
    if component != "_unclassified" and relative_path.split("/", 1)[0] != component:
        raise IntakeManifestError("intake_manifest_invalid")
    seen_rel.add(relative_path)

    if intake_path != _compute_intake_path(relative_path, component, artifact_id):
        raise IntakeManifestError("intake_manifest_invalid")
    if entry.get("original_path") != _canonical_original_path(source_root, relative_path):
        raise IntakeManifestError("intake_manifest_invalid")
    if not is_sha256(entry.get("sha256")):
        raise IntakeManifestError("intake_manifest_invalid")
    for field in ("size", "mtime_ns", "device", "inode", "mode"):
        value = entry.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise IntakeManifestError("intake_manifest_invalid")
    if entry["mode"] > 0o7777:
        raise IntakeManifestError("intake_manifest_invalid")
    return dict(entry)


def _validate_review_items(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise IntakeManifestError("intake_manifest_invalid")
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise IntakeManifestError("intake_manifest_invalid")
        keys = set(item)
        if not _REVIEW_REQUIRED_KEYS.issubset(keys) or not keys.issubset(_REVIEW_REQUIRED_KEYS | _REVIEW_OPTIONAL_KEYS):
            raise IntakeManifestError("intake_manifest_invalid")
        path = item.get("path")
        if not isinstance(path, str) or (path != _ROOT_PATH and not _is_safe_relative_posix(path)):
            raise IntakeManifestError("intake_manifest_invalid")
        reason = item.get("reason")
        if not _is_reason_code(reason):
            raise IntakeManifestError("intake_manifest_invalid")
        if item.get("blocking") is not True:
            raise IntakeManifestError("intake_manifest_invalid")
        if "artifact_id" in item and not is_artifact_id(item["artifact_id"]):
            raise IntakeManifestError("intake_manifest_invalid")
        if "detail" in item and not _is_reason_code(item["detail"]):
            raise IntakeManifestError("intake_manifest_invalid")
        if "candidates" in item:
            if reason != "study-name-conflict":
                raise IntakeManifestError("intake_manifest_invalid")
            candidates = item["candidates"]
            if not isinstance(candidates, dict) or set(candidates) != {"forms", "dictionary_mapping"}:
                raise IntakeManifestError("intake_manifest_invalid")
            for value in candidates.values():
                if not isinstance(value, str) or value != safe_review_slug(value)[:128]:
                    raise IntakeManifestError("intake_manifest_invalid")
                try:
                    pipeline_lock.lock_path_for(value)
                except ValueError:
                    raise IntakeManifestError("intake_manifest_invalid") from None
        result.append(dict(item))
    return result


def _validate_errors(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise IntakeManifestError("intake_manifest_invalid")
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise IntakeManifestError("intake_manifest_invalid")
        keys = set(item)
        if not _ERROR_REQUIRED_KEYS.issubset(keys) or not keys.issubset(_ERROR_REQUIRED_KEYS | _ERROR_OPTIONAL_KEYS):
            raise IntakeManifestError("intake_manifest_invalid")
        path = item.get("path")
        if path is not None and (not isinstance(path, str) or (path != _ROOT_PATH and not _is_safe_relative_posix(path))):
            raise IntakeManifestError("intake_manifest_invalid")
        if not _is_reason_code(item.get("reason")):
            raise IntakeManifestError("intake_manifest_invalid")
        if "detail" in item and not _is_reason_code(item["detail"]):
            raise IntakeManifestError("intake_manifest_invalid")
        result.append(dict(item))
    return result


def _validate_removals(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise IntakeManifestError("intake_manifest_invalid")
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != _REMOVAL_KEYS:
            raise IntakeManifestError("intake_manifest_invalid")
        if not is_artifact_id(item.get("artifact_id")):
            raise IntakeManifestError("intake_manifest_invalid")
        if not _is_safe_relative_posix(item.get("relative_path")):
            raise IntakeManifestError("intake_manifest_invalid")
        if not is_sha256(item.get("sha256")):
            raise IntakeManifestError("intake_manifest_invalid")
        if not is_timestamp_z(item.get("removed_at")):
            raise IntakeManifestError("intake_manifest_invalid")
        result.append(dict(item))
    return result


def _validate_manifest_v3(raw: Any, *, expect_study: str | None) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != _ALLOWED_MANIFEST_KEYS:
        raise IntakeManifestError("intake_manifest_invalid")
    if raw.get("schema") != _MANIFEST_SCHEMA:
        raise IntakeManifestError("intake_manifest_invalid")

    study = raw.get("study")
    if not isinstance(study, str):
        raise IntakeManifestError("intake_manifest_invalid")
    try:
        pipeline_lock.lock_path_for(study)
    except ValueError:
        raise IntakeManifestError("intake_manifest_invalid") from None
    if expect_study is not None and study != expect_study:
        raise IntakeManifestError("intake_manifest_invalid")

    if raw.get("study_name_source") not in _STUDY_NAME_SOURCES:
        raise IntakeManifestError("intake_manifest_invalid")
    if raw.get("status") not in _STATUSES:
        raise IntakeManifestError("intake_manifest_invalid")

    source_root = raw.get("source_root")
    if not _is_canonical_absolute_dir(source_root):
        raise IntakeManifestError("intake_manifest_invalid")

    entries = raw.get("entries")
    if not isinstance(entries, dict):
        raise IntakeManifestError("intake_manifest_invalid")
    seen_ids: set[str] = set()
    seen_rel: set[str] = set()
    seen_intake: set[str] = set()
    validated_entries = {
        intake_path: _validate_entry(intake_path, entry, source_root, seen_ids, seen_rel, seen_intake)
        for intake_path, entry in entries.items()
    }

    review_items = _validate_review_items(raw.get("review_items"))
    errors = _validate_errors(raw.get("errors"))
    removals = _validate_removals(raw.get("removals"))

    expected_status = "failed" if errors else ("review_required" if review_items else "ready")
    if raw["status"] != expected_status:
        raise IntakeManifestError("intake_manifest_invalid")

    return {
        "schema": _MANIFEST_SCHEMA,
        "study": study,
        "study_name_source": raw["study_name_source"],
        "status": raw["status"],
        "source_root": source_root,
        "entries": validated_entries,
        "review_items": review_items,
        "errors": errors,
        "removals": removals,
    }


def _empty_manifest_v3() -> dict[str, Any]:
    return {
        "schema": _MANIFEST_SCHEMA,
        "study": None,
        "study_name_source": None,
        "status": None,
        "source_root": None,
        "entries": {},
        "review_items": [],
        "errors": [],
        "removals": [],
    }


def _verify_entry_links(study_fd: int, manifest: dict[str, Any]) -> None:
    """Descriptor-relative liveness proof for every recorded symlink: walked
    fresh right now (never trusted from load time), required to be exactly
    the expected symlink pointing at the recorded ``original_path``."""
    for intake_path, entry in manifest["entries"].items():
        parts, basename = _split_intake_path(intake_path)
        try:
            parent_fd = _descend(study_fd, parts, create=False)
        except IntakeManifestError:
            raise IntakeManifestError("intake_manifest_invalid") from None
        if parent_fd is None:
            raise IntakeManifestError("intake_manifest_invalid")
        try:
            try:
                info = os.lstat(basename, dir_fd=parent_fd)
            except OSError:
                raise IntakeManifestError("intake_manifest_invalid") from None
            if not stat.S_ISLNK(info.st_mode):
                raise IntakeManifestError("intake_manifest_invalid")
            try:
                target = os.readlink(basename, dir_fd=parent_fd)
            except OSError:
                raise IntakeManifestError("intake_manifest_invalid") from None
            if target != entry["original_path"]:
                raise IntakeManifestError("intake_manifest_invalid")
        finally:
            os.close(parent_fd)


# --- public read path ----------------------------------------------------------------------


def _open_and_read_manifest(study: str, *, verify_links: bool) -> dict[str, Any]:
    pipeline_lock.lock_path_for(study)  # validates plain-name; ValueError on a caller bug

    intake_root_fd = _open_workspace_root_readonly(Path(config.INTAKE_DIR))
    if intake_root_fd is None:
        raise IntakeManifestError("intake_manifest_missing")
    try:
        try:
            study_fd = _open_existing_dir_strict(intake_root_fd, study)
        except IntakeManifestError:
            raise IntakeManifestError("intake_manifest_invalid") from None
        if study_fd is None:
            raise IntakeManifestError("intake_manifest_missing")
        try:
            raw = _read_manifest_json(study_fd)
            if raw is None:
                raise IntakeManifestError("intake_manifest_missing")
            manifest = _validate_manifest_v3(raw, expect_study=study)
            if verify_links:
                _verify_entry_links(study_fd, manifest)
                if _inventory_unexpected_nodes(study_fd, set(manifest["entries"])):
                    raise IntakeManifestError("intake_manifest_invalid")
            return manifest
        finally:
            os.close(study_fd)
    finally:
        os.close(intake_root_fd)


def load_intake_manifest(study: str) -> dict[str, Any]:
    """Load and fully validate ``study``'s intake-manifest/v3, re-verifying
    every recorded symlink live. Never returns a synthetic empty manifest:
    raises :class:`IntakeManifestError` with a fixed code instead."""
    return _open_and_read_manifest(study, verify_links=True)


def _load_manifest_schema_only(study: str) -> dict[str, Any]:
    """Schema-only variant of :func:`load_intake_manifest` -- opens and
    validates the manifest structure WITHOUT the live entry-symlink
    liveness check. Used by the registry scan and placement decision,
    which only need ``study_name_source``/``source_root``/``status`` for
    a sibling study and must never be blocked by that study's OWN stale
    links -- reconciliation (this same call, if that study turns out to
    be the chosen destination) detects and reports those on its own."""
    return _open_and_read_manifest(study, verify_links=False)


# --- registry scan / reuse / promotion -------------------------------------------------------


def _scan_generated_manifests_for_source(canonical_source: str) -> list[str]:
    """Every study whose v3 manifest is ``study_name_source == "generated"``
    and whose ``source_root`` canonically matches. Every sibling under
    ``INTAKE_DIR`` is treated as hostile: a symlink/reparse point,
    non-directory, unreadable, invalid-name, or manifest-invalid/missing
    sibling fails closed with ``intake-tree-unsafe`` instead of being
    silently skipped -- a malformed or hidden sibling must never be
    invisible to collision detection. Caller MUST hold
    :func:`~phi_engine.utils.pipeline_lock.intake_registry_lock`."""
    try:
        intake_root_fd = _open_workspace_root_readonly(Path(config.INTAKE_DIR))
    except IntakeManifestError:
        raise IntakeManifestError("intake-tree-unsafe") from None
    if intake_root_fd is None:
        return []
    try:
        try:
            with os.scandir(intake_root_fd) as it:
                dirents = sorted(it, key=lambda d: d.name)
        except OSError:
            raise IntakeManifestError("intake-tree-unsafe") from None

        matches: list[str] = []
        for dirent in dirents:
            name = dirent.name
            try:
                is_symlink = dirent.is_symlink()
                is_dir = (not is_symlink) and dirent.is_dir(follow_symlinks=False)
            except OSError:
                raise IntakeManifestError("intake-tree-unsafe") from None
            if is_symlink or not is_dir:
                raise IntakeManifestError("intake-tree-unsafe")
            try:
                pipeline_lock.lock_path_for(name)
            except ValueError:
                raise IntakeManifestError("intake-tree-unsafe") from None
            try:
                manifest = _load_manifest_schema_only(name)
            except IntakeManifestError:
                raise IntakeManifestError("intake-tree-unsafe") from None
            if manifest["study_name_source"] == "generated" and manifest["source_root"] == canonical_source:
                matches.append(name)
        return matches
    finally:
        os.close(intake_root_fd)


def _study_dir_absent(study: str) -> bool:
    try:
        intake_root_fd = _open_workspace_root_readonly(Path(config.INTAKE_DIR))
    except IntakeManifestError:
        raise IntakeManifestError("intake-tree-unsafe") from None
    if intake_root_fd is None:
        return True
    try:
        try:
            os.lstat(study, dir_fd=intake_root_fd)
        except OSError:
            return True
        return False
    finally:
        os.close(intake_root_fd)


def _load_destination_manifest_or_none(study: str) -> dict[str, Any] | None:
    """``None`` only when ``study`` has no directory at all (a genuinely
    brand-new destination). A pre-existing directory with a missing or
    invalid manifest is NOT the same as brand-new -- it fails
    ``intake-tree-unsafe`` rather than being silently treated as an
    available destination."""
    if _study_dir_absent(study):
        return None
    try:
        return _load_manifest_schema_only(study)
    except IntakeManifestError as exc:
        if exc.code == "intake_manifest_missing":
            raise IntakeManifestError("intake-tree-unsafe") from None
        raise


class _Placement(NamedTuple):
    study: str
    study_name_source: str
    promote_from: str | None  # non-None: rename this generated study into `study` before reconciling


def _resolve_registry_placement(
    canonical_source: str,
    resolution: intake_naming.StudyResolution,
    matches: list[str],
) -> _Placement:
    """Registry-lock-protected placement DECISION ONLY -- never touches
    the filesystem. Caller MUST hold ``intake_registry_lock`` and MUST
    have computed ``matches`` (every generated-source-root match for
    ``canonical_source``) BEFORE calling ``resolve_intake_study``, since
    the injected ``generate_study_name`` hook already reused the sole
    match or allocated fresh against that SAME ``matches`` list for the
    "generated" branch. Raises value-free
    ``IntakeManifestError('study-name-collision')`` -- and creates
    nothing -- for every forbidden transition: multiple generated
    matches, a rename request onto a ready generated tree, a same-source
    dual tree, or a different-source occupied destination."""
    if resolution.source == "generated":
        # The injected hook already reused the sole match or allocated a
        # fresh name (raising collision itself for >1 matches); nothing
        # left to decide, and never a promotion.
        return _Placement(resolution.name, "generated", None)

    if len(matches) > 1:
        raise IntakeManifestError("study-name-collision")

    if len(matches) == 1 and matches[0] != resolution.name:
        generated_name = matches[0]
        if not _study_dir_absent(resolution.name):
            raise IntakeManifestError("study-name-collision")  # same-source dual tree
        generated_manifest = _load_manifest_schema_only(generated_name)
        if generated_manifest["status"] == "ready":
            raise IntakeManifestError("study-name-collision")  # renaming an established study
        return _Placement(resolution.name, resolution.source, generated_name)

    destination = _load_destination_manifest_or_none(resolution.name)
    if destination is not None and destination["source_root"] != canonical_source:
        raise IntakeManifestError("study-name-collision")  # different-source occupied destination

    return _Placement(resolution.name, resolution.source, None)


def _rollback_tree_rename(old_study: str, new_study: str) -> None:
    intake_root_fd = _open_workspace_root_creating(Path(config.INTAKE_DIR))
    try:
        with contextlib.suppress(OSError):
            os.rename(new_study, old_study, src_dir_fd=intake_root_fd, dst_dir_fd=intake_root_fd)
    finally:
        os.close(intake_root_fd)


def _rollback_output_dirs(created_dir_paths: list[str]) -> None:
    """Best-effort, deepest-first removal of OUTPUT_DIR directories a
    failed attempt created, ONLY when each is now empty (a directory
    that still holds unrelated content is simply skipped, never forced).
    Never raises -- a rollback step failing must not mask the original
    error driving it."""
    if not created_dir_paths:
        return
    output_fd = _open_workspace_root_creating(Path(config.OUTPUT_DIR))
    try:
        for dir_path in reversed(created_dir_paths):
            dir_parts = tuple(dir_path.split("/"))
            parent_parts, basename = dir_parts[:-1], dir_parts[-1]
            with contextlib.suppress(Exception):
                parent_fd = _descend(output_fd, parent_parts, create=False)
                if parent_fd is not None:
                    try:
                        os.rmdir(basename, dir_fd=parent_fd)
                    finally:
                        os.close(parent_fd)
    finally:
        os.close(output_fd)


def _rollback_promotion(old_study: str, new_study: str, created_audit_dirs: list[str]) -> None:
    """Undo a completed :func:`_promote_generated_tree`: move the audit
    review directory content back (best-effort -- never allowed to block
    or mask the tree rename-back, which is the primary safety property),
    remove the destination audit ancestor directories THIS promotion
    created (deepest-first, only if now empty), and rename the intake
    tree back to ``old_study``. Called when reconciliation or the review
    note fails AFTER promotion already renamed the tree, so the caller's
    outer exception propagates with every durable path exactly where it
    started."""
    with contextlib.suppress(Exception):
        _move_intake_review_dir(new_study, old_study, [])
    _rollback_output_dirs(created_audit_dirs)
    _rollback_tree_rename(old_study, new_study)


def _move_intake_review_dir(old_study: str, new_study: str, created_dirs: list[str]) -> None:
    """Descriptor-safe move of ONLY the intake-owned review directory
    (``<OUTPUT_DIR>/<old_study>/audit/human_review/intake``) into the
    promoted study. A missing source (no review dir was ever written) is
    a legitimate no-op. Any other failure -- including an already-
    occupied destination -- raises ``intake-tree-unsafe`` so the caller
    rolls the tree rename back; nothing here is ever left half-moved.
    Every ``new_study``-relative destination directory THIS call
    actually creates is appended to ``created_dirs`` (before the rename
    that might fail), so a caller can remove them again on rollback even
    if this call raises."""
    if os.rename not in os.supports_dir_fd:
        raise IntakeManifestError("intake-tree-unsafe")

    output_fd = _open_workspace_root_creating(Path(config.OUTPUT_DIR))
    try:
        old_review_fd = _descend(output_fd, (old_study, "audit", "human_review"), create=False)
        if old_review_fd is None:
            return  # nothing was ever written for this study; legitimate no-op
        try:
            probe_fd = _open_existing_dir_strict(old_review_fd, "intake")
            if probe_fd is None:
                return  # no intake-owned review subdir to move
            os.close(probe_fd)

            new_review_fd = _descend(
                output_fd,
                (new_study, "audit", "human_review"),
                create=True,
                on_created=lambda walked: created_dirs.append("/".join(walked)),
            )
            try:
                existing_fd = _open_existing_dir_strict(new_review_fd, "intake")
                if existing_fd is not None:
                    os.close(existing_fd)
                    raise IntakeManifestError("intake-tree-unsafe")
                try:
                    os.rename("intake", "intake", src_dir_fd=old_review_fd, dst_dir_fd=new_review_fd)
                except OSError:
                    raise IntakeManifestError("intake-tree-unsafe") from None
            finally:
                os.close(new_review_fd)
        finally:
            os.close(old_review_fd)
    finally:
        os.close(output_fd)


def _promote_generated_tree(old_study: str, new_study: str) -> list[str]:
    """Descriptor-safe, atomic promotion of a sole, non-ready generated
    intake tree into ``new_study``: same-filesystem rename of the intake
    tree, then the intake-owned audit review directory (if any), with a
    full rollback of the tree rename -- and of any destination audit
    directory this attempt created -- if the audit move cannot complete.
    Caller MUST already hold ``pipeline_lock(old_study)`` and
    ``pipeline_lock(new_study)`` (in that order) plus the registry lock,
    and MUST NOT rename anything before both are held. Never merges,
    never overwrites, never touches a ready tree -- the caller has
    already proven every precondition. Returns the destination audit
    ancestor directories THIS call created, so a caller whose LATER
    reconciliation attempt fails can remove them again on rollback."""
    if os.rename not in os.supports_dir_fd:
        raise IntakeManifestError("intake-tree-unsafe")

    intake_root_fd = _open_workspace_root_creating(Path(config.INTAKE_DIR))
    try:
        old_fd = _open_existing_dir_strict(intake_root_fd, old_study)
        if old_fd is None:
            raise IntakeManifestError("intake-tree-unsafe")
        os.close(old_fd)
        if _open_existing_dir_strict(intake_root_fd, new_study) is not None:
            raise IntakeManifestError("study-name-collision")
        try:
            os.rename(old_study, new_study, src_dir_fd=intake_root_fd, dst_dir_fd=intake_root_fd)
        except OSError:
            raise IntakeManifestError("intake-tree-unsafe") from None
    finally:
        os.close(intake_root_fd)

    created_dirs: list[str] = []
    try:
        _move_intake_review_dir(old_study, new_study, created_dirs)
    except BaseException:
        _rollback_output_dirs(created_dirs)
        _rollback_tree_rename(old_study, new_study)
        raise
    return created_dirs


# --- reconciliation --------------------------------------------------------------------------


def _load_existing_for_reconcile(study_fd: int, *, freshly_reserved: bool) -> dict[str, Any]:
    """In-memory empty v3 state ONLY when ``freshly_reserved`` -- THIS
    call created the study directory, proving no prior manifest could
    ever have existed. A pre-existing directory with no manifest file is
    NOT the same as brand-new; it fails ``intake-tree-unsafe`` rather
    than being silently treated as an empty reservation. Any PRESENT
    manifest is always fully schema-validated; it is never silently
    discarded/reset on validation failure."""
    raw = _read_manifest_json(study_fd)
    if raw is None:
        if freshly_reserved:
            return _empty_manifest_v3()
        raise IntakeManifestError("intake-tree-unsafe")
    return _validate_manifest_v3(raw, expect_study=None)


def _create_or_verify_symlink(parent_fd: int, basename: str, target: str) -> bool:
    """Returns ``True`` only when THIS call actually created the
    symlink; ``False`` when an already-existing symlink was verified to
    match exactly (idempotent re-run, not this attempt's mutation)."""
    if os.symlink not in os.supports_dir_fd:
        raise IntakeManifestError("intake-tree-unsafe")
    try:
        os.symlink(target, basename, dir_fd=parent_fd)
        return True
    except FileExistsError:
        pass
    try:
        info = os.lstat(basename, dir_fd=parent_fd)
    except OSError:
        raise IntakeManifestError("intake-tree-unsafe") from None
    if not stat.S_ISLNK(info.st_mode):
        raise IntakeManifestError("intake-tree-unsafe")
    try:
        existing_target = os.readlink(basename, dir_fd=parent_fd)
    except OSError:
        raise IntakeManifestError("intake-tree-unsafe") from None
    if existing_target != target:
        raise IntakeManifestError("intake-tree-unsafe")
    return False


@dataclass
class _ReconcileJournal:
    """The smallest mutation journal that makes one reconcile attempt's
    filesystem writes reversible: every directory THIS attempt created
    (shallow-to-deep, for deepest-first ``rmdir`` on rollback), every
    symlink THIS attempt created (for ``unlink`` on rollback), and every
    symlink THIS attempt pruned (``intake_path``, prior target -- for
    ``symlink`` recreation on rollback). Never touches the manifest or
    review note; those are restored separately from their own captured
    prior bytes."""

    created_dir_paths: list[str] = field(default_factory=list)
    created_link_paths: list[str] = field(default_factory=list)
    pruned_links: list[tuple[str, str]] = field(default_factory=list)


def _write_entries(
    study_fd: int,
    raw_source: Path,
    canonical_source: str,
    candidates: tuple[IntakeCandidate, ...],
    existing_by_rel: dict[str, dict[str, Any]],
    journal: _ReconcileJournal,
) -> tuple[dict[str, Any], set[str], list[dict[str, Any]]]:
    entries: dict[str, Any] = {}
    seen_rel: set[str] = set()
    entry_errors: list[dict[str, Any]] = []

    for candidate in candidates:
        prior = existing_by_rel.get(candidate.relative_path)
        artifact_id = prior["artifact_id"] if prior is not None else _new_artifact_id()
        intake_path = _compute_intake_path(candidate.relative_path, candidate.component, artifact_id)
        original_path = _canonical_original_path(canonical_source, candidate.relative_path)

        try:
            with open_verified_source(
                raw_source,
                candidate.relative_path,
                required_source_component=candidate.source_component,
                expected_identity=candidate.identity,
            ) as fd:
                mode = stat.S_IMODE(os.fstat(fd).st_mode)
                parts, basename = _split_intake_path(intake_path)
                created_dirs_here: list[tuple[str, ...]] = []
                parent_fd = _descend(
                    study_fd, parts, create=True, on_created=created_dirs_here.append
                )
                try:
                    created_link = _create_or_verify_symlink(parent_fd, basename, original_path)
                finally:
                    os.close(parent_fd)
        except VerifiedSourceError as exc:
            entry_errors.append({"path": candidate.relative_path, "reason": exc.reason})
            continue

        journal.created_dir_paths.extend("/".join(walked) for walked in created_dirs_here)
        if created_link:
            journal.created_link_paths.append(intake_path)

        entries[intake_path] = {
            "artifact_id": artifact_id,
            "intake_path": intake_path,
            "component": candidate.component,
            "relative_path": candidate.relative_path,
            "original_path": original_path,
            "sha256": candidate.sha256,
            "size": candidate.identity.size,
            "mtime_ns": candidate.identity.mtime_ns,
            "device": candidate.identity.device,
            "inode": candidate.identity.inode,
            "mode": mode,
        }
        seen_rel.add(candidate.relative_path)

    return entries, seen_rel, entry_errors


def _prune_stale_entries(
    study_fd: int, prior_entries: dict[str, Any], new_entries: dict[str, Any], journal: _ReconcileJournal
) -> tuple[list[dict[str, Any]], set[str]]:
    """Remove ONLY symlinks whose prior-manifest intake_path key is absent
    from the freshly reconciled entries, and ONLY after a live
    descriptor-relative lstat/readlink proves the current object is
    exactly the expected symlink. Anything else -- already gone, wrong
    type, mismatched target, unopenable ancestry -- is left untouched and
    surfaced as a fixed-code error instead. Returns the errors plus the
    set of stale ``intake_path`` keys that were left in place (already
    reported here; the caller's unexpected-node inventory must not
    double-report them). Every successfully pruned link is journaled
    (``intake_path``, prior target) so a later failure can recreate it."""
    stale_errors: list[dict[str, Any]] = []
    left_in_place: set[str] = set()
    for intake_path, prior_entry in prior_entries.items():
        if intake_path in new_entries:
            continue
        parts, basename = _split_intake_path(intake_path)
        rel = prior_entry.get("relative_path")
        try:
            parent_fd = _descend(study_fd, parts, create=False)
        except IntakeManifestError:
            stale_errors.append({"path": rel, "reason": "intake-tree-unsafe"})
            left_in_place.add(intake_path)
            continue
        if parent_fd is None:
            continue  # directory chain already gone -- nothing to prune
        try:
            try:
                info = os.lstat(basename, dir_fd=parent_fd)
            except FileNotFoundError:
                continue  # already gone
            except OSError:
                stale_errors.append({"path": rel, "reason": "intake-tree-unsafe"})
                left_in_place.add(intake_path)
                continue
            if not stat.S_ISLNK(info.st_mode):
                stale_errors.append({"path": rel, "reason": "intake-tree-unsafe"})
                left_in_place.add(intake_path)
                continue
            try:
                target = os.readlink(basename, dir_fd=parent_fd)
            except OSError:
                stale_errors.append({"path": rel, "reason": "intake-tree-unsafe"})
                left_in_place.add(intake_path)
                continue
            if target != prior_entry.get("original_path"):
                stale_errors.append({"path": rel, "reason": "intake-tree-unsafe"})
                left_in_place.add(intake_path)
                continue
            try:
                os.unlink(basename, dir_fd=parent_fd)
            except OSError:
                stale_errors.append({"path": rel, "reason": "intake-tree-unsafe"})
                left_in_place.add(intake_path)
            else:
                journal.pruned_links.append((intake_path, target))
        finally:
            os.close(parent_fd)
    return stale_errors, left_in_place


def _allowed_directory_prefixes(intake_paths: set[str]) -> set[str]:
    """Every directory PREFIX implied by ``intake_paths`` (e.g.
    ``datasets/nested/aid__f.csv`` implies the two allowed directories
    ``datasets`` and ``datasets/nested``). Any directory NOT in this set
    is unexpected -- including an otherwise-empty one, which a leaf-only
    inventory would never see."""
    prefixes: set[str] = set()
    for intake_path in intake_paths:
        parts = intake_path.split("/")
        for depth in range(1, len(parts)):
            prefixes.add("/".join(parts[:depth]))
    return prefixes


def _inventory_unexpected_nodes(study_fd: int, expected_intake_paths: set[str]) -> list[dict[str, Any]]:
    """Recursively walk every node under ``study_fd`` (skipping only the
    canonical manifest filename at the root) and fail
    ``intake-tree-unsafe`` for any leaf (symlink, regular file, or any
    other non-directory node) whose intake-relative path is not exactly
    one of ``expected_intake_paths``, AND for any directory (root,
    component, or nested -- present or empty) whose intake-relative path
    is not one of the allowed prefixes those paths imply. Detection
    only -- nothing here is ever deleted, moved, or modified; an
    unexpected directory is still recursed into so nested problems are
    found in the same pass, never silently skipped. Errors never carry
    the offending path (it was never a legitimate entry) -- only the
    fixed sentinel."""
    allowed_dirs = _allowed_directory_prefixes(expected_intake_paths)
    errors: list[dict[str, Any]] = []

    def walk(dir_fd: int, prefix: tuple[str, ...]) -> None:
        try:
            with os.scandir(dir_fd) as it:
                dirents = sorted(it, key=lambda d: d.name)
        except OSError:
            errors.append({"path": _ROOT_PATH, "reason": "intake-tree-unsafe"})
            return
        for dirent in dirents:
            if not prefix and dirent.name == _MANIFEST_FILENAME:
                continue
            rel_parts = prefix + (dirent.name,)
            rel = "/".join(rel_parts)
            try:
                is_symlink = dirent.is_symlink()
            except OSError:
                errors.append({"path": _ROOT_PATH, "reason": "intake-tree-unsafe"})
                continue
            if is_symlink:
                if rel not in expected_intake_paths:
                    errors.append({"path": _ROOT_PATH, "reason": "intake-tree-unsafe"})
                continue
            try:
                is_dir = dirent.is_dir(follow_symlinks=False)
            except OSError:
                errors.append({"path": _ROOT_PATH, "reason": "intake-tree-unsafe"})
                continue
            if is_dir:
                if rel not in allowed_dirs:
                    errors.append({"path": _ROOT_PATH, "reason": "intake-tree-unsafe"})
                try:
                    sub_fd = os.open(dirent.name, _DIR_OPEN_FLAGS, dir_fd=dir_fd)
                except OSError:
                    errors.append({"path": _ROOT_PATH, "reason": "intake-tree-unsafe"})
                    continue
                try:
                    walk(sub_fd, rel_parts)
                finally:
                    os.close(sub_fd)
                continue
            errors.append({"path": _ROOT_PATH, "reason": "intake-tree-unsafe"})

    walk(study_fd, ())
    return errors


def _rollback_reconcile_mutations(study_fd: int, journal: _ReconcileJournal) -> None:
    """Best-effort, deepest-first undo of every filesystem mutation this
    reconcile attempt made to the intake tree: unlink every symlink it
    created, recreate every symlink it pruned, then remove every
    directory it created (reverse creation order, so children are
    removed before their parents). Never raises -- a rollback step
    failing must not mask the original error driving it."""
    for intake_path in reversed(journal.created_link_paths):
        parts, basename = _split_intake_path(intake_path)
        with contextlib.suppress(Exception):
            parent_fd = _descend(study_fd, parts, create=False)
            if parent_fd is not None:
                try:
                    os.unlink(basename, dir_fd=parent_fd)
                finally:
                    os.close(parent_fd)

    for intake_path, target in journal.pruned_links:
        parts, basename = _split_intake_path(intake_path)
        with contextlib.suppress(Exception):
            parent_fd = _descend(study_fd, parts, create=False)
            if parent_fd is not None:
                try:
                    _create_or_verify_symlink(parent_fd, basename, target)
                finally:
                    os.close(parent_fd)

    for dir_path in reversed(journal.created_dir_paths):
        dir_parts = tuple(dir_path.split("/"))
        parent_parts, basename = dir_parts[:-1], dir_parts[-1]
        with contextlib.suppress(Exception):
            parent_fd = _descend(study_fd, parent_parts, create=False)
            if parent_fd is not None:
                try:
                    os.rmdir(basename, dir_fd=parent_fd)
                finally:
                    os.close(parent_fd)


def _enrich_review_artifact_ids(review_items: list[dict[str, Any]], entries: dict[str, Any]) -> None:
    by_rel = {entry["relative_path"]: entry["artifact_id"] for entry in entries.values()}
    for item in review_items:
        if "artifact_id" not in item:
            artifact_id = by_rel.get(item["path"])
            if artifact_id is not None:
                item["artifact_id"] = artifact_id


def _review_note_text(manifest: dict[str, Any]) -> str:
    from collections import Counter

    reasons: Counter[str] = Counter(item["reason"] for item in manifest["review_items"])
    reasons.update(item["reason"] for item in manifest["errors"])
    lines = [
        "# Intake Review",
        "",
        f"- review_items: {len(manifest['review_items'])}",
        f"- errors: {len(manifest['errors'])}",
        "",
    ]
    lines.extend(f"- {reason}: {count}" for reason, count in sorted(reasons.items()))
    lines.append("")
    return "\n".join(lines)


def _write_review_note(study: str, manifest: dict[str, Any], created_dirs: list[str]) -> None:
    """Writes the note ONLY when there is something to report (an empty
    ``review_items``/``errors`` manifest never touches the note tree at
    all). Every OUTPUT_DIR directory THIS call actually creates is
    appended to ``created_dirs`` (before the write that might fail), so
    a caller can remove them again on rollback even if this call raises
    partway through (including after the atomic rename but during
    fsync)."""
    if not manifest["review_items"] and not manifest["errors"]:
        return
    output_fd = _open_workspace_root_creating(Path(config.OUTPUT_DIR))
    try:
        note_dir_fd = _descend(
            output_fd,
            (study, "audit", "human_review", "intake"),
            create=True,
            on_created=lambda walked: created_dirs.append("/".join(walked)),
        )
        try:
            _atomic_write_in_dir(note_dir_fd, "intake_review.md", _review_note_text(manifest).encode("utf-8"), 0o600)
        finally:
            os.close(note_dir_fd)
    finally:
        os.close(output_fd)


def _read_review_note_bytes(study: str) -> bytes | None:
    """Raw pre-mutation snapshot of the review note, or ``None`` if none
    existed yet. Used only to restore exact prior bytes/absence on a
    failed reconcile attempt."""
    output_fd = _open_workspace_root_readonly(Path(config.OUTPUT_DIR))
    if output_fd is None:
        return None
    try:
        note_dir_fd = _descend(output_fd, (study, "audit", "human_review", "intake"), create=False)
        if note_dir_fd is None:
            return None
        try:
            with contextlib.suppress(IntakeManifestError):
                return _read_regular_file_bytes(note_dir_fd, "intake_review.md")
            return None
        finally:
            os.close(note_dir_fd)
    finally:
        os.close(output_fd)


def _restore_review_note(study: str, prior_bytes: bytes | None) -> None:
    """Best-effort: never raises -- a rollback step failing must not mask
    the original error driving it. Restoring to ABSENCE never creates a
    directory chain that did not already exist -- a missing ancestor
    simply means there is nothing to unlink, not a reason to fabricate
    empty directories mid-rollback (:func:`_rollback_output_dirs` is what
    removes directories THIS attempt itself created)."""
    with contextlib.suppress(Exception):
        if prior_bytes is None:
            output_fd = _open_workspace_root_readonly(Path(config.OUTPUT_DIR))
            if output_fd is None:
                return
            try:
                note_dir_fd = _descend(output_fd, (study, "audit", "human_review", "intake"), create=False)
                if note_dir_fd is None:
                    return
                try:
                    with contextlib.suppress(OSError):
                        os.unlink("intake_review.md", dir_fd=note_dir_fd)
                finally:
                    os.close(note_dir_fd)
            finally:
                os.close(output_fd)
        else:
            output_fd = _open_workspace_root_creating(Path(config.OUTPUT_DIR))
            try:
                note_dir_fd = _descend(output_fd, (study, "audit", "human_review", "intake"), create=True)
                try:
                    _atomic_write_in_dir(note_dir_fd, "intake_review.md", prior_bytes, 0o600)
                finally:
                    os.close(note_dir_fd)
            finally:
                os.close(output_fd)


def _reconcile_study_tree(
    *,
    canonical_source: str,
    raw_source: Path,
    study: str,
    study_name_source: str,
    preflight: IntakePreflight,
    resolution: intake_naming.StudyResolution,
) -> dict[str, Any]:
    intake_root_fd = _open_workspace_root_creating(Path(config.INTAKE_DIR))
    try:
        study_fd, freshly_reserved = _open_study_dir_creating(intake_root_fd, study)
        study_fd_open = True
        try:
            journal = _ReconcileJournal()
            prior_manifest_bytes = _read_manifest_bytes(study_fd)
            note_touched = False
            prior_note_bytes: bytes | None = None
            note_created_dirs: list[str] = []
            try:
                existing = _load_existing_for_reconcile(study_fd, freshly_reserved=freshly_reserved)

                existing_entries = existing.get("entries") or {}
                existing_by_rel = {entry["relative_path"]: entry for entry in existing_entries.values()}

                entries, seen_rel, entry_errors = _write_entries(
                    study_fd, raw_source, canonical_source, preflight.candidates, existing_by_rel, journal
                )

                removed_rels = sorted(set(existing_by_rel) - seen_rel)
                removals = list(existing.get("removals") or [])
                now = utc_now_z()
                for rel in removed_rels:
                    old = existing_by_rel[rel]
                    removals.append(
                        {"artifact_id": old["artifact_id"], "relative_path": rel, "sha256": old["sha256"], "removed_at": now}
                    )

                prune_errors, left_in_place = _prune_stale_entries(study_fd, existing_entries, entries, journal)
                unexpected_errors = _inventory_unexpected_nodes(study_fd, set(entries) | left_in_place)

                review_items = list(preflight.review_items) + list(resolution.review_items)
                errors = (
                    list(preflight.errors)
                    + list(resolution.errors)
                    + entry_errors
                    + prune_errors
                    + unexpected_errors
                )
                _enrich_review_artifact_ids(review_items, entries)

                status = "failed" if errors else ("review_required" if review_items else "ready")
                manifest = {
                    "schema": _MANIFEST_SCHEMA,
                    "study": study,
                    "study_name_source": study_name_source,
                    "status": status,
                    "source_root": canonical_source,
                    "entries": entries,
                    "review_items": review_items,
                    "errors": errors,
                    "removals": removals,
                }
                _validate_manifest_v3(manifest, expect_study=study)  # self-check before persisting

                # Mark the note transaction touched BEFORE attempting the
                # write (not after it returns): any failure from this point
                # on -- write, rename, or fsync -- must unconditionally
                # restore the prior note leaf on rollback, even though
                # `_write_review_note` never gets to return normally.
                note_touched = bool(review_items or errors)
                if note_touched:
                    prior_note_bytes = _read_review_note_bytes(study)
                    _write_review_note(study, manifest, note_created_dirs)
                payload = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n"
                _atomic_write_in_dir(study_fd, _MANIFEST_FILENAME, payload, 0o600)
            except BaseException:
                # Full transactional rollback: undo every symlink/directory
                # this attempt created, recreate every link it pruned, put
                # the manifest and review note back exactly as they were
                # (or remove them, and their now-empty parent directories,
                # if they did not exist before), and -- only for a
                # reservation THIS call itself made -- remove the now-empty
                # study directory so a retry gets a genuinely fresh
                # reservation again.
                _rollback_reconcile_mutations(study_fd, journal)
                _restore_manifest_bytes(study_fd, prior_manifest_bytes)
                if note_touched:
                    _restore_review_note(study, prior_note_bytes)
                    _rollback_output_dirs(note_created_dirs)
                if freshly_reserved:
                    os.close(study_fd)
                    study_fd_open = False
                    with contextlib.suppress(OSError, NotImplementedError):
                        os.rmdir(study, dir_fd=intake_root_fd)
                raise
        finally:
            if study_fd_open:
                os.close(study_fd)
    finally:
        os.close(intake_root_fd)

    return manifest


# --- public write path -----------------------------------------------------------------------


@contextlib.contextmanager
def _registry_lock_or_unsafe():
    """Hold the intake-registry lock, converting a raw ``OSError`` from
    ACQUISITION alone (e.g. hostile/symlinked workspace ancestry) to the
    value-free ``intake-tree-unsafe`` contract. Never swallows
    ``PipelineBusyError`` (contention is not a workspace-safety issue)
    and never converts anything raised by the body it wraps."""
    try:
        pipeline_lock.acquire_intake_registry_lock()
    except OSError:
        raise IntakeManifestError("intake-tree-unsafe") from None
    try:
        yield
    finally:
        pipeline_lock.release_intake_registry_lock()


@contextlib.contextmanager
def _study_lock_or_unsafe(study: str):
    """Same acquisition-only ``OSError`` conversion as
    :func:`_registry_lock_or_unsafe`, for a single study's pipeline
    lock."""
    try:
        pipeline_lock.acquire_pipeline_lock(study)
    except OSError:
        raise IntakeManifestError("intake-tree-unsafe") from None
    try:
        yield
    finally:
        pipeline_lock.release_pipeline_lock(study)


def intake_add(source: Path, study: str | None = None, *, support_confirmed_no_phi: bool = False) -> dict[str, Any]:
    """Deterministic, symlink-only intake reconciliation. Runs preflight
    (never an LLM), resolves the study name (local-only, support-content-
    only AI boundary), reconciles the intake-manifest/v3 tree atomically
    under the registry-then-study lock order, and returns the persisted
    manifest. Never copies/moves/writes/chmods/deletes a source artifact."""
    raw_source = Path(source)
    try:
        canonical_source = intake_naming.canonical_source_root(raw_source)
    except (OSError, RuntimeError):
        raise IntakeManifestError("source-unreadable") from None

    with _registry_lock_or_unsafe():
        preflight = intake_preflight.inspect_intake_source(raw_source)
        matches = _scan_generated_manifests_for_source(canonical_source)

        def _allocate_or_reuse_generated_name() -> str:
            if len(matches) > 1:
                raise IntakeManifestError("study-name-collision")
            if len(matches) == 1:
                return matches[0]
            return intake_naming._generate_study_name()

        resolution = intake_naming._resolve_intake_study(
            raw_source,
            preflight,
            explicit_study=study,
            support_confirmed_no_phi=support_confirmed_no_phi,
            intake_root=Path(config.INTAKE_DIR),
            generate_study_name=_allocate_or_reuse_generated_name,
        )

        placement = _resolve_registry_placement(canonical_source, resolution, matches)

        if placement.promote_from is not None:
            with _study_lock_or_unsafe(placement.promote_from):
                with _study_lock_or_unsafe(placement.study):
                    promoted_audit_dirs = _promote_generated_tree(placement.promote_from, placement.study)
                    try:
                        manifest = _reconcile_study_tree(
                            canonical_source=canonical_source,
                            raw_source=raw_source,
                            study=placement.study,
                            study_name_source=placement.study_name_source,
                            preflight=preflight,
                            resolution=resolution,
                        )
                    except BaseException:
                        _rollback_promotion(placement.promote_from, placement.study, promoted_audit_dirs)
                        raise
        else:
            with _study_lock_or_unsafe(placement.study):
                manifest = _reconcile_study_tree(
                    canonical_source=canonical_source,
                    raw_source=raw_source,
                    study=placement.study,
                    study_name_source=placement.study_name_source,
                    preflight=preflight,
                    resolution=resolution,
                )

    return manifest
