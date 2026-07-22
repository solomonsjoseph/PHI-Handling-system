from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from phi_engine.config import config
from phi_engine.pipeline.dependencies import (
    DependencyDecision,
    DependencyDecisionBasis,
    DependencyKind,
    DependencyLevel,
    DependencyReasonCode,
    DependencyRecommendation,
    OrganizedDataset,
    OrganizedHeader,
    ParsedSupportArtifact,
    Sensitivity,
    SupportParseStatus,
    recommendation_identity,
)
from phi_engine.security.phi_review import Action


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
DATASET_ID = "a_" + "1" * 32
SUPPORT_ID = "a_" + "2" * 32
HEADER_ID = "h_" + "3" * 24
MODEL_SPEC = "qwen3:8b@sha256:" + "d" * 64
RECOMMENDATION_ID = recommendation_identity(
    dataset_artifact_id=DATASET_ID,
    support_artifact_id=SUPPORT_ID,
    kind=DependencyKind.MAPPING,
    reason_code=DependencyReasonCode.EXACT_HEADER_MATCH,
    header_ids=(HEADER_ID,),
    transform_requirement_ids=(),
)


@pytest.fixture
def routing():
    import phi_engine.security.model_routing as module

    return module


def _candidate(routing):
    return routing.CandidateRuleView(
        rule_id="usa_rule", action=Action.DROP, citation="45 CFR 164.514", jurisdictions=("USA",)
    )


def _header_task(routing, *, samples=("Alice", "Bob"), evidence=None):
    return routing.ConfidentialHeaderTask(
        dataset_artifact_id=DATASET_ID,
        dataset_sha256=SHA_A,
        header_id=HEADER_ID,
        raw_header="participant_name",
        samples=samples,
        candidate_rules=(_candidate(routing),),
        evidence=evidence or routing.ResolutionEvidence(profile_input_sha256=SHA_C),
    )


def _row(routing, *, support_id=SUPPORT_ID, support_sha=SHA_B, cells=None):
    if cells is None:
        cells=(routing.MatchedSupportCell(0, "participant_name"), routing.MatchedSupportCell(1, "Direct identifier"))
    return routing.MatchedSupportRow(
        support_artifact_id=support_id,
        support_sha256=support_sha,
        sheet_index=0,
        table_index=0,
        row_index=0,
        matched_column_indices=(0,),
        cells=cells,
    )


def _support_task(routing, *, sensitivity=Sensitivity.CONFIDENTIAL, rows=None):
    if rows is None:
        rows=(_row(routing),)
    return routing.SupportSignalTask(
        dataset_artifact_id=DATASET_ID,
        dataset_sha256=SHA_A,
        header_ids=(HEADER_ID,),
        support_artifact_id=SUPPORT_ID,
        support_sha256=SHA_B,
        normalized_support_sha256=SHA_C,
        sensitivity=sensitivity,
        dependency_decision_id="dd_" + "4" * 32,
        matched_rows=rows,
        candidate_rules=(_candidate(routing),),
    )


def _approved_decision(
    *,
    recommendation_id=RECOMMENDATION_ID,
    dataset_sha=SHA_A,
    support_sha=SHA_B,
    normalized_sha=SHA_C,
    kind=DependencyKind.MAPPING,
    level=DependencyLevel.HELPFUL,
    sensitivity=Sensitivity.NON_CONFIDENTIAL,
    reason_code=DependencyReasonCode.EXACT_HEADER_MATCH,
    basis=None,
):
    return DependencyDecision(
        schema_version="dependency-decision/v1",
        decision_id="dd_" + "4" * 32,
        recommendation_id=recommendation_id,
        dataset_artifact_id=DATASET_ID,
        dataset_sha256=dataset_sha,
        support_artifact_id=SUPPORT_ID,
        support_sha256=support_sha,
        normalized_support_sha256=normalized_sha,
        kind=kind,
        level=level,
        sensitivity=sensitivity,
        reason_code=reason_code,
        basis=basis or DependencyDecisionBasis(
            rulebook_sha256=SHA_A,
            scrub_config_sha256=SHA_B,
            support_role_sha256=SHA_C,
        ),
        decided_by="reviewer",
        decided_at="2026-07-14T00:00:00Z",
    )


def _verified_phase3_context(routing, tmp_path: Path):
    normalized_path = tmp_path / "normalized-support.jsonl"
    normalized_rows = [
        {
            "support_artifact_id": SUPPORT_ID,
            "source_sha256": SHA_B,
            "sheet_index": 0,
            "table_index": 0,
            "row_index": 0,
            "cells": [
                {"column_index": 0, "value": "participant_name"},
                {"column_index": 1, "value": "Direct identifier"},
            ],
        },
        {
            "support_artifact_id": SUPPORT_ID,
            "source_sha256": SHA_B,
            "sheet_index": 0,
            "table_index": 0,
            "row_index": 1,
            "cells": [{"column_index": 0, "value": "unrelated"}],
        },
    ]
    raw = "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
        for row in normalized_rows
    ).encode("utf-8")
    normalized_path.write_bytes(raw)
    normalized_sha = hashlib.sha256(raw).hexdigest()
    dataset = OrganizedDataset(
        artifact_id=DATASET_ID,
        source_sha256=SHA_A,
        normalized_rows_path=tmp_path / "original-workbook-must-not-open.xlsx",
        normalized_rows_sha256="e" * 64,
        headers=(
            OrganizedHeader(
                header_id=HEADER_ID,
                column_index=0,
                raw_name="participant_name",
                normalized_name="participant_name",
            ),
        ),
    )
    support = ParsedSupportArtifact(
        artifact_id=SUPPORT_ID,
        source_sha256=SHA_B,
        kind=DependencyKind.MAPPING,
        format="xlsx",
        parse_status=SupportParseStatus.PARSED,
        normalized_rows_path=normalized_path,
        normalized_rows_sha256=normalized_sha,
        failure_code=None,
    )
    recommendation = DependencyRecommendation(
        schema_version="dependency-recommendation/v1",
        recommendation_id=RECOMMENDATION_ID,
        dataset_artifact_id=DATASET_ID,
        dataset_sha256=SHA_A,
        support_artifact_id=SUPPORT_ID,
        support_sha256=SHA_B,
        normalized_support_sha256=normalized_sha,
        kind=DependencyKind.MAPPING,
        suggested_level=DependencyLevel.HELPFUL,
        default_sensitivity=Sensitivity.NON_CONFIDENTIAL,
        reason_code=DependencyReasonCode.EXACT_HEADER_MATCH,
        header_ids=(HEADER_ID,),
        matched_rule_ids=(),
        transform_requirement_ids=(),
        basis=DependencyDecisionBasis(
            rulebook_sha256=SHA_A,
            scrub_config_sha256=SHA_B,
            support_role_sha256=SHA_C,
        ),
    )
    decision = _approved_decision(normalized_sha=normalized_sha)
    return dataset, support, recommendation, decision


def _with_normalized_raw(
    support: ParsedSupportArtifact,
    recommendation: DependencyRecommendation,
    decision: DependencyDecision,
    raw: bytes,
):
    support.normalized_rows_path.write_bytes(raw)
    normalized_sha = hashlib.sha256(raw).hexdigest()
    return (
        replace(support, normalized_rows_sha256=normalized_sha),
        replace(recommendation, normalized_support_sha256=normalized_sha),
        replace(decision, normalized_support_sha256=normalized_sha),
    )


def _normalized_row(row_index: int, values: list[str]) -> dict:
    return {
        "support_artifact_id": SUPPORT_ID,
        "source_sha256": SHA_B,
        "sheet_index": 0,
        "table_index": 0,
        "row_index": row_index,
        "cells": [
            {"column_index": column_index, "value": value}
            for column_index, value in enumerate(values)
        ],
    }


def _build_verified_support_task(routing, tmp_path: Path):
    dataset, support, recommendation, decision = _verified_phase3_context(
        routing, tmp_path
    )
    router = routing.ModelTaskRouter()
    task = router.build_support_signal_task(
        dataset=dataset,
        support=support,
        recommendation=recommendation,
        decision=decision,
        current_basis=recommendation.basis,
        candidate_rules=(_candidate(routing),),
    )
    return router, task, dataset, support, recommendation, decision


def _header_payload(**overrides):
    payload = {
        "dataset_artifact_id": DATASET_ID,
        "header_id": HEADER_ID,
        "inferred_variable_type": "identifier",
        "action": "drop",
        "matched_rule_id": "usa_rule",
        "rule_citation": "45 CFR 164.514",
        "jurisdictions": ["USA"],
        "confidence": 0.99,
    }
    payload.update(overrides)
    return payload


