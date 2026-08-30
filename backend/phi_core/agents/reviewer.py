"""Reviewer: the one Reviewer role (docs #42), with three modes.

PREVIEW (docs #43, this class's ``preview`` method) independently
challenges Judge's decisions before execution: a deterministic checklist
pass (docs #91's "deterministic Evidence Gate") plus the LLM cross-check
that used to belong to the retired ``Sentinel`` role, unified into one
``PASS``/``CORRECTION_REQUIRED``/``HUMAN_REVIEW_REQUIRED`` verdict.
``Sentinel`` never existed as a role after this phase; its prompt and
review logic live here now, migrated verbatim.

The (existing, unchanged) completeness-audit mode below -- confirms
DeterministicVerifier covered every decision, not that
DeterministicVerifier's per-cell checks were correct (that is its own
job, and its own tests) -- is the coverage-audit half of the FINAL role
(docs #42); its method stays named ``run`` and its behavior is untouched
by Phase 10.

DeterministicVerifier's own completeness pass ("reverse completeness")
synthesizes an ``undecided`` verdict for a written column that has
neither a Judge decision nor an ``omit_by_file`` entry -- but it never
synthesizes anything for a column that IS in ``omit_by_file`` with no
matching decision, because its extra-records loop skips columns already
in ``omit_cols``. ``run`` closes exactly that gap: it independently
opens each file's real written export and confirms every
``omit_by_file`` column is genuinely absent, rather than trusting that
Executor honoured the deferral.

``run`` is a completeness audit of DeterministicVerifier's coverage
against the raw ground truth -- the decisions Judge actually produced,
and the real written column count -- never a re-trust of
DeterministicVerifier's own ``status``/``failed_file_ids`` claims, and
never a second re-derivation of its per-cell shape checks, which would
just be DeterministicVerifier running twice. It never calls an LLM and
never logs a PHI value: every finding and every log payload carries only
file/column identifiers, counts, and template text.

FINAL (docs #55, this class's ``finalize`` method, Phase 10): the real
Reviewer Final gate. Consumes the frozen
``VerifiedClassificationManifest``, the raw-worker's ``ExecutionResult``,
``DeterministicVerifier``'s governed ``VerificationResult``, every
``HumanDecision`` on record for this run, and the safe (no-raw-value)
output metadata ``DeterministicVerifier.run`` additionally returns
(checksums/file counts/column counts/schema validity). Runs the full
section-55 checklist and returns one of ``PASS``/``FAIL``/
``HUMAN_REVIEW_REQUIRED`` -- never an LLM call, and never a value beyond
file/column identifiers and counts in any finding.

``preview`` is the only method on this class that calls an LLM.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from phi_core.control.artifacts import ArtifactService
from phi_core.control.records import (
    ExecutionResult,
    HumanDecision,
    ReviewFinding,
    VerificationResult,
    VerifiedClassificationManifest,
)
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
                re.match(pattern, col_norm)
                for pattern, allowed, *_ in _HARD_RULE_TABLE
                if "keep" not in allowed
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

    # ---- FINAL (docs #55, Phase 10) -----------------------------------

    _UNRESOLVED_HUMAN_ACTIONS = frozenset({"DEFER", "HOLD", "REQUEST_MORE_EVIDENCE"})

    async def finalize(
        self,
        *,
        manifest: VerifiedClassificationManifest,
        execution_result: ExecutionResult,
        verification_result: VerificationResult,
        decisions: list[dict[str, Any]],
        human_decisions: list[HumanDecision] | None = None,
        safe_output_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reviewer Final (docs #55): the real completeness/authorization/
        privacy/utility gate over an already-executed, already-verified
        run. Never calls an LLM; every finding carries only file/column
        identifiers, check names, and counts -- never a raw value.

        Runs the 8-item section-55 checklist against:
        - ``manifest``: the frozen ``VerifiedClassificationManifest`` that
          authorized this execution (``decision_refs``,
          ``human_review_refs``, ``unresolved_items``).
        - ``execution_result``: the raw worker's own success/failure
          report for this attempt.
        - ``verification_result``: ``DeterministicVerifier``'s governed
          ``VerificationResult`` (docs #54).
        - ``decisions``: the same decision list Executor actually ran
          (file_id/column/action; no row values).
        - ``human_decisions``: every ``HumanDecision`` on record for this
          run (may be empty for a run with no human review at all).
        - ``safe_output_metadata``: ``DeterministicVerifier.run``'s own
          additive section-54 fields (``column_counts``,
          ``schema_valid``) -- safe counts/booleans, never raw values.

        Returns ``{"verdict": "PASS"|"FAIL"|"HUMAN_REVIEW_REQUIRED",
        "checks": [{"name": str, "pass": bool, "detail": str}, ...],
        "findings": [ReviewFinding-shaped dict, ...],
        "signal": {"failure_class": str} | None}``. ``signal`` is the
        Phase 10 root-cause-classifier hint (``control.rewind
        .RewindRouter.classify`` consumes it directly); it is ``None``
        exactly when ``verdict == "PASS"``.
        """
        human_decisions = human_decisions or []
        safe_output_metadata = safe_output_metadata or {}
        checks: list[dict[str, Any]] = []

        def _check(name: str, ok: bool, detail: str = "") -> None:
            checks.append({"name": name, "pass": ok, "detail": detail})

        authorized_refs = set(manifest.decision_refs)
        deferred_refs = {
            f"{d.get('file_id', '')}:{d.get('column', '')}"
            for d in decisions if d.get("action") == "human_review"
        }
        executed_refs = {
            f"{d.get('file_id', '')}:{d.get('column', '')}"
            for d in decisions if d.get("action") not in (None, "human_review")
        }

        # 1. every approved action executed? (a deferred column is not
        #    "missing" -- it is legitimately not executed yet.)
        missing_execution = authorized_refs - executed_refs - deferred_refs
        _check("every_approved_action_executed", not missing_execution,
               f"{len(missing_execution)} authorized decision(s) never reached execution"
               if missing_execution else "")

        # 2. anything omitted? -- fewer verdicts than decisions for a file
        #    means DeterministicVerifier never actually checked something
        #    it was handed.
        column_counts = safe_output_metadata.get("column_counts") or {}
        omitted = sorted(
            fid for fid, counts in column_counts.items()
            if counts.get("verdicts", 0) < counts.get("decisions", 0)
        )
        _check("nothing_omitted", not omitted,
               f"file(s) with fewer verdicts than decisions: {omitted}" if omitted else "")

        # 3. anything unauthorized? -- an executed decision the manifest
        #    never named.
        unauthorized = executed_refs - authorized_refs
        _check("nothing_unauthorized", not unauthorized,
               f"{len(unauthorized)} executed decision(s) were never authorized by the manifest"
               if unauthorized else "")

        # 4. human decisions followed? -- every manifest human_review_ref
        #    must have a matching, recorded HumanDecision; nothing may be
        #    silently left unaddressed.
        resolved_decision_ids = {hd.decision_id for hd in human_decisions}
        unresolved_human_refs = [r for r in manifest.human_review_refs if r not in resolved_decision_ids]
        _check("human_decisions_followed", not unresolved_human_refs,
               f"{len(unresolved_human_refs)} human_review_refs have no recorded HumanDecision"
               if unresolved_human_refs else "")

        # 5. deterministic verification passed?
        _check("deterministic_verification_passed", verification_result.passed,
               "" if verification_result.passed
               else f"failed_checks={verification_result.failed_checks}")

        # 6. privacy intent preserved? -- no executed decision matches a
        #    known direct-identifier hard-rule pattern while still 'keep'.
        leaks = sorted({
            (d.get("file_id", ""), d.get("column", "")) for d in decisions
            if d.get("action") == "keep" and any(
                re.match(pattern, str(d.get("column") or "").strip().lower().replace(" ", "_"))
                for pattern, *_ in _HARD_RULE_TABLE
            )
        })
        _check("privacy_intent_preserved", not leaks,
               f"{len(leaks)} column(s) matching a direct-identifier pattern still kept" if leaks else "")

        # 7. utility requirement respected? -- the raw worker actually
        #    succeeded and every dataset DeterministicVerifier expected
        #    stayed schema-readable (a silently corrupted/unreadable
        #    export destroys the deliverable's research utility even
        #    when no single decision looks wrong in isolation).
        schema_valid = safe_output_metadata.get("schema_valid") or {}
        unreadable = sorted(fid for fid, ok in schema_valid.items() if not ok)
        _check("utility_requirement_respected", execution_result.success and not unreadable,
               "" if execution_result.success and not unreadable
               else f"execution_success={execution_result.success}, unreadable_files={unreadable}")

        # 8. unresolved issue? -- the manifest itself still carries
        #    unresolved_items, or a human decision explicitly deferred/
        #    held/asked for more evidence rather than resolving.
        pending_human_actions = [
            hd.decision_id for hd in human_decisions if hd.action in self._UNRESOLVED_HUMAN_ACTIONS
        ]
        unresolved = manifest.unresolved_items > 0 or bool(pending_human_actions) or bool(unresolved_human_refs)
        _check("no_unresolved_issue", not unresolved,
               f"unresolved_items={manifest.unresolved_items}, "
               f"pending_human_decisions={len(pending_human_actions)}" if unresolved else "")

        failed = [c for c in checks if not c["pass"]]
        human_review_triggers = {"human_decisions_followed", "no_unresolved_issue"}
        if not failed:
            verdict = "PASS"
        elif any(c["name"] in human_review_triggers for c in failed):
            verdict = "HUMAN_REVIEW_REQUIRED"
        else:
            verdict = "FAIL"

        findings = [
            ReviewFinding(
                verdict="HUMAN_REVIEW_REQUIRED" if verdict == "HUMAN_REVIEW_REQUIRED" else "CORRECTION_REQUIRED",
                kind=c["name"], detail=c["detail"],
            ).model_dump()
            for c in failed
        ]
        signal = None if verdict == "PASS" else self._finalize_signal(failed, execution_result)

        return {"verdict": verdict, "checks": checks, "findings": findings, "signal": signal}

    @staticmethod
    def _finalize_signal(failed: list[dict[str, Any]], execution_result: ExecutionResult) -> dict[str, str]:
        """Map Reviewer Final's failed checks onto a
        ``{"failure_class": <records.FailureClass member>}`` hint for
        ``control.rewind.RewindRouter.classify`` (docs #56). Fixed
        priority order when multiple checks fail simultaneously: an
        execution-side failure (nothing ran, or nothing readable) is the
        earliest, most upstream-actionable cause, so it takes precedence
        over a downstream method/regulation/human-review signal that
        might be a symptom of the same root cause rather than a second,
        independent problem."""
        failed_names = {c["name"] for c in failed}
        if (not execution_result.success or "every_approved_action_executed" in failed_names
                or "nothing_omitted" in failed_names or "utility_requirement_respected" in failed_names):
            return {"failure_class": "EXECUTION_ERROR"}
        if "nothing_unauthorized" in failed_names or "deterministic_verification_passed" in failed_names:
            return {"failure_class": "METHOD_ERROR"}
        if "privacy_intent_preserved" in failed_names:
            return {"failure_class": "REGULATION_ERROR"}
        # "human_decisions_followed" / "no_unresolved_issue"
        return {"failure_class": "HUMAN_REVIEW_REQUIRED"}
