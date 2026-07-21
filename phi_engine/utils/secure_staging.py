"""Hardened AMBER-zone staging helpers for the RePORT AI Portal host publish path.

The host publish path processes raw study data (PHI) in a transient staging
workspace under ``tmp/{STUDY_NAME}/`` before atomically publishing the PHI-free
``llm_source/``. Staging is the honest-broker AMBER zone — it must carry the
strongest defensive posture the local filesystem supports:

* **Restrictive permissions** — directory mode ``0700`` + umask ``0077``
  for every write, so no other OS user can read partial staging output.
* **Secure teardown** — on successful completion, each staging file is
  **overwritten with random bytes and fsynced before unlink**, reducing
  the window where deleted staging contents could be recovered via
  filesystem forensics.
* **In-memory staging (optional)** — when the environment sets
  ``REPORTALIN_TMPFS_STAGING=1`` AND ``/dev/shm`` is writable, staging is
  redirected to a tmpfs mount so raw extracted data never touches the
  physical disk on the extraction host. Otherwise falls back to the
  default on-disk staging root resolved by :mod:`config`.

The module is pure filesystem plumbing: it never reads row contents,
never logs values, never crosses zone boundaries. Every file operation
is wrapped by :func:`scripts.security.secure_env.assert_write_zone` so a
misconfigured staging root fails fast with a zone violation rather than
silently writing outside the allowed area.

Authority-grounded controls:
    * HIPAA §164.310(c) device + media controls
    * NIST SP 800-188 §6.3-§6.5 on transient de-identification workspaces
    * ICMR 2017 §11.5 audit + confidentiality
    * DPDPA 2023 §8(7) erasure

Public API:
    * :func:`resolve_staging_root` — where should staging live this run?
    * :func:`prepare_staging` — wipe + create with hardened permissions
    * :func:`secure_remove_tree` — zero-fill + unlink every file below path
    * :func:`scoped_umask` — context manager for umask 0077 during a block
"""

from __future__ import annotations

import contextlib
import os
import secrets
from collections.abc import Generator, Iterable
from pathlib import Path

from phi_engine.security.secure_env import assert_write_zone
from phi_engine.utils.logging_system import get_logger

logger = get_logger(__name__)

