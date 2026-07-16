"""Behavior tests for the phi_engine benchmark adapter."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.phi_engine_adapter import (
    PHI_ENGINE_TO_CORPUS,
    PhiEngineAdapter,
    _map_phi_engine_type,
)


REQUIRED_SCORE_KEYS = {
    "tp",
    "fp",
    "fn",
    "per_entity_type",
    "per_hipaa_category",
    "per_detection_regime",
    "gap_spans",
    "matched_gold",
    "record_id",
    "corpus_file",
    "predicted_count",
    "gold_count",
    "predictions",
    "text_sha256",
    "strict_all_span_score",
}


def _record(record_id: str, text: str, spans: list[dict]) -> dict:
    return {
        "record_id": record_id,
        "text": text,
        "gold_spans": spans,
        "layer": "test",
        "jurisdiction": "test",
        "detection_regime": "rule_applicable",
        "de_id_tier": "identifiable",
        "risk_tier": "minimal",
        "vulnerability_tags": [],
        "context": "treatment",
        "format": "text",
        "authority_citations": [],
        "metadata": {},
    }


def _gold_span(text: str, value: str, entity_type: str, jurisdiction: str) -> dict:
    start = text.index(value)
    return {
        "start": start,
        "end": start + len(value),
        "entity_type": entity_type,
        "hipaa_category": "G",
        "detection_regime": "rule_applicable",
        "jurisdiction": jurisdiction,
    }


@pytest.fixture
def three_record_corpus(tmp_path):
    ssn = "123-45-6789"
    phone = "415-555-2671"

    ssn_text = f"Study intake lists SSN {ssn} for insurance routing."
    phone_text = f"Callback number {phone} is listed for ambulatory follow-up."
    no_phi_text = "clinical note states symptoms improved after hydration and rest."

    records = [
        _record("ssn-record", ssn_text, [_gold_span(ssn_text, ssn, "SSN", "us")]),
        _record("phone-record", phone_text, [_gold_span(phone_text, phone, "PHONE", "us")]),
        _record("no-phi-record", no_phi_text, []),
    ]

    jsonl_path = tmp_path / "fixture.jsonl"
    jsonl_path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return {
        "dir": tmp_path,
        "jsonl_path": jsonl_path,
        "records": records,
        "ssn": ssn,
        "phone": phone,
    }


@pytest.fixture
def adapter():
    adapter = PhiEngineAdapter()
    assert adapter._available, "phi_engine presidio/regex dependencies must be available in .venv"
    return adapter


def test_mapping_contracts_include_ssn_and_unknown_fallback():
    assert PHI_ENGINE_TO_CORPUS["SSN"] == frozenset({"SSN"})
    assert PHI_ENGINE_TO_CORPUS["US_PHONE"] == frozenset(
        {"PHONE", "PHONE_HOME", "PHONE_WORK", "PHONE_REQUESTOR"}
    )
    assert _map_phi_engine_type("SOME_MADE_UP_UNMAPPED_NAME") == frozenset({"UNKNOWN"})


def test_analyze_text_emits_ssn_and_us_phone_spans(adapter, three_record_corpus):
    ssn_record, phone_record, _ = three_record_corpus["records"]
    ssn_span = ssn_record["gold_spans"][0]
    phone_span = phone_record["gold_spans"][0]

    ssn_predictions = adapter.analyze_text(ssn_record["text"])
    phone_predictions = adapter.analyze_text(phone_record["text"])

    assert any(
        span.entity_type == "SSN"
        and span.start == ssn_span["start"]
        and span.end == ssn_span["end"]
        and span.mapped_types == frozenset({"SSN"})
        for span in ssn_predictions
    ), ssn_predictions
    assert any(
        span.entity_type == "US_PHONE"
        and span.start == phone_span["start"]
        and span.end == phone_span["end"]
        and "PHONE" in span.mapped_types
        for span in phone_predictions
    ), phone_predictions


def test_run_file_scores_fixture_records_and_keeps_clean_record_clean(adapter, three_record_corpus):
    scores = adapter.run_file(
        three_record_corpus["jsonl_path"],
        strategy="overlap",
        overlap_threshold=0.5,
        entity_type_agnostic=True,
    )

    assert len(scores) == 3
    scores_by_id = {score["record_id"]: score for score in scores}
    assert set(scores_by_id) == {"ssn-record", "phone-record", "no-phi-record"}
    for score in scores:
        assert REQUIRED_SCORE_KEYS.issubset(score.keys())
        assert {"tp", "fp", "fn", "gap_spans", "matched_gold"}.issubset(
            score["strict_all_span_score"].keys()
        )
        assert score["text_sha256"]
        for prediction in score["predictions"]:
            assert "text" not in prediction

    assert scores_by_id["ssn-record"]["tp"] >= 1
    assert scores_by_id["phone-record"]["tp"] >= 1
    assert scores_by_id["no-phi-record"]["gold_count"] == 0
    assert scores_by_id["no-phi-record"]["predicted_count"] == 0
    assert scores_by_id["no-phi-record"]["predictions"] == []
    assert scores_by_id["no-phi-record"]["fp"] == 0


def test_run_all_write_results_outputs_dual_scoring_profile(adapter, three_record_corpus, tmp_path):
    result = adapter.run_all(three_record_corpus["dir"])
    output_dir = tmp_path / "results"

    adapter.write_results(result, output_dir)

    summary_path = output_dir / "phi_engine_benchmark_result.json"
    raw_path = output_dir / "phi_engine_raw_predictions.jsonl"
    assert summary_path.exists()
    assert raw_path.exists()

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    raw_rows = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines()]

    assert summary["scoring_profile"] == "legacy_overlap_coverable"
    assert summary["total_records"] == 3
    assert summary["total_gold_spans"] == 2
    assert summary["raw_prediction_artifact"] == "phi_engine_raw_predictions.jsonl"
    assert summary["aggregate_recall"] > 0
    assert summary["aggregate_precision"] > 0
    assert summary["strict_all_span_recall"] > 0
    assert summary["strict_all_span_precision"] > 0
    assert summary["strict_all_span_f1"] > 0
    assert result.strict_all_span.tp >= 2
    assert len(raw_rows) == 3
    assert {row["record_id"] for row in raw_rows} == {"ssn-record", "phone-record", "no-phi-record"}
    assert all("text" not in row for row in raw_rows)
