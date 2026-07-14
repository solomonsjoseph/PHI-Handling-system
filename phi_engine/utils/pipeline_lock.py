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
import re
import stat
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import phi_engine.config.config as config

__all__ = [
    "PipelineBusyError",
    "acquire_pipeline_lock",
    "held_lock_path",
    "is_locally_held",
    "lock_path_for",
    "pipeline_lock",
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


_STUDY_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII)
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


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
    study_name = config.STUDY_NAME if study is None else study
    if (
        not isinstance(study_name, str)
        or _STUDY_NAME_PATTERN.fullmatch(study_name) is None
        or study_name.endswith(".")
        or study_name.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
    ):
        raise ValueError("study must be a plain folder name, not a path")
    return study_name


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


def _open_lock_parent(lock_dir: Path) -> tuple[int | None, os.stat_result]:
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path_info = os.lstat(lock_dir)
    _validate_lock_parent_info(path_info, lock_dir)

    # Windows' os.open cannot portably open a directory descriptor. Keep its
    # immediate file-lock branch portable, but do not claim chmod provides a
    # private ACL. Reparse/type and canonical-identity checks still fail closed.
    if os.name != "posix":
        return None, path_info

    required_flags = ("O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, flag) for flag in required_flags):
        raise OSError(errno.ENOTSUP, "secure pipeline lock directory open is unavailable")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(lock_dir, flags)
    try:
        descriptor_info = os.fstat(descriptor)
        _validate_lock_parent_info(descriptor_info, lock_dir)
        if not _same_file_identity(path_info, descriptor_info):
            raise OSError(errno.ESTALE, "pipeline lock parent changed during open", lock_dir)
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

        parent_descriptor: int | None = None
        descriptor: int | None = None
        try:
            parent_descriptor, parent_info = _open_lock_parent(lock_path.parent)
            descriptor = _open_lock_file(lock_path, parent_descriptor)
            _try_advisory_lock(descriptor, lock_path)
            _verify_canonical_entry(
                descriptor,
                lock_path,
                parent_descriptor,
                parent_info,
            )
            _write_lock_metadata(descriptor, study_name)
        except BaseException:
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            raise
        finally:
            if parent_descriptor is not None:
                os.close(parent_descriptor)

        _LOCK_OWNERS[canonical_path] = _LockOwner(
            path=lock_path,
            descriptor=descriptor,
            pid=pid,
            thread_id=thread_id,
        )


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
