"""D64: two read-side projections of the one sanitized ``TraceEvent`` stream
(v3 #64 "observability").

There is deliberately no second event system here: both projections are
pure functions over already-sanitized ``TraceEvent`` records (sanitized at
write time by ``trace_sanitizer.sanitize_payload``, wired into
``TraceEventStore.append``). The User Agent Trace collapses each event to
the friendly ``{agent, message}`` shape v3 #64 shows; the Maintainer Trace
is the full sanitized record, unmodified, for forensic use.
"""
from __future__ import annotations

from typing import Any

from .records import TraceEvent

# agent -> friendly display name, per v3 #64's own example list
# ("Regulations Expert" for Statute, "PHI Methods Expert" for Praxis).
_AGENT_DISPLAY_NAME: dict[str, str] = {
    "Schema": "Schema",
    "Lexicon": "Lexicon",
    "Instrument": "Instrument",
    "Judge": "Judge",
    "Statute": "Regulations Expert",
    "Praxis": "PHI Methods Expert",
    "Reviewer": "Reviewer",
    "Executor": "Executor",
    "HumanReview": "Human Review",
}

# (agent, phase) -> friendly one-line message, falling back to a generic
# "<agent> is working" sentence when a phase has no curated copy yet.
_PHASE_MESSAGE: dict[tuple[str, str], str] = {
    ("Schema", ""): "Analyzing dataset headers",
    ("Lexicon", ""): "Interpreting dictionary",
    ("Instrument", ""): "Reviewing study forms",
    ("Judge", ""): "Classifying study variables",
    ("Statute", ""): "Checking regulatory evidence",
    ("Praxis", ""): "Evaluating handling method",
    ("Reviewer", ""): "Reviewing classification",
    ("Reviewer", "final"): "Performing final review",
    ("Executor", ""): "Applying verified transformation plan",
    ("HumanReview", ""): "1 decision requires confirmation",
    ("Cleanup", ""): "Destroying temporary session state",
}


def _friendly_message(event: TraceEvent) -> str:
    key = (event.agent, event.phase)
    if key in _PHASE_MESSAGE:
        return _PHASE_MESSAGE[key]
    fallback_key = (event.agent, "")
    if fallback_key in _PHASE_MESSAGE:
        return _PHASE_MESSAGE[fallback_key]
    agent = event.agent or "System"
    return f"{agent} is working" if not event.phase else f"{agent}: {event.phase}"


def user_agent_trace(events: list[TraceEvent]) -> list[dict[str, str]]:
    """Friendly, sequence-ordered ``{agent, message, event_id, ts}`` rows.

    Never includes ``payload`` -- the free-form field is maintainer-only
    even after sanitization, since v3 #64's user projection is a fixed
    curated vocabulary of stage announcements, not arbitrary event detail.
    """
    ordered = sorted(events, key=lambda e: e.seq)
    return [
        {
            "event_id": e.event_id,
            "agent": _AGENT_DISPLAY_NAME.get(e.agent, e.agent or "System"),
            "message": _friendly_message(e),
            "ts": e.ts,
        }
        for e in ordered
    ]


def maintainer_trace(events: list[TraceEvent]) -> list[dict[str, Any]]:
    """Full sanitized forensic record, sequence-ordered. Every field
    already sanitized at write time; this projection adds no further
    filtering, only ordering."""
    ordered = sorted(events, key=lambda e: e.seq)
    return [e.model_dump(mode="json") for e in ordered]
