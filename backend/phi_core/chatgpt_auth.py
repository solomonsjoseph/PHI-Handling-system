"""ChatGPT subscription OAuth device-code flow.

The backend owns the one-time device-code login (``start_device_login`` /
``poll_once``) and writes the auth file that LiteLLM's own
``litellm.llms.chatgpt.authenticator.Authenticator`` then reads, refreshes,
and consumes on every ``chatgpt/*`` completion call. No token refresh or
request-signing logic is duplicated here: that is entirely LiteLLM's job
once the file exists. This module's only responsibility is producing that
file non-interactively, since ``Authenticator`` itself only knows how to
print a code to stdout and block for up to 15 minutes.

Deliberately NOT Fernet-encrypted like the ``api_key`` field in the
``settings`` Mongo document. LiteLLM's ``Authenticator._read_auth_file``
opens this file and calls ``json.load`` on it directly; teaching it to
decrypt first would mean patching a third-party package. Instead the
containing directory is ``chmod 0700`` and the file ``chmod 0600``, the
same protection a plain SSH private key gets. This is a deliberate,
narrower guarantee than the encrypted-at-rest ``api_key``, not an
oversight.
"""
from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from .paths import CHATGPT_TOKEN_DIR

# Mirrors litellm.llms.chatgpt.common_utils.py (litellm/llms/chatgpt/common_utils.py).
CHATGPT_AUTH_BASE = "https://auth.openai.com"
CHATGPT_DEVICE_CODE_URL = "https://auth.openai.com/api/accounts/deviceauth/usercode"
CHATGPT_DEVICE_TOKEN_URL = "https://auth.openai.com/api/accounts/deviceauth/token"
CHATGPT_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
CHATGPT_DEVICE_VERIFY_URL = "https://auth.openai.com/codex/device"
CHATGPT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

_HTTP_TIMEOUT = 20.0
# The device code itself is valid for 15 minutes server-side (matches
# litellm's own DEVICE_CODE_TIMEOUT_SECONDS); a login left un-polled past
# this point is presumed dead rather than kept alive indefinitely.
DEVICE_CODE_EXPIRES_IN_S = 15 * 60


def _auth_file() -> Path:
    """Resolve the auth file path the same way litellm's ``Authenticator``
    does: ``$CHATGPT_TOKEN_DIR/$CHATGPT_AUTH_FILE``, falling back to the
    pinned :data:`phi_core.paths.CHATGPT_TOKEN_DIR` when the env var is
    unset (server startup sets it via ``os.environ.setdefault``, so in
    practice they always agree)."""
    token_dir = Path(os.environ.get("CHATGPT_TOKEN_DIR", str(CHATGPT_TOKEN_DIR)))
    return token_dir / os.environ.get("CHATGPT_AUTH_FILE", "auth.json")


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    """Base64url-decode the middle segment of a JWT. No signature
    verification -- we only need to read a claim OpenAI itself issued us,
    not authenticate the token."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload_b64 = parts[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return {}


def _account_id_from_id_token(id_token: str | None) -> str:
    if not id_token:
        return ""
    claims = _decode_jwt_payload(id_token)
    auth_claims = claims.get("https://api.openai.com/auth")
    if isinstance(auth_claims, dict):
        account_id = auth_claims.get("chatgpt_account_id")
        if isinstance(account_id, str) and account_id:
            return account_id
    return ""


def _write_auth_file(record: dict[str, Any]) -> None:
    """Write the auth record atomically: a live access/refresh token must
    never be briefly world-readable (default-umask ``write_text`` then
    ``chmod`` afterward) nor observable mid-write by a concurrent
    ``read_auth()`` call running in another thread (see ``llm.py``'s
    ``asyncio.to_thread(call_llm, ...)``)."""
    path = _auth_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(record))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@dataclass
class DeviceLogin:
    device_auth_id: str
    user_code: str
    verify_url: str          # CHATGPT_DEVICE_VERIFY_URL
    interval_s: int
    started_at: float
    status: str = "pending"  # "pending" | "connected" | "expired" | "error"
    detail: str = ""


async def start_device_login() -> DeviceLogin:
    """Request a fresh device code. Raises on any transport/response error
    -- the caller (the ``POST /api/settings/chatgpt/login`` route) turns
    that into a 5xx rather than fabricating a login the operator can never
    complete."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(
            CHATGPT_DEVICE_CODE_URL, json={"client_id": CHATGPT_CLIENT_ID}
        )
        resp.raise_for_status()
        data = resp.json()

    device_auth_id = data.get("device_auth_id")
    user_code = data.get("user_code") or data.get("usercode")
    if not device_auth_id or not user_code:
        raise RuntimeError(f"device code response missing fields: {data}")
    interval = int(data.get("interval") or 5)

    return DeviceLogin(
        device_auth_id=device_auth_id,
        user_code=user_code,
        verify_url=CHATGPT_DEVICE_VERIFY_URL,
        interval_s=interval,
        started_at=time.time(),
        status="pending",
    )


