"""Per-run context helpers: run_id resolution and sidecar writers.

All runtime-only fields (timestamps, run identifiers) that would otherwise
bake into the primary pipeline artifacts are instead written here to per-run
sidecars under ``output/{STUDY}/runs/{run_id}/``.  Primary artifacts stay
content-only and byte-identical across consecutive runs on identical input.

Public API
----------
resolve_run_id()
    Return the active run identifier.  Reads ``REPORTAL_RUN_ID`` env var
    when set; otherwise mints a fresh ``run_<uuid4().hex>`` string.

write_extraction_timing_sidecar(...)
    Atomically write ``output/{STUDY}/runs/{run_id}/extraction_timing.json``.

write_lineage_timing_sidecar(...)
    Atomically write ``output/{STUDY}/runs/{run_id}/lineage_timing.json``.

scan_for_in_progress_scrubs(study_runs_dir)
    Return a list of ``scrub.in_progress`` token paths found under
    ``study_runs_dir/*/``.  Used by the wrapper CLI (P3.1) to refuse with
    exit 6 when a prior run left staging partially scrubbed.

write_cleanup_token(run_dir) / delete_cleanup_token(run_dir)
    Write / delete a ``cleanup.in_progress`` token in *run_dir*, mirroring the
    ``scrub.in_progress`` mechanism for the dataset-cleanup leg (Note 13 Gap 7).

scan_for_in_progress_cleanups(study_runs_dir)
    Return a list of ``cleanup.in_progress`` token paths found under
    ``study_runs_dir/*/``.  Used to refuse when a prior run left the cleanup
    leg partially applied.

SCRUB_RECOVERY_MESSAGE / CLEANUP_RECOVERY_MESSAGE
    Human-readable message templates (``{path}`` placeholder) for the wrapper
    to surface when it detects an in-progress token.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from phi_engine.utils._extraction_io import atomic_write_json

__all__ = [
    "CLEANUP_RECOVERY_MESSAGE",
    "SCRUB_RECOVERY_MESSAGE",
    "delete_cleanup_token",
    "resolve_run_id",
    "scan_for_in_progress_cleanups",
    "scan_for_in_progress_scrubs",
    "write_cleanup_token",
    "write_extraction_timing_sidecar",
    "write_lineage_timing_sidecar",
]

SCRUB_RECOVERY_MESSAGE = (
    "Previous run left staging partially scrubbed (in-progress token found: {path}). "
    "Run `make rebuild-llm-source` to clean the runs/ directory, then retry."
)

CLEANUP_RECOVERY_MESSAGE = (
    "Previous run left dataset cleanup partially applied (in-progress token found: {path}). "
    "Run `make rebuild-llm-source` to clean the runs/ directory, then retry."
)

_ENV_VAR = "REPORTAL_RUN_ID"


def resolve_run_id() -> str:
    """Return the active run identifier.

    Reads the ``REPORTAL_RUN_ID`` environment variable when set; otherwise
    generates a fresh ``run_<uuid4().hex>`` string.  The future
    ``extract_to_llm_source`` CLI wrapper (P3.1) will set ``REPORTAL_RUN_ID``
    before launching the pipeline so every sidecar produced in one invocation
    shares the same run_id.  When the env var is absent, each call generates
    a distinct id — existing single-run behaviour is preserved.
    """
    env_val = os.environ.get(_ENV_VAR)
    if env_val:
        return env_val
    return f"run_{uuid4().hex}"


def write_extraction_timing_sidecar(
    *,
    output_dir: Path,
    run_id: str,
    study: str,
    extraction_utc: str,
    pipeline_version: str,
    per_form_timing: dict[str, Any] | None = None,
) -> Path:
    """Atomically write ``runs/{run_id}/extraction_timing.json`` under *output_dir*.

    Parameters
    ----------
    output_dir:
        Study-level output root (e.g. ``output/{STUDY}``).
    run_id:
        Active run identifier (from :func:`resolve_run_id`).
    study:
        Study name, included verbatim for human readability.
    extraction_utc:
        ISO-8601 UTC timestamp string captured at extraction start.
    pipeline_version:
        ``__version__`` of the pipeline at extraction time.
    per_form_timing:
        Optional mapping of form/file name → elapsed-seconds float.

    Returns
    -------
    Path
        Absolute path to the written sidecar file.
    """
    sidecar_dir = output_dir / "runs" / run_id
    sidecar_path = sidecar_dir / "extraction_timing.json"

    payload: dict[str, Any] = {
        "run_id": run_id,
        "study": study,
        "extraction_utc": extraction_utc,
        "pipeline_version": pipeline_version,
    }
    if per_form_timing is not None:
        payload["per_form_timing"] = per_form_timing

    atomic_write_json(sidecar_path, payload)
    return sidecar_path


def write_lineage_timing_sidecar(
    *,
    runs_dir: Path,
    run_id: str,
    study: str,
    generated_utc: str,
    mtime_utc: dict[str, str],
) -> Path:
    """Atomically write ``{runs_dir}/{run_id}/lineage_timing.json``.

    Parameters
    ----------
    runs_dir:
        Parent directory for per-run sidecars (e.g. ``output/{STUDY}/runs``).
    run_id:
        Active run identifier.
    study:
        Study name.
    generated_utc:
        ISO-8601 UTC timestamp at manifest generation time.
    mtime_utc:
        Mapping of file path (relative to their respective root) → ISO-8601
        mtime string, collected from all input and output file records.

    Returns
    -------
    Path
        Absolute path to the written sidecar file.
    """
    sidecar_dir = runs_dir / run_id
    sidecar_path = sidecar_dir / "lineage_timing.json"

    payload: dict[str, Any] = {
        "run_id": run_id,
        "study": study,
        "generated_utc": generated_utc,
        "mtime_utc": mtime_utc,
    }
    atomic_write_json(sidecar_path, payload)
    return sidecar_path


_IN_PROGRESS_TOKEN_NAME = "scrub.in_progress"  # noqa: S105


def scan_for_in_progress_scrubs(study_runs_dir: Path) -> list[Path]:
    """Return ``scrub.in_progress`` token paths found under *study_runs_dir*.

    Scans one level deep (``study_runs_dir/*/scrub.in_progress``).  A
    non-existent *study_runs_dir* returns an empty list so callers do not need
    to guard for the directory's existence.

    Parameters
    ----------
    study_runs_dir:
        The ``runs/`` directory under the study output root
        (e.g. ``output/{STUDY}/runs``).

    Returns
    -------
    list[Path]
        Absolute paths to every ``scrub.in_progress`` file found, one per
        partially-scrubbed run.  Empty list when none are found.
    """
    if not study_runs_dir.is_dir():
        return []
    return sorted(p for p in study_runs_dir.glob(f"*/{_IN_PROGRESS_TOKEN_NAME}") if p.is_file())


_CLEANUP_IN_PROGRESS_TOKEN_NAME = "cleanup.in_progress"  # noqa: S105


def write_cleanup_token(run_dir: Path) -> Path:
    """Atomically write a ``cleanup.in_progress`` token in *run_dir*.

    Mirrors the ``scrub.in_progress`` mechanism for the dataset-cleanup leg
    (Note 13 Gap 7).  The token is written before the cleanup loop and deleted
    on completion (:func:`delete_cleanup_token`); a surviving token signals a
    prior run that aborted mid-cleanup, so the staged datasets may be partially
    rewritten and must not be promoted.

    Parameters
    ----------
    run_dir:
        The per-run directory the token is written into
        (e.g. ``output/{STUDY}/runs/{run_id}``).  Created if absent.

    Returns
    -------
    Path
        Absolute path to the written token file.
    """
    token_path = run_dir / _CLEANUP_IN_PROGRESS_TOKEN_NAME
    token_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(token_path, {})
    return token_path


def delete_cleanup_token(run_dir: Path) -> None:
    """Delete the ``cleanup.in_progress`` token in *run_dir* if present.

    Idempotent: a missing token is not an error, matching the
    ``in_progress_token.unlink(missing_ok=True)`` semantics of the
    ``scrub.in_progress`` mechanism.

    Parameters
    ----------
    run_dir:
        The per-run directory the token lives in
        (e.g. ``output/{STUDY}/runs/{run_id}``).
    """
    (run_dir / _CLEANUP_IN_PROGRESS_TOKEN_NAME).unlink(missing_ok=True)


def scan_for_in_progress_cleanups(study_runs_dir: Path) -> list[Path]:
    """Return ``cleanup.in_progress`` token paths found under *study_runs_dir*.

    Scans one level deep (``study_runs_dir/*/cleanup.in_progress``).  A
    non-existent *study_runs_dir* returns an empty list so callers do not need
    to guard for the directory's existence.

    Parameters
    ----------
    study_runs_dir:
        The ``runs/`` directory under the study output root
        (e.g. ``output/{STUDY}/runs``).

    Returns
    -------
    list[Path]
        Absolute paths to every ``cleanup.in_progress`` file found, one per
        partially-cleaned run.  Empty list when none are found.
    """
    if not study_runs_dir.is_dir():
        return []
    return sorted(
        p for p in study_runs_dir.glob(f"*/{_CLEANUP_IN_PROGRESS_TOKEN_NAME}") if p.is_file()
    )
