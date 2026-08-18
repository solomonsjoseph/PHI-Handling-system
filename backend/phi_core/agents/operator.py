"""Operator: the deterministic self-verification layer between Executor and
Publish Guard.

Executor already applies Judge/Sentinel's decisions correctly in the common
case; what it lacks is any review of its own work. Operator is that review
layer: for every decision Executor received, it re-reads Executor's actual
written output and checks the decision was carried out -- completeness
first (every decision has a corresponding outcome, every written column
has a decision or was deliberately deferred), then a shape check per
transform action.

Operator never re-runs detector-based row scanning (that stays with
``verify_keep_decisions`` and Publish Guard) and never calls an LLM: every
check here is a deterministic comparison against Executor's own written
bytes and the decision list's shape metadata.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .base import Agent
from .batching import run_batched
from .reasoning import _read_dataset_headers, _scrub_text_cell
from ..file_readers import iter_dataset_rows


def _cap_age_90_ok(value: str) -> bool:
    if value == "90+":
        return True
    if value.isdigit():
        return 0 <= int(value) <= 89
    return False


_YEAR_ONLY_RE = re.compile(r"^\d{4}$")
_ZIP3_RE = re.compile(r"^\d{3}$")
_HASH_RE = re.compile(r"^[0-9a-f]{16}$")
_PSEUDONYM_RE = re.compile(r"^P[0-9a-f]{8}$")

# One shape predicate per transform action, checked over every non-empty
# written cell. Matches _apply_action's/PseudonymRegistry's real output
# shapes in reasoning.py -- never a detector re-run, just "does this look
# like what the action was supposed to produce."
_SHAPE_CHECKS = {
    "cap_age_90": _cap_age_90_ok,
    "year_only": lambda v: bool(_YEAR_ONLY_RE.match(v)),
    "zip3_truncate": lambda v: bool(_ZIP3_RE.match(v)),
    "hash": lambda v: bool(_HASH_RE.match(v)),
    "pseudonymize": lambda v: bool(_PSEUDONYM_RE.match(v)),
}


def _read_columns(path: str, ext: str) -> tuple[list[str], dict[str, list[str]]]:
    """One ``iter_dataset_rows`` pass over a dataset file: header order plus,
    per column, every row's value in row order (empty cells included, so a
    row-aligned source/written comparison stays possible). Raises on a
    corrupt or unsupported file -- callers isolate that per file_id rather
    than letting it abort the whole run."""
    header: list[str] = []
    columns: dict[str, list[str]] = {}
    for _row_index, row in iter_dataset_rows(Path(path), ext):
        if not header:
            header = list(row.keys())
            for name in header:
                columns[name] = []
        for name in header:
            columns[name].append(row.get(name, ""))
    if not header:
        # Zero data rows is a valid, empty dataset -- iter_dataset_rows has
        # nothing to yield a header from, so fall back to the real on-disk
        # header rather than reporting every column missing. Never raises
        # (returns an empty set on a genuinely unreadable file), so a real
        # read failure still surfaces through the loop above, not here.
        header = sorted(_read_dataset_headers(Path(path), ext))
        columns = {name: [] for name in header}
    return header, columns


def _verify_record(record: dict[str, Any], view: dict[str, Any] | None) -> dict[str, Any]:
    """Check one four-part work record against its file's pre-loaded
    written (and, for scrub_text, source) column view. Never opens a file:
    ``view`` was built once per file before any record was checked."""
    verdict = dict(record)
    column = record["column"]
    action = record["method"]

    if view is None:
        # Executor never wrote this file at all (write failure, every known
        # column deferred to omit_by_file, a decision naming a file_id
        # Operator has never heard of), OR the written file exists but
        # could not be read (corrupt/unsupported export) -- either way
        # nothing in it is verifiable, so every decision for it is a hard
        # failure. Worded to cover both causes rather than implying the
        # file was never written when it may simply be unreadable.
        verdict.update(
            checks=[],
            verdict="fail",
            problem=f"file {record['file_id']!r} is missing from exports or could not be read",
            performed="nothing verifiable: the file never reached exports or could not be read",
        )
        return verdict

    header = view["header"]
    written = view["written"]

    if action == "undecided":
        # Reverse completeness: this column exists in the written output
        # but no decision names it and it isn't a deliberate omission
        # either -- it is invisible to Judge/Sentinel entirely. By
        # construction (see run()) this column is always present and never
        # in omit, so no further lookup is needed.
        verdict.update(
            checks=[{"name": "has_decision", "pass": False}],
            verdict="fail",
            problem="written column has no Judge/Sentinel decision and is not marked omitted",
            performed="column present in the written output with no matching decision",
        )
        return verdict

    present = column in header

    if column in view["omit"]:
        ok = not present
        verdict.update(
            checks=[{"name": "omit_expected", "pass": ok}],
            verdict="pass" if ok else "fail",
            problem="" if ok else "column was supposed to be omitted but is present in output",
            performed="column omitted as expected" if ok
            else "column present in output despite being marked omitted",
        )
        return verdict

    if not present:
        # A stale/misspelled column name from Judge/Sentinel that never
        # matched a real column -- finding 12, surfaced rather than
        # silently ignored the way Executor itself leaves it.
        verdict.update(
            checks=[{"name": "column_presence", "pass": False}],
            verdict="fail",
            problem="decision has no corresponding column in the written output",
            performed="no matching column found in the written output",
        )
        return verdict

    cells = written.get(column, [])
    non_empty = [v for v in cells if v != ""]

    if action == "drop":
        ok = len(non_empty) == 0
        verdict.update(
            checks=[{"name": "column_presence", "pass": True}, {"name": "drop_empty", "pass": ok}],
            verdict="pass" if ok else "fail",
            problem="" if ok else "drop column left populated",
            performed="column present, all cells empty" if ok
            else f"column present, {len(non_empty)} non-empty cell(s) remain",
        )
        return verdict

    if action == "keep":
        verdict.update(
            checks=[{"name": "column_presence", "pass": True}],
            verdict="pass",
            problem="",
            performed="column present, left unchanged",
        )
        return verdict

    shape_check = _SHAPE_CHECKS.get(action)
    if shape_check is not None:
        ok = all(shape_check(v) for v in non_empty)
        verdict.update(
            checks=[{"name": "column_presence", "pass": True}, {"name": "shape", "pass": ok}],
            verdict="pass" if ok else "fail",
            # Never the raw offending value -- a malformed cap_age_90 cell
            # could still be sensitive-shaped, so only the failure of the
            # shape check itself is reported, never the cell's content.
            problem="" if ok else f"{action}: a written cell failed the expected output shape check",
            performed=f"column transformed via {action}, {len(non_empty)} value(s) checked" if ok
            else f"column transformed via {action}, one or more values fail the expected shape",
        )
        return verdict

    if action == "scrub_text":
        if view.get("source_error"):
            # The source Executor itself read (stored_path) is missing or
            # unreadable -- change-detection is impossible, so this fails
            # closed rather than vacuously passing for lack of evidence.
            verdict.update(
                checks=[{"name": "column_presence", "pass": True}, {"name": "scrub_ran", "pass": False}],
                verdict="fail",
                problem="cannot verify scrub_text ran",
                performed="source could not be read; scrub_text cannot be verified",
            )
            return verdict
        source = view.get("source") or {}
        source_cells = source.get(column, [])
        # Row-aligned comparison against the original, only where the
        # source cell actually had something to scrub. Change-detection
        # only: no value from either side is ever placed in the verdict.
        relevant = [(s, w) for s, w in zip(source_cells, cells) if s != ""]
        changed = any(s != w for s, w in relevant)
        # A column can legitimately have nothing to scrub: every relevant
        # cell coming through unchanged from source is correct, not a
        # leak, as long as none of the written cells still contain
        # anything _scrub_text_cell would redact -- i.e. the written
        # output is already a fixed point of the same scrub function.
        stable = all(_scrub_text_cell(v) == v for v in non_empty)
        ok = changed or stable
        verdict.update(
            checks=[{"name": "column_presence", "pass": True}, {"name": "scrub_ran", "pass": ok}],
            verdict="pass" if ok else "fail",
            problem="" if ok else "scrub_text produced no observable change",
            performed="scrub_text ran, at least one cell changed" if changed
            else ("scrub_text ran, output already clean" if ok
                  else "scrub_text produced no observable change from source"),
        )
        return verdict

    # Unrecognized action reaching Operator (should not happen: Executor's
    # own decision vocabulary is closed and human_review never survives to
    # here) -- fail closed rather than silently reporting a pass Operator
    # never actually verified.
    verdict.update(
        checks=[{"name": "column_presence", "pass": True}, {"name": "known_action", "pass": False}],
        verdict="fail",
        problem=f"unrecognized action {action!r}; Operator cannot verify it",
        performed="action not recognized by Operator's verification rules",
    )
    return verdict


class Operator(Agent):
    NAME = "Operator"
    PROMPT = ""  # deterministic; Operator never calls an LLM

    async def run(self, files: list[dict[str, Any]], decisions: list[dict[str, Any]],
                  exports: dict[str, str],
                  omit_by_file: dict[str, set[str]] | None = None) -> dict[str, Any]:
        """Re-check every value Executor wrote against the decision Judge/
        Sentinel settled on. Only decisions whose file maps to a dataset
        file get a verdict: metadata/narrative files never carry per-column
        decisions in this pipeline. A decision naming a file_id Operator
        has never heard of is still surfaced as a failure rather than
        silently dropped.

        Returns ``{"verdicts": [...], "failed_file_ids": [...],
        "status": "clean" | "issues"}``.
        """
        omit_by_file = omit_by_file or {}
        files_by_id = {f.get("file_id"): f for f in files}
        dataset_ids = {fid for fid, f in files_by_id.items() if f.get("kind") == "dataset"}

        by_file: dict[str, list[dict[str, Any]]] = {}
        for d in decisions:
            fid = d.get("file_id", "")
            if fid in dataset_ids or fid not in files_by_id:
                by_file.setdefault(fid, []).append(d)
        # A dataset file Executor wrote with zero decisions at all (every
        # column fell through Executor's fail-closed default) must still
        # go through reverse completeness below -- seed it with an empty
        # group rather than skipping it because no decision named it.
        for fid in dataset_ids:
            if fid in exports:
                by_file.setdefault(fid, [])

        views: dict[str, dict[str, Any] | None] = {}
        failed_file_ids: list[str] = []
        extra_records: list[dict[str, Any]] = []
        for file_id, group in by_file.items():
            if file_id not in exports:
                views[file_id] = None
                failed_file_ids.append(file_id)
                continue
            f = files_by_id.get(file_id) or {}
            ext = f.get("subtype", "")
            try:
                header, written = _read_columns(exports[file_id], ext)
            except Exception:
                # A corrupt or unsupported written file must not crash the
                # whole run or silently pass -- isolate the failure to
                # this file_id; every sibling file is still checked.
                views[file_id] = None
                failed_file_ids.append(file_id)
                continue

            source: dict[str, list[str]] | None = None
            source_error = False
            if any(d.get("action") == "scrub_text" for d in group):
                src_path = f.get("stored_path")
                if not src_path:
                    source_error = True
                else:
                    try:
                        _src_header, source = _read_columns(src_path, ext)
                    except Exception:
                        source = None
                        source_error = True

            omit_cols = set(omit_by_file.get(file_id, ()) or ())
            views[file_id] = {
                "header": header,
                "written": written,
                "source": source,
                "source_error": source_error,
                "omit": omit_cols,
            }

            # Reverse completeness: every column in the written output
            # must have a decision or be a deliberate omission. A column
            # with neither is invisible to Judge/Sentinel entirely.
            decided_columns = {d.get("column", "") for d in group}
            for column in header:
                if column not in decided_columns and column not in omit_cols:
                    extra_records.append({
                        "file_id": file_id, "column": column, "violation": {}, "method": "undecided",
                    })

        records: list[dict[str, Any]] = [
            {
                "file_id": file_id,
                "column": d.get("column", ""),
                "violation": {"phi_category": d.get("phi_category"), "citation": d.get("citation")},
                "method": d.get("action"),
            }
            for file_id, group in by_file.items()
            for d in group
        ] + extra_records

        def _check_batch(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [_verify_record(r, views.get(r["file_id"])) for r in batch]

        async def _on_batch(index: int, results: list[dict[str, Any]]) -> None:
            n_pass = sum(1 for r in results if r["verdict"] == "pass")
            n_fail = len(results) - n_pass
            await self._log(f"operator.batch:{index}", "info",
                            {"pass": n_pass, "fail": n_fail, "count": len(results)})

        verdicts = await run_batched(records, _check_batch, batch_size=8, pool_size=6,
                                     on_batch=_on_batch)

        status = "issues" if failed_file_ids or any(v["verdict"] == "fail" for v in verdicts) else "clean"
        return {"verdicts": verdicts, "failed_file_ids": failed_file_ids, "status": status}
