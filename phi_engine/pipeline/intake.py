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
import time
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
from phi_engine.utils.atomic_fs import AtomicRenameUnavailable
from phi_engine.utils.atomic_fs import renameat2_noreplace as _shared_renameat2_noreplace

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


def _open_study_dir_reserving(parent_fd: int, name: str, *, must_be_fresh: bool) -> tuple[int, bool]:
    """Open the study directory as ONE atomic state transition matching
    the placement decision -- never a separate existence probe followed
    by a later create/adopt, which is exactly the reservation race the
    security review found. ``must_be_fresh=True`` means the placement
    decision was that this destination is meant to be BRAND NEW:
    creation here is a bare ``mkdir`` with no prior existence check, so
    there is no gap between "absent" and "created" a hostile same-
    namespace actor (advisory locks never stop one) could win; a name
    that already exists at this exact instant, for ANY reason, is
    exactly the collision the reservation exists to prevent --
    ``study-name-collision``, never silently adopted. ``must_be_fresh
    =False`` means the caller already proved (registry-lock-protected)
    that this destination is meant to be REUSED -- the directory MUST
    already exist; if it does not (a hostile deletion since that
    proof), this fails ``study-name-collision`` too rather than
    silently downgrading into a fresh reservation nobody asked for."""
    if must_be_fresh:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            raise IntakeManifestError("study-name-collision") from None
        except OSError:
            raise IntakeManifestError("intake-tree-unsafe") from None
        try:
            fd = os.open(name, _DIR_OPEN_FLAGS, dir_fd=parent_fd)
        except OSError:
            raise IntakeManifestError("intake-tree-unsafe") from None
        try:
            _verify_and_lock_down_dir(fd)
        except BaseException:
            os.close(fd)
            raise
        return fd, True

    fd = _open_existing_dir_strict(parent_fd, name)
    if fd is None:
        raise IntakeManifestError("study-name-collision")
    return fd, False


def _descend(
    parent_fd: int,
    parts: tuple[str, ...],
    *,
    create: bool,
    on_created: Callable[[tuple[str, ...], int, int], None] | None = None,
) -> int | None:
    """Return an owned fd for the directory chain ``parts`` relative to
    ``parent_fd``. ``create=True`` creates missing 0700 segments
    (``intake-tree-unsafe`` on any unsafe node), invoking ``on_created``
    with the cumulative path tuple AND the (device, inode) identity of
    each segment THIS call actually created -- captured immediately via
    ``fstat`` on the freshly opened descriptor, so a caller can journal
    exactly what it made and later prove, by that inode alone, that a
    rollback removal is touching the very directory this call created
    rather than anything swapped in afterward. ``create=False`` requires
    every segment to already exist as a verified real directory,
    returning ``None`` (not raising) the moment any segment in the
    chain is simply absent."""
    current = parent_fd
    owns_current = False
    walked: list[str] = []
    try:
        for part in parts:
            walked.append(part)
            if create:
                nxt, created = _open_dir_creating(current, part)
                if created and on_created is not None:
                    info = os.fstat(nxt)
                    on_created(tuple(walked), info.st_dev, info.st_ino)
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


# --- atomic no-replace rename / quarantine (hostile-namespace-safe deletes) ---------------


def _renameat2_noreplace(old_dir_fd: int, old: str, new_dir_fd: int, new: str) -> None:
    """Atomic, no-replace rename via the Linux ``renameat2(2)`` syscall
    (``RENAME_NOREPLACE``): the kernel either performs the rename or
    fails with ``EEXIST`` -- there is no window between an existence
    check and the rename itself for a hostile same-namespace actor to
    race a fresh node into the destination name. Delegates to the
    shared primitive (``phi_engine.utils.atomic_fs``) also used by the
    XLS isolation boundary's bundle publication, so both callers share
    one ``renameat2(2)`` implementation rather than two independently
    maintained copies. Raises ``FileNotFoundError``/``FileExistsError``
    for those specific fixed outcomes, a generic ``OSError`` for any
    other errno, and ``IntakeManifestError('intake-tree-unsafe')`` --
    fail-closed, never a check-then-rename fallback -- when this
    platform cannot provide the guarantee at all."""
    try:
        _shared_renameat2_noreplace(old_dir_fd, old, new_dir_fd, new)
    except AtomicRenameUnavailable:
        raise IntakeManifestError("intake-tree-unsafe") from None


_QUARANTINE_DIRNAME = ".intake_quarantine"

# Hard bound on the shared retained-quarantine root: a fixed maximum
# entry count and a fixed maximum total allocated-byte footprint,
# checked descriptor-relatively (never trusting a persistent counter
# that could drift from a crash or a hostile process) before ANY new
# node is accepted into quarantine. Quarantine entries are exclusively
# unguessable-named symlinks and directories this module itself
# retains after proving their identity -- never source data (intake is
# symlink-only) -- so these bounds exist purely to cap same-UID
# inode/disk exhaustion from ordinary add/remove churn, not to bound
# any single retained object's size.
#
# Offline operator lifecycle (the only supported cleanup path): this
# module never deletes a verified-retained node itself -- POSIX has no
# conditional-unlink primitive, and unlinking by a mutable name after
# verification would reopen exactly the same TOCTOU this module exists
# to close. When usage approaches these bounds, an operator STOPS every
# phi_engine pipeline process for the workspace, takes exclusive control
# of the workspace filesystem (no concurrent same-UID writer), inspects
# ``BASE_DIR/.intake_quarantine`` (auditable via each entry's ``kind``
# prefix, embedded creation timestamp, and mode-0700 containment), and
# only then removes retained entries with ordinary offline tooling
# (e.g. ``rm -rf`` under that exclusive control). This module never
# attempts online deletion under hostile same-UID assumptions; that is
# a fundamentally different, weaker threat model than the fail-closed
# rename/verify contract every other operation here provides.
_QUARANTINE_MAX_ENTRIES = 10_000
_QUARANTINE_MAX_BYTES = 256 * 1024 * 1024  # 256 MiB


def _open_quarantine_root_creating() -> int:
    """Open (creating if absent) the single protected, private ``0700``
    quarantine directory shared by every retained-quarantine primitive
    in this module -- ``BASE_DIR/.intake_quarantine``, a sibling of
    ``intake/`` and ``output/`` directly under the workspace root, so a
    cross-directory atomic rename from either tree always stays on the
    same filesystem. Deliberately outside both ``INTAKE_DIR`` (the
    registry scan only walks its own children) and any single study's
    own tree (a study's unexpected-node inventory only walks that
    study's own directory), so a retained node can never poison either
    invariant. Online cleanup of retained nodes is intentionally never
    performed here -- see the offline operator lifecycle documented at
    :data:`_QUARANTINE_MAX_BYTES` above; growth past the fixed bounds
    fails closed instead."""
    return _open_workspace_root_creating(Path(config.BASE_DIR) / _QUARANTINE_DIRNAME)


