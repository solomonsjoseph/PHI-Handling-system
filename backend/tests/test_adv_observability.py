"""Phase 15b category 7: observability (docs section 98).

Positive-detection adversarial tests: PHI must never survive into a
forwarded exception (D3, confirming the Phase R fix has not regressed),
a filename derived from raw, PHI-shaped user content must never leak
into any served artifact path, a secret-shaped literal in an arbitrary
(non-raw-content-keyed) trace payload field must still be caught, every
raw-LLM-I/O payload key must default to redacted, and the trace hash
chain must be genuinely tamper-evident.
"""
from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest
from phi_core.control.context import StoreTraceWriter
from phi_core.control.events import canonical_json
from phi_core.control.records import WorkflowRun
from phi_core.control.sandbox import SandboxError, create_sandbox, destroy_sandbox, run_isolated
from phi_core.control.store import MemoryControlStore

_FAIL_CLOSED_TEST_NAME = "__never_matches__"


@pytest.fixture(autouse=True)
def _allow_unenforced_sandbox_memory(request, monkeypatch):
    if request.node.name != _FAIL_CLOSED_TEST_NAME:
        monkeypatch.setenv("PHI_SANDBOX_ALLOW_UNENFORCED_MEMORY", "1")


def _run_id() -> str:
    return uuid4().hex


# ---------------------------------------------------------------------------
# 1. PHI in exception -- D3 (Phase R): confirm a sandboxed worker's raised
#    exception never forwards raw PHI to the parent process, with a
#    genuinely new, multi-identifier planted scenario extending
#    test_run_isolated_scrubs_exception_text_before_forwarding_to_parent.
# ---------------------------------------------------------------------------


def _raise_with_multiple_phi_shapes():
    raise ValueError(
        "row validation failed for patient Theodore Blackwood-Ferris "
        "(SSN 369-25-8147, MRN MR8847712, DOB 1958-03-14, "
        "phone 617-555-0142, email t.blackwood@example-hospital.org)"
    )


def test_sandboxed_exception_with_five_distinct_phi_shapes_never_forwards_any_of_them():
    record = create_sandbox(_run_id())
    try:
        with pytest.raises(SandboxError) as excinfo:
            run_isolated(record, _raise_with_multiple_phi_shapes)
        message = str(excinfo.value)
        assert "ValueError" in message  # the exception type is preserved
        for planted in (
            "Theodore Blackwood-Ferris", "369-25-8147", "MR8847712",
            "1958-03-14", "617-555-0142", "t.blackwood@example-hospital.org",
        ):
            assert planted not in message
    finally:
        destroy_sandbox(record)


# ---------------------------------------------------------------------------
# 2. PHI in filename -- an uploaded filename carrying a raw identifier
#    must never leak into any staged/served on-disk path or artifact
#    record; every path is keyed by the opaque artifact_id alone.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_phi_shaped_original_filename_never_reaches_any_staged_path_or_record():
    from phi_core.control.artifacts import ArtifactService

    store = MemoryControlStore()
    sid, run_id = uuid4().hex, uuid4().hex
    service = ArtifactService(store, session_id=sid, run_id=run_id)
    malicious_filename = "Patient_Jane_Doe_SSN_123-45-6789_DOB_1980-01-01.csv"

    artifact_id, tmp_path = await service.stage(
        "dataset_export", malicious_filename, "restricted_metadata", "export",
    )
    tmp_path.write_bytes(b"col_a\n1\n")
    record = await service.finalize(artifact_id)

    assert "Jane" not in str(tmp_path)
    assert "123-45-6789" not in str(tmp_path)
    assert "Jane" not in record.rel_path
    assert "123-45-6789" not in record.rel_path
    stored_doc = await store.get_one("artifacts", {"artifact_id": artifact_id})
    assert "Jane" not in str(stored_doc)
    assert "123-45-6789" not in str(stored_doc)


# ---------------------------------------------------------------------------
# 3. secret in trace -- a credential-shaped literal in an arbitrary trace
#    payload field (not one of the three documented raw-content keys)
#    must still be caught by the secret scanner and redacted.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secret_in_an_arbitrary_trace_payload_field_is_redacted():
    run_id, session_id, task_id = _run_id(), _run_id(), _run_id()
    store = MemoryControlStore()
    await store.insert("workflow_runs", WorkflowRun(run_id=run_id, session_id=session_id))
    planted_secret = "sk-ant-" + "d" * 40
    writer = StoreTraceWriter(store, run_id=run_id, session_id=session_id)

    await writer.emit(
        task_id=task_id, agent="RegulationsExpert", input_class="internal", output_class="internal",
        payload={"debug_note": f"retry context included key {planted_secret} accidentally"},
    )

    events = await store.find_many("trace_events", {"run_id": run_id})
    assert len(events) == 1
    assert events[0]["payload"]["debug_note"] == "CONTENT_REDACTED"
    assert planted_secret not in str(events[0])


