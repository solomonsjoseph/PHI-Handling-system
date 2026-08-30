"""PHI_Handling_Audit_Report.pdf, Technical_Appendix.pdf, and
Human_Review_Summary.pdf (docs #58): built with reportlab's platypus API
from structured data, never from arbitrary HTML -- reportlab (BSD) avoids
the HTML-injection surface into content ReportingSafetyGate (docs #60)
must scan, which a weasyprint-style HTML-to-PDF pipeline would introduce.

Every builder function returns both the written ``Path`` and the plain
text it assembled, so a caller (a test, or Packaging/Integration's
``ZIPBuilder``) can inspect content without a second implementation --
``ZIPBuilder`` itself independently re-extracts via ``file_readers.read_pdf``
before running the reporting-safety scan, so these two text
representations only need to agree in substance, not byte-for-byte.

Every value placed on a page already comes from a safe-display field on a
typed control record (``ColumnLedgerRow``, ``EvidenceRecord``,
``RunManifest``, ``HumanDecision``/``ResolutionEntry``) -- this module
never reads a raw dataset cell or raw source header.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .column_ledger import ColumnLedgerRow
from .final_assurance import ReviewerFinalResult
from .records import (
    EvidenceRecord,
    ExecutionResult,
    HumanDecision,
    HumanReviewEvent,
    RunManifest,
    VerificationResult,
    VerifiedClassificationManifest,
)

_STYLES = getSampleStyleSheet()
_TABLE_STYLE = TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2b3a55")),
    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("FONTSIZE", (0, 0), (-1, -1), 8),
    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0f2f7")]),
])


def _build_doc(path: Path) -> SimpleDocTemplate:
    path.parent.mkdir(parents=True, exist_ok=True)
    return SimpleDocTemplate(
        str(path), pagesize=letter,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        title=path.stem,
    )


def _para(text: str) -> Paragraph:
    """A table-cell/body paragraph. Escapes reportlab's mini-XML markup
    characters before rendering so a safe-display string that happens to
    contain ``<``/``>``/``&`` never breaks (or is misread as) markup."""
    safe = escape(text) if text else "-"
    return Paragraph(safe.replace("\n", "<br/>"), _STYLES["BodyText"])


def _table(
    headers: Sequence[str], rows: Sequence[Sequence[str]], empty_note: str,
    col_widths: Sequence[float] | None = None,
) -> list:
    """One flowable-list fragment: a styled table, or a plain note when
    there are no rows -- never an empty (invalid) reportlab Table.

    ``col_widths`` should always be given for a table with more than a
    couple of columns: reportlab's auto-width algorithm for
    ``Paragraph``-cell tables can compute columns too narrow for a single
    word, producing mid-word line breaks that survive into ``pypdf``'s
    text extraction (e.g. ``APPROVE`` -> ``APPROV``/``E`` on two lines).
    """
    if not rows:
        return [Paragraph(empty_note, _STYLES["BodyText"])]
    data = [list(headers)] + [[_para(c) for c in row] for row in rows]
    table = Table(data, colWidths=list(col_widths) if col_widths else None, repeatRows=1)
    table.setStyle(_TABLE_STYLE)
    return [table]


def build_audit_report_pdf(
    *,
    manifest: VerifiedClassificationManifest,
    rows: Sequence[ColumnLedgerRow],
    execution_result: ExecutionResult,
    verification_result: VerificationResult,
    reviewer_final: ReviewerFinalResult,
    path: Path,
) -> tuple[Path, str]:
    """docs #58's primary report: plain-language, safe-display-only."""
    corrections = sum(1 for r in rows if r.correction_made == "Yes")
    human_reviewed = sum(1 for r in rows if r.human_review == "Yes")
    overall = (
        "PASSED"
        if verification_result.passed and execution_result.success and reviewer_final.verdict == "PASS"
        else "DID NOT PASS"
    )

    summary_lines = [
        f"Run: {manifest.run_id}",
        f"Overall result: {overall}",
        f"Columns processed: {len(rows)}",
        f"Columns corrected during review: {corrections}",
        f"Columns that went to human review: {human_reviewed}",
        f"Executor result: {'succeeded' if execution_result.success else 'failed'}",
        f"Deterministic verification: {'passed' if verification_result.passed else 'failed'} "
        f"({verification_result.manifest_coverage_percent}% column coverage)",
    ]

    story: list = [
        Paragraph("PHI Handling Audit Report", _STYLES["Title"]),
        Spacer(1, 0.15 * inch),
        Paragraph(
            "This report describes, in plain language, what was found in the study data, "
            "what action was taken on each column, and why. It confirms the final result of the "
            "automated review, correction, and human-oversight process for this run.",
            _STYLES["BodyText"],
        ),
        Spacer(1, 0.2 * inch),
    ]
    story.extend(Paragraph(line, _STYLES["BodyText"]) for line in summary_lines)
    story.append(Spacer(1, 0.25 * inch))
    story.append(Paragraph("What happened to each column", _STYLES["Heading2"]))
    story.append(Spacer(1, 0.1 * inch))

    table_headers = ["Dataset", "Column", "What It Means", "Final Action", "What We Did", "Why"]
    table_rows = [
        (r.dataset_file, r.column, r.what_it_means, r.final_approved_action, r.what_we_did, r.why)
        for r in rows
    ]
    audit_col_widths = [w * inch for w in (0.9, 0.9, 1.4, 0.9, 1.4, 1.6)]
    story.extend(_table(table_headers, table_rows, "No columns were processed in this run.", audit_col_widths))

    doc = _build_doc(path)
    doc.build(story)

    text_lines = ["PHI Handling Audit Report", *summary_lines, "What happened to each column:"]
    for r in rows:
        text_lines.append(
            f"{r.dataset_file} / {r.column}: {r.what_it_means} -> {r.final_approved_action} "
            f"({r.what_we_did}). {r.why}"
        )
    return path, "\n".join(text_lines)


