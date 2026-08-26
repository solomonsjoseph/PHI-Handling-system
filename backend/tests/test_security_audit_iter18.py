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



# ---- D15 4b: bounded process-local maps ---------------------------------


@pytest.mark.asyncio
async def test_rate_buckets_evict_least_recently_used_past_the_key_cap(monkeypatch):
    import server as srv
    from phi_core.control import limits

    monkeypatch.setattr(srv, "_RATE_BUCKETS", srv.collections.OrderedDict())
    monkeypatch.setattr(limits, "MAX_RATE_BUCKET_KEYS", 3)
    # Isolate the LRU/cleanup mechanics from principal resolution (dev
    # mode resolves a soft principal even with no token/cookie).
    monkeypatch.setattr(srv, "_rate_limit_identity", lambda request: request.host)

    class _Req:
        def __init__(self, host):
            self.host = host

    dep = srv.rate_limited("probe", limit=100, window_seconds=60)
    for host in ("h1", "h2", "h3", "h4"):
        await dep(_Req(host))

    # 4 distinct identities hit a cap of 3: the least-recently-used (h1)
    # is evicted, the 3 most recent survive.
    assert len(srv._RATE_BUCKETS) == 3
    assert "probe:h1" not in srv._RATE_BUCKETS
    assert "probe:h4" in srv._RATE_BUCKETS


@pytest.mark.asyncio
async def test_rate_bucket_key_is_removed_once_its_window_empties(monkeypatch):
    import server as srv

    monkeypatch.setattr(srv, "_RATE_BUCKETS", srv.collections.OrderedDict())
    monkeypatch.setattr(srv, "_rate_limit_identity", lambda request: "h1")
    monkeypatch.setattr(srv.time, "monotonic", lambda: 1000.0)

    dep = srv.rate_limited("probe", limit=100, window_seconds=1)
    await dep(object())
    assert "probe:h1" in srv._RATE_BUCKETS

    # Advance well past the window: the next request's cleanup pass finds
    # the bucket empty and removes the key entirely rather than leaving a
    # dead empty-list entry behind.
    monkeypatch.setattr(srv.time, "monotonic", lambda: 2000.0)
    await dep(object())
    assert list(srv._RATE_BUCKETS["probe:h1"]) == [2000.0]  # re-created fresh, not stale


def test_chatgpt_logins_prune_expired_entries_and_cap_the_rest(monkeypatch):
    import time as time_mod

    import server as srv
    from phi_core import chatgpt_auth
    from phi_core.control import limits

    monkeypatch.setattr(srv, "_chatgpt_logins", {})
    monkeypatch.setattr(limits, "MAX_CHATGPT_LOGINS", 2)
    now = time_mod.time()

    def _login(started_at):
        return chatgpt_auth.DeviceLogin(
            device_auth_id="d", user_code="u", verify_url="v", interval_s=5,
            started_at=started_at, status="pending",
        )

    srv._chatgpt_logins["expired"] = _login(now - chatgpt_auth.DEVICE_CODE_EXPIRES_IN_S - 10)
    srv._chatgpt_logins["old"] = _login(now - 5)
    srv._chatgpt_logins["new"] = _login(now)

    srv._prune_chatgpt_logins()

    assert "expired" not in srv._chatgpt_logins  # past DEVICE_CODE_EXPIRES_IN_S
    # After expiry cleanup, 2 survivors sit exactly at MAX_CHATGPT_LOGINS
    # (2): pruning also evicts the oldest of those to make room for the
    # insertion it is always called right before.
    assert len(srv._chatgpt_logins) == 1
    assert "new" in srv._chatgpt_logins
    assert "old" not in srv._chatgpt_logins

# ---- SEC-002 / D15: SSE fan-out lifecycle -------------------------------


def test_unsubscribe_drops_the_run_bucket_when_the_last_subscriber_leaves():
    import server as srv
    sub = srv._event_broker.subscribe("run-s1")
    assert srv._event_broker.subscriber_count("run-s1") == 1
    srv._event_broker.unsubscribe(sub)
    assert srv._event_broker.subscriber_count("run-s1") == 0, "bucket leaked after last subscriber"


def test_unsubscribe_keeps_the_bucket_when_other_subscribers_remain():
    import server as srv
    first = srv._event_broker.subscribe("run-s2")
    second = srv._event_broker.subscribe("run-s2")
    srv._event_broker.unsubscribe(first)
    assert srv._event_broker.subscriber_count("run-s2") == 1, "bucket prematurely freed"
    # cleanup
    srv._event_broker.unsubscribe(second)
    assert srv._event_broker.subscriber_count("run-s2") == 0


def test_settled_statuses_include_terminal_pipeline_states():
    """SEC-002: streams for settled sessions must be refused (409). This
    frozen set is the enforcement key -- adding/removing values changes
    what states can still accept new stream subscribers."""
    import server as srv
    for st in ("complete", "failed", "cancelled",
               "intake_failed", "awaiting_human_review"):
        assert st in srv._SETTLED_STATUSES


def test_max_stream_subscribers_cap_is_bounded():
    """SEC-002: the per-run subscriber cap must be small and finite
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
    monkeypatch.setattr(srv, "get_db", lambda: _StubDB({"id": "sid2", "status": "reading", "_pipeline_run_id": "run-x"}))
    subs = [srv._event_broker.subscribe("run-x") for _ in range(srv._MAX_STREAM_SUBSCRIBERS_PER_SESSION)]
    try:
        with pytest.raises(HTTPException) as excinfo:
            await srv.session_stream("sid2")
        assert excinfo.value.status_code == 429
    finally:
        for sub in subs:
            srv._event_broker.unsubscribe(sub)
