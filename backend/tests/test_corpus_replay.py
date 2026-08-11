"""Offline replay tests (workstream A hardening). No Mongo and no LLM --
these exercise the real deterministic pipeline layer (hard rules, executor
transforms, Presidio/rule scrubber, publish guard) directly.

Every assertion here was verified against the actual measured behaviour of
``phi_core.agents.reasoning._apply_action`` and ``apply_sentinel_hard_rules``
during implementation, not assumed from the plan narrative alone; a couple
of the plan's illustrative examples turned out not to reproduce exactly as
worded once tested against the real code (see inline notes), so the
assertions below track what genuinely happens.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest


def _replay_and_verify(scenario_id, *, row_count, seed, edge_case_tags=None, unmatched="oracle"):
    from phi_corpus.planters import plant
    from phi_corpus.replay import replay
    from phi_corpus.verify import verify

    art = plant(scenario_id, edge_case_tags=edge_case_tags or [], row_count=row_count, seed=seed)
    with tempfile.TemporaryDirectory() as td:
        rr = replay(art, Path(td), unmatched=unmatched)
        report = verify(art.ground_truth, rr.decisions, file_name_map=rr.file_name_map,
                         guard_report=rr.guard_report, export_paths=rr.export_paths)
    return art, rr, report


# ---- L0: the harness's own correctness proof ------------------------------


@pytest.mark.parametrize("scenario_id", ["oncology_v1", "hipaa_max_adversarial_v1"])
def test_l0_replayed_with_oracle_is_leak_clean_and_transform_conformant(scenario_id):
    _art, _rr, report = _replay_and_verify(scenario_id, row_count=12, seed=42, unmatched="oracle")
    assert report["leak"]["status"] == "clean"
    assert report["transform"]["nonconformant"] == 0


# ---- the harness detects, rather than passes, measured defects -----------


def test_naaccr_zip_non_us_edge_case_produces_a_transform_violation():
    """`zip3_truncate` on a foreign postcode fabricates a US ZIP3
    (`_apply_action` strips non-digits and left-pads with zeros, so
    'K1A 0B1' becomes '101' -- confirmed against the real code). The
    oracle expects an empty string for a non-US value, so this must show
    as a transform violation."""
    _art, _rr, report = _replay_and_verify(
        "l2_naaccr_registry_v1", row_count=20, seed=302,
        edge_case_tags=["zip_non_us"], unmatched="oracle",
    )
    violations = [v for v in report["transform"]["violations"]
                  if v["column"] == "Addr at DX--Postal Code"]
    assert violations, json.dumps(report["transform"]["violations"][:5], indent=2)


def test_cms_dob_two_digit_year_edge_case_produces_a_transform_violation():
    """`year_only`'s regex is `re.search(r"(\\d{4})", value)`; a 2-digit-year
    US-short date like '03/15/85' has no 4-consecutive-digit run, so the
    real executor returns an empty string and destroys the year -- a
    genuine, reproduced defect, not a coverage gap (the column's own
    header is irrelevant here; oracle mode supplies the correct action and
    the transform still fails on the value shape)."""
    _art, _rr, report = _replay_and_verify(
        "l2_cms_claims_v1", row_count=20, seed=303,
        edge_case_tags=["dob_two_digit_year"], unmatched="oracle",
    )
    violations = [v for v in report["transform"]["violations"] if v["column"] == "BENE_BIRTH_DT"]
    assert violations, json.dumps(report["transform"]["violations"][:5], indent=2)


def test_keeper_hijack_barcode_reaches_the_export_as_a_leak():
    """The hard-rule "clinical / stratifier" allow-list includes the
    literal `barcode` alongside genuinely clinical headers, so a
    header-driven pipeline force-`keep`s a column named "barcode" even
    though this scenario plants an MRN under it. Oracle mode does not
    correct this: `apply_sentinel_hard_rules` only leaves genuinely
    UNMATCHED columns for the oracle substitution to touch, and this one
    matches (wrongly) via the keeper pattern -- the leak is structural."""
    _art, _rr, report = _replay_and_verify(
        "l3_keeper_hijack_v1", row_count=10, seed=402, unmatched="oracle",
    )
    hits = [h for h in report["leak"]["hits"] if h["column"] == "barcode"]
    assert hits, json.dumps(report["leak"]["hits"][:5], indent=2)


def test_sdtm_studyid_hard_rule_false_positive_is_a_utility_loss():
    """`STUDYID` is a protocol number, not a patient identifier, and this
    scenario plants it with `expected_action="keep"`. The (H) hard rule's
    allow-list regex matches the literal `study_id` inside `studyid`
    (`study[_ ]?id`), forcing `pseudonymize` regardless of oracle mode
    (a MATCHED column's hard-rule action is never touched by the
    unmatched-only oracle substitution) -- a measured false positive."""
    _art, _rr, report = _replay_and_verify(
        "l1_sdtm_oncology_v1", row_count=12, seed=201, unmatched="oracle",
    )
    losses = [u for u in report["utility"]["losses"] if u["column"] == "STUDYID"]
    assert losses, json.dumps(report["utility"]["losses"][:5], indent=2)


# ---- masking --------------------------------------------------------------


def test_no_raw_leak_literal_of_length_6_or_more_appears_in_any_report():
    from phi_corpus.planters import plant
    from phi_corpus.tiers import ladder_for

    for entry in ladder_for("all"):
        art = plant(entry.scenario_id, edge_case_tags=list(entry.edge_case_tags),
                     row_count=entry.row_count, seed=entry.seed, tier=entry.tier)
        _art, _rr, report = _replay_and_verify(
            entry.scenario_id, row_count=entry.row_count, seed=entry.seed,
            edge_case_tags=list(entry.edge_case_tags), unmatched="oracle",
        )
        blob = json.dumps(report)
        for cell in art.ground_truth["planted"]:
            for lit in cell.get("leak_literals") or []:
                if len(lit) >= 6:
                    assert lit not in blob, (entry.scenario_id, lit)
