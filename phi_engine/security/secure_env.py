"""Zone-enforcement helpers for the RePORT AI Portal runtime.

Defines the path-assertion helpers that keep raw datasets, staging, and
clean published output from bleeding into one another. The four-tier
architecture (RED / AMBER / GREEN / GREEN-PROTECT) in the developer-
guide PHI-architecture page is implemented in code as the zone guards
here.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

__all__ = [
    "ZoneViolationError",
    "assert_clean_zone",
    "assert_not_raw",
    "assert_output_not_in_data",
    "assert_output_zone",
    "assert_write_zone",
    "validate_paths",
]

# ---------------------------------------------------------------------------
# Markers — resolved at import to avoid repeated config reads
# ---------------------------------------------------------------------------
# The clean zone is study-scoped: output/{STUDY}/llm_source/.
# CLEAN_MARKER points to the study-scoped llm_source dir for the active study.


def _resolve_markers() -> tuple[str, str, str, str, str]:
    """Resolve zone marker paths from config, with fallback for isolated testing."""
    try:
        import config as _cfg

        return (
            os.path.realpath(_cfg.RAW_DATA_DIR),
            os.path.realpath(_cfg.DATA_DIR),
            os.path.realpath(_cfg.STUDY_LLM_SOURCE_DIR),
            os.path.realpath(_cfg.OUTPUT_DIR),
            os.path.realpath(_cfg.TMP_DIR),
        )
    except ImportError:
        project = str(Path(__file__).resolve().parents[2])
        return (
            os.path.join(project, "data", "raw"),
            os.path.join(project, "data"),
            os.path.join(project, "output"),  # conservative fallback
            os.path.join(project, "output"),
            os.path.join(project, "tmp"),
        )


(
    _RAW_MARKER,
    _DATA_MARKER,
    _CLEAN_MARKER,
    _OUTPUT_MARKER,
    _TMP_MARKER,
) = _resolve_markers()


def _is_within(path: str | Path, base: str | Path) -> bool:
    """Return True when *path* is the same as or contained within *base*."""
    resolved_path = _resolve(path)
    resolved_base = _resolve(base)
    try:
        return os.path.commonpath([resolved_path, resolved_base]) == resolved_base
    except ValueError:
        return False


class ZoneViolationError(PermissionError):
    """Raised when code attempts to access a forbidden data zone."""


def _resolve(p: str | Path) -> str:
    return os.path.realpath(str(p))


def assert_not_raw(path: str | Path) -> None:
    """Hard-fail if *path* resides under data/raw/.

    Raises:
        ZoneViolationError: path is inside the raw vault.
    """
    resolved = _resolve(path)
    if _is_within(resolved, _RAW_MARKER):
        raise ZoneViolationError(
            f"Access to raw data zone is forbidden at this pipeline stage: {path}"
        )


def assert_clean_zone(path: str | Path) -> None:
    """Hard-fail if *path* does NOT reside under output/{STUDY}/llm_source/.

    Raises:
        ZoneViolationError: path is outside the clean zone.
    """
    resolved = _resolve(path)
    if not _is_within(resolved, _CLEAN_MARKER):
        raise ZoneViolationError(f"Only clean-zone paths are allowed here. Got: {path}")


def assert_output_not_in_data(path: str | Path) -> None:
    """Hard-fail if *path* is under data/ — processed output must go to output/.

    The data/ directory is reserved exclusively for raw study data (data/raw/).
    All processed artifacts (clean JSONL, indexes, session data, etc.)
    must be written under output/.

    Raises:
        ZoneViolationError: path is inside the data directory.
    """
    resolved = _resolve(path)
    if _is_within(resolved, _DATA_MARKER):
        raise ZoneViolationError(
            f"Writing processed output into data/ is forbidden. "
            f"All output must go under output/. Got: {path}"
        )


def assert_output_zone(path: str | Path) -> None:
    """Hard-fail if *path* is not under output/, or is in raw.

    Used for chunking inputs that may span multiple output sub-trees
    (clean JSONL, data dictionary mappings, etc.) but must never touch
    raw data.

    Raises:
        ZoneViolationError: path is outside output/ or in a forbidden sub-zone.
    """
    resolved = _resolve(path)
    if not _is_within(resolved, _OUTPUT_MARKER):
        raise ZoneViolationError(f"Only paths under output/ are allowed here. Got: {path}")
    if _is_within(resolved, _RAW_MARKER):
        raise ZoneViolationError(
            f"Access to raw data zone is forbidden at this pipeline stage: {path}"
        )


def assert_write_zone(path: str | Path) -> None:
    """Hard-fail if *path* is not under output/ or tmp/, or is in raw.

    Accepts paths under either the durable output zone (``output/``) or the
    transient staging zone (``tmp/``). Both are safe write destinations for
    extraction legs. Raw data is always rejected.

    Use this in place of :func:`assert_output_zone` for call sites that write
    to the staging workspace (``tmp/{STUDY}/``) before atomic publish to
    ``output/{STUDY}/llm_source/``. Audit files that must land in durable
    storage should continue to use :func:`assert_output_zone`.

    Raises:
        ZoneViolationError: path is outside both output/ and tmp/, or is in
            the raw data zone.
    """
    resolved = _resolve(path)
    if not (_is_within(resolved, _OUTPUT_MARKER) or _is_within(resolved, _TMP_MARKER)):
        raise ZoneViolationError(f"Only paths under output/ or tmp/ are allowed here. Got: {path}")
    if _is_within(resolved, _RAW_MARKER):
        raise ZoneViolationError(
            f"Access to raw data zone is forbidden at this pipeline stage: {path}"
        )


def validate_paths(
    paths: Sequence[str | Path],
    *,
    deny_raw: bool = True,
    require_clean: bool = False,
    deny_data_output: bool = False,
) -> None:
    """Batch-validate a sequence of paths against zone policies.

    Args:
        paths: file or directory paths to check.
        deny_raw: reject any path under data/raw/.
        require_clean: require every path to be under output/{STUDY}/clean/.
        deny_data_output: reject any path under data/ (prevents writing
            processed artifacts into the raw data directory).

    Note:
        ``assert_output_zone`` is always called regardless of flag values —
        every path must reside under ``output/``.

    Raises:
        ZoneViolationError: on first violation found.
    """
    if isinstance(paths, str | Path):
        raise TypeError("paths must be a sequence of path values, not a single path")
    for p in paths:
        if deny_raw:
            assert_not_raw(p)
        if require_clean:
            assert_clean_zone(p)
        if deny_data_output:
            assert_output_not_in_data(p)
        assert_output_zone(p)
