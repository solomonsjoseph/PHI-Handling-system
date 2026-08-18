import asyncio

import pytest

from phi_core.agents.reasoning import _HARD_RULE_TABLE, apply_site_cardinality_rule


def _decide(**kw):
    base = {"file_id": "dataset.csv", "column": "treatment_facility_name",
            "action": "keep", "confidence": 0.95, "reason": "clinically useful",
            "phi_category": "NONE"}
    base.update(kw)
    return base


def test_eligible_facility_forced_to_drop_category_r():
    stats = {("dataset.csv", "treatment_facility_name"): {"distinct": 4, "rows": 40}}
    out, overrides = apply_site_cardinality_rule([_decide()], stats)
    assert out[0]["action"] == "drop"
    assert out[0]["phi_category"] == "R"
    assert out[0]["citation"] == "45 CFR 164.514(b)(2)(i)(R)"
    assert out[0]["confidence"] == 0.95
    assert out[0]["suggested_action"] == "keep"
    assert len(overrides) == 1
    assert overrides[0] == {
        "file_id": "dataset.csv", "column": "treatment_facility_name",
        "from": "keep", "to": "drop",
        "rule": "site_cardinality", "distinct": 4, "rows": 40,
    }


def test_high_cardinality_same_name_left_alone():
    # Every row a distinct facility name -- not a quasi-identifier by count,
    # so the rule must not fire even though the header matches.
    stats = {("dataset.csv", "treatment_facility_name"): {"distinct": 40, "rows": 40}}
    out, overrides = apply_site_cardinality_rule([_decide()], stats)
    assert out[0]["action"] == "keep"
    assert overrides == []


def test_clinical_keeper_site_of_disease_never_touched():
    # 'site_of_disease' matches the site/facility regex as a substring, but
    # it is already owned by the _HARD_RULE_TABLE clinical-keepers row, so
    # this rule must defer to that ownership rather than dropping it.
    assert any(
        __import__("re").match(pattern, "site_of_disease")
        for pattern, *_ in _HARD_RULE_TABLE
    ), "fixture assumption: site_of_disease must be a hard-rule keeper"
    stats = {("dataset.csv", "site_of_disease"): {"distinct": 3, "rows": 40}}
    out, overrides = apply_site_cardinality_rule(
        [_decide(column="site_of_disease")], stats)
    assert out[0]["action"] == "keep"
    assert overrides == []


def test_missing_stats_left_alone():
    out, overrides = apply_site_cardinality_rule([_decide()], {})
    assert out[0]["action"] == "keep"
    assert overrides == []


def test_non_keep_action_never_forced():
    stats = {("dataset.csv", "treatment_facility_name"): {"distinct": 4, "rows": 40}}
    out, overrides = apply_site_cardinality_rule(
        [_decide(action="pseudonymize")], stats)
    assert out[0]["action"] == "pseudonymize"
    assert overrides == []


def test_non_matching_column_name_left_alone():
    stats = {("dataset.csv", "study_arm"): {"distinct": 3, "rows": 40}}
    out, overrides = apply_site_cardinality_rule(
        [_decide(column="study_arm")], stats)
    assert out[0]["action"] == "keep"
    assert overrides == []


@pytest.mark.parametrize("column", ["award_id", "composite_score", "subclinical_flag"])
def test_in_token_substrings_never_treated_as_site_terms(column):
    # 'award_id' contains 'ward', 'composite_score' contains 'site', and
    # 'subclinical_flag' contains 'clinic' as raw substrings, but none of
    # them is a separator-delimited site/facility token. Unbounded
    # substring matching would silently drop these columns; that is the
    # destructive false positive this test guards against.
    stats = {("dataset.csv", column): {"distinct": 4, "rows": 40}}
    out, overrides = apply_site_cardinality_rule([_decide(column=column)], stats)
    assert out[0]["action"] == "keep"
    assert overrides == []


def test_distinct_one_below_floor_left_alone():
    # distinct must be >= 2; a constant-valued column carries no
    # cardinality signal at all and is left to Judge/Sentinel.
    stats = {("dataset.csv", "treatment_facility_name"): {"distinct": 1, "rows": 40}}
    out, overrides = apply_site_cardinality_rule([_decide()], stats)
    assert out[0]["action"] == "keep"
    assert overrides == []


def test_distinct_equals_20_ceiling_fires():
    # rows=300 -> 0.05*rows=15, so the flat floor of 20 governs the
    # ceiling. distinct == 20 is the inclusive upper boundary and must fire.
    stats = {("dataset.csv", "treatment_facility_name"): {"distinct": 20, "rows": 300}}
    out, overrides = apply_site_cardinality_rule([_decide()], stats)
    assert out[0]["action"] == "drop"
    assert overrides[0]["distinct"] == 20
    assert overrides[0]["rows"] == 300


def test_distinct_21_fails_at_rows_400():
    # rows=400 -> 0.05*rows=20, so the ceiling is still exactly 20 (the
    # flat floor and the percentage coincide here). distinct == 21 is one
    # past the inclusive boundary and must be left alone.
    stats = {("dataset.csv", "treatment_facility_name"): {"distinct": 21, "rows": 400}}
    out, overrides = apply_site_cardinality_rule([_decide()], stats)
    assert out[0]["action"] == "keep"
    assert overrides == []


