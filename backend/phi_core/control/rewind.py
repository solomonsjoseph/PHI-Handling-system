"""RewindRouter: the root-cause classifier and rewind-routing responsibility
(docs #56, Phase 10, section 56 "final failure rewind").

Section 56 explicitly scopes the actual re-execution machinery to
``Manager.rewind`` (``control/manager.py:871``, built
in Wave R-b, never called from anywhere live before this module): "do not
implement" the re-execution loop itself in an earlier phase. This module
is the missing decision layer on top of that primitive -- given a failure
signal, decide which of five conceptual root-cause categories applies and
which workflow node is the earliest affected stage, then call the
existing ``rewind()`` with that resolved target. It never builds a second
rewind mechanism of its own.

The five-value classification vocabulary below (``FailureCategory``) is
this module's own, NOT a member of ``records.FailureClass`` (a closed
Literal this wave never edits): ``SEMANTIC_ERROR`` and
``UNRESOLVED_UNCERTAINTY`` have no matching ``FailureClass`` member, so
each category maps onto the closest existing one only when a caller
needs to *persist* the failure (``FailureCategory.failure_class_for``
below) -- the routing decision itself is made on the five-value category,
never on the narrower ``FailureClass`` vocabulary directly.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from .manager import Manager
from .store import ControlStore

FailureCategory = Literal[
    "EXECUTION_ERROR", "METHOD_ERROR", "REGULATION_ERROR", "SEMANTIC_ERROR", "UNRESOLVED_UNCERTAINTY",
]

# Section 56's diagrammed earliest-affected stage per category.
# METHOD_ERROR and REGULATION_ERROR both loop back through Judge
# ("Judge -> {PHI Methods Expert,Regulations Expert} -> Judge -> Reviewer
# Preview -> new manifest -> Executor"), so both target `decide`.
# SEMANTIC_ERROR is earliest of all: a specialist's own finding was wrong,
# so the fix must start at `specialists`, not merely re-run Judge over the
# same (bad) specialist output. UNRESOLVED_UNCERTAINTY's target depends on
# `stage` -- see `_UNRESOLVED_UNCERTAINTY_NODE_BY_STAGE` below.
_CATEGORY_TO_NODE: dict[FailureCategory, str] = {
    "EXECUTION_ERROR": "execute",
    "METHOD_ERROR": "decide",
    "REGULATION_ERROR": "decide",
    "SEMANTIC_ERROR": "specialists",
}

# UNRESOLVED_UNCERTAINTY's rewind target is genuinely discretionary
# (section 56 names both `human_review_decisions` and, implicitly,
# a post-execution human-review point). Design decision, documented here
# once rather than re-litigated at each call site: "post_execution" (the
# default -- this is where every live caller today, Reviewer Final,
# actually surfaces the signal from) targets `human_review_audit`, the
# D9 node reserved for exactly this "a human must look at what already
# ran" situation. "pre_execution" targets `human_review_decisions`, the
# earlier node reserved for uncertainty discovered before Judge's
# decisions were ever executed -- no live caller uses this stage today,
# but the classifier supports it for a future pre-execution reviewer.
_UNRESOLVED_UNCERTAINTY_NODE_BY_STAGE: dict[str, str] = {
    "post_execution": "human_review_audit",
    "pre_execution": "human_review_decisions",
}

# The mapped FailureClass to persist alongside a routing decision (docs
# #56's explicit instruction: SEMANTIC_ERROR -> SPECIALIST_INTERPRETATION_
# ERROR, UNRESOLVED_UNCERTAINTY -> HUMAN_REVIEW_REQUIRED, the other three
# are exact-name matches against records.FailureClass).
_CATEGORY_TO_FAILURE_CLASS: dict[FailureCategory, str] = {
    "EXECUTION_ERROR": "EXECUTION_ERROR",
    "METHOD_ERROR": "METHOD_ERROR",
    "REGULATION_ERROR": "REGULATION_ERROR",
    "SEMANTIC_ERROR": "SPECIALIST_INTERPRETATION_ERROR",
    "UNRESOLVED_UNCERTAINTY": "HUMAN_REVIEW_REQUIRED",
}

# Reverse map: an incoming signal's own `failure_class` hint (any
# records.FailureClass member a caller happens to carry, e.g.
# Reviewer.finalize's own `_finalize_signal`, or Executor/raw-worker's
# ExecutionResult.failure_class) -> which of the five categories above it
# actually routes to. Not every FailureClass member has a natural
# earliest-affected rewind stage; a member absent here is the disclosed
# "truly, structurally blocked" case `classify` fails closed on (see
# `UnroutableFailure`) rather than guessing a stage.
_FAILURE_CLASS_TO_CATEGORY: dict[str, FailureCategory] = {
    "EXECUTION_ERROR": "EXECUTION_ERROR",
    "EXECUTOR_CODE_ERROR": "EXECUTION_ERROR",
    "OUTPUT_ERROR": "EXECUTION_ERROR",
    "SANDBOX_ERROR": "EXECUTION_ERROR",
    "VERIFICATION_ERROR": "EXECUTION_ERROR",
    "METHOD_ERROR": "METHOD_ERROR",
    "EVIDENCE_ERROR": "METHOD_ERROR",
    "REGULATION_ERROR": "REGULATION_ERROR",
    "SPECIALIST_INTERPRETATION_ERROR": "SEMANTIC_ERROR",
    "CLASSIFICATION_ERROR": "SEMANTIC_ERROR",
    "HUMAN_REVIEW_REQUIRED": "UNRESOLVED_UNCERTAINTY",
    "HUMAN_INPUT_REQUIRED": "UNRESOLVED_UNCERTAINTY",
    "REVIEW_CONFLICT": "UNRESOLVED_UNCERTAINTY",
}


class UnroutableFailure(RuntimeError):
    """Raised by ``classify`` when a signal names a ``failure_class`` this
    router has no rewind route for. This is the module's one disclosed
    "truly, structurally blocked" case (section 56: never implement FINAL
    FAIL -> STOP FOREVER *unless* truly blocked) -- a caller receiving
    this must fail closed to the existing human-review escalation path,
    never silently drop the failure or guess a stage."""


@dataclass(frozen=True)
class RewindDecision:
    """The routing decision `classify` reaches: which of the five
    categories applies, the `FailureClass` to persist alongside it, and
    the workflow node `rewind()` should target."""

    category: FailureCategory
    failure_class: str
    to_node: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "category": self.category, "failure_class": self.failure_class,
            "to_node": self.to_node, "reason": self.reason,
        }


def _extract_failure_class(signal: "str | dict[str, Any]") -> str:
    """Accepts either a bare ``FailureClass``-shaped string, or a dict
    carrying one under ``"failure_class"`` -- the exact shape
    ``agents/reviewer.py::Reviewer.finalize``'s own ``_finalize_signal``
    already returns, so its output can be handed to ``classify``
    unchanged."""
    if isinstance(signal, str):
        return signal
    if isinstance(signal, dict):
        failure_class = signal.get("failure_class")
        if failure_class:
            return str(failure_class)
    raise UnroutableFailure(f"signal carries no recognizable failure_class: {signal!r}")


class RewindRouter:
    """docs #56: classify a failure signal into one of five root-cause
    categories, resolve the earliest affected workflow node, and route
    the run back there via the existing ``Manager.rewind``.
    Stateless: every method is deterministic given its arguments, so a
    caller never needs to construct or share an instance."""

    @staticmethod
    def classify(signal: "str | dict[str, Any]", *, stage: str = "post_execution") -> RewindDecision:
        """Classify ``signal`` (a bare ``FailureClass`` string, or a dict
        carrying one under ``"failure_class"``) into a ``RewindDecision``.

        ``stage`` selects which node ``UNRESOLVED_UNCERTAINTY`` targets
        (see the module-level rationale above); ignored for every other
        category. Raises ``UnroutableFailure`` for a ``failure_class``
        this router has no rewind route for, rather than guessing.
        """
        if stage not in _UNRESOLVED_UNCERTAINTY_NODE_BY_STAGE:
            raise ValueError(f"unknown stage: {stage!r}")
        failure_class = _extract_failure_class(signal)
        category = _FAILURE_CLASS_TO_CATEGORY.get(failure_class)
        if category is None:
            raise UnroutableFailure(
                f"no rewind route exists for failure_class={failure_class!r} "
                "(section 56's disclosed structurally-blocked case)"
            )
        to_node = (
            _UNRESOLVED_UNCERTAINTY_NODE_BY_STAGE[stage] if category == "UNRESOLVED_UNCERTAINTY"
            else _CATEGORY_TO_NODE[category]
        )
        mapped_failure_class = _CATEGORY_TO_FAILURE_CLASS[category]
        return RewindDecision(
            category=category, failure_class=mapped_failure_class, to_node=to_node,
            reason=f"root_cause={category}; original_failure_class={failure_class}",
        )

    @staticmethod
    async def route(
        *, super_orchestrator: Manager, run_id: str,
        signal: "str | dict[str, Any]", stage: str = "post_execution",
    ) -> tuple[RewindDecision, Any]:
        """Classify ``signal`` then call the EXISTING
        ``Manager.rewind`` with the resolved target node --
        never a second rewind mechanism. ``rewind()`` itself remains the
        sole authority enforcing "never a terminal target" and "target
        strictly earlier than the run's current node"; a
        ``control.workflow.WorkflowError`` from either of those checks
        propagates unchanged (the caller's job to decide whether that
        means "fall back to human-review escalation", not this router's).

        Returns ``(decision, updated_workflow_run)``.
        """
        decision = RewindRouter.classify(signal, stage=stage)
        run = await super_orchestrator.rewind(run_id=run_id, to_node=decision.to_node, reason=decision.reason)
        return decision, run


async def record_rewind_decision(store: ControlStore, *, run_id: str, decision: RewindDecision) -> None:
    """Persist ``decision`` for audit -- insert-only, the same fixed
    convention ``control/verification.py::record_verification_result``
    and ``agents/reasoning.py``'s ``execution_results`` writes already
    use. No dedicated ``records.py`` contract exists for a rewind
    decision (that file is closed this phase); this writes a plain dict
    into its own ``rewind_decisions`` collection rather than overloading
    an unrelated typed record."""
    await store.insert("rewind_decisions", {
        "run_id": run_id,
        "category": decision.category,
        "failure_class": decision.failure_class,
        "to_node": decision.to_node,
        "reason": decision.reason,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
