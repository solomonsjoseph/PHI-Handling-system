"""Human review invariant tests (GOAL clause 61-65).

Every human decision must carry reviewer id + comment + timestamp. The
endpoint refuses when reviewer is missing.
"""
import os
import re
import uuid
from types import SimpleNamespace

import pytest
import requests

from phi_core.agents.reasoning import (
    ACTION_TYPES,
    _ACTION_PLAIN,
    _CATEGORY_PLAIN,
    _escalation_reason_phrase,
    annotate_pending_review,
    apply_blocking_floor,
    apply_confidence_floor,
    apply_sentinel_escalations,
    validate_decisions,
    verify_keep_decisions,
)


BASE_URL = os.environ.get("PHI_TEST_BASE_URL", "http://localhost:8001")


def _backend_up() -> bool:
    try:
        return requests.get(f"{BASE_URL}/api/health", timeout=2).ok
    except Exception:
        return False


# Applied per-test to the four live-server tests below, not module-wide:
# the plain-English reviewer-prompt tests further down are pure Python and
# must run (and pass) with no backend reachable.
needs_backend = pytest.mark.skipif(not _backend_up(), reason="backend not reachable")


@needs_backend
def test_human_review_requires_reviewer():
    """Endpoint rejects when reviewer identity missing (GOAL invariant)."""
    # Use a non-existent session id — the reviewer check runs before the
    # session lookup, so we still see the 400 we want.
    fake_sid = uuid.uuid4().hex
    r = requests.post(
        f"{BASE_URL}/api/sessions/{fake_sid}/human-review",
        json={"resolutions": [], "reviewer": "", "comment": "no reviewer"},
        timeout=10,
    )
    assert r.status_code == 400, r.text
    assert "reviewer" in r.text.lower()


@needs_backend
def test_human_review_requires_actual_knowledge_ack():
    """HHS §164.514(b)(2)(ii): endpoint must reject when reviewer omits or
    denies actual-knowledge attestation, when the submission resolves at
    least one column. A pure-defer submission with nothing resolved makes
    no actual-knowledge claim and is exempt (see next test's sibling
    behavior) -- this test exercises the resolving case."""
    fake_sid = uuid.uuid4().hex
    r = requests.post(
        f"{BASE_URL}/api/sessions/{fake_sid}/human-review",
        json={
            "resolutions": [{"file_id": "f1", "column": "c1", "mode": "approve"}],
            "reviewer": "jane.doe@lab.edu",
            "comment": "test",
            "actual_knowledge_ack": False,
        },
        timeout=10,
    )
    assert r.status_code == 400, r.text
    assert "actual" in r.text.lower() and "knowledge" in r.text.lower()


@needs_backend
def test_human_review_pure_defer_exempt_from_actual_knowledge_ack():
    """A submission that only defers (nothing approved or comment-resolved)
    makes no actual-knowledge claim about anything, so the gate does not
    apply: the request proceeds past it straight to the session lookup,
    which 404s on a fake id rather than 400ing on the attestation gate."""
    fake_sid = uuid.uuid4().hex
    r = requests.post(
        f"{BASE_URL}/api/sessions/{fake_sid}/human-review",
        json={
            "resolutions": [{"file_id": "f1", "column": "c1", "mode": "defer"}],
            "reviewer": "jane.doe@lab.edu",
            "comment": "not ready to decide yet",
            "actual_knowledge_ack": False,
        },
        timeout=10,
    )
    assert r.status_code == 404, r.text


@needs_backend
def test_human_review_captures_session_review_when_provided():
    """Sending reviewer + comment + actual_knowledge_ack on an awaiting-review
    session must persist reviewer id, timestamp, comment, and the
    actual-knowledge attestation on the session document."""
    r = requests.get(f"{BASE_URL}/api/sessions", timeout=10)
    sessions = r.json().get("sessions", [])
    awaiting = [s for s in sessions if s.get("status") == "awaiting_human_review"]
    if not awaiting:
        pytest.skip("no awaiting_human_review session on this deployment")
    sid = awaiting[0]["id"]
    rr = requests.post(
        f"{BASE_URL}/api/sessions/{sid}/human-review",
        json={
            "resolutions": [],
            "reviewer": "test-reviewer@example.org",
            "comment": "accepted per QA plan v1",
            "actual_knowledge_ack": True,
        },
        timeout=10,
    )
    # 200 means the tail started; response contains status field
    assert rr.status_code == 200, rr.text
    assert rr.json().get("status") in ("resuming", "still_awaiting")


