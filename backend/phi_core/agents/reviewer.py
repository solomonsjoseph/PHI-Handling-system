"""Reviewer: confirms Operator covered every decision, not that Operator's
per-cell checks were correct (that is Operator's own job, and Operator's
own tests).

Operator's own completeness pass ("reverse completeness") synthesizes an
``undecided`` verdict for a written column that has neither a Judge/
Sentinel decision nor an ``omit_by_file`` entry -- but it never
synthesizes anything for a column that IS in ``omit_by_file`` with no
matching decision, because ``Operator.run``'s extra-records loop skips
columns already in ``omit_cols``. Reviewer closes exactly that gap: it
independently opens each file's real written export and confirms every
``omit_by_file`` column is genuinely absent, rather than trusting that
Executor honoured the deferral.

Reviewer is a completeness audit of Operator's coverage against the raw
ground truth -- the decisions Judge/Sentinel actually produced, and the
real written column count -- never a re-trust of Operator's own
``status``/``failed_file_ids`` claims, and never a second re-derivation
of Operator's per-cell shape checks, which would just be Operator running
twice. It never calls an LLM and never logs a PHI value: every finding
and every log payload carries only file/column identifiers, counts, and
template text.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import Agent
from .batching import run_batched
from .operator import _read_columns


class Reviewer(Agent):
    NAME = "Reviewer"
    PROMPT = ""  # deterministic; Reviewer never calls an LLM

    async def run(self, decisions: list[dict[str, Any]], operator_result: dict[str, Any],
                  exports: dict[str, str],
                  omit_by_file: dict[str, set[str]] | None = None) -> dict[str, Any]:
        """Audit Operator's coverage per file_id: every decision Judge/
        Sentinel produced has a matching Operator verdict, every
        ``omit_by_file`` column is genuinely absent from the written
        export, and a file Operator reported zero failures for still has
        its decision count match its real written column count.

        Returns ``{"findings": [...], "status": "clean" | "issues",
        "coverage": {"decisions": n, "verdicts": n, "missing": n},
        "exports": {...}}`` where ``exports`` is a filtered copy of the
        input, excluding every file with at least one finding or already
        in Operator's ``failed_file_ids``. The input ``exports`` dict is
        never mutated.
        """
        omit_by_file = omit_by_file or {}
        failed_file_ids = set(operator_result.get("failed_file_ids", []) or [])

        by_file_decisions: dict[str, list[dict[str, Any]]] = {}
        for d in decisions:
            by_file_decisions.setdefault(d.get("file_id", ""), []).append(d)

        by_file_verdicts: dict[str, list[dict[str, Any]]] = {}
        for v in operator_result.get("verdicts", []) or []:
            by_file_verdicts.setdefault(v.get("file_id", ""), []).append(v)

        file_ids = (set(by_file_decisions) | set(by_file_verdicts)
                    | set(exports) | failed_file_ids | set(omit_by_file))

        def _check_file(file_id: str) -> dict[str, Any]:
            file_decisions = by_file_decisions.get(file_id, [])
            verdict_columns = {v.get("column", "") for v in by_file_verdicts.get(file_id, [])}

            findings: list[dict[str, Any]] = []
            missing = 0
            for d in file_decisions:
                column = d.get("column", "")
                if column in verdict_columns:
                    continue
                missing += 1
                findings.append({
                    "file_id": file_id, "column": column, "kind": "missing_operator_verdict",
                    "detail": "Operator produced no verdict for this decision.",
                })
            decisions_checked = len(file_decisions)
            operator_verdicts_found = decisions_checked - missing

            # Omit-leak and coverage_mismatch both need the real written
            # header -- only meaningful for a file Operator actually
            # produced a readable export for. A file already in
            # failed_file_ids never reached a readable export (Operator
            # already flagged it); a _read_columns failure here must not
            # raise out of Reviewer, only skip these two checks.
            if file_id not in failed_file_ids and file_id in exports:
                ext = Path(exports[file_id]).suffix.lstrip(".").lower()
                try:
                    header, _written = _read_columns(exports[file_id], ext)
                except Exception:
                    header = None

                if header is not None:
                    omit_cols = set(omit_by_file.get(file_id, ()) or ())
                    header_set = set(header)
                    for column in sorted(omit_cols):
                        if column in header_set:
                            findings.append({
                                "file_id": file_id, "column": column, "kind": "omit_column_leaked",
                                "detail": "Column was marked for omission but is still "
                                          "present in the written export.",
                            })

                    file_verdicts = by_file_verdicts.get(file_id, [])
                    zero_fail = all(v.get("verdict") != "fail" for v in file_verdicts)
                    if zero_fail and decisions_checked != len(header):
                        findings.append({
                            "file_id": file_id, "kind": "coverage_mismatch",
                            "detail": f"decision count ({decisions_checked}) does not "
                                      f"match the written column count ({len(header)})",
                        })

            return {
                "file_id": file_id,
                "columns": sorted({d.get("column", "") for d in file_decisions}),
                "decisions_checked": decisions_checked,
                "operator_verdicts_found": operator_verdicts_found,
                "missing": missing,
                "findings": findings,
                "verdict": "clean" if not findings else "issues",
            }

        def _check_batch(batch: list[str]) -> list[dict[str, Any]]:
            return [_check_file(fid) for fid in batch]

        async def _on_batch(_index: int, results: list[dict[str, Any]]) -> None:
            for r in results:
                payload = {
                    "file_id": r["file_id"], "columns": r["columns"],
                    "decisions_checked": r["decisions_checked"],
                    "operator_verdicts_found": r["operator_verdicts_found"],
                    "missing": r["missing"], "verdict": r["verdict"],
                }
                if r["verdict"] == "clean":
                    status_text = (f"file {r['file_id']}: all {r['decisions_checked']} "
                                    "decision(s) have a matching Operator verdict")
                else:
                    status_text = (f"file {r['file_id']}: {r['missing']} of "
                                    f"{r['decisions_checked']} decision(s) missing an "
                                    "Operator verdict, or another coverage issue found")
                await self._log("review.coverage_check", "info", payload, status_text=status_text)

        results = await run_batched(sorted(file_ids), _check_batch, batch_size=8, pool_size=6,
                                     on_batch=_on_batch)

        findings: list[dict[str, Any]] = []
        total_decisions = total_verdicts = total_missing = 0
        blocked: set[str] = set(failed_file_ids)
        for r in results:
            findings.extend(r["findings"])
            total_decisions += r["decisions_checked"]
            total_verdicts += r["operator_verdicts_found"]
            total_missing += r["missing"]
            if r["findings"]:
                blocked.add(r["file_id"])

        status = "issues" if findings or failed_file_ids else "clean"
        filtered_exports = {fid: path for fid, path in exports.items() if fid not in blocked}

        return {
            "findings": findings,
            "status": status,
            "coverage": {"decisions": total_decisions, "verdicts": total_verdicts,
                         "missing": total_missing},
            "exports": filtered_exports,
        }
