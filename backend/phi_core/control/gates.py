"""The canonical decision gate (D11).

``run_decision_gates`` is the one path every decision-mutation site is meant
to use: Judge's initial proposal, a Sentinel-driven retry, a human-review
resolution, migration replay, and administrative recovery. It composes the
existing deterministic gate functions in ``phi_core.agents.reasoning``
*unchanged* -- this module owns their fixed order, the audit trail (one
``GateResult`` per gate), the aggregated override/rejection/demotion
records those functions already produce, and the two things
``reasoning.py`` never had: an exact per-column coverage proof
(``assert_exact_coverage``) and a durable, monotonic ``decision_version``
counter every downstream write can key off.

The temporary adapter module used during the Phase 3 migration was deleted
once both live callers moved directly to this typed interface. New callers
must use this module directly.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from phi_core.agents.reasoning import (
    ACTION_TYPES,
    annotate_pending_review,
    apply_age_dob_rule,
    apply_blocking_floor,
    apply_confidence_floor,
    apply_sentinel_escalations,
    apply_sentinel_hard_rules,
    apply_site_cardinality_rule,
    validate_decisions,
    verify_keep_decisions,
)

from .context import AgentContext
from .records import GateResult
from .store import ControlStore

GATE_SEQUENCE_VERSION = "gates/1"

# The fixed order D11 mandates, reusing reasoning.py's functions verbatim.
# ``apply_sentinel_escalations`` only runs when a ``sentinel_report`` is
# supplied; the two remaining steps (unreadable-schema synthesis and the
# final coverage proof) are this module's own contribution, not present in
# reasoning.py, and are recorded as their own gates for the same audit
# trail.
GATE_NAMES: tuple[str, ...] = (
    "validate_decisions",
    "apply_sentinel_hard_rules",
    "apply_age_dob_rule",
    "apply_site_cardinality_rule",
    "apply_sentinel_escalations",
    "apply_confidence_floor",
    "apply_blocking_floor",
    "verify_keep_decisions",
    "annotate_pending_review",
    "synthesize_unreadable_schema",
    "assert_exact_coverage",
)


def _canonical(value: Any) -> Any:
    """Recursively normalize ``value`` into a JSON-stable shape.

    Dict keys that are not already strings (``schema_stats``'s
    ``(file_id, column)`` tuple keys) are rendered as a sorted, canonical
    JSON string so the whole structure can be hashed deterministically.
    """
    if isinstance(value, Mapping):
        items = []
        for key, child in value.items():
            key_text = key if isinstance(key, str) else json.dumps(
                _canonical(key), sort_keys=True, ensure_ascii=False, default=str
            )
            items.append((key_text, _canonical(child)))
        return dict(sorted(items))
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_canonical(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _digest(*parts: Any) -> str:
    payload = json.dumps(_canonical(list(parts)), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_unreadable(file_entry: Mapping[str, Any]) -> bool:
    """A file is schema-unreadable when no real column list is known for it."""
    return file_entry.get("columns") is None or bool(file_entry.get("unreadable_reason"))


def _synthesize_unreadable_schema(
    decisions: list[dict[str, Any]],
    files: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fail closed for every schema Schema/Instrument could not read.

    An unreadable file never had any column for Judge to decide on, so
    nothing upstream of this gate ever produces a decision for it. Adding
    one explicit ``human_review`` record here -- rather than leaving the
    file silently absent from the decision set -- is what lets
    ``assert_exact_coverage`` *prove* fail-closed coverage instead of
    merely hoping for it. Idempotent: re-running this on an already
    healed file is a no-op.
    """
    out = list(decisions)
    added: list[dict[str, Any]] = []
    for file_entry in files:
        if not _is_unreadable(file_entry):
            continue
        file_id = file_entry.get("file_id")
        already = any(
            d.get("file_id") == file_id and not d.get("column")
            and str(d.get("reason", "")).startswith("unreadable_schema")
            for d in out
        )
        if already:
            continue
        out.append(
            {
                "file_id": file_id,
                "column": "",
                "action": "human_review",
                "subject": "study",
                "phi_category": None,
                "confidence": 0.0,
                "reason": f"unreadable_schema: {file_entry.get('unreadable_reason') or 'schema could not be read'}",
                "suggested_action": None,
                "suggested_confidence": None,
                "suggested_reason": None,
            }
        )
        added.append({"file_id": file_id, "rule": "unreadable_schema"})
    return out, added


