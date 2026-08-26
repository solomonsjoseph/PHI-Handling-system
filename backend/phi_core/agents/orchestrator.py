"""Orchestrator: run the full agent pipeline for a session.

Pipeline:
  1. Specialists (Lexicon, Schema, Instrument) + Statute + Praxis ALL in
     parallel (Statute/Praxis don't read file content, so they overlap
     with specialists rather than serialising after them).
  2. Judge <-> Sentinel loop (short-circuits on 0 blocking issues; capped at ITERATION_CAP=2)
  3. Human review gate if Sentinel still has blocking issues
  4. Executor applies decisions
  5. Publish Guard (deterministic residual PHI scan)
  6. Auditor + Scout in parallel (Scout doesn't depend on Auditor)
  7. Ledger (Compare + Aggregate) sub-agent split
  8. Herald (Abstract + Sections) sub-agent split

Cancellation: the orchestrator checks ``is_cancelled(sid)`` between
phases. When True the pipeline exits early with status='cancelled' and
no further LLM calls are made. This is why every await for a phase is
followed by a cancel check.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from motor.motor_asyncio import AsyncIOMotorDatabase

from phi_core.control.activation import ActivationFactory
from phi_core.control.context import AgentContext
from phi_core.control.store import ControlStore

from ..paths import cleanup_session_unpacked
from ..security import scrub_decision, scrub_persisted_text
from .base import ITERATION_CAP, AgentMessage
from .experts import Praxis, Statute
from .llm import LlmConfig
from .manager import Manager
from .operator import Operator
from .outward import Herald, Ledger, Scout
from .reasoning import (
    BLOCKING_ISSUE_FLOOR,
    Auditor,
    Executor,
    Judge,
    Sentinel,
    annotate_pending_review,
    apply_age_dob_rule,
    apply_blocking_floor,
    apply_confidence_floor,
    apply_sentinel_escalations,
    apply_sentinel_hard_rules,
    apply_site_cardinality_rule,
    auditor_escalation_reason,
    materialize_auditor_disagreements,
    plain_human_review_reasons,
    validate_decisions,
)
from .reviewer import Reviewer
from .specialists import Instrument, Lexicon, Schema

PhaseCb = Callable[[str, dict[str, Any]], Awaitable[None]]


class PipelineCancelled(Exception):
    """Raised by the orchestrator when the operator requested cancel."""

class ResultAcceptanceError(Exception):
    """Raised when SuperOrchestrator.accept_result refuses a completed
    child's result (D9/D5: 'child success is not acceptance'). A caller
    must never treat an unaccepted result as authoritative."""


def _blocking_issues(sentinel_out: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the sentinel issues whose severity is 'blocking'.

    Advisory issues are recorded for the audit trail but do NOT trigger
    another Judge iteration. This closes the "Sentinel nitpicks endlessly"
    pathology and is Sir's Q1 requirement -- iterate 'where required',
    not always.
    """
    return [i for i in (sentinel_out.get("issues") or [])
            if str(i.get("severity", "")).lower() == "blocking"]


def _escalation_issues(sentinel_out: dict[str, Any]) -> list[dict[str, Any]]:
    """Issues Sentinel marked 'escalate' -- genuine ambiguity it cannot
    correct itself. Routes straight to human_review, skipping further Judge
    iterations for that column (unlike 'blocking', which sends it back)."""
    return [i for i in (sentinel_out.get("issues") or [])
            if str(i.get("severity", "")).lower() == "escalate"]


async def _check_cancel(db: AsyncIOMotorDatabase, sid: str, on_phase: PhaseCb) -> None:
    doc = await db.sessions.find_one({"id": sid}, {"cancel_requested": 1, "status": 1})
    if doc and doc.get("cancel_requested"):
        await on_phase("cancelled",
                       {"reason": "operator requested cancel via /api/sessions/{sid}/cancel"})
        raise PipelineCancelled()


async def _cancel_and_await(task: "asyncio.Task[Any]") -> None:
    """Cancel ``task`` and wait for the cancellation to actually land.

    ``Task.cancel()`` alone only requests cancellation; the task keeps
    running until it next reaches an await point, and a caller that
    never awaits it again never observes whether that landed cleanly or
    raised something else. Every exit path this helper is used on is
    about to return or re-raise, so this is the last chance to fence
    Scout's still-running background task before the caller moves on.
    """
    if task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        # The cancellation itself, or any exception the task raised on
        # its way out, is expected here and not this function's problem
        # to surface -- the caller has already decided the run is done.
        pass


async def _escalate_to_human_review(
    *, db: AsyncIOMotorDatabase, session_filter: dict[str, Any], reasons: list[str],
    reasons_plain: list[str], close_last_phase: Callable[[], Awaitable[None]],
    phase_timings: dict[str, Any], run_elapsed_s: float,
    approved_decisions: list[dict[str, Any]], sentinel_report: dict[str, Any] | None,
    manager: Manager, store: "ControlStore | None", run_id: str, node: str,
    audit_version: str = "",
) -> dict[str, Any]:
    """The single path by which a run becomes 'awaiting_human_review' (D10).

    Manager keeps ``close_run`` (the deterministic report) but no longer
    owns the workflow-authority write: this function persists the session
    document itself, exactly matching every other decision-mutation call
    site's own pattern, then asks ``SuperOrchestrator.request_human_review``
    to open the durable, typed ``HumanReviewRequest`` and pause the run at
    ``node``. Every current caller of the deleted
    ``Manager.escalate_to_human_review`` calls this instead.

    Tolerates an unknown ``run_id`` (``store`` is ``None``, or no
    ``WorkflowRun`` exists yet for it): the session-document write above is
    the tested, load-bearing contract every caller of this function
    depends on; the durable request is additive until every entry path
    reliably opens a run through ``SuperOrchestrator.start_run`` first.

    ``audit_version`` (D13 step 4/7): non-empty only for an Auditor-verdict
    escalation (``node="human_review_audit"`` with a real audit content
    hash); the reviewer's later ``confirm_auditor_confidence`` submission
    must echo it back, so a confirmation against a since-superseded audit
    verdict is rejected rather than silently accepted.
    """
    # Mirrors what the deleted `Manager.escalate_to_human_review` set
    # internally, so `close_run`'s report still carries the same
    # `"escalation"` field content it always has.
    manager._escalation = {"reasons": reasons}
    await manager._log("manager.escalate", "info", {"reasons": reasons})
    await close_last_phase()
    report = await manager.close_run("awaiting_human_review")
    await db.sessions.update_one(session_filter, {"$set": {
        "status": "awaiting_human_review",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "phase_timings": phase_timings,
        "run_elapsed_s": round(run_elapsed_s, 3),
        "human_review_reasons": reasons,
        "human_review_reasons_plain": reasons_plain,
        "manager_report": report,
    }})
    if store is not None and run_id:
        from phi_core.control.policy import CapabilityPolicy
        from phi_core.control.superorchestrator import SuperOrchestrator
        from phi_core.control.tasks import TaskService
        from phi_core.control.workflow import WorkflowError

        # D13 step 7: the request's own decision_version is the run's
        # current one (D11's CAS-incremented authority), not a hardcoded
        # 0 -- the later delivery-gate match on (principal, file_id,
        # decision_version) has nothing authoritative to compare against
        # otherwise.
        run_doc = await store.get_one("workflow_runs", {"run_id": run_id})
        current_decision_version = int(run_doc.get("decision_version", 0)) if run_doc else 0
        try:
            await SuperOrchestrator(store, TaskService(store, CapabilityPolicy(None))).request_human_review(
                run_id=run_id, node=node, reason_codes=reasons, decision_version=current_decision_version,
                audit_version=audit_version,
            )
        except WorkflowError as exc:
            if not str(exc).startswith("unknown run_id:"):
                raise
    return {"status": "awaiting_human_review", "decisions": approved_decisions,
            "sentinel": sentinel_report, "phase_timings": phase_timings,
            "manager_report": report}


