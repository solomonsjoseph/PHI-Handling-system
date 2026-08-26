"""Shared, fail-closed atomic no-replace rename primitive.

Extracted from ``phi_engine.pipeline.intake`` (which used this exact
Linux ``renameat2(2)`` ``RENAME_NOREPLACE`` call for its own promotion
and quarantine transactions) so every caller that needs the same
create-or-fail publication guarantee -- intake's study-directory
promotion, and the XLS isolation boundary's bundle publication -- goes
through one primitive instead of two independently-maintained copies
of the same ``ctypes`` syscall wiring.

The kernel performs the rename or fails with ``EEXIST`` atomically;
there is no window between an existence check and the rename itself
for a hostile same-namespace actor to race a fresh node into the
destination name. This is Linux-only: no portable equivalent gives the
same atomic no-replace guarantee, so callers on any other platform (or
where the syscall itself is unavailable) must fail closed via
:class:`AtomicRenameUnavailable` rather than silently falling back to
a racy check-then-rename.
"""

from __future__ import annotations

import ctypes
import errno
import os
import sys
from typing import Any

__all__ = ["AtomicRenameUnavailable", "renameat2_noreplace"]


class AtomicRenameUnavailable(Exception):
    """Value-free: this platform, or this runtime's ``libc``, cannot
    provide the atomic no-replace rename guarantee. Every caller must
    treat this as fail-closed -- never substitute a check-then-rename
    fallback, which would reopen exactly the race this primitive
    exists to close."""


_RENAMEAT2_UNSUPPORTED = object()
_renameat2_fn: Any = None
_RENAME_NOREPLACE = 0x1


def _load_renameat2() -> Any:
    """Resolve libc's ``renameat2(2)`` once, or the sentinel
    ``_RENAMEAT2_UNSUPPORTED`` when this platform cannot provide it."""
    global _renameat2_fn
    if _renameat2_fn is not None:
        return _renameat2_fn
    if not sys.platform.startswith("linux"):
        _renameat2_fn = _RENAMEAT2_UNSUPPORTED
        return _renameat2_fn
    fn = None
    for loader in (lambda: ctypes.CDLL("libc.so.6", use_errno=True), lambda: ctypes.CDLL(None, use_errno=True)):
        try:
            fn = loader().renameat2
            break
        except (OSError, AttributeError):
            continue
    if fn is None:
        _renameat2_fn = _RENAMEAT2_UNSUPPORTED
        return _renameat2_fn
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    _renameat2_fn = fn
    return _renameat2_fn


def renameat2_noreplace(old_dir_fd: int, old: str, new_dir_fd: int, new: str) -> None:
    """Atomic, no-replace rename via the Linux ``renameat2(2)`` syscall
    (``RENAME_NOREPLACE``). Raises ``FileNotFoundError``/``FileExistsError``
    for those specific fixed outcomes, a generic ``OSError`` for any
    other errno, and :class:`AtomicRenameUnavailable` -- fail-closed,
    never a check-then-rename fallback -- when this platform cannot
    provide the guarantee at all."""
    fn = _load_renameat2()
    if fn is _RENAMEAT2_UNSUPPORTED:
        raise AtomicRenameUnavailable()
    result = fn(
        ctypes.c_int(old_dir_fd),
        os.fsencode(old),
        ctypes.c_int(new_dir_fd),
        os.fsencode(new),
        ctypes.c_uint(_RENAME_NOREPLACE),
    )
    if result == 0:
        return
    err = ctypes.get_errno()
    if err == errno.ENOENT:
        raise FileNotFoundError(err, os.strerror(err))
    if err == errno.EEXIST:
        raise FileExistsError(err, os.strerror(err))
    raise OSError(err, os.strerror(err))


def _reset_cache_for_tests() -> None:
    """Test-only: clear the cached resolved symbol so a test can force
    re-resolution (e.g. under a monkeypatched ``sys.platform``)."""
    global _renameat2_fn
    _renameat2_fn = None
