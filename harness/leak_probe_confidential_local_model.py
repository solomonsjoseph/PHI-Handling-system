"""Leak probe: does raw synthetic PHI reach the local-model transport unscanned?

Run: `source .venv/bin/activate && python3 harness/leak_probe_confidential_local_model.py`

Reuses the exact task-construction shapes from tests/test_model_routing.py
(ConfidentialHeaderTask / SupportSignalTask / payload builders already proven
to pass the router's own verification checks in the passing test suite) so
this probe exercises the REAL production code path, not a stub.

Instrumentation:
  1. Wraps `phi_gate_check` (module-level name inside model_routing) with a
     spy that records every call.
  2. Replaces `_local_completion_transport` with a capture stub that records
     the exact prompt string handed to the local-model transport, then
     returns a valid contract response so the call completes normally
     (proving this is a live leak, not an incidental exception path).
  3. Plants a unique, unambiguous synthetic PHI marker (SSN-shaped: matches
     phi_patterns.SSN exactly -- `\\b\\d{3}-\\d{2}-\\d{4}\\b`) in the raw
     input fields documented as "not headers-only"
     (ConfidentialHeaderTask.samples, MatchedSupportCell.value).
  4. Reports, per call site: was phi_gate_check invoked at all, and did the
     raw marker reach the transport unredacted.

Finding reproduced this session (2026-07-21): both probed call sites show
gate_invocations=0 and marker_reached_transport_unscanned=True. A control
call to the SAME gate function (`ModelTaskRouter._gate_ordinary_segments`,
the function the NON_CONFIDENTIAL/ordinary path actually calls) on the
identical marker raises `ModelResponseError: prompt_gate_blocked`
(`phi_gate: BLOCK -- findings=['SSN']`), proving the detector works and
would have caught this content -- it simply is never invoked on the
CONFIDENTIAL branch. See docs/LEAK_TEST_REPORT_2026-07-21.md.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import phi_engine.security.model_routing as routing  # noqa: E402
from phi_engine.pipeline.dependencies import Sensitivity  # noqa: E402
from phi_engine.security.phi_review import Action  # noqa: E402

MARKER_SSN = "078-05-1120"  # matches phi_patterns.SSN exactly: \b\d{3}-\d{2}-\d{4}\b
MARKER_TAG = f"LEAK-PROBE-MARKER-SSN-{MARKER_SSN}"

DATASET_ID = "a_" + "1" * 32
SUPPORT_ID = "a_" + "2" * 32
HEADER_ID = "h_" + "3" * 24
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def candidate():
    return routing.CandidateRuleView(
        rule_id="usa_rule", action=Action.DROP, citation="45 CFR 164.514", jurisdictions=("USA",)
    )


def header_task(samples):
    return routing.ConfidentialHeaderTask(
        dataset_artifact_id=DATASET_ID,
        dataset_sha256=SHA_A,
        header_id=HEADER_ID,
        raw_header="participant_name",
        samples=samples,
        candidate_rules=(candidate(),),
        evidence=routing.ResolutionEvidence(profile_input_sha256=SHA_C),
    )


def header_payload(**overrides):
    payload = {
        "dataset_artifact_id": DATASET_ID,
        "header_id": HEADER_ID,
        "inferred_variable_type": "identifier",
        "action": "drop",
        "matched_rule_id": "usa_rule",
        "rule_citation": "45 CFR 164.514",
        "jurisdictions": ["USA"],
        "confidence": 0.99,
    }
    payload.update(overrides)
    return payload


def support_task(cells, *, sensitivity=Sensitivity.CONFIDENTIAL):
    row = routing.MatchedSupportRow(
        support_artifact_id=SUPPORT_ID,
        support_sha256=SHA_B,
        sheet_index=0,
        table_index=0,
        row_index=0,
        matched_column_indices=(0,),
        cells=cells,
    )
    return routing.SupportSignalTask(
        dataset_artifact_id=DATASET_ID,
        dataset_sha256=SHA_A,
        header_ids=(HEADER_ID,),
        support_artifact_id=SUPPORT_ID,
        support_sha256=SHA_B,
        normalized_support_sha256=SHA_C,
        sensitivity=sensitivity,
        dependency_decision_id="dd_" + "4" * 32,
        matched_rows=(row,),
        candidate_rules=(candidate(),),
    )


def signal_payload(**overrides):
    payload = {
        "dataset_artifact_id": DATASET_ID,
        "header_id": HEADER_ID,
        "support_artifact_id": SUPPORT_ID,
        "support_sha256": SHA_B,
        "signal_type": "definition_binding",
        "action": "drop",
        "matched_rule_id": "usa_rule",
        "rule_citation": "45 CFR 164.514",
        "jurisdictions": ["USA"],
        "transform_requirement_id": None,
        "transform_id": None,
        "confidence": 0.99,
    }
    payload.update(overrides)
    return payload


def run_probe(name, fn):
    gate_calls = []
    captured_prompts = []

    real_gate_check = routing.phi_gate_check

    def spy_gate_check(texts):
        gate_calls.append(tuple(texts) if not isinstance(texts, str) else (texts,))
        return real_gate_check(texts)

    routing.phi_gate_check = spy_gate_check
    try:
        _, transport_error = fn(captured_prompts)
    finally:
        routing.phi_gate_check = real_gate_check

    marker_in_prompt = any(MARKER_TAG in p for p in captured_prompts)
    return {
        "probe": name,
        "gate_invocations": len(gate_calls),
        "prompts_sent_to_local_transport": len(captured_prompts),
        "marker_reached_transport_unscanned": marker_in_prompt and len(gate_calls) == 0,
        "transport_call_succeeded": transport_error is None,
        "transport_error": transport_error,
        "captured_prompt_excerpt": (
            next(p for p in captured_prompts if MARKER_TAG in p)[:400]
            if marker_in_prompt else None
        ),
    }


def probe_resolve_confidential_header(captured_prompts):
    def local_complete(self, prompt):
        captured_prompts.append(prompt)
        return json.dumps(header_payload())

    routing._local_completion_transport = local_complete
    try:
        task = header_task(samples=(MARKER_TAG, "Bob"))
        result = routing.ModelTaskRouter().resolve_confidential_header(task)
        return result, None
    except Exception as exc:  # noqa: BLE001 -- probe wants to observe, not raise
        return None, f"{type(exc).__name__}: {exc}"


def probe_extract_support_signals_confidential(captured_prompts):
    def local_complete(self, prompt):
        captured_prompts.append(prompt)
        return json.dumps([signal_payload()])

    routing._local_completion_transport = local_complete
    try:
        cells = (
            routing.MatchedSupportCell(0, "participant_ssn"),
            routing.MatchedSupportCell(1, MARKER_TAG),
        )
        task = support_task(cells, sensitivity=Sensitivity.CONFIDENTIAL)
        result = routing.ModelTaskRouter().extract_support_signals(task)
        return result, None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def control_gate_blocks_same_marker() -> dict:
    """Same marker, through the gate function the ORDINARY path calls."""
    try:
        routing.ModelTaskRouter._gate_ordinary_segments(MARKER_TAG)
        return {"blocked": False, "detail": "did not raise (unexpected)"}
    except Exception as exc:  # noqa: BLE001
        return {"blocked": True, "detail": f"{type(exc).__name__}: {exc}"}


if __name__ == "__main__":
    report = {
        "marker_planted": MARKER_TAG,
        "marker_matches_ssn_pattern": True,
        "probes": [
            run_probe(
                "resolve_confidential_header (model_routing.py:968-982)",
                probe_resolve_confidential_header,
            ),
            run_probe(
                "extract_support_signals CONFIDENTIAL branch (model_routing.py:984-1008)",
                probe_extract_support_signals_confidential,
            ),
        ],
        "control_same_marker_through_wired_gate": control_gate_blocks_same_marker(),
    }
    print(json.dumps(report, indent=2))
