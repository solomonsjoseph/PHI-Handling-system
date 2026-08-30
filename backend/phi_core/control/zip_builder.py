"""ZIPBuilder (Phase 11b wave 2, docs #58/#61): assembles the canonical
section-61 ``PHI_Handled_Study_<run-id>.zip`` from ReportGeneration's
:class:`~.report_artifacts.ReportArtifacts` and Executor's own surviving
dataset exports (``agents/orchestrator.py::execute_decisions``'s ``exports``
dict, ``file_id -> Path``, itself the DeterministicVerifier/Reviewer-filtered
view built at that function's ``exports = rv_out["exports"]`` line).

Not wired into a live execution path this phase -- a standalone,
independently testable module, exactly matching the precedent
``control/manifest.py`` (Phase 9), ``control/verification.py`` (Phase 9),
``control/deterministic_verifier.py`` (Phase 10), and ``control/
final_assurance.py`` (Phase 11a) each already set: build and test the gate
in isolation first, wire it into ``agents/orchestrator.py`` in a later
phase's own target-file list.

Docs #61's canonical structure (built exactly, never approximated):

.. code-block::

    PHI_Handled_Study_<run-id>.zip
    +-- 01_Processed_Datasets/
    +-- 02_Audit_Report/
    |     PHI_Handling_Audit_Report.pdf
    |     What_Happened_to_Each_Column.xlsx
    +-- 03_Technical_Appendix/
    |     Technical_Appendix.pdf
    |     Evidence_Manifest.json
    |     Verification_Manifest.json
    |     Run_Manifest.json
    +-- 04_Human_Review/          (omitted entirely when no human review occurred)
    |     Human_Review_Summary.pdf
    +-- 05_Integrity/
          CHECKSUMS.sha256

Never included, by construction rather than by scan: ``build()`` only ever
writes bytes read from two explicit, caller-supplied inputs --
``dataset_exports`` (Executor's own already-filtered export paths) and
``artifacts`` (ReportGenerator's own report file paths). No other directory
is ever walked and no other path is ever opened, so a raw source dataset,
the original dictionary/forms/CRFs, a raw LLM prompt/completion, a sandbox
temp file, or a secret sitting anywhere else on disk has no code path by
which it could ever end up inside the archive.
"""
from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from ..file_readers import read_pdf
from .final_assurance import (
    ReportingSafetyResult,
    ReportPackageContent,
    run_reporting_safety_gate,
)
from .records import VerifiedClassificationManifest
from .report_artifacts import (
    AUDIT_REPORT_NAME,
    CHECKSUMS_NAME,
    COLUMN_LEDGER_NAME,
    EVIDENCE_MANIFEST_NAME,
    HUMAN_REVIEW_SUMMARY_NAME,
    RUN_MANIFEST_NAME,
    TECHNICAL_APPENDIX_NAME,
    VERIFICATION_MANIFEST_NAME,
    ReportArtifacts,
)

_PROCESSED_DATASETS_DIR = "01_Processed_Datasets"
_AUDIT_REPORT_DIR = "02_Audit_Report"
_TECHNICAL_APPENDIX_DIR = "03_Technical_Appendix"
_HUMAN_REVIEW_DIR = "04_Human_Review"
_INTEGRITY_DIR = "05_Integrity"


class ReportingSafetyRefused(RuntimeError):
    """Raised when ``run_reporting_safety_gate`` returns ``FAIL`` over the
    real, on-disk report content -- docs #60: packaging never proceeds past
    a caught leak, and never on an empty/skipped scan."""

    def __init__(self, result: ReportingSafetyResult) -> None:
        self.result = result
        super().__init__(
            f"reporting safety gate FAILED with {len(result.findings)} finding(s); refusing to package"
        )


