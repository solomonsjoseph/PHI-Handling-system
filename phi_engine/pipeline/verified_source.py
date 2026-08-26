"""Fail-closed, descriptor-verified access to files under an intake source root.

Single primitive reused by organizer snapshots, preflight hashing, workbook
metadata inspection, and AI-support extraction. ``open_verified_source``
pins the source root by walking every ancestor path segment with
``O_DIRECTORY | O_NOFOLLOW`` (not just the final component), then resolves
``<source_root>/<relative_path>`` relative to that pinned descriptor by
walking each remaining segment the same way and opening the final regular
file with ``O_RDONLY | O_NOFOLLOW | O_NONBLOCK``; on Linux it opportunistically
tries the ``openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS)`` syscall first for
the post-root portion and always falls back to the portable descriptor walk,
which alone determines the fixed reason code on any failure. No source path,
content, or raw exception text ever escapes this module -- only fixed reason
strings.

A caller that must open many files under the same root without letting the
root identity drift between opens (multi-file traversal, not a single
lookup) should pin the root once with the private ``_open_pinned_root`` /
``_open_from_root_fd`` pair rather than calling the public
``open_verified_source`` once per file -- the public entry point re-pins the
root from the supplied pathname on every call, which is correct for a single
lookup but does not hold identity across a whole traversal.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import ContextManager, Iterator

__all__ = ["FileIdentity", "VerifiedSourceError", "open_verified_source"]


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int


class VerifiedSourceError(Exception):
    """Fixed-reason failure raised by :func:`open_verified_source`.

    ``reason`` is always one of the fixed codes below -- never raw path,
    content, or exception text.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _identity(info: os.stat_result) -> FileIdentity:
    return FileIdentity(device=info.st_dev, inode=info.st_ino, size=info.st_size, mtime_ns=info.st_mtime_ns)


def _validate_relative_path(relative_path: str) -> tuple[str, ...]:
    """Validate the raw string before any path-library normalization.

    Rejects NUL (which would silently truncate a C string passed to the
    openat2 syscall), absolute paths, and every empty/``.``/``..`` segment --
    including ones hidden by a doubled separator or a trailing slash. This
    runs identically ahead of both the openat2 fast path and the portable
    descriptor-walk fallback, so the two can never accept/reject different
    logical inputs.
    """

    if not isinstance(relative_path, str) or not relative_path:
        raise VerifiedSourceError("source-target-outside-root")
    if "\x00" in relative_path:
        raise VerifiedSourceError("source-target-outside-root")
    if relative_path.startswith("/"):
        raise VerifiedSourceError("source-target-outside-root")
    segments = relative_path.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise VerifiedSourceError("source-target-outside-root")
    return tuple(segments)


def _map_file_open_errno(exc: OSError) -> str:
    if exc.errno == errno.ELOOP:
        return "source-symlink-not-allowed"
    return "source-unreadable"


def _map_dir_open_errno(exc: OSError) -> str:
    if exc.errno == errno.ELOOP:
        return "source-symlink-not-allowed"
    return "source-unreadable"


_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


def _require_capabilities() -> None:
    """Fail closed rather than silently degrade on a platform that lacks the
    primitives this module's fail-closed guarantee depends on.
    """

    if os.open not in os.supports_dir_fd:
        raise VerifiedSourceError("source-unreadable")
    if os.stat not in os.supports_dir_fd or os.stat not in os.supports_follow_symlinks:
        raise VerifiedSourceError("source-unreadable")
    if _O_NOFOLLOW == 0 or _O_DIRECTORY == 0 or _O_NONBLOCK == 0:
        raise VerifiedSourceError("source-unreadable")


def _classify_component(name: str, parent_fd: int) -> str | None:
    """Best-effort, race-prone-but-precise pre-check: distinguish a symlink
    component from a merely-non-directory one before attempting to open it,
    so the fixed reason is accurate rather than inferred from an ambiguous
    errno. Returns None (proceed to the real open, which is authoritative
    for the remaining TOCTOU window) when the entry cannot even be lstat'd.
    """

    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except (OSError, TypeError, NotImplementedError):
        return None
    if stat.S_ISLNK(info.st_mode):
        return "source-symlink-not-allowed"
    return None


