"""Cleanup propagation — prune dictionary artifacts after dataset drops.

Runs against the staging workspace (``tmp/{STUDY_NAME}/{datasets,dictionary}/``)
after :func:`scripts.extraction.dataset_cleanup.emit_dataset_cleanup_audit_envelope` completes.

The dictionary leg carries no PHI and therefore emits no audit report — the
prune step is side-effect-only, keeping the dictionary schema aligned with
the surviving dataset schema so the LLM sees no dangling references. The
dataset leg's own audit (``AUDIT_DATASET_REPORT_PATH``) remains the single
source of truth for what was removed.

Pruning rule
------------

A variable ``V`` is pruned from the dictionary leg **iff** it was dropped
from at least one dataset *and* never survives in any final surviving
dataset JSONL schema. Variables dropped from one dataset but kept in another
are NOT pruned.

Comparisons are case-folded; dataset provenance fields
(``source_file``, ``_provenance``, ``_metadata``) are excluded from
the surviving-vars set.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import config
from scripts.extraction.io import (
    JSONLParseError,
    atomic_write_jsonl,
    load_json_object_line,
)
from scripts.security.secure_env import assert_write_zone
from scripts.utils.logging_system import get_logger

logger = get_logger(__name__)

__all__ = [
    "PROVENANCE_FIELDS",
    "compute_propagation_set",
    "prune_dictionary",
    "run_propagation",
]


# ── Constants ───────────────────────────────────────────────────────────────

# Dataset-row metadata keys that are NOT "variables" — they should be excluded
# from the "surviving dataset vars" set so propagation doesn't treat them as
# schema members.
#
# Self-contained local literal. Per Note 18 ("ONE DEPENDENCY TO BREAK
# IMMEDIATELY"), the host publish pipeline MUST NOT import the AI agent tools
# module. This set mirrors agent_tools._INTERNAL_COLUMNS (source_file,
# _provenance, _source_row, _ingestion_ts) plus the extraction-leg provenance
# blob (_metadata) and the PHI scrub version marker (_phi_scrubbed) — but the
# two lists now evolve independently while carrying identical content; there is
# deliberately no cross-import between them.
PROVENANCE_FIELDS: frozenset[str] = frozenset(
    {
        "source_file",
        "_provenance",
        "_source_row",
        "_ingestion_ts",
        "_metadata",
        "_phi_scrubbed",
    }
)

_DICT_VAR_KEY: str = "Question Short Name (Databank Fieldname)"


# ── Step 1: compute_propagation_set ─────────────────────────────────────────


def compute_propagation_set(
    audit_path: Path,
    datasets_dir: Path,
) -> set[str]:
    """Return the case-folded set of variables that should propagate-prune.

    Algorithm:
        1. Load ``audit_path`` (the dataset leg's unified audit). Union all
           ``scope == "dataset-column"`` events' ``name`` into
           ``dataset_dropped_vars`` (case-folded).
        2. Scan every ``datasets_dir/*.jsonl``. Union all row keys (excluding
           :data:`PROVENANCE_FIELDS`) into ``surviving_dataset_vars``
           (case-folded).
        3. Return ``dataset_dropped_vars - surviving_dataset_vars``.

    Variables dropped from one dataset but kept in another → excluded from
    the returned set (they "survive" somewhere). Missing audit or empty
    datasets dir → empty set.
    """
    if not audit_path.exists():
        level = logging.INFO
        if datasets_dir.is_dir() and any(datasets_dir.glob("*.jsonl")):
            level = logging.WARNING
        logger.log(
            level,
            "Dataset audit not found at %s — propagation set is empty "
            "(caller may have invoked run_propagation before emit_dataset_cleanup_audit_envelope)",
            audit_path,
        )
        return set()

    audit_payload = json.loads(audit_path.read_text(encoding="utf-8"))
    dropped: set[str] = set()
    for entry in audit_payload.get("removed", []):
        if entry.get("scope") == "dataset-column":
            name = entry.get("name")
            if isinstance(name, str) and name:
                dropped.add(name.casefold())

    if not dropped:
        return set()

    surviving: set[str] = set()
    if datasets_dir.is_dir():
        for jsonl_file in sorted(datasets_dir.glob("*.jsonl")):
            with jsonl_file.open("r", encoding="utf-8") as fh:
                for line_no, raw in enumerate(fh, start=1):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        row = load_json_object_line(
                            raw, source_path=jsonl_file, line_number=line_no
                        )
                    except JSONLParseError as exc:
                        # Fail-closed: a malformed line means we cannot determine
                        # which variables survive in this file.  Silently continuing
                        # would risk over-pruning — a variable that only appears on
                        # the unparseable row would be excluded from 'surviving' and
                        # then pruned from the dictionary (dangling-reference
                        # integrity loss).  Abort so the operator can inspect the
                        # malformed JSONL before propagation runs.
                        raise RuntimeError(
                            f"Propagation aborted: malformed JSONL at "
                            f"{jsonl_file.name}:{line_no} — fix or remove the "
                            f"file before re-running. "
                            f"(Original error: {exc})"
                        ) from exc
                    for key in row:
                        if isinstance(key, str) and key not in PROVENANCE_FIELDS:
                            surviving.add(key.casefold())

    return dropped - surviving


# ── Step 2: prune_dictionary ────────────────────────────────────────────────


def prune_dictionary(drop_set: set[str], dict_dir: Path) -> int:
    """Walk ``dict_dir/**/*.jsonl`` and drop rows in ``drop_set``.

    Each row's :data:`_DICT_VAR_KEY` value is compared case-folded against
    ``drop_set`` (which callers pass pre-folded — see
    :func:`compute_propagation_set`). Matching rows are removed and the file
    is rewritten atomically.

    Returns the total number of rows removed across all files (for logging).
    No audit artifact is written — the dictionary leg carries no PHI.
    """
    total_removed = 0

    if not dict_dir.is_dir():
        return 0

    assert_write_zone(dict_dir)
    for jsonl_file in sorted(dict_dir.rglob("*.jsonl")):
        kept_rows: list[dict[str, Any]] = []
        removed_in_file = 0
        with jsonl_file.open("r", encoding="utf-8") as fh:
            for line_no, raw in enumerate(fh, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = load_json_object_line(raw, source_path=jsonl_file, line_number=line_no)
                except JSONLParseError:
                    logger.debug("Skipping malformed line %d in %s", line_no, jsonl_file.name)
                    continue
                var_name = row.get(_DICT_VAR_KEY)
                if isinstance(var_name, str) and var_name.casefold() in drop_set:
                    removed_in_file += 1
                else:
                    kept_rows.append(row)
        if removed_in_file:
            atomic_write_jsonl(jsonl_file, kept_rows)
            total_removed += removed_in_file
            logger.info("Pruned %d rows from %s", removed_in_file, jsonl_file.name)

    return total_removed


# ── Step 4: run_propagation ─────────────────────────────────────────────────


def run_propagation() -> None:
    """Orchestrate the propagation: compute drop set, prune the dictionary leg.

    All paths resolved from ``config.STAGING_*`` and ``config.AUDIT_*`` —
    never touches the promoted trio bundle directly. The dictionary leg emits
    no audit report (no PHI); only its prune count is logged.
    """
    drop_set = compute_propagation_set(
        config.AUDIT_DATASET_REPORT_PATH,
        config.STAGING_DATASETS_DIR,
    )
    logger.info(
        "Propagation drop-set (%d vars): %s",
        len(drop_set),
        sorted(drop_set),
    )

    dict_removed = prune_dictionary(drop_set, config.STAGING_DICTIONARY_DIR)
    logger.info(
        "Propagation complete: %d dictionary rows pruned",
        dict_removed,
    )
