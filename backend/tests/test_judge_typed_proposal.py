"""Plan step 1: Judge.run returns a typed proposal (JudgeDecision/JudgeProposal),
not a bare untrusted dict. validate_decisions (D11's first gate) still owns
vocabulary correctness (action/subject/phi_category); this boundary only
catches a wrong *shape* one level up, before anything downstream sees it.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from phi_core.agents.reasoning import Judge
from phi_core.control.testing import make_ctx


def _judge() -> Judge:
    return Judge(make_ctx("Judge"))


@pytest.mark.asyncio
async def test_well_formed_decisions_pass_through_the_typed_boundary_unchanged():
    judge = _judge()
    judge.call_json = AsyncMock(return_value={"decisions": [
        {"file_id": "f1", "column": "mrn", "phi_category": "H", "subject": "participant",
         "action": "pseudonymize", "reason": "direct identifier", "confidence": 0.95,
         "citation": "164.514(b)(2)(i)(H)"},
    ]})
    result = await judge.run(schema={"columns": [{"name": "mrn"}]}, instrument={}, lexicon={}, statute={})
    assert result == {"decisions": [
        {"file_id": "f1", "column": "mrn", "phi_category": "H", "subject": "participant",
         "action": "pseudonymize", "reason": "direct identifier", "confidence": 0.95,
         "citation": "164.514(b)(2)(i)(H)"},
    ]}


@pytest.mark.asyncio
async def test_extra_fields_like_justification_survive_the_typed_boundary():
    """A Sentinel-feedback correction round asks Judge to add a
    `justification` field; JudgeDecision's extra='allow' must not drop it."""
    judge = _judge()
    judge.call_json = AsyncMock(return_value={"decisions": [
        {"file_id": "f1", "column": "mrn", "action": "pseudonymize",
         "justification": "already reviewed against Statute, not a false positive"},
    ]})
    result = await judge.run(schema={"columns": [{"name": "mrn"}]}, instrument={}, lexicon={}, statute={})
    assert result["decisions"][0]["justification"] == "already reviewed against Statute, not a false positive"


@pytest.mark.asyncio
async def test_malformed_entry_with_a_real_file_id_and_column_fails_closed_to_human_review():
    """A shape failure (confidence is a non-numeric string here) that still
    names a real (file_id, column) must not be dropped silently -- it
    becomes an explicit human_review entry, same fail-closed shape as
    every other boundary check in this pipeline."""
    judge = _judge()
    judge.call_json = AsyncMock(return_value={"decisions": [
        {"file_id": "f1", "column": "mrn", "action": "pseudonymize", "confidence": "not-a-number"},
    ]})
    result = await judge.run(schema={"columns": [{"name": "mrn"}]}, instrument={}, lexicon={}, statute={})
    assert result == {"decisions": [
        {"file_id": "f1", "column": "mrn", "phi_category": None, "subject": "",
         "action": "human_review", "reason": "judge_output_malformed", "confidence": 0.0, "citation": ""},
    ]}


@pytest.mark.asyncio
async def test_entry_with_no_salvageable_file_id_or_column_is_dropped():
    """Nothing safe can be constructed without a real (file_id, column) --
    the entry is dropped, and assert_exact_coverage (downstream, in
    control/gates.py) is what catches the resulting coverage gap, exactly
    like it catches any other missing decision."""
    judge = _judge()
    judge.call_json = AsyncMock(return_value={"decisions": [
        {"action": "pseudonymize", "confidence": "not-a-number"},  # no file_id/column at all
        {"file_id": "f1", "column": "notes", "action": "scrub_text", "confidence": 0.8},
    ]})
    result = await judge.run(schema={"columns": [{"name": "notes"}]}, instrument={}, lexicon={}, statute={})
    assert result == {"decisions": [
        {"file_id": "f1", "column": "notes", "phi_category": None, "subject": "",
         "action": "scrub_text", "reason": "", "confidence": 0.8, "citation": ""},
    ]}


@pytest.mark.asyncio
async def test_non_dict_top_level_reply_produces_an_empty_proposal_not_a_crash():
    judge = _judge()
    judge.call_json = AsyncMock(return_value=["not", "a", "dict"])
    result = await judge.run(schema={"columns": []}, instrument={}, lexicon={}, statute={})
    assert result == {"decisions": []}