def _quarantine_usage(quarantine_root_fd: int) -> tuple[int, int]:
    """Descriptor-relative ``(entry_count, total_bytes)`` for the shared
    quarantine root, walked fresh from filesystem truth on every check
    -- never an in-memory counter that could drift under a crash or a
    concurrent hostile process. ``total_bytes`` sums every top-level
    entry's own size (a symlink's is the length of its target text) plus,
    for a retained directory, every node in its subtree -- this module
    only ever quarantines directories it has itself proven are either
    empty or hold a single small audit artifact, so this stays cheap in
    practice while still being real filesystem truth, not an estimate."""
    entry_count = 0
    total_bytes = 0

    def _walk(dir_fd: int) -> None:
        nonlocal total_bytes
        with os.scandir(dir_fd) as it:
            for dirent in it:
                try:
                    info = dirent.stat(follow_symlinks=False)
                except OSError:
                    continue
                total_bytes += info.st_size
                if stat.S_ISDIR(info.st_mode):
                    try:
                        sub_fd = os.open(dirent.name, _DIR_OPEN_FLAGS, dir_fd=dir_fd)
                    except OSError:
                        continue
                    try:
                        _walk(sub_fd)
                    finally:
                        os.close(sub_fd)

    with os.scandir(quarantine_root_fd) as it:
        for dirent in it:
            entry_count += 1
            try:
                info = dirent.stat(follow_symlinks=False)
            except OSError:
                continue
            total_bytes += info.st_size
            if stat.S_ISDIR(info.st_mode):
                try:
                    sub_fd = os.open(dirent.name, _DIR_OPEN_FLAGS, dir_fd=quarantine_root_fd)
                except OSError:
                    continue
                try:
                    _walk(sub_fd)
                finally:
                    os.close(sub_fd)
    return entry_count, total_bytes


def _quarantine_retain(parent_fd: int, quarantine_root_fd: int, basename: str, kind: str) -> str | None:
    """Atomically move ``basename`` out of ``parent_fd`` into the
    shared protected quarantine root under a private, unguessable name
    -- a cross-directory ``RENAME_NOREPLACE``, so there is no window in
    which the object exists at neither name, and nothing else can ever
    guess or race the quarantine name once assigned. The quarantine
    name itself is auditable without being sensitive: a fixed, non-
    identifying ``kind`` (``link``/``dir``/``file`` -- never a source
    filename), the whole-second UTC creation timestamp, and a random
    token. Checked descriptor-relatively against the fixed entry-count
    and allocated-byte bounds BEFORE accepting the move -- fails closed
    with ``IntakeManifestError('quarantine-limit-exceeded')`` and
    performs no rename at all once either bound would be exceeded, so
    quarantine growth can never run past its hard limits. The byte
    check is against ``basename``'s OWN prospective footprint, not just
    prior usage: its size is read via a single descriptor-relative
    ``fstatat(parent_fd, basename, follow_symlinks=False)`` -- the same
    call whose (device, inode) is the expected identity the post-move
    ``verify`` gate re-checks -- so a node whose incoming size alone
    would cross the remaining capacity is rejected before any rename,
    never merely when prior usage had already exhausted it. The
    capacity comparison is overflow-safe (``incoming > max - used``,
    never ``used + incoming > max``). Returns the quarantine name, or
    ``None`` when ``basename`` was already gone (a legitimate no-op,
    not a failure). Raises ``IntakeManifestError('intake-tree-unsafe')``
    for any other rename or stat failure; the node, if it still exists,
    is left exactly where it was."""
    entry_count, total_bytes = _quarantine_usage(quarantine_root_fd)
    try:
        incoming_info = os.stat(basename, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        raise IntakeManifestError("intake-tree-unsafe") from None
    incoming_bytes = incoming_info.st_size
    if entry_count >= _QUARANTINE_MAX_ENTRIES or incoming_bytes > _QUARANTINE_MAX_BYTES - total_bytes:
        raise IntakeManifestError("quarantine-limit-exceeded")
    quarantine_name = f"{kind}.{int(time.time())}.{secrets.token_hex(16)}"
    try:
        _renameat2_noreplace(parent_fd, basename, quarantine_root_fd, quarantine_name)
    except FileNotFoundError:
        return None
    except (OSError, IntakeManifestError):
        raise IntakeManifestError("intake-tree-unsafe") from None
    return quarantine_name


def _restore_from_quarantine_root(quarantine_root_fd: int, quarantine_name: str, parent_fd: int, basename: str) -> bool:
    """Best-effort atomic restore of a quarantined node back to its
    original name, WITHOUT replacing anything that has since reclaimed
    that name. ``False`` means the original name is occupied again --
    the quarantined node is left exactly where it is (retained, never
    deleted, never forced back), so the caller fails closed instead of
    losing it."""
    try:
        _renameat2_noreplace(quarantine_root_fd, quarantine_name, parent_fd, basename)
        return True
    except (OSError, IntakeManifestError):
        return False


def _quarantine_and_gate(
    parent_fd: int,
    quarantine_root_fd: int,
    basename: str,
    kind: str,
    verify: Callable[[int, str], bool],
) -> tuple[str, str | None]:
    """The shared retained-quarantine primitive every destructive
    rollback/prune step in this module funnels through: atomically move
    ``basename`` out of ``parent_fd`` into the shared protected
    quarantine root, then run ``verify(quarantine_root_fd,
    quarantine_name)`` against the quarantined object. A verified match
    is RETAINED in quarantine -- POSIX has no conditional-unlink
    primitive, so a node this call has already verified is never
    deleted by a mutable name afterward; the retained node is only ever
    reclaimed via the offline operator lifecycle documented at
    :data:`_QUARANTINE_MAX_BYTES`. A mismatch is restored to its
    original name without replacing anything that has since reclaimed
    it, or -- if that restore itself races -- retained under
    quarantine, failing closed either way. Returns ``(outcome,
    quarantine_name)``: ``("absent", None)`` when nothing was there,
    ``("retained", <name>)`` for a verified match kept in quarantine --
    the name a caller MUST journal to restore this exact node later --
    or ``("unsafe", None)`` for a mismatch. Propagates
    ``IntakeManifestError('quarantine-limit-exceeded')`` rather than
    swallowing it into ``"unsafe"``: capacity exhaustion is a distinct,
    fixed fail-closed condition every forward caller must surface, not
    a silent no-op prune."""
    try:
        quarantine_name = _quarantine_retain(parent_fd, quarantine_root_fd, basename, kind)
    except IntakeManifestError as exc:
        if exc.code == "quarantine-limit-exceeded":
            raise
        return "unsafe", None
    if quarantine_name is None:
        return "absent", None
    try:
        matched = verify(quarantine_root_fd, quarantine_name)
    except OSError:
        matched = False
    if matched:
        return "retained", quarantine_name
    _restore_from_quarantine_root(quarantine_root_fd, quarantine_name, parent_fd, basename)
    return "unsafe", None


def _verify_symlink_identity(expected_target: str, expected_identity: tuple[int, int] | None) -> Callable[[int, str], bool]:
    """Verification predicate for :func:`_quarantine_and_gate`: proves,
    by TYPE + TARGET (and by DEVICE/INODE when ``expected_identity`` was
    captured at this attempt's own creation time), that the quarantined
    object is exactly the symlink this call is responsible for."""

    def _verify(dir_fd: int, name: str) -> bool:
        info = os.lstat(name, dir_fd=dir_fd)
        if not stat.S_ISLNK(info.st_mode):
            return False
        if expected_identity is not None and (info.st_dev, info.st_ino) != expected_identity:
            return False
        return os.readlink(name, dir_fd=dir_fd) == expected_target

    return _verify


def _verify_regular_identity(expected_identity: tuple[int, int]) -> Callable[[int, str], bool]:
    """Verification predicate for :func:`_quarantine_and_gate`: proves,
    by DEVICE/INODE alone -- never by name or byte content, which a
    different regular file could coincidentally match -- that the
    quarantined object is exactly the regular file THIS attempt itself
    wrote."""

    def _verify(dir_fd: int, name: str) -> bool:
        info = os.lstat(name, dir_fd=dir_fd)
        return stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == expected_identity

    return _verify


def _verify_dir_identity(expected_identity: tuple[int, int]) -> Callable[[int, str], bool]:
    """Verification predicate for :func:`_quarantine_and_gate`: proves,
    by DEVICE/INODE alone, that the quarantined object is exactly the
    directory THIS call opened and recorded before quarantining it."""

    def _verify(dir_fd: int, name: str) -> bool:
        info = os.lstat(name, dir_fd=dir_fd)
        return stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == expected_identity

    return _verify


# --- atomic same-directory writes ----------------------------------------------------------


def _atomic_write_in_dir(
    dir_fd: int,
    filename: str,
    payload: bytes,
    mode: int,
    *,
    on_committed: Callable[[tuple[int, int]], None] | None = None,
) -> None:
    """Write-temp/fsync/rename-replace commit. ``on_committed``, when
    given, is called with the (device, inode) identity of the file THIS
    call just installed at ``filename`` -- captured immediately after
    the commit rename, BEFORE the final directory fsync, so a caller
    can journal exactly what this attempt wrote even if that later
    fsync itself subsequently raises."""
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
    if on_committed is not None:
        info = os.lstat(filename, dir_fd=dir_fd)
        on_committed((info.st_dev, info.st_ino))
    os.fsync(dir_fd)


def _atomic_write_in_dir_noreplace(dir_fd: int, filename: str, payload: bytes, mode: int) -> bool:
    """Same write-temp/fsync construction as :func:`_atomic_write_in_dir`,
    but the final commit is an atomic NO-REPLACE rename -- used ONLY to
    install prior content during rollback, always AFTER the current
    occupant (if any) has already been quarantined and identity-
    verified. Returns ``True`` when this call's payload actually landed
    at ``filename`` (the name was free at that instant); ``False`` when
    the name is occupied again -- nothing is written there, the temp
    file is discarded, and the caller's already-quarantined content
    stays exactly where it is instead of being clobbered back over a
    name something else has since reclaimed."""
    if os.rename not in os.supports_dir_fd or os.open not in os.supports_dir_fd:
        raise IntakeManifestError("intake-tree-unsafe")
    temp_name = f".{filename}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC
    fd = os.open(temp_name, flags, mode, dir_fd=dir_fd)
    try:
        written = 0
        while written < len(payload):
            written += os.write(fd, payload[written:])
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        _renameat2_noreplace(dir_fd, temp_name, dir_fd, filename)
    except FileExistsError:
        with contextlib.suppress(OSError):
            os.unlink(temp_name, dir_fd=dir_fd)
        return False
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name, dir_fd=dir_fd)
        raise
    os.fsync(dir_fd)
    return True


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


