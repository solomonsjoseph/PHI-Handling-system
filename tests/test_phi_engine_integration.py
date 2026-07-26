"""Smoke tests for phi_engine integration.

Verifies that:
- phi_engine package imports without error
- phi_engine.security subpackage imports without error
- archive/ is not importable from any active phi_engine module
"""

import importlib
import os
import sys
import pathlib
import pytest


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
        """archive/ was removed entirely (fully superseded by phi_engine/);
        it must not exist, let alone be importable."""
        archive_path = REPO_ROOT / "archive"
        assert not archive_path.exists(), "archive/ must not exist; it was removed as superseded"

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
