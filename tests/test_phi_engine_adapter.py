"""Behavior tests for the phi_engine benchmark adapter."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from benchmarks.phi_engine_adapter import (
    PHI_ENGINE_TO_CORPUS,
    PhiEngineAdapter,
    _map_phi_engine_type,
)
from phi_engine.security.phi_patterns import _verhoeff_validate


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


def _load_india_identifiers_module():
    """Load generators/in/in_identifiers.py; ``in`` is a Python keyword."""
    spec = importlib.util.spec_from_file_location(
        "in_identifiers",
        str(Path(__file__).resolve().parents[1] / "generators" / "in" / "in_identifiers.py"),
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_in_identifiers = _load_india_identifiers_module()


def _valid_aadhaar() -> str:
    aadhaar = _in_identifiers._verhoeff_make("23456789012")
    assert aadhaar.startswith(tuple("23456789"))
    assert len(aadhaar) == 12
    assert _verhoeff_validate(aadhaar)
    return aadhaar


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
    aadhaar = _valid_aadhaar()
    ssn = "123-45-6789"

    aadhaar_text = f"Aadhaar number {aadhaar} verified for study screening."
    ssn_text = f"Study intake lists SSN {ssn} for insurance routing."
    no_phi_text = "clinical note states symptoms improved after hydration and rest."

    records = [
        _record("aadhaar-record", aadhaar_text, [_gold_span(aadhaar_text, aadhaar, "AADHAAR", "in")]),
        _record("ssn-record", ssn_text, [_gold_span(ssn_text, ssn, "SSN", "us")]),
        _record("no-phi-record", no_phi_text, []),
    ]

    jsonl_path = tmp_path / "fixture.jsonl"
    jsonl_path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return {
        "dir": tmp_path,
        "jsonl_path": jsonl_path,
        "records": records,
        "aadhaar": aadhaar,
        "ssn": ssn,
    }


@pytest.fixture
def adapter():
    adapter = PhiEngineAdapter()
    assert adapter._available, "phi_engine presidio/regex dependencies must be available in .venv"
    return adapter


def test_mapping_contracts_include_aadhaar_ssn_and_unknown_fallback():
    assert PHI_ENGINE_TO_CORPUS["AADHAAR"] == frozenset({"AADHAAR"})
    assert PHI_ENGINE_TO_CORPUS["SSN"] == frozenset({"SSN"})
    assert _map_phi_engine_type("SOME_MADE_UP_UNMAPPED_NAME") == frozenset({"UNKNOWN"})


def test_analyze_text_emits_aadhaar_and_ssn_spans(adapter, three_record_corpus):
    aadhaar_record, ssn_record, _ = three_record_corpus["records"]
    aadhaar_span = aadhaar_record["gold_spans"][0]
    ssn_span = ssn_record["gold_spans"][0]

    aadhaar_predictions = adapter.analyze_text(aadhaar_record["text"])
    ssn_predictions = adapter.analyze_text(ssn_record["text"])

    assert any(
        span.entity_type == "AADHAAR"
        and span.start == aadhaar_span["start"]
        and span.end == aadhaar_span["end"]
        and span.mapped_types == frozenset({"AADHAAR"})
        for span in aadhaar_predictions
    ), aadhaar_predictions
    assert any(
        span.entity_type == "SSN"
        and span.start == ssn_span["start"]
        and span.end == ssn_span["end"]
        and span.mapped_types == frozenset({"SSN"})
        for span in ssn_predictions
    ), ssn_predictions


def test_run_file_scores_fixture_records_and_keeps_clean_record_clean(adapter, three_record_corpus):
    scores = adapter.run_file(
        three_record_corpus["jsonl_path"],
        strategy="overlap",
        overlap_threshold=0.5,
        entity_type_agnostic=True,
    )

    assert len(scores) == 3
    scores_by_id = {score["record_id"]: score for score in scores}
    assert set(scores_by_id) == {"aadhaar-record", "ssn-record", "no-phi-record"}
    for score in scores:
        assert REQUIRED_SCORE_KEYS.issubset(score.keys())
        assert {"tp", "fp", "fn", "gap_spans", "matched_gold"}.issubset(
            score["strict_all_span_score"].keys()
        )
        assert score["text_sha256"]
        for prediction in score["predictions"]:
            assert "text" not in prediction

    assert scores_by_id["aadhaar-record"]["tp"] >= 1
    assert scores_by_id["ssn-record"]["tp"] >= 1
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
    assert {row["record_id"] for row in raw_rows} == {"aadhaar-record", "ssn-record", "no-phi-record"}
    assert all("text" not in row for row in raw_rows)
