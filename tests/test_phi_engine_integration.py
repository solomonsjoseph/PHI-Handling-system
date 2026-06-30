"""Smoke tests for phi_engine integration.

Verifies that:
- phi_engine package imports without error
- phi_engine.security subpackage imports without error
- archive/ is not importable from any active phi_engine module
- benchmark_config.yaml exists and has benchmark_mode=true
"""

import importlib
import os
import sys
import pathlib
import pytest
import yaml


REPO_ROOT = pathlib.Path(__file__).parent.parent


class TestPhiEngineImport:
    def test_phi_engine_importable(self):
        """phi_engine top-level package must import without error."""
        import phi_engine  # noqa: F401
        assert phi_engine.__doc__ is not None

    def test_phi_engine_security_importable(self):
        """phi_engine.security subpackage must import without error."""
        import phi_engine.security  # noqa: F401

    def test_phi_engine_audit_importable(self):
        """phi_engine.audit subpackage must import without error."""
        import phi_engine.audit  # noqa: F401

    def test_phi_engine_utils_importable(self):
        """phi_engine.utils subpackage must import without error."""
        import phi_engine.utils  # noqa: F401

    def test_phi_engine_config_importable(self):
        """phi_engine.config subpackage must import without error."""
        import phi_engine.config  # noqa: F401


class TestArchiveNotImportable:
    def test_archive_not_in_sys_path(self):
        """archive/ directory must not be a Python package importable from phi_engine."""
        archive_path = REPO_ROOT / "archive"
        assert archive_path.exists(), "archive/ directory must exist"
        # archive/ must not have an __init__.py (not a package)
        init = archive_path / "__init__.py"
        assert not init.exists(), (
            "archive/__init__.py must not exist; archive must not be importable"
        )

    def test_phi_engine_does_not_import_from_archive(self):
        """No .py file under phi_engine/ should import from 'archive' package."""
        phi_engine_dir = REPO_ROOT / "phi_engine"
        for py_file in phi_engine_dir.rglob("*.py"):
            content = py_file.read_text(encoding="utf-8")
            assert "from archive" not in content, (
                f"{py_file.relative_to(REPO_ROOT)} imports from archive/"
            )
            assert "import archive" not in content, (
                f"{py_file.relative_to(REPO_ROOT)} imports archive"
            )


class TestBenchmarkConfig:
    def test_benchmark_config_exists(self):
        """phi_engine/config/benchmark_config.yaml must exist."""
        cfg = REPO_ROOT / "phi_engine" / "config" / "benchmark_config.yaml"
        assert cfg.exists(), f"benchmark_config.yaml not found at {cfg}"

    def test_benchmark_mode_true(self):
        """benchmark_config.yaml must have benchmark_mode: true."""
        cfg = REPO_ROOT / "phi_engine" / "config" / "benchmark_config.yaml"
        with cfg.open() as f:
            data = yaml.safe_load(f)
        assert data.get("benchmark_mode") is True, (
            f"benchmark_mode must be true, got: {data.get('benchmark_mode')}"
        )

    def test_benchmark_config_bypasses_llm(self):
        """benchmark_config.yaml must set provider and model to none."""
        cfg = REPO_ROOT / "phi_engine" / "config" / "benchmark_config.yaml"
        with cfg.open() as f:
            data = yaml.safe_load(f)
        assert data.get("provider") == "none", (
            f"provider must be none, got: {data.get('provider')}"
        )
        assert data.get("model") == "none", (
            f"model must be none, got: {data.get('model')}"
        )
