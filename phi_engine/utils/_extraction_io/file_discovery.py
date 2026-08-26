"""File-discovery helpers for the RePORT AI Portal extraction pipeline.

Provides a single ``discover_files`` function that scans a directory for
files matching a set of extensions, skipping hidden files, OS junk, and
Excel lock files.  Returns a **sorted**, deterministic list of ``Path``
objects so repeated runs produce identical ordering.

All three extraction modules (dictionary, dataset, PDF) previously
implemented this same logic inline.  This module consolidates it into
one tested, canonical helper.
"""

# SHARED UTILITY (Note 20 Gap C / Note 19): stays in scripts/, imported
# read-only by plugin skills; never moved into a skill, never imports from
# plugins/.

from __future__ import annotations

from pathlib import Path

__all__ = [
    "DEFAULT_JUNK_FILENAMES",
    "SUPPORTED_TABULAR_EXTENSIONS",
    "discover_files",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_JUNK_FILENAMES: frozenset[str] = frozenset(
    {
        ".DS_Store",
        "Thumbs.db",
        "desktop.ini",
        "__MACOSX",
    }
)
"""Filenames unconditionally skipped during discovery."""

SUPPORTED_TABULAR_EXTENSIONS: tuple[str, ...] = (".xlsx", ".csv")
"""File extensions recognised as tabular data sources."""


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_files(
    directory: Path | str,
    *,
    extensions: tuple[str, ...] | frozenset[str] | None = None,
    junk: frozenset[str] = DEFAULT_JUNK_FILENAMES,
    label: str = "supported",
    not_found_label: str | None = None,
) -> list[Path]:
    """Return a sorted list of non-hidden, non-junk files matching *extensions*.

    Args:
        directory: The directory to scan (non-recursive, immediate children only).
        extensions: Allowed lowercase extensions (e.g. ``(".xlsx", ".csv")``).
            When ``None``, all non-junk, non-hidden files are returned.
        junk: Set of filenames to unconditionally skip.
        label: Human-readable label used in the ``FileNotFoundError`` message
            (e.g. ``"Dataset"``, ``"Data dictionary"``).
        not_found_label: Label used in the ``ValueError`` when no matching files
            are found (e.g. ``"dictionary"``, ``"dataset"``).  Defaults to
            ``label.lower()`` when not supplied.

    Returns:
        Sorted list of ``Path`` objects.

    Raises:
        FileNotFoundError: If *directory* does not exist or is not a directory.
        ValueError: If no matching files are found.
    """
    ddir = Path(directory)
    no_files = (not_found_label or label).lower()

    if not ddir.exists() or not ddir.is_dir():
        raise FileNotFoundError(f"{label} directory not found: {ddir}")

    ext_set = frozenset(extensions) if extensions is not None else None

    files: list[Path] = []
    for child in sorted(ddir.iterdir()):
        # Skip junk
        if child.name in junk:
            continue
        # Skip hidden files and Excel lock files
        if child.name.startswith(".") or child.name.startswith("~$"):
            continue
        if not child.is_file():
            continue
        # Filter by extension when specified
        if ext_set is not None and child.suffix.lower() not in ext_set:
            continue
        files.append(child)

    if not files:
        ext_msg = f"Supported extensions: {', '.join(sorted(extensions))}" if extensions else ""
        raise ValueError(
            f"No supported {no_files} files found in: {ddir}" + (f"\n{ext_msg}" if ext_msg else "")
        )

    return files
