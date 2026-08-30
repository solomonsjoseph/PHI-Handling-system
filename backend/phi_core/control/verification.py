"""DeterministicVerifier's post-execution verdict (docs #54).

docs/MASTER_ARCHITECTURE_V2.md's agent-mapping table is explicit about
Operator's fate: "Operator -> migrate useful deterministic verification
into DeterministicVerifier then remove." Phase 9 does not remove
Operator -- Phase 10 does, once DeterministicVerifier passes end to end
-- but this module is the additive first step docs #54 calls for now:
Operator's existing ``{"verdicts", "failed_file_ids", "status"}`` result
is converted into a governed :class:`~.records.VerificationResult` and
persisted alongside the idempotency spine's
:class:`~.records.ExecutionTask`/:class:`~.records.ExecutionResult`
records, so post-execution verification becomes queryable, versioned
control-plane data instead of an ephemeral dict only Reviewer's
coverage-audit pass ever sees. Operator's class, its own module, and
every existing consumer of its raw return value are unchanged.
"""
from __future__ import annotations

from typing import Any

from .records import VerificationResult
from .store import ControlStore


def build_verification_result(
    *,
    run_id: str,
    task_id: str,
    attempt_id: str,
    manifest_id: str,
    manifest_version: str,
    input_artifact_version: int,
    output_artifact_version: int,
    operator_result: dict[str, Any],
) -> VerificationResult:
    """Convert ``Operator.run``'s raw result into a
    :class:`~.records.VerificationResult` (docs #54).

    ``manifest_coverage_percent`` is the share of Operator's own
    per-decision verdicts that came back ``"pass"``; zero decisions is
    treated as full (100%) coverage -- the same vacuous-coverage
    convention ``control/gates.py``'s ``assert_exact_coverage`` already
    uses for an empty input, rather than inventing a second one here.
    ``failed_checks`` names every distinct ``file_id:column:method``
    Operator flagged ``"fail"``, plus every ``file_id`` in
    ``failed_file_ids`` (a file Operator could not read, or that never
    reached Executor's ``exports`` at all) as ``file_id:unreadable``.
    ``passed`` requires both Operator's own aggregate ``status ==
    "clean"`` and an empty ``failed_checks`` list -- kept as two
    conditions rather than one so a future caller that constructs
    ``operator_result`` by hand (a test, a replay) cannot accidentally
    report ``passed=True`` by fabricating ``status`` alone while still
    listing real failures.
    """
    verdicts = operator_result.get("verdicts") or []
    failed_file_ids = operator_result.get("failed_file_ids") or []
    total = len(verdicts)
    passing = sum(1 for v in verdicts if v.get("verdict") == "pass")
    coverage_percent = 100 if total == 0 else round(100 * passing / total)
    failed_checks = [
        f"{v.get('file_id', '')}:{v.get('column', '')}:{v.get('method', '')}"
        for v in verdicts if v.get("verdict") == "fail"
    ] + [f"{fid}:unreadable" for fid in failed_file_ids]
    passed = operator_result.get("status") == "clean" and not failed_checks
    return VerificationResult(
        task_id=task_id, run_id=run_id, attempt_id=attempt_id,
        manifest_id=manifest_id, manifest_version=manifest_version,
        input_artifact_version=input_artifact_version, output_artifact_version=output_artifact_version,
        manifest_coverage_percent=coverage_percent, failed_checks=failed_checks,
        passed=passed, detail=f"operator_status={operator_result.get('status', '')}",
    )


async def record_verification_result(store: ControlStore, result: VerificationResult) -> None:
    """Persist ``result`` into the verification-results collection -- the
    same fixed insert-only convention this phase's ``ExecutionTask``/
    ``ExecutionResult`` writes (``agents/reasoning.py::Executor.run``)
    already use."""
    await store.insert("verification_results", result)