def _signal_payload(**overrides):
    payload = {
        "dataset_artifact_id": DATASET_ID,
        "header_id": HEADER_ID,
        "support_artifact_id": SUPPORT_ID,
        "support_sha256": SHA_B,
        "signal_type": "definition_binding",
        "action": "drop",
        "matched_rule_id": "usa_rule",
        "rule_citation": "45 CFR 164.514",
        "jurisdictions": ["USA"],
        "transform_requirement_id": None,
        "transform_id": None,
        "confidence": 0.99,
    }
    payload.update(overrides)
    return payload




@pytest.mark.parametrize("enum_name, expected", [
    ("ModelFailureCode", {
        "disabled", "offline_attestation_missing", "provider_unsupported", "base_url_invalid",
        "base_url_not_allowed", "model_allowlist_empty", "model_not_installed", "model_digest_mismatch",
        "input_too_large", "connection_failed", "timeout", "http_error", "redirect_rejected",
        "response_too_large", "response_too_deep", "response_too_many_items", "string_too_long",
        "invalid_json", "invalid_schema", "binding_mismatch", "rule_mismatch", "confidence_low",
        "unsupported_action", "prompt_gate_blocked",
    }),
    ("VariableType", {"identifier", "date", "quasi_identifier", "categorical", "numeric_count", "free_text", "other"}),
    ("SupportSignalType", {"definition_binding", "action_binding", "transform_binding", "explicit_non_phi"}),
])
def test_enums_are_exact(routing, enum_name, expected):
    assert {item.value for item in getattr(routing, enum_name)} == expected


def test_raw_tasks_and_matched_content_have_no_serializer_or_repr_leak(routing):
    task = _header_task(routing, samples=("TOP SECRET",))
    support = _support_task(routing)
    assert not hasattr(task, "to_json")
    assert not hasattr(support, "to_json")
    assert not hasattr(support.matched_rows[0], "to_json")
    assert not hasattr(support.matched_rows[0].cells[0], "to_json")
    assert "TOP SECRET" not in repr(task)
    assert "Direct identifier" not in repr(support)
    assert "profile_input_sha256" not in repr(task)


def test_resolution_evidence_is_strict_value_free_and_unknown_content_rejects(routing):
    evidence = routing.ResolutionEvidence(profile_input_sha256=SHA_C)
    assert evidence.profile_input_sha256 == SHA_C
    for hostile in (
        {"profile_input_sha256": SHA_C, "raw_value": "TOP SECRET"},
        {"profile_input_sha256": "TOP SECRET"},
        {"profile_input_sha256": 1},
    ):
        with pytest.raises(routing.ModelResponseError) as exc_info:
            _header_task(routing, evidence=hostile)
        assert exc_info.value.code is routing.ModelFailureCode.INVALID_SCHEMA


def test_confidential_samples_are_first_25_distinct_nonempty_values(routing):
    samples = ("", "a", "a", *(str(i) for i in range(30)))
    task = _header_task(routing, samples=samples)
    assert task.samples == ("a", *(str(i) for i in range(24)))


@pytest.mark.parametrize("response_type,payload", [
    ("HeaderResolution", _header_payload()),
    ("SupportSignal", _signal_payload()),
    ("ExtractedRuleCandidate", {
        "rule_id": "live_usa_name", "action": "drop", "literal_aliases": ["name"],
        "citation": "45 CFR 164.514", "jurisdiction": "USA",
    }),
    ("OfficialRuleExtraction", {
        "registry_source_id": "usa_hipaa_164_514", "jurisdiction": "USA", "source_sha256": SHA_A,
        "candidates": [{"rule_id": "live_usa_name", "action": "drop", "literal_aliases": ["name"],
                        "citation": "45 CFR 164.514", "jurisdiction": "USA"}],
    }),
])
def test_response_round_trip_uses_exact_json_field_contract(routing, response_type, payload):
    cls = getattr(routing, response_type)
    parsed = cls.from_json(payload)
    assert parsed.to_json() == payload
    with pytest.raises(routing.ModelResponseError) as exc_info:
        cls.from_json({**payload, "unexpected": "rejected"})
    assert exc_info.value.code is routing.ModelFailureCode.INVALID_SCHEMA


def test_response_parser_rejects_bool_confidence_and_nonfinite_values(routing):
    for bad in (True, float("nan"), float("inf"), -0.1, 1.1):
        with pytest.raises(routing.ModelResponseError) as exc_info:
            routing.HeaderResolution.from_json(_header_payload(confidence=bad))
        assert exc_info.value.code is routing.ModelFailureCode.INVALID_SCHEMA


@pytest.mark.parametrize("case, expected", [
    ('{"a":1,"a":2}', "INVALID_JSON"),
    ('{"a":1} trailing', "INVALID_JSON"),
    ('{"a":NaN}', "INVALID_JSON"),
    ('{"a":Infinity}', "INVALID_JSON"),
    ('```json\n{"a":1}\n```', None),
])
def test_central_json_parser_rejects_ambiguous_json(routing, case, expected):
    if expected is None:
        assert routing._parse_model_json(case) == {"a": 1}
    else:
        with pytest.raises(routing.ModelResponseError) as exc_info:
            routing._parse_model_json(case)
        assert exc_info.value.code is getattr(routing.ModelFailureCode, expected)


def test_central_json_parser_enforces_size_depth_items_and_string_limits(routing):
    cases = [
        ('"' + "x" * (256 * 1024) + '"', routing.ModelFailureCode.RESPONSE_TOO_LARGE),
        ("[" * 17 + "0" + "]" * 17, routing.ModelFailureCode.RESPONSE_TOO_DEEP),
        ("[" * 2000 + "0" + "]" * 2000, routing.ModelFailureCode.RESPONSE_TOO_DEEP),
        (json.dumps(list(range(513))), routing.ModelFailureCode.RESPONSE_TOO_MANY_ITEMS),
        (json.dumps("x" * 4097), routing.ModelFailureCode.STRING_TOO_LONG),
        ("\ud800", routing.ModelFailureCode.INVALID_JSON),
    ]
    for raw, code in cases:
        with pytest.raises(routing.ModelResponseError) as exc_info:
            routing._parse_model_json(raw)
        assert exc_info.value.code is code


def test_confidence_overflow_is_a_controlled_schema_error(routing):
    with pytest.raises(routing.ModelResponseError) as exc_info:
        routing.HeaderResolution.from_json(_header_payload(confidence=10**10000))
    assert exc_info.value.code is routing.ModelFailureCode.INVALID_SCHEMA


def test_support_task_enforces_row_cell_string_and_canonical_size_bounds(routing):
    too_many_rows = tuple(_row(routing) for _ in range(129))
    too_many_cells = tuple(
        routing.MatchedSupportCell(index, "x") for index in range(4097)
    )
    too_long_cell = (routing.MatchedSupportCell(0, "x" * 257),)
    huge_canonical = tuple(
        routing.MatchedSupportCell(index, "x" * 256) for index in range(300)
    )
    for rows in (
        too_many_rows,
        (_row(routing, cells=too_many_cells),),
        (_row(routing, cells=too_long_cell),),
        (_row(routing, cells=huge_canonical),),
    ):
        with pytest.raises(routing.ModelResponseError) as exc_info:
            _support_task(routing, rows=rows)
        assert exc_info.value.code is routing.ModelFailureCode.INPUT_TOO_LARGE


def test_support_row_requires_unique_columns_and_exact_membership(routing):
    cases = (
        dict(
            matched_column_indices=(0, 0),
            cells=(routing.MatchedSupportCell(0, "x"),),
        ),
        dict(
            matched_column_indices=(1,),
            cells=(routing.MatchedSupportCell(0, "x"),),
        ),
        dict(
            matched_column_indices=(0,),
            cells=(
                routing.MatchedSupportCell(0, "x"),
                routing.MatchedSupportCell(0, "y"),
            ),
        ),
    )
    for values in cases:
        with pytest.raises(routing.ModelResponseError) as exc_info:
            routing.MatchedSupportRow(
                support_artifact_id=SUPPORT_ID,
                support_sha256=SHA_B,
                sheet_index=0,
                table_index=0,
                row_index=0,
                **values,
            )
        assert exc_info.value.code is routing.ModelFailureCode.INVALID_SCHEMA


def test_router_constructor_exposes_no_client_fetcher_or_decision_injection(routing):
    assert tuple(inspect.signature(routing.ModelTaskRouter).parameters) == ()


