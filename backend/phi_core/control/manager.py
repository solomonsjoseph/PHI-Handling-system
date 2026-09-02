"""D9 sequencing (:class:`Manager`) and agent-facing execution supervision
(:class:`ManagerSupervision`), co-located per rewrite plan step 7.

These are two fully independent classes, each a faithful, behavior-preserving
rename of a pre-existing, separately-tested class -- neither wraps or
delegates to the other:

- :class:`Manager` is renamed from ``SuperOrchestrator``
  (``control/superorchestrator.py``, deleted): "SuperOrchestrator becomes
  Manager, one entity with one name that sequences the run" (decision 1).
  Constructed ``Manager(store, tasks)`` at every call site (server.py's 11
  lifecycle routes, orchestrator.py's own driver code) -- exactly
  ``SuperOrchestrator``'s original two-argument constructor, unchanged.
  Holds every D9 sequencing method below (``start_run``, ``advance``,
  ``create_child_work``, ``request_human_review``, ``recover``, ...) --
  exactly what ``SuperOrchestrator`` always was, minus the ten methods with
  zero production caller the rewrite plan names for deletion (``resume``,
  ``dependencies_satisfied``, ``observe_handoff``,
  ``require_artifacts_current``, ``evaluate_handoff_budget``,
  ``route_budget_exceeded``, ``authorize_execution``, ``begin_export``,
  ``confirm_export``, ``authorize_publication``).
- :class:`ManagerSupervision` is renamed from ``ExecutionHealthSupervisor``
  (``agents/manager.py``, deleted): the agent-facing execution-health
  supervisor plus the deterministic guardian query broker
  (``attach_schema``/``ask_schema``, ``attach_instrument``/
  ``ask_instrument``, ``attach_lexicon``/``ask_lexicon``). Constructed
  ``ManagerSupervision(ctx, *, db=None)`` -- exactly
  ``ExecutionHealthSupervisor``'s original constructor -- and is the ONLY
  thing ``AgentContext.manager`` now types to: agents reaching it via
  ``ctx.manager`` structurally cannot reach ``advance()``,
  ``create_child_work()``, or any other D9 state write, since
  ``ManagerSupervision`` holds no ``store`` and no ``TaskService``.
  orchestrator.py's existing call sites (``state.manager.consult(...)``,
  ``.close_run(...)``, ``.run(...)``, ``.attach_schema(...)``) are
  unchanged: ``state.manager`` was always this class, under its old name.

Moving both into one file satisfies "put ManagerSupervision in
control/manager.py" (rewrite plan step 7, Ruling 14) without inventing any
new coupling between the two classes' behavior.

``Manager``'s exclusive authority, mutually exclusive with every other
collaborator:

- ``Manager`` is the only writer of ``workflow_runs.state`` and
  ``workflow_runs.node``, the only caller of ``TaskService.enqueue``
  (through ``create_child_work``), the only issuer of a human-review
  request, the only consumer of a human-review event, and the only
  acceptor of a material child result.
- ``TaskService`` (``control/tasks.py``) separately owns every
  ``work_items`` state and lease transition -- this class never CAS-writes
  ``work_items`` directly, including for acceptance: a task reaching
  ``succeeded`` is infrastructure-level completion, not the material
  acceptance ``accept_result`` alone decides, and that decision is
  recorded onto the *run's* checkpoint, never onto the task.
- ``run_decision_gates`` (D11, ``control/gates.py``) separately owns
  ``workflow_runs.decision_version``.
- ``ArtifactService`` (D14, ``control/artifacts.py``) separately owns
  every ``artifacts``/``publication_pointers`` transition.

Caller-supplies-the-typed-result convention: D9's published method
signatures list bare identifiers (``run_id``, ``task_id``, ...) for every
method, but two of them -- ``advance`` and ``request_human_review`` --
need a value this class cannot safely re-derive from persisted state
alone (which outcome a node reached; the human-review request's
originating task) without inventing a bespoke, unverifiable rule per
call site. Each accepts one additional keyword argument here, defaulted
so every other call keeps D9's exact arity. This mirrors every other
typed-result boundary already in this codebase (``TaskService.complete``/
``.fail``, ``ArtifactService.certify_publication``): the caller reports
its own typed result; the callee validates, records, and never
re-guesses it.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional
from uuid import uuid4

from ..agents.base import Agent
from ..security import scrub_persisted_text
from . import limits
from .artifacts import MANIFEST_COLLECTION
from .context import StoreTraceWriter
from .handoff import INSTRUMENT, JUDGE, LEXICON, SCHEMA, InstrumentQuestion, LexiconQuestion, SchemaQuestion
from .policy import MANIFESTS, POLICY_VERSION, CapabilityDenied, _bounded_budget
from .records import (
    CapabilityGrant,
    CleanupManifest,
    ControlRecord,
    HandoffEnvelope,
    HandoffResult,
    HumanReviewEvent,
    HumanReviewRequest,
    ResourceBudget,
    VerifiedClassificationManifest,
    WorkflowRun,
    WorkItem,
)
from .runs import check_run_budget
from .store import ControlStore
from .tasks import TaskService
from .workflow import (
    CHECKPOINT_VERSION,
    NON_TERMINAL_NODES,
    Checkpoint,
    WorkflowError,
    is_terminal,
    next_node,
    resume_node,
)
from .workflow import (
    node as validate_node,
)

WORKFLOW_VERSION = "wf/1"

# Every RunState terminal value is spelled identically to the matching
# TERMINAL_NODES value (both D3 and D9 use "complete", "partially_complete",
# "blocked", "failed", "cancelled" verbatim), so a terminal node's name is
# always a valid RunState too -- advance() relies on this rather than
# maintaining a second node->state translation table.

# WorkItem states cancel_subtree/reconcile_leases already treat as
# terminal (mirrors tasks.py's own _TERMINAL_STATES). A live sibling or
# run-wide task count only counts non-terminal work.
_TERMINAL_TASK_STATES = frozenset({"succeeded", "failed", "cancelled", "rejected", "superseded"})

# ---- ManagerSupervision's own constants (module-level so the class body
# stays literal-free; assigned as class attributes below so
# `self.ROLES`/`self.BUDGET_S` access matches every ported method body
# unchanged). ----

# Who reports to the Manager and what each owes. Logged as the run charter
# and shown to the Manager when it supervises, so "the right agent is doing
# the right task" is an explicit expectation rather than an assumption.
_ROLES = {
    "Lexicon": "reads the data dictionary; returns one entry per documented column",
    "Schema": "reads dataset column HEADERS ONLY; returns one classification per header",
    "Instrument": "reads study form text; returns the PHI fields it collects",
    "RegulationsExpert": "returns the rulebook for the run's jurisdiction",
    "PHIMethodsExpert": "returns the current best-practice technique for one HIPAA category",
    "Judge": "returns exactly one handling decision per dataset column",
    "Executor": "deterministic; applies approved decisions, makes no LLM call",
    "Operator": "deterministic; self-verifies what Executor wrote against decisions",
    "Reviewer": "PREVIEW: challenges Judge's decisions before execution, zero leak/100% "
                "accuracy; FINAL: confirms Operator covered every decision",
}

# Phase 17-B: Auditor (LLM re-derivation role) is retired; Reviewer's
# FINAL mode is the sole post-execution safety net now. Scout, Ledger,
# and Herald moved out of the core PHI path into an opt-in post-run
# report (``outward.run_post_run_report``) and are no longer part of
# this manager's per-run charter/budget bookkeeping.

# Soft per-call expectations, seeded from measured warm-cache baselines
# and rounded up so they do not cry wolf.
# Advisory only: a slow call that SUCCEEDS is never retried -- retrying it
# would burn the very wall-clock the budget exists to protect. An overrun is
# recorded, and is shown to the Manager when that call also fails.
_BUDGET_S = {
    "Judge": 40.0, "Reviewer": 40.0, "Lexicon": 40.0, "Schema": 25.0,
    "Instrument": 40.0,
    "RegulationsExpert": 60.0, "PHIMethodsExpert": 60.0,
}
_DEFAULT_BUDGET_S = 45.0
_MAX_ATTEMPTS = 3                    # 1 initial + 2 supervised retries


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _worker_for_task_type(task_type: str) -> str:
    """The one manifest whose ``task_types`` grants ``task_type``.

    ``create_child_work`` receives only ``task_type`` (D9's exact
    signature has no ``worker`` parameter), but ``TaskService.enqueue``
    needs the agent name to issue a capability grant. This is the single
    reverse lookup over the same ``MANIFESTS`` mapping the forward
    direction (``CapabilityPolicy._manifest_for``) already validates
    against, so there is exactly one source of truth for the mapping, not
    two that could drift apart.
    """
    for agent, manifest in MANIFESTS.items():
        if task_type in manifest.task_types:
            return agent
    raise CapabilityDenied(f"task type {task_type!r} is not granted to any agent")


def _budget_widens(requested: ResourceBudget, ceiling: ResourceBudget) -> bool:
    """True if ``requested`` asks for more than ``ceiling`` on any field.

    D5: a caller may narrow a child's proposed budget, never widen it
    past what the target agent's own manifest (bounded by the global
    ceilings) already authorizes.
    """
    fields = set(ResourceBudget.model_fields) - {"schema_version"}
    return any(getattr(requested, field) > getattr(ceiling, field) for field in fields)


@dataclass(frozen=True)
class ManagerDecision:
    action: str            # "retry" | "extend_timeout" | "grant_web_search" | "escalate"
    note: Optional[str]


@dataclass(frozen=True)
class ManagerAdvice:
    action: str            # "continue" | "escalate_human_review"
    note: Optional[str]


class ManagerSupervision(Agent):
    """The narrow, agent-facing supervision surface -- ``AgentContext.
    manager``'s type. Owns the health of the run, never its content.
    Holds no ``store``, no ``TaskService``: an agent reaching this via
    ``ctx.manager`` cannot reach ``advance()``, ``create_child_work()``,
    or any other durable state write.

      1. run()                     open the run; assign roles and deliverables.
      2. note_phase()              track phase transitions (deterministic).
      3. run_supervised()          when an agent fails, times out, returns
                                    garbage, or under-delivers, decide whether to
                                    retry, grant more time, grant the web-search
                                    tool, or give up -- with a coaching note.
      4. consult()                 answer an agent that is unsure whether to keep
                                    working or hand off to a human.
      5. close_run()                persistable report of every intervention.

    Per D10, this class owns no path to 'awaiting_human_review': that
    write is the shared ``orchestrator._escalate_to_human_review()``
    helper's and ``Manager.request_human_review()``'s, the durable
    authority.

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
    ROLES = _ROLES
    BUDGET_S = _BUDGET_S
    DEFAULT_BUDGET_S = _DEFAULT_BUDGET_S
    MAX_ATTEMPTS = _MAX_ATTEMPTS

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

    BACKOFF_S = {2: 2.0, 3: 5.0}        # sleep before attempt N
    DECISION_TIMEOUT_S = 12.0           # the Manager's own calls stay short
    NOTE_MAX_CHARS = 200
    # Wave 4b: repeated denials on the same (sender, recipient) edge within
    # one run's observation window escalate rather than being reported as
    # an ordinary BLOCK forever -- mirrors run_supervised's own "repeated
    # failure of the same kind means the agent is blocked" philosophy
    # (see PROMPT above), applied to handoff denials instead of call
    # failures.
    HANDOFF_DENIAL_ESCALATION_THRESHOLD = 3
    LEXICON_QUERY_BUDGET = 8             # Lexicon.answer calls an LLM; cap queries/run

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
        self._handoff_denials: dict[tuple[str, str], int] = {}
        self._handoff_budget_denials: dict[str, int] = {}
        self._schema = None
        self._instrument = None
        self._lexicon = None
        self._lexicon_queries = 0

    # ---- open -----------------------------------------------------------
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

    # ---- watch ------------------------------------------------------
    async def note_phase(self, phase: str, elapsed_s: float) -> None:
        """Record a phase transition. In-memory: the orchestrator already emits a
        progress event per phase, so a second agent_log row would only double log
        volume for no new information."""
        self._phases.append({"phase": phase, "elapsed_s": round(elapsed_s, 3)})

    # ---- step in ----------------------------------------------------
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

    # ---- advise -----------------------------------------------------
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

    # ---- observe handoffs ---------------------------------------------
    # Wave 4b (docs #9/#10/#87): the handoff-observation action responder.
    # ``Manager.observe_handoff`` (retired step 7: zero production
    # caller) would have already recorded the durable, per-run denial
    # count; this is the *response* half -- this class watches the same
    # ``HandoffResult`` a caller reports and picks one of section 10's
    # nine actions (ALLOW, BLOCK, PAUSE, CANCEL, LIMIT, REDIRECT, RETRY,
    # ESCALATE, INVALIDATE).
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
    # Wave 4a's own (now-retired) ``observe_handoff`` docstring already
    # declined to do. Forward-compatible hooks: the action vocabulary
    # this method's return type carries already includes them, so a
    # later phase can wire a real trigger without changing this method's
    # contract.

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

    # ---- close ------------------------------------------------------
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


