from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from harness.capability_registry import load_capabilities


def _git_sha() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).resolve().parent,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return result.stdout.strip() or None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object: {path}")
    return data


def _planned_limitations() -> list[str]:
    limitations: list[str] = []
    for capability in load_capabilities():
        if capability.status != "planned":
            continue
        if capability.limitations:
            for limitation in capability.limitations:
                limitations.append(f"{capability.id}: {limitation}")
        else:
            limitations.append(f"{capability.id}: {capability.public_claim}")
    return sorted(limitations)


# MANIFEST.json's "jurisdictions" list carries "file_formats" alongside real
# regulatory jurisdictions (it is the top-level corpus/ directory name for the
# file-format generators, not a jurisdiction). It must never count toward the
# multi-jurisdiction claim boundary below, or a single-jurisdiction (USA-only)
# corpus that merely also generated file-format output would be misreported
# as having broader regulatory-jurisdiction evidence than it actually has.
_NON_JURISDICTION_MANIFEST_CATEGORIES = frozenset({"file_formats"})


def _claim_level(manifest: dict[str, Any], validation_report: dict[str, Any]) -> tuple[str, str]:
    """Return (claim_level, basis) -- basis is a self-documenting explanation of
    the level so a reviewer never has to cross-reference README's Claim Level
    table just to know what triggered it."""
    validation_passes = validation_report.get("validation_status") == "PASS"
    if not validation_passes:
        return "L1", "validation_status is not PASS"
    jurisdictions = manifest.get("jurisdictions", [])
    if isinstance(jurisdictions, list):
        manifested_jurisdictions = {str(jurisdiction) for jurisdiction in jurisdictions}
    else:
        manifested_jurisdictions = set()
    real_jurisdictions = manifested_jurisdictions - _NON_JURISDICTION_MANIFEST_CATEGORIES
    extra_jurisdictions = sorted(real_jurisdictions - {"us"})
    if extra_jurisdictions:
        return (
            "L2-partial",
            f"manifest declares jurisdiction(s) beyond us: {extra_jurisdictions}",
        )
    return "L1", "us-only manifested jurisdiction evidence"


def build_release_evidence(
    *,
    corpus_dir: Path,
    manifest_path: Path,
    validation_report_path: Path,
    mia_report_path: Path | None = None,
) -> dict[str, Any]:
    # corpus_dir is part of the public API and CLI contract; artifact hashes are
    # computed from the explicit manifest and report paths.
    _ = corpus_dir
    manifest = _read_json(manifest_path)
    validation_report = _read_json(validation_report_path)

    from datetime import datetime, timezone

    claim_level, claim_level_basis = _claim_level(manifest, validation_report)
    return {
        "manifest_sha256": _sha256(manifest_path),
        "validation_report_sha256": _sha256(validation_report_path),
        "mia_report_sha256": _sha256(mia_report_path) if mia_report_path is not None else None,
        "claim_level": claim_level,
        "claim_level_basis": claim_level_basis,
        "limitations": _planned_limitations(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": _git_sha(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build release evidence hash manifest.")
    parser.add_argument("--corpus-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--validation-report", type=Path, required=True)
    parser.add_argument("--mia-report", type=Path, default=None)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--allow-failed-validation",
        action="store_true",
        help="Write evidence even if the validation report did not PASS (not recommended for release).",
    )
    args = parser.parse_args(argv)

    validation_report = _read_json(args.validation_report)
    if validation_report.get("validation_status") != "PASS" and not args.allow_failed_validation:
        print(
            f"Refusing to build release evidence: validation_report status is "
            f"{validation_report.get('validation_status')!r}, not 'PASS'. "
            f"Pass --allow-failed-validation to override.",
        )
        return 1

    evidence = build_release_evidence(
        corpus_dir=args.corpus_dir,
        manifest_path=args.manifest,
        validation_report_path=args.validation_report,
        mia_report_path=args.mia_report,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
