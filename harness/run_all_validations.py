from __future__ import annotations

import argparse
import importlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from validators.common import ValidationResult

VALIDATORS = (
    "offset_validator",
    "hash_validator",
    "taxonomy_validator",
    "citation_validator",
    "jurisdiction_separator",
    "format_parse_validator",
    "no_real_phi_static_validator",
)


def _result_dict(result: ValidationResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "issues": [asdict(issue) for issue in result.issues],
    }


def run_validations(corpus_dir: Path, manifest_path: Path | None) -> dict[str, Any]:
    validators: dict[str, Any] = {}
    validation_status = "PASS"
    for validator_name in VALIDATORS:
        module = importlib.import_module(f"validators.{validator_name}")
        result = module.validate(corpus_dir, manifest_path)
        validators[validator_name] = _result_dict(result)
        if not result.ok:
            validation_status = "FAIL"
    return {
        "validation_status": validation_status,
        "corpus_dir": str(corpus_dir),
        "manifest": str(manifest_path) if manifest_path is not None else "",
        "validators": validators,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run all corpus validators")
    parser.add_argument("--corpus-dir", type=Path, default=Path("corpus"))
    parser.add_argument("--manifest", type=Path, default=Path("corpus/MANIFEST.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    report = run_validations(args.corpus_dir, args.manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["validation_status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
