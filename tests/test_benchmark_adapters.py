"""
Tests for free-tool benchmark adapters.

Covers: SpaCyAdapter (always available via requirements.txt),
        PhilterAdapter, CliniDeIDAdapter, PyDeIDAdapter, PhysioNetDeIDAdapter
        (availability-gated; tests validate interface contract even when tool
        is not installed).

All adapters must:
- Instantiate without error even if the underlying tool is not installed
- Expose an _available bool
- Return BenchmarkResult from run_all() (even if tool is absent)
- Follow the same interface as PresidioAdapter (analyze_text, run_file, run_all, write_results)
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from benchmarks.metrics import BenchmarkResult


# ---------------------------------------------------------------------------
# Fixture: one-record JSONL corpus directory
# ---------------------------------------------------------------------------

SAMPLE_TEXT = (
    "Patient John Smith, DOB 03/12/1985, SSN 123-45-6789, "
    "MRN 4829301, phone (555) 123-4567."
)

SAMPLE_GOLD = [
    {"start": 8, "end": 18, "entity_type": "NAME_PATIENT", "hipaa_category": "A",
     "detection_regime": "contextual_ner_required", "jurisdiction": "us"},
    {"start": 25, "end": 35, "entity_type": "DATE_DOB", "hipaa_category": "C",
     "detection_regime": "contextual_ner_required", "jurisdiction": "us"},
    {"start": 41, "end": 52, "entity_type": "SSN", "hipaa_category": "G",
     "detection_regime": "rule_applicable", "jurisdiction": "us"},
]

SAMPLE_RECORD = {
    "record_id": "test_001",
    "text": SAMPLE_TEXT,
    "gold_spans": SAMPLE_GOLD,
    "layer": "test",
    "jurisdiction": "us",
    "detection_regime": "contextual_ner_required",
    "de_id_tier": "identifiable",
    "risk_tier": "minimal",
    "vulnerability_tags": [],
    "context": "treatment",
    "format": "text",
    "authority_citations": ["45 CFR 164.514(b)(2)(i)"],
    "metadata": {},
}


@pytest.fixture
def corpus_dir(tmp_path):
    """Create a minimal corpus directory with one JSONL file."""
    jsonl = tmp_path / "test_records.jsonl"
    jsonl.write_text(json.dumps(SAMPLE_RECORD) + "\n")
    return tmp_path


# ---------------------------------------------------------------------------
# SpaCy adapter (always installed via requirements.txt)
# ---------------------------------------------------------------------------

class TestSpaCyAdapter:

    def setup_method(self):
        from benchmarks.spacy_adapter import SpaCyAdapter
        self.adapter = SpaCyAdapter()

    def test_instantiates(self):
        assert hasattr(self.adapter, "_available")

    def test_available(self):
        # spaCy is in requirements.txt; it must be available
        assert self.adapter._available, "spaCy must be available (it is in requirements.txt)"

    def test_analyze_text_returns_list(self):
        spans = self.adapter.analyze_text(SAMPLE_TEXT)
        assert isinstance(spans, list)

    def test_analyze_text_finds_person(self):
        spans = self.adapter.analyze_text(SAMPLE_TEXT)
        entity_types = [s.entity_type for s in spans]
        # spaCy en_core_web_sm should detect PERSON in this text
        assert "PERSON" in entity_types, f"PERSON not found; got {entity_types}"

    def test_run_all_returns_benchmark_result(self, corpus_dir):
        result = self.adapter.run_all(corpus_dir)
        assert isinstance(result, BenchmarkResult)
        assert result.tool_name.startswith("spacy")

    def test_run_all_sets_total_records(self, corpus_dir):
        result = self.adapter.run_all(corpus_dir)
        assert result.total_records == 1

    def test_run_all_empty_dir_returns_zero_records(self, tmp_path):
        result = self.adapter.run_all(tmp_path)
        assert result.total_records == 0

    def test_write_results_creates_file(self, corpus_dir, tmp_path):
        result = self.adapter.run_all(corpus_dir)
        self.adapter.write_results(result, tmp_path)
        assert (tmp_path / "spacy_benchmark_result.json").exists()


# ---------------------------------------------------------------------------
# Philter adapter (may not be installed -- interface contract only)
# ---------------------------------------------------------------------------

class TestPhilterAdapter:

    def setup_method(self):
        from benchmarks.philter_adapter import PhilterAdapter
        self.adapter = PhilterAdapter()

    def test_instantiates(self):
        assert hasattr(self.adapter, "_available")

    def test_run_all_returns_benchmark_result_always(self, corpus_dir):
        result = self.adapter.run_all(corpus_dir)
        assert isinstance(result, BenchmarkResult)

    def test_tool_name_reflects_availability(self, corpus_dir):
        result = self.adapter.run_all(corpus_dir)
        assert "philter" in result.tool_name.lower()

    def test_write_results_creates_file(self, corpus_dir, tmp_path):
        result = self.adapter.run_all(corpus_dir)
        self.adapter.write_results(result, tmp_path)
        assert (tmp_path / "philter_benchmark_result.json").exists()


# ---------------------------------------------------------------------------
# CliniDeID adapter
# ---------------------------------------------------------------------------

class TestCliniDeIDAdapter:

    def setup_method(self):
        from benchmarks.clinideid_adapter import CliniDeIDAdapter
        self.adapter = CliniDeIDAdapter()

    def test_instantiates(self):
        assert hasattr(self.adapter, "_available")

    def test_run_all_returns_benchmark_result_always(self, corpus_dir):
        result = self.adapter.run_all(corpus_dir)
        assert isinstance(result, BenchmarkResult)

    def test_tool_name_reflects_availability(self, corpus_dir):
        result = self.adapter.run_all(corpus_dir)
        assert "clinideid" in result.tool_name.lower()

    def test_write_results_creates_file(self, corpus_dir, tmp_path):
        result = self.adapter.run_all(corpus_dir)
        self.adapter.write_results(result, tmp_path)
        assert (tmp_path / "clinideid_benchmark_result.json").exists()


# ---------------------------------------------------------------------------
# PyDeID adapter
# ---------------------------------------------------------------------------

class TestPyDeIDAdapter:

    def setup_method(self):
        from benchmarks.pydeid_adapter import PyDeIDAdapter
        self.adapter = PyDeIDAdapter()

    def test_instantiates(self):
        assert hasattr(self.adapter, "_available")

    def test_run_all_returns_benchmark_result_always(self, corpus_dir):
        result = self.adapter.run_all(corpus_dir)
        assert isinstance(result, BenchmarkResult)

    def test_tool_name_reflects_availability(self, corpus_dir):
        result = self.adapter.run_all(corpus_dir)
        assert "pydeid" in result.tool_name.lower()

    def test_write_results_creates_file(self, corpus_dir, tmp_path):
        result = self.adapter.run_all(corpus_dir)
        self.adapter.write_results(result, tmp_path)
        assert (tmp_path / "pydeid_benchmark_result.json").exists()


# ---------------------------------------------------------------------------
# PhysioNet deid adapter
# ---------------------------------------------------------------------------

class TestPhysioNetDeIDAdapter:

    def setup_method(self):
        from benchmarks.physionet_adapter import PhysioNetDeIDAdapter
        self.adapter = PhysioNetDeIDAdapter()

    def test_instantiates(self):
        assert hasattr(self.adapter, "_available")

    def test_run_all_returns_benchmark_result_always(self, corpus_dir):
        result = self.adapter.run_all(corpus_dir)
        assert isinstance(result, BenchmarkResult)

    def test_tool_name_reflects_availability(self, corpus_dir):
        result = self.adapter.run_all(corpus_dir)
        assert "physionet" in result.tool_name.lower()

    def test_write_results_creates_file(self, corpus_dir, tmp_path):
        result = self.adapter.run_all(corpus_dir)
        self.adapter.write_results(result, tmp_path)
        assert (tmp_path / "physionet_benchmark_result.json").exists()
