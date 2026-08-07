"""Backend tests for the 12-agent PHI handling pipeline.

Covers:
  - /api/health (version + 18 HIPAA categories)
  - /api/settings/llm GET+POST (BYO-key, api_key never returned)
  - full intake -> handle -> results flow using /root/fixtures/study1.zip
  - agent-trace has messages spanning multiple specialists
  - decisions[] for direct identifiers
  - human-review resume + export CSV redaction content check
"""
from __future__ import annotations

import io
import os
import csv
import time
import zipfile
from pathlib import Path

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://quality-enhance-9.preview.emergentagent.com").rstrip("/")
FIXTURE_ZIP = Path("/root/fixtures/study1.zip")

TIMEOUT = 30
POLL_TIMEOUT_SEC = 300  # up to 5 min for full agent pipeline


@pytest.fixture(scope="session")
def api():
    s = requests.Session()
    return s


@pytest.fixture(scope="session")
def study_zip_bytes():
    if FIXTURE_ZIP.exists():
        return FIXTURE_ZIP.read_bytes()
    # Rebuild fixture
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("datasets/patients.csv",
                   "patient_name,dob,ssn,mrn,phone,email,notes\n"
                   "James Smith,03/15/1975,123-45-6789,MRN-12345678,(415) 555-1234,james.smith@example.edu,Mild headache\n"
                   "Mary Johnson,07/22/1982,987-65-4321,MRN-98765432,(212) 555-9876,mary.j@example.edu,Followup HTN\n")
        z.writestr("data_dictionary/columns.csv",
                   "column_name,description,phi_flag\n"
                   "patient_name,Full patient name,yes\n"
                   "ssn,Social Security Number,yes\n")
        # minimal PDF
        z.writestr("forms/consent.pdf", b"%PDF-1.4\n%EOF\n")
    return buf.getvalue()


# ------------------------- Health -----------------------------------------

def test_health(api):
    r = api.get(f"{BASE_URL}/api/health", timeout=TIMEOUT)
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "ok"
    assert d["version"] == "2.0.0"
    assert isinstance(d["hipaa_categories"], dict)
    assert len(d["hipaa_categories"]) == 18


# ------------------------- LLM settings -----------------------------------

def test_llm_settings_default(api):
    r = api.get(f"{BASE_URL}/api/settings/llm", timeout=TIMEOUT)
    assert r.status_code == 200
    d = r.json()
    assert d["provider"] == "emergent"
    assert d["model"] == "claude-sonnet-4-5-20250929"
    # `openai_compatible` is opt-in via ALLOWED_LLM_BASE_URL_HOSTS env var
    # so the endpoint hides it from the default provider list. This is a
    # SSRF-defence design decision (see phi_core/security.py:52).
    for p in ("emergent", "anthropic", "openai", "gemini", "openrouter"):
        assert p in d["providers"], f"missing {p}: {d['providers']}"


def test_llm_settings_post_persist(api):
    payload = {
        "provider": "anthropic",
        "model": "claude-sonnet-4-5-20250929",
        "api_key": "sk-test-should-not-leak",
        "base_url": "",
        "temperature": 0.2,
        "max_tokens": 1500,
    }
    r = api.post(f"{BASE_URL}/api/settings/llm", json=payload, timeout=TIMEOUT)
    assert r.status_code == 200
    r2 = api.get(f"{BASE_URL}/api/settings/llm", timeout=TIMEOUT)
    d = r2.json()
    assert d["provider"] == "anthropic"
    assert d["temperature"] == 0.2
    assert d.get("api_key", "") == ""
    assert d.get("api_key_set") is True
    # Reset to emergent default so the pipeline test uses EMERGENT_LLM_KEY
    api.post(f"{BASE_URL}/api/settings/llm", json={
        "provider": "emergent",
        "model": "claude-sonnet-4-5-20250929",
        "api_key": "",
        "base_url": "",
        "temperature": 0.1,
        "max_tokens": 2000,
    }, timeout=TIMEOUT).raise_for_status()