async def poll_once(login: DeviceLogin) -> DeviceLogin:
    """Poll the device-token endpoint exactly once and, if authorized,
    exchange the resulting code for tokens and write the auth file.

    Never loops -- the caller (an HTTP request) sets the polling cadence.
    Terminal statuses (``connected``, ``expired``, ``error``) are returned
    unchanged on a repeat call so a stale login object is inert rather than
    re-triggering a token exchange."""
    if login.status in ("connected", "expired", "error"):
        return login
    if time.time() - login.started_at > DEVICE_CODE_EXPIRES_IN_S:
        login.status = "expired"
        login.detail = "device code expired after 15 minutes"
        return login

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        try:
            resp = await client.post(
                CHATGPT_DEVICE_TOKEN_URL,
                json={
                    "device_auth_id": login.device_auth_id,
                    "user_code": login.user_code,
                },
            )
        except httpx.HTTPError as exc:
            login.status = "error"
            login.detail = f"device token poll failed: {exc}"
            return login

        # 403/404 both mean "not yet authorized" per litellm's own
        # Authenticator._poll_for_authorization_code.
        if resp.status_code in (403, 404):
            login.status = "pending"
            login.detail = "waiting for the code to be entered"
            return login
        if resp.status_code != 200:
            login.status = "error"
            login.detail = f"device token poll returned HTTP {resp.status_code}"
            return login

        data = resp.json()
        if not all(k in data for k in ("authorization_code", "code_challenge", "code_verifier")):
            login.status = "pending"
            login.detail = "waiting for authorization"
            return login

        try:
            redirect_uri = f"{CHATGPT_AUTH_BASE}/deviceauth/callback"
            body = (
                "grant_type=authorization_code"
                f"&code={data['authorization_code']}"
                f"&redirect_uri={redirect_uri}"
                f"&client_id={CHATGPT_CLIENT_ID}"
                f"&code_verifier={data['code_verifier']}"
            )
            token_resp = await client.post(
                CHATGPT_OAUTH_TOKEN_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                content=body,
            )
            token_resp.raise_for_status()
            tokens = token_resp.json()
        except httpx.HTTPError as exc:
            login.status = "error"
            login.detail = f"token exchange failed: {exc}"
            return login

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    id_token = tokens.get("id_token")
    if not (access_token and refresh_token and id_token):
        missing = sorted({"access_token", "refresh_token", "id_token"} - tokens.keys())
        login.status = "error"
        login.detail = f"token exchange response missing fields: {missing}"
        return login

    expires_at = _decode_jwt_payload(access_token).get("exp")
    account_id = _account_id_from_id_token(id_token)
    _write_auth_file({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "account_id": account_id,
        "expires_at": expires_at,
    })
    login.status = "connected"
    login.detail = "connected"
    return login


def read_auth() -> dict[str, Any] | None:
    """Read the raw auth record, or ``None`` when no account is connected.

    This is the fail-fast probe used by ``agents/llm.py`` before letting
    any call reach litellm's ``ChatGPTConfig`` -- a missing file there
    means litellm would instead print a device code and block for up to
    15 minutes."""
    try:
        return json.loads(_auth_file().read_text())
    except (OSError, json.JSONDecodeError):
        return None


def auth_status() -> dict[str, Any]:
    """Payload for ``GET /api/settings/chatgpt/status``."""
    auth = read_auth()
    if not auth:
        return {"connected": False, "account_id": "", "plan": "", "expires_at": None, "email": ""}

    id_token = auth.get("id_token") or ""
    claims = _decode_jwt_payload(id_token)
    account_id = auth.get("account_id") or _account_id_from_id_token(id_token)
    auth_claims = claims.get("https://api.openai.com/auth")
    plan = auth_claims.get("chatgpt_plan_type", "") if isinstance(auth_claims, dict) else ""
    return {
        "connected": True,
        "account_id": account_id or "",
        "plan": plan,
        "expires_at": auth.get("expires_at"),
        "email": claims.get("email", ""),
    }


def clear_auth() -> None:
    """Delete the auth file. A no-op when no account is connected."""
    try:
        _auth_file().unlink()
    except FileNotFoundError:
        pass
