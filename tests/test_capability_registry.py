from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from harness.capability_registry import (
    Capability,
    capability_rows,
    load_capabilities,
    require_status,
)


def _write_registry(tmp_path: Path, entries: list[dict]) -> Path:
    registry_path = tmp_path / "capability_registry.json"
    registry_path.write_text(json.dumps(entries), encoding="utf-8")
    return registry_path


def test_duplicate_ids_raise_value_error(tmp_path: Path) -> None:
    registry_path = _write_registry(
        tmp_path,
        [
            {"id": "dup", "kind": "validator", "status": "planned", "public_claim": "first"},
            {"id": "dup", "kind": "validator", "status": "planned", "public_claim": "second"},
        ],
    )

    with pytest.raises(ValueError, match="duplicate capability id: dup"):
        load_capabilities(registry_path)


def test_unknown_status_raises_value_error(tmp_path: Path) -> None:
    registry_path = _write_registry(
        tmp_path,
        [{"id": "bad", "kind": "validator", "status": "aspirational", "public_claim": "bad"}],
    )

    with pytest.raises(ValueError, match="unknown capability status: aspirational"):
        load_capabilities(registry_path)


def test_require_status_accepts_met_and_rejects_below_minimum() -> None:
    capabilities = load_capabilities()

    require_status(capabilities, "us_hipaa", "tested")
    with pytest.raises(
        RuntimeError,
        match="capability clinician_review is planned, below required implemented",
    ):
        require_status(capabilities, "clinician_review", "implemented")


def test_capability_rows_are_strings_and_sorted() -> None:
    capabilities = [
        Capability(id="z", kind="validator", status="planned", jurisdiction="", public_claim="Z"),
        Capability(id="b", kind="jurisdiction", status="tested", jurisdiction="us", public_claim="B"),
        Capability(id="a", kind="jurisdiction", status="tested", jurisdiction="us", public_claim="A"),
    ]

    rows = capability_rows(capabilities)

    assert [(row["kind"], row["jurisdiction"], row["id"]) for row in rows] == [
        ("jurisdiction", "us", "a"),
        ("jurisdiction", "us", "b"),
        ("validator", "", "z"),
    ]
    assert all(isinstance(value, str) for row in rows for value in row.values())


def test_module_cli_prints_markdown_registry_table() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "harness.capability_registry"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "| ID | Kind | Status | Jurisdiction | Claim | Output | Limitations |" in result.stdout
    assert "us_hipaa" in result.stdout
    assert "clinician_review" in result.stdout
    assert "| manifested |" not in result.stdout
    assert "planned" in result.stdout
