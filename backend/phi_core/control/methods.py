"""The MethodRegistry service (docs #38, Phase 2D).

Research discovery does not grant execution permission. Praxis's
``MethodFinding`` output is a research artifact; a ``MethodRecord`` only
becomes safe to execute once ``promote`` has walked it through the fixed
``researched -> candidate -> validated -> approved`` lifecycle (or has been
retired via ``-> deprecated`` from any state). ``get_approved_methods`` is
the sole query surface a future execution-time caller (Judge, Executor, or
a Methods specialist) should use -- it never returns a record whose
lifecycle is anything but ``approved``.

Methods are global reference data, not run-scoped, so this module is plain
functions over a ``method_id``, mirroring ``evidence.py``'s shape rather
than ``ArtifactService``'s per-run class.
"""
from __future__ import annotations

from typing import Sequence

from .records import MethodLifecycle, MethodRecord
from .store import ControlStore

# Fixed legal-transition adjacency, mirroring ArtifactError's
# fixed-reason-string convention: every state can move to "deprecated",
# and otherwise only to the single next lifecycle stage. No transition may
# be skipped and nothing may leave "deprecated".
_TRANSITIONS: dict[MethodLifecycle, tuple[MethodLifecycle, ...]] = {
    "researched": ("candidate", "deprecated"),
    "candidate": ("validated", "deprecated"),
    "validated": ("approved", "deprecated"),
    "approved": ("deprecated",),
    "deprecated": (),
}


class MethodError(RuntimeError):
    """Raised with a fixed, testable ``reason`` string on any method refusal."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


async def register_method(
    store: ControlStore,
    *,
    hipaa_category: str,
    name: str,
    evidence_refs: Sequence[str] = (),
    parameters_schema: dict | None = None,
) -> MethodRecord:
    """Insert a new ``MethodRecord`` at lifecycle ``"researched"``."""
    record = MethodRecord(
        hipaa_category=hipaa_category,
        name=name,
        evidence_refs=list(evidence_refs),
        parameters_schema=dict(parameters_schema or {}),
    )
    await store.insert("methods", record)
    return record


async def get_method(store: ControlStore, method_id: str) -> MethodRecord | None:
    doc = await store.get_one("methods", {"method_id": method_id})
    return MethodRecord.model_validate(doc) if doc is not None else None


async def promote(store: ControlStore, method_id: str, *, to: MethodLifecycle) -> MethodRecord:
    """Move ``method_id`` one legal step forward in the lifecycle, or to
    ``"deprecated"`` from any non-terminal state.

    Uses ``store.compare_and_set`` keyed on the record's current lifecycle
    value so two concurrent promotions cannot silently clobber each other:
    the loser gets ``MethodError("method_state_race", ...)``, the same
    shape ``ArtifactService.finalize`` uses for its own CAS race.
    """
    record = await get_method(store, method_id)
    if record is None:
        raise MethodError("method_missing", method_id)
    allowed = _TRANSITIONS[record.lifecycle]
    if to not in allowed:
        raise MethodError(
            "method_illegal_transition",
            f"{record.lifecycle!r} -> {to!r}",
        )
    updated = record.model_copy(update={"lifecycle": to})
    if not await store.compare_and_set(
        "methods", {"method_id": method_id}, {"lifecycle": record.lifecycle}, updated
    ):
        raise MethodError("method_state_race", method_id)
    return updated


async def get_approved_methods(
    store: ControlStore, hipaa_category: str | None = None
) -> list[MethodRecord]:
    """The query-approved-only surface: never returns a non-approved record."""
    query: dict = {"lifecycle": "approved"}
    if hipaa_category is not None:
        query["hipaa_category"] = hipaa_category
    docs = await store.find_many("methods", query)
    return [MethodRecord.model_validate(doc) for doc in docs]
