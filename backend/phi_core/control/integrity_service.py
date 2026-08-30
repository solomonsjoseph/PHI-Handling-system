"""IntegrityService (Phase 11b wave 2, docs #62): exact-output binding.

Section 62, verbatim intent: ``VerifiedClassificationManifest``,
``ExecutionResult``, ``VerificationResult``, ``ReviewerFinalResult``, the
``ReportPackage`` (the ZIP's own manifest, ``control/zip_builder.py::
ZipManifest``), and the packaged ``Checksums`` must all bind to the exact
same versions -- "a report must never describe a different execution than
the files packaged with it." This module makes that binding structural: it
refuses (``ExactOutputBindingViolation``) rather than silently certifying a
package whenever any of the six pieces disagree on ``(run_id, manifest_id,
manifest_version)``.

``ReviewerFinalResult`` (``control/final_assurance.py``) carries no
run/manifest identity field of its own -- Phase 11a's frozen contract,
pinned by its own tests, is not reopened here. Its binding is therefore the
``(run_id, manifest_id, manifest_version)`` the caller *declares* it was
computed from: exactly the arguments a real caller already has in hand,
since ``Reviewer.finalize()`` is only ever invoked immediately after the
same ``manifest``/``execution_result``/``verification_result`` triple is
already in scope (``agents/orchestrator.py::execute_decisions``). A caller
that declares a binding inconsistent with the manifest's own real identity
is caught here exactly like any other mismatch -- this is what makes the
check meaningful rather than vacuous: it does not merely trust the caller's
assertion, it cross-checks it against the four pieces that do carry their
own real identity fields.
"""
from __future__ import annotations

from dataclasses import dataclass

from .records import ExecutionResult, VerificationResult, VerifiedClassificationManifest
from .zip_builder import ZipManifest


@dataclass(frozen=True)
class BindingKey:
    run_id: str
    manifest_id: str
    manifest_version: str


class ExactOutputBindingViolation(RuntimeError):
    """Raised when the pieces of a report package do not all reference the
    same ``(run_id, manifest_id, manifest_version)`` triple -- docs #62's
    binding is refused, never silently packaged."""

    def __init__(self, mismatches: list[str]) -> None:
        self.mismatches = mismatches
        super().__init__("exact-output binding violated: " + "; ".join(mismatches))


class IntegrityService:
    """Stateless, like ``DeterministicVerifier``/``RewindRouter``: every
    method is a pure function over its arguments -- no store, no I/O."""

    @staticmethod
    def verify_exact_output_binding(
        *,
        manifest: VerifiedClassificationManifest,
        execution_result: ExecutionResult,
        verification_result: VerificationResult,
        reviewer_final_run_id: str,
        reviewer_final_manifest_id: str,
        reviewer_final_manifest_version: str,
        zip_manifest: ZipManifest,
    ) -> BindingKey:
        """Return the shared :class:`BindingKey` when every piece agrees;
        raise :class:`ExactOutputBindingViolation` (listing every
        disagreeing piece, not just the first) otherwise.

        The authoritative key is ``manifest``'s own real identity
        (``run_id``, ``manifest_id``, ``schema_version``) -- the frozen
        ``VerifiedClassificationManifest`` docs #49 already treats as the
        one immutable source of truth for "which decision set was this."
        Every other piece is checked against it, never against each other
        directly, so a caller cannot game the check by making two wrong
        pieces agree with each other while both disagree with the real
        manifest.
        """
        expected = BindingKey(
            run_id=manifest.run_id, manifest_id=manifest.manifest_id,
            manifest_version=str(manifest.schema_version),
        )
        mismatches: list[str] = []

        exec_key = BindingKey(
            run_id=execution_result.run_id, manifest_id=execution_result.manifest_id,
            manifest_version=execution_result.manifest_version,
        )
        if exec_key != expected:
            mismatches.append(f"ExecutionResult binding {exec_key} != manifest binding {expected}")

        ver_key = BindingKey(
            run_id=verification_result.run_id, manifest_id=verification_result.manifest_id,
            manifest_version=verification_result.manifest_version,
        )
        if ver_key != expected:
            mismatches.append(f"VerificationResult binding {ver_key} != manifest binding {expected}")

        reviewer_final_key = BindingKey(
            run_id=reviewer_final_run_id, manifest_id=reviewer_final_manifest_id,
            manifest_version=reviewer_final_manifest_version,
        )
        if reviewer_final_key != expected:
            mismatches.append(
                f"ReviewerFinalResult declared binding {reviewer_final_key} != manifest binding {expected}"
            )

        report_package_key = BindingKey(
            run_id=zip_manifest.run_id, manifest_id=zip_manifest.manifest_id,
            manifest_version=zip_manifest.manifest_version,
        )
        if report_package_key != expected:
            mismatches.append(f"ReportPackage binding {report_package_key} != manifest binding {expected}")

        checksums_key = BindingKey(
            run_id=zip_manifest.checksums_run_id, manifest_id=zip_manifest.checksums_manifest_id,
            manifest_version=zip_manifest.checksums_manifest_version,
        )
        if checksums_key != expected:
            mismatches.append(f"Checksums binding {checksums_key} != manifest binding {expected}")

        if mismatches:
            raise ExactOutputBindingViolation(mismatches)
        return expected
