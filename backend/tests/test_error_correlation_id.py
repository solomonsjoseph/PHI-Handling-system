"""4.23: pipeline worker failures and unhandled exceptions must never
persist or return raw exception text to the client -- only a fixed message
plus a short correlation id, with full detail server-logged."""
from __future__ import annotations

import asyncio


class _FakeUpdateResult:
    matched_count = 1


class _FakeSessionsCollection:
    def __init__(self):
        self.calls = []

    async def update_one(self, filter_, update):
        self.calls.append((filter_, update))
        return _FakeUpdateResult()


class _FakeDB:
    def __init__(self):
        self.sessions = _FakeSessionsCollection()


def test_fail_session_correlated_never_persists_raw_exception_text(monkeypatch):
    import server as srv

    monkeypatch.setattr(srv, "cleanup_session_unpacked", lambda sid: None)

    emitted = []

    async def _fake_emit(sid, ev, run_id=None):
        emitted.append(ev)

    monkeypatch.setattr(srv, "_emit", _fake_emit)

    db = _FakeDB()
    secret = "sk-super-secret-token-12345 at /app/data/uploads/abcd/unpacked/patient_roster.csv"
    exc = RuntimeError(secret)

    asyncio.run(srv._fail_session_correlated(db, "sid1", {"id": "sid1"}, exc, run_id="run1"))

    assert len(db.sessions.calls) == 1
    _filter, update = db.sessions.calls[0]
    doc_set = update["$set"]
    assert doc_set["status"] == "failed"
    assert doc_set["error"] == "pipeline failed"
    assert secret not in doc_set["error"]
    error_id = doc_set["error_id"]
    assert error_id and len(error_id) == 12

    assert len(emitted) == 1
    assert secret not in emitted[0].message
    assert error_id in emitted[0].message


def test_unhandled_exception_handler_returns_generic_body_no_secret(monkeypatch):
    import server as srv
    from starlette.requests import Request

    secret = "postgresql://user:hunter2@internal-db.corp:5432/phi and /etc/shadow"

    async def _receive():
        return {"type": "http.request"}

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/whatever",
        "headers": [],
        "query_string": b"",
        "client": ("127.0.0.1", 12345),
    }
    request = Request(scope, receive=_receive)

    response = asyncio.run(srv._unhandled_exception_handler(request, ValueError(secret)))

    assert response.status_code == 500
    body = response.body.decode("utf-8")
    assert secret not in body
    assert '"code":"INTERNAL"' in body
    assert '"message":"unexpected error"' in body
    assert "error_id" in body


def test_error_id_is_unique_per_failure(monkeypatch):
    import server as srv

    monkeypatch.setattr(srv, "cleanup_session_unpacked", lambda sid: None)

    async def _fake_emit(sid, ev, run_id=None):
        pass

    monkeypatch.setattr(srv, "_emit", _fake_emit)

    db = _FakeDB()
    asyncio.run(srv._fail_session_correlated(db, "s", {"id": "s"}, RuntimeError("x"), run_id=None))
    asyncio.run(srv._fail_session_correlated(db, "s", {"id": "s"}, RuntimeError("x"), run_id=None))

    ids = [update["$set"]["error_id"] for _f, update in db.sessions.calls]
    assert ids[0] != ids[1]
