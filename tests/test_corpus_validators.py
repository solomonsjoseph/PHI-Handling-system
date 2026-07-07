from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from harness.generate_corpus import build_manifest, build_seeded_corpus
from harness.run_all_validations import run_validations
from validators.citation_validator import validate as validate_citations
from validators.hash_validator import validate as validate_hashes
from validators.jurisdiction_separator import validate as validate_jurisdictions
from validators.offset_validator import validate as validate_offsets


def _record(**overrides):
    text = overrides.pop("text", "Patient Alpha has ID A123.")
    base = {
        "record_id": "rec-001",
        "text": text,
        "gold_spans": [
            {
                "start": 8,
                "end": 13,
                "value": "Alpha",
                "entity_type": "NAME",
                "detection_regime": "contextual_ner_required",
                "authority": "Synthetic authority",
            }
        ],
        "jurisdiction": "us",
        "format": "text",
        "authority_citations": ["Synthetic authority"],
    }
    base.update(overrides)
    return base


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def _write_manifest(corpus_dir: Path, path: Path, sha256: str | None = None) -> Path:
    manifest_path = corpus_dir / "MANIFEST.json"
    digest = sha256 if sha256 is not None else hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        "files": {
            "us/bad": {
                "path": str(path),
                "records": 1,
                "spans": 1,
                "sha256": digest,
            }
        }
    }
    manifest_path.write_text(json.dumps(manifest))
    return manifest_path


def test_offset_mismatch_is_detected(tmp_path):
    path = tmp_path / "us" / "bad.jsonl"
    record = _record(gold_spans=[{"start": 8, "end": 13, "value": "Bravo", "entity_type": "NAME", "detection_regime": "contextual_ner_required"}])
    _write_jsonl(path, [record])

    result = validate_offsets(tmp_path)

    assert not result.ok
    assert result.issues[0].code == "OFFSET_MISMATCH"
    assert "Bravo" not in result.issues[0].message


def test_hash_mismatch_is_detected(tmp_path):
    path = tmp_path / "us" / "bad.jsonl"
    _write_jsonl(path, [_record()])
    manifest_path = _write_manifest(tmp_path, path, sha256="0" * 64)

    result = validate_hashes(tmp_path, manifest_path)

    assert not result.ok
    assert result.issues[0].code == "HASH_MISMATCH"


def test_missing_authority_is_detected(tmp_path):
    path = tmp_path / "us" / "bad.jsonl"
    record = _record(authority_citations=[], gold_spans=[{"start": 8, "end": 13, "value": "Alpha", "entity_type": "NAME", "detection_regime": "contextual_ner_required", "authority": ""}])
    _write_jsonl(path, [record])

    result = validate_citations(tmp_path)

    assert not result.ok
    assert {issue.code for issue in result.issues} == {"MISSING_AUTHORITY"}


def test_bad_jurisdiction_folder_is_detected(tmp_path):
    path = tmp_path / "in" / "bad.jsonl"
    _write_jsonl(path, [_record(jurisdiction="us")])

    result = validate_jurisdictions(tmp_path)

    assert not result.ok
    assert result.issues[0].code == "JURISDICTION_MISMATCH"


def test_valid_generated_india_corpus_passes_all_validators(tmp_path):
    summaries = build_seeded_corpus(seed=42, out_dir=tmp_path, jurisdiction="in")
    build_manifest(seed=42, summaries=summaries, out_dir=tmp_path)

    report = run_validations(tmp_path, tmp_path / "MANIFEST.json")

    assert report["validation_status"] == "PASS"
    assert all(result["ok"] for result in report["validators"].values())


def test_run_all_validations_cli_fails_bad_corpus_and_writes_output(tmp_path):
    path = tmp_path / "in" / "bad.jsonl"
    raw_secret = "gmail.com"
    record = _record(
        text=f"Patient Alpha uses {raw_secret} and is marked actual patient.",
        jurisdiction="us",
        authority_citations=[],
        gold_spans=[{"start": 8, "end": 13, "value": "Bravo", "entity_type": "NAME", "detection_regime": "contextual_ner_required", "authority": ""}],
    )
    _write_jsonl(path, [record])
    manifest_path = _write_manifest(tmp_path, path, sha256="1" * 64)
    output_path = tmp_path / "validation.json"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "harness.run_all_validations",
            "--corpus-dir",
            str(tmp_path),
            "--manifest",
            str(manifest_path),
            "--output",
            str(output_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert output_path.exists()
    report = json.loads(output_path.read_text())
    assert report["validation_status"] == "FAIL"
    serialized_messages = json.dumps(report["validators"])
    assert raw_secret not in serialized_messages
    assert "Bravo" not in serialized_messages
    assert "actual patient" not in serialized_messages.lower()
