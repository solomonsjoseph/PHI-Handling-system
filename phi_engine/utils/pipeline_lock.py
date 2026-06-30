"""Canonical per-study pipeline lock (Wave 4 B3.7 / D1 extraction).

This module is the single source of truth for the exclusive, per-study process
lock that gates the host publish path. It was extracted verbatim from
``main.py`` so the new orchestrator skill (``report-ai-study-pipeline/run.py``)
and ``main.py`` share *one* lock implementation rather than racing two copies on
the same ``fcntl`` flock.

**Lock-baton handoff.** The orchestrator (or the legacy
``extract_to_llm_source`` wrapper) acquires the study flock *before* spawning
``main.py --pipeline`` as a subprocess. Re-acquiring the same lock file from the
child would deadlock (POSIX: same path, different fd), so the child honours a
baton passed via two env vars:

* ``REPORTAL_PIPELINE_LOCK_HELD_BY_PARENT == "1"`` — the parent claims the lock,
* ``REPORTAL_PIPELINE_LOCK_PARENT_PID`` — the parent's PID, which must name a
  live process equal to ``os.getppid()`` (GAP-3 validation).

A leaked/stale baton inherited by an unrelated direct ``python main.py`` run
fails validation and falls through to a real acquisition, so a forged env var
can never silently disable the lock.

The module owns the lock handle as a process-level singleton; query it with
:func:`is_locally_held` rather than reaching for the handle directly.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import phi_engine.config.config as config
from phi_engine.utils.logging_system import get_logger

__all__ = [
    "acquire_pipeline_lock",
    "baton_is_valid",
    "held_lock_path",
    "is_locally_held",
    "lock_path_for",
    "pipeline_lock",
    "release_pipeline_lock",
]

_logger = get_logger(__name__)

#: Process-level singleton holding the open lock file handle (or ``None``).
_PIPELINE_LOCK_FILE: Any | None = None

_BATON_HELD = "REPORTAL_PIPELINE_LOCK_HELD_BY_PARENT"
_BATON_PID = "REPORTAL_PIPELINE_LOCK_PARENT_PID"


def lock_path_for(study: str | None = None) -> Path:
    """Return the lock-file path for *study* (defaults to ``config.STUDY_NAME``)."""
    study_name = study if study is not None else config.STUDY_NAME
    return Path(config.TMP_DIR) / f".{study_name}.pipeline.lock"


def is_locally_held() -> bool:
    """True iff *this* process currently owns the lock handle.

    Distinct from "the lock is held by someone": when the parent-baton was
    honoured this returns ``False`` even though the run is protected, mirroring
    the guards in ``main.py`` that defer destruction to the baton holder.
    """
    return _PIPELINE_LOCK_FILE is not None


def held_lock_path() -> Path | None:
    """Path of the lock file *this* process holds open, or ``None``.

    Used by the verifier's assertion 11 to distinguish "the lock is the
    evidence of THIS in-progress run" (pass) from "a stale lock left by a dead
    run" (fail). Returns ``None`` when no handle is held or the handle is closed.
    """
    fh = _PIPELINE_LOCK_FILE
    if fh is None or getattr(fh, "closed", True):
        return None
    return Path(str(fh.name))


def baton_is_valid() -> bool:
    """Validate the parent lock-baton (GAP-3): live parent PID == ``getppid()``.

    Public so the verifier's assertion 11 can recognise that a present lock file
    is the evidence of an in-progress *orchestrated* run (the parent holds it),
    not a stale leftover (risk #7 — the baton moved from wrapper→main to
    orchestrator→skill subprocess).
    """
    if os.environ.get(_BATON_HELD) != "1":
        return False
    pid_str = os.environ.get(_BATON_PID, "").strip()
    try:
        claimed_pid = int(pid_str)
    except (ValueError, TypeError):
        claimed_pid = None
    if claimed_pid is None or claimed_pid != os.getppid():
        return False
    try:
        os.kill(claimed_pid, 0)
    except OSError:
        return False
    return True


def acquire_pipeline_lock(study: str | None = None) -> None:
    """Hold an exclusive per-study process lock for the lifetime of this run.

    Honours a validated parent baton (skips acquisition). Raises
    :class:`RuntimeError` if another host publish run already holds the lock.
    """
    global _PIPELINE_LOCK_FILE

    if os.environ.get(_BATON_HELD) == "1":
        if baton_is_valid():
            # Parent already holds the lock — skip acquisition.
            return
        _logger.debug(
            "%s=1 but PID validation failed (claimed_pid=%s, getppid=%s) — "
            "acquiring lock normally.",
            _BATON_HELD,
            os.environ.get(_BATON_PID, "").strip(),
            os.getppid(),
        )

    study_name = study if study is not None else config.STUDY_NAME
    lock_dir = Path(config.TMP_DIR)
    lock_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        lock_dir.chmod(0o700)
    lock_path = lock_dir / f".{study_name}.pipeline.lock"
    if _PIPELINE_LOCK_FILE is not None:
        if Path(str(_PIPELINE_LOCK_FILE.name)) == lock_path:
            return
        _PIPELINE_LOCK_FILE.close()
        _PIPELINE_LOCK_FILE = None

    fh = lock_path.open("a+", encoding="utf-8")
    with contextlib.suppress(OSError):
        lock_path.chmod(0o600)

    try:
        fh.seek(0)
        if os.name == "posix":
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif os.name == "nt":
            import msvcrt

            fh.write("\0")
            fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()}\nstudy={study_name}\n")
        fh.flush()
        os.fsync(fh.fileno())
    except OSError as exc:
        fh.close()
        raise RuntimeError(
            f"Another host publish run already holds the study lock: {lock_path}"
        ) from exc

    _PIPELINE_LOCK_FILE = fh


def release_pipeline_lock(study: str | None = None) -> None:
    """Release the process-local pipeline lock handle.

    Symmetric to the acquire-side baton guard: when the parent holds the lock we
    never acquired locally, so there is nothing to release. ``study`` is accepted
    for API symmetry but unused (the handle is a singleton).
    """
    global _PIPELINE_LOCK_FILE
    if os.environ.get(_BATON_HELD) == "1" and _PIPELINE_LOCK_FILE is None:
        return
    if _PIPELINE_LOCK_FILE is None:
        return
    lock_path = Path(str(_PIPELINE_LOCK_FILE.name))
    with contextlib.suppress(OSError):
        lock_path.unlink()
    _PIPELINE_LOCK_FILE.close()
    _PIPELINE_LOCK_FILE = None


@contextlib.contextmanager
def pipeline_lock(study: str | None = None) -> Iterator[None]:
    """Context manager wrapper around acquire/release for a study lock."""
    acquire_pipeline_lock(study)
    try:
        yield
    finally:
        release_pipeline_lock(study)