# --- Task 23: reviewer prompts speak plain English on every escalation
# path -----------------------------------------------------------------
#
# Pure Python, no live backend: these exercise the five deterministic
# routes that can set `action == "human_review"` --
# `validate_decisions` (invalid model output), `apply_confidence_floor`,
# `apply_blocking_floor`, `apply_sentinel_escalations`, and
# `verify_keep_decisions` -- and check the `reviewer_prompt`
# `annotate_pending_review` attaches to each never leaks a bare
# HIPAA letter, action identifier, confidence number, or agent name.

_BARE_ACTION_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(a) for a in _ACTION_PLAIN) + r")\b"
)
# Standalone uppercase HIPAA category letter, including a quoted one
# ('R', "R", (R)). The only exemption is 'I' immediately followed by an
# apostrophe -- an English contraction ("I'm", "I've", "I'll", "I'd"),
# which the prompts legitimately use for the first-person pronoun and
# which the implementation is written to always use (never a bare "I ").
# A quoted or otherwise bare 'I' (not part of a contraction) still
# matches, same as every other Safe Harbor letter.
_BARE_CATEGORY_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-HJ-R](?![A-Za-z0-9])|I(?!['A-Za-z0-9]))"
)
_BARE_CONFIDENCE_RE = re.compile(r"\d*\.\d+")
_AGENT_NAMES = ("Judge", "Sentinel", "Executor", "Auditor", "Reviewer", "Operator")


def _assert_plain_english(prompt: str) -> None:
    assert not _BARE_ACTION_RE.search(prompt), f"bare action id leaked: {prompt!r}"
    assert not _BARE_CATEGORY_RE.search(prompt), f"bare HIPAA letter leaked: {prompt!r}"
    assert not _BARE_CONFIDENCE_RE.search(prompt), f"bare confidence number leaked: {prompt!r}"
    for name in _AGENT_NAMES:
        assert name not in prompt, f"bare agent name leaked: {prompt!r}"


def test_action_plain_covers_every_executable_action():
    assert set(_ACTION_PLAIN) == ACTION_TYPES - {"human_review"}


def test_category_plain_covers_every_hipaa_letter_and_none_and_quasi():
    letters = {chr(c) for c in range(ord("A"), ord("R") + 1)}
    assert letters <= set(_CATEGORY_PLAIN)
    assert {"NONE", "QUASI"} <= set(_CATEGORY_PLAIN)


def test_bare_category_regex_catches_quoted_letter_but_not_i_contraction():
    """Direct regression for the quoted-category gap: a single-quoted
    (or otherwise bare) HIPAA letter like 'R' must be caught, while the
    'I' contractions the prompts actually use must not false-positive."""
    assert _BARE_CATEGORY_RE.search("category 'R' was assigned")
    assert _BARE_CATEGORY_RE.search('category "G" was assigned')
    assert _BARE_CATEGORY_RE.search("flagged as category R here")
    assert not _BARE_CATEGORY_RE.search("I'm not confident enough")
    assert not _BARE_CATEGORY_RE.search("I've seen this before")
    assert not _BARE_CATEGORY_RE.search("I'll leave it as is")
    assert not _BARE_CATEGORY_RE.search("I'd say this is fine")
    # A bare, non-contraction 'I' still counts as a leak.
    assert _BARE_CATEGORY_RE.search("this is category I territory")


def _path1_invalid_action_decision() -> dict:
    """Path 1: `validate_decisions` -- a model reply proposes an action
    outside the executable vocabulary."""
    raw = {
        "file_id": "f1", "column": "ssn", "action": "delete_forever",
        "subject": "participant", "phi_category": "G",
        "suggested_action": "drop", "suggested_confidence": 0.55,
        "suggested_reason": "looks like a direct identifier",
    }
    safe, _ = validate_decisions([raw])
    return safe[0]


def _path2_confidence_floor_decision() -> dict:
    """Path 2: `apply_confidence_floor` demotes a low-confidence keep."""
    raw = {"file_id": "f1", "column": "zip_code", "action": "keep",
           "confidence": 0.40, "reason": "looked safe on the header",
           "phi_category": "B"}
    out, _ = apply_confidence_floor([raw])
    return out[0]


def _path3_blocking_floor_decision() -> dict:
    """Path 3: `apply_blocking_floor` forces review after repeated
    blocking issues on the same column."""
    raw = {"file_id": "f1", "column": "notes", "action": "keep",
           "confidence": 0.9, "reason": "clinically useful",
           "phi_category": "NONE"}
    out, _ = apply_blocking_floor([raw], {("f1", "notes"): 3})
    return out[0]


def _path4_sentinel_escalation_decision() -> dict:
    """Path 4: `apply_sentinel_escalations` converts a Sentinel
    'escalate' verdict to human review."""
    raw = {"file_id": "f1", "column": "treatment_facility_name", "action": "keep",
           "confidence": 0.85, "reason": "clinically necessary", "phi_category": "R"}
    out, _ = apply_sentinel_escalations(
        [raw],
        [{"file_id": "f1", "column": "treatment_facility_name",
          "problem": "low-cardinality site column, possibly identifying"}],
    )
    return out[0]


def _path5_keep_verification_decision(tmp_path, monkeypatch) -> dict:
    """Path 5: `verify_keep_decisions` demotes a keep whose row values
    match a deterministic PHI detector."""
    source = tmp_path / "dataset.csv"
    source.write_text("ssn\n123-45-6789\n", encoding="utf-8")
    monkeypatch.setattr(
        "phi_core.agents.reasoning.detect_text",
        lambda *_a, **_k: [SimpleNamespace(hipaa_category="G")],
    )
    raw = {"file_id": "dataset.csv", "column": "ssn", "action": "keep",
           "confidence": 0.9, "reason": "looked fine on the header",
           "phi_category": "G"}
    out, _ = verify_keep_decisions([raw], {"dataset.csv": source})
    return out[0]


def test_reviewer_prompt_plain_english_path1_invalid_model_action():
    decision = _path1_invalid_action_decision()
    assert decision["action"] == "human_review"
    prompt = annotate_pending_review([decision])[0]["reviewer_prompt"]
    _assert_plain_english(prompt)
    assert _ACTION_PLAIN["drop"] in prompt
    assert _escalation_reason_phrase(decision) in prompt


def test_reviewer_prompt_plain_english_path2_confidence_floor():
    decision = _path2_confidence_floor_decision()
    assert decision["action"] == "human_review"
    prompt = annotate_pending_review([decision])[0]["reviewer_prompt"]
    _assert_plain_english(prompt)
    assert _ACTION_PLAIN["keep"] in prompt
    assert _CATEGORY_PLAIN["B"] in prompt
    assert _escalation_reason_phrase(decision) in prompt


def test_reviewer_prompt_plain_english_path3_blocking_floor():
    decision = _path3_blocking_floor_decision()
    assert decision["action"] == "human_review"
    prompt = annotate_pending_review([decision])[0]["reviewer_prompt"]
    _assert_plain_english(prompt)
    assert _ACTION_PLAIN["keep"] in prompt
    assert _escalation_reason_phrase(decision) in prompt


def test_reviewer_prompt_plain_english_path4_sentinel_escalation():
    decision = _path4_sentinel_escalation_decision()
    assert decision["action"] == "human_review"
    prompt = annotate_pending_review([decision])[0]["reviewer_prompt"]
    _assert_plain_english(prompt)
    assert _CATEGORY_PLAIN["R"] in prompt
    assert _escalation_reason_phrase(decision) in prompt


def test_reviewer_prompt_plain_english_path5_keep_verification(tmp_path, monkeypatch):
    decision = _path5_keep_verification_decision(tmp_path, monkeypatch)
    assert decision["action"] == "human_review"
    prompt = annotate_pending_review([decision])[0]["reviewer_prompt"]
    _assert_plain_english(prompt)
    assert _CATEGORY_PLAIN["G"] in prompt
    assert _escalation_reason_phrase(decision) in prompt


def test_escalation_reason_phrases_distinguish_all_five_routes(tmp_path, monkeypatch):
    """The five escalation paths must not collapse into one indistinguishable
    'not confident enough' sentence: each gets its own safe, plain-English
    why-phrase, built only from the trusted, code-controlled reason-prefix
    each path writes (never from suggested_reason, an agent name, a raw
    identifier, a confidence float, or dataset PHI)."""
    decisions = [
        _path1_invalid_action_decision(),
        _path2_confidence_floor_decision(),
        _path3_blocking_floor_decision(),
        _path4_sentinel_escalation_decision(),
        _path5_keep_verification_decision(tmp_path, monkeypatch),
    ]
    phrases = [_escalation_reason_phrase(d) for d in decisions]
    assert len(set(phrases)) == 5, f"escalation routes are not distinguishable: {phrases}"
    for decision, phrase in zip(decisions, phrases):
        prompt = annotate_pending_review([decision])[0]["reviewer_prompt"]
        _assert_plain_english(prompt)
        assert phrase in prompt


def test_reviewer_prompt_with_dictionary_description_stays_plain_english():
    """The dictionary-description clause is user data, not agent
    vocabulary, but the whole sentence must still pass the same check."""
    raw = {"file_id": "f1", "column": "ssn", "action": "keep",
           "confidence": 0.4, "reason": "looked safe", "phi_category": "G"}
    out, _ = apply_confidence_floor([raw])
    prompt = annotate_pending_review(
        out, dictionary_by_column={"ssn": "Social Security number, 9 digits"}
    )[0]["reviewer_prompt"]
    _assert_plain_english(prompt)
    assert "Social Security number, 9 digits" in prompt
