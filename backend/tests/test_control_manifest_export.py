"""Tests for Evidence_Manifest.json / Verification_Manifest.json /
Run_Manifest.json / CHECKSUMS.sha256 (Phase 11b, docs #58): plain JSON
exports of the already-typed records, and a real sha256 checksums file.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from phi_core.control.manifest_export import (
    export_evidence_manifest,
    export_run_manifest,
    export_verification_manifest,
    write_checksums,
)
from phi_core.control.records import EvidenceRecord, RunManifest, VerificationResult


def _evidence(**overrides) -> EvidenceRecord:
    base = dict(
        evidence_type="regulation", jurisdiction="us", authority="HHS",
        source="https://hhs.gov/hipaa", title="HIPAA Safe Harbor", publisher="HHS",
        verification_status="VERIFIED",
    )
    base.update(overrides)
    return EvidenceRecord(**base)


def test_export_evidence_manifest_round_trips_every_record(tmp_path: Path):
    records = [_evidence(evidence_id="e1"), _evidence(evidence_id="e2", authority="FDA")]
    path = export_evidence_manifest(records, tmp_path / "Evidence_Manifest.json")

    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert [e["evidence_id"] for e in payload["evidence"]] == ["e1", "e2"]
    assert payload["evidence"][1]["authority"] == "FDA"


def test_export_evidence_manifest_empty_list_still_writes_valid_json(tmp_path: Path):
    path = export_evidence_manifest([], tmp_path / "empty.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {"evidence": []}


def test_export_verification_manifest_round_trips_the_record(tmp_path: Path):
    result = VerificationResult(
        verification_id="v1", run_id="r1", passed=True, manifest_coverage_percent=100,
        failed_checks=[],
    )
    path = export_verification_manifest(result, tmp_path / "Verification_Manifest.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["verification_id"] == "v1"
    assert payload["run_id"] == "r1"
    assert payload["passed"] is True
    assert payload["manifest_coverage_percent"] == 100
    # Round trip back into the typed record.
    assert VerificationResult(**payload) == result


def test_export_run_manifest_carries_docs_63_reproducibility_fields_and_no_raw_values(tmp_path: Path):
    manifest = RunManifest(
        run_id="r1", repository_commit="abc123", application_version="2.0.0",
        workflow_version="1.4", agent_role_versions={"Judge": "3"},
        prompt_template_versions={"judge_prompt": "5"}, model_versions={"openai": "gpt-5"},
        provider_versions={"openai": "2024-01"}, run_privacy_policy_version="p1",
        method_registry_versions={"date_shift": "2"}, validator_versions={"deterministic": "1"},
        transformation_hashes={"date_shift": "sha256:abc"}, feature_flags={"strict_mode": "true"},
        trace_root_hash="root_hash_123",
    )
    path = export_run_manifest(manifest, tmp_path / "Run_Manifest.json")
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["repository_commit"] == "abc123"
    assert payload["application_version"] == "2.0.0"
    assert payload["agent_role_versions"] == {"Judge": "3"}
    assert payload["trace_root_hash"] == "root_hash_123"
    # No field on RunManifest's own schema carries a raw study value (docs #63):
    # every field is version/hash/flag metadata, never patient data.
    assert set(payload) <= {
        "schema_version", "manifest_id", "run_id", "repository_commit", "application_version",
        "workflow_version", "agent_role_versions", "prompt_template_versions", "model_versions",
        "provider_versions", "run_privacy_policy_version", "method_registry_versions",
        "validator_versions", "transformation_hashes", "feature_flags", "trace_root_hash",
        "created_at",
    }
    assert RunManifest(**payload) == manifest


def test_write_checksums_lists_every_real_file_with_matching_sha256(tmp_path: Path):
    file_a = tmp_path / "a.json"
    file_a.write_text('{"x": 1}', encoding="utf-8")
    file_b = tmp_path / "b.xlsx"
    file_b.write_bytes(b"fake xlsx bytes")

    checksums_path = write_checksums([file_a, file_b], tmp_path / "CHECKSUMS.sha256")
    text = checksums_path.read_text(encoding="utf-8")

    expected_a = hashlib.sha256(file_a.read_bytes()).hexdigest()
    expected_b = hashlib.sha256(file_b.read_bytes()).hexdigest()
    assert f"{expected_a}  a.json" in text
    assert f"{expected_b}  b.xlsx" in text
    # Every line accounted for, none extra.
    lines = [ln for ln in text.splitlines() if ln]
    assert len(lines) == 2


def test_write_checksums_empty_input_writes_empty_file(tmp_path: Path):
    path = write_checksums([], tmp_path / "empty.sha256")
    assert path.read_text(encoding="utf-8") == ""
