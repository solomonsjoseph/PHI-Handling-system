"""Security-audit iteration_18 regression tests.

Locks the SEC-002 (SSE queue leak / DoS) and SEC-003 (scrubber missing
identifier shapes) fixes so they don't silently regress.
"""
from __future__ import annotations

import pytest


# ---- SEC-003: scrubber breadth ----------------------------------------


def test_scrub_persisted_text_redacts_iso_dates():
    from phi_core.security import scrub_persisted_text
    out = scrub_persisted_text("Enrolled 2024-05-20 after screening.")
    assert "2024-05-20" not in out
    assert "[C]" in out


def test_scrub_persisted_text_redacts_us_and_named_dates():
    from phi_core.security import scrub_persisted_text
    assert "5/20/2024" not in scrub_persisted_text("visit 5/20/2024")
    assert "20-May-2024" not in scrub_persisted_text("dose on 20-May-2024")
    assert "May 20, 2024" not in scrub_persisted_text("received May 20, 2024 email")


def test_scrub_persisted_text_redacts_age_over_89():
    from phi_core.security import scrub_persisted_text
    out = scrub_persisted_text("Patient age of 93 with hypertension.")
    assert "93" not in out.split("hypertension")[0]
    assert "[C-age]" in out or "[C]" in out


def test_scrub_persisted_text_does_not_redact_clinical_heart_rate():
    """Clinical values like heart rate (60-120) must survive."""
    from phi_core.security import scrub_persisted_text
    out = scrub_persisted_text("Heart rate 82 bpm, glucose 105 mg/dL.")
    assert "82" in out
    assert "105" in out


def test_scrub_persisted_text_redacts_street_addresses():
    from phi_core.security import scrub_persisted_text
    out = scrub_persisted_text("Lives at 1234 Main Street Apt 5.")
    assert "1234 Main" not in out
    assert "[B]" in out


def test_scrub_persisted_text_redacts_mrn_shapes():
    from phi_core.security import scrub_persisted_text
    for probe in ("MRN 1234567", "MRN:HP12345678", "Account ACCT-654321"):
        out = scrub_persisted_text(probe)
        assert "[H]" in out, f"failed to redact {probe!r}: {out!r}"


def test_scrub_persisted_text_redacts_urls_and_ips():
    from phi_core.security import scrub_persisted_text
    out = scrub_persisted_text("Uploaded to https://portal.example.com/u/abcxyz via 10.20.30.40.")
    assert "portal.example.com" not in out
    assert "10.20.30.40" not in out
    assert "[N]" in out
    assert "[O]" in out


# ---- SEC-002: SSE queue lifecycle -------------------------------------


def test_release_stream_drops_queue_when_last_subscriber_leaves():
    import server as srv
    # simulate: one subscriber joins, then leaves
    srv._progress_queues["s1"] = srv.asyncio.Queue()
    srv._progress_subscribers["s1"] = 1
    srv._release_stream("s1")
    assert "s1" not in srv._progress_queues, "queue leaked after last subscriber"
    assert "s1" not in srv._progress_subscribers


def test_release_stream_keeps_queue_when_other_subscribers_remain():
    import server as srv
    srv._progress_queues["s2"] = srv.asyncio.Queue()
    srv._progress_subscribers["s2"] = 2
    srv._release_stream("s2")
    assert "s2" in srv._progress_queues, "queue prematurely freed"
    assert srv._progress_subscribers["s2"] == 1
    # cleanup
    srv._release_stream("s2")
    assert "s2" not in srv._progress_queues


def test_settled_statuses_include_terminal_pipeline_states():
    """SEC-002: streams for settled sessions must be refused (409). This
    frozen set is the enforcement key -- adding/removing values changes
    what states can still accept new stream subscribers."""
    import server as srv
    for st in ("complete", "failed", "cancelled",
               "intake_failed", "awaiting_human_review"):
        assert st in srv._SETTLED_STATUSES


def test_max_stream_subscribers_cap_is_bounded():
    """SEC-002: the per-session subscriber cap must be small and finite
    so an attacker cannot open thousands of streams."""
    import server as srv
    assert 1 <= srv._MAX_STREAM_SUBSCRIBERS_PER_SESSION <= 16


class _StubDB:
    def __init__(self, doc):
        self._doc = doc
        self.sessions = self

    async def find_one(self, *_a, **_kw):
        return self._doc


@pytest.mark.asyncio
async def test_stream_refuses_settled_session(monkeypatch):
    import server as srv
    from fastapi import HTTPException
    monkeypatch.setattr(srv, "get_db", lambda: _StubDB({"id": "sid", "status": "complete"}))
    with pytest.raises(HTTPException) as excinfo:
        await srv.session_stream("sid")
    assert excinfo.value.status_code == 409
    assert "settled" in excinfo.value.detail.lower()


@pytest.mark.asyncio
async def test_stream_refuses_when_cap_reached(monkeypatch):
    import server as srv
    from fastapi import HTTPException
    monkeypatch.setattr(srv, "get_db", lambda: _StubDB({"id": "sid2", "status": "reading"}))
    srv._progress_subscribers["sid2"] = srv._MAX_STREAM_SUBSCRIBERS_PER_SESSION
    try:
        with pytest.raises(HTTPException) as excinfo:
            await srv.session_stream("sid2")
        assert excinfo.value.status_code == 429
    finally:
        srv._progress_subscribers.pop("sid2", None)