def assert_exact_coverage(
    decisions: list[dict[str, Any]],
    files: list[dict[str, Any]],
) -> tuple[str, str]:
    """Prove exactly-one-decision-per-real-column over the hydrated ``files``.

    Returns ``(status, detail)``; ``status`` is ``"pass"`` or ``"fail"``.
    This function only proves the invariant -- it never mutates
    ``decisions``. Healing an absent unreadable-schema record is
    ``run_decision_gates``'s job (``_synthesize_unreadable_schema``, run
    immediately before this check), so a bug that skips synthesis is
    caught here as a failure (``missing_unreadable_schema_record``) rather
    than silently patched over by the prover itself.

    ``files`` entries: ``file_id`` (required), ``columns`` (the real
    column list, or ``None`` when the schema could not be read),
    ``unreadable_reason`` (optional).
    """
    readable = [f for f in files if not _is_unreadable(f)]
    unreadable_ids = {f.get("file_id") for f in files if _is_unreadable(f)}
    real_file_ids = {f.get("file_id") for f in files}
    real_pairs = {(f.get("file_id"), column) for f in readable for column in (f.get("columns") or [])}

    column_decisions = [d for d in decisions if d.get("column")]
    pairs = [(d.get("file_id"), d.get("column")) for d in column_decisions]
    counts = Counter(pairs)

    problems: list[str] = []

    duplicates = sorted({p for p, n in counts.items() if n > 1})
    if duplicates:
        problems.append(f"duplicate_decision:{duplicates}")

    missing = sorted(real_pairs - set(pairs))
    if missing:
        problems.append(f"missing_decision:{missing}")

    invented = sorted({p for p in set(pairs) if p not in real_pairs and p[0] not in unreadable_ids})
    if invented:
        problems.append(f"invented_decision:{invented}")

    unknown_files = sorted({d.get("file_id") for d in decisions} - real_file_ids)
    if unknown_files:
        problems.append(f"unknown_file:{unknown_files}")

    invalid_actions = sorted(
        (d.get("file_id"), d.get("column"), d.get("action"))
        for d in decisions
        if d.get("action") not in ACTION_TYPES
    )
    if invalid_actions:
        problems.append(f"invalid_action:{invalid_actions}")

    missing_unreadable_record = sorted(
        file_id
        for file_id in unreadable_ids
        if not any(
            d.get("file_id") == file_id and not d.get("column")
            and str(d.get("reason", "")).startswith("unreadable_schema")
            for d in decisions
        )
    )
    if missing_unreadable_record:
        problems.append(f"missing_unreadable_schema_record:{missing_unreadable_record}")

    if problems:
        return "fail", "; ".join(problems)
    detail = f"exact coverage over {len(real_pairs)} column(s) across {len(files)} file(s)"
    if unreadable_ids:
        detail += f"; unreadable_schema recorded for {sorted(unreadable_ids)}"
    return "pass", detail


async def _next_decision_version(store: ControlStore | None, run_id: str) -> int:
    """CAS-increment ``workflow_runs.decision_version`` for ``run_id``.

    ``AgentContext`` (Phase 2, already committed) has no public store
    accessor, so the store is threaded through explicitly here rather than
    reached for through a private attribute of ``ctx.gateway``. When
    ``store`` is omitted the counter is not persisted and every
    ``GateResult`` in the call is stamped with ``decision_version=0``; a
    durable caller always supplies one.
    """
    if store is None:
        return 0
    for _ in range(8):
        current = await store.get_one("workflow_runs", {"run_id": run_id})
        if current is None:
            raise RuntimeError(
                f"no workflow_runs record for run_id={run_id!r}; open the run before gating decisions"
            )
        expected = current.get("decision_version", 0)
        version = int(expected) + 1
        updated = dict(current)
        updated["decision_version"] = version
        if await store.compare_and_set(
            "workflow_runs", {"run_id": run_id}, {"decision_version": expected}, updated
        ):
            return version
    raise RuntimeError(f"could not CAS-increment decision_version for run_id={run_id!r}")


@dataclass(frozen=True)
class GateOutcome:
    """Result of one ``run_decision_gates`` call."""

    ok: bool
    decisions: list[dict[str, Any]]
    gate_results: list[GateResult]
    overrides: list[dict[str, Any]]
    rejections: list[dict[str, Any]]
    demotions: list[dict[str, Any]]
    decision_version: int


class DecisionGateFailure(RuntimeError):
    """Raised by a caller when ``GateOutcome.ok`` is ``False``.

    ``run_decision_gates`` itself never raises on a coverage failure --
    it returns the full audit trail either way, so a caller can log or
    persist ``GateOutcome.gate_results`` before deciding how to fail.
    Every call site that can reach ``execute`` MUST check ``outcome.ok``
    and raise this (or an equivalent fail-closed refusal) rather than let
    an incomplete or invented decision set pass through silently.
    """

    def __init__(self, outcome: GateOutcome) -> None:
        self.outcome = outcome
        coverage = next((gr for gr in outcome.gate_results if gr.gate == "assert_exact_coverage"), None)
        detail = coverage.detail if coverage is not None else "unknown coverage failure"
        super().__init__(f"decision gate sequence failed exact-coverage proof: {detail}")


