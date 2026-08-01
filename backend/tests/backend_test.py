"""End-to-end backend API tests for PHI Handling Console v2.0.0."""
import os
import time

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://4c41e485-69a1-4a28-aef4-6d5a717dfaab.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"


# ------- Health --------------------------------------------------------------
def test_health():
    r = requests.get(f"{API}/health", timeout=30)
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "ok"
    assert d["version"] == "2.0.0"
    assert d["supported_jurisdictions"] == ["us"]
    assert len(d["hipaa_categories"]) == 18


# ------- Corpus --------------------------------------------------------------
@pytest.fixture(scope="module")
def corpus():
    payload = {"jurisdiction": "us", "seed": 20260420, "count_per_category": 2, "include_quasi_identifiers": True}
    r = requests.post(f"{API}/corpus/generate", json=payload, timeout=60)
    assert r.status_code == 200, r.text
    return r.json()


def test_corpus_generate_deterministic(corpus):
    assert corpus["total_records"] == 40
    assert corpus["total_gold_spans"] >= 71
    h1 = corpus["hash"]
    r = requests.post(f"{API}/corpus/generate", json={
        "jurisdiction": "us", "seed": 20260420, "count_per_category": 2, "include_quasi_identifiers": True
    }, timeout=60)
    assert r.status_code == 200
    assert r.json()["hash"] == h1


def test_corpus_list(corpus):
    r = requests.get(f"{API}/corpus", timeout=30)
    assert r.status_code == 200
    ids = [c["id"] for c in r.json()["corpora"]]
    assert corpus["id"] in ids


# ------- Benchmark -----------------------------------------------------------
def test_benchmark_run(corpus):
    r = requests.post(f"{API}/benchmark/run", json={
        "corpus_id": corpus["id"], "detectors": ["presidio", "rule"]
    }, timeout=180)
    assert r.status_code == 200, r.text
    d = r.json()
    assert "precision" in d and "recall" in d and "f1" in d
    # Baseline ~0.78 (allow tolerance)
    assert d["f1"] >= 0.70, f"F1 too low: {d['f1']}"
    # per-category recall
    per = d.get("per_category", {})
    high_recall = sum(1 for v in per.values() if v.get("recall", 0) >= 0.99)
    assert high_recall >= 15, f"Only {high_recall} categories with recall>=1.0; details={per}"