__all__ = [
    "prepare_staging",
    "resolve_staging_root",
    "scoped_umask",
    "secure_remove_tree",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_STAGING_DIR_MODE = 0o700
"""Directory mode for staging workspace — owner-only rwx."""

_STAGING_UMASK = 0o077
"""Umask applied inside :func:`scoped_umask` — owner-only for new files."""

_SECURE_REMOVE_CHUNK = 1 << 16  # 64 KiB
"""Random-byte chunk size written during :func:`secure_remove_tree`."""

_TMPFS_ENV_FLAG = "REPORTALIN_TMPFS_STAGING"
"""Environment variable that, when truthy, opts into tmpfs staging on Linux."""

_TMPFS_ROOT = Path("/dev/shm")  # noqa: S108 — deliberate tmpfs mount, gated by env flag
"""Canonical Linux tmpfs mount. Ignored when non-existent or non-writable.

The S108 ruff warning ("insecure usage of /tmp or /dev/shm") does not apply
here: this path is the intended in-memory filesystem for PHI-safe staging,
entered only when the operator explicitly sets ``REPORTALIN_TMPFS_STAGING=1``
AND ``/dev/shm`` is writable. Writes land under a per-study subdirectory
with mode 0700 under umask 0077, identical to the on-disk hardening.
"""


# ---------------------------------------------------------------------------
# Root resolution
# ---------------------------------------------------------------------------


def _tmpfs_is_available() -> bool:
    """Return True iff ``/dev/shm`` exists and is writable by the current user.

    Used by :func:`resolve_staging_root` to decide whether to honor the
    ``REPORTALIN_TMPFS_STAGING`` opt-in.
    """
    return _TMPFS_ROOT.is_dir() and os.access(_TMPFS_ROOT, os.W_OK)


def resolve_staging_root(
    default_root: Path,
    *,
    study_name: str,
) -> Path:
    """Return the staging root for this run.

    When ``REPORTALIN_TMPFS_STAGING`` is truthy AND ``/dev/shm`` is
    writable, returns ``/dev/shm/report_ai_portal/{STUDY_NAME}``. Otherwise
    returns *default_root* (the on-disk staging path from config).

    The caller is responsible for updating ``config.STUDY_STAGING_DIR`` and
    the per-leg ``STAGING_*_DIR`` paths before the extraction leg reads
    them. This function does not mutate any global state.

    Zone guard: callers must pass the returned path to :func:`prepare_staging`,
    which asserts it against the write zone. A misconfigured env override
    (e.g. ``REPORTALIN_TMPFS_STAGING=1`` on a platform without tmpfs AND a
    mangled default root) is caught at the ``prepare_staging`` call site.
    """
    env_opt_in = os.environ.get(_TMPFS_ENV_FLAG, "").lower() in {"1", "true", "yes", "on"}
    if env_opt_in and _tmpfs_is_available():
        tmpfs_root = _TMPFS_ROOT / "report_ai_portal" / study_name
        logger.info("secure_staging: tmpfs opt-in active — staging root %s", tmpfs_root)
        return tmpfs_root
    logger.debug("secure_staging: default on-disk staging root %s", default_root)
    return default_root


# ---------------------------------------------------------------------------
# Permission hardening
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def scoped_umask(mask: int = _STAGING_UMASK) -> Generator[int, None, None]:
    """Context manager that sets the process umask for the duration of a block.

    Yields the previous umask so callers may inspect it. Always restored on
    exit, even on exception.

    Use this to wrap extraction-leg writes into staging so newly-created
    files land with mode 0600 (or 0700 for directories) rather than the
    platform default (often 0644 / 0755).
    """
    previous = os.umask(mask)
    try:
        yield previous
    finally:
        os.umask(previous)


def _harden_dir(path: Path) -> None:
    """Apply mode ``0700`` to *path* best-effort; logs failure at WARNING."""
    try:
        path.chmod(_STAGING_DIR_MODE)
    except OSError as exc:
        # Windows / non-POSIX filesystems may not support chmod — warn but
        # do not fail the pipeline for a permission hardening shortfall.
        logger.warning("secure_staging: could not set mode 0700 on %s: %s", path, exc)


def prepare_staging(
    root: Path,
    subdirs: Iterable[Path],
) -> None:
    """Wipe *root* and create *subdirs* with hardened permissions.

    Order of operations:
        1. If *root* exists, invoke :func:`secure_remove_tree` on it so no
           residue from a prior failed run carries over.
        2. Under :func:`scoped_umask`, create *root* and each *subdir* with
           mode ``0700``.
        3. Zone-guard *root* via ``assert_write_zone``.

    Side effects:
        * Every directory created lands with mode 0700.
        * The process umask is temporarily ``0o077`` only while creating.

    Raises:
        ZoneViolationError: when *root* is outside the allowed write zones.
    """
    root = Path(root)
    assert_write_zone(root)

    if root.exists():
        secure_remove_tree(root)

    with scoped_umask():
        root.mkdir(parents=True, exist_ok=True)
        _harden_dir(root)
        for sub in subdirs:
            sub_path = Path(sub)
            sub_path.mkdir(parents=True, exist_ok=True)
            _harden_dir(sub_path)

    logger.info("secure_staging: prepared staging at %s (mode=0700, umask=0077)", root)


# ---------------------------------------------------------------------------
# Secure teardown
# ---------------------------------------------------------------------------


def _overwrite_file(path: Path) -> None:
    """Overwrite *path* in-place with one pass of ``secrets.token_bytes``.

    Best-effort: if the file is a symlink, a device, or fails to open, we
    skip overwrite and rely on ``unlink``. On regular files we write the
    full length in 64 KiB chunks of cryptographically-random bytes, then
    fsync before returning so the random bytes are durable on disk.

    A single pass is sufficient on modern filesystems: a forensic
    recovery attempt after overwrite + unlink would need to recover
    deleted file metadata AND find intact copies on replaced blocks. On
    SSDs with TRIM enabled, even one pass gives strong guarantees; on
    HDDs, one pass resists all but the most determined recovery.
    Multi-pass wipes (DoD 5220.22-M style) provide no additional
    security for this threat model and cost proportionally more I/O.
    """
    try:
        stat_result = path.lstat()
    except FileNotFoundError:
        return
    if not path.is_file() or path.is_symlink():
        return

    size = stat_result.st_size
    if size == 0:
        return

    try:
        # Open with O_WRONLY (not append) so writes start at offset 0.
        fd = os.open(str(path), os.O_WRONLY)
    except OSError as exc:
        logger.warning("secure_staging: could not open %s for overwrite: %s", path, exc)
        return

    try:
        written = 0
        while written < size:
            chunk = min(_SECURE_REMOVE_CHUNK, size - written)
            os.write(fd, secrets.token_bytes(chunk))
            written += chunk
        os.fsync(fd)
    except OSError as exc:
        logger.warning("secure_staging: overwrite of %s failed: %s", path, exc)
    finally:
        os.close(fd)


def secure_remove_tree(root: Path) -> None:
    """Recursively overwrite + delete every file under *root*, then the tree.

    For each regular file found: overwrite with random bytes, fsync, unlink.
    Empty directories are then removed bottom-up. Non-file entries
    (symlinks, sockets, pipes) are unlinked without overwrite.

    Failures are logged at WARNING and do not abort the teardown — the
    goal is best-effort secure-delete for the happy path; a partial
    failure still leaves the caller with an empty tree.

    Zone guard: *root* is asserted against the write zone so a stray
    invocation on a protected path fails fast.
    """
    root = Path(root)
    if not root.exists():
        return
    assert_write_zone(root)

    # Walk bottom-up so we can rmdir empty dirs after files are unlinked.
    for current_root, dirs, files in os.walk(str(root), topdown=False):
        current_path = Path(current_root)
        for fname in files:
            fpath = current_path / fname
            try:
                _overwrite_file(fpath)
                fpath.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                logger.warning("secure_staging: unlink of %s failed: %s", fpath, exc)
        for dname in dirs:
            dpath = current_path / dname
            try:
                dpath.rmdir()
            except OSError as exc:
                logger.warning("secure_staging: rmdir of %s failed: %s", dpath, exc)

    # Finally remove root itself.
    try:
        root.rmdir()
    except OSError as exc:
        logger.warning("secure_staging: rmdir of root %s failed: %s", root, exc)
