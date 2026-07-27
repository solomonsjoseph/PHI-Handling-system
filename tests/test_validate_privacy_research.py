from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.validate_privacy_research import (
    main,
    validate_candidates,
    validate_dispositions,
    validate_evidence,
    validate_report,
)


def _evidence_row(**overrides) -> dict:
    row = {
        "claim_id": "reg-0001",
        "claim_text": "45 CFR 164.514(b)(2) enumerates 18 Safe Harbor identifier categories.",
        "claim_type": "law",
        "source_title": "45 CFR 164.514",
        "publisher": "US Government",
        "source_url_or_path": "https://www.ecfr.gov/current/title-45/section-164.514",
        "source_version_or_date": "current",
        "pinpoint": "164.514(b)(2)",
        "accessed_at": "2026-07-20",
        "jurisdiction": "US",
        "product_and_version": "",
        "primary_source": True,
        "corroborating_claim_ids": [],
        "status": "confirmed",
        "review_note": "",
    }
    row.update(overrides)
    return row


def _candidate_row(**overrides) -> dict:
    row = {
        "candidate_id": "oss-0001",
        "category": "open_local_phi_pii",
        "vendor": "Microsoft",
        "product": "Presidio",
        "version_or_release": "2.2.x",
        "active_status": "active",
        "cost_model": "open_source_free",
        "license": "MIT",
        "deployment": "self_hosted",
        "supported_data_classes": ["PII"],
        "modalities": ["text"],
        "channels": ["prompt_input"],
        "detect_actions": ["ner", "regex"],
        "transform_actions": ["redact"],
        "custom_policy_support": True,
        "input_data_location": "in-process, never leaves host",
        "retention_and_training": "n/a, self-hosted",
        "baa_dpa_status": "n/a",
        "regions_and_subprocessors": "n/a",
        "encryption_and_key_control": "n/a",
        "private_networking": "unknown",
        "audit_behavior": "no built-in audit log",
        "known_bypasses": [],
        "independent_evidence_claim_ids": ["reg-0001"],
        "vendor_claim_ids": [],
        "benchmark_status": "not_attempted",
        "benchmark_artifact": "",
        "not_run_reason": "",
        "score": None,
    }
    row.update(overrides)
    return row


def _disposition_row(**overrides) -> dict:
    row = {
        "capability": "phi_pii_detection",
        "current_control": "phi_engine regex/checksum pattern catalog",
        "disposition": "integrate",
        "selected_candidate_id": "oss-0001",
        "fallback_candidate_id": "repository",
        "hard_gate_results": {"active_supported_release": True},
        "weighted_score": 62.5,
        "rationale_claim_ids": ["reg-0001"],
    }
    row.update(overrides)
    return row


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _all_capability_dispositions() -> list[dict]:
    from harness.validate_privacy_research import REQUIRED_CAPABILITIES

    return [
        _disposition_row(
            capability=cap,
            selected_candidate_id="repository",
            fallback_candidate_id="repository",
            hard_gate_results={},
            disposition="retain",
            rationale_claim_ids=["reg-0001"],
        )
        for cap in REQUIRED_CAPABILITIES
    ]


def test_validate_evidence_accepts_well_formed_rows(tmp_path):
    path = tmp_path / "evidence.jsonl"
    _write_jsonl(path, [_evidence_row()])
    assert validate_evidence(path) == []


def test_validate_evidence_rejects_duplicate_claim_ids(tmp_path):
    path = tmp_path / "evidence.jsonl"
    _write_jsonl(path, [_evidence_row(), _evidence_row(claim_text="second claim, same id")])
    errors = validate_evidence(path)
    assert any("duplicate claim_id" in e for e in errors)


def test_validate_evidence_rejects_missing_pinpoint(tmp_path):
    path = tmp_path / "evidence.jsonl"
    _write_jsonl(path, [_evidence_row(pinpoint="")])
    errors = validate_evidence(path)
    assert any("pinpoint" in e for e in errors)