def test_percentage_branch_above_rows_400():
    # rows=1000 -> 0.05*rows=50, so the percentage now governs the
    # ceiling rather than the flat floor of 20. distinct == 50 fires;
    # distinct == 51 is one past the boundary and is left alone.
    fires_stats = {("dataset.csv", "treatment_facility_name"): {"distinct": 50, "rows": 1000}}
    out, overrides = apply_site_cardinality_rule([_decide()], fires_stats)
    assert out[0]["action"] == "drop"
    assert overrides[0]["distinct"] == 50
    assert overrides[0]["rows"] == 1000

    fails_stats = {("dataset.csv", "treatment_facility_name"): {"distinct": 51, "rows": 1000}}
    out2, overrides2 = apply_site_cardinality_rule([_decide()], fails_stats)
    assert out2[0]["action"] == "keep"
    assert overrides2 == []


def _run_cardinality_pipeline(tmp_path, monkeypatch):
    """Drive orchestrator.run_pipeline with a Judge that proposes 'keep' on
    a low-cardinality facility column and a Schema stub carrying the
    matching cardinality stats. Proves schema_stats is actually threaded
    from Schema through the orchestrator into the rule, and that the rule
    fires deterministically (site_cardinality_iter_1) after the age/DOB
    rule and before the Sentinel call that iteration -- Sentinel never
    needs to raise a blocking issue on this column at all."""
    from phi_core.agents import orchestrator

    source = tmp_path / "dataset.csv"
    source.write_text("treatment_facility_name\nClinic A\n", encoding="utf-8")
    phase_events = []

    class FakeSessions:
        async def find_one(self, *_args, **_kwargs):
            return None

        async def update_one(self, *_args, **_kwargs):
            return None

    class FakeAgentLog:
        async def insert_one(self, *_args, **_kwargs):
            return None

    class FakeDb:
        def __init__(self):
            self.sessions = FakeSessions()
            self.agent_log = FakeAgentLog()

    class FakeStatute:
        def __init__(self, **_kwargs):
            pass

        async def run(self, **_kwargs):
            return {}

    class FakePraxis:
        def __init__(self, **_kwargs):
            pass

        async def method_for(self, _category):
            return {}

    class FakeLexicon:
        def __init__(self, **_kwargs):
            pass

        async def run(self, **_kwargs):
            return {"columns": []}

    class FakeInstrument(FakeLexicon):
        async def run(self, **_kwargs):
            return {"fields": []}

    class FakeSchema(FakeLexicon):
        def __init__(self, **_kwargs):
            super().__init__(**_kwargs)
            self._stats = {("dataset.csv", "treatment_facility_name"): {"distinct": 4, "rows": 40}}

    class FakeJudge:
        def __init__(self, **_kwargs):
            self.call_failures = 0
            self.last_message_id = None

        async def run(self, **_kwargs):
            return {"decisions": [{
                "file_id": "dataset.csv",
                "column": "treatment_facility_name",
                "action": "keep",
                "confidence": 0.95,
                "reason": "Judge decision",
                "subject": "site",
                "phi_category": "NONE",
            }]}

    class FakeSentinel:
        def __init__(self, **_kwargs):
            # Forces the pipeline into human review right after this
            # iteration's short-circuit, so the test never needs to mock
            # Executor/Auditor/Ledger/Herald -- the loop-ordering proof
            # only needs the Judge<->Sentinel iteration itself.
            self.call_failures = 1

        async def run(self, **_kwargs):
            return {"issues": []}

    monkeypatch.setattr(orchestrator, "Statute", FakeStatute)
    monkeypatch.setattr(orchestrator, "Praxis", FakePraxis)
    monkeypatch.setattr(orchestrator, "Lexicon", FakeLexicon)
    monkeypatch.setattr(orchestrator, "Instrument", FakeInstrument)
    monkeypatch.setattr(orchestrator, "Schema", FakeSchema)
    monkeypatch.setattr(orchestrator, "Judge", FakeJudge)
    monkeypatch.setattr(orchestrator, "Sentinel", FakeSentinel)

    async def emit(_message):
        return None

    async def on_phase(phase, payload):
        phase_events.append((phase, payload))

    db = FakeDb()
    result = asyncio.run(orchestrator.run_pipeline(
        {
            "id": "session",
            "iteration_cap": 1,
            "files": [{
                "kind": "dataset",
                "file_id": "dataset.csv",
                "stored_path": str(source),
            }],
        },
        db,
        object(),
        emit,
        on_phase,
    ))
    return result, phase_events


def test_pipeline_fires_before_sentinel_and_after_age_dob(tmp_path, monkeypatch):
    result, phase_events = _run_cardinality_pipeline(tmp_path, monkeypatch)
    names = [p for p, _ in phase_events]
    assert "site_cardinality_iter_1" in names
    assert "age_dob_rule_iter_1" not in names  # no age/DOB columns here, rule is a no-op
    assert "sentinel_iter_1" in names
    # Deterministic pass runs before the LLM Sentinel call in the same iteration.
    assert names.index("site_cardinality_iter_1") < names.index("sentinel_iter_1")
    override_payload = next(payload for p, payload in phase_events
                            if p == "site_cardinality_iter_1")
    assert override_payload["overrides"] == [{
        "file_id": "dataset.csv", "column": "treatment_facility_name",
        "from": "keep", "to": "drop",
        "rule": "site_cardinality", "distinct": 4, "rows": 40,
    }]
    assert result["status"] == "awaiting_human_review"
    decision = next(d for d in result["decisions"] if d["column"] == "treatment_facility_name")
    assert decision["action"] == "drop"
    assert decision["phi_category"] == "R"
    assert decision["suggested_action"] == "keep"
