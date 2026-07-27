"""Orchestrator: run the full agent pipeline for a session.

Pipeline:
  1. Specialists in parallel (Lexicon, Schema, Instrument)
  2. Statute (jurisdiction rules)
  3. Judge <-> Sentinel loop (up to ITERATION_CAP)
  4. Human review gate if Sentinel still not approved
  5. Executor applies decisions
  6. Auditor verifies
  7. Scout + Ledger (comparative benchmark) run in parallel
  8. Herald drafts manuscript
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from motor.motor_asyncio import AsyncIOMotorDatabase

from .base import AgentMessage, ITERATION_CAP
from .experts import Statute, Praxis
from .llm import LlmConfig
from .outward import Herald, Ledger, Scout
from .reasoning import Auditor, Executor, Judge, Sentinel, apply_sentinel_hard_rules
from .specialists import Instrument, Lexicon, Schema


PhaseCb = Callable[[str, dict[str, Any]], Awaitable[None]]


async def run_pipeline(
    session: dict[str, Any],
    db: AsyncIOMotorDatabase,
    llm_cfg: LlmConfig,
    emit: Callable[[AgentMessage], Awaitable[None]],
    on_phase: PhaseCb,
) -> dict[str, Any]:
    sid = session["id"]
    files = session.get("files", [])
    dataset_files = [f for f in files if f["kind"] == "dataset"]
    form_files = [f for f in files if f["kind"] == "narrative"]
    dict_files = [f for f in files if f["kind"] == "metadata"]

    common = dict(session_id=sid, llm=llm_cfg, db=db, emit=emit)

    # 1. Specialists in parallel
    await on_phase("specialists", {"agents": ["Lexicon", "Schema", "Instrument"]})
    lex_task = Lexicon(**common).run(dict_files=dict_files) if dict_files else _empty({"columns": []})
    inst_task = Instrument(**common).run(form_files=form_files) if form_files else _empty({"fields": []})
    # Lexicon must complete before Schema so Schema can enrich its prompt
    lexicon = await lex_task
    schema_task = Schema(**common).run(dataset_files=dataset_files, lexicon_columns=lexicon.get("columns", [])) if dataset_files else _empty({"columns": []})
    schema, instrument = await asyncio.gather(schema_task, inst_task)

    # 2. Statute
    await on_phase("statute", {})
    statute = await Statute(**common).run(jurisdiction=session.get("jurisdiction", "us"))

    # 3. Judge <-> Sentinel loop
    judge = Judge(**common)
    sentinel = Sentinel(**common)
    prior_feedback = ""
    approved_decisions: list[dict[str, Any]] = []
    for iteration in range(1, ITERATION_CAP + 1):
        await on_phase(f"judge_iter_{iteration}", {"iteration": iteration})
        j = await judge.run(schema=schema, instrument=instrument, lexicon=lexicon,
                            statute=statute, prior_feedback=prior_feedback)
        decisions = j.get("decisions", [])
        # Sentinel deterministic hard-rules: force known direct identifiers off
        # 'human_review' before invoking the LLM Sentinel. Closes the accuracy
        # gap where Judge routes obvious PHI to human review out of caution.
        decisions, overrides = apply_sentinel_hard_rules(decisions)
        if overrides:
            await on_phase(f"sentinel_hard_rules_iter_{iteration}",
                           {"iteration": iteration, "overrides": overrides})
        await on_phase(f"sentinel_iter_{iteration}", {"iteration": iteration, "decision_count": len(decisions)})
        s = await sentinel.run(decisions=decisions, statute=statute, instrument=instrument)
        if s.get("verdict") == "approved":
            approved_decisions = decisions
            break
        prior_feedback = _summarise_issues(s.get("issues", []))
        approved_decisions = decisions   # keep last set for possible human hand-off

    if not approved_decisions:
        approved_decisions = []
    human_needed = any(d.get("action") == "human_review" for d in approved_decisions)
    if s.get("verdict") != "approved":
        human_needed = True
        await on_phase("human_review_required", {"reason": "sentinel did not approve within cap"})

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
        await db.sessions.update_one(
            {"id": sid},
            {"$set": {"status": "awaiting_human_review", "updated_at": datetime.now(timezone.utc).isoformat()}},
        )
        return {"status": "awaiting_human_review", "decisions": approved_decisions, "sentinel": s}

    # 5. Executor
    await on_phase("executor", {"decision_count": len(approved_decisions)})
    exec_out = await Executor(**common).run(files=files, decisions=approved_decisions)

    # 6. Auditor
    await on_phase("auditor", {})
    audit = await Auditor(**common).run(decisions=approved_decisions, exports=exec_out["exports"], files=files)

    # 7. Scout + Ledger
    await on_phase("scout_ledger", {})
    scout, benchmark = await asyncio.gather(
        Scout(**common).run(),
        _empty(None),   # placeholder for future synthetic benchmark run
    )
    ledger = await Ledger(**common).run(decisions=approved_decisions, audit=audit, scout=scout, benchmark_result=benchmark)

    # 8. Herald
    await on_phase("herald", {})
    herald = await Herald(**common).run(ledger=ledger, audit=audit,
                                        target_venue=session.get("target_venue") or "JAMIA Open")

    result = {
        "status": "complete",
        "decisions": approved_decisions,
        "audit": audit,
        "scout": scout,
        "ledger": ledger,
        "herald": herald,
        "exports": exec_out["exports"],
    }
    await db.sessions.update_one(
        {"id": sid},
        {"$set": {
            "agent_audit": audit,
            "agent_ledger": ledger,
            "agent_herald": herald,
            "agent_scout": scout,
            "export_paths": exec_out["exports"],
            "status": "complete",
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