def _open_dir_relative(name: str, parent_fd: int) -> int:
    flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
    reason = _classify_component(name, parent_fd)
    if reason is not None:
        raise VerifiedSourceError(reason)
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise VerifiedSourceError(_map_dir_open_errno(exc)) from None


def _open_pinned_root(source: Path) -> int:
    """Open a no-follow descriptor for ``source``, walking every ancestor
    path segment (not just the final component) with
    ``O_DIRECTORY | O_NOFOLLOW`` from the filesystem root, so a symlink
    anywhere in the supplied ancestry -- not only in ``source`` itself --
    can never be silently followed. The caller owns and must close the
    returned descriptor.
    """

    _require_capabilities()
    try:
        raw = os.fspath(source)
    except TypeError:
        raise VerifiedSourceError("source-unreadable") from None
    if "\x00" in raw:
        raise VerifiedSourceError("source-target-outside-root")
    # Reject any ".." segment in the RAW supplied path before any lexical
    # normalization. os.path.abspath() collapses ".." by pure string
    # manipulation -- it does not walk the filesystem -- so a path like
    # "link/../pkg" would abspath() straight to ".../pkg" without ever
    # noticing "link" was a symlink component that must be rejected. The
    # descriptor walk below never sees the collapsed ".." at all in that
    # case, silently bypassing symlink-ancestry rejection. Refusing any
    # raw ".." segment up front closes that gap; the fixed-code reason
    # matches the existing outside-root-escape vocabulary.
    if any(segment == ".." for segment in raw.split("/") if segment):
        raise VerifiedSourceError("source-target-outside-root")
    try:
        abs_path = os.path.abspath(raw)
    except (OSError, ValueError):
        raise VerifiedSourceError("source-unreadable") from None
    segments = [segment for segment in abs_path.split("/") if segment]

    flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
    try:
        fd = os.open("/", flags)
    except OSError as exc:
        raise VerifiedSourceError(_map_dir_open_errno(exc)) from None
    for segment in segments:
        try:
            next_fd = _open_dir_relative(segment, fd)
        except VerifiedSourceError:
            os.close(fd)
            raise
        os.close(fd)
        fd = next_fd
    return fd


