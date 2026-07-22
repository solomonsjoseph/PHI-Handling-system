"""Focused deprecation coverage for the legacy intake-manifest/v2 public API.

This is an intentionally narrow lifecycle slice: it proves that public use of
`intake_add` (the legacy intake-manifest/v2 path) now surfaces an explicit,
stable DeprecationWarning ahead of any v3 cutover work, and that the legacy
result and behavior are otherwise unchanged at this isolated step.
"""

from __future__ import annotations

import inspect
import os
import sys
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

TEST_PHI_KEY_HEX = "0" * 64


def _drop_phi_runtime_modules() -> None:
    keep = {"phi_engine", "phi_engine.utils", "phi_engine.utils.pipeline_lock"}
    for name in list(sys.modules):
        if name in keep:
            continue
        if name.startswith("phi_engine."):
            del sys.modules[name]


@contextmanager
def _workspace(tmp_path: Path, study: str = "DeprecationStudy") -> Iterator[Path]:
    old_workspace = os.environ.get("PHI_WORKSPACE")
    old_study = os.environ.get("STUDY_NAME")
    old_key = os.environ.get("PHI_KEY_PATH")
    key = tmp_path / "phi_key"
    key.write_text(TEST_PHI_KEY_HEX, encoding="utf-8")
    key.chmod(0o600)
    try:
        os.environ["PHI_WORKSPACE"] = str(tmp_path / "workspace")
        os.environ["STUDY_NAME"] = study
        os.environ["PHI_KEY_PATH"] = str(key)
        _drop_phi_runtime_modules()
        yield Path(os.environ["PHI_WORKSPACE"])
    finally:
        if old_workspace is None:
            os.environ.pop("PHI_WORKSPACE", None)
        else:
            os.environ["PHI_WORKSPACE"] = old_workspace
        if old_study is None:
            os.environ.pop("STUDY_NAME", None)
        else:
            os.environ["STUDY_NAME"] = old_study
        if old_key is None:
            os.environ.pop("PHI_KEY_PATH", None)
        else:
            os.environ["PHI_KEY_PATH"] = old_key
        _drop_phi_runtime_modules()


def test_intake_add_emits_deprecation_warning_and_keeps_legacy_v2_result(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "labs.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")

    with _workspace(tmp_path):
        from phi_engine.pipeline.intake import intake_add

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            call_lineno = inspect.currentframe().f_lineno + 1
            manifest = intake_add(source, "DeprecationStudy")

        deprecation_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]

        # Exactly one DeprecationWarning for this single public call -- no
        # duplicate emission per file/entry processed inside intake_add.
        assert len(deprecation_warnings) == 1
        warning = deprecation_warnings[0]

        # stacklevel=2 must attribute the warning to this call site (this test
        # file, this exact line), not to the warnings.warn() line inside
        # phi_engine/pipeline/intake.py.
        assert warning.filename == __file__
        assert warning.lineno == call_lineno

        # Exact equality against the stable public warning text -- not a
        # substring match -- so any drift in the deprecation message is caught.
        expected_message = (
            "phi_engine.pipeline.intake.intake_add: intake-manifest/v2 is deprecated "
            "and will be replaced by a future manifest schema; this call path is "
            "scheduled for removal."
        )
        assert str(warning.message) == expected_message

        # Behavior for this isolated slice is otherwise unchanged: the legacy
        # v2 result shape and content are exactly what intake_add produced
        # before this change.
        assert manifest["schema"] == "intake-manifest/v2"
        entries_by_rel = {entry["relative_path"]: entry for entry in manifest["entries"].values()}
        assert set(entries_by_rel) == {"labs.csv"}
        assert manifest["errors"] == []
