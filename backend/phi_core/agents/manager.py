from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from ..control.handoff import INSTRUMENT, JUDGE, LEXICON, SCHEMA, InstrumentQuestion, LexiconQuestion, SchemaQuestion
from ..control.records import ControlRecord, HandoffEnvelope, HandoffResult
from ..security import scrub_persisted_text
from .base import Agent


@dataclass(frozen=True)
class ManagerDecision:
    action: str            # "retry" | "extend_timeout" | "grant_web_search" | "escalate"
    note: Optional[str]


@dataclass(frozen=True)
class ManagerAdvice:
    action: str            # "continue" | "escalate_human_review"
    note: Optional[str]


class ExecutionHealthSupervisor(Agent):
    """The supervising agent. Owns the health of the run, never its content.

      1. run()                     open the run; assign roles and deliverables.
      2. note_phase()              track phase transitions (deterministic).
      3. run_supervised()          when an agent fails, times out, returns
                                   garbage, or under-delivers, decide whether to
                                   retry, grant more time, grant the web-search
                                   tool, or give up -- with a coaching note.
      4. consult()                 answer an agent that is unsure whether to keep
                                   working or hand off to a human.
      5. close_run()                persistable report of every intervention.

    Per D10, this class no longer owns any path to 'awaiting_human_review':
    the deleted escalate_to_human_review() write moved to the shared
    orchestrator._escalate_to_human_review() helper and
    SuperOrchestrator.request_human_review(), the durable authority.

    Wave 4b (docs #87): demoted from an independent peer class
    ``run_pipeline`` called directly for phase sequencing into a
    subordinate ExecutionHealthSupervisor -- ``run_pipeline`` now asks
    ``SuperOrchestrator.advance()`` for sequencing and dispatches through
    a registry; this class supervises execution health FOR that
    authority (retry/extend-timeout/grant-web-search/escalate, plus the
    new handoff-observation responder below), never in place of it. The
    class name changed; its role identity (``NAME = "Manager"``, the
    agent identifier every ``AgentContext``/``AgentManifest``/
    capability-grant/task record still keys on) did not -- renaming
    that string is a separate, much larger cross-cutting change (
    ``control/policy.py``'s ``MANIFESTS``, every persisted record with
    ``agent="Manager"``) out of this wave's scope. ``Manager`` remains
    importable as a compatibility name at the bottom of this module for
    the one caller outside this wave's authority to migrate
    (``server.py``'s human-review-resume path); see that alias's own
    comment.

    It is never given a prompt, a reply, a decision, or a file. Its LLM
    payload is always counts, enums, and timings.

    Exception: the deterministic guardian query broker below
    (``attach_schema``/``ask_schema``, ``attach_instrument``/``ask_instrument``,
    ``attach_lexicon``/``ask_lexicon``) does pass a column or field name
    through, so Judge/Sentinel can ask a specialist a targeted question
    instead of relying only on the broadcast summary. That name and the
    specialist's lookup result never reach this class's own LLM calls --
    ``ask_schema``/``ask_instrument`` never call an LLM at all, and
    ``ask_lexicon`` forwards straight to ``Lexicon.answer`` without this
    class itself reading the reply content.
    """

    NAME = "Manager"

    # Who reports to the Manager and what each owes. Logged as the run charter
    # and shown to the Manager when it supervises, so "the right agent is doing
    # the right task" is an explicit expectation rather than an assumption.
    ROLES = {
        "Lexicon": "reads the data dictionary; returns one entry per documented column",
        "Schema": "reads dataset column HEADERS ONLY; returns one classification per header",
        "Instrument": "reads study form text; returns the PHI fields it collects",
        "RegulationsExpert": "returns the rulebook for the run's jurisdiction",
        "PHIMethodsExpert": "returns the current best-practice technique for one HIPAA category",
        "Judge": "returns exactly one handling decision per dataset column",
        "Sentinel": "reviews Judge's decisions for zero leak; returns issues",
        "Executor": "deterministic; applies approved decisions, makes no LLM call",
        "Operator": "deterministic; self-verifies what Executor wrote against decisions",
        "Reviewer": "deterministic; confirms Operator covered every decision",
        "Auditor": "verifies executor output against decisions; returns metrics",
        "Scout": "returns the competitive landscape",
        "Ledger.Compare": "returns per-competitor delta notes",
        "Ledger.Aggregate": "returns the benchmark rollup",
        "Herald.Abstract": "drafts title, abstract, methods",
        "Herald.Sections": "drafts results, discussion, limitations, conclusion",
    }

    # Soft per-call expectations, seeded from measured warm-cache baselines
    # and rounded up so they do not cry wolf.
    # Advisory only: a slow call that SUCCEEDS is never retried -- retrying it
    # would burn the very wall-clock the budget exists to protect. An overrun is
    # recorded, and is shown to the Manager when that call also fails.
    BUDGET_S = {
        "Judge": 40.0, "Sentinel": 40.0, "Lexicon": 40.0, "Schema": 25.0,
        "Auditor": 25.0, "Scout": 40.0, "Instrument": 40.0,
        "Ledger.Compare": 35.0, "Ledger.Aggregate": 35.0,
        "Herald.Abstract": 75.0, "Herald.Sections": 75.0,
        "RegulationsExpert": 60.0, "PHIMethodsExpert": 60.0,
    }
    DEFAULT_BUDGET_S = 45.0

    PROMPT = (
        "You are Manager, the supervisor of a 12-agent clinical-data handling "
        "pipeline. Each agent is a specialist with one job. Your job is that each "
        "of them finishes their own job, on time, and that the run either moves "
        "forward or reaches a human cleanly.\n\n"
        "How you manage:\n"
        "- Know the role. Every request tells you which agent it is, what that "
        "agent owed, and what it actually delivered. Judge the work against what "
        "was owed, not against what you would have preferred.\n"
        "- Intervene proportionally, least intrusive first. A retry with clearer "
        "instruction is cheaper than more time; more time is cheaper than handing "
        "over a tool; a tool is cheaper than taking a human's attention. Escalate "
        "when the agent is genuinely blocked, not at the first stumble.\n"
        "- Support, do not take over. You give an agent clearer instruction, more "
        "time, or the tool it lacks. You never do its work and never supply its "
        "answer.\n"
        "- Adapt. You are told what has already happened in this run, including "
        "guidance that already fixed the same kind of failure. Reuse what worked; "
        "stop repeating what did not.\n"
        "- Know when to stop. Repeated failure of the same kind means the agent "
        "is blocked, and a blocked run belongs to a human, promptly.\n\n"
        "Hard constraint: you supervise EXECUTION HEALTH ONLY -- attempt counts, "
        "error kinds, elapsed seconds, iteration counts, issue counts, and how "
        "many items were owed versus delivered. You are never given prompt text, "
        "model replies, decisions, column names, file contents, or any patient or "
        "study data. Never ask for them, never guess at them, never refer to them. "
        "Judging the data is your team's job; keeping your team working is yours.\n\n"
        "Each request names the task and the actions legal right now. Choose "
        "exactly one action from legal_actions -- never invent one. The optional "
        "note is short operational coaching for the struggling agent about output "
        "format, strictness, completeness, or pacing (for example: 'return strict "
        "JSON only, one entry per item you were given'); it must never mention "
        "data content.\n\n"
        'Respond with strict JSON only: {"action": "<one of legal_actions>", '
        '"note": "<optional, <=200 chars>"}'
    )

    MAX_ATTEMPTS = 3                    # 1 initial + 2 supervised retries
    BACKOFF_S = {2: 2.0, 3: 5.0}        # sleep before attempt N
    DECISION_TIMEOUT_S = 12.0           # the Manager's own calls stay short
    NOTE_MAX_CHARS = 200
    LEXICON_QUERY_BUDGET = 8             # Lexicon.answer calls an LLM; cap queries/run
    # Wave 4b: repeated denials on the same (sender, recipient) edge within
    # one run's observation window escalate rather than being reported as
    # an ordinary BLOCK forever -- mirrors run_supervised's own "repeated
    # failure of the same kind means the agent is blocked" philosophy
    # (see PROMPT above), applied to handoff denials instead of call
    # failures.
    HANDOFF_DENIAL_ESCALATION_THRESHOLD = 3

    def __init__(self, ctx, *, db=None):
        super().__init__(ctx)
        self._db = db
        self._t0 = time.perf_counter()
        self._phases: list[dict[str, Any]] = []
        self._interventions: list[dict[str, Any]] = []
        self._consults: list[dict[str, Any]] = []
        self._late_calls: list[dict[str, Any]] = []
        self._notes_that_worked: dict[str, str] = {}   # error_kind -> coaching note
        self._escalation: dict[str, Any] | None = None
        self._schema = None
        self._instrument = None
        self._lexicon = None
        self._lexicon_queries = 0
        # Wave 4b: handoff-observation action responder state (see
        # respond_to_handoff/respond_to_handoff_budget below). Both are
        # process-local, run-scoped counters -- SuperOrchestrator.
        # observe_handoff already keeps the durable per-run denial count;
        # these back this class's own supervisory *response*, not a
        # second copy of that durable record.
        self._handoff_denials: dict[tuple[str, str], int] = {}
        self._handoff_budget_denials: dict[str, int] = {}

    # ---- 1. open -------------------------------------------------------
    async def run(self, *, roster: list[str], phase_plan: list[str]) -> dict[str, Any]:
        """Open the run: put every agent's role and time expectation on record.
        Deterministic -- there is nothing to decide yet, so no LLM call."""
        charter = {
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "max_attempts": self.MAX_ATTEMPTS,
            "phase_plan": phase_plan,
            "assignments": [
                {"agent": a,
                 "role": self.ROLES.get(a, "unlisted"),
                 "budget_s": self.BUDGET_S.get(a, self.DEFAULT_BUDGET_S)}
                for a in roster
            ],
        }
        await self._log("manager.charter", "info", charter)
        return charter

    # ---- 2. watch ------------------------------------------------------
    async def note_phase(self, phase: str, elapsed_s: float) -> None:
        """Record a phase transition. In-memory: the orchestrator already emits a
        progress event per phase, so a second agent_log row would only double log
        volume for no new information."""
        self._phases.append({"phase": phase, "elapsed_s": round(elapsed_s, 3)})

    # ---- 3. step in ----------------------------------------------------
    async def run_supervised(
        self, *, agent_name: str, phase: str, base_system_prompt: str,
        primary_attempt: Callable[[str, bool], Awaitable[str]],
        escalated_attempt: Optional[Callable[[str, bool], Awaitable[str]]] = None,
        validate: Optional[Callable[[str], dict[str, Any] | None]] = None,
    ) -> tuple[str, bool, Optional[str]]:
        """Drive up to MAX_ATTEMPTS attempts on one agent's LLM call.

        `primary_attempt` / `escalated_attempt` are closures owned by the calling
        Agent method; each takes (system_prompt, extended) and returns reply text,
        raising asyncio.TimeoutError or any Exception on failure. They alone hold
        the prompt and the timeout arithmetic. `validate` returns None when the
        reply is acceptable, else {"kind": ..., plus integer counts}.

        Returns (reply, ok, final_error_kind).
        """
        budget = self.BUDGET_S.get(agent_name, self.DEFAULT_BUDGET_S)
        attempt_fn = primary_attempt
        extended = False
        tool_granted = False
        guidance = ""
        note_in_play: Optional[str] = None
        last_error_kind: Optional[str] = None
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            system_prompt = base_system_prompt
            if guidance:
                system_prompt += f"\n\n[Manager operational note] {guidance}"
            failure: dict[str, Any] | None = None
            reply = ""
            t0 = time.perf_counter()
            try:
                reply = await attempt_fn(system_prompt, extended)
            except asyncio.TimeoutError:
                failure = {"kind": "timeout"}
            except Exception as exc:
                failure = {"kind": f"exception:{type(exc).__name__}"}
            else:
                if not reply.strip():
                    failure = {"kind": "empty_reply"}
                elif validate is not None:
                    failure = validate(reply)
            attempt_s = round(time.perf_counter() - t0, 3)

            if failure is None:
                if attempt_s > budget:
                    late = {"agent": agent_name, "phase": phase,
                            "attempt_s": attempt_s, "budget_s": budget}
                    self._late_calls.append(late)
                    await self._log(f"manager.late.{agent_name}", "info", late)
                if attempt > 1:
                    self._interventions.append(
                        {"agent": agent_name, "phase": phase, "attempt": attempt,
                         "action": "recovered"})
                    await self._log(f"manager.recovered.{agent_name}", "info",
                                    {"phase": phase, "attempt": attempt})
                    if note_in_play and last_error_kind:
                        # Remember coaching that actually worked, so a later
                        # agent hitting the same failure is helped immediately.
                        self._notes_that_worked[last_error_kind] = note_in_play
                return reply, True, None

            error_kind = failure["kind"]
            last_error_kind = error_kind
            counts = {k: v for k, v in failure.items() if k != "kind"}
            record = {"agent": agent_name, "phase": phase, "attempt": attempt,
                      "error_kind": error_kind, "attempt_s": attempt_s,
                      "over_budget": attempt_s > budget, **counts}

            if attempt >= self.MAX_ATTEMPTS:
                record["action"] = "escalate"
                record["reason"] = "attempts_exhausted"
                self._interventions.append(record)
                await self._log(f"manager.supervise.{agent_name}", "info", record)
                return "", False, error_kind

            legal = {"retry", "escalate"}
            if not extended:
                legal.add("extend_timeout")
            if escalated_attempt is not None and not tool_granted:
                legal.add("grant_web_search")

            remembered = self._notes_that_worked.get(error_kind)
            decision = await self._decide(
                task="supervise", legal=legal, default_action="escalate",
                payload={
                    "agent": agent_name,
                    "agent_role": self.ROLES.get(agent_name, "unlisted"),
                    "phase": phase, "attempt": attempt,
                    "max_attempts": self.MAX_ATTEMPTS,
                    "error_kind": error_kind,
                    "attempt_seconds": attempt_s,
                    "budget_seconds": budget,
                    "over_budget": attempt_s > budget,
                    "tool_already_granted": tool_granted,
                    "timeout_already_extended": extended,
                    "run_history": self._history_digest(),
                    "note_that_worked_earlier": remembered,
                    **counts,
                })

            record["action"] = decision.action
            record["note"] = decision.note
            self._interventions.append(record)
            await self._log(f"manager.supervise.{agent_name}", "info", record)

            if decision.action == "escalate":
                return "", False, error_kind
            if decision.action == "extend_timeout":
                extended = True
            elif decision.action == "grant_web_search" and escalated_attempt is not None:
                tool_granted = True
                attempt_fn = escalated_attempt
            # Adaptation: the Manager's own note wins; otherwise fall back to
            # coaching that already resolved this same error kind in this run.
            note_in_play = decision.note or remembered
            if note_in_play:
                guidance = note_in_play
            await asyncio.sleep(self.BACKOFF_S.get(attempt + 1, 5.0))

        return "", False, "attempts_exhausted"   # loop always returns above

    # ---- 4. advise -----------------------------------------------------
    async def consult(self, *, agent_name: str, phase: str,
                      signal: dict[str, int | float | str]) -> ManagerAdvice:
        """Answer an agent unsure whether to keep working or hand off.

        `signal` carries counts / scores / enums only -- never prompt, reply, or
        decision content.

        Fails OPEN to 'continue': a consult is a wall-clock optimisation, never a
        safety gate, so an unreachable Manager must not change pipeline behaviour.
        The deterministic post-loop checks remain the real guarantee.
        """
        legal = {"continue", "escalate_human_review"}
        decision = await self._decide(
            task="consult", legal=legal, default_action="continue",
            payload={"agent": agent_name,
                     "agent_role": self.ROLES.get(agent_name, "unlisted"),
                     "phase": phase, "run_history": self._history_digest(), **signal})
        record = {"agent": agent_name, "phase": phase, **signal,
                  "action": decision.action, "note": decision.note}
        self._consults.append(record)
        await self._log(f"manager.consult.{agent_name}", "info", record)
        return ManagerAdvice(action=decision.action, note=decision.note)

    # ---- 5. observe handoffs ---------------------------------------------
    # Wave 4b (docs #9/#10/#87): the handoff-observation action responder.
    # ``SuperOrchestrator.observe_handoff`` already records the durable,
    # per-run denial count (Wave 4a); this is the *response* half -- this
    # class watches the same ``HandoffResult`` a caller reports and picks
    # one of section 10's nine actions (ALLOW, BLOCK, PAUSE, CANCEL,
    # LIMIT, REDIRECT, RETRY, ESCALATE, INVALIDATE).
    #
    # Only four of the nine have a clear, testable trigger derivable from
    # a single observed ``HandoffResult`` plus this class's own run-scoped
    # denial counters -- implemented in full below. The remaining five
    # would require context this method does not have (PAUSE: whether a
    # human should be looped in; REDIRECT: which alternate recipient the
    # original request actually intended; RETRY: every one of
    # ``HandoffGateway``'s checks 1-10 is deterministic given the same
    # envelope, so retrying it unchanged can never succeed; INVALIDATE:
    # which artifact_id a denial reason maps to, a different domain
    # entirely (``ArtifactService.invalidate_descendants``)) -- inventing
    # a trigger for any of them would be exactly the "a fixed escalation
    # policy tied to a rule that has never fired against real data"
    # Wave 4a's own ``observe_handoff`` docstring already declined to do.
    # Forward-compatible hooks: the action vocabulary this method's
    # return type carries already includes them, so a later phase can
    # wire a real trigger without changing this method's contract.

    async def respond_to_handoff(self, *, result: HandoffResult) -> str:
        """One of ALLOW / BLOCK / CANCEL / ESCALATE, chosen from ``result``
        and this run's own denial history on ``(result.sender,
        result.recipient)``.

        - ALLOW: the gateway already let it through -- no supervisory
          reaction needed.
        - CANCEL: ``residual_phi_detected``/``secret_detected`` is a
          genuine leak signal, not a shape/policy mismatch a retry or a
          plain block addresses -- severe enough to stop the attempting
          task rather than merely record the denial.
        - ESCALATE: this exact edge has now been denied
          ``HANDOFF_DENIAL_ESCALATION_THRESHOLD`` times in this run --
          "repeated failure of the same kind means the agent is
          blocked" (the same philosophy ``run_supervised`` already
          applies to call failures), so a human should see it rather
          than the run silently re-attempting forever.
        - BLOCK: every other denial -- the deterministic gateway's own
          verdict stands; this class never tries to force a denied
          handoff through (section 10: "Manager intelligence does not
          replace deterministic policy").
        """
        if result.allowed:
            return "ALLOW"
        if result.reason_code in ("residual_phi_detected", "secret_detected"):
            return "CANCEL"
        edge = (result.sender, result.recipient)
        self._handoff_denials[edge] = self._handoff_denials.get(edge, 0) + 1
        if self._handoff_denials[edge] >= self.HANDOFF_DENIAL_ESCALATION_THRESHOLD:
            return "ESCALATE"
        return "BLOCK"

    async def respond_to_handoff_budget(self, *, category: str) -> str:
        """LIMIT: the response to a ``HandoffGateway`` retry/correction
        budget refusal (check 11, ``BudgetExceeded`` -- a distinct
        observation channel from ``respond_to_handoff`` above, since a
        budget refusal never produces a ``HandoffResult`` at all; the
        gateway raises instead of returning). Unlike a plain denial,
        exhausting a budget has exactly one clear supervisory meaning:
        this edge has hit its ceiling for this run."""
        self._handoff_budget_denials[category] = self._handoff_budget_denials.get(category, 0) + 1
        return "LIMIT"


    # ---- guardian query broker -----------------------------------------
    # Deterministic lookups a specialist already indexed during its own
    # run. The Manager holds the only reference for querying purposes so
    # Judge/Sentinel ask through one place, and every query is logged the
    # same way regardless of which specialist answered.
    #
    # Wave R-c Step 6: each successful query is additionally recorded as
    # a Judge -> specialist handoff attempt through ``ctx.handoff``, the
    # only broker topology ``HandoffGateway.ALLOWED_EDGES`` registers
    # for these three edges (``requesting_agent`` above stays a free-
    # text logging field only -- it is never what the handoff envelope
    # names as sender). See ``_record_handoff`` below for why a denied
    # handoff never blocks the broker's own already-working answer.

    def attach_schema(self, schema) -> None:
        self._schema = schema

    def attach_instrument(self, instrument) -> None:
        self._instrument = instrument

    def attach_lexicon(self, lexicon) -> None:
        self._lexicon = lexicon

    async def _record_handoff(self, recipient: str, payload: ControlRecord) -> None:
        """Fire-and-record one Judge -> specialist handoff attempt through
        ``ctx.handoff`` (Wave R-c Step 6), when this context carries the
        facade. ``HandoffGateway.handoff`` is a validating audit rail
        alongside this broker's already-authorized in-process relay --
        its ``allowed``/``denied`` verdict is written to the trace store
        but never gates ``ask_schema``/``ask_instrument``/``ask_lexicon``'s
        own return value, so a future policy tightening on the handoff
        edge cannot silently break the guardian query broker this wave
        did not otherwise change. ``ctx.handoff is None`` (every pre-
        existing unit test built via ``control.testing.make_ctx``) is a
        no-op, matching every other optional facade guard in this class
        (``ctx.cache``, ``ctx.artifacts``, ...)."""
        if self.ctx.handoff is None:
            return
        await self.ctx.handoff.handoff(HandoffEnvelope(
            run_id=self.ctx.run_id, sender=JUDGE, recipient=recipient,
            data_class="restricted_metadata", payload=payload.model_dump(),
        ))

    async def ask_schema(self, requesting_agent: str, column: str,
                          file_id: str | None = None) -> dict[str, Any]:
        if self._schema is None:
            return {"available": False, "reason": "schema_not_attached"}
        result = self._schema.verify(column, file_id)
        await self._record_handoff(SCHEMA, SchemaQuestion(column=column, file_id=file_id or ""))
        await self._log("manager.ask_schema", "info",
                        {"requesting_agent": requesting_agent,
                         "present": result.get("present")})
        return {"available": True, **result}

    async def ask_instrument(self, requesting_agent: str, field_or_variable: str,
                              file_id: str | None = None) -> dict[str, Any]:
        if self._instrument is None:
            return {"available": False, "reason": "instrument_not_attached"}
        result = self._instrument.verify(field_or_variable, file_id)
        await self._record_handoff(
            INSTRUMENT, InstrumentQuestion(field_or_variable=field_or_variable, file_id=file_id or ""),
        )
        await self._log("manager.ask_instrument", "info",
                        {"requesting_agent": requesting_agent,
                         "present": result.get("present")})
        return {"available": True, **result}

    async def ask_lexicon(self, requesting_agent: str, column: str,
                           assumption: str, reasoning: str) -> dict[str, Any]:
        if self._lexicon is None:
            return {"available": False, "reason": "lexicon_not_attached"}
        if self._lexicon_queries >= self.LEXICON_QUERY_BUDGET:
            return {"available": False, "reason": "budget_exhausted"}
        self._lexicon_queries += 1
        result = await self._lexicon.answer(column, assumption, reasoning)
        await self._record_handoff(
            LEXICON, LexiconQuestion(column=column, assumption=assumption, reasoning=reasoning),
        )
        await self._log("manager.ask_lexicon", "info",
                        {"requesting_agent": requesting_agent,
                         "verdict": result.get("verdict"),
                         "queries_used": self._lexicon_queries})
        return {"available": True, **result}

    # ---- 6. close ------------------------------------------------------
    async def close_run(self, outcome: str) -> dict[str, Any]:
        """Deterministic run report. Called once per settled exit."""
        real = [i for i in self._interventions if i.get("action") != "recovered"]
        per_agent: dict[str, int] = {}
        for i in real:
            per_agent[i["agent"]] = per_agent.get(i["agent"], 0) + 1
        report = {
            "outcome": outcome,
            "run_elapsed_s": round(time.perf_counter() - self._t0, 3),
            "phases_seen": len(self._phases),
            "intervention_count": len(real),
            "recovered_count": len(self._interventions) - len(real),
            "off_task_count": len([i for i in real if i.get("error_kind") == "off_task"]),
            "late_call_count": len(self._late_calls),
            "interventions_by_agent": per_agent,
            "interventions": self._interventions[:50],
            "late_calls": self._late_calls[:20],
            "consults": self._consults[:20],
            "coaching_reused": self._notes_that_worked,
            "escalation": self._escalation,
        }
        await self._log("manager.closeout", "info", report)
        return report

    # ---- internals -----------------------------------------------------
    def _history_digest(self) -> dict[str, Any]:
        """Compact, content-free summary of this run so far, so each decision is
        informed by the ones before it."""
        real = [i for i in self._interventions if i.get("action") != "recovered"]
        by_kind: dict[str, int] = {}
        for i in real:
            k = str(i.get("error_kind", "unknown"))
            by_kind[k] = by_kind.get(k, 0) + 1
        return {"interventions_so_far": len(real),
                "recovered_so_far": len(self._interventions) - len(real),
                "failures_by_kind": by_kind,
                "late_calls_so_far": len(self._late_calls)}

    async def _decide(self, *, task: str, legal: set[str], default_action: str,
                      payload: dict[str, Any]) -> ManagerDecision:
        """One short, strictly bounded LLM call. Any failure, any unparseable
        reply, or any action outside `legal` collapses to `default_action`."""
        body = json.dumps({"task": task, "legal_actions": sorted(legal), **payload},
                          default=str)
        try:
            parsed = await self.call_json(body, phase=f"manager.{task}", default={},
                                          timeout_s=self.DECISION_TIMEOUT_S,
                                          status_text=f"Manager checking in on {task}")
        except Exception:
            parsed = {}
        action = parsed.get("action") if isinstance(parsed, dict) else None
        note = parsed.get("note") if isinstance(parsed, dict) else None
        if action not in legal:
            action = default_action
        if isinstance(note, str) and note.strip():
            note = scrub_persisted_text(note.strip())[: self.NOTE_MAX_CHARS]
        else:
            note = None
        return ManagerDecision(action=action, note=note)


# Wave 4b compatibility name, partially resolved by the orchestrator after
# landing: `server.py`'s independent human-review-resume path now imports
# and constructs `ExecutionHealthSupervisor` directly (it no longer needs
# this alias). The alias remains load-bearing for three lower-risk
# consumers, none of which construct production runtime behavior through
# it: `phi_core/agents/__init__.py`'s public `from .manager import
# Manager` re-export, `phi_core/agents/base.py`'s `TYPE_CHECKING`-only
# reference, and three test files
# (`test_architecture_boundaries.py`, `test_control_phaseR_integration.py`,
# `test_manager.py`) that import the name `Manager` directly. A full
# rename across those is a genuine, low-risk-but-multi-file cleanup,
# appropriately scoped to Phase 17's whole-repo `cleanup-audit`, not an ad
# hoc patch here.
Manager = ExecutionHealthSupervisor
