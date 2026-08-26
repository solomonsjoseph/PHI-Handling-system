"""Canonical non-blocking per-study pipeline lock.

The lock inode is persistent informational state, not the lock itself.  Mutual
exclusion comes exclusively from the operating-system advisory lock held on the
open descriptor.  A process that acquires an inode left by an earlier owner
overwrites its stale informational content and reuses the inode.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import stat
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import phi_engine.config.config as config
from phi_engine.study_name import validate_study_name

__all__ = [
    "PipelineBusyError",
    "acquire_intake_registry_lock",
    "acquire_pipeline_lock",
    "held_lock_path",
    "intake_registry_lock",
    "intake_registry_lock_path",
    "is_locally_held",
    "lock_path_for",
    "pipeline_lock",
    "release_intake_registry_lock",
    "release_pipeline_lock",
]


class PipelineBusyError(RuntimeError):
    """Another process owns the per-study pipeline lock.

    The exception deliberately carries only the lock path.  Informational file
    content is untrusted, may be stale, and is never used to decide ownership.
    """

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        super().__init__(f"Study pipeline lock is busy: {lock_path}")


# Study-name pattern/reserved-name enforcement lives in the dependency-free
# phi_engine.study_name module (imported above) -- reused as-is here and by
# phi_engine.config.config's own STUDY_NAME fallback validation, so there
# is exactly one plain-folder-name convention across the codebase.


@dataclass(frozen=True)
class _LockOwner:
    path: Path
    descriptor: int
    pid: int
    thread_id: int


# Access to this per-study registry is synchronized across all callers. Each
# entry represents exactly one logical invocation; acquisition is never
# implicitly re-entrant, even for the owning thread.
_LOCK_OWNERS: dict[Path, _LockOwner] = {}
_LOCK_OWNERS_GUARD = threading.Lock()


def _validated_study_name(study: str | None) -> str:
    return validate_study_name(config.STUDY_NAME if study is None else study)


def lock_path_for(study: str | None = None) -> Path:
    """Return ``tmp/.<validated-study>.pipeline.lock`` without creating it."""
    study_name = _validated_study_name(study)
    return Path(config.TMP_DIR) / f".{study_name}.pipeline.lock"


def is_locally_held() -> bool:
    """Return whether this process currently owns any lock descriptor."""
    pid = os.getpid()
    with _LOCK_OWNERS_GUARD:
        _discard_inherited_owners_locked(pid)
        return any(owner.pid == pid for owner in _LOCK_OWNERS.values())


def held_lock_path() -> Path | None:
    """Return the sole lock path owned by this calling thread, if any."""
    pid = os.getpid()
    thread_id = threading.get_ident()
    with _LOCK_OWNERS_GUARD:
        _discard_inherited_owners_locked(pid)
        paths = [
            owner.path
            for owner in _LOCK_OWNERS.values()
            if owner.pid == pid and owner.thread_id == thread_id
        ]
    return paths[0] if len(paths) == 1 else None


def _canonical_lock_path(lock_path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(lock_path)))


def _is_reparse_point(info: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(getattr(info, "st_file_attributes", 0) & reparse_flag)


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _validate_lock_parent_info(info: os.stat_result, lock_dir: Path) -> None:
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
        raise OSError(errno.ENOTDIR, "pipeline lock parent is not a trusted directory", lock_dir)
    if os.name == "posix" and hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise PermissionError(errno.EPERM, "pipeline lock parent has an unexpected owner", lock_dir)


def _open_dir_ancestry_segment(parent_fd: int, name: str, *, create: bool) -> int | None:
    """Open a single ancestry SEGMENT directly under ``parent_fd``,
    following no symlink/reparse point. ``create=True`` creates a
    missing segment fresh as a private ``0700`` directory (verified,
    never re-chmod'd once it already existed); ``create=False`` returns
    ``None`` -- never raises -- the moment the segment is simply absent.
    A pre-existing symlink/reparse point or any other unexpected node
    type is always rejected regardless of ``create``: never followed,
    never replaced. Shared core for every workspace directory this
    package ever creates OR merely reads."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    for _attempt in range(2):
        try:
            fd = os.open(name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            if not create:
                return None
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            fd = os.open(name, flags, dir_fd=parent_fd)
            try:
                info = os.fstat(fd)
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise OSError(errno.ENOTDIR, "created ancestry segment is not a directory", name)
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, 0o700)
            except BaseException:
                os.close(fd)
                raise
            return fd
        else:
            try:
                info = os.fstat(fd)
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
                    raise OSError(errno.ENOTDIR, "ancestry segment is not a trusted directory", name)
            except BaseException:
                os.close(fd)
                raise
            return fd
    raise OSError(errno.EEXIST, "ancestry segment creation raced repeatedly", name)


def _walk_dir_ancestry(path: Path, *, create: bool) -> int | None:
    """Walk every ancestor segment of ``path`` from the filesystem root by
    directory descriptor (POSIX only), following no symlink/reparse point
    anywhere in the chain and rejecting any ``.``/``..`` segment.
    ``create=True`` creates missing segments fresh as private ``0700``
    directories, always returning an owned descriptor for the final
    directory on POSIX (or falling back to plain
    ``Path.mkdir(parents=True)`` and returning ``None`` on non-POSIX).
    ``create=False`` never creates anything and returns ``None`` -- not
    an exception -- the moment any segment (including the final one) is
    simply absent; a symlink/reparse point anywhere in an EXISTING
    prefix still raises, and non-POSIX always returns ``None`` (no
    descriptor is available there either way). The one shared ancestry
    primitive for both the per-study/registry lock parent and every
    intake workspace read/scan/create path (INTAKE_DIR, OUTPUT_DIR,
    study, component, nested, and intake-owned audit directories)."""
    resolved = Path(os.fspath(path))
    if not resolved.is_absolute():
        raise OSError(errno.EINVAL, "directory ancestry walk requires an absolute path", str(path))
    parts = resolved.parts
    if any(part in (".", "..") for part in parts[1:]):
        raise OSError(errno.EINVAL, "directory ancestry walk rejects '.'/'..' segments", str(path))

    required_flags = ("O_DIRECTORY", "O_NOFOLLOW")
    if os.name != "posix" or any(not hasattr(os, flag) for flag in required_flags):
        if create:
            resolved.mkdir(parents=True, exist_ok=True)
        return None

    try:
        current = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        if not create:
            return None
        raise
    owns_current = False
    try:
        for part in parts[1:]:
            nxt = _open_dir_ancestry_segment(current, part, create=create)
            os.close(current)
            owns_current = False
            if nxt is None:
                return None
            current = nxt
            owns_current = True
        return current if owns_current else os.dup(current)
    except BaseException:
        if owns_current:
            os.close(current)
        raise


def _create_dir_ancestry(path: Path) -> int | None:
    """Walk every ancestor segment of ``path`` from the filesystem root by
    directory descriptor, creating missing segments fresh as private
    ``0700`` directories. See :func:`_walk_dir_ancestry` for the full
    contract; this is its ``create=True`` specialization, used for every
    workspace directory this package ever creates."""
    return _walk_dir_ancestry(path, create=True)


def _read_dir_ancestry(path: Path) -> int | None:
    """Like :func:`_create_dir_ancestry` but never creates anything:
    returns ``None`` (not an exception) the moment any ancestry segment,
    including the final one, is simply absent -- while still rejecting a
    symlink/reparse-point ancestor anywhere in an existing prefix. Used
    for every workspace directory this package only ever reads or
    scans."""
    return _walk_dir_ancestry(path, create=False)


def _open_lock_parent(lock_dir: Path) -> tuple[int | None, os.stat_result]:
    descriptor = _create_dir_ancestry(lock_dir)
    if descriptor is None:
        path_info = os.lstat(lock_dir)
        _validate_lock_parent_info(path_info, lock_dir)
        return None, path_info

    try:
        descriptor_info = os.fstat(descriptor)
        _validate_lock_parent_info(descriptor_info, lock_dir)
        if not hasattr(os, "fchmod"):
            raise OSError(errno.ENOTSUP, "secure pipeline lock parent mode is unavailable")
        os.fchmod(descriptor, 0o700)
        descriptor_info = os.fstat(descriptor)
        if stat.S_IMODE(descriptor_info.st_mode) != 0o700:
            raise PermissionError(errno.EPERM, "pipeline lock parent mode is not private", lock_dir)
        return descriptor, descriptor_info
    except BaseException:
        os.close(descriptor)
        raise


def _validate_lock_file_info(info: os.stat_result, lock_path: Path) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or _is_reparse_point(info)
        or info.st_nlink != 1
    ):
        raise OSError(errno.EPERM, "pipeline lock entry is not a private regular file", lock_path)
    if os.name == "posix" and hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise PermissionError(errno.EPERM, "pipeline lock entry has an unexpected owner", lock_path)


