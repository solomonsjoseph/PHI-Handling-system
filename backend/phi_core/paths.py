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
        raise UnsafePath(f"filename resolves outside base: {user_name!r}") from None
    if common != str(base_resolved) or candidate == base_resolved:
        raise UnsafePath(f"filename resolves outside base: {user_name!r}")
    return candidate


DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
UPLOAD_DIR = DATA_DIR / "uploads"
EXPORT_DIR = DATA_DIR / "exports"
CHATGPT_TOKEN_DIR = DATA_DIR / "chatgpt"
for _d in (UPLOAD_DIR, EXPORT_DIR, CHATGPT_TOKEN_DIR):
    _d.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(_d, 0o700)


def cleanup_session_unpacked(sid: str) -> None:
    """Delete the hydrated raw-file tree for a settled session.

    Called once a pipeline run reaches a terminal status (complete,
    failed, cancelled, blocked). ``unpacked/`` holds the per-file
    decrypted PHI the Executor read from; once the run has settled there
    is nothing left that needs it. ``intake.zip`` is deliberately left
    alone here: the operator-upload path already unlinks it right after
    ``build_manifest`` hydrates the manifest, and the corpus generator
    path keeps it so `GET /api/corpus/study/{sid}/zip` can still serve
    the reproducible input after the run completes.
    """
    import shutil
    shutil.rmtree(UPLOAD_DIR / sid / "unpacked", ignore_errors=True)