def test_confidential_header_internally_owns_exact_offline_client(
    routing, monkeypatch
):
    local_calls = []

    def local_complete(self, prompt):
        assert type(self) is routing.OfflineLocalLLMClient
        local_calls.append(prompt)
        return json.dumps(_header_payload())

    monkeypatch.setattr(routing, "_local_completion_transport", local_complete)
    monkeypatch.setattr(
        routing,
        "_ordinary_completion_transport",
        lambda self, prompt: pytest.fail("ordinary client must not be used"),
    )
    result = routing.ModelTaskRouter().resolve_confidential_header(
        _header_task(routing)
    )
    assert result.to_json() == _header_payload()
    assert len(local_calls) == 1


def test_confidential_local_unavailability_never_falls_back_to_ordinary(
    routing, monkeypatch
):
    def unavailable(self, prompt):
        raise routing.LocalModelUnavailableError(routing.ModelFailureCode.DISABLED)

    monkeypatch.setattr(routing, "_local_completion_transport", unavailable)
    monkeypatch.setattr(
        routing,
        "_ordinary_completion_transport",
        lambda self, prompt: pytest.fail("ordinary client must not be used"),
    )
    with pytest.raises(routing.LocalModelUnavailableError) as exc_info:
        routing.ModelTaskRouter().resolve_confidential_header(_header_task(routing))
    assert exc_info.value.code is routing.ModelFailureCode.DISABLED


def test_confidential_support_internally_uses_exact_offline_client(
    routing, monkeypatch
):
    local_calls = []

    def local_complete(self, prompt):
        assert type(self) is routing.OfflineLocalLLMClient
        local_calls.append(prompt)
        return json.dumps([_signal_payload()])

    monkeypatch.setattr(routing, "_local_completion_transport", local_complete)
    monkeypatch.setattr(
        routing,
        "_ordinary_completion_transport",
        lambda self, prompt: pytest.fail("ordinary client must not be used"),
    )
    result = routing.ModelTaskRouter().extract_support_signals(
        _support_task(routing)
    )
    assert [item.to_json() for item in result] == [_signal_payload()]
    assert len(local_calls) == 1


def test_verified_nonconfidential_builder_uses_only_exact_matched_normalized_rows(
    routing, tmp_path, monkeypatch
):
    router, task, dataset, *_ = _build_verified_support_task(routing, tmp_path)
    assert not dataset.normalized_rows_path.exists()
    assert len(task.matched_rows) == 1
    assert task.matched_rows[0].matched_column_indices == (0,)
    assert tuple(cell.column_index for cell in task.matched_rows[0].cells) == (0, 1)
    ordinary_calls = []

    def ordinary_complete(self, prompt):
        assert type(self) is config.LLMClient
        ordinary_calls.append(prompt)
        return json.dumps([_signal_payload()])

    monkeypatch.setattr(
        routing, "_ordinary_completion_transport", ordinary_complete
    )
    monkeypatch.setattr(
        routing,
        "_local_completion_transport",
        lambda self, prompt: pytest.fail("local client must not be used"),
    )
    assert [item.to_json() for item in router.extract_support_signals(task)] == [
        _signal_payload()
    ]
    assert len(ordinary_calls) == 1

def test_verified_builder_accepts_human_level_override(routing, tmp_path):
    dataset, support, recommendation, decision = _verified_phase3_context(
        routing, tmp_path
    )
    decision = replace(decision, level=DependencyLevel.REQUIRED)
    task = routing.ModelTaskRouter().build_support_signal_task(
        dataset=dataset,
        support=support,
        recommendation=recommendation,
        decision=decision,
        current_basis=recommendation.basis,
        candidate_rules=(_candidate(routing),),
    )
    assert task.dependency_decision_id == decision.decision_id




def test_normalized_artifact_streams_and_hash_tamper_blocks_dispatch(
    routing, tmp_path, monkeypatch
):
    dataset, support, recommendation, decision = _verified_phase3_context(
        routing, tmp_path
    )
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda self: pytest.fail("normalized artifact must not use read_bytes"),
    )
    router = routing.ModelTaskRouter()
    task = router.build_support_signal_task(
        dataset=dataset,
        support=support,
        recommendation=recommendation,
        decision=decision,
        current_basis=recommendation.basis,
        candidate_rules=(_candidate(routing),),
    )
    support.normalized_rows_path.write_text("tampered", encoding="utf-8")
    monkeypatch.setattr(
        routing,
        "_ordinary_completion_transport",
        lambda client, prompt: pytest.fail("tampered artifact reached transport"),
    )
    with pytest.raises(routing.ModelResponseError) as exc_info:
        router.extract_support_signals(task)
    assert exc_info.value.code is routing.ModelFailureCode.BINDING_MISMATCH