def build_technical_appendix_pdf(
    *,
    manifest: VerifiedClassificationManifest,
    rows: Sequence[ColumnLedgerRow],
    evidence_records: Sequence[EvidenceRecord],
    run_manifest: RunManifest,
    verification_result: VerificationResult,
    path: Path,
) -> tuple[Path, str]:
    """docs #58's technical appendix: deeper provenance than the primary
    report -- method versions, applicable rules, evidence references, and
    the docs #63 reproducibility metadata."""
    story: list = [
        Paragraph("Technical Appendix", _STYLES["Title"]),
        Spacer(1, 0.15 * inch),
        Paragraph(f"Run: {manifest.run_id}  |  Manifest: {manifest.manifest_id}", _STYLES["BodyText"]),
        Paragraph(f"Column coverage: {verification_result.manifest_coverage_percent}%", _STYLES["BodyText"]),
        Spacer(1, 0.2 * inch),
        Paragraph("Per-column provenance", _STYLES["Heading2"]),
        Spacer(1, 0.1 * inch),
    ]
    provenance_headers = [
        "Decision ID", "Dataset / Column", "Method", "Version", "Rule / Policy",
        "Reviewer Result", "Final Verification", "Evidence References",
    ]
    provenance_rows = [
        (r.decision_id, f"{r.dataset_file} / {r.column}", r.method_used, r.method_version,
         r.applicable_rule, r.reviewer_result, r.final_verification, r.evidence_references)
        for r in rows
    ]
    provenance_col_widths = [w * inch for w in (1.0, 0.85, 0.65, 0.45, 0.85, 0.85, 0.75, 0.85)]
    story.extend(_table(
        provenance_headers, provenance_rows, "No columns were processed in this run.", provenance_col_widths,
    ))

    story.append(Spacer(1, 0.25 * inch))
    story.append(Paragraph("Regulatory and methodological evidence", _STYLES["Heading2"]))
    story.append(Spacer(1, 0.1 * inch))
    evidence_headers = ["Evidence ID", "Jurisdiction", "Authority", "Title", "Publisher", "Status"]
    evidence_rows = [
        (ev.evidence_id, ev.jurisdiction, ev.authority, ev.title, ev.publisher, ev.verification_status)
        for ev in evidence_records
    ]
    evidence_col_widths = [w * inch for w in (1.1, 0.8, 0.9, 1.6, 1.0, 0.8)]
    story.extend(_table(
        evidence_headers, evidence_rows, "No evidence sources were recorded for this run.", evidence_col_widths,
    ))

    story.append(Spacer(1, 0.25 * inch))
    story.append(Paragraph("Run reproducibility metadata", _STYLES["Heading2"]))
    story.append(Spacer(1, 0.1 * inch))
    repro_lines = [
        f"Repository commit: {run_manifest.repository_commit or '-'}",
        f"Application version: {run_manifest.application_version or '-'}",
        f"Workflow version: {run_manifest.workflow_version or '-'}",
        f"RunPrivacyPolicy version: {run_manifest.run_privacy_policy_version or '-'}",
        f"Trace root hash: {run_manifest.trace_root_hash or '-'}",
    ]
    story.extend(Paragraph(line, _STYLES["BodyText"]) for line in repro_lines)

    doc = _build_doc(path)
    doc.build(story)

    text_lines = ["Technical Appendix", f"Run: {manifest.run_id}", f"Manifest: {manifest.manifest_id}"]
    for r in rows:
        text_lines.append(
            f"{r.decision_id} | {r.dataset_file}/{r.column} | method={r.method_used} v{r.method_version} "
            f"| rule={r.applicable_rule} | evidence={r.evidence_references}"
        )
    for ev in evidence_records:
        text_lines.append(f"evidence {ev.evidence_id}: {ev.title} ({ev.authority}, {ev.jurisdiction})")
    text_lines.extend(repro_lines)
    return path, "\n".join(text_lines)