def _walk_open(root_fd: int, parts: tuple[str, ...]) -> int:
    parent_fd = root_fd
    owns_parent = False
    try:
        for name in parts[:-1]:
            child_fd = _open_dir_relative(name, parent_fd)
            if owns_parent:
                os.close(parent_fd)
            parent_fd = child_fd
            owns_parent = True

        final_name = parts[-1]
        reason = _classify_component(final_name, parent_fd)
        if reason is not None:
            raise VerifiedSourceError(reason)
        # O_NONBLOCK prevents the open() itself from hanging forever on a
        # hostile FIFO; it has no effect on ordinary regular-file reads.
        flags = os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC
        try:
            return os.open(final_name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise VerifiedSourceError(_map_file_open_errno(exc)) from None
    finally:
        if owns_parent:
            os.close(parent_fd)


# --- Optional Linux openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS) fast path ---
# Purely opportunistic: on ANY failure (unsupported platform/arch, syscall
# unavailable, or the resolution itself failing for whatever reason) this
# silently yields None and the caller falls back to the portable descriptor
# walk above, which independently determines the fail-closed reason code.
# openat2 never decides a reason code on its own. Only used for the portion
# of the path relative to an already-pinned root_fd -- it never participates
# in pinning the root itself.

_OPENAT2_SYSCALL_NUMBERS = {"x86_64": 437, "aarch64": 437}
_RESOLVE_BENEATH = 0x08
_RESOLVE_NO_SYMLINKS = 0x04


class _OpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


def _try_openat2(root_fd: int, parts: tuple[str, ...]) -> int | None:
    if platform.system() != "Linux":
        return None
    syscall_no = _OPENAT2_SYSCALL_NUMBERS.get(platform.machine())
    if syscall_no is None:
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        libc.syscall.argtypes = [ctypes.c_long, ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t]
        how = _OpenHow(
            flags=os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC,
            mode=0,
            resolve=_RESOLVE_BENEATH | _RESOLVE_NO_SYMLINKS,
        )
        path_bytes = "/".join(parts).encode("utf-8")
        result = libc.syscall(syscall_no, root_fd, path_bytes, ctypes.byref(how), ctypes.sizeof(how))
    except Exception:
        return None
    if result < 0:
        return None
    return result


def _open_relative_fd(root_fd: int, parts: tuple[str, ...]) -> int:
    fast_fd = _try_openat2(root_fd, parts)
    if fast_fd is not None:
        return fast_fd
    return _walk_open(root_fd, parts)


@contextmanager
def _open_verified_core(fd: int, expected_identity: FileIdentity | None) -> Iterator[int]:
    """Shared verification/yield/cleanup core: given an already-opened file
    descriptor (from either the public root-pathname entry point or a
    private parent-fd-relative one), validate it is a regular file,
    optionally check it against a known-good identity, yield it, and --
    regardless of whether the caller's own code raised -- compare its
    identity again before returning. Always closes ``fd``; a close failure
    never masks a security-significant result already determined.
    """

    try:
        try:
            info = os.fstat(fd)
        except OSError:
            raise VerifiedSourceError("source-unreadable") from None
        if not stat.S_ISREG(info.st_mode):
            raise VerifiedSourceError("source-unreadable")
        baseline = _identity(info)
        if expected_identity is not None and baseline != expected_identity:
            raise VerifiedSourceError("source-unreadable")
        try:
            yield fd
        finally:
            try:
                post = os.fstat(fd)
                mismatched = _identity(post) != baseline
            except OSError:
                mismatched = True
            if mismatched:
                # `from None` suppresses implicit chaining: without it, a
                # consumer exception raised inside the `with` block (or a
                # raw filesystem exception) would be attached as
                # __context__ and rendered ("During handling of the above
                # exception...") alongside this fixed-code reason,
                # potentially leaking source content/paths embedded in the
                # suppressed exception's own message into logs/tracebacks.
                raise VerifiedSourceError("source-unreadable") from None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _open_from_root_fd(
    root_fd: int,
    relative_path: str,
    *,
    required_source_component: str | None = None,
    expected_identity: FileIdentity | None = None,
) -> ContextManager[int]:
    """Private counterpart of :func:`open_verified_source` for a caller that
    already holds a pinned root descriptor (see :func:`_open_pinned_root`)
    and must open many files under it without ever re-touching the root
    pathname between opens. Never closes ``root_fd`` -- the caller owns it.
    """

    parts = _validate_relative_path(relative_path)
    if required_source_component is not None and parts[0] != required_source_component:
        raise VerifiedSourceError("source-target-outside-root")
    fd = _open_relative_fd(root_fd, parts)
    return _open_verified_core(fd, expected_identity)


def _open_from_parent_fd(
    parent_fd: int,
    name: str,
    *,
    expected_identity: FileIdentity | None = None,
) -> ContextManager[int]:
    """Open a single entry ``name`` directly under ``parent_fd`` -- a
    directory descriptor the caller already holds from its own discovery
    step (e.g. an ``os.scandir(parent_fd)`` result) -- and reuse
    :func:`_open_verified_core`'s identity pre/post-check plumbing.

    Unlike :func:`_open_from_root_fd`, this never walks a multi-segment
    path and never reopens anything from a root: ``parent_fd`` IS the
    already-verified location, held continuously since it was discovered,
    so there is no window in which an unrelated rename/replace elsewhere in
    the tree could substitute a different object for this lookup.
    """

    if "\x00" in name or "/" in name or name in ("", ".", ".."):
        raise VerifiedSourceError("source-target-outside-root")
    reason = _classify_component(name, parent_fd)
    if reason is not None:
        raise VerifiedSourceError(reason)
    flags = os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise VerifiedSourceError(_map_file_open_errno(exc)) from None
    return _open_verified_core(fd, expected_identity)


def _open_dir_from_parent_fd(parent_fd: int, name: str, expected_identity: FileIdentity) -> int:
    """Open subdirectory ``name`` directly under ``parent_fd``, verify it is
    a symlink-free directory, and require its freshly-opened identity to
    match ``expected_identity`` (captured from the caller's own discovery
    step, e.g. a no-follow ``DirEntry.stat()``) -- closing the race between
    listing a directory and actually descending into it. The caller owns
    and must close the returned descriptor; on any failure this function
    closes what it opened itself and raises.
    """

    reason = _classify_component(name, parent_fd)
    if reason is not None:
        raise VerifiedSourceError(reason)
    flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise VerifiedSourceError(_map_dir_open_errno(exc)) from None
    try:
        try:
            info = os.fstat(fd)
        except OSError:
            raise VerifiedSourceError("source-unreadable") from None
        if not stat.S_ISDIR(info.st_mode):
            raise VerifiedSourceError("source-unreadable")
        if _identity(info) != expected_identity:
            raise VerifiedSourceError("source-unreadable")
    except VerifiedSourceError:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return fd


def _recheck_dir_identity(fd: int, expected_identity: FileIdentity) -> bool:
    """True if ``fd`` (an already-open, caller-owned directory descriptor)
    still matches ``expected_identity`` -- used to detect a directory being
    replaced/mutated while its children were being traversed. Never raises;
    an unreadable descriptor is treated as a mismatch (fail closed).
    """

    try:
        info = os.fstat(fd)
    except OSError:
        return False
    return _identity(info) == expected_identity


@contextmanager
def open_verified_source(
    source_root: Path,
    relative_path: str,
    *,
    required_source_component: str | None = None,
    expected_identity: FileIdentity | None = None,
) -> ContextManager[int]:
    """Yield a verified, symlink-free, read-only file descriptor.

    ``relative_path`` is always source-root-relative. ``source_root`` itself
    is pinned by walking every ancestor path segment (not just its final
    component) with ``O_NOFOLLOW``, then ``<source_root>/<relative_path>`` is
    resolved the same way, so no symlink or reparse point anywhere in either
    the source root's own ancestry or the relative path can be followed --
    even one whose target remains inside ``source_root``.

    A baseline identity is captured immediately after opening. If
    ``expected_identity`` is supplied it must match that baseline before the
    descriptor is ever yielded. Regardless, the baseline is compared again
    once the caller is done with the descriptor (in a ``finally``, so this
    still fires even if the caller's own code raised) -- any drift fails
    closed with the same fixed ``source-unreadable`` reason used for any
    other unreadable/untrustworthy source, whether or not the caller raised
    something else in the meantime.

    This entry point re-pins the root from ``source_root`` on every call; a
    caller opening many files under the same root across a single logical
    operation should hold one root descriptor instead (see the module
    docstring) so root identity cannot drift between those opens, and
    should prefer :func:`_open_from_parent_fd` against a still-held
    discovery-time parent descriptor for a multi-file traversal so no
    intermediate component is ever reopened by path either.

    Raises :class:`VerifiedSourceError` with a fixed reason code on any
    failure; never raw path/content/exception text.
    """

    parts = _validate_relative_path(relative_path)
    if required_source_component is not None and parts[0] != required_source_component:
        raise VerifiedSourceError("source-target-outside-root")

    root_fd = _open_pinned_root(source_root)
    try:
        fd = _open_relative_fd(root_fd, parts)
    finally:
        try:
            os.close(root_fd)
        except OSError:
            pass
    with _open_verified_core(fd, expected_identity) as verified_fd:
        yield verified_fd