def test_verified_rows_use_open_descriptor_when_path_is_replaced(
    routing, tmp_path, monkeypatch
):
    dataset, support, recommendation, decision = _verified_phase3_context(
        routing, tmp_path
    )
    approved_path = support.normalized_rows_path
    replacement = tmp_path / "replacement.jsonl"
    hostile_raw = (
        json.dumps(
            _normalized_row(0, ["participant_name", "hostile replacement"]),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    replacement.write_bytes(hostile_raw)
    real_open = Path.open
    open_count = 0

    def open_and_replace(self, *args, **kwargs):
        nonlocal open_count
        descriptor = real_open(self, *args, **kwargs)
        if self == approved_path and args and args[0] == "rb":
            open_count += 1
            replacement.replace(approved_path)
        return descriptor

    monkeypatch.setattr(Path, "open", open_and_replace)
    task = routing.ModelTaskRouter().build_support_signal_task(
        dataset=dataset,
        support=support,
        recommendation=recommendation,
        decision=decision,
        current_basis=recommendation.basis,
        candidate_rules=(_candidate(routing),),
    )

    assert open_count == 1
    assert tuple(cell.value for cell in task.matched_rows[0].cells) == (
        "participant_name",
        "Direct identifier",
    )


def test_verified_rows_reject_oversized_line_before_json_parse(
    routing, tmp_path, monkeypatch
):
    dataset, support, recommendation, decision = _verified_phase3_context(
        routing, tmp_path
    )
    raw = b"x" * (routing._MAX_NORMALIZED_LINE_BYTES + 1)
    support, recommendation, decision = _with_normalized_raw(
        support, recommendation, decision, raw
    )
    monkeypatch.setattr(
        routing,
        "_strict_json_loads",
        lambda line: pytest.fail("oversized line reached JSON parser"),
    )

    with pytest.raises(routing.ModelResponseError) as exc_info:
        routing.ModelTaskRouter().build_support_signal_task(
            dataset=dataset,
            support=support,
            recommendation=recommendation,
            decision=decision,
            current_basis=recommendation.basis,
            candidate_rules=(_candidate(routing),),
        )
    assert exc_info.value.code is routing.ModelFailureCode.INPUT_TOO_LARGE


def test_verified_rows_reject_total_bytes_before_json_parse(
    routing, tmp_path, monkeypatch
):
    dataset, support, recommendation, decision = _verified_phase3_context(
        routing, tmp_path
    )
    raw = b"x" * 65
    support, recommendation, decision = _with_normalized_raw(
        support, recommendation, decision, raw
    )
    monkeypatch.setattr(routing, "_MAX_NORMALIZED_SUPPORT_BYTES", 64)
    monkeypatch.setattr(
        routing,
        "_strict_json_loads",
        lambda line: pytest.fail("oversized artifact reached JSON parser"),
    )

    with pytest.raises(routing.ModelResponseError) as exc_info:
        routing.ModelTaskRouter().build_support_signal_task(
            dataset=dataset,
            support=support,
            recommendation=recommendation,
            decision=decision,
            current_basis=recommendation.basis,
            candidate_rules=(_candidate(routing),),
        )
    assert exc_info.value.code is routing.ModelFailureCode.INPUT_TOO_LARGE


def test_verified_rows_reject_129th_match(routing, tmp_path):
    dataset, support, recommendation, decision = _verified_phase3_context(
        routing, tmp_path
    )
    raw = "".join(
        json.dumps(
            _normalized_row(row_index, ["participant_name"]),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for row_index in range(129)
    ).encode("utf-8")
    support, recommendation, decision = _with_normalized_raw(
        support, recommendation, decision, raw
    )

    with pytest.raises(routing.ModelResponseError) as exc_info:
        routing.ModelTaskRouter().build_support_signal_task(
            dataset=dataset,
            support=support,
            recommendation=recommendation,
            decision=decision,
            current_basis=recommendation.basis,
            candidate_rules=(_candidate(routing),),
        )
    assert exc_info.value.code is routing.ModelFailureCode.INPUT_TOO_LARGE


def test_verified_rows_reject_4097th_included_cell(
    routing, tmp_path, monkeypatch
):
    dataset, support, recommendation, decision = _verified_phase3_context(
        routing, tmp_path
    )
    rows = [
        _normalized_row(row_index, ["participant_name", *([""] * 31)])
        for row_index in range(127)
    ]
    rows.append(_normalized_row(127, ["participant_name", *([""] * 32)]))
    raw = "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
        for row in rows
    ).encode("utf-8")
    support, recommendation, decision = _with_normalized_raw(
        support, recommendation, decision, raw
    )
    monkeypatch.setattr(routing, "_MAX_TASK_BYTES", 1024 * 1024)

    with pytest.raises(routing.ModelResponseError) as exc_info:
        routing.ModelTaskRouter().build_support_signal_task(
            dataset=dataset,
            support=support,
            recommendation=recommendation,
            decision=decision,
            current_basis=recommendation.basis,
            candidate_rules=(_candidate(routing),),
        )
    assert exc_info.value.code is routing.ModelFailureCode.INPUT_TOO_LARGE


def test_verified_rows_reject_included_cell_over_256_codepoints(
    routing, tmp_path
):
    dataset, support, recommendation, decision = _verified_phase3_context(
        routing, tmp_path
    )
    raw = (
        json.dumps(
            _normalized_row(0, ["participant_name", "x" * 257]),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    support, recommendation, decision = _with_normalized_raw(
        support, recommendation, decision, raw
    )

    with pytest.raises(routing.ModelResponseError) as exc_info:
        routing.ModelTaskRouter().build_support_signal_task(
            dataset=dataset,
            support=support,
            recommendation=recommendation,
            decision=decision,
            current_basis=recommendation.basis,
            candidate_rules=(_candidate(routing),),
        )
    assert exc_info.value.code is routing.ModelFailureCode.INPUT_TOO_LARGE


def test_verified_rows_reject_incremental_matched_payload_over_64k(
    routing, tmp_path
):
    dataset, support, recommendation, decision = _verified_phase3_context(
        routing, tmp_path
    )
    rows = [
        _normalized_row(
            row_index,
            ["participant_name", *(["x" * 32] * 9)],
        )
        for row_index in range(100)
    ]
    raw = "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
        for row in rows
    ).encode("utf-8")
    support, recommendation, decision = _with_normalized_raw(
        support, recommendation, decision, raw
    )

    with pytest.raises(routing.ModelResponseError) as exc_info:
        routing.ModelTaskRouter().build_support_signal_task(
            dataset=dataset,
            support=support,
            recommendation=recommendation,
            decision=decision,
            current_basis=recommendation.basis,
            candidate_rules=(_candidate(routing),),
        )
    assert exc_info.value.code is routing.ModelFailureCode.INPUT_TOO_LARGE


def test_router_phi_gate_blocks_verified_nonconfidential_prompt_before_client(
    routing, tmp_path, monkeypatch
):
    dataset, support, recommendation, decision = _verified_phase3_context(
        routing, tmp_path
    )
    row = {
        "support_artifact_id": SUPPORT_ID,
        "source_sha256": SHA_B,
        "sheet_index": 0,
        "table_index": 0,
        "row_index": 0,
        "cells": [
            {"column_index": 0, "value": "participant_name"},
            {"column_index": 1, "value": "123-45-6789"},
        ],
    }
    raw = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
    support.normalized_rows_path.write_bytes(raw)
    normalized_sha = hashlib.sha256(raw).hexdigest()
    support = replace(support, normalized_rows_sha256=normalized_sha)
    recommendation = replace(
        recommendation, normalized_support_sha256=normalized_sha
    )
    decision = replace(decision, normalized_support_sha256=normalized_sha)
    router = routing.ModelTaskRouter()
    task = router.build_support_signal_task(
        dataset=dataset,
        support=support,
        recommendation=recommendation,
        current_basis=recommendation.basis,
        decision=decision,
        candidate_rules=(_candidate(routing),),
    )
    monkeypatch.setattr(routing, "_ordinary_completion_transport", lambda self, prompt: pytest.fail("blocked prompt reached ordinary client"))
    with pytest.raises(routing.ModelResponseError) as exc_info:
        router.extract_support_signals(task)
    assert exc_info.value.code is routing.ModelFailureCode.PROMPT_GATE_BLOCKED


def test_nonconfidential_support_requires_trusted_builder(routing, monkeypatch):
    calls = []
    monkeypatch.setattr(routing, "_ordinary_completion_transport", lambda self, prompt: calls.append(prompt) or json.dumps([_signal_payload()]))
    with pytest.raises(routing.ModelResponseError) as exc_info:
        routing.ModelTaskRouter().extract_support_signals(
            _support_task(routing, sensitivity=Sensitivity.NON_CONFIDENTIAL)
        )
    assert exc_info.value.code is routing.ModelFailureCode.BINDING_MISMATCH
    assert calls == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda rec, dec: (
            replace(rec, recommendation_id="dr_" + "6" * 32),
            replace(dec, recommendation_id="dr_" + "6" * 32),
        ),
        lambda rec, dec: (
            replace(
                rec,
                transform_requirement_ids=("tr_" + "7" * 32,),
            ),
            dec,
        ),
        lambda rec, dec: (replace(rec, dataset_sha256="6" * 64), dec),
        lambda rec, dec: (replace(rec, support_sha256="6" * 64), dec),
        lambda rec, dec: (replace(rec, normalized_support_sha256="6" * 64), dec),
        lambda rec, dec: (replace(rec, kind=DependencyKind.DICTIONARY), dec),
        lambda rec, dec: (
            replace(rec, default_sensitivity=Sensitivity.CONFIDENTIAL),
            dec,
        ),
        lambda rec, dec: (
            replace(rec, reason_code=DependencyReasonCode.SAME_STEM_COMPANION),
            dec,
        ),
        lambda rec, dec: (
            replace(rec, basis=replace(rec.basis, rulebook_sha256="6" * 64)),
            dec,
        ),
        lambda rec, dec: (
            replace(rec, basis=replace(rec.basis, scrub_config_sha256="6" * 64)),
            dec,
        ),
        lambda rec, dec: (
            replace(rec, basis=replace(rec.basis, support_role_sha256="6" * 64)),
            dec,
        ),
        lambda rec, dec: (rec, replace(dec, level=DependencyLevel.IGNORED)),
    ],
)
def test_stale_phase3_approval_blocks_with_zero_dispatch(
    routing, tmp_path, monkeypatch, mutate
):
    dataset, support, recommendation, decision = _verified_phase3_context(
        routing, tmp_path
    )
    recommendation, decision = mutate(recommendation, decision)
    calls = []
    monkeypatch.setattr(
        routing,
        "_ordinary_completion_transport",
        lambda self, prompt: calls.append(prompt)
        or json.dumps([_signal_payload()]),
    )
    with pytest.raises(routing.ModelResponseError) as exc_info:
        routing.ModelTaskRouter().build_support_signal_task(
            dataset=dataset,
            support=support,
            recommendation=recommendation,
            decision=decision,
            current_basis=recommendation.basis,
            candidate_rules=(_candidate(routing),),
        )
    assert exc_info.value.code is routing.ModelFailureCode.BINDING_MISMATCH
    assert calls == []


def test_trusted_builder_rejects_stale_current_basis(routing, tmp_path):
    dataset, support, recommendation, decision = _verified_phase3_context(
        routing, tmp_path
    )
    current_basis = recommendation.basis
    stale_basis = replace(current_basis, rulebook_sha256="6" * 64)
    recommendation = replace(recommendation, basis=stale_basis)
    decision = replace(decision, basis=stale_basis)

    with pytest.raises(routing.ModelResponseError) as exc_info:
        routing.ModelTaskRouter().build_support_signal_task(
            dataset=dataset,
            support=support,
            recommendation=recommendation,
            decision=decision,
            current_basis=current_basis,
            candidate_rules=(_candidate(routing),),
        )
    assert exc_info.value.code is routing.ModelFailureCode.BINDING_MISMATCH


def test_task_and_normalized_artifact_tamper_block_dispatch(
    routing, tmp_path, monkeypatch
):
    router, task, _, support, *_ = _build_verified_support_task(routing, tmp_path)
    tampered_task = replace(
        task,
        matched_rows=(
            replace(
                task.matched_rows[0],
                cells=(routing.MatchedSupportCell(0, "covert confidential value"),),
                matched_column_indices=(0,),
            ),
        ),
    )
    calls = []
    monkeypatch.setattr(
        routing,
        "_ordinary_completion_transport",
        lambda self, prompt: calls.append(prompt)
        or json.dumps([_signal_payload()]),
    )
    with pytest.raises(routing.ModelResponseError) as exc_info:
        router.extract_support_signals(tampered_task)
    assert exc_info.value.code is routing.ModelFailureCode.BINDING_MISMATCH
    support.normalized_rows_path.write_text("tampered", encoding="utf-8")
    with pytest.raises(routing.ModelResponseError) as exc_info:
        router.extract_support_signals(task)
    assert exc_info.value.code is routing.ModelFailureCode.BINDING_MISMATCH
    assert calls == []


@pytest.mark.parametrize(
    "row",
    [
        {"support_artifact_id": SUPPORT_ID},
        {
            "support_artifact_id": SUPPORT_ID,
            "source_sha256": SHA_B,
            "sheet_index": 0,
            "table_index": 0,
            "row_index": 0,
            "cells": [
                {"column_index": 0, "value": "participant_name"},
                {"column_index": 0, "value": "duplicate"},
            ],
        },
        {
            "support_artifact_id": SUPPORT_ID,
            "source_sha256": SHA_B,
            "sheet_index": 0,
            "table_index": 0,
            "row_index": 0,
            "cells": [{"column_index": 0, "value": "participant_name", "raw": "x"}],
        },
    ],
)
def test_normalized_row_schema_is_exact_before_binding(routing, tmp_path, row):
    dataset, support, recommendation, decision = _verified_phase3_context(
        routing, tmp_path
    )
    raw = (json.dumps(row, separators=(",", ":")) + "\n").encode()
    support.normalized_rows_path.write_bytes(raw)
    object.__setattr__(
        support, "normalized_rows_sha256", hashlib.sha256(raw).hexdigest()
    )
    recommendation = replace(
        recommendation,
        normalized_support_sha256=support.normalized_rows_sha256,
    )
    decision = replace(
        decision,
        normalized_support_sha256=support.normalized_rows_sha256,
    )
    with pytest.raises(routing.ModelResponseError) as exc_info:
        routing.ModelTaskRouter().build_support_signal_task(
            dataset=dataset,
            support=support,
            recommendation=recommendation,
            decision=decision,
            current_basis=recommendation.basis,
            candidate_rules=(_candidate(routing),),
        )
    assert exc_info.value.code is routing.ModelFailureCode.BINDING_MISMATCH


def test_support_transform_ids_must_be_null_without_trusted_projection(
    routing, monkeypatch
):
    for field in ("transform_requirement_id", "transform_id"):
        monkeypatch.setattr(
            routing,
            "_local_completion_transport",
            lambda self, prompt, field=field: json.dumps(
                [_signal_payload(**{field: "Direct identifier"})]
            ),
        )
        with pytest.raises(routing.ModelResponseError) as exc_info:
            routing.ModelTaskRouter().extract_support_signals(_support_task(routing))
        assert exc_info.value.code is routing.ModelFailureCode.INVALID_SCHEMA


def test_support_transform_ids_use_strict_value_free_formats(routing):
    valid = routing.SupportSignal.from_json(
        _signal_payload(
            transform_requirement_id="tr_" + "1" * 32,
            transform_id="tx_" + "2" * 32,
        )
    )
    assert valid.transform_requirement_id == "tr_" + "1" * 32
    assert valid.transform_id == "tx_" + "2" * 32
    for field in ("transform_requirement_id", "transform_id"):
        with pytest.raises(routing.ModelResponseError) as exc_info:
            routing.SupportSignal.from_json(
                _signal_payload(**{field: "raw cell content"})
            )
        assert exc_info.value.code is routing.ModelFailureCode.INVALID_SCHEMA


def test_binding_rule_and_confidence_failures_are_controlled(routing, monkeypatch):
    cases = [
        (_header_payload(dataset_artifact_id="a_" + "9" * 32), routing.ModelFailureCode.BINDING_MISMATCH),
        (_header_payload(matched_rule_id="invented"), routing.ModelFailureCode.RULE_MISMATCH),
        (_header_payload(confidence=0.1), routing.ModelFailureCode.CONFIDENCE_LOW),
        (_header_payload(action="invented"), routing.ModelFailureCode.UNSUPPORTED_ACTION),
    ]
    payloads = iter(json.dumps(payload) for payload, _ in cases)
    monkeypatch.setattr(
        routing,
        "_local_completion_transport",
        lambda self, prompt: next(payloads),
    )
    for _, code in cases:
        with pytest.raises(routing.ModelResponseError) as exc_info:
            routing.ModelTaskRouter().resolve_confidential_header(_header_task(routing))
        assert exc_info.value.code is code
        assert str(exc_info.value) == code.value
        assert "participant_name" not in str(exc_info.value)


def test_model_response_retry_is_bounded_to_two_local_submissions(
    routing, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        routing,
        "_local_completion_transport",
        lambda self, prompt: calls.append(prompt) or "not-json",
    )
    with pytest.raises(routing.ModelResponseError) as exc_info:
        routing.ModelTaskRouter().resolve_confidential_header(_header_task(routing))
    assert exc_info.value.code is routing.ModelFailureCode.INVALID_JSON
    assert len(calls) == 2


def test_local_physical_generate_submissions_are_capped_at_two(
    routing, monkeypatch
):
    first_name, first_digest = MODEL_SPEC.split("@", 1)
    local = routing.OfflineLocalLLMClient(_local_config(max_retries=1))
    monkeypatch.setattr(routing, "_new_local_client", lambda: local)
    monkeypatch.setattr(routing.http.client, "HTTPConnection", FakeHTTPConnection)
    FakeHTTPConnection.requests = []
    FakeHTTPConnection.responses = [
        FakeHTTPResponse(
            200, _tags({"name": first_name, "digest": first_digest})
        ),
        FakeHTTPResponse(200, json.dumps({"response": "not-json"}).encode()),
        FakeHTTPResponse(
            200, _tags({"name": first_name, "digest": first_digest})
        ),
        FakeHTTPResponse(200, json.dumps({"response": "not-json"}).encode()),
    ]
    with pytest.raises(routing.ModelResponseError) as exc_info:
        routing.ModelTaskRouter().resolve_confidential_header(_header_task(routing))
    assert exc_info.value.code is routing.ModelFailureCode.INVALID_JSON
    assert local._config.max_retries == 0
    assert [
        request for request in FakeHTTPConnection.requests if request[1] == "/api/generate"
    ] == [
        FakeHTTPConnection.requests[1],
        FakeHTTPConnection.requests[3],
    ]


def test_ordinary_physical_provider_submissions_are_capped_at_two(
    routing, tmp_path, monkeypatch
):
    ordinary = config.LLMClient(
        provider="ollama",
        model="qwen3:8b",
        base_url="http://127.0.0.1:11434",
        max_retries=9,
    )
    calls = []
    monkeypatch.setattr(routing, "_new_ordinary_client", lambda: ordinary)
    monkeypatch.setattr(
        config.LLMClient,
        "_complete_ollama",
        lambda self, prompt: calls.append(prompt) or "not-json",
    )
    router, task, *_ = _build_verified_support_task(routing, tmp_path)
    with pytest.raises(routing.ModelResponseError) as exc_info:
        router.extract_support_signals(task)
    assert exc_info.value.code is routing.ModelFailureCode.INVALID_JSON
    assert ordinary._max_retries == 0
    assert len(calls) == 2


def test_local_configuration_defaults_and_strict_env_lists(routing):
    local = config._load_local_llm_config({"local_llm": {
        "provider": "none", "models": [], "base_url": "http://127.0.0.1:11434",
        "allowed_base_urls": ["http://127.0.0.1:11434"], "offline_approved": False,
        "timeout_s": 60, "max_retries": 1,
    }}, {})
    assert local == config.LocalLLMConfig(
        provider="none", models=(), base_url="http://127.0.0.1:11434",
        allowed_base_urls=("http://127.0.0.1:11434",), offline_approved=False,
        timeout_s=60, max_retries=1,
    )
    env = {
        "PHI_LOCAL_LLM_PROVIDER": "ollama",
        "PHI_LOCAL_LLM_MODELS": f" {MODEL_SPEC} ,other@sha256:{'e' * 64}",
        "PHI_LOCAL_LLM_ALLOWED_BASE_URLS": " http://127.0.0.1:11434 , http://[::1]:11434 ",
        "PHI_LOCAL_LLM_BASE_URL": "http://[::1]:11434",
        "PHI_LOCAL_LLM_OFFLINE_APPROVED": "true",
        "PHI_LOCAL_LLM_TIMEOUT_S": "10",
        "PHI_LOCAL_LLM_MAX_RETRIES": "0",
    }
    parsed = config._load_local_llm_config({"local_llm": local.to_yaml_value()}, env)
    assert parsed.models == (MODEL_SPEC, "other@sha256:" + "e" * 64)
    assert parsed.allowed_base_urls == ("http://127.0.0.1:11434", "http://[::1]:11434")
    assert parsed.base_url == "http://[::1]:11434"
    assert parsed.offline_approved is True


@pytest.mark.parametrize("block, env", [
    ({"local_llm": {"provider": "none", "models": MODEL_SPEC, "allowed_base_urls": [], "base_url": "http://127.0.0.1:11434", "offline_approved": False, "timeout_s": 60, "max_retries": 1}}, {}),
    ({"local_llm": {"provider": "none", "models": [], "allowed_base_urls": "http://127.0.0.1:11434", "base_url": "http://127.0.0.1:11434", "offline_approved": False, "timeout_s": 60, "max_retries": 1}}, {}),
    ({"local_llm": {}}, {"PHI_LOCAL_LLM_MODELS": f"{MODEL_SPEC},"}),
    ({"local_llm": {}}, {"PHI_LOCAL_LLM_MODELS": f"{MODEL_SPEC},{MODEL_SPEC}"}),
    ({"local_llm": {}}, {"PHI_LOCAL_LLM_MODELS": "bad-model"}),
    ({"local_llm": {}}, {"PHI_LOCAL_LLM_OFFLINE_APPROVED": "maybe"}),
    ({"local_llm": {}}, {"PHI_LOCAL_LLM_MAX_RETRIES": "2"}),
])
def test_malformed_local_security_configuration_is_rejected(routing, block, env):
    with pytest.raises(config.LocalLLMConfigurationError):
        config._load_local_llm_config(block, env)


def test_cli_returns_2_for_malformed_local_security_configuration(monkeypatch, capsys):
    from phi_engine.cli.main import main

    monkeypatch.setenv("PHI_LOCAL_LLM_MODELS", "not-a-name-and-digest")
    assert main(["status", "--study", "local-config-check"]) == 2
    assert capsys.readouterr().err == "invalid local LLM configuration\n"


def _local_config(**overrides):
    values = {
        "provider": "ollama",
        "models": (MODEL_SPEC,),
        "base_url": "http://127.0.0.1:11434",
        "allowed_base_urls": ("http://127.0.0.1:11434",),
        "offline_approved": True,
        "timeout_s": 2,
        "max_retries": 1,
    }
    values.update(overrides)
    return config.LocalLLMConfig(**values)


@pytest.mark.parametrize("overrides, code", [
    ({"provider": "none"}, "DISABLED"),
    ({"offline_approved": False}, "OFFLINE_ATTESTATION_MISSING"),
    ({"provider": "openai"}, "PROVIDER_UNSUPPORTED"),
    ({"models": ()}, "MODEL_ALLOWLIST_EMPTY"),
    ({"base_url": "http://user:secret@127.0.0.1:11434"}, "BASE_URL_INVALID"),
    ({"base_url": "http://127.0.0.1:11434/api"}, "BASE_URL_INVALID"),
    ({"base_url": "http://127.0.0.1:11434?target=external"}, "BASE_URL_INVALID"),
    ({"base_url": "http://127.0.0.1:11434#fragment"}, "BASE_URL_INVALID"),
    ({"base_url": "http://127.0.0.1:11434/"}, "BASE_URL_INVALID"),
    ({"base_url": "http://localhost:11434", "allowed_base_urls": ("http://localhost:11434",)}, "BASE_URL_NOT_ALLOWED"),
    ({"allowed_base_urls": ("http://[::1]:11434",)}, "BASE_URL_NOT_ALLOWED"),
])
def test_offline_client_fails_closed_before_transport(routing, overrides, code):
    client = routing.OfflineLocalLLMClient(_local_config(**overrides))
    with pytest.raises(routing.LocalModelUnavailableError) as exc_info:
        client.complete("safe prompt")
    assert exc_info.value.code is getattr(routing.ModelFailureCode, code)
    assert str(exc_info.value) == getattr(routing.ModelFailureCode, code).value
    assert "secret" not in str(exc_info.value)


@dataclass
class FakeHTTPResponse:
    status: int
    payload: bytes

    def read(self, amount: int | None = None) -> bytes:
        if amount is None:
            return self.payload
        return self.payload[:amount]

    def close(self) -> None:
        pass


class FakeHTTPConnection:
    responses: list[FakeHTTPResponse] = []
    requests: list[tuple[str, str, bytes | None, dict[str, str]]] = []

    def __init__(self, host: str, port: int, timeout: float):
        assert host in {"127.0.0.1", "::1"}
        self.host = host
        self.port = port
        self.timeout = timeout

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None):
        self.requests.append((method, path, body, headers or {}))

    def getresponse(self):
        return self.responses.pop(0)

    def close(self) -> None:
        pass


