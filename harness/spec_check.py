"""Recurring spec-conformance checker for the standalone PHI refactor.

Runs, in order, and writes a per-check pass/fail report:

    1. ``pytest`` -- the full test suite (subprocess exit code).
    2. Intake invariant -- every entry under ``<workspace>/intake/<study>/``
       (when present) is a symlink; zero regular files except
       ``intake_manifest.json``.
    3. LLM-boundary canary -- ``config.yaml``'s ``llm.provider`` defaults to
       ``none``, plus a grep-based assertion that no module under
       ``phi_engine/pipeline/`` calls ``get_llm_client`` outside the
       ``llm_detector``/``phi_alignment`` exemption.
    4. Source-immutability -- for a stress-fixture source tree (when
       present), recompute sha256 of every source file and compare against
       the manifest recorded at fixture-build time.

Usage::

    .venv/bin/python -m harness.spec_check [--workspace W] [--study S]

Exit code is 0 iff every check passed (subprocess return codes/booleans are
recorded verbatim in the JSON report either way -- this module never masks a
failure, it only aggregates).
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = REPO_ROOT / "tmp" / "spec_check_report.json"

# Modules allowed to call ``get_llm_client`` directly -- the two structural
# LLM-boundary chokepoints (header classification, opt-in AI rule alignment).
# Both construct headers-only prompts; see llm_detector.py:49-58 and
# phi_alignment.py's prompt builder.
_LLM_CLIENT_EXEMPT_STEMS = frozenset({"llm_detector", "phi_alignment"})


def _resolve_report_path(workspace: Path | None) -> Path:
    """Resolve where the spec-check report should be written.

    A supplied ``workspace`` keeps the report workspace-local
    (``workspace/tmp/spec_check_report.json``) so a workspace-scoped run
    never recreates the repository-root ``tmp/`` tree. With no workspace,
    fall back to the legacy ``REPO_ROOT/tmp/spec_check_report.json``
    location for backward compatibility.
    """
    if workspace is not None:
        return Path(workspace) / "tmp" / "spec_check_report.json"
    return REPORT_PATH


def _check_pytest(*, extra_args: tuple[str, ...] = ()) -> dict[str, Any]:
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests/",
        "-q",
        "-x",
        "--timeout=600",
        *extra_args,
    ]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    tail = "\n".join(proc.stdout.splitlines()[-40:])
    return {
        "check": "pytest",
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "command": cmd,
        "output_tail": tail,
    }


def _check_intake_invariant(workspace: Path | None, study: str | None) -> dict[str, Any]:
    check: dict[str, Any] = {"check": "intake_symlink_invariant", "ok": True, "violations": []}
    if workspace is None:
        check["skipped"] = "no --workspace supplied"
        return check
    intake_root = Path(workspace) / "intake"
    if not intake_root.is_dir():
        check["skipped"] = f"{intake_root} does not exist"
        return check

    study_dirs = [intake_root / study] if study else sorted(
        p for p in intake_root.iterdir() if p.is_dir()
    )
    violations: list[str] = []
    for study_dir in study_dirs:
        if not study_dir.is_dir():
            continue
        for entry in study_dir.rglob("*"):
            if entry.is_dir():
                continue
            if entry.name == "intake_manifest.json":
                if entry.is_symlink():
                    violations.append(f"{entry}: manifest must be a regular file, found symlink")
                continue
            if not entry.is_symlink():
                violations.append(f"{entry}: regular file under intake/ (must be a symlink)")
    check["ok"] = not violations
    check["violations"] = violations
    check["studies_checked"] = [str(p) for p in study_dirs]
    return check


def _check_llm_boundary() -> dict[str, Any]:
    check: dict[str, Any] = {"check": "llm_boundary_canary", "ok": True, "violations": []}

    import phi_engine.config.config as config  # local import -- avoid import cost when unused

    provider_default = config.yaml_get("llm", "provider", default=None)
    if provider_default != "none":
        check["ok"] = False
        check["violations"].append(
            f"config.yaml llm.provider default is {provider_default!r}, expected 'none'"
        )
    check["llm_provider_default"] = provider_default

    pipeline_dir = REPO_ROOT / "phi_engine" / "pipeline"
    offenders: list[str] = []
    if pipeline_dir.is_dir():
        for py_file in sorted(pipeline_dir.rglob("*.py")):
            if py_file.stem in _LLM_CLIENT_EXEMPT_STEMS:
                continue
            text = py_file.read_text(encoding="utf-8")
            try:
                tree = ast.parse(text, filename=str(py_file))
            except SyntaxError:
                offenders.append(str(py_file.relative_to(REPO_ROOT)) + " (unparseable)")
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "get_llm_client"
                ) or (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get_llm_client"
                ):
                    offenders.append(str(py_file.relative_to(REPO_ROOT)))
                    break
    if offenders:
        check["ok"] = False
        check["violations"].extend(
            f"{path}: calls get_llm_client() outside the llm_detector/phi_alignment exemption"
            for path in offenders
        )
    check["pipeline_dir_scanned"] = pipeline_dir.is_dir()
    return check


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_source_immutability(source_manifest: Path | None) -> dict[str, Any]:
    check: dict[str, Any] = {"check": "source_immutability", "ok": True, "violations": []}
    if source_manifest is None:
        check["skipped"] = "no --source-manifest supplied"
        return check
    if not source_manifest.is_file():
        check["skipped"] = f"{source_manifest} not present (fixture not built this run)"
        return check
    manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    violations: list[str] = []
    for rel_path, expected_sha in manifest.get("files", {}).items():
        abs_path = Path(manifest["source_root"]) / rel_path
        if not abs_path.is_file():
            violations.append(f"{rel_path}: source file vanished")
            continue
        actual_sha = _sha256_file(abs_path)
        if actual_sha != expected_sha:
            violations.append(f"{rel_path}: sha256 drift ({expected_sha} -> {actual_sha})")
    check["ok"] = not violations
    check["violations"] = violations
    check["files_checked"] = len(manifest.get("files", {}))
    return check


def run_spec_check(
    *,
    workspace: Path | None = None,
    study: str | None = None,
    skip_pytest: bool = False,
    source_manifest: Path | None = None,
) -> dict[str, Any]:
    checks = []
    if not skip_pytest:
        checks.append(_check_pytest())
    checks.append(_check_intake_invariant(workspace, study))
    checks.append(_check_llm_boundary())
    checks.append(_check_source_immutability(source_manifest))

    report = {
        "workspace": str(workspace) if workspace else None,
        "study": study,
        "checks": checks,
        "all_pass": all(c["ok"] for c in checks),
    }
    report_path = _resolve_report_path(workspace)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=None, help="PHI_WORKSPACE root to check")
    parser.add_argument("--study", default=None, help="Restrict intake check to one study")
    parser.add_argument(
        "--skip-pytest",
        action="store_true",
        help="Skip the pytest subprocess leg (for fast iterative use)",
    )
    parser.add_argument(
        "--source-manifest",
        default=None,
        help="Path to a stress-fixture stress_manifest.json (harness.make_stress_fixtures output)",
    )
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve() if args.workspace else None
    source_manifest = Path(args.source_manifest).resolve() if args.source_manifest else None
    report = run_spec_check(
        workspace=workspace, study=args.study, skip_pytest=args.skip_pytest,
        source_manifest=source_manifest,
    )

    for c in report["checks"]:
        status = "PASS" if c["ok"] else "FAIL"
        note = f" ({c['skipped']})" if c.get("skipped") else ""
        print(f"[{status}] {c['check']}{note}")
        for v in c.get("violations", []):
            print(f"    - {v}")

    print(f"\nReport written to {_resolve_report_path(workspace)}")
    print("ALL PASS" if report["all_pass"] else "FAILURES PRESENT")
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
