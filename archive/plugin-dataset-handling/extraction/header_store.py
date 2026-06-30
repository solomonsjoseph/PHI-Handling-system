"""Shared reader for the header-extraction skill's temp store (Note 6).

The ``header-extraction`` skill (orchestrator Phase **2b**, after dedup) writes a
per-form store of column headers, header/row counts, and source provenance to
``<run_dir>/header_extraction.json``. Downstream skills — PHI-classification (via
the publish supervisor) and SOT generation — read column headers + row counts from
THIS store instead of re-opening raw dataset files when the store is present.
``dataset-deduplication`` (Phase 2) runs **before** the store exists and uses the
same ``resolve_headers`` / ``resolve_row_count`` helpers with a direct row-1 fallback.

All readers are **fail-soft**: a missing run dir, missing/malformed store, or an
absent per-form entry returns ``None`` so the caller falls back to a direct read
(behaviour is then identical to the pre-store code). The store is destroyed after
its consumers finish (orchestrator Phase 7 cleanup).

This module lives under ``scripts/`` (a shared utility) per the one-way
``plugins/ → scripts/`` dependency rule, and imports the direct-read fallbacks
lazily to avoid an import cycle with ``raw_file_dedup``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

HEADER_STORE_FILENAME = "header_extraction.json"


def header_store_path(run_dir: Path | None) -> Path | None:
    """Return the store path for a run dir, or ``None`` if no run dir."""
    if run_dir is None:
        return None
    return Path(run_dir) / HEADER_STORE_FILENAME


def load_header_store(run_dir: Path | None) -> dict[str, Any] | None:
    """Load the parsed store, or ``None`` (fail-soft) if absent/malformed."""
    path = header_store_path(run_dir)
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _entry(store: dict[str, Any] | None, stem: str) -> dict[str, Any] | None:
    if not store:
        return None
    forms = store.get("forms")
    if not isinstance(forms, dict):
        return None
    entry = forms.get(stem)
    return entry if isinstance(entry, dict) else None


def store_headers(store: dict[str, Any] | None, stem: str) -> list[str] | None:
    """Headers for a form stem from the store, or ``None`` if not present."""
    entry = _entry(store, stem)
    if entry is None:
        return None
    headers = entry.get("headers")
    return list(headers) if isinstance(headers, list) else None


def store_row_count(store: dict[str, Any] | None, stem: str) -> int | None:
    """Row count for a form stem from the store, or ``None`` if not present."""
    entry = _entry(store, stem)
    if entry is None:
        return None
    rc = entry.get("row_count")
    return int(rc) if isinstance(rc, int) and not isinstance(rc, bool) else None


def resolve_headers(
    store: dict[str, Any] | None,
    stem: str,
    path: Path,
    *,
    reader: Callable[[Path], list[str]] | None = None,
) -> list[str]:
    """Return headers from the store, falling back to a direct row-1 read."""
    hdrs = store_headers(store, stem)
    if hdrs is not None:
        return hdrs
    if reader is None:
        from scripts.source_truth.study_intake import read_headers_only as reader
    return reader(path)


def resolve_row_count(
    store: dict[str, Any] | None,
    stem: str,
    path: Path,
    *,
    counter: Callable[[Path], int] | None = None,
) -> int:
    """Return the row count from the store, falling back to a count-only read."""
    rc = store_row_count(store, stem)
    if rc is not None:
        return rc
    if counter is None:
        from scripts.extraction.raw_file_dedup import count_data_rows_only as counter
    return counter(path)


def destroy_header_store(run_dir: Path | None) -> bool:
    """Delete the store after its consumers finish (best-effort, fail-soft)."""
    path = header_store_path(run_dir)
    if path is None or not path.is_file():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False
