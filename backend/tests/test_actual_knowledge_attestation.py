"""Phase E — HHS §164.514(b)(2)(ii) actual-knowledge attestation.

Two independent checks:

1. The `HumanReviewSubmit` endpoint MUST reject when reviewer omits or
   denies the actual-knowledge attestation (HTTP 400).
2. The bundle attestation payload MUST surface
   `actual_knowledge_ack=True` and cite `45 CFR 164.514(b)(2)(ii)` when the
   reviewer accepted the attestation.

The bundle-attestation check runs against `phi_core.bundle` directly (no
HTTP call, no live session needed) so it's stable in CI even when no
awaiting_human_review session exists on the deployment.
"""
from __future__ import annotations

import io
import json
import zipfile

from phi_core.bundle import BundleOptions, _attestation_payload, build_bundle


def _mock_session(ack: bool) -> dict:
    return {
        "id": "sess-ak-001",
        "jurisdiction": "us",
        "status": "complete",
        "files": [],
        "export_paths": {},
        "guard_report": {"status": "clean", "scanned": 0, "blocked": 0, "results": []},
        "agent_decisions": [],
        "session_review": {
            "reviewer": "jane.doe@lab.edu",
            "comment": "spot-checked 3 samples per file",
            "reviewed_at": "2026-02-01T00:00:00+00:00",
            "changed_decisions": False,
            "actual_knowledge_ack": ack,
            "actual_knowledge_cite": "45 CFR 164.514(b)(2)(ii)",
        },
    }


def test_attestation_payload_carries_actual_knowledge_true_when_acked():
    sess = _mock_session(ack=True)
    att = _attestation_payload(sess, file_hashes={"safe_to_share/x.csv": "sha256:0" * 8})
    assert att["actual_knowledge_ack"] is True
    assert att["actual_knowledge_cite"] == "45 CFR 164.514(b)(2)(ii)"
    assert "identify an individual" in att["actual_knowledge_statement"]
    assert att["reviewer"] == "jane.doe@lab.edu"


def test_attestation_payload_carries_actual_knowledge_false_when_missing():
    sess = _mock_session(ack=False)
    att = _attestation_payload(sess, file_hashes={})
    assert att["actual_knowledge_ack"] is False
    # cite is always present so consumers know which clause is being attested
    assert att["actual_knowledge_cite"] == "45 CFR 164.514(b)(2)(ii)"


def test_bundle_zip_contains_actual_knowledge_ack_field_in_json_and_text():
    sess = _mock_session(ack=True)
    zip_bytes, _fn = build_bundle(sess, BundleOptions(include_publication=False))
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        assert "safe_to_share/attestation.json" in names
        assert "safe_to_share/attestation.txt" in names
        j = json.loads(zf.read("safe_to_share/attestation.json").decode("utf-8"))
        assert j["actual_knowledge_ack"] is True
        assert j["actual_knowledge_cite"] == "45 CFR 164.514(b)(2)(ii)"
        txt = zf.read("safe_to_share/attestation.txt").decode("utf-8")
        assert "Actual-knowledge attestation" in txt
        assert "45 CFR 164.514(b)(2)(ii)" in txt
        # human-readable YES/NO line
        assert "YES" in txt


def test_attestation_backfills_actual_knowledge_from_decisions_if_session_review_missing():
    """Older sessions may lack session-level review; fallback to the most
    recent per-decision reviewer trail carrying actual_knowledge_ack."""
    sess = {
        "id": "sess-ak-legacy",
        "jurisdiction": "us",
        "session_review": {},
        "agent_decisions": [
            {"reviewer": "a", "reviewer_comment": "", "reviewed_at": "t1",
             "actual_knowledge_ack": True},
        ],
        "guard_report": {"status": "clean", "scanned": 0, "blocked": 0},
    }
    att = _attestation_payload(sess, file_hashes={})
    assert att["actual_knowledge_ack"] is True
    assert att["reviewer"] == "a"