# ------------------------- Full pipeline ---------------------------------

@pytest.fixture(scope="session")
def session_id(api, study_zip_bytes):
    r = api.post(f"{BASE_URL}/api/sessions", json={"jurisdiction": "us"}, timeout=TIMEOUT)
    assert r.status_code == 200
    sid = r.json()["id"]
    # Intake
    files = {"file": ("study1.zip", study_zip_bytes, "application/zip")}
    r2 = api.post(f"{BASE_URL}/api/sessions/{sid}/intake", files=files, timeout=60)
    assert r2.status_code == 200, r2.text
    d = r2.json()
    assert d["status"] == "ready", f"intake status={d.get('status')} err={d.get('error')} missing={d.get('missing_components')}"
    assert d["linked"] == 3, f"linked={d['linked']}"
    return sid


def _poll_until(api, sid, statuses, timeout=POLL_TIMEOUT_SEC):
    start = time.time()
    last = None
    while time.time() - start < timeout:
        r = api.get(f"{BASE_URL}/api/sessions/{sid}", timeout=TIMEOUT)
        r.raise_for_status()
        s = r.json()
        last = s
        if s.get("status") in statuses:
            return s
        time.sleep(3)
    pytest.fail(f"Timed out waiting for status {statuses}; last status={last.get('status') if last else None} err={last.get('error') if last else None}")


