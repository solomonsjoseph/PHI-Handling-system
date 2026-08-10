"""Path sanitisation helpers.

Every user-controlled path fragment must go through :func:`safe_join` before
touching disk. This closes the SEC-001 arbitrary-file-overwrite vector where
a filename like `../../.env` would let a caller drop content anywhere on the
filesystem.

Rules enforced (fail closed on violation):

* filename must not be empty or ``"."``/``".."``
* filename must not be absolute (``/``, ``C:\\`` etc.)
* filename must not contain a path separator (``/`` or ``\\``)
* filename must not contain NUL
* the resolved final path must be a *strict* descendant of ``base``
"""
from __future__ import annotations

import os
from pathlib import Path


class UnsafePath(ValueError):
    """Raised when a user-supplied name would escape the base directory."""


_FORBIDDEN_NAMES = {"", ".", ".."}


def sanitise_filename(name: str | None, *, fallback: str = "upload.bin") -> str:
    """Return a filename that is safe to append to a directory.

    Strips any directory component, rejects traversal tokens and NUL, and
    replaces the whole thing with ``fallback`` when the input is empty.
    """
    if not name:
        return fallback
    # Cross-platform basename: reject anything that looks path-like.
    if "\x00" in name:
        raise UnsafePath("filename contains NUL byte")
    # Reject absolute paths (Unix and Windows) up front.
    if name.startswith("/") or name.startswith("\\"):
        raise UnsafePath(f"filename must not be absolute: {name!r}")
    if len(name) >= 2 and name[1] == ":":
        raise UnsafePath(f"filename must not be a Windows drive path: {name!r}")
    # Reject any embedded separator - the client should upload just the leaf name.
    if "/" in name or "\\" in name:
        raise UnsafePath(f"filename must not contain path separators: {name!r}")
    if name in _FORBIDDEN_NAMES:
        raise UnsafePath(f"filename not allowed: {name!r}")
    return name


def safe_join(base: Path, user_name: str | None, *, fallback: str = "upload.bin") -> Path:
    """Join ``user_name`` onto ``base`` and enforce strict containment.

    Returns the absolute joined path when it is a descendant of ``base``.
    Raises :class:`UnsafePath` on any traversal or root escape.
    """
    clean = sanitise_filename(user_name, fallback=fallback)
    base_resolved = base.resolve()
    candidate = (base_resolved / clean).resolve()
    # `Path.is_relative_to` is available in 3.9+; use ``os.path.commonpath`` as a
    # portable safety net that also refuses paths that only share a prefix
    # (e.g. ``/tmp/foo`` vs ``/tmp/foobar``).
    try:
        common = os.path.commonpath([str(candidate), str(base_resolved)])
    except ValueError:
        raise UnsafePath(f"filename resolves outside base: {user_name!r}")
    if common != str(base_resolved) or candidate == base_resolved:
        raise UnsafePath(f"filename resolves outside base: {user_name!r}")
    return candidate


DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
UPLOAD_DIR = DATA_DIR / "uploads"
EXPORT_DIR = DATA_DIR / "exports"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
EXPORT_DIR.mkdir(parents=True, exist_ok=True)