class Manager:
    """D9 sequencing authority -- exactly ``SuperOrchestrator``'s original
    scope, renamed (see module docstring). Constructed
    ``Manager(store, tasks)`` at every call site; this class has no
    inheritance from ``Agent`` and no supervision surface -- that is
    ``ManagerSupervision``'s exclusive domain, a fully separate class in
    this same module.
    """

    def __init__(self, store: ControlStore, tasks: TaskService) -> None:
        self._store = store
        self._tasks = tasks

    # ---- shared loaders ---------------------------------------------------

    async def _load_run(self, run_id: str) -> WorkflowRun:
        document = await self._store.get_one("workflow_runs", {"run_id": run_id})
        if document is None:
            raise WorkflowError(f"unknown run_id: {run_id!r}")
        return WorkflowRun.model_validate(document)

    async def _load_task(self, task_id: str) -> WorkItem:
        document = await self._store.get_one("work_items", {"task_id": task_id})
        if document is None:
            raise WorkflowError(f"unknown task_id: {task_id!r}")
        return WorkItem.model_validate(document)

    # ---- start_run ----------------------------------------------------

    async def start_run(
        self,
        *,
        session_id: str,
        principal: str,
        run_type: str = "study",
        iteration_cap: int = 0,
        correlation_id: str = "",
        run_id: str | None = None,
        root_task_type: str = "pipeline_run",
    ) -> WorkflowRun:
        """Open a new ``WorkflowRun`` at the ``charter`` node and enqueue its
        durable root ``Pipeline`` task.

        Fails closed on an anonymous caller. A route that atomically claims
        a session before starting work may pass that claim's fresh
        ``run_id`` so its session fence, ``WorkflowRun``, and root task
        share one identity. ``root_task_type`` permits the same durable
        Pipeline authority to schedule a fresh run or a human-review resume.
        Other callers omit both and receive a minted id and
        ``"pipeline_run"`` root. This method remains the sole
        `TaskService.enqueue` caller for root work.
        """
        if not principal:
            raise CapabilityDenied("start_run requires an authenticated principal")
        if iteration_cap < 0:
            raise ValueError("iteration_cap must not be negative")
        run_id = run_id or uuid4().hex
        existing = await self._store.get_one("workflow_runs", {"run_id": run_id})
        if existing is None:
            now = _now()
            run = WorkflowRun(
                run_id=run_id,
                session_id=session_id,
                workflow_version=WORKFLOW_VERSION,
                policy_version=POLICY_VERSION,
                run_type=run_type,
                state="running",
                node="charter",
                checkpoint={"node": "charter", "checkpoint_version": CHECKPOINT_VERSION, "payload_refs": []},
                checkpoint_version=CHECKPOINT_VERSION,
                started_at=now,
                updated_at=now,
                correlation_id=correlation_id or run_id,
                budget=ResourceBudget(
                    wall_seconds=limits.MAX_RUN_WALL_S, max_tokens=limits.MAX_TOKENS_PER_RUN,
                    max_cost_usd=limits.MAX_COST_PER_RUN_USD, max_tool_calls=limits.MAX_TOOL_CALLS_PER_RUN,
                    max_artifact_bytes=limits.MAX_ARTIFACT_BYTES_PER_RUN,
                ),
            )
            await self._store.insert("workflow_runs", run)
        else:
            run = WorkflowRun.model_validate(existing)
            if run.session_id != session_id:
                raise WorkflowError(f"run_id {run_id!r} does not belong to session {session_id!r}")
        await self._tasks.enqueue(
            run_id=run_id,
            session_id=session_id,
            worker="Pipeline",
            task_type=root_task_type,
            input_ref={"run_type": run_type},
            correlation_id=correlation_id or run_id,
        )
        return run

    # ---- cancel_run -----------------------------------------------------

    async def cancel_run(self, *, session_id: str, run_id: str, principal: str, reason: str) -> WorkflowRun:
        """Idempotently request cancellation.

        Sets ``cancel_requested``/``cancel_requested_at`` under CAS rather
        than forcing an immediate terminal transition: only the running
        handler -- which alone knows whether it is between two phases or
        mid-provider-call -- can safely land on the ``cancelled`` terminal
        node via a later ``advance(outcome="cancelled")``. A run already
        terminal, or already cancel-requested, is returned unchanged.
        """
        if not principal:
            raise CapabilityDenied("cancel_run requires an authenticated principal")
        run = await self._load_run(run_id)
        if run.session_id != session_id:
            raise WorkflowError(f"run_id {run_id!r} does not belong to session {session_id!r}")
        if is_terminal(run.node) or run.cancel_requested:
            return run
        updated = run.model_copy(
            update={
                "cancel_requested": True,
                "cancel_requested_at": _now(),
                "updated_at": _now(),
            }
        )
        matched = await self._store.compare_and_set(
            "workflow_runs", {"run_id": run_id}, {"updated_at": run.updated_at, "cancel_requested": False}, updated
        )
        if not matched:
            raise WorkflowError(f"lost the race requesting cancellation for run_id={run_id!r}")
        for task in await self._store.find_many("work_items", {"run_id": run_id}):
            if task.get("parent_task_id") == "":
                await self._tasks.cancel_subtree(run_id=run_id, task_id=task["task_id"], reason=reason)
        return updated

    # ---- advance --------------------------------------------------------

    async def advance(self, *, run_id: str, outcome: str) -> WorkflowRun:
        """Transition ``run_id`` from its current node on ``outcome``.

        ``outcome`` is supplied by the caller -- the pipeline handler that
        just observed what actually happened (a ``GateOutcome``, a
        ``HumanReviewEvent.kind``, an accepted/rejected child result).
        ``next_node`` is the sole authority translating a reported outcome
        into the next node: it fails closed (``WorkflowError``) on any
        ``(node, outcome)`` pair it does not recognise, so a caller cannot
        invent an outcome that skips, reorders, or disables a gate. Every
        node commits a checkpoint in the same update (D9); a terminal
        target also stamps ``state``/``terminal_outcome``/``completed_at``,
        since every terminal node name is spelled identically to its
        matching ``RunState`` value.
        """
        run = await self._load_run(run_id)
        if is_terminal(run.node):
            raise WorkflowError(f"run_id={run_id!r} is already terminal at node {run.node!r}")
        target = next_node(run.node, outcome)
        now = _now()
        updates: dict[str, Any] = {
            "node": target,
            "checkpoint": {"node": target, "checkpoint_version": CHECKPOINT_VERSION, "payload_refs": []},
            "checkpoint_version": CHECKPOINT_VERSION,
            "updated_at": now,
        }
        if is_terminal(target):
            updates["state"] = target
            updates["terminal_outcome"] = target
            updates["completed_at"] = now
        updated = run.model_copy(update=updates)
        matched = await self._store.compare_and_set(
            "workflow_runs", {"run_id": run_id}, {"updated_at": run.updated_at, "node": run.node}, updated
        )
        if not matched:
            raise WorkflowError(f"lost the race advancing run_id={run_id!r} from node {run.node!r}")
        return updated

    # ---- create_child_work ------------------------------------------------

    async def create_child_work(
        self,
        *,
        run_id: str,
        parent_task_id: str,
        task_type: str,
        input_ref: dict[str, Any],
        budget: ResourceBudget,
    ) -> WorkItem:
        """Validate depth, fanout, run-wide task count, run-wide and
        per-parent parallelism, and budget against the parent's grant,
        then delegate to ``TaskService.enqueue``.

        Fails closed (``CapabilityDenied``) on: an unknown parent task or
        grant; a ``task_type`` the parent's manifest does not list under
        ``allowed_child_task_types``; a depth or live-sibling count past
        the manifest's (or the global D5) ceiling; the run reaching
        ``limits.MAX_TASKS_PER_RUN`` total tasks ever created; the run or
        the immediate parent reaching its non-terminal task ceiling
        (``limits.MAX_PARALLEL_TASKS_PER_RUN``/``MAX_PARALLEL_TASKS_PER_PARENT``);
        or a proposed ``budget`` that asks for more than the target
        agent's own manifest, bounded by the global limits, already
        authorizes.
        """
        parent = await self._load_task(parent_task_id)
        if parent.run_id != run_id:
            raise WorkflowError(f"parent task {parent_task_id!r} does not belong to run {run_id!r}")
        try:
            await check_run_budget(self._store, run_id)
            grant_document = await self._store.get_one("capability_grants", {"grant_id": parent.grant_id})
            if grant_document is None:
                raise CapabilityDenied(f"parent task {parent_task_id!r} has no capability grant")
            grant = CapabilityGrant.model_validate(grant_document)
            siblings = await self._store.find_many("work_items", {"parent_task_id": parent_task_id})
            # check_child's fanout ceiling counts total children ever created
            # (D5: "Ledger and Herald each need 2"), excluding only explicitly
            # voided ones -- unchanged from its original definition.
            fanout_siblings = [s for s in siblings if s.get("state") not in ("cancelled", "rejected", "superseded")]
            self._tasks.policy.check_child(grant, task_type, depth=parent.depth + 1, children=len(fanout_siblings))
            live_siblings = [s for s in siblings if s.get("state") not in _TERMINAL_TASK_STATES]
            if len(live_siblings) >= limits.MAX_PARALLEL_TASKS_PER_PARENT:
                raise CapabilityDenied(
                    f"parent {parent_task_id!r} already has {len(live_siblings)} live children "
                    f"(MAX_PARALLEL_TASKS_PER_PARENT={limits.MAX_PARALLEL_TASKS_PER_PARENT})"
                )
            run_tasks = await self._store.find_many("work_items", {"run_id": run_id})
            if len(run_tasks) >= limits.MAX_TASKS_PER_RUN:
                raise CapabilityDenied(
                    f"run_id={run_id!r} already created {len(run_tasks)} tasks "
                    f"(MAX_TASKS_PER_RUN={limits.MAX_TASKS_PER_RUN})"
                )
            live_run_tasks = [t for t in run_tasks if t.get("state") not in _TERMINAL_TASK_STATES]
            if len(live_run_tasks) >= limits.MAX_PARALLEL_TASKS_PER_RUN:
                raise CapabilityDenied(
                    f"run_id={run_id!r} already has {len(live_run_tasks)} live tasks "
                    f"(MAX_PARALLEL_TASKS_PER_RUN={limits.MAX_PARALLEL_TASKS_PER_RUN})"
                )
            worker = _worker_for_task_type(task_type)
            ceiling = _bounded_budget(MANIFESTS[worker].budget)
            if _budget_widens(budget, ceiling):
                raise CapabilityDenied(f"proposed budget for {task_type!r} exceeds {worker!r}'s manifest authority")
        except CapabilityDenied as exc:
            # Every refusal in this method -- run-wall, depth, fanout,
            # per-parent/per-run parallelism, task-count, and budget-widen
            # -- lands here once, so no denial site can be added later
            # without also being traced. D5 step 7 (Mandatory acceptance
            # tests): the refusal is always a `TraceEvent` with
            # `outcome="budget_exceeded"`, never only a raised exception.
            await StoreTraceWriter(self._store, run_id=run_id, session_id=parent.session_id).emit(
                outcome="budget_exceeded", status_text=str(exc), task_id=parent_task_id, agent=parent.worker,
            )
            raise
        return await self._tasks.enqueue(
            run_id=run_id,
            session_id=parent.session_id,
            worker=worker,
            task_type=task_type,
            parent_task_id=parent_task_id,
            depth=parent.depth + 1,
            input_ref=input_ref,
            correlation_id=parent.correlation_id,
        )

    # ---- request_human_review ---------------------------------------------

    async def request_human_review(
        self,
        *,
        run_id: str,
        node: str,
        reason_codes: list[str],
        decision_version: int,
        audit_version: str = "",
        evidence_version: str = "",
        task_id: str = "",
        required_role: str = "",
    ) -> HumanReviewRequest:
        """Open a ``HumanReviewRequest`` and pause the run at ``node``.

        The only path to ``awaiting_human_review``: D10 repoints every
        current escalation caller here. ``task_id``/``required_role`` are
        not part of D9's published signature (see module docstring); both
        default to ``""`` so every existing call keeps D9's exact arity.
        """
        run = await self._load_run(run_id)
        validate_node(node)
        # D13: "a rerun of Auditor produces a new audit_version and cannot
        # clear an older request; only supersede closes it." Without this,
        # a second escalation before the first is resolved leaves two
        # simultaneously "open" requests for the same run_id, and every
        # reader that picks "the" open request (session_human_review's
        # find_many(...)[0]) binds to an arbitrary one of them. At most
        # one open request per run_id, always: superseding here, at the
        # sole issuance point, is the one place that invariant can be
        # enforced unconditionally rather than hoped for by every caller.
        for stale in await self._store.find_many("human_review_requests", {"run_id": run_id, "state": "open"}):
            await self.supersede_human_review(
                request_id=stale["request_id"], principal="system",
                reason=f"superseded by new escalation at node={node!r} (reasons={list(reason_codes)!r})",
                policy_version=POLICY_VERSION,
            )
        request = HumanReviewRequest(
            run_id=run_id,
            session_id=run.session_id,
            workflow_version=WORKFLOW_VERSION,
            task_id=task_id,
            node=node,
            reason_codes=list(reason_codes),
            decision_version=decision_version,
            audit_version=audit_version,
            evidence_version=evidence_version,
            required_role=required_role,
        )
        await self._store.insert("human_review_requests", request)
        now = _now()
        updated = run.model_copy(
            update={"state": "awaiting_human_review", "node": node, "paused_at": now, "updated_at": now}
        )
        matched = await self._store.compare_and_set(
            "workflow_runs", {"run_id": run_id}, {"updated_at": run.updated_at}, updated
        )
        if not matched:
            raise WorkflowError(f"lost the race pausing run_id={run_id!r} for human review")
        return request

    # ---- supersede_human_review ---------------------------------------------

    async def supersede_human_review(
        self, *, request_id: str, principal: str, reason: str, policy_version: str,
    ) -> HumanReviewRequest:
        """Close an open ``HumanReviewRequest`` without a review event (D13).

        The only other way a request leaves ``"open"`` is
        ``consume_review_event`` resolving it with an actual reviewer
        submission. This is for the case D13 names explicitly: a new
        Auditor verdict (or any other re-escalation) makes an older open
        request moot before a reviewer ever acted on it -- there is no
        review event to consume, only an explicit reason this class
        records. Idempotent: a request that already left ``"open"`` (by
        either path, or a concurrent supersede) is returned unchanged.
        """
        doc = await self._store.get_one("human_review_requests", {"request_id": request_id})
        if doc is None:
            raise WorkflowError(f"unknown request_id: {request_id!r}")
        request = HumanReviewRequest.model_validate(doc)
        if request.state != "open":
            return request
        superseded = request.model_copy(update={
            "state": "superseded", "resolved_at": _now(),
            "superseded_by": principal, "superseded_reason": reason,
            "superseded_policy_version": policy_version,
        })
        matched = await self._store.compare_and_set(
            "human_review_requests", {"request_id": request_id}, {"state": "open"}, superseded
        )
        if not matched:
            raise WorkflowError(f"lost the race superseding request_id={request_id!r}")
        return superseded

    # ---- consume_review_event ----------------------------------------------

    async def consume_review_event(self, *, run_id: str, event: HumanReviewEvent) -> WorkflowRun:
        """Record ``event``, resolve its ``HumanReviewRequest``, and resume
        the run to ``running``.

        Does not itself move ``node`` -- that is ``advance``'s exclusive
        job, called separately by the caller once it knows the outcome
        the event produced (D9 draws that line at "the only consumer of a
        human-review event" versus "the only writer of ... node", two
        distinct exclusivities in the same paragraph).
        """
        if event.run_id != run_id:
            raise WorkflowError(f"event.run_id {event.run_id!r} does not match run_id {run_id!r}")
        run = await self._load_run(run_id)
        request_document = await self._store.get_one("human_review_requests", {"request_id": event.request_id})
        if request_document is None:
            raise WorkflowError(f"unknown request_id: {event.request_id!r}")
        request = HumanReviewRequest.model_validate(request_document)
        if request.state == "open":
            resolved_request = request.model_copy(update={"state": "resolved", "resolved_at": _now()})
            matched = await self._store.compare_and_set(
                "human_review_requests", {"request_id": request.request_id}, {"state": "open"}, resolved_request
            )
            if not matched:
                raise WorkflowError(f"lost the race resolving request_id={request.request_id!r}")
        await self._store.insert("human_review_events", event)
        if run.state != "awaiting_human_review":
            return run
        updated = run.model_copy(update={"state": "running", "resumed_at": _now(), "updated_at": _now()})
        matched = await self._store.compare_and_set(
            "workflow_runs", {"run_id": run_id}, {"updated_at": run.updated_at}, updated
        )
        if not matched:
            raise WorkflowError(f"lost the race resuming run_id={run_id!r}")
        return updated

    # ---- accept_result ----------------------------------------------------

    async def accept_result(self, *, run_id: str, task_id: str, result: dict[str, Any]) -> bool:
        """The sole acceptance authority for a completed child task.

        A child's ``succeeded`` state (``TaskService.complete``) is
        infrastructure-level completion, not acceptance; a task cannot
        accept its own result. Returns ``False`` -- never raises -- for
        every refusal reason (wrong run, not yet succeeded, or a
        caller-asserted ``result`` that diverges from the task's own
        fenced ``output_ref``): a caller checks the boolean, it does not
        need to distinguish refusal reasons to decide not to proceed.
        Idempotent: accepting an already-accepted task returns ``True``
        without a second write.
        """
        task = await self._load_task(task_id)
        if task.run_id != run_id or task.state != "succeeded" or result != task.output_ref:
            return False
        for _ in range(10):
            run = await self._load_run(run_id)
            accepted_ids = set(run.checkpoint.get("accepted_task_ids") or [])
            if task_id in accepted_ids:
                return True
            accepted_ids.add(task_id)
            updated = run.model_copy(
                update={
                    "checkpoint": {**run.checkpoint, "accepted_task_ids": sorted(accepted_ids)},
                    "updated_at": _now(),
                }
            )
            if await self._store.compare_and_set(
                "workflow_runs", {"run_id": run_id}, {"updated_at": run.updated_at}, updated
            ):
                return True
        raise WorkflowError(f"could not record acceptance for task_id={task_id!r} after retries")

    # ---- run metadata --------------------------------------------------

    async def set_hold(self, *, run_id: str, reason: str) -> WorkflowRun:
        """Persist a retention hold through the workflow run's CAS boundary."""
        for _ in range(10):
            run = await self._load_run(run_id)
            if run.hold == reason:
                return run
            updated = run.model_copy(update={"hold": reason, "updated_at": _now()})
            if await self._store.compare_and_set(
                "workflow_runs", {"run_id": run_id}, {"updated_at": run.updated_at}, updated
            ):
                return updated
        raise WorkflowError(f"could not set hold for run_id={run_id!r} after retries")

    async def clear_hold(self, *, run_id: str) -> WorkflowRun:
        """Clear a retention hold through the workflow run's CAS boundary."""
        for _ in range(10):
            run = await self._load_run(run_id)
            if not run.hold:
                return run
            updated = run.model_copy(update={"hold": "", "updated_at": _now()})
            if await self._store.compare_and_set(
                "workflow_runs", {"run_id": run_id}, {"updated_at": run.updated_at}, updated
            ):
                return updated
        raise WorkflowError(f"could not clear hold for run_id={run_id!r} after retries")

    async def record_opaque_map(self, *, run_id: str, opaque_map: dict[str, str]) -> WorkflowRun:
        """Persist the run's opaque identifiers through the CAS boundary."""
        for _ in range(10):
            run = await self._load_run(run_id)
            if run.opaque_map == opaque_map:
                return run
            updated = run.model_copy(update={"opaque_map": dict(opaque_map), "updated_at": _now()})
            if await self._store.compare_and_set(
                "workflow_runs", {"run_id": run_id}, {"updated_at": run.updated_at}, updated
            ):
                return updated
        raise WorkflowError(f"could not record opaque map for run_id={run_id!r} after retries")

    async def erase_opaque_map(self, *, run_id: str) -> WorkflowRun:
        """D5 right-to-erasure/retention capability: clear ``run_id``'s
        sensitive-header vault to empty through the same CAS boundary
        ``record_opaque_map`` uses. Idempotent: erasing an
        already-empty map is a no-op success, not an error.

        This method exists so a session-erasure or retention-sweep
        caller has something to call; it is not itself wired to one --
        server.py's ``session_delete`` route and
        ``_purge_settled_sessions_loop`` are outside this module's
        scope."""
        return await self.record_opaque_map(run_id=run_id, opaque_map={})

    # ---- recover ------------------------------------------------------

    async def recover(
        self, *, run_id: str, cause: str, expected_node: str | None = None
    ) -> WorkflowRun:
        """Resume a run from its last committed checkpoint (D9's fail-closed
        resume default). A terminal run is returned unchanged.

        ``expected_node`` (rewrite plan step 6): closes a disclosed gap.
        ``request_human_review`` opens a durable ``HumanReviewRequest`` and
        the session pauses at ``awaiting_human_review``, but not every path
        that reaches that status calls ``advance()`` to keep
        ``workflow_runs.node`` in lockstep with it. A caller re-entering
        ``run_pipeline`` at a specific node -- because the session
        document's own persisted status says that is where review is
        pending -- needs the checkpoint to agree, not silently diverge and
        break the next ``advance()`` call's transition lookup. When
        supplied and it disagrees with the checkpoint-resolved node, the
        run is resynchronized to ``expected_node`` instead, with the
        divergence recorded on the checkpoint for audit.
        """
        run = await self._load_run(run_id)
        if is_terminal(run.node):
            return run
        checkpoint = Checkpoint(
            node=run.node,
            checkpoint_version=run.checkpoint_version or CHECKPOINT_VERSION,
            payload_refs=tuple(run.checkpoint.get("payload_refs") or ()),
        )
        resume_to = resume_node(checkpoint)
        resynced_from = None
        if expected_node is not None and expected_node != resume_to:
            resynced_from = resume_to
            resume_to = expected_node
        now = _now()
        checkpoint_update = {**run.checkpoint, "recovery_cause": cause}
        if resynced_from is not None:
            checkpoint_update["resynced_from"] = resynced_from
        updated = run.model_copy(
            update={
                "node": resume_to,
                "state": "running" if run.state == "awaiting_human_review" and resume_to != run.node else run.state,
                "resumed_at": now,
                "updated_at": now,
                "checkpoint": checkpoint_update,
            }
        )
        matched = await self._store.compare_and_set(
            "workflow_runs", {"run_id": run_id}, {"updated_at": run.updated_at, "node": run.node}, updated
        )
        if not matched:
            raise WorkflowError(f"lost the race recovering run_id={run_id!r}")
        return updated

    # ---- authorize_manifest_freeze -------------------------------------------

    async def authorize_manifest_freeze(
        self, *, run_id: str, artifact_id: str, manifest: VerifiedClassificationManifest,
    ) -> VerifiedClassificationManifest:
        """The manifest-freeze-authorization responsibility (D9/docs #49,
        #87): the authority that lets a
        :class:`~.records.VerifiedClassificationManifest` actually be
        recorded as ``verified_for_execution``.

        The model's own validator already refuses ``unresolved_items !=
        0`` at ``status="verified_for_execution"`` construction time; what
        this method adds is the *run-lifecycle* eligibility check the
        model cannot see -- refuses (``WorkflowError``) once the run is
        terminal or paused for human review, and refuses a manifest whose
        own ``run_id`` does not match ``run_id`` (never freeze a manifest
        into a run it does not belong to). Writes into Wave R-b's
        :data:`MANIFEST_COLLECTION`, keyed by ``artifact_id`` plus a
        ``status`` field -- the exact shape
        ``ArtifactService.invalidate_descendants``/``open_for_download``
        already read from, per that wave's documented convention for any
        future code creating a manifest.
        """
        if manifest.run_id != run_id:
            raise WorkflowError(
                f"manifest.run_id {manifest.run_id!r} does not match run_id {run_id!r}"
            )
        run = await self._load_run(run_id)
        if is_terminal(run.node) or run.state == "awaiting_human_review":
            raise WorkflowError(
                f"cannot authorize a manifest freeze for run_id={run_id!r} in state {run.state!r}"
            )
        document = manifest.model_dump(mode="python")
        document["artifact_id"] = artifact_id
        existing = await self._store.get_one(MANIFEST_COLLECTION, {"artifact_id": artifact_id})
        if existing is None:
            await self._store.insert(MANIFEST_COLLECTION, document)
        else:
            await self._store.replace_one(MANIFEST_COLLECTION, {"artifact_id": artifact_id}, document)
        return manifest

    # ---- rewind ---------------------------------------------------------

    async def rewind(self, *, run_id: str, to_node: str, reason: str) -> WorkflowRun:
        """The rewind-routing responsibility (D9/docs #56, #87): route a
        run back to an earlier checkpoint node.

        Section 56 ("final failure rewind") explicitly scopes the actual
        re-execution that follows a rewind to a later phase ("do not
        implement" there) -- this method is the routing decision alone.
        It fails closed on an unknown ``to_node`` (``validate_node``'s own
        table lookup), refuses a terminal target (``advance`` is the only
        path to a terminal node, never ``rewind``), and refuses a target
        that is not strictly earlier than the run's current node using
        ``NON_TERMINAL_NODES``'s own canonical checkpoint order -- a
        "rewind" to the same or a later node is not a rewind. Commits a
        fresh checkpoint at ``to_node`` under the same CAS boundary
        ``advance`` uses, and resumes the run to ``running`` (a rewind out
        of ``awaiting_human_review`` is itself the resolution, not a
        second pending review)."""
        target = validate_node(to_node)
        if is_terminal(target):
            raise WorkflowError(f"rewind refuses a terminal target node: {to_node!r}")
        run = await self._load_run(run_id)
        if is_terminal(run.node):
            raise WorkflowError(f"run_id={run_id!r} is already terminal at node {run.node!r}")
        if NON_TERMINAL_NODES.index(target) > NON_TERMINAL_NODES.index(run.node):
            raise WorkflowError(
                f"rewind target {to_node!r} is not earlier than current node {run.node!r}"
            )
        now = _now()
        updated = run.model_copy(
            update={
                "node": target,
                "state": "running",
                "checkpoint": {
                    "node": target, "checkpoint_version": CHECKPOINT_VERSION,
                    "payload_refs": [], "rewind_reason": reason,
                },
                "checkpoint_version": CHECKPOINT_VERSION,
                "updated_at": now,
            }
        )
        matched = await self._store.compare_and_set(
            "workflow_runs", {"run_id": run_id}, {"updated_at": run.updated_at, "node": run.node}, updated
        )
        if not matched:
            raise WorkflowError(f"lost the race rewinding run_id={run_id!r}")
        return updated

    # ---- authorize_final_release -------------------------------------------

    async def authorize_final_release(self, *, run_id: str) -> bool:
        """The final-release-authorization responsibility (D9/docs #87):
        the gate before a run's outputs may be released/exported.

        Checks the *run* has actually reached a releasable terminal state
        (``complete`` or ``partially_complete``) with no
        ``HumanReviewRequest`` still ``open``."""
        run = await self._load_run(run_id)
        if run.state not in ("complete", "partially_complete"):
            return False
        open_requests = await self._store.find_many(
            "human_review_requests", {"run_id": run_id, "state": "open"}
        )
        return not open_requests

    # ---- begin_cleanup / confirm_cleanup --------------------------------------

    async def begin_cleanup(self, *, run_id: str) -> WorkflowRun:
        """The cleanup-lifecycle responsibility (D9/docs #77, #87): advance
        ``workflow_runs.state`` to ``destroying``. Section 77's
        :class:`~.records.CleanupManifest` invariant -- "never transitions
        a run to SESSION_DESTROYED until this reports verified" -- is
        ``confirm_cleanup``'s job, below; entering ``destroying`` is only
        the announcement that cleanup has begun, gated on nothing beyond
        the run existing, since cleanup may need to run even on a run
        that never reached a normal terminal node (a ``blocked`` or
        ``cancelled`` run still needs its sandbox/session destroyed).
        Idempotent, CAS-guarded."""
        for _ in range(10):
            run = await self._load_run(run_id)
            if run.state == "destroying":
                return run
            updated = run.model_copy(update={"state": "destroying", "updated_at": _now()})
            if await self._store.compare_and_set(
                "workflow_runs", {"run_id": run_id}, {"updated_at": run.updated_at}, updated
            ):
                return updated
        raise WorkflowError(f"could not begin cleanup for run_id={run_id!r} after retries")

    async def confirm_cleanup(self, *, run_id: str, manifest: CleanupManifest) -> WorkflowRun:
        """The sole path to ``session_destroyed`` (docs #77): refuses
        unless ``manifest.run_id`` matches ``run_id``, unless
        ``manifest.verification_status == "verified"`` (section 77's own
        "never transitions ... until this reports verified" invariant),
        and unless the run already entered ``destroying`` via
        ``begin_cleanup`` -- there is no path to confirming a cleanup
        that was never begun."""
        if manifest.run_id != run_id:
            raise WorkflowError(
                f"manifest.run_id {manifest.run_id!r} does not match run_id {run_id!r}"
            )
        if manifest.verification_status != "verified":
            raise WorkflowError(
                f"cleanup for run_id={run_id!r} is not verified: {manifest.verification_status!r}"
            )
        for _ in range(10):
            run = await self._load_run(run_id)
            if run.state == "session_destroyed":
                return run
            if run.state != "destroying":
                raise WorkflowError(
                    f"run_id={run_id!r} must be destroying before it can be session_destroyed, "
                    f"is {run.state!r}"
                )
            updated = run.model_copy(update={"state": "session_destroyed", "updated_at": _now()})
            if await self._store.compare_and_set(
                "workflow_runs", {"run_id": run_id}, {"updated_at": run.updated_at}, updated
            ):
                return updated
        raise WorkflowError(f"could not confirm cleanup for run_id={run_id!r} after retries")

    # ---- close_run ------------------------------------------------------

    async def close_run(self, *, run_id: str) -> dict[str, Any]:
        """The formal-run-closure responsibility (docs section 9, #87): the
        read/verify authority for whether ``run_id`` may actually be
        considered closed.

        Composes ``terminal_outcome``'s existing node/state read with the
        two closure preconditions this class alone can see across:
        no ``HumanReviewRequest`` still ``open`` (a run cannot close with
        an outstanding human decision), and no ``WorkItem`` this run ever
        created still in a non-terminal ``TaskState`` (nothing left for
        ``TaskService``/``control/worker.py`` to still be supervising).
        Does not itself destroy or export anything -- the cleanup and
        export lifecycle methods above are the actuators; this is the
        formal confirmation step section 9 calls "closure".

        No collision with ``ManagerSupervision.close_run`` (a run-report
        method with a different signature, on a fully separate class in
        this same module): the two never coexist on one object.
        """
        run = await self._load_run(run_id)
        open_requests = await self._store.find_many(
            "human_review_requests", {"run_id": run_id, "state": "open"}
        )
        tasks = await self._store.find_many("work_items", {"run_id": run_id})
        live_task_ids = sorted(
            t["task_id"] for t in tasks if t.get("state") not in _TERMINAL_TASK_STATES
        )
        closeable = is_terminal(run.node) and not open_requests and not live_task_ids
        return {
            "run_id": run_id,
            "closeable": closeable,
            "node": run.node,
            "state": run.state,
            "open_human_review_request_ids": sorted(r["request_id"] for r in open_requests),
            "live_task_ids": live_task_ids,
        }

    # ---- terminal_outcome ---------------------------------------------

    async def terminal_outcome(self, *, run_id: str) -> dict[str, Any]:
        """A read-only summary of ``run_id``'s terminal status, if any."""
        run = await self._load_run(run_id)
        return {
            "run_id": run.run_id,
            "node": run.node,
            "state": run.state,
            "is_terminal": is_terminal(run.node),
            "terminal_outcome": run.terminal_outcome,
            "completed_at": run.completed_at,
        }
