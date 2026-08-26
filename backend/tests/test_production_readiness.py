"""Phase 4 production-readiness gates: boot-time config validation, identity
and ownership, retention, signing, health checks, and the other hardening
controls. Each test targets a single named gate from the plan's Verification
section (V6)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _run_import(env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
    import os
    env = dict(os.environ)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", "import server"],
        cwd=str(BACKEND_DIR), env=env, capture_output=True, text=True, timeout=60,
    )


def test_production_boot_refuses_without_api_tokens():
    proc = _run_import({
        "PHI_ENV": "production", "API_TOKENS": "", "API_TOKEN": "",
        "REVIEWER_PRINCIPALS": "",
        "CORS_ALLOWED_ORIGINS": "", "MONGO_URL": "mongodb://localhost:27017",
        "APP_ENCRYPTION_KEY": "", "ATTESTATION_SIGNING_KEY": "",
        "DATA_DIR": "/tmp/phi_data",
    })
    assert proc.returncode != 0
    assert "API_TOKENS" in proc.stderr
    assert "REVIEWER_PRINCIPALS" in proc.stderr
    assert "CORS_ALLOWED_ORIGINS" in proc.stderr
    assert "MONGO_URL" in proc.stderr


def test_dev_boot_starts_with_no_configuration():
    proc = _run_import({
        "PHI_ENV": "dev", "API_TOKENS": "", "API_TOKEN": "",
        "CORS_ALLOWED_ORIGINS": "", "MONGO_URL": "mongodb://localhost:27017",
        "APP_ENCRYPTION_KEY": "", "ATTESTATION_SIGNING_KEY": "",
        "DATA_DIR": "/tmp/phi_data",
    })
    assert proc.returncode == 0, proc.stderr


def test_owner_scoped_route_uses_resolve_principal_not_bare_token_gate():
    """4.2: an owner-scoped route resolves an identity, it does not just
    check a bare shared-secret dependency."""
    from server import app
    matching = [r for r in app.router.routes if getattr(r, "path", "") == "/api/sessions/{sid}"]
    assert matching, "session_get route not registered"
    dep_fns = {d.call.__name__ for d in matching[0].dependant.dependencies}
    assert "resolve_principal" in dep_fns


@pytest.mark.asyncio
async def test_owner_mismatch_returns_404_not_403():
    """A wrong owner and a missing id must be indistinguishable, so session
    ids stay unguessable (never a 403 that confirms existence)."""
    import server as srv
    from fastapi import HTTPException

    class _StubDB:
        def __init__(self):
            self.sessions = self
        async def find_one(self, query, *_a, **_kw):
            return None  # no document matches this owner filter

    _srv_mod_get_db_backup = srv.get_db
    srv.get_db = lambda: _StubDB()
    try:
        with pytest.raises(HTTPException) as excinfo:
            await srv.session_get("sid", principal="someone-else")
        assert excinfo.value.status_code == 404
    finally:
        srv.get_db = _srv_mod_get_db_backup


def test_token_principals_parses_api_tokens_and_legacy_token(monkeypatch):
    from phi_core.security import token_principals
    monkeypatch.setenv("API_TOKENS", "alice:tok-a,bob:tok-b")
    monkeypatch.delenv("API_TOKEN", raising=False)
    principals = token_principals()
    assert principals == {"tok-a": "alice", "tok-b": "bob"}

def test_reviewer_principals_parses_name_role_pairs_and_drops_unknown_roles(monkeypatch):
    from phi_core.security import reviewer_principals
    monkeypatch.setenv("REVIEWER_PRINCIPALS", "alice:lead_reviewer,bob:reviewer,eve:admin")
    principals = reviewer_principals()
    assert principals == {"alice": "lead_reviewer", "bob": "reviewer"}


def test_reviewer_role_grants_dev_lead_reviewer_only_when_unset_and_only_to_dev(monkeypatch):
    from phi_core.security import reviewer_role
    monkeypatch.delenv("REVIEWER_PRINCIPALS", raising=False)
    monkeypatch.setenv("PHI_ENV", "dev")
    assert reviewer_role("dev") == "lead_reviewer"
    assert reviewer_role("someone-else") is None
    monkeypatch.setenv("PHI_ENV", "production")
    assert reviewer_role("dev") is None


def test_reviewer_role_respects_configured_principals_over_the_dev_fallback(monkeypatch):
    from phi_core.security import reviewer_role
    monkeypatch.setenv("REVIEWER_PRINCIPALS", "alice:reviewer")
    monkeypatch.setenv("PHI_ENV", "dev")
    assert reviewer_role("alice") == "reviewer"
    # Once REVIEWER_PRINCIPALS is configured at all, the "dev" convenience
    # principal no longer gets a free role unless explicitly listed.
    assert reviewer_role("dev") is None


@pytest.mark.asyncio
async def test_cookie_auth_round_trip(monkeypatch):
    """4.3: POST /api/auth/session exchanges a valid token for a signed
    cookie; resolve_principal accepts that cookie and rejects a forged one."""
    monkeypatch.setenv("API_TOKENS", "alice:tok-a")
    monkeypatch.delenv("API_TOKEN", raising=False)
    import server as srv
    from phi_core.crypto import verify_principal_cookie

    resp = await srv.auth_session(srv.AuthSessionBody(token="tok-a"))
    assert resp.status_code == 200
    set_cookie_header = resp.headers.get("set-cookie", "")
    assert "phi_session=" in set_cookie_header
    assert "HttpOnly" in set_cookie_header
    assert "SameSite=strict" in set_cookie_header or "samesite=strict" in set_cookie_header.lower()

    cookie_value = set_cookie_header.split("phi_session=")[1].split(";")[0]
    assert verify_principal_cookie(cookie_value) == "alice"
    assert verify_principal_cookie(cookie_value + "tampered") is None

    principal = await srv.resolve_principal(x_api_token=None, phi_session=cookie_value)
    assert principal == "alice"


@pytest.mark.asyncio
async def test_auth_session_rejects_unknown_token(monkeypatch):
    monkeypatch.setenv("API_TOKENS", "alice:tok-a")
    monkeypatch.delenv("API_TOKEN", raising=False)
    import server as srv
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await srv.auth_session(srv.AuthSessionBody(token="wrong-token"))
    assert excinfo.value.status_code == 401


def test_data_dirs_created_with_0700_permissions():
    import stat

    from phi_core.paths import CACHE_DIR, EVIDENCE_DIR, PUBLISHED_DIR, REVERSAL_DIR, STAGING_DIR, UPLOAD_DIR
    for d in (UPLOAD_DIR, STAGING_DIR, EVIDENCE_DIR, REVERSAL_DIR, PUBLISHED_DIR, CACHE_DIR):
        mode = stat.S_IMODE(d.stat().st_mode)
        assert mode == 0o700, f"{d} has mode {oct(mode)}, expected 0o700"


def test_cleanup_session_unpacked_removes_only_unpacked_subdir(tmp_path, monkeypatch):
    from phi_core import paths as paths_mod
    monkeypatch.setattr(paths_mod, "UPLOAD_DIR", tmp_path)
    sid = "sess-cleanup-test"
    session_dir = tmp_path / sid
    (session_dir / "unpacked").mkdir(parents=True)
    (session_dir / "unpacked" / "raw.csv").write_text("phi data", encoding="utf-8")
    (session_dir / "intake.zip").write_bytes(b"zip bytes")

    paths_mod.cleanup_session_unpacked(sid)

    assert not (session_dir / "unpacked").exists()
    assert (session_dir / "intake.zip").exists()


@pytest.mark.asyncio
async def test_session_delete_removes_document_files_and_agent_log(tmp_path, monkeypatch):
    import server as srv
    from phi_core.control import superorchestrator as super_module

    cancel_calls: list[dict] = []

    class FakeSuperOrchestrator:
        def __init__(self, *_args):
            pass

        async def cancel_run(self, **kwargs):
            cancel_calls.append(kwargs)


    sid = "a" * 32  # a real session id is always a bare uuid4().hex token
    export = tmp_path / "export.csv"
    export.write_text("redacted", encoding="utf-8")
    session_dir = srv.UPLOAD_DIR / sid
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "marker.txt").write_text("x", encoding="utf-8")

    class _StubCollection:
        def __init__(self):
            self.deleted = []
        async def find_one(self, query, *_a, **_kw):
            return {
                "id": sid,
                "owner": "alice",
                "export_paths": {"a": str(export)},
                "_pipeline_run_id": "a" * 32,
            }
        async def delete_many(self, query):
            self.deleted.append(("agent_log", query))
        async def delete_one(self, query):
            self.deleted.append(("sessions", query))

    class _EmptyControlCollection:
        """Every control-plane collection `session_delete` touches
        (`session_tombstones`, `artifacts`, `publication_pointers`) via
        `MongoControlStore` -- empty in this test, matching real Motor
        semantics closely enough (insert succeeds, nothing to find)."""
        async def insert_one(self, document):
            from types import SimpleNamespace
            return SimpleNamespace(inserted_id="fake-id")
        async def find_one(self, query, *_a, **_kw):
            return None
        def find(self, query):
            async def _empty_cursor():
                return
                yield  # pragma: no cover - makes this an async generator
            return _empty_cursor()
        async def delete_one(self, query):
            from types import SimpleNamespace
            return SimpleNamespace(deleted_count=0)

    class _StubDB:
        def __init__(self):
            self.sessions = _StubCollection()
            self.agent_log = self.sessions
            self.trace_events = self.sessions

        def __getitem__(self, _name: str) -> _EmptyControlCollection:
            return _EmptyControlCollection()

    db = _StubDB()
    monkeypatch.setattr(super_module, "SuperOrchestrator", FakeSuperOrchestrator)
    monkeypatch.setattr(srv, "get_db", lambda: db)

    resp = await srv.session_delete(sid, principal="alice")
    assert resp == {"deleted": True}
    assert not export.exists()
    assert not session_dir.exists()
    assert ("sessions", {"id": sid, "owner": "alice"}) in db.sessions.deleted
    assert cancel_calls == [{
        "session_id": sid,
        "run_id": "a" * 32,
        "principal": "alice",
        "reason": "session deleted",
    }]


@pytest.mark.asyncio
async def test_session_delete_records_erasure_pending_on_a_filesystem_failure(tmp_path, monkeypatch):
    """A failed filesystem deletion never silently reports success: the
    session document survives as `erasure_pending` with the exact error
    and an attempt count, rather than being deleted with bytes still on
    disk or the failure being swallowed."""
    import server as srv
    from phi_core.control import artifacts as artifacts_module
    from phi_core.control import superorchestrator as super_module

    class FakeSuperOrchestrator:
        def __init__(self, *_args):
            pass

        async def cancel_run(self, **kwargs):
            pass

    sid = "beef" * 8
    session_dir = srv.UPLOAD_DIR / sid
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "marker.txt").write_text("x", encoding="utf-8")

    class _StubCollection:
        def __init__(self):
            self.updates: list[dict] = []

        async def find_one(self, query, *_a, **_kw):
            return {"id": sid, "owner": "alice", "export_paths": {}, "_pipeline_run_id": sid, "erasure_attempts": 0}

        async def update_one(self, query, update):
            self.updates.append(update["$set"])

        async def delete_many(self, query):
            raise AssertionError("must not delete agent_log/trace_events before erasure is confirmed")

        async def delete_one(self, query):
            raise AssertionError("must not delete the session document before erasure is confirmed")

    class _EmptyControlCollection:
        async def insert_one(self, document):
            from types import SimpleNamespace
            return SimpleNamespace(inserted_id="fake-id")

        async def find_one(self, query, *_a, **_kw):
            return None

        def find(self, query):
            async def _empty_cursor():
                return
                yield  # pragma: no cover - makes this an async generator
            return _empty_cursor()

        async def delete_one(self, query):
            from types import SimpleNamespace
            return SimpleNamespace(deleted_count=0)

    class _StubDB:
        def __init__(self):
            self.sessions = _StubCollection()
            self.agent_log = self.sessions
            self.trace_events = self.sessions

        def __getitem__(self, _name: str) -> _EmptyControlCollection:
            return _EmptyControlCollection()

    def _failing_erase(_session_id: str) -> dict[str, str]:
        return {"staging": "simulated permission denied"}

    db = _StubDB()
    monkeypatch.setattr(super_module, "SuperOrchestrator", FakeSuperOrchestrator)
    monkeypatch.setattr(srv, "get_db", lambda: db)
    monkeypatch.setattr(artifacts_module, "erase_session_artifacts", _failing_erase)

    resp = await srv.session_delete(sid, principal="alice")

    assert resp == {"deleted": False, "erasure_pending": True}
    assert len(db.sessions.updates) == 1
    update = db.sessions.updates[0]
    assert update["status"] == "erasure_pending"
    assert "simulated permission denied" in update["erasure_error"]
    assert update["erasure_attempts"] == 1
    # The uploaded directory (not part of the simulated failure) is still
    # erased -- only the failed root is retried, not every already-done step.
    assert not session_dir.exists()


@pytest.mark.asyncio
async def test_retention_purge_removes_expired_partially_complete_session(tmp_path, monkeypatch):
    """Expired partial reviews lose their PHI files on the normal retention window."""
    import asyncio
    from datetime import datetime, timedelta, timezone

    import server as srv

    sid = "beef" * 8  # a real session id is always a bare uuid4().hex token
    upload_dir = tmp_path / "uploads"
    session_dir = upload_dir / sid
    session_dir.mkdir(parents=True)
    (session_dir / "source.csv").write_text("raw PHI", encoding="utf-8")
    export = tmp_path / "exports" / "handled.csv"
    export.parent.mkdir()
    export.write_text("handled data", encoding="utf-8")
    expired_session = {
        "id": sid,
        "status": "partially_complete",
        "updated_at": (datetime.now(timezone.utc) - timedelta(days=srv.RETENTION_DAYS + 1)).isoformat(),
        "export_paths": {"dataset": str(export)},
    }

    class _Sessions:
        def __init__(self):
            self.deleted = []

        def find(self, query, *_a, **_kw):
            status_filter = query["status"]["$in"]
            cutoff = query["updated_at"]["$lt"]

            async def matching_sessions():
                if (
                    expired_session["status"] in status_filter
                    and expired_session["updated_at"] < cutoff
                ):
                    yield expired_session

            return matching_sessions()

        async def delete_one(self, query):
            self.deleted.append(query)

    class _AgentLog:
        def __init__(self):
            self.deleted = []

        async def delete_many(self, query):
            self.deleted.append(query)

    class _StubDB:
        def __init__(self):
            self.sessions = _Sessions()
            self.agent_log = _AgentLog()
            self.trace_events = _AgentLog()

    async def stop_after_one_pass(_seconds):
        raise asyncio.CancelledError

    db = _StubDB()
    monkeypatch.setattr(srv, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(srv, "get_db", lambda: db)
    monkeypatch.setattr(srv.asyncio, "sleep", stop_after_one_pass)

    with pytest.raises(asyncio.CancelledError):
        await srv._purge_settled_sessions_loop()

    assert not session_dir.exists()
    assert not export.exists()
    assert db.agent_log.deleted == [{"session_id": sid}]
    assert db.sessions.deleted == [{"id": sid}]


def _review_retention_stub_db(*, sid: str, updated_at: str, hold: str = ""):
    """A minimal stub DB exercising exactly step 2 of
    ``_purge_settled_sessions_loop``: an
    ``awaiting_human_review`` session past ``REVIEW_RETENTION_DAYS``."""
    session = {"id": sid, "status": "awaiting_human_review", "updated_at": updated_at,
               "_pipeline_run_id": "run-" + sid}

    class _Sessions:
        def __init__(self):
            self.updates: list[dict] = []

        def find(self, query, *_a, **_kw):
            async def matching():
                if query.get("status") == "awaiting_human_review" and session["updated_at"] < query["updated_at"]["$lt"]:
                    yield session
            return matching()

        async def update_one(self, query, update):
            self.updates.append(update["$set"])
            session.update(update["$set"])

    class _WorkflowRuns:
        async def find_one(self, query, *_a, **_kw):
            if query.get("run_id") == session["_pipeline_run_id"]:
                return {"hold": hold}
            return None

    class _StubDB:
        def __init__(self):
            self.sessions = _Sessions()
            self.workflow_runs = _WorkflowRuns()

    return _StubDB(), session


@pytest.mark.asyncio
async def test_awaiting_review_cannot_retain_raw_phi_beyond_review_retention_days(tmp_path, monkeypatch):
    """A paused human review with no hold loses its raw PHI
    once REVIEW_RETENTION_DAYS elapses, and the session moves to the
    terminal `expired_awaiting_review` status rather than being silently
    left `awaiting_human_review` forever."""
    import asyncio
    from datetime import datetime, timedelta, timezone

    import server as srv

    sid = "cafe" * 8
    upload_dir = tmp_path / "uploads"
    session_dir = upload_dir / sid
    session_dir.mkdir(parents=True)
    (session_dir / "source.csv").write_text("raw PHI", encoding="utf-8")
    stale = (datetime.now(timezone.utc) - timedelta(days=srv.REVIEW_RETENTION_DAYS + 1)).isoformat()
    db, _session = _review_retention_stub_db(sid=sid, updated_at=stale)

    async def stop_after_one_pass(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(srv, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(srv, "get_db", lambda: db)
    monkeypatch.setattr(srv.asyncio, "sleep", stop_after_one_pass)

    with pytest.raises(asyncio.CancelledError):
        await srv._purge_settled_sessions_loop()

    assert not session_dir.exists()
    assert db.sessions.updates == [{"status": "expired_awaiting_review", "updated_at": _session["updated_at"]}]


@pytest.mark.asyncio
async def test_awaiting_review_retains_raw_phi_while_the_run_is_held(tmp_path, monkeypatch):
    """D14: a legal/administrative hold on the run suspends the
    awaiting-review retention timer entirely -- no erasure, no status
    change, regardless of how stale `updated_at` is."""
    import asyncio
    from datetime import datetime, timedelta, timezone

    import server as srv

    sid = "d00d" * 8
    upload_dir = tmp_path / "uploads"
    session_dir = upload_dir / sid
    session_dir.mkdir(parents=True)
    (session_dir / "source.csv").write_text("raw PHI", encoding="utf-8")
    ancient = (datetime.now(timezone.utc) - timedelta(days=srv.REVIEW_RETENTION_DAYS * 10)).isoformat()
    db, _session = _review_retention_stub_db(sid=sid, updated_at=ancient, hold="litigation-hold-1")

    async def stop_after_one_pass(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(srv, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(srv, "get_db", lambda: db)
    monkeypatch.setattr(srv.asyncio, "sleep", stop_after_one_pass)

    with pytest.raises(asyncio.CancelledError):
        await srv._purge_settled_sessions_loop()

    assert session_dir.exists()
    assert db.sessions.updates == []

@pytest.mark.asyncio
async def test_health_reports_mongo_down_as_503(monkeypatch):
    import server as srv

    class _StubDB:
        async def command(self, *_a, **_kw):
            raise ConnectionError("mongo unreachable")
        settings = None

    class _StubDB2(_StubDB):
        settings = type("S", (), {"find_one": staticmethod(lambda *a, **k: _async_none())})()

    async def _async_none():
        return {}

    monkeypatch.setattr(srv, "get_db", lambda: _StubDB2())
    resp = await srv.health()
    assert resp.status_code == 503


def test_attestation_signing_functions_exist_and_round_trip(monkeypatch):
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, load_pem_public_key
    from phi_core.crypto import sign_bytes, signing_public_key_pem

    k = Ed25519PrivateKey.generate()
    der = k.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption())
    monkeypatch.setenv("ATTESTATION_SIGNING_KEY", base64.b64encode(der).decode())

    pem = signing_public_key_pem()
    sig = sign_bytes(b"attestation bytes")
    assert pem is not None and sig is not None
    pub = load_pem_public_key(pem.encode())
    pub.verify(base64.b64decode(sig), b"attestation bytes")