async def execute_decisions(
    *,
    db: AsyncIOMotorDatabase,
    sid: str,
    session: dict[str, Any],
    session_filter: dict[str, Any],
    files: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    statute: dict[str, Any],
    praxis_methods: dict[str, Any],
    dictionary_by_column: dict[str, str],
    make_ctx: Callable[[str], Awaitable[AgentContext]],
    make_child_ctx: Callable[[str, str], Awaitable[AgentContext]],
    complete_and_accept: Callable[[AgentContext, dict[str, Any]], Awaitable[bool]],
    manager: Manager,
    on_phase: PhaseCb,
    close_last_phase: Callable[[], Awaitable[None]],
    phase_timings: dict[str, dict[str, float]],
    run_started: float,
    omit_by_file: dict[str, set[str]] | None = None,
    sentinel_report: dict[str, Any] | None = None,
    extra_completion_fields: dict[str, Any] | None = None,
    extra_result_fields: dict[str, Any] | None = None,
    run_id: str = "",
    store: "ControlStore | None" = None,
) -> dict[str, Any]:
    """The D9 ``execute`` node onward -- Executor, Operator, Reviewer,
    Publish Guard, Auditor/Scout, Ledger, Herald, and the terminal
    completion write.

    Reached identically whether ``gate_decisions`` transitioned here
    directly (``proceed``: a fresh run with no ``human_review`` decisions
    left) or via ``human_review_decisions`` (``resolved``: a resume,
    possibly with some columns still deferred through ``omit_by_file``).
    """
    async def require_accepted(ctx: AgentContext, result: dict[str, Any], agent: str) -> None:
        if not await complete_and_accept(ctx, result):
            raise ResultAcceptanceError(f"{agent} result was not accepted")

    await on_phase("executor", {"decision_count": len(decisions)})
    try:
        executor_ctx = await make_ctx("Executor")
        exec_out = await Executor(executor_ctx).run(
            files=files, decisions=decisions, omit_by_file=omit_by_file)
        await require_accepted(executor_ctx, exec_out, "Executor")
    except Exception as exc:
        # Executor is deterministic and irreversible (writes exports to disk);
        # a crash here must never be papered over by an LLM's advice.
        # consult() fails open by design and is never a safety gate, so the
        # escalation itself is unconditional, fixed code -- see manager.py.
        await manager._log("executor.crashed", "info",
                           {"error_kind": f"exception:{type(exc).__name__}"})
        return await _escalate_to_human_review(
            db=db, session_filter=session_filter, reasons=["executor_crashed"],
            reasons_plain=plain_human_review_reasons(["executor_crashed"]),
            close_last_phase=close_last_phase, phase_timings=phase_timings,
            run_elapsed_s=time.perf_counter() - run_started,
            approved_decisions=decisions, sentinel_report=sentinel_report,
            manager=manager, store=store, run_id=run_id, node="human_review_decisions")

    # Reversal key: the mandatory second deliverable (PHI-handled output
    # plus the key to reverse it), distinct from the optional publishing
    # stack below. Persisted now, separate from `exports`, never bundled.
    if exec_out.get("reversal_key_blob"):
        await db.sessions.update_one(session_filter, {"$set": {
            "reversal_key_blob": exec_out["reversal_key_blob"],
            "reversal_key_created_at": datetime.now(timezone.utc).isoformat(),
        }})

    # Scout has no dependency on Operator, Reviewer, Publish Guard, or
    # Auditor, so it starts here and runs in the background across all of
    scout_ctx = await make_ctx("Scout")
    scout_agent = Scout(scout_ctx)
    scout_task = asyncio.create_task(scout_agent.run())

    # Operator: deterministic self-verification of what Executor wrote,
    # one stage before Publish Guard, mirroring the Judge/Sentinel split one
    # stage later. exec_out["exports"] stays Executor's own factual record
    # of what it wrote and is never mutated here; `exports` is the
    # Operator-then-Reviewer-filtered view every later step in this
    # function uses.
    await on_phase("operator", {"decision_count": len(decisions)})
    try:
        operator_ctx = await make_ctx("Operator")
        op_out = await Operator(operator_ctx).run(
            files=files, decisions=decisions, exports=exec_out["exports"], omit_by_file=omit_by_file)
        await require_accepted(operator_ctx, op_out, "Operator")
    except Exception as exc:
        # Fail open into the existing failed-file machinery: a file Operator
        # cannot verify is dropped from exports exactly like an unreadable
        # file already is, rather than trusting it or inventing a new path.
        await manager._log("operator.crashed", "info",
                           {"error_kind": f"exception:{type(exc).__name__}"})
        op_out = {"failed_file_ids": list(exec_out["exports"].keys()), "verdicts": []}
    # Operator's own `failed_file_ids` only covers a file it could not read
    # or that never made it into `exports` at all (see operator.py). A
    # shape-check or reverse-completeness failure surfaces as a per-column
    # 'fail' verdict on an otherwise-readable file, and must block that
    # file from `exports` exactly the same way -- fold both into one set.
    op_failed_ids = sorted(set(op_out["failed_file_ids"]) |
                           {v["file_id"] for v in op_out["verdicts"] if v.get("verdict") == "fail"})
    exports = {fid: p for fid, p in exec_out["exports"].items()
              if fid not in op_failed_ids}

    # Reviewer: confirms Operator's coverage of every decision against
    # the real written export, catching gaps Operator's own pass cannot see
    # (e.g. an omit_by_file column that leaked into the header). Its own
    # filtered exports become canonical for every remaining step below,
    # starting with Publish Guard.
    try:
        reviewer_ctx = await make_ctx("Reviewer")
        rv_out = await Reviewer(reviewer_ctx).run(
            decisions=decisions,
            operator_result={"failed_file_ids": op_failed_ids, "verdicts": op_out["verdicts"]},
            exports=exports,
            omit_by_file=omit_by_file,
        )
        await require_accepted(reviewer_ctx, rv_out, "Reviewer")
    except Exception as exc:
        # Same fail-open shape as Operator above: an unverifiable file is
        # dropped from exports, never trusted.
        await manager._log("reviewer.crashed", "info",
                           {"error_kind": f"exception:{type(exc).__name__}"})
        rv_out = {"exports": {}, "findings": []}
    reviewer_blocked_ids = sorted(set(exports) - set(rv_out["exports"]))
    exports = rv_out["exports"]
    # A run with any column still deferred via `omit_by_file` (a resume
    # that left some columns pending) can never be "complete" -- a fresh
    # run's `omit_by_file` is always empty, since `gate_decisions` never
    # reaches this function while a `human_review` decision is present,
    # so this reduces to the original `op_failed_ids or reviewer_blocked_ids`
    # check there. `decisions` itself is deliberately not consulted here:
    # a resume caller pre-filters `decisions` to exclude every still-
    # pending column before calling this function (its `omit_by_file` is
    # the authoritative record of what remains pending).
    final_status = ("partially_complete" if (op_failed_ids or reviewer_blocked_ids or omit_by_file)
                    else "complete")

    # Advisory checkpoint: a Manager consult here is never a safety gate
    # (Publish Guard below remains the deterministic boundary regardless of
    # its advice); it only lets a systemically bad run reach a human sooner
    # than Publish Guard's blunter "blocked" report would.
    coverage_advice = await manager.consult(
        agent_name="Reviewer", phase="reviewer",
        signal={"operator_failed_count": len(op_failed_ids),
                "reviewer_blocked_count": len(reviewer_blocked_ids),
                "decision_count": len(decisions)})
    if coverage_advice.action == "escalate_human_review":
        await db.sessions.update_one(session_filter, {"$set": {
            "reviewer_findings": rv_out["findings"], "operator_failures": op_failed_ids}})
        # Recursive cancellation (D9/Phase 4 step 5): this return path
        # previously left Scout's background task running unobserved --
        # the "Scout leak" the plan names explicitly. Fence it here,
        # before returning, same as every other exit path below.
        await _cancel_and_await(scout_task)
        return await _escalate_to_human_review(
            db=db, session_filter=session_filter, reasons=["manager_advisory_coverage_escalation"],
            reasons_plain=plain_human_review_reasons(["manager_advisory_coverage_escalation"]),
            close_last_phase=close_last_phase, phase_timings=phase_timings,
            run_elapsed_s=time.perf_counter() - run_started,
            approved_decisions=decisions, sentinel_report=sentinel_report,
            manager=manager, store=store, run_id=run_id, node="human_review_audit")

    # Publish Guard: deterministic last-mile PHI scan on emitted exports.
    # GOAL invariant: exports are only 'ready to share publicly' after this
    # boundary check clears. Runs synchronously; downloads are 403 until clean.
    from ..publish_guard import scan_all_exports as _scan_all_exports
    if exec_out["exports"]:
        # If Operator's checks wiped every file out of a non-empty
        # Executor output, scan_all_exports naturally reports "blocked"
        # on the resulting empty dict (scanned == 0) -- exactly the
        # existing "can't certify clean" behavior, no special-casing
        # needed here.
        guard_report = _scan_all_exports(
            exports, decisions=decisions, jurisdiction=session.get("jurisdiction", "us")
        ).to_dict()
    else:
        # Executor itself produced nothing exportable this round (e.g.
        # every column of the only dataset is still deferred). This is a
        # legitimate empty-so-far state, not a leak.
        guard_report = {"status": "clean", "results": [], "scanned": 0, "blocked": 0}
    if store is not None and run_id and guard_report.get("results"):
        from phi_core.control.artifacts import ArtifactService, register_guard_rejections
        await register_guard_rejections(
            ArtifactService(store, session_id=sid, run_id=run_id), guard_report=guard_report,
        )
    await on_phase("publish_guard", {"status": guard_report["status"],
                                     "scanned": guard_report["scanned"],
                                     "blocked": guard_report["blocked"]})

    if guard_report["status"] != "clean":
        await close_last_phase()
        manager_report = await manager.close_run("blocked")
        await db.sessions.update_one(
            session_filter,
            {"$set": {
                "status": "blocked",
                "guard_report": guard_report,
                "export_paths": exports,
                "agent_decisions": decisions,
                "phase_timings": phase_timings,
                "run_elapsed_s": round(time.perf_counter() - run_started, 3),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "manager_report": manager_report,
                "reviewer_findings": rv_out["findings"],
                "operator_failures": op_failed_ids,
            }},
        )
        await _cancel_and_await(scout_task)
        cleanup_session_unpacked(sid)
        return {"status": "blocked", "guard": guard_report,
                "decisions": decisions, "phase_timings": phase_timings}

    try:
        await _check_cancel(db, sid, on_phase)
    except PipelineCancelled:
        await _cancel_and_await(scout_task)
        raise

    # Auditor (Scout already started earlier, in parallel with
    # Operator/Reviewer/Publish Guard). Ledger + Herald still need
    # Auditor's metrics + Scout's landscape so they wait on both here.
    await on_phase("auditor_scout", {})
    from phi_core.control.artifacts import _hash_file
    artifact_refs: list[tuple[str, str]] = []
    for file_id, path in exports.items():
        try:
            sha256, _size = _hash_file(path)
        except OSError:
            continue
        artifact_refs.append((file_id, sha256))
    auditor_agent = Auditor(await make_ctx("Auditor"))
    audit, scout, benchmark = await asyncio.gather(
        auditor_agent.run(
            decisions=decisions, exports=exports, files=files, artifact_refs=artifact_refs,
            statute=statute, praxis_methods=praxis_methods,
            audit_controls=(
                await asyncio.gather(
                    store.find_many("evidence_claims", {"run_id": run_id}),
                    store.find_many("gate_results", {"run_id": run_id}),
                )
                if store is not None and run_id else ([], [])
            ),
        ),
        scout_task,
        _empty(None),   # placeholder for future synthetic benchmark run
        return_exceptions=True,
    )
    # Auditor/Scout are presentational (Publish Guard already gated the
    # export above); an unhandled exception here must not crash a run that
    # already succeeded. Log it and fall back to a report that visibly says
    # "not verified" rather than claiming a clean audit it never performed.
    audit_crashed = isinstance(audit, Exception)
    if audit_crashed:
        await auditor_agent._log("auditor.crashed", "info",
                                  {"error": f"{type(audit).__name__}: {audit}"})
        audit = {"verdict": "issues", "issues": [{"file": "", "problem": "Auditor crashed; not verified"}],
                 "metrics": {}, "confidence": 0.0,
                 "summary": "Auditor raised an exception; audit not performed."}
    if isinstance(scout, Exception):
        await scout_agent._log("scout.crashed", "info", {"error": f"{type(scout).__name__}: {scout}"})
        scout = {}
    if isinstance(benchmark, Exception):
        benchmark = None

    audit_advice = await manager.consult(
        agent_name="Auditor", phase="auditor_scout",
        signal={"audit_verdict": str(audit.get("verdict")), "audit_crashed": audit_crashed})
    # The confidence-floor escalation is a deterministic gate, not an LLM
    # advisory: this is the design doc's "second human review", distinct
    # from Sentinel's pre-execution round -- it must fire on the numbers
    # every time, never fail open the way `consult()` legitimately does.
    auditor_reason = auditor_escalation_reason(audit, artifact_refs=dict(artifact_refs))
    if audit_advice.action == "escalate_human_review" or auditor_reason:
        reasons = [r for r in ("manager_advisory_audit_escalation" if audit_advice.action == "escalate_human_review" else None,
                               auditor_reason) if r]
        # Turn any per-column Auditor disagreement into a resolvable
        # human_review decision, so this second review has an actual
        # lever for the reviewer to pull rather than only a status flag
        # that can be blindly resubmitted against a non-deterministic model.
        decisions = materialize_auditor_disagreements(decisions, audit, dictionary_by_column)
        # D13 step 4/7: a content hash of the actual verdict, not an
        # incrementing counter -- any change to Auditor's issues/metrics
        # for this run mints a new value, so a reviewer's later
        # `confirm_auditor_confidence` submission can be checked against
        # exactly the verdict they saw, not merely "some verdict existed".
        # Persisted onto the session document (not just the durable
        # `HumanReviewRequest`) so the frontend's plain `GET
        # /api/sessions/{sid}` poll can render the confirm control without
        # a second endpoint.
        audit_version = hashlib.sha256(
            json.dumps(audit, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
        await db.sessions.update_one(session_filter, {"$set": {
            "guard_report": guard_report, "export_paths": exports,
            "reviewer_findings": rv_out["findings"], "operator_failures": op_failed_ids,
            "audit": audit, "agent_decisions": decisions, "audit_version": audit_version,
        }})
        return await _escalate_to_human_review(
            db=db, session_filter=session_filter, reasons=reasons,
            reasons_plain=plain_human_review_reasons(reasons),
            close_last_phase=close_last_phase, phase_timings=phase_timings,
            run_elapsed_s=time.perf_counter() - run_started,
            approved_decisions=decisions, sentinel_report=sentinel_report,
            manager=manager, store=store, run_id=run_id, node="human_review_audit",
            audit_version=audit_version)

    await _check_cancel(db, sid, on_phase)

    # Ledger (split into Compare + Aggregate under the hood). D5 plan step
    # 5: Compare/Aggregate are durable child work under Ledger's own task,
    # not a bare root enqueue, with SuperOrchestrator.accept_result as the
    # sole authority accepting each subagent's material result.
    await on_phase("ledger", {})
    ledger_ctx = await make_ctx("Ledger")
    ledger = await Ledger(
        ledger_ctx,
        await make_child_ctx("Ledger.Compare", ledger_ctx.task_id),
        await make_child_ctx("Ledger.Aggregate", ledger_ctx.task_id),
        complete_and_accept=complete_and_accept,
    ).run(decisions=decisions, audit=audit, scout=scout, benchmark_result=benchmark)
    if ledger_ctx.tasks is not None:
        await ledger_ctx.tasks.complete(ledger)
    await require_accepted(ledger_ctx, ledger, "Ledger")

    await _check_cancel(db, sid, on_phase)

    # Herald (split into Abstract + Sections under the hood so no LLM
    # call exceeds the 90 s hard timeout). Same durable-child-work
    # treatment as Ledger above.
    await on_phase("herald", {})
    herald_ctx = await make_ctx("Herald")
    herald = await Herald(
        herald_ctx,
        await make_child_ctx("Herald.Abstract", herald_ctx.task_id),
        await make_child_ctx("Herald.Sections", herald_ctx.task_id),
        complete_and_accept=complete_and_accept,
    ).run(ledger=ledger, audit=audit, target_venue=session.get("target_venue") or "JAMIA Open")
    if herald_ctx.tasks is not None:
        await herald_ctx.tasks.complete(herald)
    await require_accepted(herald_ctx, herald, "Herald")

    await close_last_phase()
    manager_report = await manager.close_run(final_status)
    result = {
        "status": final_status,
        "decisions": decisions,
        "audit": audit,
        "scout": scout,
        "ledger": ledger,
        "herald": herald,
        "exports": exports,
        "guard": guard_report,
        "phase_timings": phase_timings,
        "run_elapsed_s": round(time.perf_counter() - run_started, 3),
        "manager_report": manager_report,
        "operator_failures": op_failed_ids,
        "reviewer_findings": rv_out["findings"],
    }
    result.update(extra_result_fields or {})
    completion_set = {
        "agent_audit": audit,
        "agent_ledger": ledger,
        "agent_herald": herald,
        "agent_scout": scout,
        "guard_report": guard_report,
        "export_paths": exports,
        "status": final_status,
        "phase_timings": phase_timings,
        "run_elapsed_s": result["run_elapsed_s"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "manager_report": manager_report,
        "operator_failures": op_failed_ids,
        "reviewer_findings": rv_out["findings"],
    }
    completion_set.update(extra_completion_fields or {})
    await db.sessions.update_one(session_filter, {"$set": completion_set})
    if final_status == "complete":
        cleanup_session_unpacked(sid)
    return result


async def run_pipeline(
    session: dict[str, Any],
    db: AsyncIOMotorDatabase,
    llm_cfg: LlmConfig,
    emit: Callable[[AgentMessage], Awaitable[None]],
    on_phase: PhaseCb,
    run_id: str | None = None,
    control_store: "ControlStore | None" = None,
    root_task_id: str | None = None,
) -> dict[str, Any]:
    from phi_core.control.gates import DecisionGateFailure, run_decision_gates

    sid = session["id"]
    session_filter = {"id": sid}
    if run_id is not None:
        session_filter["_pipeline_run_id"] = run_id
    files = session.get("files", [])
    dataset_files = [f for f in files if f["kind"] == "dataset"]
    form_files = [f for f in files if f["kind"] == "narrative"]
    dict_files = [f for f in files if f["kind"] == "metadata"]

    _phase_timings: dict[str, dict[str, float]] = {}
    _last_phase: dict[str, str | None] = {"key": None, "t0": 0.0}
    _run_started = time.perf_counter()
    _original_on_phase = on_phase

    async def timed_on_phase(phase: str, payload: dict[str, Any]) -> None:
        now = time.perf_counter()
        prev = _last_phase["key"]
        if prev and prev not in ("cancelled", "complete", "__end__") and prev != phase:
            row = _phase_timings.setdefault(prev, {"start_s": _last_phase["t0"] - _run_started})
            row["end_s"] = now - _run_started
            row["duration_ms"] = (now - _last_phase["t0"]) * 1000
        _phase_timings.setdefault(phase, {"start_s": now - _run_started})
        _last_phase["key"] = phase
        _last_phase["t0"] = now
        payload = dict(payload or {})
        payload["_elapsed_s"] = round(now - _run_started, 3)
        await manager.note_phase(phase, now - _run_started)
        await _original_on_phase(phase, payload)

    async def close_last_phase() -> None:
        prev = _last_phase["key"]
        if not prev:
            return
        now = time.perf_counter()
        row = _phase_timings.setdefault(prev, {"start_s": _last_phase["t0"] - _run_started})
        row.setdefault("end_s", now - _run_started)
        row.setdefault("duration_ms", (now - _last_phase["t0"]) * 1000)

    on_phase = timed_on_phase
    iteration_cap = int(session.get("iteration_cap") or ITERATION_CAP)
    iteration_cap = max(1, min(iteration_cap, ITERATION_CAP))

    factory = ActivationFactory(db, llm_cfg, store=control_store)
    effective_run_id = run_id or session.get("_pipeline_run_id") or sid
    _manager_box: dict[str, "Manager | None"] = {"value": None}

    async def make_ctx(agent: str) -> AgentContext:
        if root_task_id:
            return await factory.activate_child(
                session_id=sid,
                run_id=effective_run_id,
                parent_task_id=root_task_id,
                agent=agent,
                emit=emit,
                manager=_manager_box["value"],
                lease_owner=f"pipeline:{effective_run_id}",
            )
        return await factory.activate(
            session_id=sid,
            run_id=effective_run_id,
            agent=agent,
            emit=emit,
            manager=_manager_box["value"],
            lease_owner=f"pipeline:{effective_run_id}",
        )

    async def make_child_ctx(agent: str, parent_task_id: str) -> AgentContext:
        return await factory.activate_child(
            session_id=sid,
            run_id=effective_run_id,
            parent_task_id=parent_task_id,
            agent=agent,
            emit=emit,
            manager=_manager_box["value"],
            lease_owner=f"pipeline:{effective_run_id}",
        )

    async def complete_and_accept(ctx: AgentContext, result: dict[str, Any]) -> bool:
        return await factory.complete_and_accept(ctx, result)

    async def require_accepted(ctx: AgentContext, result: dict[str, Any], agent: str) -> None:
        if not await complete_and_accept(ctx, result):
            raise ResultAcceptanceError(f"{agent} result was not accepted")

    manager = Manager(await make_ctx("Manager"), db=db)
    _manager_box["value"] = manager
    manager_result = await manager.run(
        roster=["Lexicon", "Schema", "Instrument", "Statute", "Praxis", "Judge",
                "Sentinel", "Executor", "Auditor", "Scout", "Ledger", "Herald"],
        phase_plan=["specialists", "statute", "praxis", "judge_iter", "sentinel_iter",
                    "executor", "publish_guard", "auditor_scout", "ledger", "herald"],
    )
    await require_accepted(manager.ctx, manager_result, "Manager")

    # 1+2+2b. Specialists, Statute, and Praxis kicked off IN PARALLEL.
    #
    # Speedup rationale (Sir Q "the entire process is very slow"):
    #  - Statute only needs the jurisdiction string from the session.
    #  - Praxis only needs the hardcoded HIPAA category list.
    #  - Neither reads file content, so they don't have to wait on
    #    Lexicon/Schema/Instrument. Launching them at t=0 overlaps all
    #    of their runtime (10 web searches on cold cache) with the
    #    specialist file parsing.
    await on_phase("specialists", {"agents": ["Lexicon", "Schema", "Instrument"]})
    await on_phase("statute", {})
    await on_phase("praxis", {})

    hipaa_cats = ["A", "B", "C", "D", "F", "G", "H", "I", "J", "K",
                  "L", "M", "N", "O", "P", "Q", "R"]
    async def _praxis_method(category: str) -> dict[str, Any]:
        # Praxis is called per-category via `method_for`, never `run` --
        # `Agent.__init_subclass__`'s completion wrap only ever sees `run`,
        # so this path completes/fails its own task explicitly, matching
        # what that wrap does for every other agent.
        ctx = await make_ctx("Praxis")
        agent = Praxis(ctx)
        try:
            result = await agent.method_for(category)
        except Exception as exc:
            await agent._log("praxis.category_failed", "info",
                              {"category": category, "error": f"{type(exc).__name__}: {exc}"})
            if ctx.tasks is not None:
                await ctx.tasks.fail(f"agent_crashed:{type(exc).__name__}")
            raise
        result = result if isinstance(result, dict) else {}
        if ctx.tasks is not None:
            await ctx.tasks.complete(result)
        await require_accepted(ctx, result, "Praxis")
        return result

    async def _statute_run() -> dict[str, Any]:
        ctx = await make_ctx("Statute")
        agent = Statute(ctx)
        result = await agent.run(jurisdiction=session.get("jurisdiction", "us"))
        await require_accepted(ctx, result, "Statute")
        return result

    statute_task = asyncio.create_task(_statute_run())
    praxis_gather_task = asyncio.gather(
        *[_praxis_method(category) for category in hipaa_cats],
        return_exceptions=True,
    )

    # Specialists: independent of each other now that Schema no longer
    # enriches its prompt from Lexicon's dictionary columns (Task 6 made
    # Schema deterministic) -- Lexicon, Schema and Instrument all launch
    # under one gather instead of Schema waiting on Lexicon.
    lexicon_ctx = await make_ctx("Lexicon") if dict_files else None
    schema_ctx = await make_ctx("Schema") if dataset_files else None
    instrument_ctx = await make_ctx("Instrument") if form_files else None
    lexicon_agent = Lexicon(lexicon_ctx) if lexicon_ctx else None
    schema_agent = Schema(schema_ctx) if schema_ctx else None
    instrument_agent = Instrument(instrument_ctx) if instrument_ctx else None
    lex_task = lexicon_agent.run(dict_files=dict_files) if lexicon_agent else _empty({"columns": [], "notes": ""})
    schema_task = schema_agent.run(dataset_files=dataset_files) if schema_agent else _empty({"columns": []})
    inst_task = instrument_agent.run(form_files=form_files) if instrument_agent else _empty({"fields": []})
    lexicon, schema, instrument = await asyncio.gather(lex_task, schema_task, inst_task)
    if lexicon_agent:
        await require_accepted(lexicon_ctx, lexicon, "Lexicon")
    if schema_agent:
        await require_accepted(schema_ctx, schema, "Schema")
    if instrument_agent:
        await require_accepted(instrument_ctx, instrument, "Instrument")
    # Deterministic guardian query broker: Manager holds the only reference
    # to each specialist for targeted ask_schema/ask_instrument/ask_lexicon
    # lookups, attached only when that specialist actually ran.
    if lexicon_agent:
        manager.attach_lexicon(lexicon_agent)
    if schema_agent:
        manager.attach_schema(schema_agent)
    if instrument_agent:
        manager.attach_instrument(instrument_agent)
    # Carried forward for the site/facility cardinality rule. Fakes/mocks
    # in tests never set `_stats`, so default to empty rather than assume
    # a real Schema instance ran.
    schema_stats = getattr(schema_agent, "_stats", {}) if schema_agent else {}
    prompt_scrub_counts = {
        "lexicon": lexicon_agent.scrub_count if lexicon_agent else 0,
        "instrument": instrument_agent.scrub_count if instrument_agent else 0,
    }

    # Now await the parallel experts.
    statute = await statute_task
    praxis_results = await praxis_gather_task
    praxis_methods: dict[str, Any] = {}
    for cat, res in zip(hipaa_cats, praxis_results, strict=True):
        if isinstance(res, Exception):
            continue
        praxis_methods[cat] = res

    # 3. Judge <-> Sentinel loop -- short-circuits on 0 blocking issues.
    judge_call_failures = 0
    sentinel_call_failures = 0
    last_judge_message_id: str | None = None
    prior_feedback = ""
    approved_decisions: list[dict[str, Any]] = []
    advisory_issues: list[dict[str, Any]] = []
    all_sentinel_overrides: list[dict[str, Any]] = []
    all_model_output_rejections: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    manager_early_escalation = False
    # Anti-loop rule: (file_id, column) -> action that Sentinel rejected as
    # blocking in a prior iteration. If Judge's revision proposes the exact
    # same action again, it isn't a real revision -- escalate immediately
    # instead of burning another iteration on a repeated proposal. Praxis
    # multi-method scoring can later refine this to (action, method) so a
    # differently-keyed hash doesn't get treated as a repeat.
    prior_blocking_actions: dict[tuple[str, str], dict[str, Any]] = {}
    # Blocking-issue floor (Task 19/20): a dedicated per-column counter,
    # independent of iteration_cap, that forces human_review once Sentinel
    # has raised 'blocking' on a column BLOCKING_ISSUE_FLOOR times. This
    # keeps a low rigor setting from letting a genuinely contested column
    # ship without review -- the loop runs up to max_iterations even when
    # iteration_cap is lower, but never further than Thorough already did.
    blocking_attempts: dict[tuple[str, str], int] = {}
    max_iterations = max(iteration_cap, BLOCKING_ISSUE_FLOOR)
    for iteration in range(1, max_iterations + 1):
        await _check_cancel(db, sid, on_phase)
        await on_phase(f"judge_iter_{iteration}", {"iteration": iteration})
        judge_ctx = await make_ctx("Judge")
        judge = Judge(judge_ctx)
        j = await judge.run(schema=schema, instrument=instrument, lexicon=lexicon,
                            statute=statute, praxis=praxis_methods,
                            prior_feedback=prior_feedback)
        await require_accepted(judge_ctx, j, "Judge")
        judge_call_failures += judge.call_failures
        last_judge_message_id = judge.last_message_id
        decisions = j.get("decisions", [])
        # 2.10: coerce any model-proposed action/subject/category outside the
        # executable vocabulary to the fail-closed default before the hard-rule
        # table or Sentinel ever see it.
        decisions, rejections = validate_decisions(decisions)
        all_model_output_rejections.extend(rejections)
        # Sentinel deterministic hard-rules: force known direct identifiers off
        # 'human_review' before invoking the LLM Sentinel. Closes the accuracy
        # gap where Judge routes obvious PHI to human review out of caution.
        decisions, overrides = apply_sentinel_hard_rules(decisions)
        if overrides:
            all_sentinel_overrides.extend(overrides)
            await on_phase(f"sentinel_hard_rules_iter_{iteration}",
                           {"iteration": iteration, "overrides": overrides})
        # Cross-column rule: a retained age column means DOB must be dropped,
        # not transformed. Deterministic, so it runs before Sentinel rather
        # than relying on the LLM to catch it and spend an iteration.
        decisions, age_dob_overrides = apply_age_dob_rule(decisions)
        if age_dob_overrides:
            all_sentinel_overrides.extend(age_dob_overrides)
            await on_phase(f"age_dob_rule_iter_{iteration}",
                           {"iteration": iteration, "overrides": age_dob_overrides})
        # Site/facility cardinality rule (Task 22, Sentinel plan item 4): a
        # confidently wrong 'keep' on a low-cardinality site or facility
        # column passes both the confidence floor and Sentinel's LLM
        # judgment, because the risk is knowable from the column's shape,
        # not from Judge's confidence score. Schema-driven and
        # deterministic, so it runs before the anti-loop check and before
        # Sentinel ever sees the column.
        decisions, cardinality_overrides = apply_site_cardinality_rule(decisions, schema_stats)
        if cardinality_overrides:
            all_sentinel_overrides.extend(cardinality_overrides)
            await on_phase(f"site_cardinality_iter_{iteration}",
                           {"iteration": iteration, "overrides": cardinality_overrides})
        # Anti-loop: a decision repeating a previously-rejected action isn't
        # a real revision. Force it straight to human review rather than
        # resubmitting it to Sentinel for the same rejection.
        anti_loop_forced: list[dict[str, Any]] = []
        if prior_blocking_actions:
            forced_decisions = []
            for d in decisions:
                key = (d.get("file_id"), d.get("column"))
                prior = prior_blocking_actions.get(key)
                if prior and d.get("action") == prior.get("action"):
                    forced = dict(d)
                    forced.update(
                        action="human_review",
                        # Fixed, compile-time prefix `_escalation_reason_phrase`
                        # (reasoning.py) matches on to classify this as an
                        # anti-loop escalation in the plain-English reviewer
                        # prompt. The free text after the colon is never read
                        # by that classifier and never surfaces to a reviewer.
                        reason=(
                            f"Anti-loop: Judge repeated the previously-rejected "
                            f"action {prior.get('action')!r} without a substantive "
                            "revision; forced to human review rather than "
                            "resubmitting to Sentinel for the same rejection."
                        ),
                        suggested_action=prior.get("action"),
                        suggested_confidence=d.get("confidence"),
                        suggested_reason=(
                            f"Judge repeated the previously-rejected action "
                            f"{prior.get('action')!r} without change. Sentinel's objection: "
                            f"{prior.get('issue_text') or 'see prior iteration issues'}"
                        ),
                    )
                    anti_loop_forced.append({
                        "file_id": d.get("file_id"), "column": d.get("column"),
                        "repeated_action": d.get("action"),
                    })
                    forced_decisions.append(forced)
                else:
                    forced_decisions.append(d)
            decisions = forced_decisions
            if anti_loop_forced:
                await on_phase(f"anti_loop_iter_{iteration}",
                               {"iteration": iteration, "forced": anti_loop_forced})
        # Confidence floor: below 0.80 always goes to human review, whether
        # or not Sentinel would agree with it. Deterministic, so it runs
        # before the LLM review rather than costing a review call on a
        # decision that is going to human review regardless.
        decisions, floor_overrides = apply_confidence_floor(decisions)
        if floor_overrides:
            all_sentinel_overrides.extend(floor_overrides)
            await on_phase(f"confidence_floor_iter_{iteration}",
                           {"iteration": iteration, "overrides": floor_overrides})
        await _check_cancel(db, sid, on_phase)
        await on_phase(f"sentinel_iter_{iteration}", {"iteration": iteration, "decision_count": len(decisions)})
        sentinel_ctx = await make_ctx("Sentinel")
        sentinel = Sentinel(sentinel_ctx)
        s = await sentinel.run(decisions=decisions, statute=statute, instrument=instrument,
                               parent_id=last_judge_message_id)
        await require_accepted(sentinel_ctx, s, "Sentinel")
        sentinel_call_failures += sentinel.call_failures
        # Sentinel-originated escalation: genuine ambiguity Sentinel can't
        # correct itself. Applied immediately -- these columns skip the
        # remaining Judge iterations rather than looping.
        escalations = _escalation_issues(s)
        if escalations:
            decisions, escalation_overrides = apply_sentinel_escalations(decisions, escalations)
            if escalation_overrides:
                all_sentinel_overrides.extend(escalation_overrides)
                await on_phase(f"sentinel_escalation_iter_{iteration}",
                               {"iteration": iteration, "overrides": escalation_overrides})
        blocking = _blocking_issues(s)
        # Record this iteration's blocking columns/actions so the next
        # iteration's anti-loop check can compare against them.
        blocking_by_column = {(b.get("file_id"), b.get("column")): b for b in blocking if b.get("column")}
        # Blocking-issue floor: increment the per-column counter for every
        # column Sentinel raised 'blocking' on this iteration, independent
        # of iteration_cap.
        for key in blocking_by_column:
            blocking_attempts[key] = blocking_attempts.get(key, 0) + 1
        for d in decisions:
            key = (d.get("file_id"), d.get("column"))
            if key in blocking_by_column and d.get("action") != "human_review":
                prior_blocking_actions[key] = {
                    "action": d.get("action"),
                    "issue_text": blocking_by_column[key].get("problem") or "",
                }
        # Every advisory issue stays in the audit trail even after early
        # approval (Sir Q1: 'nitpicks logged where required').
        advisory_issues.extend(
            i for i in (s.get("issues") or [])
            if str(i.get("severity", "")).lower() == "advisory"
        )
        # A column at the floor never gets a fourth Judge iteration: force
        # it to human_review now, before the next iteration's Judge call.
        decisions, blocking_floor_overrides = apply_blocking_floor(decisions, blocking_attempts)
        if blocking_floor_overrides:
            all_sentinel_overrides.extend(blocking_floor_overrides)
            await on_phase(f"blocking_floor_iter_{iteration}",
                           {"iteration": iteration, "overrides": blocking_floor_overrides})
        approved_decisions = decisions
        if not blocking:
            # Sir Q1: iterate only when required. No blocking issues means
            # Sentinel has nothing PHI-critical to complain about, so we
            # approve and skip the remaining iterations.
            await on_phase(f"sentinel_short_circuit_iter_{iteration}",
                           {"iteration": iteration,
                            "advisory_issues": len(advisory_issues)})
            s["verdict"] = "approved"
            break
        if blocking_by_column and iteration >= iteration_cap and all(
            blocking_attempts.get(key, 0) >= BLOCKING_ISSUE_FLOOR for key in blocking_by_column
        ):
            # Every still-blocking column has already been forced to
            # human_review by apply_blocking_floor above -- the cap is
            # passed and the floor is satisfied, so looping further would
            # only re-litigate columns already settled. `blocking_by_column`
            # must be non-empty here: `all()` over an empty generator is
            # vacuously True, which would let a malformed Sentinel reply
            # (blocking issues with no `column` key, so nothing was ever
            # tracked toward the floor) short-circuit the loop after a
            # single iteration under a low iteration_cap, well before any
            # column actually earned three tries.
            break
        prior_feedback = _summarise_issues(blocking)
        if iteration < max_iterations:
            advice = await manager.consult(
                agent_name="Judge", phase=f"judge_sentinel_iter_{iteration}",
                signal={"iteration": iteration, "iteration_cap": iteration_cap,
                        "blocking_count": len(blocking),
                        "advisory_count": len(advisory_issues),
                        "decision_count": len(decisions),
                        "judge_call_failures": judge_call_failures,
                        "sentinel_call_failures": sentinel_call_failures})
            if advice.action == "escalate_human_review":
                manager_early_escalation = True
                break

    llm_failures = {
        "judge": judge_call_failures,
        "sentinel": sentinel_call_failures,
        "empty_decisions": not decisions,
    }

    if not approved_decisions:
        approved_decisions = []
    # SEC-006: scrub any PHI substrings the LLM may have echoed into the
    # `reason`/`citation` fields before we persist. The audit found a real
    # patient name in a stored decision reason on the live deployment.
    approved_decisions = [scrub_decision(d) for d in approved_decisions]
    dictionary_by_column = {c.get("name"): c.get("description", "")
                            for c in lexicon.get("columns", []) if c.get("name")}
    # Final, authoritative decision-mutation event before this decision set
    # is allowed anywhere near Executor: the full canonical D11 gate
    # sequence, re-run one last time over whatever the Judge/Sentinel loop
    # converged on. Every gate above already ran per-iteration for cost
    # reasons (Sentinel review is a paid call, so cheap deterministic
    # gates run before it); this pass is idempotent over already-settled
    # decisions and its only NEW contribution is `assert_exact_coverage`,
    # the fail-closed proof that Executor must never receive a duplicate,
    # missing, or invented decision.
    gate_outcome = await run_decision_gates(
        decisions=approved_decisions,
        files=dataset_files,
        statute=statute,
        instrument=instrument,
        schema_stats=schema_stats,
        jurisdiction=session.get("jurisdiction", "us"),
        blocking_attempts=blocking_attempts,
        sentinel_report=s if isinstance(s, dict) else None,
        stage="orchestrator.final_decision",
        # A fresh real activation, not `judge.ctx`: a test double can
        # (and does) stand in for `Judge` without retaining `.ctx`, and
        # this stamp is bookkeeping only -- `run_decision_gates` never
        # calls the gateway this context carries. Deliberately calls
        # `factory.activate` directly rather than `make_ctx` (which would
        # otherwise route it through `activate_child` under the root
        # Pipeline task): this stamp has no root task to be a child of
        # and is intentionally out of scope for the D5 child-of-root
        # wiring in this step.
        ctx=await factory.activate(
            session_id=sid,
            run_id=effective_run_id,
            agent="Judge",
            emit=emit,
            manager=_manager_box["value"],
            lease_owner=f"pipeline:{effective_run_id}",
        ),
    )
    for gate_result in gate_outcome.gate_results:
        await factory.store.insert("gate_results", gate_result)
    if gate_outcome.overrides:
        all_sentinel_overrides.extend(gate_outcome.overrides)
    keep_demotions = gate_outcome.demotions
    if keep_demotions:
        await on_phase("keep_verification", {"demotions": keep_demotions})
    # Re-annotate with the real lexicon-derived dictionary: the gate
    # sequence's own internal annotate_pending_review pass has no
    # dictionary context, so this overwrite is purely additive (same
    # action/confidence/coverage, richer reviewer_prompt text).
    approved_decisions = annotate_pending_review(gate_outcome.decisions, dictionary_by_column)
    if not gate_outcome.ok:
        raise DecisionGateFailure(gate_outcome)
    if isinstance(s, dict):
        s = dict(s)
        if isinstance(s.get("issues"), list):
            s["issues"] = [scrub_persisted_text(x) if isinstance(x, str) else x for x in s["issues"]]
        for k in ("summary", "reason", "notes"):
            if isinstance(s.get(k), str):
                s[k] = scrub_persisted_text(s[k])
    # Human review is required when a decision routes to human_review, an
    # agent exhausted supervised retries, Sentinel still has unresolved
    # BLOCKING issues after the iteration cap, or the Manager advises early
    # escalation because the Judge/Sentinel loop is not converging.
    reasons: list[str] = []
    human_needed = any(d.get("action") == "human_review" for d in approved_decisions)
    if human_needed:
        reasons.append("decision_routed_human_review")
    if judge_call_failures or sentinel_call_failures or not decisions:
        human_needed = True
        if judge_call_failures:
            reasons.append("judge_call_failure")
        if sentinel_call_failures:
            reasons.append("sentinel_call_failure")
        if not decisions:
            reasons.append("empty_decisions")
    if _blocking_issues(s):
        human_needed = True
        reasons.append("sentinel_blocking_after_cap")
        await on_phase("human_review_required",
                       {"reason": "sentinel still has blocking issues after cap",
                        "blocking_count": len(_blocking_issues(s))})
    if manager_early_escalation:
        human_needed = True
        reasons.append("manager_advisory_early_escalation")

    # 4. Human review gate (persist and pause if needed)
    await db.sessions.update_one(
        session_filter,
        {"$set": {
            "agent_decisions": approved_decisions,
            "agent_sentinel_last": s,
            "agent_statute": statute,
            "agent_specialists": {"schema": schema, "instrument": instrument, "lexicon": lexicon},
            "agent_praxis": praxis_methods,
            "sentinel_overrides": all_sentinel_overrides,
            "keep_demotions": keep_demotions,
            "prompt_scrub_counts": prompt_scrub_counts,
            "llm_failures": llm_failures,
            "model_output_rejections": all_model_output_rejections,
            "human_review_required": human_needed,
        }},
    )
    if human_needed:
        return await _escalate_to_human_review(
            db=db, session_filter=session_filter, reasons=reasons,
            reasons_plain=plain_human_review_reasons(reasons),
            close_last_phase=close_last_phase, phase_timings=_phase_timings,
            run_elapsed_s=time.perf_counter() - _run_started,
            approved_decisions=approved_decisions, sentinel_report=s,
            manager=manager, store=control_store, run_id=effective_run_id,
            node="human_review_decisions")

    return await execute_decisions(
        db=db, sid=sid, session=session, session_filter=session_filter,
        files=files, decisions=approved_decisions,
        statute=statute, praxis_methods=praxis_methods,
        dictionary_by_column=dictionary_by_column,
        make_ctx=make_ctx, make_child_ctx=make_child_ctx, complete_and_accept=complete_and_accept,
        manager=manager, on_phase=on_phase,
        close_last_phase=close_last_phase, phase_timings=_phase_timings,
        run_started=_run_started, sentinel_report=s,
        extra_completion_fields={"advisory_issues": advisory_issues, "iteration_cap": iteration_cap},
        extra_result_fields={"advisory_issues": advisory_issues, "iteration_cap": iteration_cap},
        run_id=effective_run_id, store=control_store,
    )


def _summarise_issues(issues: list[dict[str, Any]]) -> str:
    if not issues:
        return ""
    parts = []
    for i in issues[:10]:
        parts.append(f"- {i.get('column')} ({i.get('file_id', '?')[:6]}): {i.get('problem')} -> {i.get('suggested_action')}")
    return "\n".join(parts)


async def _empty(v):
    return v
