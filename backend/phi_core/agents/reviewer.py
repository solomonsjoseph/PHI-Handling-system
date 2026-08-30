"""Reviewer: the one Reviewer role (docs #42), with two modes.

PREVIEW (docs #43, this class's ``preview`` method) independently
challenges Judge's decisions before execution: a deterministic checklist
pass (docs #91's "deterministic Evidence Gate") plus the LLM cross-check
that used to belong to the retired ``Sentinel`` role, unified into one
``PASS``/``CORRECTION_REQUIRED``/``HUMAN_REVIEW_REQUIRED`` verdict.
``Sentinel`` never existed as a role after this phase; its prompt and
review logic live here now, migrated verbatim.

The (existing, unchanged) completeness-audit mode below -- confirms
Operator covered every decision, not that Operator's per-cell checks were
correct (that is Operator's own job, and Operator's own tests) -- is the
FINAL-mode half of the same role (docs #42), formally dispatched in a
later phase; its method stays named ``run`` and its behavior is
untouched by this phase.

Operator's own completeness pass ("reverse completeness") synthesizes an
``undecided`` verdict for a written column that has neither a Judge
decision nor an ``omit_by_file`` entry -- but it never synthesizes
anything for a column that IS in ``omit_by_file`` with no matching
decision, because ``Operator.run``'s extra-records loop skips columns
already in ``omit_cols``. ``run`` closes exactly that gap: it
independently opens each file's real written export and confirms every
``omit_by_file`` column is genuinely absent, rather than trusting that
Executor honoured the deferral.

``run`` is a completeness audit of Operator's coverage against the raw
ground truth -- the decisions Judge actually produced, and the real
written column count -- never a re-trust of Operator's own
``status``/``failed_file_ids`` claims, and never a second re-derivation
of Operator's per-cell shape checks, which would just be Operator running
twice. It never calls an LLM and never logs a PHI value: every finding
and every log payload carries only file/column identifiers, counts, and
template text. ``preview`` is the only method on this class that calls
an LLM.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from phi_core.control.artifacts import ArtifactService
from phi_core.control.records import ReviewFinding
from phi_core.paths import artifact_id_from_export_alias

from .base import Agent
from .batching import run_batched
from .deterministic_rules import _HARD_RULE_TABLE
from .reasoning import _read_columns


class Reviewer(Agent):
    NAME = "Reviewer"
    # PREVIEW-mode prompt (migrated verbatim from the retired Sentinel
    # role). ``run`` (FINAL/completeness-audit mode) never calls an LLM
    # and ignores this prompt entirely.
    PROMPT = (
        "You are Reviewer, in PREVIEW mode. Review Judge's decisions with ONE goal: zero PHI "
        "leak, 100% accuracy. Cross-check every 'keep' against RegulationsExpert rules and "
        "Instrument fields. Flag any column whose action is inconsistent with its PHI category "
        "or citation.\n\n"
        "You are the only agent that may send a column to a human. Judge never does. For every "
        "issue, set severity honestly:\n"
        "  - 'blocking' when you know the correct action/category/method and Judge's decision is "
        "wrong -- state the correct value in `suggested_action`. This sends the column back to "
        "Judge for one more try, with your correction attached. Reserve for real leaks or clear "
        "regulatory/method mismatches (e.g. keep on a phone column, keep on a name column, drop on "
        "a study arm, hash used where RegulationsExpert requires zip3_truncate).\n"
        "  - 'escalate' when you disagree with Judge's decision but the correct answer is genuinely "
        "ambiguous -- you cannot state a confident correction yourself. This routes the column "
        "straight to a human, skipping further Judge iterations. Use this rarely: only for real "
        "regulatory ambiguity, not merely low confidence on Judge's part.\n"
        "  - 'advisory' for style, retention-policy nits, or preference between two safe transforms "
        "(e.g. hash vs pseudonymize when both close the leak). Advisory issues NEVER trigger a "
        "re-iteration or escalation; they are logged and included in the audit trail.\n\n"
        "Return JSON: "
        '{"verdict": "approved|revise", "issues": [{"file_id": str, "column": str, '
        '"problem": str, "suggested_action": str, "severity": "blocking|advisory|escalate"}], '
        '"summary": str}. '
        "Set verdict='approved' unless at least one blocking or escalate issue remains after your "
        "review. Nitpick sparingly and only where it materially reduces PHI risk."
    )

    async def preview(
        self,
        decisions: list[dict[str, Any]],
        statute: dict[str, Any] | None = None,
        instrument: dict[str, Any] | None = None,
        files: list[dict[str, Any]] | None = None,
        parent_id: str | None = None,
        deterministic_only: bool = False,
    ) -> dict[str, Any]:
        """Reviewer Preview mode (docs #42/#43).

        Runs the deterministic checklist first (docs #91: migrated
        Sentinel hard-rule/Evidence-Gate behavior), then -- unless
        ``deterministic_only`` -- the LLM cross-check the retired
        Sentinel role used to own. ``deterministic_only=True`` skips the
        LLM call entirely (used for the mandatory re-review after a
        human decision, docs #46, where the checklist alone is enough to
        catch a resolution that reintroduces an obvious hard-rule
        violation without spending another provider call or requiring
        one to be configured).

        Returns a dict shaped
        ``{"verdict": "approved"|"revise", "issues": [...], "summary": str,
          "preview_status": "PASS"|"CORRECTION_REQUIRED"|"HUMAN_REVIEW_REQUIRED",
          "findings": [ReviewFinding-shaped dict, ...]}``.
        ``verdict``/``issues`` keep the exact shape the orchestrator's
        Judge<->Reviewer loop (``_blocking_issues``/``_escalation_issues``)
        already consumes; ``preview_status``/``findings`` are the typed
        section-43 three-value contract.
        """
        findings = self._deterministic_checklist(decisions, files)
        issues: list[dict[str, Any]] = [self._finding_to_issue(f) for f in findings]
        summary = ""
        if not deterministic_only:
            prompt = (
                f"Judge decisions: {decisions}\n\n"
                f"RegulationsExpert rules: {statute}\n\n"
                f"Instrument fields: {instrument}\n"
                "Respond with JSON only. Remember: only 'blocking' severity triggers another iteration."
            )
            out = await self.call_json(
                prompt, phase="reviewer.preview",
                default={"verdict": "approved", "issues": []},
                parent_id=parent_id,
                status_text="Cross-checking Judge's decisions against RegulationsExpert and Instrument",
            )
            llm_issues = out.get("issues") or []
            issues.extend(llm_issues)
            summary = out.get("summary", "") or ""
        actionable = [i for i in issues if str(i.get("severity", "")).lower() in ("blocking", "escalate")]
        verdict = "approved" if not actionable else "revise"
        has_escalate = any(str(i.get("severity", "")).lower() == "escalate" for i in issues)
        has_blocking = any(str(i.get("severity", "")).lower() == "blocking" for i in issues)
        if has_escalate:
            preview_status = "HUMAN_REVIEW_REQUIRED"
        elif has_blocking:
            preview_status = "CORRECTION_REQUIRED"
        else:
            preview_status = "PASS"
        return {
            "verdict": verdict, "issues": issues, "summary": summary,
            "preview_status": preview_status, "findings": findings,
        }

    @staticmethod
    def _finding_to_issue(finding: dict[str, Any]) -> dict[str, Any]:
        """Adapt one deterministic-checklist ``ReviewFinding``-shaped dict
        into the legacy ``{file_id, column, problem, suggested_action,
        severity}`` issue shape the Judge<->Reviewer loop already
        understands."""
        verdict_to_severity = {
            "HUMAN_REVIEW_REQUIRED": "escalate",
            "CORRECTION_REQUIRED": "blocking",
            "PASS": "advisory",
        }
        return {
            "file_id": finding.get("file_id", ""),
            "column": finding.get("column", ""),
            "problem": finding.get("detail", ""),
            "suggested_action": "",
            "severity": verdict_to_severity.get(finding.get("verdict"), "advisory"),
        }

    @staticmethod
    def _deterministic_checklist(
        decisions: list[dict[str, Any]],
        files: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Section-43 checklist items this class can decide without an
        LLM call (docs #91's "deterministic Evidence Gate"):

        - "unsafe KEEP": a decision matching a known direct-identifier
          hard-rule pattern (``_HARD_RULE_TABLE``) still proposes 'keep'.
          This should never fire on real Judge output, since the hard
          rules already ran earlier in the same iteration -- it is
          defense-in-depth against a caller that skipped them (e.g. a
          human resolution that re-approves a 'keep'), so it is the one
          checklist item marked CORRECTION_REQUIRED/blocking severity.
        - "all files accounted": every dataset file named in ``files``
          has at least one decision. Advisory only -- during an
          in-progress Judge<->Reviewer negotiation this is expected to
          be incomplete on early iterations, so it must never block.
        - "missing evidence": a non-human_review decision carries no
          ``reason``. Advisory only, for the same reason.

        Every entry is a ``ReviewFinding``-shaped plain dict (mirrors
        ``control.records.ReviewFinding`` without requiring every caller
        to construct the pydantic model just to build an issue list).
        """
        findings: list[dict[str, Any]] = []
        decided_files: set[str] = set()
        for d in decisions:
            file_id = d.get("file_id", "")
            column = d.get("column", "")
            action = d.get("action")
            decided_files.add(file_id)
            col_norm = str(column or "").strip().lower().replace(" ", "_")
            if action == "keep" and any(
                re.match(pattern, col_norm) for pattern, *_ in _HARD_RULE_TABLE
            ):
                findings.append({
                    "verdict": "CORRECTION_REQUIRED", "file_id": file_id, "column": column,
                    "kind": "unsafe_keep",
                    "detail": (
                        f"Column '{column}' matches a known direct-identifier hard-rule pattern "
                        "but is still proposed as 'keep'."
                    ),
                })
            elif action and action != "human_review" and not (d.get("reason") or "").strip():
                findings.append({
                    "verdict": "PASS", "file_id": file_id, "column": column,
                    "kind": "missing_evidence",
                    "detail": f"Column '{column}' has action {action!r} with no recorded reason.",
                })
        if files:
            for f in files:
                file_id = f.get("file_id", "")
                if f.get("kind") == "dataset" and file_id and file_id not in decided_files:
                    findings.append({
                        "verdict": "PASS", "file_id": file_id, "column": "",
                        "kind": "file_not_yet_accounted",
                        "detail": f"File '{file_id}' has no decisions yet.",
                    })
        return findings


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
                findings.append(ReviewFinding(
                    verdict="CORRECTION_REQUIRED", file_id=file_id, column=column,
                    kind="missing_operator_verdict",
                    detail="Operator produced no verdict for this decision.",
                ).model_dump())
            decisions_checked = len(file_decisions)
            operator_verdicts_found = decisions_checked - missing

            # Omit-leak and coverage_mismatch both need the real written
            # header -- only meaningful for a file Operator actually
            # produced a readable export for. A file already in
            # failed_file_ids never reached a readable export (Operator
            # already flagged it); a _read_columns failure here must not
            # raise out of Reviewer, only skip these two checks.
            if file_id not in failed_file_ids and file_id in exports:
                # Reviewer has no `files` list to cross-check a registered
                # subtype against (unlike Operator) -- the export path's
                # own suffix is the only signal available for its ext.
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
                            findings.append(ReviewFinding(
                                verdict="CORRECTION_REQUIRED", file_id=file_id, column=column,
                                kind="omit_column_leaked",
                                detail="Column was marked for omission but is still "
                                       "present in the written export.",
                            ).model_dump())

                    file_verdicts = by_file_verdicts.get(file_id, [])
                    zero_fail = all(v.get("verdict") != "fail" for v in file_verdicts)
                    if zero_fail and decisions_checked != len(header):
                        findings.append(ReviewFinding(
                            verdict="CORRECTION_REQUIRED", file_id=file_id,
                            kind="coverage_mismatch",
                            detail=f"decision count ({decisions_checked}) does not "
                                   f"match the written column count ({len(header)})",
                        ).model_dump(exclude={"column"}))

            return {
                "file_id": file_id,
                "columns": sorted({d.get("column", "") for d in file_decisions}),
                "decisions_checked": decisions_checked,
                "operator_verdicts_found": operator_verdicts_found,
                "missing": missing,
                "findings": findings,
                "verdict": "issues" if (findings or file_id in failed_file_ids) else "clean",
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
                elif r["file_id"] in failed_file_ids:
                    status_text = (f"file {r['file_id']}: already flagged as unreadable "
                                    "or missing by Operator")
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
        artifact_service = getattr(self.ctx.artifacts, "_service", None)
        if isinstance(artifact_service, ArtifactService):
            findings_by_file: dict[str, list[dict[str, Any]]] = {}
            for finding in findings:
                findings_by_file.setdefault(finding["file_id"], []).append(finding)
            for file_id in blocked:
                export_path = exports.get(file_id)
                if not export_path:
                    continue
                artifact_id = artifact_id_from_export_alias(export_path)
                if not artifact_id:
                    continue
                kinds = sorted({str(finding.get("kind", "")) for finding in findings_by_file.get(file_id, [])})
                reason = (
                    f"Reviewer excluded export: {', '.join(kinds)}."
                    if kinds
                    else "Reviewer excluded export after an Operator failure."
                )
                await artifact_service.reject_export(
                    artifact_id=artifact_id,
                    file_path=export_path,
                    reason=reason,
                )
        filtered_exports = {fid: path for fid, path in exports.items() if fid not in blocked}

        return {
            "findings": findings,
            "status": status,
            "coverage": {"decisions": total_decisions, "verdicts": total_verdicts,
                         "missing": total_missing},
            "exports": filtered_exports,
        }
