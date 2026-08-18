"""Regression coverage for decisions two deterministic mechanisms both want
to route to human_review. Idempotence was previously verified only by code
inspection (Sentinel plan item 5): applying a second mechanism to a decision
a first mechanism already settled must never corrupt the record, never
re-fire on a decision it no longer applies to, and must carry forward the
most specific explanation available rather than a stale one."""

from phi_core.agents.reasoning import (
    BLOCKING_ISSUE_FLOOR,
    apply_blocking_floor,
    apply_confidence_floor,
    apply_site_cardinality_rule,
    verify_keep_decisions,
)


def _decide(**kw):
    base = {"file_id": "dataset.csv", "column": "barcode", "action": "keep",
            "confidence": 0.5, "reason": "Judge decision", "phi_category": "NONE"}
    base.update(kw)
    return base


def test_confidence_floor_and_keep_verification_agree_on_human_review(tmp_path):
    """A 'keep' at confidence 0.5 whose row values also match a deterministic
    detector triggers both the confidence floor and verify_keep_decisions.
    Both mechanisms run in the exact order orchestrator.py uses: the
    confidence floor once per Judge/Sentinel iteration (reasoning.py inside
    run_pipeline's loop), keep verification once afterward, on the settled
    decision list (orchestrator.py's post-loop `verify_keep_decisions` call).
    Final action must be human_review either way -- the two mechanisms must
    never disagree on the terminal state -- and the surviving suggested_reason
    must be the more specific, detector-grounded keep-verification text
    rather than the generic confidence-floor text, per the Sentinel plan's
    ordering requirement."""
    dataset = tmp_path / "dataset.csv"
    dataset.write_text("barcode\n" + "MRN-" + "1" * 8 + "\n", encoding="utf-8")

    floored, floor_overrides = apply_confidence_floor([_decide()])
    assert floored[0]["action"] == "human_review"
    assert len(floor_overrides) == 1
    assert floor_overrides[0]["rule"] == "confidence_floor"

    verified, demotions = verify_keep_decisions(floored, {"dataset.csv": dataset})

    assert verified[0]["action"] == "human_review"
    assert len(demotions) == 1
    assert demotions[0]["detector"] == "H"
    assert verified[0]["suggested_action"] == "keep"
    assert "detector 'H'" in verified[0]["suggested_reason"]
    assert "Keep verification" in verified[0]["reason"]


def test_site_cardinality_and_blocking_floor_compose_idempotently(tmp_path):
    """A low-cardinality facility column triggers the site-cardinality rule
    (keep -> drop) and, once Sentinel has raised BLOCKING_ISSUE_FLOOR
    blocking issues against that same forced 'drop', the blocking floor
    (drop -> human_review). The two must compose without corrupting each
    other's record: the blocking floor's suggested_action must reflect the
    cardinality rule's 'drop', not the stale original 'keep', and
    re-applying both passes to the already-settled decision must be a
    true no-op. Finally, verify_keep_decisions -- which orchestrator.py
    also runs over the settled list, after this loop, on every run --
    must leave a suggested_action=='drop' decision alone even when its
    row value would match a detector, because its post-repair rescan
    (Task 24 case 1) is scoped to decisions that began as 'keep', not
    'drop'. Rescanning a forced drop would be wrong: 'drop' already
    removes the value, so there is nothing for a human to confirm about
    a row-level match."""
    stats = {("dataset.csv", "treatment_facility_name"): {"distinct": 4, "rows": 40}}
    decisions = [_decide(column="treatment_facility_name", confidence=0.95)]

    dropped, cardinality_overrides = apply_site_cardinality_rule(decisions, stats)
    assert dropped[0]["action"] == "drop"
    assert dropped[0]["phi_category"] == "R"
    assert len(cardinality_overrides) == 1
    assert cardinality_overrides[0]["rule"] == "site_cardinality"

    attempts = {("dataset.csv", "treatment_facility_name"): BLOCKING_ISSUE_FLOOR}
    settled, blocking_overrides = apply_blocking_floor(dropped, attempts)
    assert settled[0]["action"] == "human_review"
    assert settled[0]["suggested_action"] == "drop"
    assert len(blocking_overrides) == 1
    assert blocking_overrides[0]["rule"] == "blocking_issue_floor"
    assert blocking_overrides[0]["from"] == "drop"

    # Idempotence: re-running both passes over the settled decision must
    # not touch it again -- site-cardinality only fires on 'keep', and the
    # blocking floor only fires on a decision that isn't already
    # human_review.
    replayed, cardinality_overrides_2 = apply_site_cardinality_rule(settled, stats)
    assert replayed[0]["action"] == "human_review"
    assert cardinality_overrides_2 == []
    replayed_again, blocking_overrides_2 = apply_blocking_floor(replayed, attempts)
    assert replayed_again[0]["action"] == "human_review"
    assert blocking_overrides_2 == []

    # verify_keep_decisions runs over this same settled list on every real
    # pipeline run (orchestrator.py, post-loop). The dataset value below
    # would match detector 'H' if scanned -- proving a missing demotion
    # here is because suggested_action=='drop' is correctly excluded, not
    # because the value happens not to match anything.
    dataset = tmp_path / "dataset.csv"
    dataset.write_text("treatment_facility_name\n" + "MRN-" + "1" * 8 + "\n", encoding="utf-8")
    final, demotions = verify_keep_decisions(replayed_again, {"dataset.csv": dataset})
    assert final == replayed_again
    assert demotions == []