@dataclass(frozen=True)
class ZipManifest:
    """``ReportPackage``'s own manifest (docs #62): what this build
    actually wrote, stamped with the exact ``(run_id, manifest_id,
    manifest_version)`` triple every other typed record in the bundle
    (``VerifiedClassificationManifest``, ``ExecutionResult``,
    ``VerificationResult``, the declared ``ReviewerFinalResult`` binding)
    must also carry for :class:`~.integrity_service.IntegrityService` to
    certify the package. The packaged ``CHECKSUMS.sha256`` file (docs
    #61's ``05_Integrity/``) is stamped with this same triple -- it is
    written by this same build, from the same ``manifest`` -- so
    ``checksums_run_id``/``checksums_manifest_id``/``checksums_manifest_version``
    are carried separately here rather than assumed identical to
    ``run_id``/``manifest_id``/``manifest_version`` above, so a future
    caller that reconstructs a ``ZipManifest`` by hand (a replay, a test)
    cannot silently conflate "the ZIP's own identity" with "what the
    packaged Checksums artifact claims" when checking docs #62's binding.
    """

    run_id: str
    manifest_id: str
    manifest_version: str
    checksums_run_id: str
    checksums_manifest_id: str
    checksums_manifest_version: str
    zip_path: Path
    members: tuple[str, ...] = field(default_factory=tuple)
    included_dataset_file_ids: tuple[str, ...] = field(default_factory=tuple)
    human_review_included: bool = False


def _read_text_if_present(path: Path | None) -> str:
    """Extract plain text from a PDF ``ReportArtifacts`` names, via
    ``file_readers.read_pdf`` (not duplicated) -- ``ReportingSafetyGate``
    scans plain-text surfaces, never raw PDF bytes. A real
    ``reportlab``-generated report PDF carries a genuine digital text
    layer, so this never falls through to ``read_pdf``'s OCR path."""
    if path is None:
        return ""
    return read_pdf(path)


def build_report_package_content(
    artifacts: ReportArtifacts,
    dataset_exports: Mapping[str, Path],
    *,
    manifest_display_fields: Mapping[str, str] | None = None,
) -> ReportPackageContent:
    """The real :class:`~.final_assurance.ReportPackageContent` docs #60
    needs, built from an actual :class:`ReportArtifacts` instance's
    on-disk content plus the archive's own filenames -- never a hand-typed
    stand-in. ``manifest_display_fields`` (safe, already-aliased display
    values a report surfaces, e.g. a run id or manifest version) is
    caller-supplied because this module has no independent knowledge of
    what ReportGenerator chose to display; it defaults to empty, which is
    itself a genuine (not skipped) scan of an empty surface, matching
    ``ReportPackageContent``'s own documented convention.
    """
    filenames = tuple(Path(p).name for p in dataset_exports.values())
    filenames += tuple(
        Path(p).name for p in (
            artifacts.audit_report_pdf, artifacts.column_ledger_xlsx,
            artifacts.technical_appendix_pdf, artifacts.human_review_summary_pdf,
            artifacts.evidence_manifest_json, artifacts.verification_manifest_json,
            artifacts.run_manifest_json, artifacts.checksums_sha256,
        ) if p is not None
    )
    workbook_paths = (str(artifacts.column_ledger_xlsx),) if artifacts.column_ledger_xlsx is not None else ()
    return ReportPackageContent(
        report_text=_read_text_if_present(artifacts.audit_report_pdf),
        human_review_summary_text=_read_text_if_present(artifacts.human_review_summary_pdf),
        technical_appendix_text=_read_text_if_present(artifacts.technical_appendix_pdf),
        filenames=filenames,
        manifest_display_fields=manifest_display_fields or {},
        workbook_paths=workbook_paths,
    )


