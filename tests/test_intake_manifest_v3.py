"""Tests for phi_engine.pipeline.intake -- the atomic, symlink-only
intake-manifest/v3 reconciliation contract, plus the fixed
intake_registry_lock() primitive it depends on.

Every test drives real filesystem state under a hermetic, per-test
PHI_WORKSPACE; there is no mocking of the reconciliation loop itself.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

TEST_PHI_KEY_HEX = "0" * 64


def _drop_phi_runtime_modules() -> None:
    # ``phi_engine.utils.pipeline_lock`` stays resident: reloading it would
    # re-run ``os.register_at_fork`` on every test, accumulating duplicate
    # atfork callbacks across the whole session and perturbing the real
    # fork-safety tests elsewhere in the combined suite. Its own module-
    # level ``config`` reference is instead explicitly rebound by
    # ``_workspace()`` below, right after the fresh reimport, so it never
    # reads a stale ``TMP_DIR``/``INTAKE_DIR`` left over from a previous
    # test's workspace.
    keep = {"phi_engine", "phi_engine.utils", "phi_engine.utils.pipeline_lock"}
    for name in list(sys.modules):
        if name in keep:
            continue
        if name.startswith("phi_engine."):
            del sys.modules[name]


@contextmanager
def _workspace(tmp_path: Path, study: str = "V3Study", *, workspace: Path | None = None) -> Iterator[Path]:
    old_workspace = os.environ.get("PHI_WORKSPACE")
    old_study = os.environ.get("STUDY_NAME")
    old_key = os.environ.get("PHI_KEY_PATH")
    key = tmp_path / "phi_key"
    key.write_text(TEST_PHI_KEY_HEX, encoding="utf-8")
    key.chmod(0o600)
    import phi_engine.utils.pipeline_lock as _pipeline_lock_module

    original_pipeline_lock_config = _pipeline_lock_module.config
    workspace_path = workspace if workspace is not None else (tmp_path / "workspace")
    try:
        os.environ["PHI_WORKSPACE"] = str(workspace_path)
        os.environ["STUDY_NAME"] = study
        os.environ["PHI_KEY_PATH"] = str(key)
        _drop_phi_runtime_modules()
        import phi_engine.config.config as _fresh_config

        _pipeline_lock_module.config = _fresh_config
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
        _pipeline_lock_module.config = original_pipeline_lock_config
        _drop_phi_runtime_modules()


def _make_canonical_source(root: Path) -> None:
    (root / "datasets").mkdir(parents=True)
    (root / "forms").mkdir(parents=True)
    (root / "data_dictionary").mkdir(parents=True)
    (root / "datasets" / "labs.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    (root / "forms" / "consent.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    (root / "data_dictionary" / "dict.csv").write_text("var,label\nSUBJID,Subject\n", encoding="utf-8")


def _entries_by_rel(manifest: dict) -> dict[str, dict]:
    return {entry["relative_path"]: entry for entry in manifest["entries"].values()}


def _snapshot_tree(root: Path) -> set[str]:
    """Every file/directory path (relative to ``root``) currently on
    disk, or the empty set if ``root`` does not exist at all. Used to
    prove a rollback restores a tree to EXACTLY its pre-call shape --
    not merely "the study is gone" -- catching stray empty directories
    a narrower existence check would miss."""
    if not root.exists():
        return set()
    return {str(p.relative_to(root)) for p in root.rglob("*")}


# --- schema validation ----------------------------------------------------------------------


def test_canonical_package_produces_exact_v3_ready_tree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add, load_intake_manifest

        manifest = intake_add(source, "CanonStudy")

        assert manifest["schema"] == "intake-manifest/v3"
        assert manifest["study"] == "CanonStudy"
        assert manifest["study_name_source"] == "user"
        assert manifest["status"] == "ready"
        assert manifest["source_root"] == str(source.resolve())
        assert manifest["review_items"] == []
        assert manifest["errors"] == []
        assert manifest["removals"] == []
        assert set(manifest) == {
            "schema", "study", "study_name_source", "status", "source_root",
            "entries", "review_items", "errors", "removals",
        }

        by_rel = _entries_by_rel(manifest)
        assert set(by_rel) == {"datasets/labs.csv", "forms/consent.pdf", "data_dictionary/dict.csv"}
        for rel, entry in by_rel.items():
            assert set(entry) == {
                "artifact_id", "intake_path", "component", "relative_path", "original_path",
                "sha256", "size", "mtime_ns", "device", "inode", "mode",
            }
            assert entry["intake_path"].endswith(f"__{Path(rel).name}")
            assert entry["intake_path"].split("/", 1)[0] == rel.split("/", 1)[0]
            assert entry["original_path"] == f"{manifest['source_root']}/{rel}"
            link_path = workspace / "intake" / "CanonStudy" / entry["intake_path"]
            assert link_path.is_symlink()
            assert os.readlink(link_path) == entry["original_path"]

        reloaded = load_intake_manifest("CanonStudy")
        assert reloaded == manifest


def test_duplicate_bytes_and_nested_duplicate_folders_remain_distinct(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_canonical_source(source)
    (source / "data_dictionary" / "dup1.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    (source / "data_dictionary" / "nested").mkdir()
    (source / "data_dictionary" / "nested" / "dup2.csv").write_text("x,y\n1,2\n", encoding="utf-8")

    with _workspace(tmp_path):
        from phi_engine.pipeline.intake import intake_add

        manifest = intake_add(source, "DupStudy")
        assert manifest["status"] == "ready"
        by_rel = _entries_by_rel(manifest)
        e1 = by_rel["data_dictionary/dup1.csv"]
        e2 = by_rel["data_dictionary/nested/dup2.csv"]
        assert e1["sha256"] == e2["sha256"]
        assert e1["artifact_id"] != e2["artifact_id"]
        assert e1["intake_path"] != e2["intake_path"]
        assert e2["intake_path"] == f"data_dictionary/nested/{e2['artifact_id']}__dup2.csv"


def test_unsupported_and_missing_components_become_review_required(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "datasets" / "weird.json").write_text("{}", encoding="utf-8")

    with _workspace(tmp_path):
        from phi_engine.pipeline.intake import intake_add

        manifest = intake_add(source, "UnsupportedStudy")
        assert manifest["status"] == "review_required"
        by_rel = _entries_by_rel(manifest)
        assert by_rel["datasets/weird.json"]["component"] == "_unclassified"
        assert by_rel["datasets/weird.json"]["intake_path"].startswith("_unclassified/datasets/")
        reasons = {item["reason"] for item in manifest["review_items"]}
        assert "unsupported-format" in reasons
        assert "missing-component-directory" in reasons  # forms/ absent


def test_multi_sheet_dataset_xlsx_becomes_unclassified_review(tmp_path: Path) -> None:
    import openpyxl

    source = tmp_path / "source"
    _make_canonical_source(source)
    wb = openpyxl.Workbook()
    wb.active.title = "Sheet1"
    wb.create_sheet("Sheet2")
    (source / "datasets" / "multi.xlsx").parent.mkdir(parents=True, exist_ok=True)
    wb.save(source / "datasets" / "multi.xlsx")

    with _workspace(tmp_path):
        from phi_engine.pipeline.intake import intake_add

        manifest = intake_add(source, "MultiSheetStudy")
        assert manifest["status"] == "review_required"
        by_rel = _entries_by_rel(manifest)
        assert by_rel["datasets/multi.xlsx"]["component"] == "_unclassified"
        assert any(item["reason"] == "dataset-xlsx-multiple-sheets" for item in manifest["review_items"])


def test_entry_and_review_records_reject_unknown_and_missing_keys() -> None:
    from phi_engine.pipeline import intake

    entry = {
        "artifact_id": "a_" + "0" * 32, "intake_path": "datasets/a_" + "0" * 32 + "__f.csv",
        "component": "datasets", "relative_path": "datasets/f.csv",
        "original_path": "/src/datasets/f.csv", "sha256": "0" * 64,
        "size": 1, "mtime_ns": 1, "device": 1, "inode": 1, "mode": 0o644,
    }
    intake._validate_entry(entry["intake_path"], entry, "/src", set(), set(), set())
    bad = dict(entry)
    bad["extra"] = "nope"
    with pytest.raises(intake.IntakeManifestError, match="intake_manifest_invalid"):
        intake._validate_entry(bad["intake_path"], bad, "/src", set(), set(), set())
    missing = {k: v for k, v in entry.items() if k != "mode"}
    with pytest.raises(intake.IntakeManifestError):
        intake._validate_entry(missing["intake_path"], missing, "/src", set(), set(), set())


def test_duplicate_artifact_id_or_relative_path_across_entries_is_rejected() -> None:
    from phi_engine.pipeline import intake

    aid = "a_" + "1" * 32
    entry_a = {
        "artifact_id": aid, "intake_path": f"datasets/{aid}__a.csv", "component": "datasets",
        "relative_path": "datasets/a.csv", "original_path": "/src/datasets/a.csv", "sha256": "0" * 64,
        "size": 1, "mtime_ns": 1, "device": 1, "inode": 1, "mode": 0o644,
    }
    manifest = {
        "schema": "intake-manifest/v3", "study": "S", "study_name_source": "user", "status": "ready",
        "source_root": "/src", "review_items": [], "errors": [], "removals": [],
        "entries": {
            entry_a["intake_path"]: entry_a,
            f"forms/{aid}__b.pdf": {**entry_a, "intake_path": f"forms/{aid}__b.pdf", "component": "forms", "relative_path": "forms/b.pdf", "original_path": "/src/forms/b.pdf"},
        },
    }
    with pytest.raises(intake.IntakeManifestError, match="intake_manifest_invalid"):
        intake._validate_manifest_v3(manifest, expect_study="S")

    aid2 = "a_" + "2" * 32
    dup_rel = {
        "schema": "intake-manifest/v3", "study": "S", "study_name_source": "user", "status": "ready",
        "source_root": "/src", "review_items": [], "errors": [], "removals": [],
        "entries": {
            entry_a["intake_path"]: entry_a,
            f"forms/{aid2}__a.csv": {**entry_a, "artifact_id": aid2, "intake_path": f"forms/{aid2}__a.csv", "component": "forms"},
        },
    }
    with pytest.raises(intake.IntakeManifestError, match="intake_manifest_invalid"):
        intake._validate_manifest_v3(dup_rel, expect_study="S")


def test_status_recompute_rejects_inconsistent_stored_status() -> None:
    from phi_engine.pipeline import intake

    raw = {
        "schema": "intake-manifest/v3", "study": "S", "study_name_source": "user",
        "status": "ready", "source_root": "/src", "entries": {}, "removals": [],
        "review_items": [{"path": "", "reason": "support-phi-status-required", "blocking": True}],
        "errors": [],
    }
    with pytest.raises(intake.IntakeManifestError, match="intake_manifest_invalid"):
        intake._validate_manifest_v3(raw, expect_study="S")


def test_removal_record_and_error_record_shapes() -> None:
    from phi_engine.pipeline import intake

    assert intake._validate_removals(
        [{"artifact_id": "a_" + "0" * 32, "relative_path": "datasets/f.csv", "sha256": "0" * 64, "removed_at": "2026-01-01T00:00:00Z"}]
    )
    with pytest.raises(intake.IntakeManifestError):
        intake._validate_removals([{"artifact_id": "a_" + "0" * 32, "relative_path": "datasets/f.csv", "sha256": "0" * 64}])
    assert intake._validate_errors([{"path": None, "reason": "study-name-inspection-failed"}])
    assert intake._validate_errors([{"path": "", "reason": "source-unreadable"}])
    with pytest.raises(intake.IntakeManifestError):
        intake._validate_errors([{"path": "../escape", "reason": "source-unreadable"}])


def test_study_name_conflict_candidates_shape() -> None:
    from phi_engine.pipeline import intake

    good = [{
        "path": "", "reason": "study-name-conflict", "blocking": True,
        "candidates": {"forms": "StudyA", "dictionary_mapping": "StudyB"},
    }]
    assert intake._validate_review_items(good)
    bad_reason = [{
        "path": "", "reason": "cross-component-hardlink", "blocking": True,
        "candidates": {"forms": "StudyA", "dictionary_mapping": "StudyB"},
    }]
    with pytest.raises(intake.IntakeManifestError):
        intake._validate_review_items(bad_reason)


def test_validator_rejects_noncanonical_source_root() -> None:
    from phi_engine.pipeline import intake

    raw = {
        "schema": "intake-manifest/v3", "study": "S", "study_name_source": "user", "status": "ready",
        "source_root": "/tmp/a/../b", "entries": {}, "review_items": [], "errors": [], "removals": [],
    }
    with pytest.raises(intake.IntakeManifestError, match="intake_manifest_invalid"):
        intake._validate_manifest_v3(raw, expect_study="S")

    trailing_slash = dict(raw, source_root="/tmp/a/")
    with pytest.raises(intake.IntakeManifestError, match="intake_manifest_invalid"):
        intake._validate_manifest_v3(trailing_slash, expect_study="S")

    canonical = dict(raw, source_root="/tmp/a")
    validated = intake._validate_manifest_v3(canonical, expect_study="S")
    assert validated["source_root"] == "/tmp/a"


def test_validator_canonical_root_join_handles_filesystem_root() -> None:
    """``source_root == "/"`` must join as ``/forms/x.pdf``, never the
    doubled-separator ``//forms/x.pdf`` a plain string interpolation
    would produce."""
    from phi_engine.pipeline import intake

    aid = "a_" + "4" * 32
    entry = {
        "artifact_id": aid, "intake_path": f"forms/{aid}__x.pdf", "component": "forms",
        "relative_path": "forms/x.pdf", "original_path": "//forms/x.pdf", "sha256": "0" * 64,
        "size": 1, "mtime_ns": 1, "device": 1, "inode": 1, "mode": 0o644,
    }
    with pytest.raises(intake.IntakeManifestError, match="intake_manifest_invalid"):
        intake._validate_entry(entry["intake_path"], entry, "/", set(), set(), set())

    canonical = dict(entry, original_path="/forms/x.pdf")
    validated = intake._validate_entry(canonical["intake_path"], canonical, "/", set(), set(), set())
    assert validated["original_path"] == "/forms/x.pdf"


def test_validator_rejects_mismatched_accepted_component_and_relative_path() -> None:
    from phi_engine.pipeline import intake

    aid = "a_" + "3" * 32
    entry = {
        "artifact_id": aid, "intake_path": f"forms/{aid}__x.pdf", "component": "forms",
        "relative_path": "datasets/x.pdf", "original_path": "/src/datasets/x.pdf", "sha256": "0" * 64,
        "size": 1, "mtime_ns": 1, "device": 1, "inode": 1, "mode": 0o644,
    }
    with pytest.raises(intake.IntakeManifestError, match="intake_manifest_invalid"):
        intake._validate_entry(entry["intake_path"], entry, "/src", set(), set(), set())

    matching = dict(entry, relative_path="forms/x.pdf", original_path="/src/forms/x.pdf")
    validated = intake._validate_entry(matching["intake_path"], matching, "/src", set(), set(), set())
    assert validated["relative_path"] == "forms/x.pdf"


def test_validator_rejects_invalid_conflict_candidate_name() -> None:
    from phi_engine.pipeline import intake

    bad = [{
        "path": "", "reason": "study-name-conflict", "blocking": True,
        "candidates": {"forms": "..", "dictionary_mapping": "StudyB"},
    }]
    with pytest.raises(intake.IntakeManifestError, match="intake_manifest_invalid"):
        intake._validate_review_items(bad)


def test_validator_requires_normalized_conflict_candidate_slug() -> None:
    """A syntactically ``lock_path_for``-valid but non-normalized
    candidate (``'Study-'``, whose ``safe_review_slug`` is ``'Study'``)
    must still be rejected -- the stored candidate must be exactly the
    normalized name naming.py itself would have produced."""
    from phi_engine.pipeline import intake

    unnormalized = [{
        "path": "", "reason": "study-name-conflict", "blocking": True,
        "candidates": {"forms": "Study-", "dictionary_mapping": "StudyB"},
    }]
    with pytest.raises(intake.IntakeManifestError, match="intake_manifest_invalid"):
        intake._validate_review_items(unnormalized)

    normalized = [{
        "path": "", "reason": "study-name-conflict", "blocking": True,
        "candidates": {"forms": "Study", "dictionary_mapping": "StudyB"},
    }]
    assert intake._validate_review_items(normalized)


# --- hostile workspace paths / symlinked ancestry --------------------------------------------


def test_symlinked_study_directory_is_rejected_and_untouched(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import IntakeManifestError, intake_add

        intake_dir = workspace / "intake"
        intake_dir.mkdir(parents=True)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (intake_dir / "HostileStudy").symlink_to(elsewhere, target_is_directory=True)

        with pytest.raises(IntakeManifestError, match="intake-tree-unsafe"):
            intake_add(source, "HostileStudy")

        assert (intake_dir / "HostileStudy").is_symlink()
        assert elsewhere.is_dir()
        assert list(elsewhere.iterdir()) == []


def test_symlinked_intake_root_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import IntakeManifestError, intake_add

        elsewhere = tmp_path / "elsewhere_root"
        elsewhere.mkdir()
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "intake").symlink_to(elsewhere, target_is_directory=True)

        with pytest.raises(IntakeManifestError, match="intake-tree-unsafe"):
            intake_add(source, "AnyStudy")


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink safety")
def test_symlinked_workspace_ancestor_is_rejected_not_followed(tmp_path: Path) -> None:
    """A symlinked ANCESTOR of INTAKE_DIR (not INTAKE_DIR itself) must
    fail closed too -- proving the shared descriptor-walk helper checks
    every segment, not merely the leaf."""
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path):
        import phi_engine.config.config as cfg
        from phi_engine.pipeline.intake import IntakeManifestError, intake_add

        real_root = tmp_path / "real_root"
        real_root.mkdir()
        alias = tmp_path / "alias_root"
        alias.symlink_to(real_root, target_is_directory=True)
        cfg.INTAKE_DIR = alias / "intake"
        try:
            with pytest.raises(IntakeManifestError, match="intake-tree-unsafe"):
                intake_add(source, "ThroughAlias")
        finally:
            del cfg.INTAKE_DIR

        assert list(real_root.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink safety")
def test_env_level_workspace_alias_is_rejected_not_followed(tmp_path: Path) -> None:
    """The REAL ``PHI_WORKSPACE`` environment value, not a monkeypatched
    ``config.INTAKE_DIR``, is a symlink. ``config.BASE_DIR`` must preserve
    that lexical (non-resolved) path -- proving ``Path.resolve()`` no
    longer erases the symlink evidence before the descriptor-relative
    ancestry walkers ever see it."""
    source = tmp_path / "source"
    _make_canonical_source(source)

    real_workspace = tmp_path / "real_workspace"
    real_workspace.mkdir()
    alias = tmp_path / "workspace_alias"
    alias.symlink_to(real_workspace, target_is_directory=True)

    with _workspace(tmp_path, "AliasStudy", workspace=alias):
        import phi_engine.config.config as cfg
        from phi_engine.pipeline.intake import IntakeManifestError, intake_add

        assert str(cfg.BASE_DIR) == str(alias)  # lexical: symlink NOT resolved away

        with pytest.raises(IntakeManifestError, match="intake-tree-unsafe"):
            intake_add(source, "AliasStudy")

    assert list(real_workspace.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink safety")
def test_env_level_workspace_alias_with_dotdot_segment_is_rejected(tmp_path: Path) -> None:
    """A configured ``PHI_WORKSPACE`` with a symlinked component followed
    by a literal ``..`` segment must fail closed via the ancestry
    walker's own ``'..'`` rejection -- proving ``BASE_DIR`` construction
    no longer lexically normalizes ``'..'`` away (which would silently
    erase the symlinked segment before any ancestry check ever runs,
    the exact bypass a naive ``os.path.abspath``/``Path.resolve`` join
    would permit)."""
    source = tmp_path / "source"
    _make_canonical_source(source)

    real_workspace = tmp_path / "real_workspace2"
    real_workspace.mkdir()
    alias = tmp_path / "workspace_alias2"
    alias.symlink_to(real_workspace, target_is_directory=True)
    workspace_with_dotdot = alias / ".." / "workspace_alias2"

    with _workspace(tmp_path, "DotDotStudy", workspace=workspace_with_dotdot):
        import phi_engine.config.config as cfg
        from phi_engine.pipeline.intake import IntakeManifestError, intake_add

        assert ".." in cfg.BASE_DIR.parts  # preserved, never collapsed away

        with pytest.raises(IntakeManifestError, match="intake-tree-unsafe"):
            intake_add(source, "DotDotStudy")

    assert list(real_workspace.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink safety")
def test_load_manifest_rejects_symlinked_intake_dir_ancestor(tmp_path: Path) -> None:
    """A previously-created, genuinely ``ready`` study must NOT become
    loadable once its ancestry is later redirected through a symlink --
    the read path (``load_intake_manifest``) must fail closed exactly
    like the write path, never silently returning the manifest through
    an aliased ancestor."""
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        import phi_engine.config.config as cfg
        from phi_engine.pipeline.intake import IntakeManifestError, intake_add, load_intake_manifest

        intake_add(source, "ReadPathStudy")

        real_root = tmp_path / "read_real_root"
        real_root.mkdir()
        alias = tmp_path / "read_alias_root"
        alias.symlink_to(real_root, target_is_directory=True)
        # the already-created, real intake tree becomes reachable ONLY
        # through the alias -- the load path must reject that redirection
        # rather than transparently following it.
        shutil.move(str(workspace / "intake"), str(real_root / "intake"))
        cfg.INTAKE_DIR = alias / "intake"
        try:
            with pytest.raises(IntakeManifestError, match="intake_manifest_invalid"):
                load_intake_manifest("ReadPathStudy")
        finally:
            del cfg.INTAKE_DIR

        assert (real_root / "intake" / "ReadPathStudy" / "intake_manifest.json").is_file()


def test_pruning_leaves_unexpected_regular_file_untouched(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add

        manifest = intake_add(source, "PruneStudy")
        by_rel = _entries_by_rel(manifest)
        dataset_key = by_rel["datasets/labs.csv"]["intake_path"]
        link_path = workspace / "intake" / "PruneStudy" / dataset_key
        assert link_path.is_symlink()

        (source / "datasets" / "labs.csv").unlink()
        link_path.unlink()
        link_path.write_text("sabotage", encoding="utf-8")
        link_path.chmod(0o600)

        manifest2 = intake_add(source, "PruneStudy")
        assert manifest2["status"] == "failed"
        assert any(e["reason"] == "intake-tree-unsafe" for e in manifest2["errors"])
        assert link_path.is_file() and not link_path.is_symlink()
        assert link_path.read_text(encoding="utf-8") == "sabotage"


def test_unexpected_new_regular_file_fails_unsafe_and_is_untouched(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add

        first = intake_add(source, "InventoryStudy")
        study_dir = workspace / "intake" / "InventoryStudy"
        planted = study_dir / "datasets" / "planted.txt"
        planted.write_text("hostile", encoding="utf-8")
        planted.chmod(0o600)

        second = intake_add(source, "InventoryStudy")
        assert second["status"] == "failed"
        assert any(e["reason"] == "intake-tree-unsafe" for e in second["errors"])
        assert planted.is_file()
        assert planted.read_text(encoding="utf-8") == "hostile"
        assert set(_entries_by_rel(second)) == set(_entries_by_rel(first))


def test_unexpected_empty_directories_fail_unsafe_during_reconcile(tmp_path: Path) -> None:
    """Empty directories -- unlike files or symlinks -- have no leaves for
    a leaf-only inventory to ever see. Root, component, and nested
    unexpected directories must all still fail closed."""
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add

        first = intake_add(source, "EmptyDirStudy")
        study_dir = workspace / "intake" / "EmptyDirStudy"
        (study_dir / "junk_root_dir").mkdir()
        (study_dir / "datasets" / "junk_component_dir").mkdir()
        (study_dir / "junk_root_dir" / "junk_nested_dir").mkdir()

        second = intake_add(source, "EmptyDirStudy")
        assert second["status"] == "failed"
        unsafe_count = sum(1 for e in second["errors"] if e["reason"] == "intake-tree-unsafe")
        assert unsafe_count >= 2  # at least the root dir and the component-nested dir
        assert (study_dir / "junk_root_dir").is_dir()
        assert (study_dir / "junk_root_dir" / "junk_nested_dir").is_dir()
        assert (study_dir / "datasets" / "junk_component_dir").is_dir()
        assert set(_entries_by_rel(second)) == set(_entries_by_rel(first))


def test_load_manifest_rejects_unmanifested_node(tmp_path: Path) -> None:
    """load_intake_manifest must run the same unexpected-node inventory
    as reconciliation, not only trust the stored manifest content."""
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import IntakeManifestError, intake_add, load_intake_manifest

        intake_add(source, "UnmanifestedStudy")
        study_dir = workspace / "intake" / "UnmanifestedStudy"
        (study_dir / "planted_dir").mkdir()

        with pytest.raises(IntakeManifestError, match="intake_manifest_invalid"):
            load_intake_manifest("UnmanifestedStudy")

        assert (study_dir / "planted_dir").is_dir()


def test_existing_manifest_less_study_dir_is_unsafe(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import IntakeManifestError, intake_add

        study_dir = workspace / "intake" / "PartialStudy"
        study_dir.mkdir(parents=True)
        study_dir.chmod(0o700)

        with pytest.raises(IntakeManifestError, match="intake-tree-unsafe"):
            intake_add(source, "PartialStudy")

        assert list(study_dir.iterdir()) == []


def test_malformed_generated_manifest_sibling_fails_unsafe(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import IntakeManifestError, intake_add

        intake_add(source, "FirstStudy")
        hostile = workspace / "intake" / "study-deadbeef"
        hostile.mkdir()
        hostile.chmod(0o700)
        manifest_path = hostile / "intake_manifest.json"
        manifest_path.write_text("{not valid json", encoding="utf-8")
        manifest_path.chmod(0o600)

        with pytest.raises(IntakeManifestError, match="intake-tree-unsafe"):
            intake_add(source, "SecondStudy")

        assert not (workspace / "intake" / "SecondStudy").exists()
        assert hostile.is_dir()
        assert manifest_path.read_text(encoding="utf-8") == "{not valid json"


def test_symlinked_registry_sibling_fails_unsafe(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import IntakeManifestError, intake_add

        intake_add(source, "FirstStudy")
        elsewhere = tmp_path / "elsewhere_sibling"
        elsewhere.mkdir()
        sibling = workspace / "intake" / "study-symlinked1"
        sibling.symlink_to(elsewhere, target_is_directory=True)

        with pytest.raises(IntakeManifestError, match="intake-tree-unsafe"):
            intake_add(source, "SecondStudy")

        assert not (workspace / "intake" / "SecondStudy").exists()
        assert sibling.is_symlink()


def test_source_symlink_is_rejected_no_entry_created(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_canonical_source(source)
    real_target = tmp_path / "outside.pdf"
    real_target.write_bytes(b"%PDF-1.4\n%%EOF")
    (source / "forms" / "linked.pdf").symlink_to(real_target)

    with _workspace(tmp_path):
        from phi_engine.pipeline.intake import intake_add

        manifest = intake_add(source, "SymlinkSourceStudy")
        by_rel = _entries_by_rel(manifest)
        assert "forms/linked.pdf" not in by_rel
        reasons = {item["reason"] for item in manifest["review_items"]} | {item["reason"] for item in manifest["errors"]}
        assert "source-symlink-not-allowed" in reasons


# --- atomic writes -----------------------------------------------------------------------------


def test_manifest_is_written_0600_via_atomic_replace(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add

        intake_add(source, "AtomicStudy")
        manifest_path = workspace / "intake" / "AtomicStudy" / "intake_manifest.json"
        assert manifest_path.is_file()
        assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
        # no leftover temp files
        leftovers = [p for p in manifest_path.parent.iterdir() if p.name.startswith(".intake_manifest.json.")]
        assert leftovers == []


def test_atomic_write_failure_cleans_up_temp_file(tmp_path: Path) -> None:
    from phi_engine.pipeline import intake

    study_dir = tmp_path / "study"
    study_dir.mkdir()
    study_dir.chmod(0o700)
    dir_fd = os.open(study_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        real_write = os.write

        def failing_write(fd, data):
            raise OSError("simulated disk failure mid-write")

        os.write = failing_write
        try:
            with pytest.raises(OSError):
                intake._atomic_write_in_dir(dir_fd, "intake_manifest.json", b'{"a":1}', 0o600)
        finally:
            os.write = real_write

        # write failed before any content landed -- no target file, no temp leftover
        assert os.listdir(study_dir) == []
    finally:
        os.close(dir_fd)


def test_initial_manifest_failure_leaves_no_study_artifacts(tmp_path: Path) -> None:
    """A fresh (never-before-existing) study whose manifest commit fails
    must leave NOTHING behind: not the manifest, not the newly created
    symlinks/directories, not even the reservation directory itself --
    so a retry gets a genuinely fresh reservation again."""
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add

        real_write = os.write

        def failing_write(fd, data):
            if data.startswith(b'{\n  "entries"'):
                raise OSError("simulated disk failure during initial manifest write")
            return real_write(fd, data)

        os.write = failing_write
        try:
            with pytest.raises(OSError):
                intake_add(source, "FreshFailStudy")
        finally:
            os.write = real_write

        assert not (workspace / "intake" / "FreshFailStudy").exists()

        # a retry must succeed cleanly (proving the reservation was fully undone)
        retried = intake_add(source, "FreshFailStudy")
        assert retried["status"] == "ready"


def test_existing_update_failure_restores_prior_tree_and_manifest(tmp_path: Path) -> None:
    """Adding a 4th source file to an already-``ready`` 3-entry study,
    then forcing the manifest commit to fail, must restore the EXACT
    prior manifest bytes and the exact prior 3-link tree -- not a
    half-applied 4-link state."""
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add

        first = intake_add(source, "UpdateFailStudy")
        assert first["status"] == "ready"
        assert len(first["entries"]) == 3
        study_dir = workspace / "intake" / "UpdateFailStudy"
        manifest_path = study_dir / "intake_manifest.json"
        prior_manifest_bytes = manifest_path.read_bytes()
        prior_links = sorted(str(p.relative_to(study_dir)) for p in study_dir.rglob("*") if p.is_symlink())
        assert len(prior_links) == 3

        (source / "data_dictionary" / "extra.csv").write_text("x,y\n1,2\n", encoding="utf-8")

        real_write = os.write

        def failing_write(fd, data):
            if data.startswith(b'{\n  "entries"'):
                raise OSError("simulated disk failure during update manifest write")
            return real_write(fd, data)

        os.write = failing_write
        try:
            with pytest.raises(OSError):
                intake_add(source, "UpdateFailStudy")
        finally:
            os.write = real_write

        assert manifest_path.read_bytes() == prior_manifest_bytes
        after_links = sorted(str(p.relative_to(study_dir)) for p in study_dir.rglob("*") if p.is_symlink())
        assert after_links == prior_links


def test_promotion_reconcile_failure_restores_old_tree_and_audit(tmp_path: Path) -> None:
    """A reconciliation failure occurring AFTER a generated tree has
    already been promoted (renamed) must roll the promotion itself back:
    the tree returns to its old generated name, its audit review
    directory returns with it, and the destination name is never left
    holding an invalid or partial tree -- nor any stray empty audit
    ancestor directory the promotion created for it."""
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        import phi_engine.pipeline.intake as intake_module
        from phi_engine.pipeline.intake import IntakeManifestError, intake_add

        generated = intake_add(source, support_confirmed_no_phi=False)
        study_a = generated["study"]
        old_review = workspace / "output" / study_a / "audit" / "human_review" / "intake" / "intake_review.md"
        assert old_review.is_file()
        before = _snapshot_tree(workspace / "output")

        def failing_reconcile(**kwargs: Any) -> None:
            raise IntakeManifestError("intake-tree-unsafe")

        original_reconcile = intake_module._reconcile_study_tree
        intake_module._reconcile_study_tree = failing_reconcile
        try:
            with pytest.raises(IntakeManifestError):
                intake_add(source, "PromoReconcileFail")
        finally:
            intake_module._reconcile_study_tree = original_reconcile

        assert (workspace / "intake" / study_a).is_dir()
        assert not (workspace / "intake" / "PromoReconcileFail").exists()
        assert old_review.is_file()
        after = _snapshot_tree(workspace / "output")
        assert after == before  # no PromoReconcileFail/, audit/, or human_review/ leftovers


def test_review_note_write_failure_restores_complete_pre_call_state(tmp_path: Path) -> None:
    """A failure while writing the required review note DURING the write
    itself (pre-rename, BEFORE the manifest is ever touched, since the
    note is written first) must leave the ENTIRE output tree exactly as
    it was -- no manifest, no note, and no partial/empty audit ancestor
    directories -- proving a review-required attempt can never commit a
    manifest that references a review note that does not exist."""
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "datasets" / "unsupported.json").write_text("{}", encoding="utf-8")

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add

        output_root = workspace / "output"
        before = _snapshot_tree(output_root)

        real_write = os.write

        def failing_write(fd, data):
            if data.startswith(b"# Intake Review"):
                raise OSError("simulated disk failure during review note write")
            return real_write(fd, data)

        os.write = failing_write
        try:
            with pytest.raises(OSError):
                intake_add(source, "NoteFailStudy")
        finally:
            os.write = real_write

        assert not (workspace / "intake" / "NoteFailStudy").exists()
        after = _snapshot_tree(output_root)
        assert after == before  # no NoteFailStudy/, audit/, human_review/, or intake/ leftovers


def test_note_fsync_failure_after_rename_restores_absence(tmp_path: Path) -> None:
    """A failure that occurs AFTER the note's atomic rename already
    succeeded (a case a write-only injection can never reach -- the real
    ``_atomic_write_in_dir`` fsyncs the STUDY directory only once the
    rename itself has completed) must still restore complete absence --
    no orphaned note file and no leftover audit ancestor directories."""
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "datasets" / "unsupported.json").write_text("{}", encoding="utf-8")

    with _workspace(tmp_path) as workspace:
        import phi_engine.pipeline.intake as intake_module
        from phi_engine.pipeline.intake import intake_add

        output_root = workspace / "output"
        before = _snapshot_tree(output_root)

        original_atomic_write = intake_module._atomic_write_in_dir

        def write_rename_then_fail(dir_fd, filename, payload, mode):
            if filename != "intake_review.md":
                return original_atomic_write(dir_fd, filename, payload, mode)
            # Reproduce the real write+fsync+rename sequence verbatim (the
            # note IS durably renamed into place), then fail exactly where
            # the real function's directory fsync would run next -- a
            # failure point no write-only injection can reach.
            temp_name = f".{filename}.{os.getpid()}.fsynctest.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            fd = os.open(temp_name, flags, mode, dir_fd=dir_fd)
            try:
                written = 0
                while written < len(payload):
                    written += os.write(fd, payload[written:])
                os.fsync(fd)
            finally:
                os.close(fd)
            os.rename(temp_name, filename, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            raise OSError("simulated fsync failure after note rename")

        intake_module._atomic_write_in_dir = write_rename_then_fail
        try:
            with pytest.raises(OSError):
                intake_add(source, "NoteFsyncFailStudy")
        finally:
            intake_module._atomic_write_in_dir = original_atomic_write

        assert not (workspace / "intake" / "NoteFsyncFailStudy").exists()
        after = _snapshot_tree(output_root)
        assert after == before  # the orphaned-note-post-rename case is fully restored too


def test_manifest_failure_after_note_commit_restores_prior_note_state(tmp_path: Path) -> None:
    """The review note is written FIRST; if the manifest write fails
    AFTER the note already committed, the just-written note -- and every
    audit ancestor directory created to hold it -- must be rolled back
    too, restoring the complete pre-call output-tree shape."""
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "datasets" / "unsupported.json").write_text("{}", encoding="utf-8")

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add

        output_root = workspace / "output"
        before = _snapshot_tree(output_root)

        real_write = os.write

        def failing_write(fd, data):
            if data.startswith(b'{\n  "entries"'):
                raise OSError("simulated disk failure during manifest write after note committed")
            return real_write(fd, data)

        os.write = failing_write
        try:
            with pytest.raises(OSError):
                intake_add(source, "NoteThenManifestFailStudy")
        finally:
            os.write = real_write

        assert not (workspace / "intake" / "NoteThenManifestFailStudy").exists()
        after = _snapshot_tree(output_root)
        assert after == before


def test_review_note_is_count_and_reason_only_never_leaks_paths(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "datasets" / "secret_patient_name.json").write_text("{}", encoding="utf-8")

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add

        manifest = intake_add(source, "NoteStudy")
        assert manifest["status"] == "review_required"
        note_path = workspace / "output" / "NoteStudy" / "audit" / "human_review" / "intake" / "intake_review.md"
        assert note_path.is_file()
        assert stat.S_IMODE(note_path.stat().st_mode) == 0o600
        text = note_path.read_text(encoding="utf-8")
        assert "secret_patient_name" not in text
        assert str(source) not in text
        assert "review_items:" in text


# --- stable IDs / removal / reappearance --------------------------------------------------------


def test_stable_ids_across_reruns_and_content_change(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path):
        from phi_engine.pipeline.intake import intake_add

        first = intake_add(source, "StableStudy")
        first_id = _entries_by_rel(first)["datasets/labs.csv"]["artifact_id"]

        (source / "datasets" / "labs.csv").write_text("SUBJID,AGE\n2,55\n", encoding="utf-8")
        second = intake_add(source, "StableStudy")
        second_entry = _entries_by_rel(second)["datasets/labs.csv"]
        assert second_entry["artifact_id"] == first_id
        assert second_entry["sha256"] != _entries_by_rel(first)["datasets/labs.csv"]["sha256"]


def test_removal_then_reappearance_gets_new_artifact_id(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add

        first = intake_add(source, "ReappearStudy")
        old_id = _entries_by_rel(first)["datasets/labs.csv"]["artifact_id"]
        old_sha = _entries_by_rel(first)["datasets/labs.csv"]["sha256"]

        (source / "datasets" / "labs.csv").unlink()
        removed_manifest = intake_add(source, "ReappearStudy")
        assert "datasets/labs.csv" not in _entries_by_rel(removed_manifest)
        removal = next(r for r in removed_manifest["removals"] if r["relative_path"] == "datasets/labs.csv")
        assert removal["artifact_id"] == old_id
        assert removal["sha256"] == old_sha

        old_link = workspace / "intake" / "ReappearStudy" / f"datasets/{old_id}__labs.csv"
        assert not old_link.exists() and not old_link.is_symlink()

        (source / "datasets" / "labs.csv").write_text("SUBJID,AGE\n9,99\n", encoding="utf-8")
        reappeared = intake_add(source, "ReappearStudy")
        new_id = _entries_by_rel(reappeared)["datasets/labs.csv"]["artifact_id"]
        assert new_id != old_id


# --- collisions --------------------------------------------------------------------------------


def _duplicate_generated_manifest(workspace: Path, old_study: str, new_study: str) -> None:
    intake_root = workspace / "intake"
    shutil.copytree(intake_root / old_study, intake_root / new_study, symlinks=True)
    os.chmod(intake_root / new_study, 0o700)
    manifest_path = intake_root / new_study / "intake_manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["study"] = new_study
    tmp = manifest_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, manifest_path)


def test_multiple_generated_matches_block_reuse_with_collision(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import IntakeManifestError, intake_add

        first = intake_add(source)
        study_a = first["study"]
        assert first["study_name_source"] == "generated"

        _duplicate_generated_manifest(workspace, study_a, "study-bbbbbbbb")

        with pytest.raises(IntakeManifestError, match="study-name-collision"):
            intake_add(source)

        assert sorted(p.name for p in (workspace / "intake").iterdir()) == sorted([study_a, "study-bbbbbbbb"])


def test_multiple_generated_matches_block_promotion_with_collision(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import IntakeManifestError, intake_add

        first = intake_add(source)
        study_a = first["study"]
        _duplicate_generated_manifest(workspace, study_a, "study-cccccccc")

        with pytest.raises(IntakeManifestError, match="study-name-collision"):
            intake_add(source, "RealStudyCollision")

        # neither pre-existing generated tree was touched, and no third tree was created
        assert sorted(p.name for p in (workspace / "intake").iterdir()) == sorted([study_a, "study-cccccccc"])


def test_destination_occupied_by_unrelated_source_fails_closed(tmp_path: Path) -> None:
    """A destination name already holding a DIFFERENT source's completed
    intake is never silently reused for promotion or merged with the new
    source -- it fails closed with a fixed code, and the original
    generated tree for THIS source is left completely untouched."""
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import IntakeManifestError, intake_add

        generated = intake_add(source)
        study_a = generated["study"]

        other_source = tmp_path / "other_source"
        _make_canonical_source(other_source)
        # occupy the destination name with an unrelated, different-source study first
        intake_add(other_source, "TakenName")

        with pytest.raises(IntakeManifestError, match="study-name-collision"):
            intake_add(source, "TakenName")

        assert (workspace / "intake" / study_a).is_dir()  # original generated tree untouched
        assert (workspace / "intake" / "TakenName").is_dir()  # unrelated study untouched


def test_ready_generated_tree_is_never_promoted(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import IntakeManifestError, intake_add

        generated = intake_add(source, support_confirmed_no_phi=False)
        # force the generated tree to "ready" by re-running it with consent granted and
        # no naming evidence available (forms-only minimal, still yields generated+ready
        # once the phi-status review item is gone) -- simplest deterministic way: patch
        # the persisted manifest's status/review_items directly to simulate a ready
        # generated tree without depending on the local LLM.
        study_a = generated["study"]
        manifest_path = workspace / "intake" / study_a / "intake_manifest.json"
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw["status"] = "ready"
        raw["review_items"] = []
        raw["errors"] = []
        tmp = manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, manifest_path)

        with pytest.raises(IntakeManifestError, match="study-name-collision"):
            intake_add(source, "ShouldNotPromote")

        assert (workspace / "intake" / study_a).is_dir()  # ready tree left exactly in place
        assert not (workspace / "intake" / "ShouldNotPromote").exists()


def test_sole_generated_match_is_reused_without_token_allocation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path):
        import phi_engine.pipeline.intake_naming as intake_naming
        from phi_engine.pipeline.intake import intake_add

        first = intake_add(source)
        study_a = first["study"]

        def _boom() -> str:
            raise AssertionError("token allocated despite a sole reusable match")

        original = intake_naming._generate_study_name
        intake_naming._generate_study_name = _boom
        try:
            second = intake_add(source)
        finally:
            intake_naming._generate_study_name = original

        assert second["study"] == study_a


def test_promotion_blocked_while_generated_study_lock_is_held(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add
        from phi_engine.utils.pipeline_lock import PipelineBusyError, pipeline_lock

        generated = intake_add(source)
        study_a = generated["study"]

        with pipeline_lock(study_a):
            with pytest.raises(PipelineBusyError):
                intake_add(source, "RealStudyBlocked")

        assert (workspace / "intake" / study_a).is_dir()
        assert not (workspace / "intake" / "RealStudyBlocked").exists()


def test_promotion_rollback_on_audit_move_failure_leaves_state_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        import phi_engine.pipeline.intake as intake_module
        from phi_engine.pipeline.intake import IntakeManifestError, intake_add

        generated = intake_add(source, support_confirmed_no_phi=False)
        study_a = generated["study"]
        old_review = workspace / "output" / study_a / "audit" / "human_review" / "intake" / "intake_review.md"
        assert old_review.is_file()

        def _boom(old_study: str, new_study: str, created_dirs: list) -> None:
            raise IntakeManifestError("intake-tree-unsafe")

        original = intake_module._move_intake_review_dir
        intake_module._move_intake_review_dir = _boom
        try:
            with pytest.raises(IntakeManifestError, match="intake-tree-unsafe"):
                intake_add(source, "PromotionRollback")
        finally:
            intake_module._move_intake_review_dir = original

        assert (workspace / "intake" / study_a).is_dir()
        assert not (workspace / "intake" / "PromotionRollback").exists()
        assert old_review.is_file()


# --- concurrency ---------------------------------------------------------------------------------


def test_intake_registry_lock_is_immediately_busy_under_contention(tmp_path: Path) -> None:
    with _workspace(tmp_path):
        from phi_engine.utils.pipeline_lock import PipelineBusyError, intake_registry_lock

        started = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with intake_registry_lock():
                started.set()
                release.wait(timeout=5)

        holder = threading.Thread(target=hold)
        holder.start()
        assert started.wait(timeout=5)
        try:
            with pytest.raises(PipelineBusyError):
                with intake_registry_lock():
                    pass
        finally:
            release.set()
            holder.join(timeout=5)


def test_intake_registry_lock_then_study_lock_nest_cleanly(tmp_path: Path) -> None:
    with _workspace(tmp_path):
        from phi_engine.utils.pipeline_lock import intake_registry_lock, is_locally_held, pipeline_lock

        with intake_registry_lock():
            with pipeline_lock("NestedStudy"):
                assert is_locally_held()
        assert not is_locally_held()


def test_concurrent_registry_scans_serialize_generated_allocation(tmp_path: Path) -> None:
    """A second caller attempting to enter the registry-protected placement
    section while the first still holds it is rejected outright (fail
    closed / non-blocking), never silently interleaved with the first
    call's scan-then-reserve sequence."""
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path):
        import phi_engine.pipeline.intake as intake_module
        from phi_engine.pipeline.intake import intake_add
        from phi_engine.utils import pipeline_lock as pl

        results: dict[str, object] = {}
        original_resolve = intake_module._resolve_registry_placement
        entered = threading.Event()
        proceed = threading.Event()

        def blocking_resolve(*args, **kwargs):
            entered.set()
            proceed.wait(timeout=5)
            return original_resolve(*args, **kwargs)

        intake_module._resolve_registry_placement = blocking_resolve
        try:
            t = threading.Thread(
                target=lambda: results.setdefault("threaded", intake_add(source, "ConcurrentStudy"))
            )
            t.start()
            assert entered.wait(timeout=5)
            with pytest.raises(pl.PipelineBusyError):
                with pl.intake_registry_lock():
                    pass
        finally:
            proceed.set()
            intake_module._resolve_registry_placement = original_resolve
            t.join(timeout=5)

        assert results["threaded"]["status"] == "ready"


