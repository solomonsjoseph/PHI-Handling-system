from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from harness.release_evidence import build_release_evidence


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return path


def test_build_release_evidence_hashes_artifacts_and_l2_claim(tmp_path):
    manifest_path = _write_json(
        tmp_path / "MANIFEST.json",
        {"jurisdictions": ["in", "us"], "validation_status": "PASS"},
    )
    validation_path = _write_json(tmp_path / "validation.json", {"validation_status": "PASS"})
    mia_path = _write_json(
        tmp_path / "mia.json",
        {"ok": True, "attack_auc": 0.5, "threshold": 0.6, "records": 20, "features": [], "note": "ok"},
    )

    evidence = build_release_evidence(
        corpus_dir=tmp_path,
        manifest_path=manifest_path,
        validation_report_path=validation_path,
        mia_report_path=mia_path,
    )

    assert evidence["manifest_sha256"] == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert evidence["validation_report_sha256"] == hashlib.sha256(validation_path.read_bytes()).hexdigest()
    assert evidence["mia_report_sha256"] == hashlib.sha256(mia_path.read_bytes()).hexdigest()
    assert evidence["claim_level"] == "L2-partial"
    assert any("canada_pipeda" in limitation for limitation in evidence["limitations"])
    assert not any("mia_framework" in limitation for limitation in evidence["limitations"])


def test_build_release_evidence_without_mia_and_us_only_is_l1(tmp_path):
    manifest_path = _write_json(tmp_path / "MANIFEST.json", {"jurisdictions": ["us"]})
    validation_path = _write_json(tmp_path / "validation.json", {"validation_status": "PASS"})

    evidence = build_release_evidence(
        corpus_dir=tmp_path,
        manifest_path=manifest_path,
        validation_report_path=validation_path,
        mia_report_path=None,
    )

    assert evidence["mia_report_sha256"] is None
    assert evidence["claim_level"] == "L1"


def test_build_release_evidence_failed_validation_is_l1(tmp_path):
    manifest_path = _write_json(tmp_path / "MANIFEST.json", {"jurisdictions": ["in", "us"]})
    validation_path = _write_json(tmp_path / "validation.json", {"validation_status": "FAIL"})

    evidence = build_release_evidence(
        corpus_dir=tmp_path,
        manifest_path=manifest_path,
        validation_report_path=validation_path,
    )

    assert evidence["claim_level"] == "L1"


def test_release_evidence_cli_writes_json(tmp_path):
    manifest_path = _write_json(tmp_path / "MANIFEST.json", {"jurisdictions": ["in", "us"]})
    validation_path = _write_json(tmp_path / "validation.json", {"validation_status": "PASS"})
    output_path = tmp_path / "evidence.json"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "harness.release_evidence",
            "--corpus-dir",
            str(tmp_path),
            "--manifest",
            str(manifest_path),
            "--validation-report",
            str(validation_path),
            "--output",
            str(output_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    payload = json.loads(output_path.read_text())
    assert {"manifest_sha256", "validation_report_sha256", "mia_report_sha256", "claim_level", "limitations"}.issubset(payload)


def test_release_evidence_cli_refuses_on_failed_validation(tmp_path):
    manifest_path = _write_json(tmp_path / "MANIFEST.json", {"jurisdictions": ["in", "us"]})
    validation_path = _write_json(tmp_path / "validation.json", {"validation_status": "FAIL"})
    output_path = tmp_path / "evidence.json"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "harness.release_evidence",
            "--corpus-dir",
            str(tmp_path),
            "--manifest",
            str(manifest_path),
            "--validation-report",
            str(validation_path),
            "--output",
            str(output_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert not output_path.exists()


def test_release_evidence_cli_allow_failed_validation_override(tmp_path):
    manifest_path = _write_json(tmp_path / "MANIFEST.json", {"jurisdictions": ["in", "us"]})
    validation_path = _write_json(tmp_path / "validation.json", {"validation_status": "FAIL"})
    output_path = tmp_path / "evidence.json"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "harness.release_evidence",
            "--corpus-dir",
            str(tmp_path),
            "--manifest",
            str(manifest_path),
            "--validation-report",
            str(validation_path),
            "--output",
            str(output_path),
            "--allow-failed-validation",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert output_path.exists()
