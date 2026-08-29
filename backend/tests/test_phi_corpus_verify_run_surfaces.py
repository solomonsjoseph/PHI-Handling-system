"""Wave R-d Part 1: ``phi_corpus.verify.scan_run_surfaces_for_leaks`` --
the section-72 forbidden surfaces ``scan_exports_for_leaks`` does not
cover: trace_events (including status_text), workflow_runs.opaque_map,
the legacy agent_log collection, HandoffEnvelope payloads (folded into the
trace_events scan -- see the function's docstring), the learning store,
research queries, errors, and ZIP metadata.

One test per surface: plant a canary literal directly into that surface's
storage, run the scanner, and assert it is detected with the correct
surface label -- and that the raw literal never appears anywhere in the
returned report (only ``mask()``-ed samples).
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from phi_core.control.opaque import OpaqueMap
from phi_core.control.store import MemoryControlStore
from phi_corpus.verify import scan_run_surfaces_for_leaks

RUN_ID = "surfaces-run-" + "r" * 18
SESSION_ID = "surfaces-session-" + "s" * 14


def _ground_truth(literal: str, *, plant_id: str = "p1") -> dict:
    return {
        "planted": [
            {
                "plant_id": plant_id,
                "tier": "l1",
                "file_name": "notes.csv",
                "column": "note",
                "hipaa_category": "F",
                "edge_case_tag": "",
                "leak_literals": [literal],
            }
        ]
    }


def _assert_never_raw(report: dict, literal: str) -> None:
    blob = str(report)
    assert literal not in blob
    assert literal.lower() not in blob.lower()


@pytest.mark.asyncio
async def test_clean_run_reports_no_hits_and_scans_every_surface() -> None:
    store = MemoryControlStore()
    report = await scan_run_surfaces_for_leaks(
        store, _ground_truth("ZZZNOTHINGMATCHESTHIS"), run_id=RUN_ID, session_id=SESSION_ID,
    )
    assert report["status"] == "clean"
    assert report["hit_count"] == 0
    assert "trace_events" in report["surfaces_scanned"]
    assert "workflow_runs.opaque_map" in report["surfaces_scanned"]
    assert "learning_proposals" in report["surfaces_scanned"]
    assert "web_cache" in report["surfaces_scanned"]
    assert "work_items.error_category" in report["surfaces_scanned"]


@pytest.mark.asyncio
async def test_detects_hit_in_trace_events_status_text() -> None:
    literal = "ZZZTRACESTATUSCANARY1"
    store = MemoryControlStore()
    await store.insert("trace_events", {
        "event_id": "evt-1", "run_id": RUN_ID, "seq": 1, "session_id": SESSION_ID,
        "phase": "judge", "status_text": f"decision note mentions {literal} inline",
        "payload": {}, "retry_category": "", "sanitized_rationale": "",
    })

    report = await scan_run_surfaces_for_leaks(store, _ground_truth(literal), run_id=RUN_ID, session_id=SESSION_ID)

    assert report["status"] == "leaked"
    hit = next(h for h in report["hits"] if h["surface"] == "trace_events")
    assert hit["plant_id"] == "p1"
    assert hit["location"] == "evt-1"
    _assert_never_raw(report, literal)


@pytest.mark.asyncio
async def test_detects_hit_in_trace_events_payload_field() -> None:
    """The write-time scrub (trace_sanitizer) is signature-based, not
    literal-aware -- a canary token embedded in a nested payload value must
    still be caught by this independent scan."""
    literal = "ZZZPAYLOADFIELDCANARY2"
    store = MemoryControlStore()
    await store.insert("trace_events", {
        "event_id": "evt-2", "run_id": RUN_ID, "seq": 2, "session_id": SESSION_ID,
        "phase": "executor", "status_text": "",
        "payload": {"nested": {"detail": f"value was {literal}"}},
    })

    report = await scan_run_surfaces_for_leaks(store, _ground_truth(literal), run_id=RUN_ID, session_id=SESSION_ID)

    assert report["status"] == "leaked"
    assert any(h["surface"] == "trace_events" and h["location"] == "evt-2" for h in report["hits"])
    _assert_never_raw(report, literal)


@pytest.mark.asyncio
async def test_detects_hit_in_workflow_runs_opaque_map_after_decryption() -> None:
    """opaque_map values are Fernet-encrypted at rest; the scanner must
    decrypt before matching or the ciphertext never substring-matches."""
    literal = "ZZZOPAQUEMAPCANARY3"
    mapping: dict[str, str] = {}
    OpaqueMap(RUN_ID, mapping).to_opaque("note", f"context containing {literal} verbatim")
    store = MemoryControlStore()
    await store.insert("workflow_runs", {"run_id": RUN_ID, "opaque_map": mapping})

    report = await scan_run_surfaces_for_leaks(store, _ground_truth(literal), run_id=RUN_ID, session_id=SESSION_ID)

    assert report["status"] == "leaked"
    hit = next(h for h in report["hits"] if h["surface"] == "workflow_runs.opaque_map")
    assert hit["location"] == RUN_ID
    _assert_never_raw(report, literal)
    # And the raw ciphertext blob itself never leaks either.
    assert list(mapping.values())[0] not in str(report)


@pytest.mark.asyncio
async def test_detects_hit_in_legacy_agent_log_collection() -> None:
    literal = "ZZZAGENTLOGCANARY4"
    store = MemoryControlStore()
    await store.insert("agent_log", {
        "session_id": SESSION_ID, "agent": "Judge", "phase": "propose",
        "payload": {"reason": f"proposed because {literal}"},
    })

    report = await scan_run_surfaces_for_leaks(store, _ground_truth(literal), run_id=RUN_ID, session_id=SESSION_ID)

    assert report["status"] == "leaked"
    hit = next(h for h in report["hits"] if h["surface"] == "agent_log")
    assert hit["location"] == SESSION_ID
    _assert_never_raw(report, literal)


@pytest.mark.asyncio
async def test_no_session_id_skips_the_legacy_agent_log_surface_without_raising() -> None:
    store = MemoryControlStore()
    await store.insert("agent_log", {"session_id": "other-session", "phase": "propose"})

    report = await scan_run_surfaces_for_leaks(store, _ground_truth("ZZZWHATEVER1"), run_id=RUN_ID, session_id="")

    assert "agent_log" not in report["surfaces_scanned"]
    assert report["status"] == "clean"


@pytest.mark.asyncio
async def test_detects_hit_in_handoff_envelope_payload_via_trace_events_phase() -> None:
    """HandoffEnvelope.payload itself is never persisted anywhere
    (control/handoff.py: "the handoff's own payload never enters the
    trace"); the only place a leak on this surface could physically
    manifest is the trace_events row a handoff attempt produces. A row
    with phase == "handoff" gets its own surface label rather than being
    folded anonymously into "trace_events", so a future regression that
    started copying the envelope payload into trace is attributed
    correctly, not merged into general trace noise."""
    literal = "ZZZHANDOFFCANARY5"
    store = MemoryControlStore()
    await store.insert("trace_events", {
        "event_id": "evt-handoff-1", "run_id": RUN_ID, "seq": 3, "session_id": SESSION_ID,
        "phase": "handoff", "status_text": "",
        "payload": {"handoff_id": "h1", "sender": "Judge", "recipient": "Schema",
                     "allowed": False, "reason": f"blocked: contains {literal}"},
    })

    report = await scan_run_surfaces_for_leaks(store, _ground_truth(literal), run_id=RUN_ID, session_id=SESSION_ID)

    assert report["status"] == "leaked"
    hit = next(h for h in report["hits"] if h["surface"] == "handoff_envelope_payload")
    assert hit["location"] == "evt-handoff-1"
    assert not any(h["surface"] == "trace_events" for h in report["hits"])
    _assert_never_raw(report, literal)


@pytest.mark.asyncio
async def test_real_handoff_gateway_never_persists_the_envelope_payload() -> None:
    """Regression proof for the invariant the surface-4 coverage above
    relies on: drive the real, unmodified ``HandoffGateway.handoff`` with a
    canary embedded in the envelope's own payload, then confirm this
    scanner finds ZERO hits over the resulting trace_events row -- proving
    the payload really never reaches persisted storage, not merely that
    the scanner would catch it if it did."""
    from phi_core.control.handoff import JUDGE, SCHEMA, HandoffGateway, SchemaQuestion
    from phi_core.control.records import HandoffEnvelope

    literal = "ZZZREALHANDOFFCANARY6"
    store = MemoryControlStore()
    gateway = HandoffGateway(store, session_id=SESSION_ID)
    envelope = HandoffEnvelope(
        run_id=RUN_ID, sender=JUDGE, recipient=SCHEMA, data_class="restricted_metadata",
        payload=SchemaQuestion(column=literal, file_id="f1").model_dump(),
    )

    await gateway.handoff(envelope)

    report = await scan_run_surfaces_for_leaks(store, _ground_truth(literal), run_id=RUN_ID, session_id=SESSION_ID)
    assert report["status"] == "clean"
    assert report["hit_count"] == 0


@pytest.mark.asyncio
async def test_detects_hit_in_learning_store_proposal_rationale() -> None:
    literal = "ZZZLEARNINGCANARY7"
    store = MemoryControlStore()
    await store.insert("learning_proposals", {
        "proposal_id": "prop-1", "kind": "prompt", "target": "Judge",
        "baseline_version": "v1", "proposed_version": "v2",
        "redacted_input_digest": "deadbeef",
        "rationale": f"proposed after observing {literal} in evaluation output",
        "state": "proposed",
    })

    report = await scan_run_surfaces_for_leaks(store, _ground_truth(literal), run_id=RUN_ID, session_id=SESSION_ID)

    assert report["status"] == "leaked"
    hit = next(h for h in report["hits"] if h["surface"] == "learning_store")
    assert hit["location"] == "prop-1"
    _assert_never_raw(report, literal)


@pytest.mark.asyncio
async def test_detects_hit_in_research_queries_web_cache_topic() -> None:
    literal = "ZZZRESEARCHQUERYCANARY8"
    store = MemoryControlStore()
    await store.insert("web_cache", {
        "topic": f"jurisdictional rule for {literal}", "jurisdiction": "us",
        "content": "some cached research content", "source": "web_search",
        "evidence_state": "UNVERIFIED", "policy_version": "policy/1", "schema_version": 1,
    })

    report = await scan_run_surfaces_for_leaks(store, _ground_truth(literal), run_id=RUN_ID, session_id=SESSION_ID)

    assert report["status"] == "leaked"
    hit = next(h for h in report["hits"] if h["surface"] == "research_queries")
    assert hit["location"] == "us"
    _assert_never_raw(report, literal)


@pytest.mark.asyncio
async def test_detects_hit_in_work_items_error_category() -> None:
    literal = "ZZZERRORCANARY9"
    store = MemoryControlStore()
    await store.insert("work_items", {
        "task_id": "task-1", "run_id": RUN_ID, "session_id": SESSION_ID,
        "worker": "Judge", "task_type": "judge_run", "state": "failed",
        "idempotency_key": "idem-1", "error_category": f"agent_crashed:{literal}",
    })

    report = await scan_run_surfaces_for_leaks(store, _ground_truth(literal), run_id=RUN_ID, session_id=SESSION_ID)

    assert report["status"] == "leaked"
    hit = next(h for h in report["hits"] if h["surface"] == "errors")
    assert hit["location"] == "task-1"
    _assert_never_raw(report, literal)


@pytest.mark.asyncio
async def test_detects_hit_in_zip_metadata_filename_and_comment(tmp_path: Path) -> None:
    filename_literal = "ZZZZIPFILENAMECANARYA"
    comment_literal = "ZZZZIPCOMMENTCANARYB"
    zip_path = tmp_path / "bundle.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.comment = f"archive built with {comment_literal}".encode("utf-8")
        zf.writestr(f"datasets/{filename_literal}/data.csv", "col\nvalue\n")

    ground_truth = {
        "planted": [
            {"plant_id": "p1", "tier": "l1", "file_name": "f", "column": "c",
             "hipaa_category": "F", "edge_case_tag": "", "leak_literals": [filename_literal]},
            {"plant_id": "p2", "tier": "l1", "file_name": "f", "column": "c",
             "hipaa_category": "F", "edge_case_tag": "", "leak_literals": [comment_literal]},
        ]
    }
    store = MemoryControlStore()

    report = await scan_run_surfaces_for_leaks(
        store, ground_truth, run_id=RUN_ID, session_id=SESSION_ID, zip_path=str(zip_path),
    )

    assert report["status"] == "leaked"
    surfaces = {h["location"] for h in report["hits"] if h["surface"] == "zip_metadata"}
    assert "zip_entry_filename" in surfaces
    assert "archive_comment" in surfaces
    _assert_never_raw(report, filename_literal)
    _assert_never_raw(report, comment_literal)


@pytest.mark.asyncio
async def test_zip_metadata_surface_skipped_without_zip_path() -> None:
    store = MemoryControlStore()
    report = await scan_run_surfaces_for_leaks(
        store, _ground_truth("ZZZIRRELEVANT1"), run_id=RUN_ID, session_id=SESSION_ID,
    )
    assert "zip_metadata" not in report["surfaces_scanned"]
