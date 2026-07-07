from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from harness.generate_corpus import build_seeded_corpus, main, seeded_generator_specs


def test_seeded_generator_specs_contains_required_specs():
    spec_ids = {spec.id for spec in seeded_generator_specs()}

    assert "us/hipaa_safe_harbor" in spec_ids
    assert "in/india_dpdpa" in spec_ids
    assert "eu/eu_identifiers" in spec_ids
    assert "file_formats/dicom_headers" in spec_ids
    assert "file_formats/xlsx_phi_corpus" in spec_ids


def test_build_seeded_corpus_india_writes_registry_outputs(tmp_path):
    summaries = build_seeded_corpus(seed=42, out_dir=tmp_path, jurisdiction="in")

    assert (tmp_path / "in" / "india_dpdpa.jsonl").exists()
    assert (tmp_path / "in" / "india_identifiers.jsonl").exists()
    assert set(summaries) == {"in"}
    for name, summary in summaries["in"].items():
        if name == "__total__":
            continue
        assert summary["span_errors"] == []


def test_build_seeded_corpus_file_formats_writes_jsonl_outputs(tmp_path):
    summaries = build_seeded_corpus(seed=42, out_dir=tmp_path, jurisdiction="file_formats")

    expected = {
        "dicom_headers",
        "fhir_bundles",
        "hl7v2_messages",
        "eml_messages",
        "xlsx_phi_corpus",
    }
    assert expected.issubset(set(summaries["file_formats"]) - {"__total__"})
    for name in expected:
        assert (tmp_path / "file_formats" / f"{name}.jsonl").exists()
        assert summaries["file_formats"][name]["span_errors"] == []


def test_main_all_creates_v2_manifest_with_registry_jurisdictions(tmp_path):
    exit_code = main(["--seed", "42", "--jurisdiction", "all", "--out-dir", str(tmp_path)])

    assert exit_code == 0
    manifest = json.loads((tmp_path / "MANIFEST.json").read_text())
    assert manifest["version"] == "2.0.0-dev"
    assert set(manifest["jurisdictions"]) == {"us", "in", "eu", "br", "au", "ug", "file_formats"}
    assert manifest["jurisdictions"] == sorted(manifest["jurisdictions"])
    assert manifest["validation_status"] == "PASS"
    assert manifest["claim_level"] == "L2-partial"
    assert len(manifest["capability_registry_sha256"]) == 64
    assert manifest["generated_at_utc"].endswith("Z")
    assert "ca" not in manifest["jurisdictions"]
    assert "uk" not in manifest["jurisdictions"]
    assert "sg" not in manifest["jurisdictions"]
    assert "jp" not in manifest["jurisdictions"]
    assert "cn" not in manifest["jurisdictions"]


def test_unknown_jurisdiction_exits_nonzero(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "harness.generate_corpus",
            "--seed",
            "42",
            "--jurisdiction",
            "ca",
            "--out-dir",
            str(tmp_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "Unsupported jurisdiction: ca" in result.stderr
