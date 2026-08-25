"""V6: offline-verifiable subset of the hardening gate checks.

Every check here is a single observable assertion runnable without Mongo,
Docker, or a live LLM credential, matching the rest of the suite's
no-TestClient-no-Mongo-double convention. Checks that genuinely need live
infrastructure (compose up, real Mongo ping, a browser cookie jar) are
listed in the module docstring below as NOT covered here, matching the
plan's "offline-verifiable subset" scope.

Checks intentionally NOT covered here (need live infra, exercised
manually / in a staging environment instead):
  - `docker compose up` end-to-end from a clean clone.
  - `/api/health` `checks.mongo` true against a real Mongo instance.
  - `npm ci` reproducibility (already exercised directly during 4.22/4.15
    development; not re-run here since it needs a network-reachable npm
    registry).
"""
from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Ownership: a wrong-owner request 404s across every owner-scoped read route
# named in the plan's V6 checklist.
# ---------------------------------------------------------------------------

class _NoMatchDB:
    """Every owner-filtered read misses, the way a real Mongo query does
    when `owner` in the filter doesn't match the document's real owner."""
    def __init__(self):
        self.sessions = self

    async def find_one(self, *_args, **_kwargs):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("call", [
    lambda srv: srv.session_get("sid", principal="mallory"),
    lambda srv: srv.session_export("sid", "file1", principal="mallory"),
    lambda srv: srv.session_bundle("sid", publication=False, attestation_pdf=False, principal="mallory"),
    lambda srv: srv.corpus_study_benchmark("sid", principal="mallory"),
    lambda srv: srv.corpus_study_benchmark_download("sid", principal="mallory"),
])
async def test_owner_mismatch_returns_404_across_every_owner_scoped_read_route(monkeypatch, call):
    import server as srv
    from fastapi import HTTPException

    monkeypatch.setattr(srv, "get_db", lambda: _NoMatchDB())
    with pytest.raises(HTTPException) as excinfo:
        await call(srv)
    assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# Hash action: two identical inputs under two different session salts
# produce different digests, and the digest is not reproducible from
# anything inside the bundle (the salt is server-held, never shipped).
# ---------------------------------------------------------------------------

def test_hash_action_digests_differ_across_sessions_and_are_not_bundle_reproducible():
    from phi_core.agents.reasoning import PseudonymRegistry, _apply_action

    reg_session_a = PseudonymRegistry(salt="session-a-secret")
    reg_session_b = PseudonymRegistry(salt="session-b-secret")

    digest_a = _apply_action("123-45-6789", "hash", "ssn", reg_session_a)
    digest_b = _apply_action("123-45-6789", "hash", "ssn", reg_session_b)

    assert digest_a != digest_b, "the same real value must hash differently under different session salts"

    # The salt is the session id under a server-held HMAC key (crypto.pseudonym_salt),
    # never shipped in the bundle; without it, the digest cannot be reproduced from
    # anything an operator or third party would receive.
    reg_without_real_salt = PseudonymRegistry(salt="")
    assert _apply_action("123-45-6789", "hash", "ssn", reg_without_real_salt) not in (digest_a, digest_b)


# ---------------------------------------------------------------------------
# guard_report.results[].file_path is blanked on every session read.
# ---------------------------------------------------------------------------

def test_scrub_blanks_guard_report_file_paths():
    from server import _scrub_session_document

    doc = {
        "id": "sid",
        "status": "complete",
        "guard_report": {
            "status": "clean",
            "results": [
                {"file_id": "a", "status": "clean", "file_path": "/app/data/exports/sid/a__study.csv"},
                {"file_id": "b", "status": "blocked", "file_path": "/app/data/exports/sid/b__notes.txt"},
            ],
        },
    }
    out = _scrub_session_document(doc)
    for r in out["guard_report"]["results"]:
        assert r["file_path"] == "", "absolute export path leaked through guard_report on a session read"
        assert "file_id" in r and "status" in r, "scrubbing must not drop the fields the UI needs"


# ---------------------------------------------------------------------------
# GET .../stream (and every owner-scoped route) 401s with no credential at
# all when tokens are configured; the route-level wiring to
# resolve_principal is checked separately in test_production_readiness.py.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_principal_rejects_missing_credential_when_tokens_configured(monkeypatch):
    monkeypatch.setenv("API_TOKENS", "alice:tok-a")
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("PHI_ENV", "production")
    from fastapi import HTTPException
    from phi_core.security import resolve_principal

    with pytest.raises(HTTPException) as excinfo:
        await resolve_principal(x_api_token=None, phi_session=None)
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_resolve_principal_accepts_valid_cookie_or_header(monkeypatch):
    monkeypatch.setenv("API_TOKENS", "alice:tok-a")
    monkeypatch.delenv("API_TOKEN", raising=False)
    from phi_core.crypto import sign_principal_cookie
    from phi_core.security import resolve_principal

    assert await resolve_principal(x_api_token="tok-a", phi_session=None) == "alice"
    cookie = sign_principal_cookie("alice")
    assert await resolve_principal(x_api_token=None, phi_session=cookie) == "alice"


# ---------------------------------------------------------------------------
# The auth cookie carries Secure outside dev, not in dev (localhost has no
# TLS in local development).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auth_cookie_is_secure_in_production_not_in_dev(monkeypatch):
    monkeypatch.setenv("API_TOKENS", "alice:tok-a")
    monkeypatch.delenv("API_TOKEN", raising=False)
    import server as srv

    monkeypatch.setenv("PHI_ENV", "production")
    resp = await srv.auth_session(srv.AuthSessionBody(token="tok-a"))
    assert "Secure" in resp.headers.get("set-cookie", "")

    monkeypatch.setenv("PHI_ENV", "dev")
    resp = await srv.auth_session(srv.AuthSessionBody(token="tok-a"))
    assert "Secure" not in resp.headers.get("set-cookie", "")