def build_human_review_summary_pdf(
    *,
    run_id: str,
    human_review_events: Sequence[HumanReviewEvent],
    human_decisions: Sequence[HumanDecision] = (),
    path: Path,
) -> tuple[Path, str]:
    """docs #58's ``Human_Review_Summary.pdf``, built only when human
    review occurred for this run. docs #47: persist only safe decision
    metadata -- every field summarized here already comes from
    ``ResolutionEntry``/``HumanDecision``, never a sandboxed source
    artifact or raw sensitive content."""
    story: list = [
        Paragraph("Human Review Summary", _STYLES["Title"]),
        Spacer(1, 0.15 * inch),
        Paragraph(f"Run: {run_id}", _STYLES["BodyText"]),
        Spacer(1, 0.2 * inch),
    ]

    decision_headers = ["Decision ID", "Action", "Principal", "Role", "Decided At", "Version"]
    decision_rows = [
        (d.decision_id, d.action, d.principal, d.role or "-", d.decided_at, str(d.version))
        for d in human_decisions
    ]
    if decision_rows:
        story.append(Paragraph("Authoritative decisions", _STYLES["Heading2"]))
        story.append(Spacer(1, 0.1 * inch))
        decision_col_widths = [w * inch for w in (1.0, 1.0, 0.9, 0.6, 1.5, 0.5)]
        story.extend(_table(decision_headers, decision_rows, "", decision_col_widths))
        story.append(Spacer(1, 0.2 * inch))

    story.append(Paragraph("Per-column resolutions", _STYLES["Heading2"]))
    story.append(Spacer(1, 0.1 * inch))
    resolution_rows = [
        (ev.principal, ev.submitted_at, r.file_id, r.column, r.mode, r.comment or "-")
        for ev in human_review_events for r in ev.resolutions
    ]
    resolution_headers = ["Reviewer", "Submitted", "Dataset", "Column", "Decision", "Comment"]
    resolution_col_widths = [w * inch for w in (0.9, 1.6, 0.9, 0.8, 0.8, 1.6)]
    story.extend(_table(
        resolution_headers, resolution_rows, "No per-column resolutions were recorded for this run.",
        resolution_col_widths,
    ))

    doc = _build_doc(path)
    doc.build(story)

    text_lines = ["Human Review Summary", f"Run: {run_id}"]
    for d in human_decisions:
        text_lines.append(f"{d.decision_id}: {d.action} by {d.principal} ({d.role or 'n/a'}) at {d.decided_at}")
    for principal, submitted_at, file_id, column, mode, comment in resolution_rows:
        text_lines.append(f"{principal} @ {submitted_at}: {file_id}/{column} -> {mode} ({comment})")
    return path, "\n".join(text_lines)
