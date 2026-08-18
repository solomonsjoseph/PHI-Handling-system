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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from motor.motor_asyncio import AsyncIOMotorDatabase

from .base import AgentMessage, ITERATION_CAP
from .experts import Praxis, Statute
from .llm import LlmConfig
from .manager import Manager
from .operator import Operator
from .reviewer import Reviewer
from .outward import Herald, Ledger, Scout
from .reasoning import (
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
    BLOCKING_ISSUE_FLOOR,
    validate_decisions,
    verify_keep_decisions,
)
from ..paths import cleanup_session_unpacked
from ..security import scrub_decision, scrub_persisted_text
from .specialists import Instrument, Lexicon, Schema


PhaseCb = Callable[[str, dict[str, Any]], Awaitable[None]]


class PipelineCancelled(Exception):
    """Raised by the orchestrator when the operator requested cancel."""


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


async def run_pipeline(
    session: dict[str, Any],
    db: AsyncIOMotorDatabase,
    llm_cfg: LlmConfig,
    emit: Callable[[AgentMessage], Awaitable[None]],
    on_phase: PhaseCb,
    run_id: str | None = None,
) -> dict[str, Any]:
    sid = session["id"]
    session_filter = {"id": sid}
    if run_id is not None:
        session_filter["_pipeline_run_id"] = run_id
    files = session.get("files", [])
    dataset_files = [f for f in files if f["kind"] == "dataset"]
    form_files = [f for f in files if f["kind"] == "narrative"]
    dict_files = [f for f in files if f["kind"] == "metadata"]

    # Sir Q "Live Wallclock Measurement": capture wallclock per phase so the
    # UI can render per-phase seconds and operators can prove the parallel
    # launch actually saved time. Wrap the caller's `on_phase` in a timer
    # that stamps `start` on first visit and `end` on the next visit.
    _phase_timings: dict[str, dict[str, float]] = {}
    _last_phase: dict[str, str | None] = {"key": None, "t0": 0.0}
    _run_started = time.perf_counter()
    _original_on_phase = on_phase  # capture before we rebind below

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

    # Alias so all existing calls to `on_phase(...)` inside this function
    # go through the timer. External callers still see the original cb.
    on_phase = timed_on_phase  # noqa: F811  # rebinding is intentional

    # Sir Q "Sentinel Iteration Cap Tuner": allow per-run rigor. Fast=1,
    # Balanced=2, Thorough=3. Falls back to the module default when the
    # session doc doesn't specify one.
    iteration_cap = int(session.get("iteration_cap") or ITERATION_CAP)
    iteration_cap = max(1, min(iteration_cap, ITERATION_CAP))

    manager = Manager(session_id=sid, llm=llm_cfg, db=db, emit=emit)
    await manager.run(
        roster=["Lexicon", "Schema", "Instrument", "Statute", "Praxis", "Judge",
                "Sentinel", "Executor", "Auditor", "Scout", "Ledger", "Herald"],
        phase_plan=["specialists", "statute", "praxis", "judge_iter", "sentinel_iter",
                    "executor", "publish_guard", "auditor_scout", "ledger", "herald"],
    )
    common = dict(session_id=sid, llm=llm_cfg, db=db, emit=emit, manager=manager)

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
    praxis_agent = Praxis(**common)

    # Fire the independent long-runners immediately. asyncio.gather()
    # eagerly schedules its awaitables, so simply calling it starts the
    # web-search work; we don't need to wrap it in create_task().
    statute_task = asyncio.create_task(
        Statute(**common).run(jurisdiction=session.get("jurisdiction", "us"))
    )
    praxis_gather_task = asyncio.gather(
        *[praxis_agent.method_for(c) for c in hipaa_cats],
        return_exceptions=True,
    )

    # Specialists: independent of each other now that Schema no longer
    # enriches its prompt from Lexicon's dictionary columns (Task 6 made
    # Schema deterministic) -- Lexicon, Schema and Instrument all launch
    # under one gather instead of Schema waiting on Lexicon.
    lexicon_agent = Lexicon(**common) if dict_files else None
    schema_agent = Schema(**common) if dataset_files else None
    instrument_agent = Instrument(**common) if form_files else None
    lex_task = lexicon_agent.run(dict_files=dict_files) if lexicon_agent else _empty({"columns": []})
    schema_task = schema_agent.run(dataset_files=dataset_files) if schema_agent else _empty({"columns": []})
    inst_task = instrument_agent.run(form_files=form_files) if instrument_agent else _empty({"fields": []})
    lexicon, schema, instrument = await asyncio.gather(lex_task, schema_task, inst_task)
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
    for cat, res in zip(hipaa_cats, praxis_results):
        if isinstance(res, Exception):
            # Judge falls back to its own reasoning for any category missing
            # here; logging makes that fallback visible in the audit trail
            # instead of a silently incomplete praxis_methods dict.
            await praxis_agent._log("praxis.category_failed", "info",
                                     {"category": cat, "error": f"{type(res).__name__}: {res}"})
            continue
        praxis_methods[cat] = res

    # 3. Judge <-> Sentinel loop -- short-circuits on 0 blocking issues.
    judge = Judge(**common)
    sentinel = Sentinel(**common)
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
        j = await judge.run(schema=schema, instrument=instrument, lexicon=lexicon,
                            statute=statute, praxis=praxis_methods,
                            prior_feedback=prior_feedback)
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
        s = await sentinel.run(decisions=decisions, statute=statute, instrument=instrument,
                               parent_id=judge.last_message_id)
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
                        "judge_call_failures": judge.call_failures,
                        "sentinel_call_failures": sentinel.call_failures})
            if advice.action == "escalate_human_review":
                manager_early_escalation = True
                break

    llm_failures = {
        "judge": judge.call_failures,
        "sentinel": sentinel.call_failures,
        "empty_decisions": not decisions,
    }

    if not approved_decisions:
        approved_decisions = []
    # SEC-006: scrub any PHI substrings the LLM may have echoed into the
    # `reason`/`citation` fields before we persist. The audit found a real
    # patient name in a stored decision reason on the live deployment.
    approved_decisions = [scrub_decision(d) for d in approved_decisions]
    approved_decisions, keep_demotions = verify_keep_decisions(
        approved_decisions,
        {f["file_id"]: Path(f["stored_path"]) for f in dataset_files},
        jurisdiction=session.get("jurisdiction", "us"),
    )
    if keep_demotions:
        await on_phase("keep_verification", {"demotions": keep_demotions})
    dictionary_by_column = {c.get("name"): c.get("description", "")
                            for c in lexicon.get("columns", []) if c.get("name")}
    approved_decisions = annotate_pending_review(approved_decisions, dictionary_by_column)
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
    if judge.call_failures or sentinel.call_failures or not decisions:
        human_needed = True
        if judge.call_failures:
            reasons.append("judge_call_failure")
        if sentinel.call_failures:
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
        return await manager.escalate_to_human_review(
            session_filter=session_filter, reasons=reasons,
            close_last_phase=close_last_phase, phase_timings=_phase_timings,
            run_elapsed_s=time.perf_counter() - _run_started,
            approved_decisions=approved_decisions, sentinel_report=s)

    # 5. Executor
    await on_phase("executor", {"decision_count": len(approved_decisions)})
    exec_out = await Executor(**common).run(files=files, decisions=approved_decisions)

    # Scout has no dependency on Operator, Reviewer, Publish Guard, or
    # Auditor, so it starts here and runs in the background across all of
    # them; only Ledger/Herald actually need its result.
    scout_agent = Scout(**common)
    scout_task = asyncio.create_task(scout_agent.run())

    # 5a. Operator: deterministic self-verification of what Executor wrote,
    # one stage before Publish Guard, mirroring the Judge/Sentinel split one
    # stage later. exec_out["exports"] stays Executor's own factual record
    # of what it wrote and is never mutated here; `exports` is the
    # Operator-then-Reviewer-filtered view every later step in this
    # function uses.
    await on_phase("operator", {"decision_count": len(approved_decisions)})
    op_out = await Operator(**common).run(files=files, decisions=approved_decisions,
                                          exports=exec_out["exports"])
    # Operator's own `failed_file_ids` only covers a file it could not read
    # or that never made it into `exports` at all (see operator.py). A
    # shape-check or reverse-completeness failure surfaces as a per-column
    # 'fail' verdict on an otherwise-readable file, and must block that
    # file from `exports` exactly the same way -- fold both into one set.
    op_failed_ids = sorted(set(op_out["failed_file_ids"]) |
                           {v["file_id"] for v in op_out["verdicts"] if v.get("verdict") == "fail"})
    exports = {fid: p for fid, p in exec_out["exports"].items()
              if fid not in op_failed_ids}

    # 5b. Reviewer: confirms Operator's coverage of every decision against
    # the real written export, catching gaps Operator's own pass cannot see
    # (e.g. an omit_by_file column that leaked into the header). Its own
    # filtered exports become canonical for every remaining step below,
    # starting with Publish Guard.
    await on_phase("reviewer", {"decision_count": len(approved_decisions)})
    rv_out = await Reviewer(**common).run(
        decisions=approved_decisions,
        operator_result={"failed_file_ids": op_failed_ids, "verdicts": op_out["verdicts"]},
        exports=exports,
        omit_by_file=None,
    )
    reviewer_blocked_ids = sorted(set(exports) - set(rv_out["exports"]))
    exports = rv_out["exports"]
    final_status = "partially_complete" if (op_failed_ids or reviewer_blocked_ids) else "complete"

    # 5c. Publish Guard: deterministic last-mile PHI scan on emitted exports.
    # GOAL invariant: exports are only 'ready to share publicly' after this
    # boundary check clears. Runs synchronously; downloads are 403 until clean.
    from ..publish_guard import scan_all_exports as _scan_all_exports
    guard_report = _scan_all_exports(exports, decisions=approved_decisions, jurisdiction=session.get("jurisdiction", "us")).to_dict()
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
                "agent_decisions": approved_decisions,
                "phase_timings": _phase_timings,
                "run_elapsed_s": round(time.perf_counter() - _run_started, 3),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "manager_report": manager_report,
                "reviewer_findings": rv_out["findings"],
                "operator_failures": op_failed_ids,
            }},
        )
        scout_task.cancel()
        cleanup_session_unpacked(sid)
        return {"status": "blocked", "guard": guard_report,
                "decisions": approved_decisions, "phase_timings": _phase_timings}

    try:
        await _check_cancel(db, sid, on_phase)
    except PipelineCancelled:
        scout_task.cancel()
        raise

    # 6+7. Auditor (Scout already started earlier, in parallel with
    # Operator/Reviewer/Publish Guard). Ledger + Herald still need
    # Auditor's metrics + Scout's landscape so they wait on both here.
    await on_phase("auditor_scout", {})
    auditor_agent = Auditor(**common)
    audit, scout, benchmark = await asyncio.gather(
        auditor_agent.run(decisions=approved_decisions, exports=exports, files=files),
        scout_task,
        _empty(None),   # placeholder for future synthetic benchmark run
        return_exceptions=True,
    )
    # Auditor/Scout are presentational (Publish Guard already gated the
    # export above); an unhandled exception here must not crash a run that
    # already succeeded. Log it and fall back to a report that visibly says
    # "not verified" rather than claiming a clean audit it never performed.
    if isinstance(audit, Exception):
        await auditor_agent._log("auditor.crashed", "info",
                                  {"error": f"{type(audit).__name__}: {audit}"})
        audit = {"verdict": "issues", "issues": [{"file": "", "problem": "Auditor crashed; not verified"}],
                 "metrics": {}, "summary": "Auditor raised an exception; audit not performed."}
    if isinstance(scout, Exception):
        await scout_agent._log("scout.crashed", "info", {"error": f"{type(scout).__name__}: {scout}"})
        scout = {}
    if isinstance(benchmark, Exception):
        benchmark = None

    await _check_cancel(db, sid, on_phase)

    # 7. Ledger (split into Compare + Aggregate under the hood).
    await on_phase("ledger", {})
    ledger = await Ledger(**common).run(decisions=approved_decisions, audit=audit,
                                        scout=scout, benchmark_result=benchmark)

    await _check_cancel(db, sid, on_phase)

    # 8. Herald (split into Abstract + Sections under the hood so no LLM
    # call exceeds the 90 s hard timeout).
    await on_phase("herald", {})
    herald = await Herald(**common).run(ledger=ledger, audit=audit,
                                        target_venue=session.get("target_venue") or "JAMIA Open")

    await close_last_phase()
    manager_report = await manager.close_run(final_status)
    result = {
        "status": final_status,
        "decisions": approved_decisions,
        "audit": audit,
        "scout": scout,
        "ledger": ledger,
        "herald": herald,
        "exports": exports,
        "guard": guard_report,
        "advisory_issues": advisory_issues,
        "phase_timings": _phase_timings,
        "run_elapsed_s": round(time.perf_counter() - _run_started, 3),
        "iteration_cap": iteration_cap,
        "manager_report": manager_report,
        "operator_failures": op_failed_ids,
        "reviewer_findings": rv_out["findings"],
    }
    completion_update = {
        "$set": {
            "agent_audit": audit,
            "agent_ledger": ledger,
            "agent_herald": herald,
            "agent_scout": scout,
            "advisory_issues": advisory_issues,
            "guard_report": guard_report,
            "export_paths": exports,
            "status": final_status,
            "phase_timings": _phase_timings,
            "run_elapsed_s": result["run_elapsed_s"],
            "iteration_cap": iteration_cap,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "manager_report": manager_report,
            "operator_failures": op_failed_ids,
            "reviewer_findings": rv_out["findings"],
        },
    }
    await db.sessions.update_one(session_filter, completion_update)
    if final_status == "complete":
        cleanup_session_unpacked(sid)
    return result


def _summarise_issues(issues: list[dict[str, Any]]) -> str:
    if not issues:
        return ""
    parts = []
    for i in issues[:10]:
        parts.append(f"- {i.get('column')} ({i.get('file_id', '?')[:6]}): {i.get('problem')} -> {i.get('suggested_action')}")
    return "\n".join(parts)


async def _empty(v):
    return v
