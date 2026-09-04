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
import re
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


# Resolved, not merely constructed. Every stored file path is built from
# DATA_DIR, and `control/execution_validators.PathPolicyValidator` refuses
# any stored path containing a '..' component, so an operator who exports
# DATA_DIR=/srv/app/../data would see every run reach Executor and fail a
# traversal check on the service's own configuration. Normalising here
# keeps that check strict where it matters, on paths the service did not
# construct itself.
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
CHATGPT_TOKEN_DIR = DATA_DIR / "chatgpt"
# D14 artifact-registry roots. ``intake`` reuses ``UPLOAD_DIR`` rather than
# adding a redundant constant; every other ``ArtifactRecord.root`` literal
# gets its own directory here.
STAGING_DIR = DATA_DIR / "staging"
EVIDENCE_DIR = DATA_DIR / "evidence"
REVERSAL_DIR = DATA_DIR / "reversal"
PUBLISHED_DIR = DATA_DIR / "published"
CACHE_DIR = DATA_DIR / "cache"
# Phase 2A (docs/MASTER_ARCHITECTURE_V2.md #21, local reference doc, never
# committed): per-run isolated raw-processing workspace root. Overridable so
# a deployment can point it at ephemeral/encrypted storage distinct from
# DATA_DIR without code changes.
SANDBOX_DIR = Path(os.environ.get("PHI_SANDBOX_DIR", str(DATA_DIR / "sandbox")))
# Rewrite plan step 5: per-run host-side staging for ContainerRunner's
# hardened Docker boundary -- generated source files land here before
# being bind-mounted read-only into the container, and the container's
# tmpfs /workspace is bind-mounted from this same tree's "workspace"
# subdirectory so the host can read the result file back after exit.
# A distinct root from SANDBOX_DIR: that one is the multiprocessing
# path's own workspace family for trusted first-party helpers, this one
# is the container path's, for model-generated code.
CONTAINER_STAGING_DIR = Path(
    os.environ.get("PHI_CONTAINER_STAGING_DIR", str(DATA_DIR / "container_staging"))
)
for _d in (
    UPLOAD_DIR,
    CHATGPT_TOKEN_DIR,
    STAGING_DIR,
    EVIDENCE_DIR,
    REVERSAL_DIR,
    PUBLISHED_DIR,
    CACHE_DIR,
    SANDBOX_DIR,
    CONTAINER_STAGING_DIR,
):
    _d.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(_d, 0o700)


# Run-scoped artifact directories are keyed by ``session_id``/``run_id``, both
# always internally generated ``uuid4().hex`` identifiers. Restricting them to
# that exact shape (rather than routing them through ``sanitise_filename``,
# which merely rejects traversal) means a run-scoped path component can never
# even superficially resemble a crafted name.
_ID_RE = re.compile(r"\A[0-9a-f]{8,64}\Z")


def is_safe_scoped_id(value: str | None) -> bool:
    """Whether ``value`` is a bare lowercase-hex identifier safe to use as
    a run-scoped directory component. Exposes ``_ID_RE``'s validation to
    callers (e.g. ``control/artifacts.py::erase_session_artifacts``) that
    need to check a single id, not a ``base/session_id/run_id`` join."""
    return bool(_ID_RE.match(value or ""))


ARTIFACT_ID_LENGTH = 32  # uuid4().hex: exactly 32 lowercase hex characters


def artifact_id_from_export_alias(path: str | Path) -> str:
    """Recover the canonical, extension-less ``ArtifactService`` artifact_id
    from a suffix-bearing guard-scannable alias
    (``agents.reasoning.Executor._finalize_export``): a same-inode hard
    link whose basename is always the bare artifact_id immediately
    followed by the export's extension. Returns ``""`` when the basename
    does not start with a well-formed artifact_id, so a caller can treat
    an unrecognised path as "no artifact" rather than guessing."""
    candidate = Path(path).name[:ARTIFACT_ID_LENGTH]
    if len(candidate) == ARTIFACT_ID_LENGTH and all(c in "0123456789abcdef" for c in candidate):
        return candidate
    return ""


def run_scoped_dir(base: Path, session_id: str, run_id: str) -> Path:
    """Return (creating if needed) ``base/session_id/run_id``.

    Raises :class:`UnsafePath` when either id is not a bare lowercase-hex
    token -- fail closed rather than accept a value that could be crafted
    to traverse or collide outside the run's own directory.
    """
    if not _ID_RE.match(session_id or ""):
        raise UnsafePath(f"session_id is not a safe run-scoped identifier: {session_id!r}")
    if not _ID_RE.match(run_id or ""):
        raise UnsafePath(f"run_id is not a safe run-scoped identifier: {run_id!r}")
    scoped = base.resolve() / session_id / run_id
    scoped.mkdir(parents=True, exist_ok=True, mode=0o700)
    return scoped


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
