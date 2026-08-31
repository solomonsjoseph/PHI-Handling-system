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

import csv
import io
import os
import time
import zipfile
from pathlib import Path

import pytest
import requests

if not os.environ.get("PHI_TEST_BASE_URL"):
    pytest.skip(
        "test_agent_pipeline.py hits a live server over the network; "
        "set PHI_TEST_BASE_URL to opt in (a bare `pytest` must never call the internet)",
        allow_module_level=True,
    )

BASE_URL = os.environ.get("PHI_TEST_BASE_URL", "").rstrip("/")
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


def test_session_not_found(api):
    r = api.get(f"{BASE_URL}/api/sessions/nonexistent-id", timeout=TIMEOUT)
    assert r.status_code == 404


# ------------------------- LLM settings -----------------------------------

def test_llm_settings_default(api):
    r = api.get(f"{BASE_URL}/api/settings/llm", timeout=TIMEOUT)
    assert r.status_code == 200
    d = r.json()
    # First-boot seeds provider + model from env / catalog so a run works
    # before the operator opens Settings.
    assert d["provider"] in ("openrouter", "openai", "anthropic", "gemini")
    assert d["model"]
    # Settings advertises exactly the four UI providers.
    assert d["providers"] == ["openrouter", "openai", "anthropic", "gemini"]


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
    # Reset to env-default ChatGPT/OpenAI so subsequent pipeline tests use
    # OPENAI_API_KEY when that is what the pod has configured.
    api.post(f"{BASE_URL}/api/settings/llm", json={
        "provider": "openai",
        "model": "gpt-5.2",
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
    # Spec asks for spans: Lexicon, Schema, Instrument, RegulationsExpert, Judge, Reviewer
    expected = {"Lexicon", "Schema", "Instrument", "RegulationsExpert", "Judge", "Reviewer"}
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
        # Server-side gate requires at least one dataset-file download before
        # any non-defer resolution is accepted.
        dataset_file = next(f for f in session.get("files", []) if f.get("kind") == "dataset")
        dl = api.get(f"{BASE_URL}/api/sessions/{session_id}/dataset-file/{dataset_file['file_id']}", timeout=TIMEOUT)
        assert dl.status_code == 200, dl.text

        # Round 1: comment-resolve every pending column (Judge interprets the
        # free text into a concrete action; the old client-supplied `action`
        # field no longer exists on the wire).
        pending = [d for d in res["decisions"] if d.get("action") == "human_review"]
        resolutions = [
            {"file_id": d["file_id"], "column": d["column"], "mode": "comment",
             "comment": "this is a direct identifier with no research value; drop it"}
            for d in pending
        ]
        r = api.post(f"{BASE_URL}/api/sessions/{session_id}/human-review",
                     json={
                         "resolutions": resolutions,
                         "reviewer": "test-suite@phi-console.local",
                         "comment": "automated regression test",
                         "actual_knowledge_ack": True,
                     }, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        state = _poll_until(api, session_id, {"complete", "partially_complete", "awaiting_human_review", "failed"})

        # Round 2: a low-confidence interpretation from round 1 lands in
        # `pending_confirmation` rather than applying outright -- confirm it.
        if state.get("status") in ("awaiting_human_review", "partially_complete"):
            res2 = api.get(f"{BASE_URL}/api/sessions/{session_id}/results", timeout=TIMEOUT).json()
            still_pending = [d for d in res2["decisions"] if d.get("action") == "human_review"]
            confirmable = [d for d in still_pending if d.get("pending_confirmation")]
            if confirmable:
                r = api.post(f"{BASE_URL}/api/sessions/{session_id}/human-review",
                             json={
                                 "resolutions": [
                                     {"file_id": d["file_id"], "column": d["column"], "mode": "approve"}
                                     for d in confirmable
                                 ],
                                 "reviewer": "test-suite@phi-console.local",
                                 "comment": "confirming round-1 interpretation",
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


def test_results_no_longer_include_audit_ledger_herald_by_default(api, session_id):
    """Phase 17-B: Auditor is retired and Scout/Ledger/Herald moved off the
    mandatory PHI path into an opt-in post-run report
    (POST /api/sessions/{sid}/post-run-report). A normal completed session's
    results must not carry populated audit/ledger/herald fields; those keys
    are either absent or present-but-empty until the opt-in endpoint below
    is explicitly called."""
    r = api.get(f"{BASE_URL}/api/sessions/{session_id}/results", timeout=TIMEOUT)
    res = r.json()
    assert not (res.get("audit") or {}).get("verdict"), \
        f"audit populated by default, but Auditor is retired: {res.get('audit')}"
    assert not (res.get("ledger") or {}).get("headline"), \
        f"ledger populated by default, but Ledger is opt-in: {res.get('ledger')}"
    assert not (res.get("herald") or {}).get("title"), \
        f"herald populated by default, but Herald is opt-in: {res.get('herald')}"


def test_post_run_report_endpoint_populates_scout_ledger_herald(api, session_id):
    """The opt-in POST /post-run-report endpoint (Phase 17-B) is the only
    way to generate Scout/Ledger/Herald output; it must never run
    automatically and must produce real content when explicitly called."""
    r = api.post(f"{BASE_URL}/api/sessions/{session_id}/post-run-report", timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    report = r.json()

    ledger = report.get("ledger") or {}
    herald = report.get("herald") or {}

    assert ledger.get("headline"), f"ledger missing headline: {ledger}"
    assert isinstance(ledger.get("comparisons"), list), f"ledger comparisons not list: {ledger}"

    assert herald.get("title"), f"herald missing title: {herald}"
    assert herald.get("abstract"), f"herald missing abstract: {herald}"
    assert isinstance(herald.get("sections"), list) and herald["sections"], f"herald sections empty: {herald}"

    assert report.get("generated_at"), f"post-run-report missing generated_at: {report}"


# ------------------------- Anti-loop & Sentinel escalate (live) -----------
#
# Task 25: live evidence for two paths that never fired on either prior
# live run (the Judge-redesign and Sentinel-design work). Both build
# deliberately adversarial corpus rather than reusing the shared
# `session_id` fixture's TB-shaped study1.zip, because the shared corpus's
# `study_id` column is now caught by `_HARD_RULE_TABLE` before Judge or
# Sentinel ever see it and can no longer reproduce the original
# disagreement.

_ANTI_LOOP_COLUMN = "enrollment_reference"
_ESCALATE_COLUMN = "screening_result_code"


@pytest.fixture(scope="module")
def escalation_zip_bytes():
    rows = [
        ("James Smith", "03/15/1975", "ENR-000101", "SR-COMMON-A"),
        ("Mary Johnson", "07/22/1982", "ENR-000102", "SR-COMMON-B"),
        ("Robert Lee", "11/02/1969", "ENR-000103", "SR-COMMON-A"),
        ("Linda Nguyen", "05/30/1990", "ENR-000104", "SR-COMMON-C"),
        ("Carlos Diaz", "09/14/1988", "ENR-000105", "SR-RARE-001"),
        ("Aisha Bello", "01/08/1977", "ENR-000106", "SR-COMMON-B"),
        ("Wei Chen", "12/25/1993", "ENR-000107", "SR-COMMON-A"),
        ("Fatima Khan", "04/19/1985", "ENR-000108", "SR-COMMON-C"),
        ("Tom Walker", "08/11/1971", "ENR-000109", "SR-COMMON-B"),
        ("Grace Kim", "02/27/1994", "ENR-000110", "SR-COMMON-A"),
    ]
    dataset_out = io.StringIO()
    w = csv.writer(dataset_out)
    w.writerow(["patient_name", "dob", _ANTI_LOOP_COLUMN, _ESCALATE_COLUMN])
    w.writerows(rows)

    dictionary_out = io.StringIO()
    dw = csv.writer(dictionary_out)
    dw.writerow(["column_name", "description", "phi_flag"])
    dw.writerow(["patient_name", "Full name of the study participant.", "yes"])
    dw.writerow(["dob", "Date of birth of the study participant.", "yes"])
    dw.writerow([
        _ANTI_LOOP_COLUMN,
        (
            "Internal linkage code assigned once per participant at enrollment; format ENR- "
            "followed by a six-digit sequence. Used only to join this dataset with other files "
            "inside the same study bundle. Not a name, date, or contact detail, but unique to one "
            "participant across the entire submission, so retaining it as-is would let anyone who "
            "obtains two files from this study re-associate a participant's records."
        ),
        "no",
    ])
    dw.writerow([
        _ESCALATE_COLUMN,
        (
            "Coded outcome of the initial screening visit. SR-COMMON-A/B/C mark the three most "
            "frequent outcomes, each shared by hundreds of participants across the parent cohort "
            "with no linkage risk on its own. SR-RARE-001 marks an outcome documented in fewer "
            "than five individuals in the entire national cohort; whether it appears in this "
            "file's rows cannot be told from this dictionary entry, and if it does, that value "
            "combined with the participant's approximate enrollment period could support "
            "re-identification under 45 CFR 164.514(b)(2)(ii). Whether this column is a safe "
            "coded clinical variable or a quasi-identifier turns on cell-level distribution this "
            "dictionary cannot show."
        ),
        "unknown",
    ])

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("datasets/screening.csv", dataset_out.getvalue())
        z.writestr("data_dictionary/columns.csv", dictionary_out.getvalue())
        z.writestr("forms/consent.pdf", b"%PDF-1.4\n%EOF\n")
    return buf.getvalue()


@pytest.fixture(scope="module")
def escalation_session_id(api, escalation_zip_bytes):
    r = api.post(f"{BASE_URL}/api/sessions", json={"jurisdiction": "us"}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    sid = r.json()["id"]

    files = {"file": ("screening.zip", escalation_zip_bytes, "application/zip")}
    r2 = api.post(f"{BASE_URL}/api/sessions/{sid}/intake", files=files, timeout=60)
    assert r2.status_code == 200, r2.text
    d = r2.json()
    assert d["status"] == "ready", f"intake status={d.get('status')} err={d.get('error')} missing={d.get('missing_components')}"
    assert d["linked"] == 3, f"linked={d['linked']}"

    r3 = api.post(f"{BASE_URL}/api/sessions/{sid}/handle", timeout=TIMEOUT)
    assert r3.status_code == 200, r3.text
    assert r3.json()["status"] == "started"

    s = _poll_until(api, sid, {"complete", "awaiting_human_review", "partially_complete", "failed"})
    assert s["status"] != "failed", f"pipeline failed: {s.get('error')}"
    return sid


def test_anti_loop_forces_human_review_on_repeated_rejection(api, escalation_session_id):
    """Judge redesign plan item 1: replay of the run-1 `study_id`-shaped
    hash-vs-Sentinel disagreement. `enrollment_reference` matches no
    `_HARD_RULE_TABLE` pattern, so Judge and Sentinel settle its action
    entirely from the dictionary text rather than a deterministic
    override -- the same conditions that produced the original repeated-
    action loop. Asserts the orchestrator's anti-loop rule
    (`orchestrator.py`) forces `human_review` the moment a revision
    repeats a previously-blocked action, with no further Judge<->Sentinel
    round trip, and that `suggested_*` carries Judge's last committed
    decision.
    """
    res = api.get(f"{BASE_URL}/api/sessions/{escalation_session_id}/results", timeout=TIMEOUT).json()
    decisions = res.get("decisions") or []
    by_col = {d["column"].lower(): d for d in decisions}
    d = by_col.get(_ANTI_LOOP_COLUMN)
    assert d is not None, f"{_ANTI_LOOP_COLUMN!r} missing from decisions: {sorted(by_col)}"

    phase_timings = res.get("phase_timings") or {}
    anti_loop_phases = sorted(k for k in phase_timings if k.startswith("anti_loop_iter_"))
    assert anti_loop_phases, (
        "orchestrator.py's anti-loop rule never fired on this live run "
        f"(phases seen: {sorted(phase_timings)}); {_ANTI_LOOP_COLUMN} ended "
        f"action={d.get('action')!r} reason={d.get('reason')!r} -- the model either agreed "
        "with Sentinel outright, or corrected itself on revision instead of repeating the "
        "rejected action."
    )
    assert d["action"] == "human_review", f"{_ANTI_LOOP_COLUMN} not forced to human_review: {d}"
    assert d.get("suggested_action"), f"suggested_action not populated: {d}"
    assert isinstance(d.get("suggested_confidence"), (int, float)), f"suggested_confidence not populated: {d}"
    assert d.get("suggested_reason"), f"suggested_reason not populated: {d}"
    assert "repeated" in d["suggested_reason"].lower(), (
        f"suggested_reason doesn't read like the anti-loop text: {d['suggested_reason']!r}"
    )


def test_sentinel_escalates_ambiguous_coded_column(api, escalation_session_id):
    """Sentinel plan item 3: a coded column that is plausibly either a
    safe categorical variable or a quasi-identifier depending on which
    rows carry the rare code, context Sentinel cannot see from headers
    alone. Asserts Sentinel raises `severity='escalate'` rather than
    silently agreeing or looping (`apply_sentinel_escalations`,
    `reasoning.py`), routing straight to `human_review` with
    `suggested_*` populated from Judge's last decision.
    """
    res = api.get(f"{BASE_URL}/api/sessions/{escalation_session_id}/results", timeout=TIMEOUT).json()
    decisions = res.get("decisions") or []
    by_col = {d["column"].lower(): d for d in decisions}
    d = by_col.get(_ESCALATE_COLUMN)
    assert d is not None, f"{_ESCALATE_COLUMN!r} missing from decisions: {sorted(by_col)}"

    phase_timings = res.get("phase_timings") or {}
    escalation_phases = sorted(k for k in phase_timings if k.startswith("sentinel_escalation_iter_"))
    assert escalation_phases, (
        "Sentinel never raised severity='escalate' on this live run "
        f"(phases seen: {sorted(phase_timings)}); {_ESCALATE_COLUMN} ended "
        f"action={d.get('action')!r} reason={d.get('reason')!r} -- Sentinel either agreed "
        "with Judge outright or treated the ambiguity as resolvable (blocking, with a "
        "confident correction) instead of recognizing genuine regulatory ambiguity."
    )
    assert d["action"] == "human_review", f"{_ESCALATE_COLUMN} not routed to human_review: {d}"
    assert (d.get("reason") or "").startswith("Sentinel escalation:"), f"reason missing the escalation prefix: {d}"
    assert d.get("suggested_action"), f"suggested_action not populated: {d}"
    assert isinstance(d.get("suggested_confidence"), (int, float)), f"suggested_confidence not populated: {d}"
    assert d.get("suggested_reason"), f"suggested_reason not populated: {d}"
    assert "ambiguous" in d["suggested_reason"].lower(), (
        f"suggested_reason doesn't read like the escalation text: {d['suggested_reason']!r}"
    )
