"""Recurring spec-conformance checker for the standalone PHI refactor.

Runs, in order, and writes a per-check pass/fail report:

    1. ``pytest`` -- the full test suite (subprocess exit code).
    2. Intake invariant -- every entry under ``<workspace>/intake/<study>/``
       (when present) is a symlink; zero regular files except
       ``intake_manifest.json``. The intake root, every study directory,
       and every component directory (``datasets``/``forms``/
       ``dictionary_mapping``/``_unclassified``) are ``lstat``-ed
       and rejected if any of them is itself a symlink.
    3. LLM-boundary canary --
       * ``config.yaml``'s ``llm.provider`` defaults to ``none``;
       * no module under ``phi_engine/pipeline/`` calls ``get_llm_client``
         outside the ``llm_detector``/``phi_alignment`` exemption;
       * ``model_routing.new_offline_local_client()`` -- the sole sanctioned
         local-LLM factory for intake study naming -- is called ONLY from
         inside ``phi_engine/pipeline/intake_naming.py``'s
         ``resolve_intake_study``/``_resolve_intake_study``; any alias
         import of that factory, any call to it from any other pipeline
         callsite, and any direct ``OfflineLocalLLMClient(...)``
         construction under ``phi_engine/pipeline/`` are violations.
    4. Source-immutability -- for a stress-fixture source tree (when
       present), recompute the COMPLETE entry set under the recorded
       ``source_root`` and compare, per entry, type (file/symlink/dir),
       sha256 (files only), size, mode, mtime_ns, uid, gid, and symlink
       target (symlinks only) against the manifest recorded at
       fixture-build time. ``atime`` is deliberately never captured or
       compared (reading a file changes it). A vanished, newly-appeared,
       or drifted entry is a violation.

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
import os
import stat
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

# The sole sanctioned local-only-LLM factory call site: intake_naming.py's
# public wrapper and its private implementation (the actual call lives in a
# nested closure defined inside the private function, which still counts as
# lexically "inside" it).
_OFFLINE_CLIENT_FACTORY = "new_offline_local_client"
_OFFLINE_CLIENT_CLASS = "OfflineLocalLLMClient"
_INTAKE_NAMING_FILENAME = "intake_naming.py"
_INTAKE_NAMING_SANCTIONED_FUNCTIONS = frozenset({"resolve_intake_study", "_resolve_intake_study"})

_INTAKE_COMPONENTS = ("datasets", "forms", "dictionary_mapping", "_unclassified")

# source_immutability comparison fields -- everything except atime.
_IMMUTABILITY_FIELDS = ("type", "mode", "size", "mtime_ns", "uid", "gid", "sha256", "symlink_target")


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


def _lstat_or_none(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except OSError:
        return None


def _check_intake_invariant(workspace: Path | None, study: str | None) -> dict[str, Any]:
    check: dict[str, Any] = {"check": "intake_symlink_invariant", "ok": True, "violations": []}
    if workspace is None:
        check["skipped"] = "no --workspace supplied"
        return check
    intake_root = Path(workspace) / "intake"

    violations: list[str] = []

    def _reject_if_symlinked_dir(path: Path, label: str) -> bool:
        """``lstat``-checks *path*; records a violation if it is a symlink.
        Returns True iff *path* exists as a real (non-symlink) directory."""
        info = _lstat_or_none(path)
        if info is None:
            return False
        if stat.S_ISLNK(info.st_mode):
            violations.append(f"{path}: {label} must not be a symlink")
            return False
        return stat.S_ISDIR(info.st_mode)

    if not _reject_if_symlinked_dir(intake_root, "intake root"):
        check["ok"] = not violations
        check["violations"] = violations
        if not violations:
            check["skipped"] = f"{intake_root} does not exist"
        return check

    study_dirs = [intake_root / study] if study else sorted(
        p for p in intake_root.iterdir() if p.is_symlink() or p.is_dir()
    )
    checked_studies: list[str] = []
    for study_dir in study_dirs:
        if not _reject_if_symlinked_dir(study_dir, "study directory"):
            continue
        checked_studies.append(str(study_dir))

        for component in _INTAKE_COMPONENTS:
            component_dir = study_dir / component
            if _lstat_or_none(component_dir) is not None:
                _reject_if_symlinked_dir(component_dir, "intake component directory")

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
    check["studies_checked"] = checked_studies
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
    check["pipeline_dir_scanned"] = pipeline_dir.is_dir()
    if not pipeline_dir.is_dir():
        return check

    offenders: list[str] = []
    offline_client_violations: list[str] = []
    for py_file in sorted(pipeline_dir.rglob("*.py")):
        text = py_file.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=str(py_file))
        except SyntaxError:
            offenders.append(str(py_file.relative_to(REPO_ROOT)) + " (unparseable)")
            continue

        if py_file.stem not in _LLM_CLIENT_EXEMPT_STEMS:
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

        offline_client_violations.extend(_scan_offline_client_canary(py_file, tree))

    if offenders:
        check["ok"] = False
        check["violations"].extend(
            f"{path}: calls get_llm_client() outside the llm_detector/phi_alignment exemption"
            for path in offenders
        )
    if offline_client_violations:
        check["ok"] = False
        check["violations"].extend(offline_client_violations)
    return check


def _scan_offline_client_canary(py_file: Path, tree: ast.AST) -> list[str]:
    """Static, best-effort AST canary for the local-only-LLM naming boundary.

    Single unified rule: every LOAD reference anywhere in the tree to
    either ``new_offline_local_client`` or ``OfflineLocalLLMClient`` (bare
    name or ``model_routing.<name>`` attribute form) is a violation UNLESS
    that exact AST node is the direct callee of the one sanctioned
    ``new_offline_local_client()`` call lexically inside
    ``intake_naming.py``'s ``resolve_intake_study``/``_resolve_intake_study``
    (including nested closures defined inside it, e.g. the
    client-memoizing inner function). This one rule catches every alias
    shape uniformly -- assignment (``factory = new_offline_local_client``),
    container literals, call arguments, walrus bindings -- without a
    separate abstraction per binding form: only the sanctioned call's own
    callee position is ever exempt. A local-variable/parameter/return type
    ANNOTATION referencing ``OfflineLocalLLMClient`` (e.g.
    ``client: OfflineLocalLLMClient | None``) is excluded -- it is an
    inert type, never a dispatch vector.

    A rebinding `from ...model_routing import new_offline_local_client as
    X` renames the identifier at import time, so the body-level reference
    scan above cannot see it (it only ever sees ``X``, not the original
    name) -- caught separately here by inspecting the import statement
    itself, the one AST shape the reference scan structurally cannot
    observe.
    """
    rel = str(py_file.relative_to(REPO_ROOT))
    is_intake_naming = py_file.name == _INTAKE_NAMING_FILENAME
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("model_routing"):
            for alias in node.names:
                if alias.name in (_OFFLINE_CLIENT_FACTORY, _OFFLINE_CLIENT_CLASS) and alias.asname not in (
                    None,
                    alias.name,
                ):
                    violations.append(
                        f"{rel}:{node.lineno}: aliased import of {alias.name} is not permitted"
                    )

    # The one sanctioned callee: id() of the `func` node of every Call,
    # so a reference elsewhere in the tree can check "am I exactly this
    # call's callee" by identity rather than re-deriving call shape.
    call_by_func_id = {id(call.func): call for call in ast.walk(tree) if isinstance(call, ast.Call)}

    # Inert type-annotation subtrees (AnnAssign targets' annotations,
    # parameter annotations, return annotations) -- excluded wholesale
    # from the reference scan since a type is never a dispatch vector.
    annotation_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and node.annotation is not None:
            annotation_ids.update(id(n) for n in ast.walk(node.annotation))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is not None:
                annotation_ids.update(id(n) for n in ast.walk(node.returns))
            params = [
                *node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                *([node.args.vararg] if node.args.vararg else []),
                *([node.args.kwarg] if node.args.kwarg else []),
            ]
            for param in params:
                if param.annotation is not None:
                    annotation_ids.update(id(n) for n in ast.walk(param.annotation))

    function_stack: list[str] = []

    class _Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            function_stack.append(node.name)
            self.generic_visit(node)
            function_stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

        def _check_reference(self, node: ast.expr, name: str) -> None:
            if id(node) in annotation_ids:
                return
            call = call_by_func_id.get(id(node))
            if (
                call is not None
                and name == _OFFLINE_CLIENT_FACTORY
                and is_intake_naming
                and any(fn in _INTAKE_NAMING_SANCTIONED_FUNCTIONS for fn in function_stack)
            ):
                return  # the one permitted reference: direct callee of the sanctioned call
            violations.append(
                f"{rel}:{node.lineno}: {name} referenced outside the sanctioned "
                "new_offline_local_client() call in intake_naming.resolve_intake_study"
            )

        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, ast.Load) and node.id in (_OFFLINE_CLIENT_FACTORY, _OFFLINE_CLIENT_CLASS):
                self._check_reference(node, node.id)
            self.generic_visit(node)

        def visit_Attribute(self, node: ast.Attribute) -> None:
            if isinstance(node.ctx, ast.Load) and node.attr in (_OFFLINE_CLIENT_FACTORY, _OFFLINE_CLIENT_CLASS):
                self._check_reference(node, node.attr)
            self.generic_visit(node)

    _Visitor().visit(tree)
    return violations


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot_entry(path: Path) -> dict[str, Any]:
    """The same complete, atime-excluding entry snapshot
    ``harness.make_stress_fixtures`` records at fixture-build time."""
    info = path.lstat()
    mode = info.st_mode
    entry: dict[str, Any] = {
        "mode": stat.S_IMODE(mode),
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "uid": info.st_uid,
        "gid": info.st_gid,
        "sha256": None,
        "symlink_target": None,
    }
    if stat.S_ISLNK(mode):
        entry["type"] = "symlink"
        entry["symlink_target"] = os.readlink(path)
    elif stat.S_ISREG(mode):
        entry["type"] = "file"
        entry["sha256"] = _sha256_file(path)
    elif stat.S_ISDIR(mode):
        entry["type"] = "dir"
    else:
        entry["type"] = "other"
    return entry


def _check_source_immutability(source_manifest: Path | None) -> dict[str, Any]:
    check: dict[str, Any] = {"check": "source_immutability", "ok": True, "violations": []}
    if source_manifest is None:
        check["skipped"] = "no --source-manifest supplied"
        return check
    if not source_manifest.is_file():
        check["skipped"] = f"{source_manifest} not present (fixture not built this run)"
        return check
    manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    source_root = Path(manifest["source_root"])
    expected_entries: dict[str, dict[str, Any]] = manifest.get("entries", {})
    violations: list[str] = []

    if not source_root.is_dir():
        violations.append(f"{source_root}: source root vanished")
        check["ok"] = False
        check["violations"] = violations
        return check

    current_paths: set[str] = set()
    for path in sorted(source_root.rglob("*")):
        rel = str(path.relative_to(source_root))
        current_paths.add(rel)
        try:
            actual = _snapshot_entry(path)
        except OSError as exc:
            violations.append(f"{rel}: unreadable during recheck ({type(exc).__name__})")
            continue
        expected = expected_entries.get(rel)
        if expected is None:
            violations.append(f"{rel}: unexpected entry not present at fixture-build time")
            continue
        for field in _IMMUTABILITY_FIELDS:
            if actual.get(field) != expected.get(field):
                violations.append(f"{rel}: {field} drift ({expected.get(field)!r} -> {actual.get(field)!r})")

    for rel in sorted(set(expected_entries) - current_paths):
        violations.append(f"{rel}: source entry vanished")

    check["ok"] = not violations
    check["violations"] = violations
    check["entries_checked"] = len(expected_entries)
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
