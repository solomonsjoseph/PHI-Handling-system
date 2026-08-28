"""Wave R-b (R-Trace) acceptance tests: D3/D4 -- ``TraceEventStore.append``
must scrub ``status_text`` and ``retry_category`` through the same
``scrub_persisted_text`` PHI/PII scrubber ``sanitize_payload`` already
applies to ``payload``, and it must do so BEFORE the value is chained into
the SHA-256 hash, not after. Real callers already interpolate raw study
content into both fields (a dictionary entry name, a column name, a raw
exception ``str(exc)`` forwarded out of a sandboxed child process), so
those are the planted literals below rather than a synthetic placeholder.
"""
from __future__ import annotations

import pytest
from phi_core.control.events import TraceEventStore
from phi_core.control.records import TraceEvent, WorkflowRun
from phi_core.control.store import MemoryControlStore

RUN_ID = "run-" + "a" * 28
SESSION_ID = "session-" + "b" * 24

# The planted sensitive literal: a raw SSN value, exactly the shape D3/D4
# describe (a raw exception ``str(exc)`` / a column-validation message
# embedding real study content). scrub_persisted_text's `_SSN_RE` is a
# strict `\b\d{3}-\d{2}-\d{4}\b` match, so the literal needs a
# non-word-character boundary on both sides (a bare space, not an
# underscore-glued suffix) to actually trigger redaction.
PLANTED_SSN = "111-22-3333"
SENSITIVE_STATUS_TEXT = f"column 'patient_ssn' failed validation for SSN {PLANTED_SSN}"
SENSITIVE_RETRY_CATEGORY = f"KeyError: no column named patient_ssn for record SSN {PLANTED_SSN}"


def _event(**overrides) -> TraceEvent:
    kwargs = dict(run_id=RUN_ID, seq=0, session_id=SESSION_ID, input_class="internal", output_class="internal")
    kwargs.update(overrides)
    return TraceEvent(**kwargs)


async def _seed_run(store: MemoryControlStore) -> None:
    await store.insert("workflow_runs", WorkflowRun(run_id=RUN_ID, session_id=SESSION_ID))


def _as_sse_projection(event: TraceEvent) -> dict:
    """Mirror ``server.py``'s ``_trace_event_to_message``: the read path
    every reader of a persisted ``TraceEvent`` (the ``/agent-trace`` API,
    the session bundle, the corpus benchmark report) goes through. It
    projects ``status_text`` straight out of the persisted document with no
    further sanitization, so whatever ``append()`` stored is exactly what a
    client receives."""
    doc = event.model_dump(mode="json")
    return {
        "status_text": doc.get("status_text", ""),
        "retry_category": doc.get("retry_category", ""),
    }


@pytest.mark.asyncio
async def test_append_scrubs_status_text_before_persisting_and_hashing() -> None:
    store = MemoryControlStore()
    await _seed_run(store)
    trace = TraceEventStore(store, run_id=RUN_ID, session_id=SESSION_ID)

    appended = await trace.append(_event(status_text=SENSITIVE_STATUS_TEXT))

    assert PLANTED_SSN not in appended.status_text
    persisted = await store.get_one("trace_events", {"run_id": RUN_ID, "seq": appended.seq})
    assert PLANTED_SSN not in persisted["status_text"]
    projection = _as_sse_projection(appended)
    assert PLANTED_SSN not in projection["status_text"]


@pytest.mark.asyncio
async def test_append_scrubs_retry_category_before_persisting_and_hashing() -> None:
    store = MemoryControlStore()
    await _seed_run(store)
    trace = TraceEventStore(store, run_id=RUN_ID, session_id=SESSION_ID)

    appended = await trace.append(_event(retry_category=SENSITIVE_RETRY_CATEGORY))

    assert PLANTED_SSN not in appended.retry_category
    persisted = await store.get_one("trace_events", {"run_id": RUN_ID, "seq": appended.seq})
    assert PLANTED_SSN not in persisted["retry_category"]
    projection = _as_sse_projection(appended)
    assert PLANTED_SSN not in projection["retry_category"]


@pytest.mark.asyncio
async def test_append_hashes_the_scrubbed_status_text_not_the_raw_value() -> None:
    """The hash must attest to the sanitized content, not the raw input
    (the same D66 posture ``sanitize_payload`` already gives ``payload``):
    two events differing only in a planted SSN inside an otherwise
    identical ``status_text`` must scrub down to the same value and
    therefore chain to distinguishable-but-consistent hashes derived from
    the scrubbed text, never a hash of the raw SSN-bearing string."""
    store = MemoryControlStore()
    await _seed_run(store)
    trace = TraceEventStore(store, run_id=RUN_ID, session_id=SESSION_ID)

    appended = await trace.append(_event(status_text=SENSITIVE_STATUS_TEXT))

    import hashlib

    from phi_core.control.events import canonical_json
    from phi_core.security import scrub_persisted_text

    expected_scrubbed = scrub_persisted_text(SENSITIVE_STATUS_TEXT)
    raw_payload_dump = appended.model_dump(mode="json")
    raw_payload_dump.pop("hash")
    # Re-derive the hash from the value actually stored on the returned
    # event (which must already be scrubbed) to prove the chain covers the
    # scrubbed text, and confirm the raw SSN never enters that computation.
    recomputed = hashlib.sha256(
        (appended.prev_hash + canonical_json(raw_payload_dump)).encode("utf-8")
    ).hexdigest()
    assert recomputed == appended.hash
    assert appended.status_text == expected_scrubbed
    assert PLANTED_SSN not in canonical_json(raw_payload_dump)