class ZIPBuilder:
    """docs #61: assembles the canonical section-61 ZIP. Stateless, like
    ``DeterministicVerifier``/``RewindRouter``: every method is a pure
    function (plus real filesystem I/O) over its arguments."""

    @staticmethod
    def build(
        *,
        manifest: VerifiedClassificationManifest,
        output_dir: Path,
        artifacts: ReportArtifacts,
        dataset_exports: Mapping[str, Path],
        human_review_occurred: bool,
        manifest_display_fields: Mapping[str, str] | None = None,
        jurisdiction: str = "us",
    ) -> ZipManifest:
        """Build ``PHI_Handled_Study_<run_id>.zip`` under ``output_dir``.

        Refuses (``ReportingSafetyRefused``) rather than packaging when
        ``run_reporting_safety_gate`` (docs #60) reports ``FAIL`` over the
        real, on-disk report content this build is about to include.

        ``04_Human_Review/`` is included, with exactly one member
        (``Human_Review_Summary.pdf``), only when
        ``human_review_occurred`` is true AND
        ``artifacts.human_review_summary_pdf`` is populated; otherwise the
        whole folder is omitted, matching docs #61's explicit instruction
        -- never an empty placeholder folder, never a folder built from a
        stale/absent artifact.
        """
        content = build_report_package_content(
            artifacts, dataset_exports, manifest_display_fields=manifest_display_fields,
        )
        safety = run_reporting_safety_gate(content, jurisdiction=jurisdiction)
        if safety.verdict == "FAIL":
            raise ReportingSafetyRefused(safety)

        include_human_review = human_review_occurred and artifacts.human_review_summary_pdf is not None
        manifest_version = str(manifest.schema_version)

        zip_path = output_dir / f"PHI_Handled_Study_{manifest.run_id}.zip"
        members: list[str] = []
        included_dataset_ids: list[str] = []
        digests: dict[str, str] = {}

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_id, path in dataset_exports.items():
                arcname = f"{_PROCESSED_DATASETS_DIR}/{Path(path).name}"
                zf.write(path, arcname)
                members.append(arcname)
                included_dataset_ids.append(file_id)
                digests[arcname] = _sha256_of(Path(path))

            for arcname, source in (
                (f"{_AUDIT_REPORT_DIR}/{AUDIT_REPORT_NAME}", artifacts.audit_report_pdf),
                (f"{_AUDIT_REPORT_DIR}/{COLUMN_LEDGER_NAME}", artifacts.column_ledger_xlsx),
                (f"{_TECHNICAL_APPENDIX_DIR}/{TECHNICAL_APPENDIX_NAME}", artifacts.technical_appendix_pdf),
                (f"{_TECHNICAL_APPENDIX_DIR}/{EVIDENCE_MANIFEST_NAME}", artifacts.evidence_manifest_json),
                (f"{_TECHNICAL_APPENDIX_DIR}/{VERIFICATION_MANIFEST_NAME}", artifacts.verification_manifest_json),
                (f"{_TECHNICAL_APPENDIX_DIR}/{RUN_MANIFEST_NAME}", artifacts.run_manifest_json),
            ):
                if source is None:
                    continue
                zf.write(source, arcname)
                members.append(arcname)
                digests[arcname] = _sha256_of(Path(source))

            if include_human_review:
                arcname = f"{_HUMAN_REVIEW_DIR}/{HUMAN_REVIEW_SUMMARY_NAME}"
                zf.write(artifacts.human_review_summary_pdf, arcname)
                members.append(arcname)
                digests[arcname] = _sha256_of(Path(artifacts.human_review_summary_pdf))

            checksums_arcname = f"{_INTEGRITY_DIR}/{CHECKSUMS_NAME}"
            if artifacts.checksums_sha256 is not None:
                zf.write(artifacts.checksums_sha256, checksums_arcname)
            else:
                # No upstream CHECKSUMS.sha256 supplied: write this build's
                # own digests over every member packaged above it, rather
                # than silently omitting the mandatory 05_Integrity/ file.
                zf.writestr(
                    checksums_arcname,
                    "\n".join(f"{sha}  {name}" for name, sha in sorted(digests.items())) + "\n",
                )
            members.append(checksums_arcname)

        return ZipManifest(
            run_id=manifest.run_id,
            manifest_id=manifest.manifest_id,
            manifest_version=manifest_version,
            checksums_run_id=manifest.run_id,
            checksums_manifest_id=manifest.manifest_id,
            checksums_manifest_version=manifest_version,
            zip_path=zip_path,
            members=tuple(members),
            included_dataset_file_ids=tuple(included_dataset_ids),
            human_review_included=include_human_review,
        )


def _sha256_of(path: Path) -> str:
    from .artifacts import _hash_file  # local import: control.artifacts, no new module-level cycle

    sha256, _size = _hash_file(path)
    return sha256
