"""What_Happened_to_Each_Column.xlsx (docs #58/#59): the per-column audit
ledger. Every row is built from real, already-typed Phases 7-10 records
(``ColumnDecision``, ``ReviewFinding``, ``HumanReviewEvent``,
``ExecutionResult``, ``VerificationResult``) -- never from raw study
values. Only safe display strings already produced by Judge/Reviewer/
Executor (``safe_display_name``, plain-language reasons, method names)
reach a cell; column headers/data never carry a raw sensitive source
value, matching docs #60's "sensitive source names must use aliases".

Section 59 lists nineteen columns "approximately" -- the exact set below
is the concrete nineteen this module emits, in the spec's own order.
"Every logical dataset column must appear": :func:`build_column_ledger_rows`
emits exactly one row per distinct ``(file_id, column_id)`` pair seen
across ``decisions``, regardless of how many correction rounds that
column went through.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import openpyxl

from .records import (
    ColumnDecision,
    ExecutionResult,
    HumanReviewEvent,
    ReviewFinding,
    VerificationResult,
)

# Section 59's column list, verbatim order.
COLUMN_LEDGER_HEADERS: tuple[str, ...] = (
    "Dataset File / Safe Dataset Name",
    "Column / Safe Column Name",
    "What It Means",
    "Sensitivity Classification",
    "Initial Proposed Action",
    "Final Approved Action",
    "What We Did",
    "Why",
    "Applicable Rule / Policy",
    "Method Used",
    "Method Version",
    "Reviewer Result",
    "Correction Made?",
    "Human Review?",
    "Human Decision",
    "Executor Result",
    "Final Verification",
    "Decision ID",
    "Evidence References",
)

# Plain-language phrasing for each ColumnOperation value (docs #41). Falls
# back to the raw operation string for any value not listed here, so an
# operation added later never silently loses its ledger row.
_OPERATION_PHRASES: Mapping[str, str] = {
    "keep": "Kept the column unchanged",
    "drop": "Removed the column",
    "pseudonymize": "Replaced values with a pseudonymous token",
    "shift": "Shifted values by a random offset",
    "date_shift": "Shifted dates by a per-subject random offset",
    "jitter": "Added random noise to the values",
    "generalize": "Generalized values to a broader category",
    "cap": "Capped values at a safe threshold",
    "redact": "Redacted the values",
    "other_approved_action": "Applied an approved alternate action",
}


@dataclass(frozen=True)
class ColumnLedgerRow:
    """One logical dataset column's full section-59 row. Every field is a
    plain, already-safe display string -- never a raw study value."""

    dataset_file: str
    column: str
    what_it_means: str
    sensitivity_classification: str
    initial_proposed_action: str
    final_approved_action: str
    what_we_did: str
    why: str
    applicable_rule: str
    method_used: str
    method_version: str
    reviewer_result: str
    correction_made: str
    human_review: str
    human_decision: str
    executor_result: str
    final_verification: str
    decision_id: str
    evidence_references: str

    def as_tuple(self) -> tuple[str, ...]:
        return (
            self.dataset_file, self.column, self.what_it_means,
            self.sensitivity_classification, self.initial_proposed_action,
            self.final_approved_action, self.what_we_did, self.why,
            self.applicable_rule, self.method_used, self.method_version,
            self.reviewer_result, self.correction_made, self.human_review,
            self.human_decision, self.executor_result, self.final_verification,
            self.decision_id, self.evidence_references,
        )


def _describe_operation(operation: str) -> str:
    return _OPERATION_PHRASES.get(operation, operation)


def _executor_result_text(execution_result: ExecutionResult | None) -> str:
    if execution_result is None:
        return ""
    if execution_result.success:
        return "succeeded"
    detail = execution_result.failure_class or execution_result.error_code or execution_result.detail
    return f"failed: {detail}" if detail else "failed"


def _final_verification_text(verification_result: VerificationResult | None) -> str:
    if verification_result is None:
        return ""
    if verification_result.passed:
        return "passed"
    if verification_result.failed_checks:
        return f"failed: {'; '.join(verification_result.failed_checks)}"
    return "failed"


def _reviewer_result_text(final: ColumnDecision, review_findings: Sequence[ReviewFinding]) -> str:
    matches = [f for f in review_findings if f.file_id == final.file_id and f.column == final.column_id]
    if not matches:
        return "N/A"
    latest = matches[-1]
    return f"{latest.verdict}: {latest.detail}" if latest.detail else latest.verdict


def _human_review_fields(final: ColumnDecision, human_review_events: Sequence[HumanReviewEvent]) -> tuple[str, str]:
    resolutions = [
        r for ev in human_review_events for r in ev.resolutions
        if r.file_id == final.file_id and r.column == final.column_id
    ]
    if not resolutions:
        return "No", ""
    latest = resolutions[-1]
    decision_text = f"{latest.mode}: {latest.comment}" if latest.comment else latest.mode
    return "Yes", decision_text


def build_column_ledger_rows(
    decisions: Sequence[ColumnDecision],
    *,
    review_findings: Sequence[ReviewFinding] = (),
    human_review_events: Sequence[HumanReviewEvent] = (),
    execution_result: ExecutionResult | None = None,
    verification_result: VerificationResult | None = None,
    method_names: Mapping[str, str] | None = None,
) -> list[ColumnLedgerRow]:
    """Group ``decisions`` by logical column (``file_id``, ``column_id``)
    and build one row per group, in first-seen order.

    ``Executor Result`` and ``Final Verification`` reflect ``execution_result``/
    ``verification_result`` as given -- both are single per-task/per-run
    records (docs #50-54), not per-column, so every row in one run shares
    the same value for these two fields; that is the honest shape of the
    underlying data, not an omission.
    """
    method_names = method_names or {}
    groups: dict[tuple[str, str], list[ColumnDecision]] = {}
    for decision in decisions:
        key = (decision.file_id, decision.column_id)
        groups.setdefault(key, []).append(decision)

    rows: list[ColumnLedgerRow] = []
    for chain in groups.values():
        ordered = sorted(chain, key=lambda d: d.created_at)
        initial, final = ordered[0], ordered[-1]

        dataset_file = final.file_id
        if final.dataset_part_id:
            dataset_file = f"{dataset_file}/{final.dataset_part_id}"

        evidence_refs = list(final.semantic_evidence_refs) + list(final.regulatory_evidence_refs)
        human_review, human_decision = _human_review_fields(final, human_review_events)

        rows.append(ColumnLedgerRow(
            dataset_file=dataset_file,
            column=final.safe_display_name or final.column_id,
            what_it_means=final.semantic_meaning,
            sensitivity_classification=final.sensitivity_classification,
            initial_proposed_action=initial.operation,
            final_approved_action=final.operation,
            what_we_did=_describe_operation(final.operation),
            why=final.plain_language_reason,
            applicable_rule=final.applicable_rule,
            method_used=method_names.get(final.method_id, final.method_id),
            method_version=str(final.method_version) if final.method_version else "",
            reviewer_result=_reviewer_result_text(final, review_findings),
            correction_made="Yes" if len(ordered) > 1 else "No",
            human_review=human_review,
            human_decision=human_decision,
            executor_result=_executor_result_text(execution_result),
            final_verification=_final_verification_text(verification_result),
            decision_id=final.decision_id,
            evidence_references="; ".join(evidence_refs),
        ))
    return rows


def write_column_ledger_xlsx(rows: Sequence[ColumnLedgerRow], path: Path) -> Path:
    """Write the section-59 workbook. Uses openpyxl -- the same library
    ``publish_guard._scan_xlsx`` already reads with -- rather than adding
    a second xlsx dependency."""
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Column Ledger"
    ws.append(list(COLUMN_LEDGER_HEADERS))
    for row in rows:
        ws.append(list(row.as_tuple()))
    wb.save(path)
    return path