def _open_lock_file(lock_path: Path, parent_descriptor: int | None) -> int:
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    if parent_descriptor is not None:
        if os.open not in os.supports_dir_fd:
            raise OSError(errno.ENOTSUP, "descriptor-relative pipeline lock open is unavailable")
        descriptor = os.open(lock_path.name, flags, 0o600, dir_fd=parent_descriptor)
    else:
        descriptor = os.open(lock_path, flags, 0o600)
    try:
        descriptor_info = os.fstat(descriptor)
        _validate_lock_file_info(descriptor_info, lock_path)
        if os.name == "posix":
            if not hasattr(os, "fchmod"):
                raise OSError(errno.ENOTSUP, "secure pipeline lock file mode is unavailable")
            os.fchmod(descriptor, 0o600)
            descriptor_info = os.fstat(descriptor)
            _validate_lock_file_info(descriptor_info, lock_path)
            if stat.S_IMODE(descriptor_info.st_mode) != 0o600:
                raise PermissionError(
                    errno.EPERM,
                    "pipeline lock file mode is not private",
                    lock_path,
                )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _canonical_entry_info(
    lock_path: Path,
    parent_descriptor: int | None,
) -> os.stat_result:
    if parent_descriptor is not None:
        if os.stat not in os.supports_dir_fd or os.stat not in os.supports_follow_symlinks:
            raise OSError(errno.ENOTSUP, "secure pipeline lock stat is unavailable")
        return os.stat(
            lock_path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    return os.stat(lock_path, follow_symlinks=False)


def _verify_canonical_entry(
    descriptor: int,
    lock_path: Path,
    parent_descriptor: int | None,
    parent_info: os.stat_result,
) -> None:
    current_parent = (
        os.fstat(parent_descriptor)
        if parent_descriptor is not None
        else os.lstat(lock_path.parent)
    )
    _validate_lock_parent_info(current_parent, lock_path.parent)
    if not _same_file_identity(parent_info, current_parent):
        raise OSError(errno.ESTALE, "pipeline lock parent identity changed", lock_path.parent)

    descriptor_info = os.fstat(descriptor)
    entry_info = _canonical_entry_info(lock_path, parent_descriptor)
    _validate_lock_file_info(descriptor_info, lock_path)
    _validate_lock_file_info(entry_info, lock_path)
    if not _same_file_identity(descriptor_info, entry_info):
        raise OSError(errno.ESTALE, "pipeline lock canonical entry changed", lock_path)


def _write_lock_metadata(descriptor: int, study_name: str) -> None:
    payload = (
        json.dumps(
            {"pid": os.getpid(), "study": study_name},
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.ftruncate(descriptor, 0)
    written = 0
    while written < len(payload):
        written += os.write(descriptor, payload[written:])
    os.fsync(descriptor)


def _discard_inherited_owners_locked(current_pid: int) -> None:
    inherited_paths = [
        path for path, owner in _LOCK_OWNERS.items() if owner.pid != current_pid
    ]
    for path in inherited_paths:
        owner = _LOCK_OWNERS.pop(path)
        with contextlib.suppress(OSError):
            os.close(owner.descriptor)


def _try_advisory_lock(descriptor: int, lock_path: Path) -> None:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "posix":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        else:
            raise OSError(f"unsupported platform for pipeline locking: {os.name}")
    except OSError as exc:
        if isinstance(exc, BlockingIOError) or exc.errno in {
            errno.EACCES,
            errno.EAGAIN,
            getattr(errno, "EDEADLK", -1),
        }:
            raise PipelineBusyError(lock_path) from exc
        raise


def _acquire_lock_descriptor(lock_path: Path) -> int:
    """Open, advisory-lock, and canonical-identity-verify the descriptor at
    ``lock_path``. Shared core for both the per-study lock and the fixed
    intake-registry lock -- identical security properties, parameterized
    only by path. Caller writes metadata and registers ownership."""
    parent_descriptor: int | None = None
    descriptor: int | None = None
    try:
        parent_descriptor, parent_info = _open_lock_parent(lock_path.parent)
        descriptor = _open_lock_file(lock_path, parent_descriptor)
        _try_advisory_lock(descriptor, lock_path)
        _verify_canonical_entry(descriptor, lock_path, parent_descriptor, parent_info)
        return descriptor
    except BaseException:
        if descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        raise
    finally:
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def _register_new_owner(canonical_path: Path, lock_path: Path, descriptor: int, pid: int, thread_id: int, label: str) -> None:
    """Caller already holds ``_LOCK_OWNERS_GUARD`` and has confirmed
    ``canonical_path`` is not currently owned. Writes metadata, and on any
    failure closes the descriptor without registering ownership."""
    try:
        _write_lock_metadata(descriptor, label)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        raise
    _LOCK_OWNERS[canonical_path] = _LockOwner(path=lock_path, descriptor=descriptor, pid=pid, thread_id=thread_id)


def _release_owned_lock(canonical_path: Path) -> None:
    """Shared release core: close this invocation's descriptor for
    ``canonical_path`` without unlinking the lock inode. No-op if this
    invocation does not currently own it."""
    pid = os.getpid()
    thread_id = threading.get_ident()
    with _LOCK_OWNERS_GUARD:
        _discard_inherited_owners_locked(pid)
        owner = _LOCK_OWNERS.get(canonical_path)
        if owner is None:
            return
        if owner.pid != pid or owner.thread_id != thread_id:
            raise RuntimeError("lock can only be released by its owning invocation")
        try:
            os.close(owner.descriptor)
        except OSError:
            try:
                os.fstat(owner.descriptor)
            except OSError as state_error:
                if state_error.errno == errno.EBADF:
                    _LOCK_OWNERS.pop(canonical_path, None)
            raise
        else:
            _LOCK_OWNERS.pop(canonical_path, None)


def acquire_pipeline_lock(study: str | None = None) -> None:
    """Immediately acquire one non-reentrant per-study OS advisory lock."""
    study_name = _validated_study_name(study)
    lock_path = lock_path_for(study_name)
    canonical_path = _canonical_lock_path(lock_path)
    pid = os.getpid()
    thread_id = threading.get_ident()

    with _LOCK_OWNERS_GUARD:
        _discard_inherited_owners_locked(pid)
        if canonical_path in _LOCK_OWNERS:
            raise PipelineBusyError(lock_path)
        descriptor = _acquire_lock_descriptor(lock_path)
        _register_new_owner(canonical_path, lock_path, descriptor, pid, thread_id, study_name)


# Fixed, workspace-wide lock guarding the intake registry (generated-manifest
# scans, name reservation, promotion, reconciliation). Not per-study: the
# label contains an underscore, a character ``_STUDY_NAME_PATTERN`` never
# accepts, so no valid study name can ever collide with this lock's path.
_INTAKE_REGISTRY_LOCK_LABEL = "__intake_registry__"


def intake_registry_lock_path() -> Path:
    """Return the fixed intake-registry advisory lock path, without creating it."""
    return Path(config.TMP_DIR) / f".{_INTAKE_REGISTRY_LOCK_LABEL}.pipeline.lock"


def acquire_intake_registry_lock() -> None:
    """Immediately acquire the single, fixed, workspace-wide intake-registry
    advisory lock. Not per-study, not reentrant. Callers performing
    generated-manifest scans, name reservation, promotion, or reconciliation
    across the intake registry MUST hold this before touching any study's
    intake state, and MUST acquire it before any per-study
    ``acquire_pipeline_lock`` (registry-then-study order, always)."""
    lock_path = intake_registry_lock_path()
    canonical_path = _canonical_lock_path(lock_path)
    pid = os.getpid()
    thread_id = threading.get_ident()

    with _LOCK_OWNERS_GUARD:
        _discard_inherited_owners_locked(pid)
        if canonical_path in _LOCK_OWNERS:
            raise PipelineBusyError(lock_path)
        descriptor = _acquire_lock_descriptor(lock_path)
        _register_new_owner(canonical_path, lock_path, descriptor, pid, thread_id, _INTAKE_REGISTRY_LOCK_LABEL)


def release_intake_registry_lock() -> None:
    """Release this invocation's intake-registry descriptor without unlinking
    the lock inode."""
    _release_owned_lock(_canonical_lock_path(intake_registry_lock_path()))


@contextlib.contextmanager
def intake_registry_lock() -> Iterator[None]:
    """Hold the fixed intake-registry descriptor for the complete context."""
    acquire_intake_registry_lock()
    try:
        yield
    finally:
        release_intake_registry_lock()


def release_pipeline_lock(study: str | None = None) -> None:
    """Release this invocation's descriptor without unlinking the lock inode."""
    pid = os.getpid()
    thread_id = threading.get_ident()
    with _LOCK_OWNERS_GUARD:
        _discard_inherited_owners_locked(pid)
        if study is None:
            owned_paths = [
                path
                for path, owner in _LOCK_OWNERS.items()
                if owner.pid == pid and owner.thread_id == thread_id
            ]
            if not owned_paths:
                return
            if len(owned_paths) != 1:
                raise RuntimeError("release requires an explicit study when multiple locks are owned")
            canonical_path = owned_paths[0]
        else:
            canonical_path = _canonical_lock_path(lock_path_for(study))

        owner = _LOCK_OWNERS.get(canonical_path)
        if owner is None:
            return
        if owner.pid != pid or owner.thread_id != thread_id:
            raise RuntimeError("pipeline lock can only be released by its owning invocation")

        try:
            os.close(owner.descriptor)
        except OSError:
            try:
                os.fstat(owner.descriptor)
            except OSError as state_error:
                if state_error.errno == errno.EBADF:
                    _LOCK_OWNERS.pop(canonical_path, None)
            raise
        else:
            _LOCK_OWNERS.pop(canonical_path, None)


def _before_fork() -> None:
    _LOCK_OWNERS_GUARD.acquire()


def _after_fork_in_parent() -> None:
    _LOCK_OWNERS_GUARD.release()


def _after_fork_in_child() -> None:
    try:
        for owner in _LOCK_OWNERS.values():
            with contextlib.suppress(OSError):
                os.close(owner.descriptor)
        _LOCK_OWNERS.clear()
    finally:
        _LOCK_OWNERS_GUARD.release()


if os.name == "posix" and hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_before_fork,
        after_in_parent=_after_fork_in_parent,
        after_in_child=_after_fork_in_child,
    )


@contextlib.contextmanager
def pipeline_lock(study: str | None = None) -> Iterator[None]:
    """Hold the per-study descriptor for the complete context."""
    acquire_pipeline_lock(study)
    try:
        yield
    finally:
        release_pipeline_lock(study)
