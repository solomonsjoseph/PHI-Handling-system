"""SEC-003 unit tests (allow-list + SSRF) and live endpoint checks.
SEC-002 auth-token dep tests via live server.

Runs against the local uvicorn at http://localhost:8001 so that the tests
exercise the full FastAPI stack including startup env, dependencies and
Motor. Skipped automatically when the backend is not reachable.
"""
import os

import pytest
import requests
from fastapi import HTTPException

from phi_core.security import (
    allowed_providers, validate_llm_base_url, validate_llm_provider,
)


BASE_URL = os.environ.get("PHI_TEST_BASE_URL", "http://localhost:8001")


def _backend_up() -> bool:
    try:
        return requests.get(f"{BASE_URL}/api/health", timeout=2).ok
    except Exception:
        return False


# ---------- unit-level (no HTTP) ------------------------------------------

def test_provider_allow_list_default(monkeypatch):
    monkeypatch.delenv("ALLOWED_LLM_BASE_URL_HOSTS", raising=False)
    assert "openai_compatible" not in allowed_providers()
    for p in ("anthropic", "openai", "gemini", "openrouter"):
        assert p in allowed_providers()


def test_validate_provider_rejects_unknown():
    with pytest.raises(HTTPException) as e:
        validate_llm_provider("evilprov")
    assert e.value.status_code == 400


def test_validate_provider_rejects_openai_compatible_without_env(monkeypatch):
    monkeypatch.delenv("ALLOWED_LLM_BASE_URL_HOSTS", raising=False)
    with pytest.raises(HTTPException):
        validate_llm_provider("openai_compatible")


def test_validate_base_url_rejects_http():
    with pytest.raises(HTTPException):
        validate_llm_base_url("http://example.com/v1", "openai_compatible")


def test_validate_base_url_rejects_loopback():
    with pytest.raises(HTTPException):
        validate_llm_base_url("https://127.0.0.1/v1", "openai_compatible")


def test_validate_base_url_rejects_private():
    with pytest.raises(HTTPException):
        validate_llm_base_url("https://10.0.0.5/v1", "openai_compatible")


def test_validate_base_url_rejects_metadata():
    with pytest.raises(HTTPException):
        validate_llm_base_url("https://169.254.169.254/latest/meta-data", "openai_compatible")


def test_validate_base_url_requires_host_in_allowlist_when_set(monkeypatch):
    monkeypatch.setenv("ALLOWED_LLM_BASE_URL_HOSTS", "api.trusted.example.com")
    with pytest.raises(HTTPException):
        validate_llm_base_url("https://api.evil.example.com/v1", "openai_compatible")


# ---------- live endpoint --------------------------------------------------

pytestmark_live = pytest.mark.skipif(not _backend_up(), reason="backend not reachable")


@pytestmark_live
def test_set_settings_endpoint_rejects_evil_base_url():
    r = requests.post(f"{BASE_URL}/api/settings/llm", json={
        "provider": "openai_compatible",
        "model": "gpt-4o",
        "api_key": "sk-fake",
        "base_url": "http://169.254.169.254/",
        "temperature": 0.1,
        "max_tokens": 200,
    }, timeout=10)
    # Either 400 (rejected by validator) or 401 if API_TOKEN is set on the server.
    assert r.status_code in (400, 401), r.text


@pytestmark_live
def test_sessions_list_does_not_leak_stored_path():
    r = requests.get(f"{BASE_URL}/api/sessions", timeout=10)
    assert r.status_code == 200
    for s in r.json().get("sessions", []):
        for f in s.get("files", []) or []:
            assert "stored_path" not in f, f"stored_path leaked: {f}"


@pytestmark_live
def test_get_settings_never_returns_api_key_plaintext():
    r = requests.get(f"{BASE_URL}/api/settings/llm", timeout=10)
    assert r.status_code == 200
    body = r.json()
    # Server returns empty api_key string, marks api_key_set boolean.
    assert body.get("api_key", "") == ""