def _read_regular_file_with_identity(dir_fd: int, filename: str) -> tuple[bytes, tuple[int, int]] | None:
    """Same descriptor-relative regular-file read as
    :func:`_read_regular_file_bytes`, but also returns the (device,
    inode) identity captured from the SAME open descriptor immediately
    after open -- never from a separate by-name ``lstat`` that could
    race a swap between the read and the identity capture. Used to pin
    an expectation (e.g. a registry-scanned manifest's identity) against
    what is actually read, never trusting a name alone."""
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
        identity = (info.st_dev, info.st_ino)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), identity
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


def _restore_manifest_bytes(
    study_fd: int,
    quarantine_root_fd: int,
    prior_bytes: bytes | None,
    attempt_identity: tuple[int, int] | None,
) -> None:
    """Best-effort: never raises -- a rollback step failing must not
    mask the original error driving it. Never touches the current node
    at ``_MANIFEST_FILENAME`` unless ``attempt_identity`` proves -- by
    quarantined DEVICE/INODE, never by name or bytes alone -- that it
    is exactly the manifest THIS failed attempt itself committed
    (``attempt_identity`` is ``None`` when this attempt's own write
    step never completed, in which case there is nothing of this
    attempt's to undo and the current node -- whatever it is -- is left
    completely alone). A verified match is retained in the shared
    quarantine directory (never unlinked by a mutable name after
    verification); prior content, when there was any, is then installed
    ONLY via an atomic no-replace rename into the now-freed name -- a
    name reclaimed since quarantining simply does not get the prior
    content written back, leaving the quarantine of this attempt's own
    manifest retained instead of clobbering whatever reclaimed it."""
    if attempt_identity is None:
        return
    with contextlib.suppress(Exception):
        outcome, _quarantine_name = _quarantine_and_gate(
            study_fd, quarantine_root_fd, _MANIFEST_FILENAME, "file",
            _verify_regular_identity(attempt_identity),
        )
        if outcome == "retained" and prior_bytes is not None:
            _atomic_write_in_dir_noreplace(study_fd, _MANIFEST_FILENAME, prior_bytes, 0o600)


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


class _GeneratedMatch(NamedTuple):
    study: str
    study_dir_identity: tuple[int, int]
    manifest_identity: tuple[int, int]


