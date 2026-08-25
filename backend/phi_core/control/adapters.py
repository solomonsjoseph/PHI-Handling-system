"""Thin translation shims for not-yet-migrated decision-gate call sites.

``orchestrator.py``'s decide loop and ``server.py``'s human-review re-gating
still pass a Judge/Sentinel-shaped payload and a ``dataset_files``-shaped
list rather than the plain lists ``control.gates.run_decision_gates``
expects. These two functions are the entire migration seam: a caller can
adopt ``run_decision_gates`` today by adapting its existing values through
here, then drop the adapter call in the same line once it moves onto the
typed contract directly.

Deleted once every caller does that (Phase 5); tracked as ``F-ADAPT-001``
in ``docs/assurance/FINDINGS.md``.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence


def legacy_decision_adapter(raw: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Normalize a Judge/Sentinel-shaped payload into a plain decision list.

    Accepts either the bare list ``run_decision_gates`` wants, or the
    ``{"decisions": [...]}`` envelope Judge's/Sentinel's own output uses.
    ``None`` or an envelope with no ``decisions`` key yields an empty list
    rather than raising -- the gate sequence's own fail-closed handling
    (``validate_decisions``) is what should decide what an empty or
    malformed decision set means, not this adapter. Never mutates ``raw``.
    """
    if raw is None:
        return []
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, Mapping)):
        return [dict(decision) for decision in raw]
    if isinstance(raw, Mapping):
        decisions = raw.get("decisions")
        if decisions is None:
            return []
        return [dict(decision) for decision in decisions]
    raise TypeError(f"unsupported legacy decision payload: {type(raw)!r}")


def legacy_files_adapter(files: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Normalize legacy ``dataset_files``-shaped entries into the ``files``
    projection ``run_decision_gates``/``assert_exact_coverage`` expect:
    ``file_id``, ``stored_path``, ``columns`` (``None`` when the schema
    could not be read), ``unreadable_reason``.

    Legacy call sites spell these fields differently depending on which
    agent produced them (Schema's cached manifest uses ``columns``;
    ``dataset_files`` uses ``stored_path``/``path``; some carry an explicit
    ``schema_error`` instead of ``unreadable_reason``), so each is accepted
    under any of its known aliases.
    """
    out: list[dict[str, Any]] = []
    for entry in files or []:
        file_id = entry.get("file_id") or entry.get("id") or ""
        columns = entry.get("columns")
        if columns is None:
            columns = entry.get("schema_columns") or entry.get("column_names")
        unreadable_reason = entry.get("unreadable_reason") or entry.get("schema_error") or ""
        out.append(
            {
                "file_id": file_id,
                "stored_path": entry.get("stored_path") or entry.get("path") or "",
                "columns": list(columns) if columns is not None else None,
                "unreadable_reason": unreadable_reason,
            }
        )
    return out