def _tags(*models):
    return json.dumps({"models": list(models)}, separators=(",", ":")).encode()


def test_offline_ollama_selects_first_exact_installed_name_and_digest_without_proxy(routing, monkeypatch):
    first_name, first_digest = MODEL_SPEC.split("@", 1)
    second = "second@sha256:" + "e" * 64
    second_name, second_digest = second.split("@", 1)
    FakeHTTPConnection.requests = []
    FakeHTTPConnection.responses = [
        FakeHTTPResponse(200, _tags({"name": second_name, "digest": second_digest}, {"name": first_name, "digest": first_digest})),
        FakeHTTPResponse(200, json.dumps({"response": "{\"ok\":true}"}).encode()),
    ]
    monkeypatch.setattr(routing.http.client, "HTTPConnection", FakeHTTPConnection)
    monkeypatch.setenv("HTTP_PROXY", "http://attacker.invalid:9999")
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.invalid:9999")
    client = routing.OfflineLocalLLMClient(_local_config(models=(MODEL_SPEC, second)))
    assert client.complete("safe") == '{"ok":true}'
    assert [(method, path) for method, path, *_ in FakeHTTPConnection.requests] == [
        ("GET", "/api/tags"), ("POST", "/api/generate")
    ]
    generate = json.loads(FakeHTTPConnection.requests[1][2])
    assert generate["model"] == first_name