def test_validate_evidence_rejects_missing_accessed_at(tmp_path):
    path = tmp_path / "evidence.jsonl"
    _write_jsonl(path, [_evidence_row(accessed_at="")])
    errors = validate_evidence(path)
    assert any("accessed_at" in e for e in errors)


def test_validate_evidence_rejects_confirmed_law_without_primary_source(tmp_path):
    path = tmp_path / "evidence.jsonl"
    _write_jsonl(path, [_evidence_row(primary_source=False)])
    errors = validate_evidence(path)
    assert any("primary_source=true" in e for e in errors)


def test_validate_evidence_rejects_confirmed_vendor_claim(tmp_path):
    path = tmp_path / "evidence.jsonl"
    _write_jsonl(path, [_evidence_row(
        claim_id="gw-0001", claim_type="vendor_claim", status="confirmed",
        source_title="Vendor blog", publisher="Vendor Inc",
        source_url_or_path="https://vendor.example/blog", pinpoint="para 3",
    )])
    errors = validate_evidence(path)
    assert any("performance/capability marked confirmed from vendor evidence alone" in e for e in errors)


def test_validate_evidence_rejects_dangling_corroboration(tmp_path):
    path = tmp_path / "evidence.jsonl"
    _write_jsonl(path, [_evidence_row(corroborating_claim_ids=["does-not-exist"])])
    errors = validate_evidence(path)
    assert any("unknown claim_id" in e for e in errors)


def test_validate_candidates_accepts_well_formed_rows(tmp_path):
    evidence_path = tmp_path / "evidence.jsonl"
    candidates_path = tmp_path / "candidates.jsonl"
    _write_jsonl(evidence_path, [_evidence_row()])
    _write_jsonl(candidates_path, [_candidate_row()])
    assert validate_candidates(candidates_path, evidence_path) == []


def test_validate_candidates_rejects_unknown_claim_reference(tmp_path):
    evidence_path = tmp_path / "evidence.jsonl"
    candidates_path = tmp_path / "candidates.jsonl"
    _write_jsonl(evidence_path, [_evidence_row()])
    _write_jsonl(candidates_path, [_candidate_row(independent_evidence_claim_ids=["ghost-0001"])])
    errors = validate_candidates(candidates_path, evidence_path)
    assert any("unknown claim_id" in e for e in errors)


def test_validate_candidates_requires_not_run_reason(tmp_path):
    evidence_path = tmp_path / "evidence.jsonl"
    candidates_path = tmp_path / "candidates.jsonl"
    _write_jsonl(evidence_path, [_evidence_row()])
    _write_jsonl(candidates_path, [_candidate_row(benchmark_status="not_run", not_run_reason="")])
    errors = validate_candidates(candidates_path, evidence_path)
    assert any("not_run_reason" in e for e in errors)


def test_validate_dispositions_requires_all_15_capabilities(tmp_path):
    evidence_path = tmp_path / "evidence.jsonl"
    candidates_path = tmp_path / "candidates.jsonl"
    dispositions_path = tmp_path / "dispositions.json"
    _write_jsonl(evidence_path, [_evidence_row()])
    _write_jsonl(candidates_path, [_candidate_row()])
    dispositions_path.write_text(json.dumps([_disposition_row()]), encoding="utf-8")
    errors = validate_dispositions(dispositions_path, candidates_path, evidence_path)
    assert any("missing disposition" in e for e in errors)


def test_validate_dispositions_accepts_full_valid_set(tmp_path):
    evidence_path = tmp_path / "evidence.jsonl"
    candidates_path = tmp_path / "candidates.jsonl"
    dispositions_path = tmp_path / "dispositions.json"
    _write_jsonl(evidence_path, [_evidence_row()])
    _write_jsonl(candidates_path, [_candidate_row()])
    dispositions_path.write_text(json.dumps(_all_capability_dispositions()), encoding="utf-8")
    assert validate_dispositions(dispositions_path, candidates_path, evidence_path) == []


