from phi_core.agents.reasoning import CONFIDENCE_FLOOR, apply_confidence_floor


def _decide(**kw):
    base = {"file_id": "f1", "column": "col", "action": "keep", "confidence": 0.9,
            "reason": "clinically useful", "phi_category": "NONE"}
    base.update(kw)
    return base


def test_below_floor_forced_to_human_review_with_suggested_fields():
    out, overrides = apply_confidence_floor([_decide(confidence=0.55)])
    assert out[0]["action"] == "human_review"
    assert out[0]["suggested_action"] == "keep"
    assert out[0]["suggested_confidence"] == 0.55
    assert "0.55" in out[0]["suggested_reason"]
    assert len(overrides) == 1
    assert overrides[0]["rule"] == "confidence_floor"


def test_at_floor_not_forced():
    out, overrides = apply_confidence_floor([_decide(confidence=CONFIDENCE_FLOOR)])
    assert out[0]["action"] == "keep"
    assert overrides == []


def test_already_human_review_not_double_touched():
    out, overrides = apply_confidence_floor([_decide(action="human_review", confidence=0.2)])
    assert out[0]["action"] == "human_review"
    assert overrides == []