@pytest.mark.parametrize("models, code", [
    (({"name": "missing", "digest": "sha256:" + "d" * 64},), "MODEL_NOT_INSTALLED"),
    (({"name": "qwen3:8b", "digest": "sha256:" + "e" * 64},), "MODEL_DIGEST_MISMATCH"),
])
def test_offline_ollama_never_falls_back_on_missing_or_mismatched_model(routing, monkeypatch, models, code):
    FakeHTTPConnection.requests = []
    FakeHTTPConnection.responses = [FakeHTTPResponse(200, _tags(*models))]
    monkeypatch.setattr(routing.http.client, "HTTPConnection", FakeHTTPConnection)
    with pytest.raises(routing.LocalModelUnavailableError) as exc_info:
        routing.OfflineLocalLLMClient(_local_config()).complete("safe")
    assert exc_info.value.code is getattr(routing.ModelFailureCode, code)
    assert [(method, path) for method, path, *_ in FakeHTTPConnection.requests] == [("GET", "/api/tags")]


@pytest.mark.parametrize("status, code", [(302, "REDIRECT_REJECTED"), (500, "HTTP_ERROR")])
def test_offline_ollama_rejects_redirects_and_http_errors(routing, monkeypatch, status, code):
    FakeHTTPConnection.requests = []
    FakeHTTPConnection.responses = [FakeHTTPResponse(status, b"")]
    monkeypatch.setattr(routing.http.client, "HTTPConnection", FakeHTTPConnection)
    with pytest.raises(routing.LocalModelUnavailableError) as exc_info:
        routing.OfflineLocalLLMClient(_local_config()).complete("safe")
    assert exc_info.value.code is getattr(routing.ModelFailureCode, code)


class FakeHTTPSConnection:
    responses: list[FakeHTTPResponse] = []
    requests: list[tuple[str, int, str, str]] = []

    def __init__(self, host: str, port: int, timeout: float):
        self.host = host
        self.port = port
        self.timeout = timeout

    def request(self, method: str, path: str, headers=None):
        self.requests.append((self.host, self.port, method, path))

    def getresponse(self):
        return self.responses.pop(0)

    def close(self):
        pass


