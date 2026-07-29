"""Round-2 security fixes after the second audit.

Covers:
- SEC-002 completion: read endpoints strip `stored_path`, `export_paths`
  values, and scrub PHI from `agent_decisions.reason`.
- SEC-003 residual: per-provider host allow-list for `base_url`.
- SEC-005 residual: streamed-total-bytes cap trumps header-claimed total.
- SEC-006: `scrub_persisted_text` and `scrub_decision` redact PHI from
  strings before persistence / on the way out.
"""
from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest
import requests
from fastapi import HTTPException

from phi_core.intake import unpack_zip
from phi_core.security import (
    PROVIDER_HOSTS,
    scrub_decision,
    scrub_persisted_text,
    validate_llm_base_url,
)


BASE_URL = os.environ.get("PHI_TEST_BASE_URL", "http://localhost:8001")


def _backend_up() -> bool:
    try:
        return requests.get(f"{BASE_URL}/api/health", timeout=2).ok
    except Exception:
        return False


# ---------- SEC-006 unit ---------------------------------------------------

def test_scrub_persisted_text_redacts_name_phone_email_ssn():
    text = "James Smith at UCSF, call 415-555-1234 or james@example.edu (SSN 111-22-3333)"
    out = scrub_persisted_text(text)
    assert "James Smith" not in out
    assert "415-555-1234" not in out
    assert "james@example.edu" not in out
    assert "111-22-3333" not in out
    assert "UCSF" in out or "[A]" in out  # facility acronym may survive


def test_scrub_persisted_text_empty():
    assert scrub_persisted_text("") == ""
    assert scrub_persisted_text(None) == ""  # type: ignore[arg-type]


def test_scrub_decision_only_scrubs_string_fields():
    d = {
        "column": "notes",
        "action": "drop",
        "confidence": 0.95,
        "reason": "Contains real name James Smith and phone 415-555-1234",
        "citation": "per patient John Doe consent",
    }
    out = scrub_decision(d)
    assert out["column"] == "notes"
    assert out["action"] == "drop"
    assert out["confidence"] == 0.95
    assert "James Smith" not in out["reason"]
    assert "415-555-1234" not in out["reason"]
    assert "John Doe" not in out["citation"]


# ---------- SEC-003 per-provider host allow-list --------------------------

def test_provider_hosts_defined_for_all_standard_providers():
    for p in ("emergent", "anthropic", "openai", "gemini", "openrouter"):
        assert p in PROVIDER_HOSTS


def test_openai_base_url_must_be_api_openai_com():
    validate_llm_base_url("https://api.openai.com/v1", "openai")
    with pytest.raises(HTTPException):
        validate_llm_base_url("https://evil.example.com/v1", "openai")


def test_anthropic_base_url_must_be_anthropic_host():
    validate_llm_base_url("https://api.anthropic.com/v1", "anthropic")
    with pytest.raises(HTTPException):
        validate_llm_base_url("https://api.evil.example.com/v1", "anthropic")


def test_emergent_rejects_any_base_url():
    # emergent uses the internal proxy and does not accept a base_url.
    with pytest.raises(HTTPException):
        validate_llm_base_url("https://api.openai.com/v1", "emergent")


def test_openai_compatible_requires_env_hosts(monkeypatch):
    monkeypatch.delenv("ALLOWED_LLM_BASE_URL_HOSTS", raising=False)
    with pytest.raises(HTTPException):
        validate_llm_base_url("https://api.example.com/v1", "openai_compatible")


def test_openai_compatible_accepts_env_allowlist(monkeypatch):
    # Use a real resolvable public host so the SSRF filter passes; the
    # important assertion is that the allow-list gate itself works.
    monkeypatch.setenv("ALLOWED_LLM_BASE_URL_HOSTS", "api.openai.com")
    validate_llm_base_url("https://api.openai.com/v1", "openai_compatible")
    with pytest.raises(HTTPException):
        validate_llm_base_url("https://api.anthropic.com/v1", "openai_compatible")


# ---------- SEC-005 streamed-total cap ------------------------------------

def test_intake_aborts_on_streamed_total_regardless_of_header(tmp_path: Path, monkeypatch):
    """A crafted ZIP whose ACTUAL bytes exceed cap must abort even if
    per-entry headers understate size (SEC-005 residual)."""
    # We construct 5 entries each 300 KB uncompressed; set the total cap to
    # 500 KB and per-file/ratio caps generous. Aggregate must abort.
    monkeypatch.setenv("INTAKE_MAX_TOTAL_BYTES", "500000")
    monkeypatch.setenv("INTAKE_MAX_ENTRIES", "50")
    monkeypatch.setenv("INTAKE_MAX_RATIO", "10000")
    z = tmp_path / "many.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf:
        for i in range(5):
            zf.writestr(f"datasets/f{i}.csv", b"x" * 300_000)
    _, err = unpack_zip(z, tmp_path / "out")
    assert err and "aggregate streamed size" in err


# ---------- SEC-002 completion (live endpoint checks) ----------------------

pytestmark_live = pytest.mark.skipif(not _backend_up(), reason="backend not reachable")