# --- source immutability --------------------------------------------------------------------------


def test_source_bytes_and_metadata_are_never_modified(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_canonical_source(source)
    target = source / "datasets" / "labs.csv"
    before_stat = target.stat()
    before_bytes = target.read_bytes()
    before_mode = stat.S_IMODE(before_stat.st_mode)

    with _workspace(tmp_path):
        from phi_engine.pipeline.intake import intake_add

        intake_add(source, "ImmutableStudy")
        (source / "forms" / "consent.pdf").unlink()  # unrelated churn should never touch labs.csv
        intake_add(source, "ImmutableStudy")

    after_stat = target.stat()
    assert target.read_bytes() == before_bytes
    assert stat.S_IMODE(after_stat.st_mode) == before_mode
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert after_stat.st_ino == before_stat.st_ino
    assert after_stat.st_dev == before_stat.st_dev


def test_full_source_tree_snapshot_is_never_modified(tmp_path: Path) -> None:
    """Full-tree source immutability: every node's type, hash, size,
    mode, mtime_ns, device/inode identity, ownership, and (for symlinks)
    target must be bit-for-bit identical before and after two
    intake_add runs -- not just one dataset file."""
    import hashlib

    source = tmp_path / "source"
    _make_canonical_source(source)
    (source / "data_dictionary" / "extra.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    outside_target = tmp_path / "outside_target.pdf"
    outside_target.write_bytes(b"%PDF-1.4\n%%EOF")
    (source / "forms" / "linked.pdf").symlink_to(outside_target)

    def _snapshot(root: Path) -> dict[str, dict]:
        snap: dict[str, dict] = {}
        for path in sorted(root.rglob("*")):
            rel = str(path.relative_to(root))
            info = path.lstat()
            entry: dict = {
                "type": "symlink" if path.is_symlink() else ("dir" if path.is_dir() else "file"),
                "mode": stat.S_IMODE(info.st_mode),
                "size": info.st_size,
                "mtime_ns": info.st_mtime_ns,
                "device": info.st_dev,
                "inode": info.st_ino,
                "uid": info.st_uid,
                "gid": info.st_gid,
            }
            if entry["type"] == "symlink":
                entry["target"] = os.readlink(path)
            elif entry["type"] == "file":
                entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            snap[rel] = entry
        return snap

    before = _snapshot(source)

    with _workspace(tmp_path):
        from phi_engine.pipeline.intake import intake_add

        intake_add(source, "FullImmutableStudy")
        intake_add(source, "FullImmutableStudy")

    after = _snapshot(source)
    assert after == before


def test_intake_never_creates_a_copy_only_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add

        manifest = intake_add(source, "SymlinkOnlyStudy")
        study_dir = workspace / "intake" / "SymlinkOnlyStudy"
        for path in study_dir.rglob("*"):
            if path.name == "intake_manifest.json":
                assert path.is_file() and not path.is_symlink()
                continue
            if path.is_dir():
                continue
            assert path.is_symlink(), f"{path} must be a symlink, never a copy"


# --- malformed / missing / v2 manifest loads ----------------------------------------------------


def test_load_missing_manifest_raises_fixed_code(tmp_path: Path) -> None:
    with _workspace(tmp_path):
        from phi_engine.pipeline.intake import IntakeManifestError, load_intake_manifest

        with pytest.raises(IntakeManifestError, match="intake_manifest_missing"):
            load_intake_manifest("NeverIntaken")


def test_load_v2_schema_manifest_raises_invalid(tmp_path: Path) -> None:
    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import IntakeManifestError, load_intake_manifest

        study_dir = workspace / "intake" / "LegacyV2"
        study_dir.mkdir(parents=True)
        study_dir.chmod(0o700)
        manifest_path = study_dir / "intake_manifest.json"
        manifest_path.write_text(
            json.dumps({
                "schema": "intake-manifest/v2", "study": "LegacyV2", "source_root": None,
                "entries": {}, "duplicates": [], "errors": [], "removals": [],
            }),
            encoding="utf-8",
        )
        manifest_path.chmod(0o600)

        with pytest.raises(IntakeManifestError, match="intake_manifest_invalid"):
            load_intake_manifest("LegacyV2")


def test_load_manifest_with_broken_link_raises_invalid(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import IntakeManifestError, intake_add, load_intake_manifest

        manifest = intake_add(source, "BrokenLinkStudy")
        entry = next(iter(manifest["entries"].values()))
        link_path = workspace / "intake" / "BrokenLinkStudy" / entry["intake_path"]
        link_path.unlink()

        with pytest.raises(IntakeManifestError, match="intake_manifest_invalid"):
            load_intake_manifest("BrokenLinkStudy")


def test_intake_add_rejects_invalid_study_name(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path):
        from phi_engine.pipeline.intake import intake_add

        with pytest.raises(ValueError):
            intake_add(source, "../escape")


# --- public contract -----------------------------------------------------------------------------


def test_public_names_match_the_approved_contract() -> None:
    import inspect

    from phi_engine.pipeline import intake

    assert set(intake.__all__) == {"IntakeManifestError", "IntakeNotReadyError", "intake_add", "load_intake_manifest"}
    sig = inspect.signature(intake.intake_add)
    assert list(sig.parameters) == ["source", "study", "support_confirmed_no_phi"]
    assert sig.parameters["study"].default is None
    assert sig.parameters["support_confirmed_no_phi"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["support_confirmed_no_phi"].default is False
    sig2 = inspect.signature(intake.load_intake_manifest)
    assert list(sig2.parameters) == ["study"]

    err = intake.IntakeManifestError("intake_manifest_missing")
    assert err.code == "intake_manifest_missing"
    not_ready = intake.IntakeNotReadyError("review_required")
    assert not_ready.status == "review_required"