def test_official_transport_uses_exact_registered_host_path_and_ignores_proxy(
    monkeypatch
):
    from phi_engine.security import official_sources

    FakeHTTPSConnection.requests = []
    FakeHTTPSConnection.responses = [FakeHTTPResponse(200, b"official")]
    monkeypatch.setattr(
        official_sources.http.client, "HTTPSConnection", FakeHTTPSConnection
    )
    monkeypatch.setenv("HTTP_PROXY", "http://attacker.invalid:9999")
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.invalid:9999")
    source = official_sources.fetch_registered_source(
        "usa_hipaa_164_514", "USA"
    )
    assert source.body == b"official"
    assert FakeHTTPSConnection.requests == [
        (
            "www.ecfr.gov",
            443,
            "GET",
            "/current/title-45/subtitle-A/subchapter-C/part-164/subpart-E/section-164.514",
        )
    ]


@pytest.mark.parametrize(
    "status,payload,code",
    [
        (302, b"", "source_redirect_rejected"),
        (200, b"x" * 4_000_001, "source_too_large"),
    ],
)
def test_official_transport_rejects_redirects_and_oversize(
    monkeypatch, status, payload, code
):
    from phi_engine.security import official_sources

    FakeHTTPSConnection.requests = []
    FakeHTTPSConnection.responses = [FakeHTTPResponse(status, payload)]
    monkeypatch.setattr(
        official_sources.http.client, "HTTPSConnection", FakeHTTPSConnection
    )
    with pytest.raises(official_sources.RegisteredSourceError, match=code):
        official_sources.fetch_registered_source("usa_hipaa_164_514", "USA")


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@www.ecfr.gov/path",
        "https://www.ecfr.gov/path?query=1",
        "https://www.ecfr.gov/path#fragment",
        "http://www.ecfr.gov/path",
    ],
)
def test_official_private_transport_rejects_noncanonical_registry_urls(
    monkeypatch, url
):
    from phi_engine.security import official_sources

    monkeypatch.setattr(
        official_sources.http.client,
        "HTTPSConnection",
        lambda *args, **kwargs: pytest.fail("invalid URL reached transport"),
    )
    source = official_sources._RegisteredSource(
        registry_source_id="test",
        jurisdiction="TEST",
        url=url,
        citation="test",
    )
    with pytest.raises(
        official_sources.RegisteredSourceError, match="source_registry_invalid"
    ):
        official_sources._fetch_registered_url(source)


def test_official_extraction_accepts_only_registered_id_and_jurisdiction(routing):
    signature = inspect.signature(routing.ModelTaskRouter.extract_official_rules)
    assert tuple(signature.parameters) == ("self", "registry_source_id", "jurisdiction")
    assert "fetcher" not in inspect.signature(routing.ModelTaskRouter).parameters


def test_official_extraction_fixed_segments_include_authoritative_schema_and_hash(
    routing, monkeypatch
):
    from phi_engine.security import official_sources

    body = b"Official HIPAA public text"
    source_sha = hashlib.sha256(body).hexdigest()
    fetch_calls = []
    fixed_calls = []
    gate_calls = []
    real_gate = routing.phi_gate_check

    def recording_gate(segments):
        gate_calls.append(tuple(segments))
        return real_gate(segments)

    def fake_transport(source):
        fetch_calls.append((source.registry_source_id, source.jurisdiction))
        return body

    def fake_verified(self, fixed_prefix, public_document, fixed_suffix):
        assert type(self) is config.LLMClient
        fixed_calls.append((fixed_prefix, public_document, fixed_suffix))
        fixed = fixed_prefix + fixed_suffix
        assert "usa_hipaa_164_514" in fixed
        assert source_sha in fixed
        for field_name in (
            "registry_source_id",
            "jurisdiction",
            "source_sha256",
            "candidates",
            "rule_id",
            "action",
            "literal_aliases",
            "citation",
        ):
            assert field_name in fixed
        assert all(action.value in fixed for action in Action)
        return json.dumps(
            {
                "registry_source_id": "usa_hipaa_164_514",
                "jurisdiction": "USA",
                "source_sha256": source_sha,
                "candidates": [
                    {
                        "rule_id": "live_usa_name",
                        "action": "drop",
                        "literal_aliases": ["name"],
                        "citation": "45 CFR 164.514",
                        "jurisdiction": "USA",
                    }
                ],
            }
        )

    monkeypatch.setattr(routing, "phi_gate_check", recording_gate)
    monkeypatch.setattr(official_sources, "_fetch_registered_url", fake_transport)
    monkeypatch.setattr(routing, "_verified_public_completion_transport", fake_verified)
    result = routing.ModelTaskRouter().extract_official_rules(
        "usa_hipaa_164_514", "USA"
    )
    assert result.source_sha256 == source_sha
    assert fetch_calls == [("usa_hipaa_164_514", "USA")]
    assert fixed_calls[0][1] == "Official HIPAA public text"
    assert len(gate_calls) == 1
    assert len(gate_calls[0]) == 2
    assert "usa_hipaa_164_514" in "".join(gate_calls[0])
    assert source_sha in "".join(gate_calls[0])


def test_verified_public_physical_provider_submissions_are_capped_at_two(
    routing, monkeypatch
):
    from phi_engine.security import official_sources

    ordinary = config.LLMClient(
        provider="ollama",
        model="qwen3:8b",
        base_url="http://127.0.0.1:11434",
        max_retries=9,
    )
    calls = []
    monkeypatch.setattr(routing, "_new_ordinary_client", lambda: ordinary)
    monkeypatch.setattr(
        config.LLMClient,
        "_complete_ollama",
        lambda self, prompt: calls.append(prompt) or "not-json",
    )
    monkeypatch.setattr(
        official_sources,
        "_fetch_registered_url",
        lambda source: b"Official HIPAA public text",
    )
    with pytest.raises(routing.ModelResponseError) as exc_info:
        routing.ModelTaskRouter().extract_official_rules(
            "usa_hipaa_164_514", "USA"
        )
    assert exc_info.value.code is routing.ModelFailureCode.INVALID_JSON
    assert ordinary._max_retries == 0
    assert len(calls) == 2


def test_unregistered_official_pair_never_reaches_transport(routing, monkeypatch):
    from phi_engine.security import official_sources

    monkeypatch.setattr(
        official_sources,
        "_fetch_registered_url",
        lambda source: pytest.fail("unregistered source reached transport"),
    )
    monkeypatch.setattr(
        routing,
        "_verified_public_completion_transport",
        lambda *args: pytest.fail(
            "unregistered source reached LLM transport"
        ),
    )
    with pytest.raises(routing.ModelResponseError) as exc_info:
        routing.ModelTaskRouter().extract_official_rules(
            "usa_hipaa_164_514", "MARS"
        )
    assert exc_info.value.code is routing.ModelFailureCode.BINDING_MISMATCH

def test_safe_errors_never_embed_prompt_response_or_underlying_exception(
    routing, monkeypatch
):
    secret = "PRIVATE-CONTENT-DO-NOT-LEAK"

    def fail(self, prompt):
        raise RuntimeError(secret)

    monkeypatch.setattr(routing, "_local_completion_transport", fail)
    with pytest.raises(routing.LocalModelUnavailableError) as exc_info:
        routing.ModelTaskRouter().resolve_confidential_header(_header_task(routing))
    assert secret not in str(exc_info.value)
    assert secret not in repr(exc_info.value)
    assert exc_info.value.code is routing.ModelFailureCode.CONNECTION_FAILED


# --- complete_bounded: real transport, real request body, real bounds ---------------------


def test_complete_bounded_request_body_carries_num_predict(routing, monkeypatch):
    name, digest = MODEL_SPEC.split("@", 1)
    FakeHTTPConnection.requests = []
    FakeHTTPConnection.responses = [
        FakeHTTPResponse(200, _tags({"name": name, "digest": digest})),
        FakeHTTPResponse(200, json.dumps({"response": '{"study_name":"X","confidence":0.9}'}).encode()),
    ]
    monkeypatch.setattr(routing.http.client, "HTTPConnection", FakeHTTPConnection)
    client = routing.OfflineLocalLLMClient(_local_config())
    result = client.complete_bounded("safe prompt", max_output_tokens=128, max_response_bytes=4096)
    assert result == '{"study_name":"X","confidence":0.9}'
    assert [(method, path) for method, path, *_ in FakeHTTPConnection.requests] == [
        ("GET", "/api/tags"),
        ("POST", "/api/generate"),
    ]
    generate_body = json.loads(FakeHTTPConnection.requests[1][2])
    assert generate_body == {
        "model": name,
        "prompt": "safe prompt",
        "stream": False,
        "options": {"num_predict": 128},
    }


