"""Human review invariant tests (GOAL clause 61-65).

Every human decision must carry reviewer id + comment + timestamp. The
endpoint refuses when reviewer is missing.
"""
import asyncio
import os
import re
import uuid
from types import SimpleNamespace

import pytest
import requests
from phi_core.agents.reasoning import (
    _ACTION_PLAIN,
    _CATEGORY_PLAIN,
    ACTION_TYPES,
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
def test_human_review_ignores_client_supplied_reviewer_field():
    """GOAL invariant, current shape: every human decision carries a
    reviewer identity, but that identity is the *authenticated principal*
    from `resolve_principal`, never the client-supplied `reviewer` field
    on the request body -- `HumanReviewSubmit.reviewer` is documented in
    server.py as "unused; identity is the authenticated principal", a
    deliberate hardening so a caller cannot spoof someone else's name as
    the reviewer of record.

    Replaces `test_human_review_requires_reviewer`, which expected a 400
    when `body.reviewer` was empty. That expectation is stale: no code
    path in `server.py` has validated `body.reviewer` since identity moved
    to the authenticated principal, so it could never actually 400 for
    that reason (the previous version of this test only ever exercised the
    *session-not-found* 404 path and happened to also match its `assert
    "reviewer" in r.text.lower()` check by coincidence against unrelated
    404 text, masking that the reviewer-emptiness check itself was dead
    code). This test instead proves an empty/garbage `body.reviewer` has
    no effect on request handling: the request fails for the SAME reason
    (session not found) as it would with any other value, since the field
    is never inspected before that lookup runs.
    """
    fake_sid = uuid.uuid4().hex
    r_empty = requests.post(
        f"{BASE_URL}/api/sessions/{fake_sid}/human-review",
        json={"resolutions": [], "reviewer": "", "comment": "no reviewer"},
        timeout=10,
    )
    r_spoofed = requests.post(
        f"{BASE_URL}/api/sessions/{fake_sid}/human-review",
        json={"resolutions": [], "reviewer": "someone.else@lab.edu", "comment": "no reviewer"},
        timeout=10,
    )
    # Both fail identically (session not found) -- the reviewer field value
    # never changes the outcome, proving it is genuinely inert.
    assert r_empty.status_code == r_spoofed.status_code == 404, (r_empty.text, r_spoofed.text)


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


def test_human_review_captures_session_review_offline(monkeypatch):
    """An in-memory awaiting-review session records reviewer audit fields."""
    import server

    session = {
        "id": "session-1",
        "owner": "reviewer-1",
        "status": "awaiting_human_review",
        "agent_decisions": [{"file_id": "file-1", "column": "review_target", "action": "human_review"}],
        # `run_decision_gates`'s `assert_exact_coverage` requires every
        # decision's (file_id, column) to name a real column of a known
        # file; the fixture must reflect that, not just the decision list.
        "files": [{"file_id": "file-1", "kind": "dataset", "columns": ["review_target"]}],
    }

    class _FakeCollection:
        async def insert_one(self, document):
            return SimpleNamespace(inserted_id="fake-id")

    class FakeDb:
        """Minimal Motor-shaped double: `.sessions` for the session lookup
        `session_human_review` reads/writes directly, `[...]` subscript for
        every other collection it opens through `MongoControlStore`
        (`gate_results`), matching real `AsyncIOMotorDatabase` semantics."""

        def __init__(self) -> None:
            self.sessions = MemorySessions()

        def __getitem__(self, _name: str) -> "_FakeCollection":
            return _FakeCollection()

    class MemorySessions:
        async def find_one(self, query, projection=None):
            if query == {"id": session["id"], "owner": session["owner"]}:
                return session
            return None

        async def update_one(self, query, update):
            assert query["id"] == session["id"]
            assert query["owner"] == session["owner"]
            assert query["status"] == {"$in": ["awaiting_human_review", "partially_complete"]}
            session.update(update["$set"])
            return SimpleNamespace(matched_count=1)

    monkeypatch.setattr(server, "get_db", lambda: FakeDb())
    result = asyncio.run(
        server.session_human_review(
            session["id"],
            server.HumanReviewSubmit(
                resolutions=[{"file_id": "file-1", "column": "review_target", "mode": "defer"}],
                comment="Needs more review.",
                actual_knowledge_ack=True,
            ),
            session["owner"],
        )
    )

    assert result == {"status": "still_awaiting", "unresolved": 1}
    review = session["session_review"][-1]
    assert review["reviewer"] == session["owner"]
    assert review["comment"] == "Needs more review."
    assert review["reviewed_at"]
    assert review["deferred_columns"] == [{"file_id": "file-1", "column": "review_target"}]


# --- Task 23: reviewer prompts speak plain English on every escalation
# path -----------------------------------------------------------------
#
# Pure Python, no live backend: these exercise the six deterministic
# routes that can set `action == "human_review"` --
# `validate_decisions` (invalid model output), `apply_confidence_floor`,
# `apply_blocking_floor`, `apply_sentinel_escalations`,
# `verify_keep_decisions`, and orchestrator.py's anti-loop forcing
# block -- and check the `reviewer_prompt` `annotate_pending_review`
# attaches to each never leaks a bare HIPAA letter, action identifier,
# confidence number, or agent name. A seventh case below covers
# `verify_keep_decisions`' unreadable-file fallback specifically, since
# it shares "Keep verification" wording with path 5 but must read as a
# distinct, accurate explanation rather than a false detector match.

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


def _path6_anti_loop_decision() -> dict:
    """Path 6: orchestrator.py's anti-loop forcing block converts a
    revision that repeats a previously-rejected action straight to
    human_review, without resubmitting it to Sentinel. That block lives
    in orchestrator.py (it needs the running iteration's
    `prior_blocking_actions` state), not reasoning.py, so this builds
    the decision with the exact `Anti-loop:` reason prefix the block
    writes, to exercise the shared classifier and reviewer-prompt
    template it feeds into the same way the other five paths do."""
    forced = {"file_id": "f1", "column": "study_id", "action": "human_review",
              "confidence": 0.7, "phi_category": "H",
              "reason": (
                  "Anti-loop: Judge repeated the previously-rejected action 'keep' "
                  "without a substantive revision; forced to human review rather "
                  "than resubmitting to Sentinel for the same rejection."
              ),
              "suggested_action": "keep", "suggested_confidence": 0.7,
              "suggested_reason": (
                  "Judge repeated the previously-rejected action 'keep' without "
                  "change. Sentinel's objection: may be a hashed direct identifier"
              )}
    return forced


def _path7_keep_verification_unreadable_decision(tmp_path) -> dict:
    """Path 7: `verify_keep_decisions`' fail-closed fallback when the
    dataset file itself can't be read (missing, corrupt, unsupported).
    Still demotes to human_review, but no detector ever ran, so the
    reason must say verification could not run rather than implying a
    match was found -- distinct wording from path 5's real match."""
    missing = tmp_path / "does-not-exist.csv"
    raw = {"file_id": "dataset.csv", "column": "study_id", "action": "keep",
           "confidence": 0.9, "reason": "looked fine on the header",
           "phi_category": "H"}
    out, _ = verify_keep_decisions([raw], {"dataset.csv": missing})
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


def test_reviewer_prompt_plain_english_path6_anti_loop():
    decision = _path6_anti_loop_decision()
    assert decision["action"] == "human_review"
    prompt = annotate_pending_review([decision])[0]["reviewer_prompt"]
    _assert_plain_english(prompt)
    assert _ACTION_PLAIN["keep"] in prompt
    assert _escalation_reason_phrase(decision) in prompt


def test_reviewer_prompt_plain_english_path7_keep_verification_unreadable(tmp_path):
    decision = _path7_keep_verification_unreadable_decision(tmp_path)
    assert decision["action"] == "human_review"
    prompt = annotate_pending_review([decision])[0]["reviewer_prompt"]
    _assert_plain_english(prompt)
    assert _escalation_reason_phrase(decision) in prompt


def test_unreadable_keep_verification_reason_is_accurate_and_distinct(tmp_path, monkeypatch):
    """Direct regression: the unreadable-file fallback must never claim a
    detector matched a row value -- nothing was ever read -- and must
    stay distinguishable from path 5's genuine detector-match wording."""
    unreadable = _path7_keep_verification_unreadable_decision(tmp_path)
    assert unreadable["reason"].startswith("Keep verification (unreadable):")
    assert "could not be read" in unreadable["reason"]
    assert "matched" not in unreadable["reason"]

    matched = _path5_keep_verification_decision(tmp_path, monkeypatch)
    assert matched["reason"].startswith("Keep verification:")
    assert not matched["reason"].startswith("Keep verification (unreadable):")

    unreadable_phrase = _escalation_reason_phrase(unreadable)
    matched_phrase = _escalation_reason_phrase(matched)
    assert unreadable_phrase != matched_phrase
    assert "read" in unreadable_phrase
    assert "found something" not in unreadable_phrase


def test_escalation_reason_phrases_distinguish_all_six_routes(tmp_path, monkeypatch):
    """The six escalation paths must not collapse into one indistinguishable
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
        _path6_anti_loop_decision(),
    ]
    phrases = [_escalation_reason_phrase(d) for d in decisions]
    assert len(set(phrases)) == 6, f"escalation routes are not distinguishable: {phrases}"
    for decision, phrase in zip(decisions, phrases, strict=True):
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
