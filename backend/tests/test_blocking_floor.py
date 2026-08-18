from phi_core.agents.reasoning import BLOCKING_ISSUE_FLOOR, apply_blocking_floor


def _decide(**kw):
    base = {"file_id": "f1", "column": "col", "action": "keep", "confidence": 0.9,
            "reason": "clinically useful", "phi_category": "NONE"}
    base.update(kw)
    return base


def test_at_floor_forced_to_human_review_with_suggested_fields():
    attempts = {("f1", "col"): BLOCKING_ISSUE_FLOOR}
    out, overrides = apply_blocking_floor([_decide()], attempts)
    assert out[0]["action"] == "human_review"
    assert out[0]["suggested_action"] == "keep"
    assert out[0]["suggested_confidence"] == 0.9
    assert "3" in out[0]["suggested_reason"]
    assert len(overrides) == 1
    override = overrides[0]
    assert override == {
        "file_id": "f1", "column": "col",
        "from": "keep", "to": "human_review",
        "rule": "blocking_issue_floor", "attempts": BLOCKING_ISSUE_FLOOR,
    }


def test_below_floor_not_forced():
    attempts = {("f1", "col"): BLOCKING_ISSUE_FLOOR - 1}
    out, overrides = apply_blocking_floor([_decide()], attempts)
    assert out[0]["action"] == "keep"
    assert overrides == []


def test_already_human_review_not_double_touched():
    attempts = {("f1", "col"): BLOCKING_ISSUE_FLOOR}
    out, overrides = apply_blocking_floor(
        [_decide(action="human_review", suggested_action="keep")], attempts)
    assert out[0]["action"] == "human_review"
    assert out[0]["suggested_action"] == "keep"
    assert overrides == []


def test_missing_key_defaults_to_zero_attempts():
    out, overrides = apply_blocking_floor([_decide()], {})
    assert out[0]["action"] == "keep"
    assert overrides == []