@pytest.mark.parametrize(
    "max_output_tokens, max_response_bytes",
    [
        (0, 4096),
        (-1, 4096),
        (True, 4096),
        (128, 0),
        (128, -1),
        (128, True),
    ],
)
def test_complete_bounded_rejects_invalid_bounds_before_transport(routing, monkeypatch, max_output_tokens, max_response_bytes):
    FakeHTTPConnection.requests = []
    FakeHTTPConnection.responses = []
    monkeypatch.setattr(routing.http.client, "HTTPConnection", FakeHTTPConnection)
    client = routing.OfflineLocalLLMClient(_local_config())
    with pytest.raises(routing.LocalModelUnavailableError) as exc_info:
        client.complete_bounded("safe prompt", max_output_tokens=max_output_tokens, max_response_bytes=max_response_bytes)
    assert exc_info.value.code is routing.ModelFailureCode.INPUT_TOO_LARGE
    assert FakeHTTPConnection.requests == []  # rejected before any transport call




def test_complete_bounded_enforces_its_own_response_byte_ceiling(routing, monkeypatch):
    name, digest = MODEL_SPEC.split("@", 1)
    oversized = json.dumps({"response": "x" * 5000}).encode()
    assert len(oversized) > 4096
    FakeHTTPConnection.requests = []
    FakeHTTPConnection.responses = [
        FakeHTTPResponse(200, _tags({"name": name, "digest": digest})),
        FakeHTTPResponse(200, oversized),
    ]
    monkeypatch.setattr(routing.http.client, "HTTPConnection", FakeHTTPConnection)
    client = routing.OfflineLocalLLMClient(_local_config())
    with pytest.raises(routing.LocalModelUnavailableError) as exc_info:
        client.complete_bounded("safe prompt", max_output_tokens=128, max_response_bytes=4096)
    assert exc_info.value.code is routing.ModelFailureCode.RESPONSE_TOO_LARGE


def test_complete_bounded_response_ceiling_is_independent_of_complete(routing, monkeypatch):
    # A response between 4096 and the ordinary complete() ceiling (256KiB)
    # must still be rejected by complete_bounded's own tighter cap, proving
    # the two methods enforce independent ceilings rather than sharing one
    # global default.
    name, digest = MODEL_SPEC.split("@", 1)
    mid_sized = json.dumps({"response": "y" * 6000}).encode()
    assert 4096 < len(mid_sized) < 256 * 1024
    FakeHTTPConnection.requests = []
    FakeHTTPConnection.responses = [
        FakeHTTPResponse(200, _tags({"name": name, "digest": digest})),
        FakeHTTPResponse(200, mid_sized),
    ]
    monkeypatch.setattr(routing.http.client, "HTTPConnection", FakeHTTPConnection)
    client = routing.OfflineLocalLLMClient(_local_config())
    with pytest.raises(routing.LocalModelUnavailableError) as exc_info:
        client.complete_bounded("safe prompt", max_output_tokens=128, max_response_bytes=4096)
    assert exc_info.value.code is routing.ModelFailureCode.RESPONSE_TOO_LARGE


def test_complete_bounded_still_enforces_endpoint_attestation_and_digest_controls(routing):
    client = routing.OfflineLocalLLMClient(_local_config(offline_approved=False))
    with pytest.raises(routing.LocalModelUnavailableError) as exc_info:
        client.complete_bounded("safe prompt", max_output_tokens=128, max_response_bytes=4096)
    assert exc_info.value.code is routing.ModelFailureCode.OFFLINE_ATTESTATION_MISSING


def test_new_offline_local_client_returns_attested_client_type(routing, monkeypatch):
    monkeypatch.setattr(routing.config, "get_local_llm_config", lambda: _local_config())
    client = routing.new_offline_local_client()
    assert isinstance(client, routing.OfflineLocalLLMClient)


class _CloseFailingResponse:
    def __init__(self, status: int, payload: bytes, close_error: Exception):
        self.status = status
        self._payload = payload
        self._close_error = close_error

    def read(self, amount: int | None = None) -> bytes:
        return self._payload if amount is None else self._payload[:amount]

    def close(self) -> None:
        raise self._close_error


class _CloseFailingConnection:
    instances: list["_CloseFailingConnection"] = []

    def __init__(self, host: str, port: int, timeout: float, *, response: object, close_error: Exception):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._response = response
        self._close_error = close_error
        self.close_called = False
        _CloseFailingConnection.instances.append(self)

    def request(self, method, path, body=None, headers=None):
        pass

    def getresponse(self):
        return self._response

    def close(self) -> None:
        self.close_called = True
        raise self._close_error


def test_response_close_failure_is_normalized_and_connection_close_still_attempted(routing, monkeypatch):
    """response.close() raising must not (a) leak the raw exception past
    complete_bounded, or (b) skip the subsequent connection.close() call."""
    name, digest = MODEL_SPEC.split("@", 1)
    good_tags = FakeHTTPResponse(200, _tags({"name": name, "digest": digest}))
    failing_response = _CloseFailingResponse(200, json.dumps({"response": "ok"}).encode(), RuntimeError("RESPONSE-CLOSE-SENTINEL"))

    made_connections: list[_CloseFailingConnection] = []

    def factory(host, port, timeout):
        if made_connections:
            conn = _CloseFailingConnection(host, port, timeout, response=failing_response, close_error=RuntimeError("RESPONSE-CLOSE-SENTINEL"))
        else:
            conn = _CloseFailingConnection(host, port, timeout, response=good_tags, close_error=RuntimeError("dummy-not-hit"))
            conn.close = lambda: None  # first (tags) round-trip closes cleanly
        made_connections.append(conn)
        return conn

    monkeypatch.setattr(routing.http.client, "HTTPConnection", factory)
    client = routing.OfflineLocalLLMClient(_local_config())
    with pytest.raises(routing.LocalModelUnavailableError) as exc_info:
        client.complete_bounded("safe prompt", max_output_tokens=128, max_response_bytes=4096)
    assert exc_info.value.code is routing.ModelFailureCode.CONNECTION_FAILED
    assert "RESPONSE-CLOSE-SENTINEL" not in str(exc_info.value)
    assert made_connections[-1].close_called  # connection.close() was still attempted




def test_both_response_and_connection_close_failures_are_normalized(routing, monkeypatch):
    """Both response.close() and connection.close() raising distinct
    sentinels must still collapse to one fixed, value-free outcome."""
    name, digest = MODEL_SPEC.split("@", 1)

    class _BothFailConnection:
        request_number = 0

        def __init__(self, host, port, timeout):
            pass

        def request(self, method, path, body=None, headers=None):
            pass

        def getresponse(self):
            _BothFailConnection.request_number += 1
            if _BothFailConnection.request_number == 1:
                return FakeHTTPResponse(200, _tags({"name": name, "digest": digest}))
            return _CloseFailingResponse(200, json.dumps({"response": "ok"}).encode(), RuntimeError("RESP-SENTINEL"))

        def close(self):
            if _BothFailConnection.request_number >= 2:
                raise RuntimeError("CONN-SENTINEL")

    monkeypatch.setattr(routing.http.client, "HTTPConnection", _BothFailConnection)
    client = routing.OfflineLocalLLMClient(_local_config())
    with pytest.raises(routing.LocalModelUnavailableError) as exc_info:
        client.complete_bounded("safe prompt", max_output_tokens=128, max_response_bytes=4096)
    assert exc_info.value.code is routing.ModelFailureCode.CONNECTION_FAILED
    message = str(exc_info.value)
    assert "RESP-SENTINEL" not in message
    assert "CONN-SENTINEL" not in message


def test_close_failure_never_masks_a_primary_fixed_failure(routing, monkeypatch):
    """When a primary fixed failure is already in flight (e.g. an HTTP
    error status), a subsequent close() failure must not override it with
    a different/raw outcome -- the primary fixed code wins."""

    class _PrimaryFailureResponse:
        status = 500

        def read(self, amount=None):
            return b""

        def close(self):
            raise RuntimeError("CLOSE-SENTINEL-DURING-PRIMARY-FAILURE")

    class _Connection:
        def __init__(self, host, port, timeout):
            pass

        def request(self, method, path, body=None, headers=None):
            pass

        def getresponse(self):
            return _PrimaryFailureResponse()

        def close(self):
            pass

    monkeypatch.setattr(routing.http.client, "HTTPConnection", _Connection)
    client = routing.OfflineLocalLLMClient(_local_config(max_retries=0))
    with pytest.raises(routing.LocalModelUnavailableError) as exc_info:
        client.complete_bounded("safe prompt", max_output_tokens=128, max_response_bytes=4096)
    assert exc_info.value.code is routing.ModelFailureCode.HTTP_ERROR
    assert "CLOSE-SENTINEL-DURING-PRIMARY-FAILURE" not in str(exc_info.value)