# ------- Sessions ------------------------------------------------------------
@pytest.fixture
def session_id():
    r = requests.post(f"{API}/sessions", json={"jurisdiction": "us"}, timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "created"
    return d["id"]


def test_session_get_and_list(session_id):
    r = requests.get(f"{API}/sessions/{session_id}", timeout=30)
    assert r.status_code == 200
    d = r.json()
    assert d["files"] == [] and d["spans"] == [] and d["progress"] == []
    rl = requests.get(f"{API}/sessions", timeout=30)
    assert rl.status_code == 200
    assert any(s["id"] == session_id for s in rl.json()["sessions"])


def _wait_status(sid: str, target: set, timeout: int = 90) -> dict:
    """Poll session until status in target or timeout."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = requests.get(f"{API}/sessions/{sid}", timeout=30)
        assert r.status_code == 200
        last = r.json()
        if last["status"] in target:
            return last
        time.sleep(1.5)
    raise AssertionError(f"Timeout waiting for {target}; last status={last.get('status')} error={last.get('error')}")


CSV_CONTENT = "patient_name,dob,ssn,mrn,phone,email,notes\nJohn Doe,1980-01-15,123-45-6789,MRN-12345678,4155551234,john@example.com,Patient is stable\nJane Roe,1975-06-20,987-65-4321,MRN-87654321,4155559876,jane@example.com,Followup scheduled\n"


def test_dataset_flow_end_to_end(session_id):
    # Upload
    files = {"file": ("patients.csv", CSV_CONTENT, "text/csv")}
    r = requests.post(f"{API}/sessions/{session_id}/upload", files=files, timeout=30)
    assert r.status_code == 200, r.text
    # Run
    r = requests.post(f"{API}/sessions/{session_id}/run", timeout=30)
    assert r.status_code == 200
    state = _wait_status(session_id, {"awaiting_review", "failed"}, timeout=120)
    assert state["status"] == "awaiting_review", f"failed: {state.get('error')}"

    # Validate file classification
    assert len(state["files"]) == 1
    f = state["files"][0]
    assert f["kind"] == "dataset"
    for col in ["patient_name", "dob", "ssn", "mrn", "phone", "email", "notes"]:
        assert col in f["columns"], f"missing column {col}"
    cls = f.get("llm_classification") or {}
    assert cls.get("content_type") == "structured_dataset", f"cls={cls}"
    notes_str = (cls.get("notes") or "") + " " + " ".join(str(v) for v in cls.values() if isinstance(v, str))
    assert "164.514" in notes_str, f"missing 164.514 citation in classification: {cls}"

    # Validate header_hint spans cover required categories
    header_hits = [s for s in state["spans"] if s["detector"] == "header_hint"]
    required = {("NAME", "A"), ("DATE", "C"), ("SSN", "G"), ("MRN", "H"), ("PHONE", "D"), ("EMAIL", "F")}
    got = {(s["entity_type"], s["hipaa_category"]) for s in header_hits}
    assert required.issubset(got), f"missing header hints. got={got}"

    # Review: accept all
    accepts = [{"span_id": s["span_id"], "action": "accept", "comment": "ok"} for s in state["spans"] if s["detector"] == "header_hint"]
    r = requests.post(f"{API}/sessions/{session_id}/review", json={"decisions": accepts, "add_manual_spans": [], "continue_iteration": False}, timeout=30)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "applying_review"

    # Finalize
    r = requests.post(f"{API}/sessions/{session_id}/finalize", timeout=60)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "complete"

    state2 = requests.get(f"{API}/sessions/{session_id}", timeout=30).json()
    assert state2["status"] == "complete"
    exports = state2.get("export_paths") or {}
    assert exports, "export_paths empty"
    file_id = list(exports.keys())[0]

    # Export download
    r = requests.get(f"{API}/sessions/{session_id}/export/{file_id}", timeout=30)
    assert r.status_code == 200
    body = r.text
    # Check redaction markers
    assert "[REDACTED:A:NAME]" in body
    assert "[REDACTED:G:SSN]" in body
    assert "[REDACTED:H:MRN]" in body
    # Non-PHI notes preserved
    assert "Patient is stable" in body or "Followup scheduled" in body


NARRATIVE_TXT = (
    "Progress note for John Smith, DOB 03/15/1975.\n"
    "SSN: 123-45-6789. MRN: MRN-12345678.\n"
    "Contact: (415) 555-1234, email john.smith@example.com.\n"
    "Portal: https://portal.hospital.example.com/patients/12345\n"
    "Access IP: 192.168.1.100.\n"
    "Enrolled in trial NCT12345678.\n"
)


def test_narrative_flow(session_id):
    # need fresh session
    sid = requests.post(f"{API}/sessions", json={"jurisdiction": "us"}, timeout=30).json()["id"]
    files = {"file": ("note.txt", NARRATIVE_TXT, "text/plain")}
    r = requests.post(f"{API}/sessions/{sid}/upload", files=files, timeout=30)
    assert r.status_code == 200
    r = requests.post(f"{API}/sessions/{sid}/run", timeout=30)
    assert r.status_code == 200
    state = _wait_status(sid, {"awaiting_review", "failed"}, timeout=120)
    assert state["status"] == "awaiting_review", state.get("error")

    spans = state["spans"]
    # This narrative contains 9 canonical HIPAA identifiers (name, DOB,
    # SSN, MRN, phone, email, URL, IP, trial code). The per-category
    # coverage check below is the stronger assertion; keep the count
    # gate loose so a detector improvement that merges two adjacent
    # spans into one (or emits an extra sub-span) does not flake this.
    assert len(spans) >= 9, f"only got {len(spans)} spans"
    cats = {s["hipaa_category"] for s in spans}
    required_cats = {"A", "C", "D", "F", "G", "H", "N", "O", "R"}
    missing = required_cats - cats
    assert not missing, f"missing categories: {missing}; got {cats}"

    values = " ".join(s["value"] for s in spans)
    assert "123-45-6789" in values
    assert "192.168.1.100" in values
    assert "NCT12345678" in values


def test_session_not_found():
    r = requests.get(f"{API}/sessions/nonexistent-id", timeout=30)
    assert r.status_code == 404