async def run_decision_gates(
    *,
    decisions: list[dict[str, Any]],
    files: list[dict[str, Any]],
    statute: Any = None,
    instrument: Any = None,
    schema_stats: dict[tuple[str, str], dict[str, int]] | None = None,
    jurisdiction: str = "us",
    blocking_attempts: dict[tuple[str, str], int] | None = None,
    sentinel_report: dict[str, Any] | None = None,
    stage: str,
    ctx: AgentContext,
    store: ControlStore | None = None,
) -> GateOutcome:
    """Run the fixed D11 gate sequence once and return its full audit trail.

    ``statute`` and ``instrument`` are not consumed by any of the reused
    ``reasoning.py`` functions (none of them accept regulatory-research or
    instrument context); they are folded into every ``GateResult.inputs_digest``
    instead, so the audit trail binds each gate's result to the *complete*
    context that produced the candidate decisions, not only to the decision
    list itself.
    """
    schema_stats = schema_stats or {}
    blocking_attempts = blocking_attempts or {}
    version = await _next_decision_version(store, ctx.run_id)

    context_digest = _digest(
        {
            "statute": statute,
            "instrument": instrument,
            "schema_stats": schema_stats,
            "jurisdiction": jurisdiction,
            "blocking_attempts": blocking_attempts,
            "sentinel_report": sentinel_report,
        }
    )

    gate_results: list[GateResult] = []
    overrides: list[dict[str, Any]] = []

    def record(gate: str, before: list[dict[str, Any]], status: str, detail: str) -> None:
        gate_results.append(
            GateResult(
                run_id=ctx.run_id,
                task_id=ctx.task_id,
                gate=gate,
                gate_version=GATE_SEQUENCE_VERSION,
                status=status,
                subject=stage,
                detail=f"decision_version={version}; {detail}",
                inputs_digest=_digest(gate, before, context_digest),
            )
        )

    current = decisions

    before = current
    current, rejections = validate_decisions(current)
    record("validate_decisions", before, "pass", f"{len(rejections)} model-output rejection(s)")

    before = current
    current, hard_rule_overrides = apply_sentinel_hard_rules(current)
    overrides.extend(hard_rule_overrides)
    record("apply_sentinel_hard_rules", before, "pass", f"{len(hard_rule_overrides)} override(s)")

    before = current
    current, age_dob_overrides = apply_age_dob_rule(current)
    overrides.extend(age_dob_overrides)
    record("apply_age_dob_rule", before, "pass", f"{len(age_dob_overrides)} override(s)")

    before = current
    current, cardinality_overrides = apply_site_cardinality_rule(current, schema_stats)
    overrides.extend(cardinality_overrides)
    record("apply_site_cardinality_rule", before, "pass", f"{len(cardinality_overrides)} override(s)")

    before = current
    if sentinel_report is not None:
        escalations = [
            issue
            for issue in (sentinel_report.get("issues") or [])
            if str(issue.get("severity", "")).lower() == "escalate"
        ]
        current, escalation_overrides = apply_sentinel_escalations(current, escalations)
        overrides.extend(escalation_overrides)
        record("apply_sentinel_escalations", before, "pass", f"{len(escalation_overrides)} override(s)")
    else:
        record("apply_sentinel_escalations", before, "not_applicable", "no sentinel_report supplied")

    before = current
    current, floor_overrides = apply_confidence_floor(current)
    overrides.extend(floor_overrides)
    record("apply_confidence_floor", before, "pass", f"{len(floor_overrides)} override(s)")

    before = current
    current, blocking_overrides = apply_blocking_floor(current, blocking_attempts)
    overrides.extend(blocking_overrides)
    record("apply_blocking_floor", before, "pass", f"{len(blocking_overrides)} override(s)")

    before = current
    dataset_paths = {f["file_id"]: Path(f["stored_path"]) for f in files if f.get("stored_path")}
    current, demotions = verify_keep_decisions(current, dataset_paths, jurisdiction=jurisdiction)
    record("verify_keep_decisions", before, "pass", f"{len(demotions)} demotion(s)")

    before = current
    current = annotate_pending_review(current, None)
    record("annotate_pending_review", before, "pass", "reviewer prompts annotated")

    before = current
    current, added_unreadable = _synthesize_unreadable_schema(current, files)
    record(
        "synthesize_unreadable_schema",
        before,
        "pass",
        f"{len(added_unreadable)} unreadable-schema record(s) synthesized" if added_unreadable else "no unreadable schema",
    )

    before = current
    coverage_status, coverage_detail = assert_exact_coverage(current, files)
    record("assert_exact_coverage", before, coverage_status, coverage_detail)

    return GateOutcome(
        ok=coverage_status == "pass",
        decisions=current,
        gate_results=gate_results,
        overrides=overrides,
        rejections=rejections,
        demotions=demotions,
        decision_version=version,
    )