def _scan_generated_manifests_for_source(canonical_source: str) -> list[_GeneratedMatch]:
    """Every study whose v3 manifest is ``study_name_source == "generated"``
    and whose ``source_root`` canonically matches -- each returned with the
    (device, inode) identity of BOTH its study directory and its manifest
    file, captured from the SAME descriptors this scan itself opened and
    read, so a later reuse/promotion can pin and re-prove this exact
    scanned expectation on the descriptor it actually opens, rather than
    trusting this scan's name alone. Every sibling under ``INTAKE_DIR`` is
    treated as hostile: a symlink/reparse point, non-directory,
    unreadable, invalid-name, or manifest-invalid/missing sibling fails
    closed with ``intake-tree-unsafe`` instead of being silently skipped
    -- a malformed or hidden sibling must never be invisible to collision
    detection. Caller MUST hold
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

        matches: list[_GeneratedMatch] = []
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
                study_fd = os.open(name, _DIR_OPEN_FLAGS, dir_fd=intake_root_fd)
            except OSError:
                raise IntakeManifestError("intake-tree-unsafe") from None
            try:
                try:
                    dir_info = os.fstat(study_fd)
                    if not stat.S_ISDIR(dir_info.st_mode) or stat.S_ISLNK(dir_info.st_mode):
                        raise IntakeManifestError("intake-tree-unsafe")
                    study_dir_identity = (dir_info.st_dev, dir_info.st_ino)
                    result = _read_regular_file_with_identity(study_fd, _MANIFEST_FILENAME)
                    if result is None:
                        raise IntakeManifestError("intake-tree-unsafe")
                    raw_bytes, manifest_identity = result
                    try:
                        raw = json.loads(raw_bytes.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        raise IntakeManifestError("intake-tree-unsafe") from None
                    manifest = _validate_manifest_v3(raw, expect_study=name)
                except IntakeManifestError:
                    raise IntakeManifestError("intake-tree-unsafe") from None
            finally:
                os.close(study_fd)
            if manifest["study_name_source"] == "generated" and manifest["source_root"] == canonical_source:
                matches.append(_GeneratedMatch(name, study_dir_identity, manifest_identity))
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
    must_be_fresh: bool  # True: reservation MUST create `study` atomically; existing is a collision
    expected_study_dir_identity: tuple[int, int] | None = None  # pinned scan expectation for the reused/promoted tree
    expected_manifest_identity: tuple[int, int] | None = None  # pinned scan expectation for that tree's manifest


def _resolve_registry_placement(
    canonical_source: str,
    resolution: intake_naming.StudyResolution,
    matches: list[_GeneratedMatch],
) -> _Placement:
    """Registry-lock-protected placement DECISION ONLY -- never touches
    the filesystem (never mkdir's, never renames). Caller MUST hold
    ``intake_registry_lock`` and MUST have computed ``matches`` (every
    generated-source-root match for ``canonical_source``, each carrying
    its scanned study-directory and manifest identity) BEFORE calling
    ``resolve_intake_study``, since the injected ``generate_study_name``
    hook already reused the sole match or allocated fresh against that
    SAME ``matches`` list for the "generated" branch. Raises value-free
    ``IntakeManifestError('study-name-collision')`` -- and creates
    nothing -- for every forbidden transition: multiple generated
    matches, a rename request onto a ready generated tree, a same-source
    dual tree, or a different-source occupied destination.

    The absence/occupancy checks here are non-racy ONLY for a single,
    non-hostile caller -- they exist purely for a fast, clean early
    rejection. The actual security boundary against a hostile same-
    namespace actor (advisory locks never stop one) is TWO-FOLD:
    ``_Placement.must_be_fresh`` -- for a genuinely brand-new destination
    (``must_be_fresh=True``), the caller MUST create it with a bare,
    check-free ``mkdir`` and treat any resulting ``EEXIST`` as
    ``study-name-collision`` -- never adopt whatever is found there --
    and, whenever this scan actually pinned an expectation for the
    reused/promoted tree, ``_Placement.expected_study_dir_identity``/
    ``expected_manifest_identity``, which the caller MUST re-prove on
    its own freshly opened descriptor before trusting that tree's
    content at all: a same-source replacement directory or a manifest
    swapped in since this scan (even with an unchanged ``source_root``
    string) must yield ``study-name-collision`` untouched, never be
    silently adopted."""
    by_name = {match.study: match for match in matches}

    if resolution.source == "generated":
        # The injected hook already reused the sole match or allocated a
        # fresh name (raising collision itself for >1 matches); nothing
        # left to decide, and never a promotion. A freshly allocated
        # token that happens to already occupy a destination -- for any
        # reason, any source -- is a foreign collision: it was NOT the
        # sole reused match, so nothing here may ever touch it.
        match = by_name.get(resolution.name)
        if match is not None:
            return _Placement(
                resolution.name, "generated", None, must_be_fresh=False,
                expected_study_dir_identity=match.study_dir_identity,
                expected_manifest_identity=match.manifest_identity,
            )
        if not _study_dir_absent(resolution.name):
            raise IntakeManifestError("study-name-collision")
        return _Placement(resolution.name, "generated", None, must_be_fresh=True)

    if len(matches) > 1:
        raise IntakeManifestError("study-name-collision")

    if len(matches) == 1 and matches[0].study != resolution.name:
        generated_match = matches[0]
        generated_name = generated_match.study
        if not _study_dir_absent(resolution.name):
            raise IntakeManifestError("study-name-collision")  # same-source dual tree
        generated_manifest = _load_manifest_schema_only(generated_name)
        if generated_manifest["status"] == "ready":
            raise IntakeManifestError("study-name-collision")  # renaming an established study
        return _Placement(
            resolution.name, resolution.source, generated_name, must_be_fresh=False,
            expected_study_dir_identity=generated_match.study_dir_identity,
            expected_manifest_identity=generated_match.manifest_identity,
        )

    destination = _load_destination_manifest_or_none(resolution.name)
    if destination is not None and destination["source_root"] != canonical_source:
        raise IntakeManifestError("study-name-collision")  # different-source occupied destination

    match = by_name.get(resolution.name)
    return _Placement(
        resolution.name, resolution.source, None, must_be_fresh=destination is None,
        expected_study_dir_identity=match.study_dir_identity if match is not None else None,
        expected_manifest_identity=match.manifest_identity if match is not None else None,
    )


def _rollback_tree_rename(old_study: str, new_study: str) -> None:
    """Best-effort, atomic no-replace rename back to ``old_study``: never
    clobbers anything a hostile actor has since created at that name --
    a raced occupant is left exactly where it is, and this simply fails
    to restore rather than deleting or replacing it."""
    intake_root_fd = _open_workspace_root_creating(Path(config.INTAKE_DIR))
    try:
        with contextlib.suppress(Exception):
            _renameat2_noreplace(intake_root_fd, new_study, intake_root_fd, old_study)
    finally:
        os.close(intake_root_fd)


def _rollback_output_dirs(quarantine_root_fd: int, created_dirs: list[tuple[str, int, int]]) -> None:
    """Best-effort, deepest-first identity-gated retained-quarantine of
    every OUTPUT_DIR directory a failed attempt created, ONLY when each
    is now empty (a directory that still holds unrelated content -- for
    example a restored/impostor review-note leaf a sibling rollback step
    left behind -- is simply skipped, never forced or swept away wholesale).
    Deletion itself is never a path-only ``rmdir``: the candidate's OWN
    (device, inode) identity is captured from the SAME open descriptor
    used for the emptiness check, and only THAT exact object is
    atomically quarantined -- a name that has since been swapped for an
    unrelated directory fails the post-quarantine identity check inside
    :func:`_quarantine_and_gate` (verified against the DEVICE/INODE
    captured at THIS attempt's own creation time, never the swap-time
    read) and is restored untouched, so the swap is caught at the exact
    final rename/verify operation. Never raises -- a rollback step
    failing must not mask the original error driving it."""
    if not created_dirs:
        return
    output_fd = _open_workspace_root_creating(Path(config.OUTPUT_DIR))
    try:
        for dir_path, device, inode in reversed(created_dirs):
            dir_parts = tuple(dir_path.split("/"))
            parent_parts, basename = dir_parts[:-1], dir_parts[-1]
            with contextlib.suppress(Exception):
                parent_fd = _descend(output_fd, parent_parts, create=False)
                if parent_fd is not None:
                    try:
                        try:
                            owned_fd = _open_existing_dir_strict(parent_fd, basename)
                        except IntakeManifestError:
                            owned_fd = None  # not a directory -- leave untouched
                        if owned_fd is not None:
                            try:
                                with os.scandir(owned_fd) as it:
                                    is_empty = next(iter(it), None) is None
                            finally:
                                os.close(owned_fd)
                            if is_empty:
                                _quarantine_and_gate(
                                    parent_fd, quarantine_root_fd, basename, "dir",
                                    _verify_dir_identity((device, inode)),
                                )
                    finally:
                        os.close(parent_fd)
    finally:
        os.close(output_fd)


def _rollback_promotion(
    old_study: str, new_study: str, created_audit_dirs: list[tuple[str, int, int]]
) -> None:
    """Undo a completed :func:`_promote_generated_tree`: move the audit
    review directory content back (best-effort -- never allowed to block
    or mask the tree rename-back, which is the primary safety property),
    identity-gate every destination audit ancestor directory THIS
    promotion created into retained quarantine (deepest-first), and
    rename the intake tree back to ``old_study``. Called when
    reconciliation or the review note fails AFTER promotion already
    renamed the tree, so the caller's outer exception propagates with
    every durable path exactly where it started."""
    with contextlib.suppress(Exception):
        _move_intake_review_dir(new_study, old_study, [])
    quarantine_root_fd = _open_quarantine_root_creating()
    try:
        _rollback_output_dirs(quarantine_root_fd, created_audit_dirs)
    finally:
        os.close(quarantine_root_fd)
    _rollback_tree_rename(old_study, new_study)


def _move_intake_review_dir(
    old_study: str, new_study: str, created_dirs: list[tuple[str, int, int]]
) -> None:
    """Descriptor-safe, atomic no-replace move of ONLY the intake-owned
    review directory (``<OUTPUT_DIR>/<old_study>/audit/human_review/
    intake``) into the promoted study. A missing source (no review dir
    was ever written) is a legitimate no-op. Any other failure --
    including an already-occupied destination, proven atomically rather
    than by a separate existence check -- raises ``intake-tree-unsafe``
    so the caller rolls the tree rename back; nothing here is ever left
    half-moved or silently replaces a raced-in destination. Every
    ``new_study``-relative destination directory THIS call actually
    creates -- path plus the (device, inode) identity captured at
    creation -- is appended to ``created_dirs`` (before the rename that
    might fail), so a caller can identity-gate their removal on
    rollback even if this call raises."""
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
                on_created=lambda walked, device, inode: created_dirs.append(("/".join(walked), device, inode)),
            )
            try:
                try:
                    _renameat2_noreplace(old_review_fd, "intake", new_review_fd, "intake")
                except (OSError, IntakeManifestError):
                    raise IntakeManifestError("intake-tree-unsafe") from None
            finally:
                os.close(new_review_fd)
        finally:
            os.close(old_review_fd)
    finally:
        os.close(output_fd)


def _promote_generated_tree(
    old_study: str,
    new_study: str,
    *,
    expected_study_dir_identity: tuple[int, int] | None = None,
    expected_manifest_identity: tuple[int, int] | None = None,
) -> list[tuple[str, int, int]]:
    """Descriptor-safe, atomic promotion of a sole, non-ready generated
    intake tree into ``new_study``: same-filesystem rename of the intake
    tree, then the intake-owned audit review directory (if any), with a
    full rollback of the tree rename -- and of any destination audit
    directory this attempt created -- if the audit move cannot complete.
    When ``expected_study_dir_identity``/``expected_manifest_identity``
    are given (the registry-lock-protected scan's pinned expectation for
    ``old_study``), they are re-proven on THIS call's own freshly opened
    descriptor -- directory device/inode, then manifest device/inode --
    BEFORE the rename: a same-source replacement directory or a
    manifest swapped in since the scan (even with an unchanged
    ``source_root`` string) yields ``study-name-collision`` untouched
    rather than being silently promoted. Caller MUST already hold
    ``pipeline_lock(old_study)`` and ``pipeline_lock(new_study)`` (in
    that order) plus the registry lock, and MUST NOT rename anything
    before both are held. Never merges, never overwrites, never touches
    a ready tree -- the caller has already proven every precondition.
    Returns the destination audit ancestor directories THIS call
    created (path plus (device, inode) identity), so a caller whose
    LATER reconciliation attempt fails can identity-gate their removal
    on rollback."""
    intake_root_fd = _open_workspace_root_creating(Path(config.INTAKE_DIR))
    try:
        old_fd = _open_existing_dir_strict(intake_root_fd, old_study)
        if old_fd is None:
            raise IntakeManifestError("intake-tree-unsafe")
        try:
            if expected_study_dir_identity is not None:
                dir_info = os.fstat(old_fd)
                if (dir_info.st_dev, dir_info.st_ino) != expected_study_dir_identity:
                    raise IntakeManifestError("study-name-collision")
            if expected_manifest_identity is not None:
                result = _read_regular_file_with_identity(old_fd, _MANIFEST_FILENAME)
                if result is None or result[1] != expected_manifest_identity:
                    raise IntakeManifestError("study-name-collision")
        finally:
            os.close(old_fd)
        try:
            _renameat2_noreplace(intake_root_fd, old_study, intake_root_fd, new_study)
        except FileExistsError:
            raise IntakeManifestError("study-name-collision") from None
        except OSError:
            raise IntakeManifestError("intake-tree-unsafe") from None
    finally:
        os.close(intake_root_fd)

    created_dirs: list[tuple[str, int, int]] = []
    try:
        _move_intake_review_dir(old_study, new_study, created_dirs)
    except BaseException:
        quarantine_root_fd = _open_quarantine_root_creating()
        try:
            _rollback_output_dirs(quarantine_root_fd, created_dirs)
        finally:
            os.close(quarantine_root_fd)
        _rollback_tree_rename(old_study, new_study)
        raise
    return created_dirs


# --- reconciliation --------------------------------------------------------------------------


def _load_existing_for_reconcile(
    study_fd: int,
    *,
    freshly_reserved: bool,
    canonical_source: str,
    expected_prior_study: str,
    expected_study_dir_identity: tuple[int, int] | None = None,
    expected_manifest_identity: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """In-memory empty v3 state ONLY when ``freshly_reserved`` -- THIS
    call created the study directory, proving no prior manifest could
    ever have existed. A pre-existing directory with no manifest file is
    NOT the same as brand-new; it fails ``intake-tree-unsafe`` rather
    than being silently treated as an empty reservation. Any PRESENT
    manifest is always fully schema-validated against
    ``expected_prior_study`` -- the directory's OWN name for a direct
    reuse, or the pre-rename generated name for a just-promoted tree
    (the manifest has not been rewritten yet at this point) -- never a
    blanket ``expect_study=None`` that would silently accept a manifest
    whose stored ``study`` field disagrees with its directory; it is
    never silently discarded/reset on validation failure. When
    ``expected_study_dir_identity``/``expected_manifest_identity`` are
    given (a registry-scanned generated match's pinned expectation),
    they are re-proven HERE, on the exact descriptor this call actually
    holds -- directory identity before the manifest is even opened,
    then the manifest's own identity captured from the SAME read that
    parses it -- so a same-source replacement directory or a manifest
    swapped in since the scan yields ``study-name-collision`` untouched
    before a single byte of it is trusted. For a REUSED destination
    (``freshly_reserved=False``), the pinned descriptor's own
    ``source_root`` MUST ALSO equal ``canonical_source`` -- re-proven
    here, on the descriptor this call actually holds, rather than
    trusted from the registry scan's earlier, unpinned read; a mismatch
    is ``study-name-collision``, and nothing is loaded or reconciled."""
    if expected_study_dir_identity is not None:
        dir_info = os.fstat(study_fd)
        if (dir_info.st_dev, dir_info.st_ino) != expected_study_dir_identity:
            raise IntakeManifestError("study-name-collision")
    try:
        result = _read_regular_file_with_identity(study_fd, _MANIFEST_FILENAME)
    except IntakeManifestError:
        raise IntakeManifestError("intake_manifest_invalid") from None
    if result is None:
        if freshly_reserved:
            return _empty_manifest_v3()
        raise IntakeManifestError("intake-tree-unsafe")
    raw_bytes, manifest_identity = result
    if expected_manifest_identity is not None and manifest_identity != expected_manifest_identity:
        raise IntakeManifestError("study-name-collision")
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise IntakeManifestError("intake_manifest_invalid") from None
    existing = _validate_manifest_v3(raw, expect_study=expected_prior_study)
    if not freshly_reserved and existing["source_root"] != canonical_source:
        raise IntakeManifestError("study-name-collision")
    return existing


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
    (shallow-to-deep, path + DEVICE/INODE identity captured at creation,
    for identity-gated quarantine-and-retain on rollback -- deepest-
    first), every directory THIS attempt pruned to empty (deepest-first;
    path, the exact RETAINED quarantine name, and the DEVICE/INODE
    identity captured immediately before quarantining -- so rollback can
    restore the EXACT quarantined node by an atomic no-replace rename,
    shallow-first, rather than adopting or recreating a same-named
    directory), every symlink THIS attempt created (``intake_path``,
    target, and DEVICE/INODE identity captured at creation -- so
    rollback can prove, by inode alone, that it is quarantining the
    right object), and every symlink THIS attempt pruned (``intake_path``,
    prior target, the exact retained quarantine name, and the DEVICE/
    INODE identity captured immediately before quarantining -- so
    rollback can restore the EXACT quarantined link by an atomic no-
    replace rename rather than synthesizing a new one). Never touches
    the manifest or review note; those are restored separately from
    their own captured prior bytes and their own captured written-
    identity."""

    created_dir_paths: list[tuple[str, int, int]] = field(default_factory=list)
    pruned_dir_paths: list[tuple[str, str, int, int]] = field(default_factory=list)
    created_link_paths: list[tuple[str, str, int, int]] = field(default_factory=list)
    pruned_links: list[tuple[str, str, str, int, int]] = field(default_factory=list)


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
                created_dirs_here: list[tuple[tuple[str, ...], int, int]] = []
                parent_fd = _descend(
                    study_fd, parts, create=True,
                    on_created=lambda walked, device, inode: created_dirs_here.append((walked, device, inode)),
                )
                try:
                    created_link = _create_or_verify_symlink(parent_fd, basename, original_path)
                    link_identity: tuple[int, int] | None = None
                    if created_link:
                        link_info = os.lstat(basename, dir_fd=parent_fd)
                        link_identity = (link_info.st_dev, link_info.st_ino)
                finally:
                    os.close(parent_fd)
        except VerifiedSourceError as exc:
            entry_errors.append({"path": candidate.relative_path, "reason": exc.reason})
            continue

        for walked, device, inode in created_dirs_here:
            journal.created_dir_paths.append(("/".join(walked), device, inode))
        if created_link:
            journal.created_link_paths.append((intake_path, original_path, link_identity[0], link_identity[1]))

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
    study_fd: int,
    quarantine_root_fd: int,
    prior_entries: dict[str, Any],
    new_entries: dict[str, Any],
    journal: _ReconcileJournal,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Remove ONLY symlinks whose prior-manifest intake_path key is absent
    from the freshly reconciled entries, and ONLY after atomically
    quarantining the current object into the shared protected quarantine
    root and proving -- by its TYPE + TARGET + DEVICE/INODE identity
    captured immediately before quarantining, never by name alone -- that
    it is still exactly the expected symlink. A verified match is
    RETAINED in quarantine (never unlinked by a mutable name after its
    own verification); a mismatched object is restored to its original
    name without replacing anything that has since claimed it, or --
    if that restore itself races -- retained under quarantine, failing
    closed instead of deleting or losing unrelated content, reported
    here as a fixed-code error rather than silently pruned. Returns the
    errors plus the set of stale ``intake_path`` keys that were left in
    place (already reported here; the caller's unexpected-node
    inventory must not double-report them). Every successfully
    quarantined-and-retained link is journaled (``intake_path``, prior
    target, the exact retained quarantine name, and the DEVICE/INODE
    identity captured before quarantining) so a later failure can
    restore the EXACT quarantined node."""
    stale_errors: list[dict[str, Any]] = []
    left_in_place: set[str] = set()
    for intake_path, prior_entry in prior_entries.items():
        if intake_path in new_entries:
            continue
        parts, basename = _split_intake_path(intake_path)
        rel = prior_entry.get("relative_path")
        expected_target = prior_entry.get("original_path")
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
                link_info = os.lstat(basename, dir_fd=parent_fd)
            except FileNotFoundError:
                continue  # already gone -- nothing to prune
            except OSError:
                stale_errors.append({"path": rel, "reason": "intake-tree-unsafe"})
                left_in_place.add(intake_path)
                continue
            expected_identity = (link_info.st_dev, link_info.st_ino)
            outcome, quarantine_name = _quarantine_and_gate(
                parent_fd, quarantine_root_fd, basename, "link",
                _verify_symlink_identity(expected_target, expected_identity),
            )
        finally:
            os.close(parent_fd)
        if outcome == "retained":
            journal.pruned_links.append(
                (intake_path, expected_target, quarantine_name, expected_identity[0], expected_identity[1])
            )
        elif outcome == "unsafe":
            stale_errors.append({"path": rel, "reason": "intake-tree-unsafe"})
            left_in_place.add(intake_path)
        # "absent" -- already gone; nothing to report
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


def _prune_stale_directories(
    study_fd: int,
    quarantine_root_fd: int,
    prior_entries: dict[str, Any],
    new_entries: dict[str, Any],
    left_in_place: set[str],
    journal: _ReconcileJournal,
) -> None:
    """Remove ONLY now-empty directory prefixes implied by the fully
    validated PRIOR manifest's entries that are no longer implied by the
    freshly reconciled entries (including anything left in place because
    pruning its symlink was unsafe) -- deepest-first, so a child is
    always removed before its parent. Every candidate is opened and its
    OWN DEVICE/INODE identity recorded FIRST, its emptiness verified
    against that SAME open descriptor, and only then atomically
    quarantined into the shared protected quarantine root -- where its
    identity is re-verified against the descriptor-recorded pair before
    it is retained, never removed by a later name-based ``rmdir``. A
    directory that is not actually empty -- because it still holds an
    unexpected/unowned node -- is silently left in place;
    :func:`_inventory_unexpected_nodes` reports that separately as
    ``intake-tree-unsafe``. A quarantine identity mismatch (a hostile
    swap between the emptiness check and the quarantine rename) is
    restored without replacement and simply left for the inventory pass
    to report, exactly like the not-empty case. Every directory THIS
    call actually retains in quarantine is journaled -- path, the exact
    retained quarantine name, and its DEVICE/INODE identity -- (deepest-
    first, matching removal order) so a later failure can restore the
    EXACT quarantined chain shallow-first on rollback."""
    prior_dirs = _allowed_directory_prefixes(set(prior_entries))
    kept_dirs = _allowed_directory_prefixes(set(new_entries) | left_in_place)
    candidates = sorted(prior_dirs - kept_dirs, key=lambda p: -p.count("/"))
    for dir_path in candidates:
        parts = tuple(dir_path.split("/"))
        parent_parts, basename = parts[:-1], parts[-1]
        try:
            parent_fd = _descend(study_fd, parent_parts, create=False)
        except IntakeManifestError:
            continue  # unsafe ancestry -- leave untouched, surfaced elsewhere
        if parent_fd is None:
            continue  # already gone
        try:
            try:
                owned_fd = _open_existing_dir_strict(parent_fd, basename)
            except IntakeManifestError:
                continue  # not a directory -- leave for inventory to report
            if owned_fd is None:
                continue  # already gone
            try:
                info = os.fstat(owned_fd)
                with os.scandir(owned_fd) as it:
                    is_empty = next(iter(it), None) is None
            finally:
                os.close(owned_fd)
            if not is_empty:
                continue  # not empty -- leave for inventory to report
            outcome, quarantine_name = _quarantine_and_gate(
                parent_fd, quarantine_root_fd, basename, "dir",
                _verify_dir_identity((info.st_dev, info.st_ino)),
            )
            if outcome == "retained":
                journal.pruned_dir_paths.append((dir_path, quarantine_name, info.st_dev, info.st_ino))
        finally:
            os.close(parent_fd)


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


def _rollback_reconcile_mutations(study_fd: int, quarantine_root_fd: int, journal: _ReconcileJournal) -> None:
    """Best-effort, deepest-first undo of every filesystem mutation this
    reconcile attempt made to the intake tree: quarantine-and-retain
    every symlink it created (by journaled link IDENTITY -- TARGET plus
    the DEVICE/INODE captured at creation, never by name alone -- a
    swapped node is left alone rather than retained under this
    attempt's responsibility), restore every directory it pruned to
    empty (shallow-first, so a pruned link's parent exists again before
    the link is recreated inside it), then restore every symlink it
    pruned -- both restored by an atomic no-replace rename of the EXACT
    journaled quarantine node back to its original name, never by
    calling :func:`_open_dir_creating` (which would adopt a same-named
    directory a hostile actor swapped in) or synthesizing a fresh
    symlink (which would do the same for a same-target impostor). If
    the original name is occupied again, the quarantined node is simply
    left retained where it is and the restore fails closed -- nothing
    here is ever adopted, forced, or recreated from scratch. A pruned
    link is restored only when every journaled pruned-directory
    ancestor on its path was ITSELF successfully restored AND its
    restored identity re-verified (by DEVICE/INODE) against the pair
    journaled at prune time -- closing the race between the (shallow-
    first) directory-restore pass and this link-restore pass. The
    instant any journaled ancestor's restore failed (occupied) or its
    identity no longer matches, every descendant under it -- the
    ancestor directory's own further descendants and every pruned link
    beneath it -- is left retained in quarantine and this call never
    even opens (``_descend``s into) that occupied/replaced ancestry;
    nothing is ever adopted into unrelated content that reclaimed a
    pruned name. Finally quarantine-and-retain every directory it
    created (by journaled DEVICE/INODE identity, reverse creation
    order, so children are handled before their parents). Never raises
    -- a rollback step failing must not mask the original error driving
    it."""
    for intake_path, target, device, inode in reversed(journal.created_link_paths):
        parts, basename = _split_intake_path(intake_path)
        with contextlib.suppress(Exception):
            parent_fd = _descend(study_fd, parts, create=False)
            if parent_fd is not None:
                try:
                    _quarantine_and_gate(
                        parent_fd, quarantine_root_fd, basename, "link",
                        _verify_symlink_identity(target, (device, inode)),
                    )
                finally:
                    os.close(parent_fd)

    # Journaled pruned-directory identity, keyed by the exact dir_path
    # string, recorded ONLY once THIS call has itself proven -- via a
    # successful no-replace restore rename -- that the name now holds
    # the exact quarantined directory again. A dir_path absent from
    # this map was never journaled as pruned by this attempt (no
    # gating owed) or its restore failed/was occupied (gating required).
    restored_dir_identity: dict[str, tuple[int, int]] = {}
    journaled_dir_paths = {dir_path for dir_path, *_rest in journal.pruned_dir_paths}

    def _ancestry_restored_and_proven(parts: tuple[str, ...]) -> bool:
        for depth in range(1, len(parts) + 1):
            prefix = "/".join(parts[:depth])
            if prefix not in journaled_dir_paths:
                continue  # not this attempt's pruned directory -- not gated here
            identity = restored_dir_identity.get(prefix)
            if identity is None:
                return False  # this ancestor's own restore failed or was occupied
            try:
                ancestor_fd = _descend(study_fd, parts[:depth], create=False)
            except IntakeManifestError:
                return False
            if ancestor_fd is None:
                return False
            try:
                info = os.fstat(ancestor_fd)
            except OSError:
                return False
            finally:
                os.close(ancestor_fd)
            if (info.st_dev, info.st_ino) != identity:
                return False  # swapped again since this attempt's own restore
        return True

    for dir_path, quarantine_name, device, inode in reversed(journal.pruned_dir_paths):
        dir_parts = tuple(dir_path.split("/"))
        parent_parts, basename = dir_parts[:-1], dir_parts[-1]
        if not _ancestry_restored_and_proven(parent_parts):
            continue  # a journaled ancestor above this one never came back -- stay retained
        restored = False
        with contextlib.suppress(Exception):
            parent_fd = _descend(study_fd, parent_parts, create=False)
            if parent_fd is not None:
                try:
                    restored = _restore_from_quarantine_root(quarantine_root_fd, quarantine_name, parent_fd, basename)
                finally:
                    os.close(parent_fd)
        if restored:
            restored_dir_identity[dir_path] = (device, inode)

    for intake_path, _target, quarantine_name, _device, _inode in journal.pruned_links:
        parts, basename = _split_intake_path(intake_path)
        if not _ancestry_restored_and_proven(parts):
            continue  # a journaled ancestor is occupied/unrestored -- retain the link, never descend
        with contextlib.suppress(Exception):
            parent_fd = _descend(study_fd, parts, create=False)
            if parent_fd is not None:
                try:
                    _restore_from_quarantine_root(quarantine_root_fd, quarantine_name, parent_fd, basename)
                finally:
                    os.close(parent_fd)

    for dir_path, device, inode in reversed(journal.created_dir_paths):
        dir_parts = tuple(dir_path.split("/"))
        parent_parts, basename = dir_parts[:-1], dir_parts[-1]
        with contextlib.suppress(Exception):
            parent_fd = _descend(study_fd, parent_parts, create=False)
            if parent_fd is not None:
                try:
                    try:
                        owned_fd = _open_existing_dir_strict(parent_fd, basename)
                    except IntakeManifestError:
                        owned_fd = None  # not a directory -- leave untouched
                    if owned_fd is not None:
                        try:
                            with os.scandir(owned_fd) as it:
                                is_empty = next(iter(it), None) is None
                        finally:
                            os.close(owned_fd)
                        if is_empty:
                            _quarantine_and_gate(
                                parent_fd, quarantine_root_fd, basename, "dir",
                                _verify_dir_identity((device, inode)),
                            )
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


def _write_review_note(
    study: str, manifest: dict[str, Any], created_dirs: list[tuple[str, int, int]], identity_box: list[tuple[int, int]]
) -> None:
    """Writes the note ONLY when there is something to report (an empty
    ``review_items``/``errors`` manifest never touches the note tree at
    all). Every OUTPUT_DIR directory THIS call actually creates -- path
    plus the (device, inode) identity captured at creation -- is
    appended to ``created_dirs`` (before the write that might fail), so
    a caller can identity-gate their removal again on rollback even if
    this call raises partway through. ``identity_box`` -- a caller-owned,
    empty-until-now list -- receives the (device, inode) identity of the
    note THIS call just committed, appended BEFORE the final directory
    fsync, so the caller still has it even if this call goes on to raise
    (including after the atomic rename but during that fsync)."""
    if not manifest["review_items"] and not manifest["errors"]:
        return
    output_fd = _open_workspace_root_creating(Path(config.OUTPUT_DIR))
    try:
        note_dir_fd = _descend(
            output_fd,
            (study, "audit", "human_review", "intake"),
            create=True,
            on_created=lambda walked, device, inode: created_dirs.append(("/".join(walked), device, inode)),
        )
        try:
            _atomic_write_in_dir(
                note_dir_fd, "intake_review.md", _review_note_text(manifest).encode("utf-8"), 0o600,
                on_committed=identity_box.append,
            )
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


def _restore_review_note(
    study: str,
    quarantine_root_fd: int,
    prior_bytes: bytes | None,
    attempt_identity: tuple[int, int] | None,
) -> None:
    """Best-effort: never raises -- a rollback step failing must not mask
    the original error driving it. Never touches the current node at
    ``intake_review.md`` unless ``attempt_identity`` proves -- by
    quarantined DEVICE/INODE, never by name or bytes alone -- that it is
    exactly the note THIS failed attempt itself committed
    (``attempt_identity`` is ``None`` when this attempt's own write step
    never completed, in which case there is nothing of this attempt's to
    undo and the current node -- whatever it is -- is left completely
    alone). A verified match is retained in the shared quarantine
    directory (never unlinked by a mutable name after verification).
    Restoring to ABSENCE never creates a directory chain that did not
    already exist. When there was prior content, it is installed ONLY
    via an atomic no-replace rename into the now-freed name -- a
    reclaimed name simply does not get the prior content written back,
    leaving the quarantine of this attempt's own note retained instead
    of clobbering whatever reclaimed it."""
    if attempt_identity is None:
        return
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
                    _quarantine_and_gate(
                        note_dir_fd, quarantine_root_fd, "intake_review.md", "file",
                        _verify_regular_identity(attempt_identity),
                    )
                finally:
                    os.close(note_dir_fd)
            finally:
                os.close(output_fd)
        else:
            output_fd = _open_workspace_root_creating(Path(config.OUTPUT_DIR))
            try:
                note_dir_fd = _descend(output_fd, (study, "audit", "human_review", "intake"), create=True)
                try:
                    outcome, _quarantine_name = _quarantine_and_gate(
                        note_dir_fd, quarantine_root_fd, "intake_review.md", "file",
                        _verify_regular_identity(attempt_identity),
                    )
                    if outcome == "retained":
                        _atomic_write_in_dir_noreplace(note_dir_fd, "intake_review.md", prior_bytes, 0o600)
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
    must_be_fresh: bool,
    promote_from: str | None = None,
    expected_study_dir_identity: tuple[int, int] | None = None,
    expected_manifest_identity: tuple[int, int] | None = None,
) -> dict[str, Any]:
    intake_root_fd = _open_workspace_root_creating(Path(config.INTAKE_DIR))
    try:
        quarantine_root_fd = _open_quarantine_root_creating()
        try:
            study_fd, freshly_reserved = _open_study_dir_reserving(
                intake_root_fd, study, must_be_fresh=must_be_fresh
            )
            study_fd_open = True
            study_dir_identity: tuple[int, int] | None = None
            if freshly_reserved:
                reservation_info = os.fstat(study_fd)
                study_dir_identity = (reservation_info.st_dev, reservation_info.st_ino)
            try:
                journal = _ReconcileJournal()
                prior_manifest_bytes = _read_manifest_bytes(study_fd)
                note_touched = False
                prior_note_bytes: bytes | None = None
                note_created_dirs: list[tuple[str, int, int]] = []
                note_identity_box: list[tuple[int, int]] = []
                manifest_identity_box: list[tuple[int, int]] = []
                try:
                    expected_prior_study = promote_from if promote_from is not None else study
                    existing = _load_existing_for_reconcile(
                        study_fd,
                        freshly_reserved=freshly_reserved,
                        canonical_source=canonical_source,
                        expected_prior_study=expected_prior_study,
                        expected_study_dir_identity=expected_study_dir_identity,
                        expected_manifest_identity=expected_manifest_identity,
                    )

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

                    prune_errors, left_in_place = _prune_stale_entries(
                        study_fd, quarantine_root_fd, existing_entries, entries, journal
                    )
                    _prune_stale_directories(
                        study_fd, quarantine_root_fd, existing_entries, entries, left_in_place, journal
                    )
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
                        _write_review_note(study, manifest, note_created_dirs, note_identity_box)
                    payload = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n"
                    _atomic_write_in_dir(
                        study_fd, _MANIFEST_FILENAME, payload, 0o600, on_committed=manifest_identity_box.append
                    )
                except BaseException:
                    # Full transactional rollback: undo every symlink/directory
                    # this attempt created, recreate every link it pruned, put
                    # the manifest and review note back exactly as they were
                    # (or remove them, and their now-empty parent directories,
                    # if they did not exist before), and -- only for a
                    # reservation THIS call itself made -- remove the now-empty
                    # study directory so a retry gets a genuinely fresh
                    # reservation again.
                    _rollback_reconcile_mutations(study_fd, quarantine_root_fd, journal)
                    manifest_written_identity = manifest_identity_box[0] if manifest_identity_box else None
                    _restore_manifest_bytes(
                        study_fd, quarantine_root_fd, prior_manifest_bytes, manifest_written_identity
                    )
                    if note_touched:
                        note_written_identity = note_identity_box[0] if note_identity_box else None
                        _restore_review_note(study, quarantine_root_fd, prior_note_bytes, note_written_identity)
                        _rollback_output_dirs(quarantine_root_fd, note_created_dirs)
                    if freshly_reserved:
                        os.close(study_fd)
                        study_fd_open = False
                        if study_dir_identity is not None:
                            with contextlib.suppress(Exception):
                                probe_fd = _open_existing_dir_strict(intake_root_fd, study)
                                if probe_fd is not None:
                                    try:
                                        with os.scandir(probe_fd) as it:
                                            is_empty = next(iter(it), None) is None
                                    finally:
                                        os.close(probe_fd)
                                    if is_empty:
                                        _quarantine_and_gate(
                                            intake_root_fd, quarantine_root_fd, study, "dir",
                                            _verify_dir_identity(study_dir_identity),
                                        )
                    raise
            finally:
                if study_fd_open:
                    os.close(study_fd)
        finally:
            os.close(quarantine_root_fd)
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
                return matches[0].study
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
                    promoted_audit_dirs = _promote_generated_tree(
                        placement.promote_from, placement.study,
                        expected_study_dir_identity=placement.expected_study_dir_identity,
                        expected_manifest_identity=placement.expected_manifest_identity,
                    )
                    try:
                        manifest = _reconcile_study_tree(
                            canonical_source=canonical_source,
                            raw_source=raw_source,
                            study=placement.study,
                            study_name_source=placement.study_name_source,
                            preflight=preflight,
                            resolution=resolution,
                            must_be_fresh=placement.must_be_fresh,
                            promote_from=placement.promote_from,
                            expected_study_dir_identity=placement.expected_study_dir_identity,
                            expected_manifest_identity=placement.expected_manifest_identity,
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
                    must_be_fresh=placement.must_be_fresh,
                    expected_study_dir_identity=placement.expected_study_dir_identity,
                    expected_manifest_identity=placement.expected_manifest_identity,
                )

    return manifest
