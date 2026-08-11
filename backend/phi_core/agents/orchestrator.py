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
from .outward import Herald, Ledger, Scout
from .reasoning import (
    Auditor,
    Executor,
    Judge,
    Sentinel,
    apply_sentinel_hard_rules,
    verify_keep_decisions,
)
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
) -> dict[str, Any]:
    sid = session["id"]
    # A previous completed run may have left export paths and a clean Guard
    # report behind. Clear both atomically before any agent is awaited, so a
    # rerun cannot serve replacement bytes under stale certification.
    await db.sessions.update_one(
        {"id": sid},
        {
            "$set": {"status": "classifying"},
            "$unset": {"guard_report": "", "export_paths": ""},
        },
    )
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

    common = dict(session_id=sid, llm=llm_cfg, db=db, emit=emit)

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

    # Specialists: Lexicon must finish before Schema (Schema enriches its
    # prompt with the dictionary columns). Instrument is independent.
    lex_task = Lexicon(**common).run(dict_files=dict_files) if dict_files else _empty({"columns": []})
    inst_task = Instrument(**common).run(form_files=form_files) if form_files else _empty({"fields": []})
    lexicon = await lex_task
    schema_task = Schema(**common).run(dataset_files=dataset_files, lexicon_columns=lexicon.get("columns", [])) if dataset_files else _empty({"columns": []})
    schema, instrument = await asyncio.gather(schema_task, inst_task)

    # Now await the parallel experts.
    statute = await statute_task
    praxis_results = await praxis_gather_task
    praxis_methods: dict[str, Any] = {}
    for cat, res in zip(hipaa_cats, praxis_results):
        if isinstance(res, Exception):
            continue
        praxis_methods[cat] = res

    # 3. Judge <-> Sentinel loop -- short-circuits on 0 blocking issues.
    judge = Judge(**common)
    sentinel = Sentinel(**common)
    prior_feedback = ""
    approved_decisions: list[dict[str, Any]] = []
    advisory_issues: list[dict[str, Any]] = []
    for iteration in range(1, iteration_cap + 1):
        await _check_cancel(db, sid, on_phase)
        await on_phase(f"judge_iter_{iteration}", {"iteration": iteration})
        j = await judge.run(schema=schema, instrument=instrument, lexicon=lexicon,
                            statute=statute, praxis=praxis_methods,
                            prior_feedback=prior_feedback)
        decisions = j.get("decisions", [])
        # Sentinel deterministic hard-rules: force known direct identifiers off
        # 'human_review' before invoking the LLM Sentinel. Closes the accuracy
        # gap where Judge routes obvious PHI to human review out of caution.
        decisions, overrides = apply_sentinel_hard_rules(decisions)
        if overrides:
            await on_phase(f"sentinel_hard_rules_iter_{iteration}",
                           {"iteration": iteration, "overrides": overrides})
        await _check_cancel(db, sid, on_phase)
        await on_phase(f"sentinel_iter_{iteration}", {"iteration": iteration, "decision_count": len(decisions)})
        s = await sentinel.run(decisions=decisions, statute=statute, instrument=instrument)
        blocking = _blocking_issues(s)
        # Every advisory issue stays in the audit trail even after early
        # approval (Sir Q1: 'nitpicks logged where required').
        advisory_issues.extend(
            i for i in (s.get("issues") or [])
            if str(i.get("severity", "")).lower() == "advisory"
        )
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
        prior_feedback = _summarise_issues(blocking)

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
    if isinstance(s, dict):
        s = dict(s)
        if isinstance(s.get("issues"), list):
            s["issues"] = [scrub_persisted_text(x) if isinstance(x, str) else x for x in s["issues"]]
        for k in ("summary", "reason", "notes"):
            if isinstance(s.get(k), str):
                s[k] = scrub_persisted_text(s[k])
    # Human review is required only when either (a) a decision itself
    # routes to human_review, or (b) Sentinel still has unresolved
    # BLOCKING issues after ITERATION_CAP iterations. Advisory issues
    # are logged and never force human review.
    human_needed = any(d.get("action") == "human_review" for d in approved_decisions)
    if _blocking_issues(s):
        human_needed = True
        await on_phase("human_review_required",
                       {"reason": "sentinel still has blocking issues after cap",
                        "blocking_count": len(_blocking_issues(s))})

    # 4. Human review gate (persist and pause if needed)
    await db.sessions.update_one(
        {"id": sid},
        {"$set": {
            "agent_decisions": approved_decisions,
            "agent_sentinel_last": s,
            "agent_statute": statute,
            "agent_specialists": {"schema": schema, "instrument": instrument, "lexicon": lexicon},
            "human_review_required": human_needed,
        }},
    )
    if human_needed:
        await close_last_phase()
        await db.sessions.update_one(
            {"id": sid},
            {"$set": {"status": "awaiting_human_review",
                      "updated_at": datetime.now(timezone.utc).isoformat(),
                      "phase_timings": _phase_timings,
                      "run_elapsed_s": time.perf_counter() - _run_started}},
        )
        return {"status": "awaiting_human_review",
                "decisions": approved_decisions,
                "sentinel": s,
                "phase_timings": _phase_timings}

    # 5. Executor
    await on_phase("executor", {"decision_count": len(approved_decisions)})
    exec_out = await Executor(**common).run(files=files, decisions=approved_decisions)

    # 5b. Publish Guard: deterministic last-mile PHI scan on emitted exports.
    # GOAL invariant: exports are only 'ready to share publicly' after this
    # boundary check clears. Runs synchronously; downloads are 403 until clean.
    from ..publish_guard import scan_all_exports as _scan_all_exports
    guard_report = _scan_all_exports(exec_out["exports"], decisions=approved_decisions, jurisdiction=session.get("jurisdiction", "us")).to_dict()
    await on_phase("publish_guard", {"status": guard_report["status"],
                                     "scanned": guard_report["scanned"],
                                     "blocked": guard_report["blocked"]})

    await _check_cancel(db, sid, on_phase)

    # 6+7. Auditor and Scout in parallel (Scout has no dependency on
    # Auditor). Ledger + Herald still need Auditor's metrics + Scout's
    # landscape so they wait on both.
    await on_phase("auditor_scout", {})
    audit, scout, benchmark = await asyncio.gather(
        Auditor(**common).run(decisions=approved_decisions, exports=exec_out["exports"], files=files),
        Scout(**common).run(),
        _empty(None),   # placeholder for future synthetic benchmark run
    )

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
    result = {
        "status": "complete",
        "decisions": approved_decisions,
        "audit": audit,
        "scout": scout,
        "ledger": ledger,
        "herald": herald,
        "exports": exec_out["exports"],
        "guard": guard_report,
        "advisory_issues": advisory_issues,
        "phase_timings": _phase_timings,
        "run_elapsed_s": round(time.perf_counter() - _run_started, 3),
        "iteration_cap": iteration_cap,
    }
    await db.sessions.update_one(
        {"id": sid},
        {"$set": {
            "agent_audit": audit,
            "agent_ledger": ledger,
            "agent_herald": herald,
            "agent_scout": scout,
            "advisory_issues": advisory_issues,
            "guard_report": guard_report,
            "export_paths": exec_out["exports"],
            "status": "complete",
            "phase_timings": _phase_timings,
            "run_elapsed_s": result["run_elapsed_s"],
            "iteration_cap": iteration_cap,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
    )
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