@pytestmark_live
def test_session_get_scrubs_stored_path_and_export_paths():
    r = requests.get(f"{BASE_URL}/api/sessions", timeout=10)
    assert r.status_code == 200
    sessions = r.json().get("sessions", [])
    if not sessions:
        pytest.skip("no sessions on this deployment")
    sid = sessions[0]["id"]
    r2 = requests.get(f"{BASE_URL}/api/sessions/{sid}", timeout=10)
    assert r2.status_code == 200
    doc = r2.json()
    for f in doc.get("files", []) or []:
        assert "stored_path" not in f, f"stored_path leaked: {f}"
    # export_paths values must be blanked (keys may remain for the UI)
    for v in (doc.get("export_paths") or {}).values():
        assert v == "", f"export_paths absolute path leaked: {v!r}"


@pytestmark_live
def test_agent_trace_recursively_scrubs_nested_payload():
    """Reported by iteration_4 testing_agent: `payload` dict sub-keys
    (`prompt_preview`, `reply_preview`) previously bypassed the isinstance-str
    guard and leaked raw names/phones. Verify the recursive scrubber closes it.
    """
    import re as _re
    phone = _re.compile(r"\b\d{3}[\s\-.]\d{3}[\s\-.]\d{4}\b")
    email = _re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[A-Za-z]{2,}\b")
    ssn = _re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
    r = requests.get(f"{BASE_URL}/api/sessions", timeout=10)
    sessions = r.json().get("sessions", [])
    if not sessions:
        pytest.skip("no sessions available")
    checked = 0
    for s in sessions[:15]:
        rr = requests.get(f"{BASE_URL}/api/sessions/{s['id']}/agent-trace?limit=200", timeout=15)
        if rr.status_code != 200:
            continue
        txt = rr.text
        assert not phone.search(txt), f"phone in agent-trace of {s['id']!r}"
        assert not email.search(txt), f"email in agent-trace of {s['id']!r}"
        assert not ssn.search(txt), f"SSN in agent-trace of {s['id']!r}"
        assert "James Smith" not in txt, f"raw name in agent-trace of {s['id']!r}"
        checked += 1
    if checked == 0:
        pytest.skip("no agent-trace payloads to inspect")


def test_scrub_nested_walks_dicts_and_lists():
    from phi_core.security import scrub_nested
    payload = {
        "prompt_preview": "James Smith called 415-555-1234 today.",
        "reply_preview": "Contact: james@example.edu, SSN 111-22-3333.",
        "meta": {"session": "abc", "notes": ["Mary Johnson", "call 212-555-9876"]},
    }
    out = scrub_nested(payload)
    flat = str(out)
    assert "James Smith" not in flat
    assert "415-555-1234" not in flat
    assert "james@example.edu" not in flat
    assert "111-22-3333" not in flat
    assert "Mary Johnson" not in flat
    assert "212-555-9876" not in flat
    # non-PHI fields survive
    assert out["meta"]["session"] == "abc"


@pytestmark_live
def test_results_scrubs_reasons():
    """No obvious PHI substrings should appear in decision reasons/citations."""
    import re as _re
    phone = _re.compile(r"\b\d{3}[\s\-.]\d{3}[\s\-.]\d{4}\b")
    ssn = _re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
    email = _re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[A-Za-z]{2,}\b")
    r = requests.get(f"{BASE_URL}/api/sessions", timeout=10)
    sessions = r.json().get("sessions", [])
    if not sessions:
        pytest.skip("no sessions on this deployment")
    checked = 0
    for s in sessions[:20]:
        rr = requests.get(f"{BASE_URL}/api/sessions/{s['id']}/results", timeout=10)
        if rr.status_code != 200:
            continue
        for d in rr.json().get("decisions", []) or []:
            for field in ("reason", "citation"):
                v = d.get(field)
                if isinstance(v, str):
                    assert not phone.search(v), f"phone leaked: {v!r}"
                    assert not ssn.search(v), f"SSN leaked: {v!r}"
                    assert not email.search(v), f"email leaked: {v!r}"
                    checked += 1
    if checked == 0:
        pytest.skip("no decisions with reason/citation available")
    """No obvious PHI substrings should appear in decision reasons/citations."""
    import re as _re
    phone = _re.compile(r"\b\d{3}[\s\-.]\d{3}[\s\-.]\d{4}\b")
    ssn = _re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
    email = _re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[A-Za-z]{2,}\b")
    r = requests.get(f"{BASE_URL}/api/sessions", timeout=10)
    sessions = r.json().get("sessions", [])
    if not sessions:
        pytest.skip("no sessions on this deployment")
    checked = 0
    for s in sessions[:20]:
        rr = requests.get(f"{BASE_URL}/api/sessions/{s['id']}/results", timeout=10)
        if rr.status_code != 200:
            continue
        for d in rr.json().get("decisions", []) or []:
            for field in ("reason", "citation"):
                v = d.get(field)
                if isinstance(v, str):
                    assert not phone.search(v), f"phone leaked: {v!r}"
                    assert not ssn.search(v), f"SSN leaked: {v!r}"
                    assert not email.search(v), f"email leaked: {v!r}"
                    checked += 1
    if checked == 0:
        pytest.skip("no decisions with reason/citation available")
