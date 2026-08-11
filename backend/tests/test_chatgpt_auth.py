"""ChatGPT OAuth device-code provider tests (workstream B).

No network, no Mongo. Every test isolates its own auth-file directory via
``monkeypatch.setenv("CHATGPT_TOKEN_DIR", ...)`` pointed at ``tmp_path``, so
tests never read or write a real operator's connected account. Route-level
tests call the FastAPI handler coroutine directly and assert on the raised
``HTTPException`` -- the pattern already used by
``backend/tests/test_security_findings.py`` -- rather than spinning up a
TestClient + Mongo double, since this codebase has no mongomock dependency.
"""
from __future__ import annotations

import base64
import json
import time

import pytest


def _b64url(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _fake_id_token(account_id: str | None = None, email: str | None = None) -> str:
    """An unsigned JWT with the two segments _decode_jwt_payload reads.
    No signature verification happens anywhere in this stack, so a bare
    ``header.payload.sig`` string is a faithful fixture."""
    header = _b64url({"alg": "none", "typ": "JWT"})
    payload: dict = {}
    if account_id is not None:
        payload["https://api.openai.com/auth"] = {"chatgpt_account_id": account_id}
    if email is not None:
        payload["email"] = email
    body = _b64url(payload)
    return f"{header}.{body}.sig"


class _FakeResp:
    def __init__(self, status_code: int, data: dict):
        self.status_code = status_code
        self._data = data

    def json(self) -> dict:
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("error", request=None, response=self)  # type: ignore[arg-type]


class _FakeAsyncClient:
    """Stand-in for httpx.AsyncClient. ``responses`` is a shared list
    popped in call order across both requests a single ``poll_once`` may
    issue (device-token poll, then token exchange) and across repeat
    ``poll_once`` calls in the same test."""

    def __init__(self, responses: list[_FakeResp]):
        self._responses = responses

    def __call__(self, *args, **kwargs) -> "_FakeAsyncClient":
        return self

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args) -> bool:
        return False

    async def post(self, url, **kwargs) -> _FakeResp:
        return self._responses.pop(0)


# --------------------------------------------------------------------------
# read_auth / auth_status
# --------------------------------------------------------------------------


def test_read_auth_absent_reports_not_connected(monkeypatch, tmp_path):
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(tmp_path))
    from phi_core import chatgpt_auth

    assert chatgpt_auth.read_auth() is None
    status = chatgpt_auth.auth_status()
    assert status["connected"] is False
    assert status["account_id"] == ""


def test_auth_status_connected_derives_account_id_from_id_token(monkeypatch, tmp_path):
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(tmp_path))
    from phi_core import chatgpt_auth

    id_token = _fake_id_token(account_id="acct_test")
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({
        "access_token": "at-fixture",
        "refresh_token": "rt-fixture",
        "id_token": id_token,
        "expires_at": time.time() + 3600,
    }))

    status = chatgpt_auth.auth_status()
    assert status["connected"] is True
    assert status["account_id"] == "acct_test"


# --------------------------------------------------------------------------
# poll_once: pending -> connected, five-key auth file, 0o600
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_once_pending_then_connected_writes_auth_file(monkeypatch, tmp_path):
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(tmp_path))
    from phi_core import chatgpt_auth

    id_token = _fake_id_token(account_id="acct_poll")
    responses = [
        _FakeResp(403, {}),  # not yet authorized
        _FakeResp(200, {"authorization_code": "AC", "code_challenge": "CC", "code_verifier": "CV"}),
        _FakeResp(200, {"access_token": "AT", "refresh_token": "RT", "id_token": id_token}),
    ]
    monkeypatch.setattr(chatgpt_auth.httpx, "AsyncClient", _FakeAsyncClient(responses))

    login = chatgpt_auth.DeviceLogin(
        device_auth_id="d1",
        user_code="ABCD-1234",
        verify_url="https://auth.openai.com/codex/device",
        interval_s=1,
        started_at=time.time(),
    )

    login = await chatgpt_auth.poll_once(login)
    assert login.status == "pending"

    login = await chatgpt_auth.poll_once(login)
    assert login.status == "connected"

    auth_file = tmp_path / "auth.json"
    assert auth_file.exists()
    assert (auth_file.stat().st_mode & 0o777) == 0o600

    record = json.loads(auth_file.read_text())
    assert set(record.keys()) == {"access_token", "refresh_token", "id_token", "account_id", "expires_at"}
    assert record["access_token"] == "AT"
    assert record["refresh_token"] == "RT"
    assert record["id_token"] == id_token
    assert record["account_id"] == "acct_poll"


# --------------------------------------------------------------------------
# fail-fast guard: elapsed time IS the regression guard
# --------------------------------------------------------------------------


def test_call_llm_chatgpt_fails_fast_without_auth_file(monkeypatch, tmp_path):
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(tmp_path))
    from phi_core.agents.llm import LlmConfig, call_llm

    cfg = LlmConfig(provider="chatgpt", model="chatgpt/gpt-5.3-codex")
    t0 = time.time()
    with pytest.raises(RuntimeError, match="ChatGPT account not connected"):
        call_llm("system", "user", cfg)
    elapsed = time.time() - t0
    assert elapsed < 1.0, f"guard should raise immediately, took {elapsed:.2f}s"


def test_call_llm_with_web_search_chatgpt_fails_fast_without_auth_file(monkeypatch, tmp_path):
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(tmp_path))
    from phi_core.agents.llm import LlmConfig, call_llm_with_web_search

    cfg = LlmConfig(provider="chatgpt", model="chatgpt/gpt-5.3-codex")
    t0 = time.time()
    with pytest.raises(RuntimeError, match="ChatGPT account not connected"):
        call_llm_with_web_search("system", "user", cfg)
    elapsed = time.time() - t0
    assert elapsed < 1.0, f"guard should raise immediately, took {elapsed:.2f}s"


# --------------------------------------------------------------------------
# POST /api/settings/llm guard
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_llm_settings_chatgpt_without_auth_returns_400(monkeypatch, tmp_path):
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(tmp_path))
    import server as srv
    from fastapi import HTTPException

    body = srv.LlmSettings(provider="chatgpt", model="chatgpt/gpt-5.3-codex")
    with pytest.raises(HTTPException) as excinfo:
        await srv.set_llm_settings(body)
    assert excinfo.value.status_code == 400
    assert "not connected" in excinfo.value.detail.lower()
