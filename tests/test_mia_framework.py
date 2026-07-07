from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from harness.mia_framework import MIAResult, run_membership_smoke


def _record(index: int) -> dict:
    text = f"Synthetic record {index:03d} for TEST patient ID {1000 + index}."
    value = str(1000 + index)
    start = text.index(value)
    return {
        "record_id": f"rec-{index:03d}",
        "text": text,
        "gold_spans": [
            {
                "start": start,
                "end": start + len(value),
                "value": value,
                "entity_type": "SYNTHETIC_ID" if index % 2 else "NAME",
                "detection_regime": "rule_applicable",
            }
        ],
        "authority_citations": ["Synthetic fixture authority"],
        "jurisdiction": "us",
        "format": "text" if index % 3 else "jsonl",
    }


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record, sort_keys=True) + "\n" for record in records))


def test_empty_directory_returns_insufficient_records_ok(tmp_path):
    result = run_membership_smoke(tmp_path)

    assert result.ok is True
    assert result.attack_auc == 0.5
    assert result.records == 0
    assert result.note == "insufficient records for smoke attack"


def test_generated_mini_corpus_returns_auc_and_features(tmp_path):
    _write_jsonl(tmp_path / "us" / "mini.jsonl", [_record(index) for index in range(20)])

    result = run_membership_smoke(tmp_path)

    assert isinstance(result, MIAResult)
    assert result.records == 20
    assert result.features == (
        "text_length",
        "gold_span_count",
        "unique_entity_type_count",
        "authority_citations_count",
        "text_digit_count",
        "text_uppercase_count",
        "format_hash_bucket_mod_8",
    )
    assert 0.0 <= result.attack_auc <= 1.0


def test_cli_writes_json_keys_without_raw_text(tmp_path):
    records = [_record(index) for index in range(20)]
    _write_jsonl(tmp_path / "us" / "mini.jsonl", records)
    output_path = tmp_path / "mia.json"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "harness.mia_framework",
            "--corpus-dir",
            str(tmp_path),
            "--output",
            str(output_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.stdout == ""
    payload = json.loads(output_path.read_text())
    assert set(payload) == {"ok", "attack_auc", "threshold", "records", "features", "note"}
    # Everything here is seeded (random_state=42 throughout), so the exit code
    # is not a coin flip -- it must match the "ok" field exactly.
    assert result.returncode == (0 if payload["ok"] else 1)
    serialized = json.dumps(payload)
    assert "Synthetic record" not in serialized
    assert "TEST patient" not in serialized