def test_validate_dispositions_rejects_same_selected_and_fallback(tmp_path):
    evidence_path = tmp_path / "evidence.jsonl"
    candidates_path = tmp_path / "candidates.jsonl"
    dispositions_path = tmp_path / "dispositions.json"
    _write_jsonl(evidence_path, [_evidence_row()])
    _write_jsonl(candidates_path, [_candidate_row()])
    rows = _all_capability_dispositions()
    rows[0] = _disposition_row(selected_candidate_id="oss-0001", fallback_candidate_id="oss-0001")
    dispositions_path.write_text(json.dumps(rows), encoding="utf-8")
    errors = validate_dispositions(dispositions_path, candidates_path, evidence_path)
    assert any("must differ" in e for e in errors)


def test_validate_report_flags_unresolved_claim_tag(tmp_path):
    evidence_path = tmp_path / "evidence.jsonl"
    report_path = tmp_path / "report.md"
    _write_jsonl(evidence_path, [_evidence_row()])
    report_path.write_text("Finding cites `reg-0001` and also `reg-9999`.\n", encoding="utf-8")
    errors = validate_report(report_path, evidence_path)
    assert any("reg-9999" in e for e in errors)
    assert not any("reg-0001`" in e and "reg-9999" not in e for e in errors)


def test_validate_report_accepts_only_known_tags(tmp_path):
    evidence_path = tmp_path / "evidence.jsonl"
    report_path = tmp_path / "report.md"
    _write_jsonl(evidence_path, [_evidence_row()])
    report_path.write_text("Finding cites `reg-0001` only.\n", encoding="utf-8")
    assert validate_report(report_path, evidence_path) == []


def test_validate_report_resolves_candidate_id_tags(tmp_path):
    evidence_path = tmp_path / "evidence.jsonl"
    candidates_path = tmp_path / "candidates.jsonl"
    report_path = tmp_path / "report.md"
    _write_jsonl(evidence_path, [_evidence_row()])
    _write_jsonl(candidates_path, [_candidate_row()])
    report_path.write_text("Selected component: `oss-0001`.\n", encoding="utf-8")
    assert validate_report(report_path, evidence_path, candidates_path) == []


def test_validate_report_flags_unresolved_candidate_tag(tmp_path):
    evidence_path = tmp_path / "evidence.jsonl"
    candidates_path = tmp_path / "candidates.jsonl"
    report_path = tmp_path / "report.md"
    _write_jsonl(evidence_path, [_evidence_row()])
    _write_jsonl(candidates_path, [_candidate_row()])
    report_path.write_text("Selected component: `oss-9999`.\n", encoding="utf-8")
    errors = validate_report(report_path, evidence_path, candidates_path)
    assert any("oss-9999" in e for e in errors)


def test_cli_end_to_end_pass(tmp_path, capsys):
    evidence_path = tmp_path / "evidence.jsonl"
    candidates_path = tmp_path / "candidates.jsonl"
    dispositions_path = tmp_path / "dispositions.json"
    report_path = tmp_path / "report.md"
    _write_jsonl(evidence_path, [_evidence_row()])
    _write_jsonl(candidates_path, [_candidate_row()])
    dispositions_path.write_text(json.dumps(_all_capability_dispositions()), encoding="utf-8")
    report_path.write_text("Cites `reg-0001`.\n", encoding="utf-8")

    exit_code = main([
        "--evidence", str(evidence_path),
        "--candidates", str(candidates_path),
        "--dispositions", str(dispositions_path),
        "--report", str(report_path),
    ])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "PASS" in out


def test_cli_end_to_end_fail_on_bad_evidence(tmp_path, capsys):
    evidence_path = tmp_path / "evidence.jsonl"
    candidates_path = tmp_path / "candidates.jsonl"
    dispositions_path = tmp_path / "dispositions.json"
    report_path = tmp_path / "report.md"
    _write_jsonl(evidence_path, [_evidence_row(pinpoint="")])
    _write_jsonl(candidates_path, [_candidate_row()])
    dispositions_path.write_text(json.dumps(_all_capability_dispositions()), encoding="utf-8")
    report_path.write_text("No claims cited.\n", encoding="utf-8")

    exit_code = main([
        "--evidence", str(evidence_path),
        "--candidates", str(candidates_path),
        "--dispositions", str(dispositions_path),
        "--report", str(report_path),
    ])
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL" in out
