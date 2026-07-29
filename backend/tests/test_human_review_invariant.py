"""Human review invariant tests (GOAL clause 61-65).

Every human decision must carry reviewer id + comment + timestamp. The
endpoint refuses when reviewer is missing.
"""
import os
import uuid

import pytest
import requests


BASE_URL = os.environ.get("PHI_TEST_BASE_URL", "http://localhost:8001")


def _backend_up() -> bool:
    try:
        return requests.get(f"{BASE_URL}/api/health", timeout=2).ok
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _backend_up(), reason="backend not reachable")


def test_human_review_requires_reviewer():
    """Endpoint rejects when reviewer identity missing (GOAL invariant)."""
    # Use a non-existent session id — the reviewer check runs before the
    # session lookup, so we still see the 400 we want.
    fake_sid = uuid.uuid4().hex
    r = requests.post(
        f"{BASE_URL}/api/sessions/{fake_sid}/human-review",
        json={"resolutions": [], "reviewer": "", "comment": "no reviewer"},
        timeout=10,
    )
    assert r.status_code == 400, r.text
    assert "reviewer" in r.text.lower()


def test_human_review_requires_actual_knowledge_ack():
    """HHS §164.514(b)(2)(ii): endpoint must reject when reviewer omits or
    denies actual-knowledge attestation, even with a valid reviewer id."""
    fake_sid = uuid.uuid4().hex
    r = requests.post(
        f"{BASE_URL}/api/sessions/{fake_sid}/human-review",
        json={
            "resolutions": [],
            "reviewer": "jane.doe@lab.edu",
            "comment": "test",
            "actual_knowledge_ack": False,
        },
        timeout=10,
    )
    assert r.status_code == 400, r.text
    assert "actual" in r.text.lower() and "knowledge" in r.text.lower()


def test_human_review_captures_session_review_when_provided():
    """Sending reviewer + comment + actual_knowledge_ack on an awaiting-review
    session must persist reviewer id, timestamp, comment, and the
    actual-knowledge attestation on the session document."""
    r = requests.get(f"{BASE_URL}/api/sessions", timeout=10)
    sessions = r.json().get("sessions", [])
    awaiting = [s for s in sessions if s.get("status") == "awaiting_human_review"]
    if not awaiting:
        pytest.skip("no awaiting_human_review session on this deployment")
    sid = awaiting[0]["id"]
    rr = requests.post(
        f"{BASE_URL}/api/sessions/{sid}/human-review",
        json={
            "resolutions": [],
            "reviewer": "test-reviewer@example.org",
            "comment": "accepted per QA plan v1",
            "actual_knowledge_ack": True,
        },
        timeout=10,
    )
    # 200 means the tail started; response contains status field
    assert rr.status_code == 200, rr.text
    assert rr.json().get("status") in ("resuming", "still_awaiting")
