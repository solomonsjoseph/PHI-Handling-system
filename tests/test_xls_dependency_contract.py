"""Dependency-pin and import-boundary contract for the legacy ``.xls``
isolation subsystem (Approach 1.9).

Asserts:
  * ``requirements.txt`` pins ``xlrd==2.0.2`` exactly.
  * ``.github/workflows/ci.yml`` runs the XLS-focused suites on Python
    3.11 with explicit ``pandas==2.2.3`` and ``pandas==3.0.5`` matrix
    entries, installed after the normal ``requirements.txt`` install.
  * No production module outside the two isolation modules
    (``phi_engine/pipeline/_xls_worker.py`` and
    ``phi_engine/pipeline/xls_isolation.py``) imports ``xlrd`` or calls
    a ``pandas`` Excel reader with the ``xlrd`` engine.
  * No production module imports ``xlwt`` (a write-only legacy format
    library; only the harness fixture-authoring code uses it).

This test makes no hashed-lock or CVE-clean claim -- only that the one
reviewed parser version is pinned and that the import boundary holds.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ALLOWED_XLRD_MODULES = frozenset(
    {
        _REPO_ROOT / "phi_engine" / "pipeline" / "_xls_worker.py",
        _REPO_ROOT / "phi_engine" / "pipeline" / "xls_isolation.py",
    }
)
# xls_isolation.py imports xlrd only indirectly (never at all, in fact --
# it never touches BIFF bytes itself); it is listed as allowed purely so
# a future defensive import there is not itself flagged as a boundary
# violation. The worker module is the one that actually imports xlrd.

_PRODUCTION_ROOTS = [_REPO_ROOT / "phi_engine"]
# harness/ authors genuine .xls fixtures with xlwt for the stress suite;
# it is fixture-authoring tooling, not production runtime code.
_EXCLUDED_DIRS = {"__pycache__"}

# organize.py's existing ".xls" route still calls the legacy
# ``pandas.ExcelFile(..., engine="xlrd")`` path directly -- this predates
# this module and is the dictionary-mapping-support-plan's Approach 4
# (organize.py's XLS routing cutover to xls_isolation.normalize_xls_*),
# which is explicitly out of Approach 1's scope and owned separately.
# This is a narrow, line-exact exception (not a blanket file exemption):
# only THIS specific known call site is grandfathered, so any additional
# or different xlrd usage organize.py picks up still fails the check.
_PENDING_APPROACH_4_MIGRATION: frozenset[tuple[Path, str]] = frozenset(
    {
        (
            _REPO_ROOT / "phi_engine" / "pipeline" / "organize.py",
            'self._route_excel(snapshot, stem, link_name, entry, engine="xlrd")',
        ),
    }
)


def _iter_production_python_files() -> list[Path]:
    files: list[Path] = []
    for root in _PRODUCTION_ROOTS:
        for path in root.rglob("*.py"):
            if any(part in _EXCLUDED_DIRS for part in path.parts):
                continue
            files.append(path)
    return files


def test_requirements_pin_xlrd_exact_version() -> None:
    text = (_REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    matches = re.findall(r"^xlrd\s*==\s*([0-9.]+)\s*$", text, flags=re.MULTILINE)
    assert matches == ["2.0.2"], f"expected exactly one 'xlrd==2.0.2' pin, found: {matches!r}"
    # Never a >=, ~=, or unpinned xlrd requirement line coexisting with it.
    other_xlrd_lines = [
        line
        for line in text.splitlines()
        if re.match(r"^\s*xlrd\b", line) and not re.match(r"^xlrd\s*==\s*2\.0\.2\s*$", line)
    ]
    assert other_xlrd_lines == []


def test_ci_workflow_has_xls_matrix_on_python_311_with_pandas_entries() -> None:
    workflow = yaml.safe_load((_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    xls_jobs = {name: job for name, job in jobs.items() if "xls" in name.lower()}
    assert xls_jobs, "expected at least one XLS-focused CI job"

    found_pandas_versions: set[str] = set()
    for job in xls_jobs.values():
        strategy = job.get("strategy", {})
        matrix = strategy.get("matrix", {})
        pandas_versions = matrix.get("pandas-version") or matrix.get("pandas_version") or matrix.get("pandas")
        assert pandas_versions, f"XLS job {job!r} must declare a pandas-version matrix"
        found_pandas_versions.update(str(v) for v in pandas_versions)

        steps = job.get("steps", [])
        python_setup_steps = [
            step
            for step in steps
            if isinstance(step.get("uses"), str) and step["uses"].startswith("actions/setup-python")
        ]
        assert python_setup_steps, "XLS job must set up Python explicitly"
        assert any(step.get("with", {}).get("python-version") == "3.11" for step in python_setup_steps)

        run_steps_text = "\n".join(step.get("run", "") for step in steps if "run" in step)
        assert "requirements.txt" in run_steps_text, "must install requirements.txt before overriding pandas"
        assert "pandas" in run_steps_text, "must explicitly (re)install the matrixed pandas version after that"

    assert {"2.2.3", "3.0.5"} <= found_pandas_versions


def test_no_xlrd_or_xlrd_engine_usage_outside_isolation_modules() -> None:
    violations: list[str] = []
    for path in _iter_production_python_files():
        if path in _ALLOWED_XLRD_MODULES:
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"^\s*import\s+xlrd\b", text, flags=re.MULTILINE):
            violations.append(f"{path}: imports xlrd")
        if re.search(r"^\s*from\s+xlrd\b", text, flags=re.MULTILINE):
            violations.append(f"{path}: imports from xlrd")
        for line in text.splitlines():
            if not re.search(r'engine\s*=\s*["\']xlrd["\']', line):
                continue
            if (path, line.strip()) in _PENDING_APPROACH_4_MIGRATION:
                continue
            violations.append(f"{path}: uses pandas engine='xlrd' ({line.strip()!r})")
    assert violations == [], "xlrd/xlrd-engine usage outside the isolation boundary:\n" + "\n".join(violations)


def test_no_production_xlwt_import() -> None:
    violations: list[str] = []
    for path in _iter_production_python_files():
        text = path.read_text(encoding="utf-8")
        if re.search(r"^\s*import\s+xlwt\b", text, flags=re.MULTILINE) or re.search(
            r"^\s*from\s+xlwt\b", text, flags=re.MULTILINE
        ):
            violations.append(str(path))
    assert violations == [], "production phi_engine code must never import xlwt (write-only, fixture-only):\n" + "\n".join(
        violations
    )


def test_only_worker_module_ever_calls_pandas_excel_reader_with_xlrd_engine() -> None:
    # Duplicate, narrower check specifically for the ExcelFile(...,
    # engine="xlrd") call shape the plan calls out by name, in case the
    # generic engine="xlrd" grep above is ever loosened.
    violations: list[str] = []
    for path in _iter_production_python_files():
        if path in _ALLOWED_XLRD_MODULES:
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"ExcelFile\s*\([^)]*xlrd", text, flags=re.DOTALL):
            violations.append(str(path))
    assert violations == []