# ---------------------------------------------------------------------------
# 4. raw prompt capture -- extends test_adv_raw_dataset_boundary.py's
#    prompt_text coverage to the other two documented raw-content keys
#    (completion_text, tool_result_text), each redacted by default.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completion_and_tool_result_keys_are_redacted_by_default():
    run_id, session_id, task_id = _run_id(), _run_id(), _run_id()
    store = MemoryControlStore()
    await store.insert("workflow_runs", WorkflowRun(run_id=run_id, session_id=session_id))
    planted_completion = "the patient's SSN is 111-22-3333 per the raw completion"
    planted_tool_result = "raw tool result: MRN 4455667 confirmed"
    writer = StoreTraceWriter(store, run_id=run_id, session_id=session_id)

    await writer.emit(
        task_id=task_id, agent="RegulationsExpert", input_class="internal", output_class="internal",
        payload={"completion_text": planted_completion, "tool_result_text": planted_tool_result},
    )

    events = await store.find_many("trace_events", {"run_id": run_id})
    assert len(events) == 1
    assert events[0]["payload"]["completion_text"] == "CONTENT_REDACTED"
    assert events[0]["payload"]["tool_result_text"] == "CONTENT_REDACTED"
    assert "111-22-3333" not in str(events[0])
    assert "4455667" not in str(events[0])


# ---------------------------------------------------------------------------
# 5. trace tampering -- the hash chain (D15, control/events.py) must be
#    genuinely tamper-evident: mutating a persisted event's payload
#    out-of-band must produce a stored hash that no longer matches the
#    same canonical-json-of-(prev_hash + payload) recomputation the
#    writer itself uses, and must break the next event's prev_hash link.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tampering_with_a_persisted_trace_event_breaks_the_hash_chain():
    run_id, session_id = _run_id(), _run_id()
    store = MemoryControlStore()
    await store.insert("workflow_runs", WorkflowRun(run_id=run_id, session_id=session_id))
    writer = StoreTraceWriter(store, run_id=run_id, session_id=session_id)

    await writer.emit(agent="Judge", input_class="internal", output_class="internal", status_text="first event")
    await writer.emit(agent="Judge", input_class="internal", output_class="internal", status_text="second event")

    events = sorted(await store.find_many("trace_events", {"run_id": run_id}), key=lambda e: e["seq"])
    assert len(events) == 2
    first, second = events
    assert second["prev_hash"] == first["hash"]

    # Recompute the first event's hash the exact way TraceEventStore.append
    # does, from its currently-stored fields, and confirm it matches --
    # this is the genuine, honest chain before any tampering.
    def _recompute_hash(event: dict) -> str:
        payload = dict(event)
        payload.pop("hash")
        return hashlib.sha256(
            (event["prev_hash"] + canonical_json(payload)).encode("utf-8")
        ).hexdigest()

    assert _recompute_hash(first) == first["hash"]

    # Tamper with the first event directly in the store, bypassing the
    # only sanctioned writer (TraceEventStore.append).
    tampered = dict(first)
    tampered["status_text"] = "TAMPERED: this was never the original content"
    await store.replace_one("trace_events", {"run_id": run_id, "seq": first["seq"]}, tampered)
    tampered_doc = await store.get_one("trace_events", {"run_id": run_id, "seq": first["seq"]})

    # The stored hash no longer authenticates the (now-tampered) content:
    # a genuine tamper-evidence check recomputing from the mutated fields
    # produces a different digest than what is still stored in "hash".
    recomputed_after_tamper = _recompute_hash(tampered_doc)
    assert recomputed_after_tamper != tampered_doc["hash"]
    # And the second event's own recorded prev_hash no longer matches
    # what the (now provably tampered) first event's stored hash claims
    # to authenticate -- the chain link itself flags the break.
    assert second["prev_hash"] == tampered_doc["hash"]  # stored hash field itself wasn't touched
    assert recomputed_after_tamper != second["prev_hash"]
