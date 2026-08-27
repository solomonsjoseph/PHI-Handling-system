"""Phase 2C acceptance tests (v3 #64-68 "observability"): TraceSanitizer
pre-persistence sanitization, the User Agent Trace / Maintainer Trace
projections, the added TraceEvent schema fields, and trace_root_hash
rollup onto WorkflowRun via seal_range."""
from __future__ import annotations

import pytest
from phi_core.control.events import TraceEventStore
from phi_core.control.records import TraceEvent, WorkflowRun
from phi_core.control.store import MemoryControlStore
from phi_core.control.trace_projection import maintainer_trace, user_agent_trace
from phi_core.control.trace_sanitizer import CONTENT_REDACTED, sanitize_payload

RUN_ID = "run-" + "a" * 28
SESSION_ID = "session-" + "b" * 24


def _event(**overrides) -> TraceEvent:
    kwargs = dict(run_id=RUN_ID, seq=0, session_id=SESSION_ID, input_class="internal", output_class="internal")
    kwargs.update(overrides)
    return TraceEvent(**kwargs)


async def _seed_run(store: MemoryControlStore) -> None:
    await store.insert("workflow_runs", WorkflowRun(run_id=RUN_ID, session_id=SESSION_ID))


# ---- schema gap fields (v3 #65) --------------------------------------------


def test_trace_event_has_the_previously_missing_schema_fields() -> None:
    event = TraceEvent(
        run_id=RUN_ID, seq=1, session_id=SESSION_ID, input_class="internal", output_class="internal",
        trace_id="tr-1", model_version="v2.1", prompt_template_version="p3", handoff_id="h1",
        sanitized_rationale="kept: not an identifier", alternatives_considered=["drop", "hash"],
        authorization_result="allowed", failure_class="", error_code="", correction_number=0,
        previous_state="pending", new_state="approved",
    )
    assert event.trace_id == "tr-1"
    assert event.alternatives_considered == ["drop", "hash"]
    assert event.previous_state == "pending" and event.new_state == "approved"


# ---- TraceSanitizer (v3 #66) -----------------------------------------------


def test_sanitize_payload_redacts_raw_prompt_by_default(monkeypatch) -> None:
    monkeypatch.delenv("TRACE_RAW_PROMPT_CONTENT", raising=False)
    out = sanitize_payload({"prompt_text": "patient John Smith DOB 01/02/1980"})
    assert out["prompt_text"] == CONTENT_REDACTED


def test_sanitize_payload_scrubs_raw_prompt_when_flag_enabled(monkeypatch) -> None:
    monkeypatch.setenv("TRACE_RAW_PROMPT_CONTENT", "true")
    out = sanitize_payload({"prompt_text": "contact me at a@b.com"})
    assert out["prompt_text"] != CONTENT_REDACTED
    assert "a@b.com" not in out["prompt_text"]


def test_sanitize_payload_redacts_secrets_even_when_flag_enabled(monkeypatch) -> None:
    monkeypatch.setenv("TRACE_RAW_COMPLETION_CONTENT", "true")
    out = sanitize_payload({"completion_text": "here is the key sk-ant-" + "x" * 30})
    assert out["completion_text"] == CONTENT_REDACTED


def test_sanitize_payload_passes_through_non_raw_scrubbed_fields() -> None:
    out = sanitize_payload({"note": "column dob_year kept, no email a@b.com here"})
    assert "a@b.com" not in out["note"]
    assert out["note"] != CONTENT_REDACTED


def test_sanitize_payload_empty_is_empty() -> None:
    assert sanitize_payload({}) == {}


# ---- sanitize-before-persist wiring (append) -------------------------------


@pytest.mark.asyncio
async def test_append_sanitizes_payload_before_hashing_and_insertion() -> None:
    store = MemoryControlStore()
    await _seed_run(store)
    trace = TraceEventStore(store, run_id=RUN_ID, session_id=SESSION_ID)

    persisted = await trace.append(_event(agent="Judge", payload={"prompt_text": "raw prompt with a@b.com"}))

    assert persisted.payload["prompt_text"] == CONTENT_REDACTED
    stored = await store.get_one("trace_events", {"run_id": RUN_ID, "seq": persisted.seq})
    assert stored["payload"]["prompt_text"] == CONTENT_REDACTED


# ---- User Agent Trace / Maintainer Trace projections (v3 #64) -------------


def test_user_agent_trace_maps_known_agents_to_friendly_copy() -> None:
    events = [
        _event(seq=1, agent="Schema"),
        _event(seq=2, agent="Statute"),
        _event(seq=3, agent="Praxis"),
    ]
    rows = user_agent_trace(events)
    assert rows[0] == {"event_id": events[0].event_id, "agent": "Schema", "message": "Analyzing dataset headers", "ts": events[0].ts}
    assert rows[1]["agent"] == "Regulations Expert"
    assert rows[1]["message"] == "Checking regulatory evidence"
    assert rows[2]["agent"] == "PHI Methods Expert"
    assert rows[2]["message"] == "Evaluating handling method"


def test_user_agent_trace_never_leaks_raw_payload() -> None:
    events = [_event(seq=1, agent="Judge", payload={"prompt_text": "should never appear"})]
    rows = user_agent_trace(events)
    assert "payload" not in rows[0]
    assert "should never appear" not in str(rows[0])


def test_user_agent_trace_is_seq_ordered() -> None:
    events = [_event(seq=3, agent="Executor"), _event(seq=1, agent="Schema"), _event(seq=2, agent="Judge")]
    rows = user_agent_trace(events)
    assert [r["agent"] for r in rows] == ["Schema", "Judge", "Executor"]


def test_maintainer_trace_returns_full_sanitized_record_seq_ordered() -> None:
    events = [_event(seq=2, agent="Judge", payload={}), _event(seq=1, agent="Schema", payload={"note": "x"})]
    rows = maintainer_trace(events)
    assert [r["seq"] for r in rows] == [1, 2]
    assert rows[0]["agent"] == "Schema"
    assert "hash" in rows[0] and "prev_hash" in rows[0]


# ---- trace_root_hash rollup onto WorkflowRun (v3 #68) ----------------------


@pytest.mark.asyncio
async def test_seal_range_rolls_up_trace_root_hash_onto_workflow_run() -> None:
    store = MemoryControlStore()
    await _seed_run(store)
    trace = TraceEventStore(store, run_id=RUN_ID, session_id=SESSION_ID)

    first = await trace.append(_event(agent="Schema"))
    second = await trace.append(_event(agent="Judge"))

    run_before = await store.get_one("workflow_runs", {"run_id": RUN_ID})
    assert run_before.get("trace_root_hash", "") == ""

    await trace.seal_range(from_seq=first.seq, to_seq=second.seq)

    run_after = await store.get_one("workflow_runs", {"run_id": RUN_ID})
    assert run_after["trace_root_hash"] == second.hash


@pytest.mark.asyncio
async def test_seal_range_without_a_durable_run_is_best_effort() -> None:
    """No WorkflowRun row exists -- sealing still succeeds even though
    there is nowhere to roll the hash up to."""
    store = MemoryControlStore()
    trace = TraceEventStore(store, run_id=RUN_ID, session_id=SESSION_ID)

    only = await trace.append(_event(agent="Schema"))
    segment = await trace.seal_range(from_seq=only.seq, to_seq=only.seq)

    assert segment["segment_hash"] == only.hash