def test_handle_pipeline_run(api, session_id):
    r = api.post(f"{BASE_URL}/api/sessions/{session_id}/handle", timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "started"

    s = _poll_until(api, session_id, {"complete", "awaiting_human_review", "failed"})
    assert s["status"] in {"complete", "awaiting_human_review"}, f"pipeline failed: {s.get('error')}"


def test_agent_trace(api, session_id):
    r = api.get(f"{BASE_URL}/api/sessions/{session_id}/agent-trace", timeout=TIMEOUT)
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert len(msgs) >= 10, f"only {len(msgs)} agent messages"
    agents = {m.get("agent") for m in msgs}
    # Spec asks for spans: Lexicon, Schema, Instrument, Statute, Judge, Sentinel
    expected = {"Lexicon", "Schema", "Instrument", "Statute", "Judge", "Sentinel"}
    missing = expected - agents
    assert not missing, f"missing agents in trace: {missing}. Present: {agents}"


def test_results_decisions(api, session_id):
    r = api.get(f"{BASE_URL}/api/sessions/{session_id}/results", timeout=TIMEOUT)
    assert r.status_code == 200
    res = r.json()
    decisions = res.get("decisions") or []
    assert len(decisions) >= 4, f"only {len(decisions)} decisions"
    allowed_actions = {"keep", "drop", "cap_age_90", "year_only", "zip3_truncate",
                       "hash", "pseudonymize", "scrub_text", "human_review"}
    for d in decisions:
        assert set(["file_id", "column", "action", "reason", "confidence"]).issubset(d.keys()), d
        assert d["action"] in allowed_actions, f"bad action: {d['action']}"

    # Direct identifier expectations
    by_col = {d["column"].lower(): d for d in decisions}

    def action_of(*names):
        for n in names:
            if n in by_col:
                return by_col[n]["action"]
        return None

    a_name = action_of("patient_name", "name")
    a_ssn = action_of("ssn")
    a_dob = action_of("dob", "date_of_birth")
    a_mrn = action_of("mrn", "medical_record_number")
    a_notes = action_of("notes")

    assert a_name in {"drop", "pseudonymize"}, f"patient_name -> {a_name}"
    assert a_ssn == "drop", f"ssn -> {a_ssn}"
    assert a_dob in {"year_only", "drop"}, f"dob -> {a_dob}"
    assert a_mrn in {"drop", "pseudonymize", "hash"}, f"mrn -> {a_mrn}"
    # notes may or may not be human_review depending on LLM; log for visibility
    print(f"notes action = {a_notes}")


def test_human_review_and_export(api, session_id):
    """Resolve any human_review decisions, poll to complete, then verify exports and redaction."""
    res = api.get(f"{BASE_URL}/api/sessions/{session_id}/results", timeout=TIMEOUT).json()
    session = api.get(f"{BASE_URL}/api/sessions/{session_id}", timeout=TIMEOUT).json()

    if res.get("human_review_required") or session.get("status") == "awaiting_human_review":
        pending = [d for d in res["decisions"] if d.get("action") == "human_review"]
        resolutions = [{"file_id": d["file_id"], "column": d["column"], "action": "drop"} for d in pending]
        r = api.post(f"{BASE_URL}/api/sessions/{session_id}/human-review",
                     json={
                         "resolutions": resolutions,
                         "reviewer": "test-suite@phi-console.local",
                         "comment": "automated regression test",
                         "actual_knowledge_ack": True,
                     }, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        _poll_until(api, session_id, {"complete", "failed"})

    final = api.get(f"{BASE_URL}/api/sessions/{session_id}", timeout=TIMEOUT).json()
    assert final["status"] == "complete", f"final status={final['status']} err={final.get('error')}"
    exports = final.get("export_paths") or {}
    assert exports, "export_paths is empty"

    # Now fetch the dataset export and validate redaction content
    files = final.get("files") or []
    dataset_file = next((f for f in files if f.get("kind") == "dataset"), None)
    assert dataset_file, "no dataset file in session.files"
    fid = dataset_file["file_id"]
    assert fid in exports, f"no export for dataset {fid}"

    r = api.get(f"{BASE_URL}/api/sessions/{session_id}/export/{fid}", timeout=TIMEOUT)
    assert r.status_code == 200
    content = r.content.decode("utf-8", errors="replace")
    print("EXPORTED CSV:\n" + content[:2000])

    reader = csv.DictReader(io.StringIO(content))
    rows = list(reader)
    assert rows, "exported csv has no data rows"

    results = api.get(f"{BASE_URL}/api/sessions/{session_id}/results", timeout=TIMEOUT).json()
    ds_decisions = {d["column"]: d["action"] for d in results["decisions"] if d["file_id"] == fid}

    for col, action in ds_decisions.items():
        if col not in rows[0]:
            continue
        vals = [r[col] for r in rows if r.get(col) is not None]
        if action == "drop":
            assert all(v == "" for v in vals), f"col {col} action=drop but contains: {vals}"
        elif action == "year_only":
            for v in vals:
                if v == "":
                    continue
                assert v.isdigit() and len(v) == 4, f"col {col} year_only got {v!r}"
        elif action == "pseudonymize":
            for v in vals:
                if v == "":
                    continue
                # hex-like token, allow prefix/dash
                stripped = v.replace("-", "").replace("_", "")
                assert all(c in "0123456789abcdefABCDEF" for c in stripped), f"col {col} pseudonymize got {v!r}"


def test_results_audit_ledger_herald_populated(api, session_id):
    r = api.get(f"{BASE_URL}/api/sessions/{session_id}/results", timeout=TIMEOUT)
    res = r.json()
    audit = res.get("audit") or {}
    ledger = res.get("ledger") or {}
    herald = res.get("herald") or {}

    assert audit.get("verdict"), f"audit missing verdict: {audit}"
    assert audit.get("summary"), f"audit missing summary: {audit}"

    assert ledger.get("headline"), f"ledger missing headline: {ledger}"
    assert isinstance(ledger.get("comparisons"), list), f"ledger comparisons not list: {ledger}"

    assert herald.get("title"), f"herald missing title: {herald}"
    assert herald.get("abstract"), f"herald missing abstract: {herald}"
    assert isinstance(herald.get("sections"), list) and herald["sections"], f"herald sections empty: {herald}"
