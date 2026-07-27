"""Tests for phi_engine.pipeline.intake -- the atomic, symlink-only
intake-manifest/v4 reconciliation contract, plus the fixed
intake_registry_lock() primitive it depends on.

Every test drives real filesystem state under a hermetic, per-test
PHI_WORKSPACE; there is no mocking of the reconciliation loop itself.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest
from tests._workspace_harness import hermetic_phi_workspace


@contextmanager
def _workspace(
    tmp_path: Path, study: str = "V3Study", *, workspace: Path | None = None
) -> Iterator[Path]:
    with hermetic_phi_workspace(tmp_path, study, workspace=workspace) as workspace_path:
        yield workspace_path



def _make_canonical_source(root: Path) -> None:
    (root / "datasets").mkdir(parents=True)
    (root / "forms").mkdir(parents=True)
    (root / "dictionary_mapping").mkdir(parents=True)
    (root / "datasets" / "labs.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    (root / "forms" / "consent.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    (root / "dictionary_mapping" / "dict.csv").write_text("var,label\nSUBJID,Subject\n", encoding="utf-8")


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


def test_canonical_package_produces_exact_v4_ready_tree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add, load_intake_manifest

        manifest = intake_add(source, "CanonStudy")

        assert manifest["schema"] == "intake-manifest/v4"
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
        assert set(by_rel) == {"datasets/labs.csv", "forms/consent.pdf", "dictionary_mapping/dict.csv"}
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
    (source / "dictionary_mapping" / "dup1.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    (source / "dictionary_mapping" / "nested").mkdir()
    (source / "dictionary_mapping" / "nested" / "dup2.csv").write_text("x,y\n1,2\n", encoding="utf-8")

    with _workspace(tmp_path):
        from phi_engine.pipeline.intake import intake_add

        manifest = intake_add(source, "DupStudy")
        assert manifest["status"] == "ready"
        by_rel = _entries_by_rel(manifest)
        e1 = by_rel["dictionary_mapping/dup1.csv"]
        e2 = by_rel["dictionary_mapping/nested/dup2.csv"]
        assert e1["sha256"] == e2["sha256"]
        assert e1["artifact_id"] != e2["artifact_id"]
        assert e1["intake_path"] != e2["intake_path"]
        assert e2["intake_path"] == f"dictionary_mapping/nested/{e2['artifact_id']}__dup2.csv"


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
        assert "missing-support-component" in reasons  # neither forms/ nor dictionary_mapping/ present


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
        "schema": "intake-manifest/v4", "study": "S", "study_name_source": "user", "status": "ready",
        "source_root": "/src", "review_items": [], "errors": [], "removals": [],
        "entries": {
            entry_a["intake_path"]: entry_a,
            f"forms/{aid}__b.pdf": {**entry_a, "intake_path": f"forms/{aid}__b.pdf", "component": "forms", "relative_path": "forms/b.pdf", "original_path": "/src/forms/b.pdf"},
        },
    }
    with pytest.raises(intake.IntakeManifestError, match="intake_manifest_invalid"):
        intake._validate_manifest_v4(manifest, expect_study="S")

    aid2 = "a_" + "2" * 32
    dup_rel = {
        "schema": "intake-manifest/v4", "study": "S", "study_name_source": "user", "status": "ready",
        "source_root": "/src", "review_items": [], "errors": [], "removals": [],
        "entries": {
            entry_a["intake_path"]: entry_a,
            f"forms/{aid2}__a.csv": {**entry_a, "artifact_id": aid2, "intake_path": f"forms/{aid2}__a.csv", "component": "forms"},
        },
    }
    with pytest.raises(intake.IntakeManifestError, match="intake_manifest_invalid"):
        intake._validate_manifest_v4(dup_rel, expect_study="S")


def test_status_recompute_rejects_inconsistent_stored_status() -> None:
    from phi_engine.pipeline import intake

    raw = {
        "schema": "intake-manifest/v4", "study": "S", "study_name_source": "user",
        "status": "ready", "source_root": "/src", "entries": {}, "removals": [],
        "review_items": [{"path": "", "reason": "support-phi-status-required", "blocking": True}],
        "errors": [],
    }
    with pytest.raises(intake.IntakeManifestError, match="intake_manifest_invalid"):
        intake._validate_manifest_v4(raw, expect_study="S")


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
        "schema": "intake-manifest/v4", "study": "S", "study_name_source": "user", "status": "ready",
        "source_root": "/tmp/a/../b", "entries": {}, "review_items": [], "errors": [], "removals": [],
    }
    with pytest.raises(intake.IntakeManifestError, match="intake_manifest_invalid"):
        intake._validate_manifest_v4(raw, expect_study="S")

    trailing_slash = dict(raw, source_root="/tmp/a/")
    with pytest.raises(intake.IntakeManifestError, match="intake_manifest_invalid"):
        intake._validate_manifest_v4(trailing_slash, expect_study="S")

    canonical = dict(raw, source_root="/tmp/a")
    validated = intake._validate_manifest_v4(canonical, expect_study="S")
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


def test_atomic_write_on_committed_fd_survives_immediate_unlink_and_recreate(tmp_path: Path) -> None:
    """`on_committed` MUST receive the same descriptor `_atomic_write_in_dir`
    wrote the payload through, kept open across the commit rename, rather
    than a bare (device, inode) pair reopened by name afterward. Proving
    that is the whole point: as long as this call's caller holds that
    descriptor open, POSIX guarantees the kernel cannot free its inode
    number for reuse -- so even a hostile actor unlinking the committed
    file and recreating an unrelated one at the same name immediately
    afterward is provably assigned a DIFFERENT inode, never the pinned
    one, closing the TOCTOU window a closed-then-reopened identity check
    would remain exposed to."""
    from phi_engine.pipeline import intake

    study_dir = tmp_path / "study"
    study_dir.mkdir()
    study_dir.chmod(0o700)
    dir_fd = os.open(study_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        pin_holder: list[int] = []
        intake._atomic_write_in_dir(
            dir_fd, "intake_manifest.json", b'{"a":1}', 0o600, on_committed=pin_holder.append
        )
        pin_fd = pin_holder[0]
        try:
            pinned_identity = os.fstat(pin_fd)
            os.unlink("intake_manifest.json", dir_fd=dir_fd)
            fd2 = os.open(
                "intake_manifest.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dir_fd
            )
            try:
                os.write(fd2, b"UNRELATED")
            finally:
                os.close(fd2)
            recreated_identity = os.stat("intake_manifest.json", dir_fd=dir_fd)
            assert not os.path.samestat(pinned_identity, recreated_identity)
        finally:
            os.close(pin_fd)
            os.unlink("intake_manifest.json", dir_fd=dir_fd)
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

        (source / "dictionary_mapping" / "extra.csv").write_text("x,y\n1,2\n", encoding="utf-8")

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

        def write_rename_then_fail(dir_fd, filename, payload, mode, *, on_committed=None):
            if filename != "intake_review.md":
                return original_atomic_write(dir_fd, filename, payload, mode, on_committed=on_committed)
            # Reproduce the real write+fsync+rename sequence verbatim (the
            # note IS durably renamed into place), then fail exactly where
            # the real function's directory fsync would run next -- a
            # failure point no write-only injection can reach. Mirrors the
            # real `_atomic_write_in_dir` contract: `on_committed` receives
            # the SAME descriptor used to write the payload, kept open
            # across the rename rather than reopened by name afterward.
            temp_name = f".{filename}.{os.getpid()}.fsynctest.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            fd = os.open(temp_name, flags, mode, dir_fd=dir_fd)
            try:
                written = 0
                while written < len(payload):
                    written += os.write(fd, payload[written:])
                os.fsync(fd)
                os.rename(temp_name, filename, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            except BaseException:
                os.close(fd)
                raise
            if on_committed is not None:
                try:
                    on_committed(fd)
                except BaseException:
                    os.close(fd)
                    raise
            else:
                os.close(fd)
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


def test_removing_last_dataset_yields_review_required_and_stays_loadable(tmp_path: Path) -> None:
    """Removing the only dataset leaves the ``datasets/`` component empty
    -- a blocking review item, not an error -- and the directory that
    entry's now-stale link lived under must be pruned so the manifest
    THIS call persists is still loadable on the very next call."""
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add, load_intake_manifest

        first = intake_add(source, "LastDatasetStudy")
        assert first["status"] == "ready"
        assert len(first["entries"]) == 3

        (source / "datasets" / "labs.csv").unlink()
        second = intake_add(source, "LastDatasetStudy")

        assert second["status"] == "review_required"
        assert not second["errors"]
        reasons = {item["reason"] for item in second["review_items"]}
        assert "missing-component-content" in reasons
        assert "datasets/labs.csv" not in _entries_by_rel(second)

        study_dir = workspace / "intake" / "LastDatasetStudy"
        assert not (study_dir / "datasets").exists()

        # the manifest THIS call just persisted must load cleanly, not
        # raise intake_manifest_invalid over the pruned-away directory
        reloaded = load_intake_manifest("LastDatasetStudy")
        assert reloaded == second


def test_removing_last_form_keeps_ready_when_dictionary_mapping_remains(tmp_path: Path) -> None:
    """The alternative support-component requirement is satisfied by
    EITHER ``dictionary_mapping/`` or ``forms/``: removing the only form
    file while ``dictionary_mapping/`` still has content must not demote
    status, and the now-empty ``forms/`` directory must be pruned so the
    manifest stays loadable."""
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "forms").mkdir(parents=True)
    (source / "dictionary_mapping").mkdir(parents=True)
    (source / "datasets" / "labs.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    (source / "forms" / "consent.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    (source / "dictionary_mapping" / "dict.csv").write_text("var,label\nSUBJID,Subject\n", encoding="utf-8")

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add, load_intake_manifest

        first = intake_add(source, "LastFormStudy")
        assert first["status"] == "ready"
        assert len(first["entries"]) == 3

        (source / "forms" / "consent.pdf").unlink()
        second = intake_add(source, "LastFormStudy")

        assert second["status"] == "ready"
        assert not second["review_items"]
        assert not second["errors"]
        assert "forms/consent.pdf" not in _entries_by_rel(second)

        study_dir = workspace / "intake" / "LastFormStudy"
        assert not (study_dir / "forms").exists()

        reloaded = load_intake_manifest("LastFormStudy")
        assert reloaded == second


def test_pruned_directory_rollback_recreates_empty_component_dir(tmp_path: Path) -> None:
    """A reconcile attempt that successfully prunes the last entry (and
    its now-empty directory) under a component, then fails during the
    manifest commit, must recreate that pruned directory on rollback --
    not leave the tree one directory short of its exact prior shape."""
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add

        first = intake_add(source, "PruneDirRollbackStudy")
        assert first["status"] == "ready"
        study_dir = workspace / "intake" / "PruneDirRollbackStudy"
        manifest_path = study_dir / "intake_manifest.json"
        prior_bytes = manifest_path.read_bytes()
        prior_tree = _snapshot_tree(study_dir)

        (source / "forms" / "consent.pdf").unlink()  # empties forms/, the ONLY forms entry

        real_write = os.write

        def failing_write(fd, data):
            if data.startswith(b'{\n  "entries"'):
                raise OSError("simulated disk failure during update manifest write")
            return real_write(fd, data)

        os.write = failing_write
        try:
            with pytest.raises(OSError):
                intake_add(source, "PruneDirRollbackStudy")
        finally:
            os.write = real_write

        assert manifest_path.read_bytes() == prior_bytes
        assert _snapshot_tree(study_dir) == prior_tree
        assert (study_dir / "forms").is_dir()


def test_pruned_directory_restore_failure_leaves_descendant_link_retained_not_adopted(tmp_path: Path) -> None:
    """The exact narrower rollback-adoption race the closure review
    found: rollback's directory-restore step correctly leaves the
    original pruned ``datasets/nested/`` directory retained in
    quarantine when an unrelated actor has since reclaimed that name --
    but it must NOT then descend into that replacement directory and
    adopt the pruned ``a.csv`` link inside it. Both the pruned
    directory and its pruned link must stay exactly retained in
    quarantine, and the replacement directory's own marker file and
    mode must be completely untouched. ``datasets/b.csv`` stays present
    throughout so the component-content requirement is never at issue
    -- the ONLY thing under test is the occupied-ancestor restore race."""
    source = tmp_path / "source"
    (source / "datasets" / "nested").mkdir(parents=True)
    (source / "forms").mkdir(parents=True)
    (source / "dictionary_mapping").mkdir(parents=True)
    (source / "datasets" / "b.csv").write_text("SUBJID,AGE\n2,41\n", encoding="utf-8")
    (source / "datasets" / "nested" / "a.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    (source / "forms" / "consent.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    (source / "dictionary_mapping" / "dict.csv").write_text("var,label\nSUBJID,Subject\n", encoding="utf-8")

    with _workspace(tmp_path) as workspace:
        import phi_engine.pipeline.intake as intake_module
        from phi_engine.pipeline.intake import intake_add

        first = intake_add(source, "OccupiedAncestorStudy")
        assert first["status"] == "ready"
        study_dir = workspace / "intake" / "OccupiedAncestorStudy"

        (source / "datasets" / "nested" / "a.csv").unlink()  # empties nested/, the ONLY entry there

        real_write = os.write

        def failing_write(fd, data):
            if data.startswith(b'{\n  "entries"'):
                raise OSError("simulated disk failure during update manifest write")
            return real_write(fd, data)

        original_rename = intake_module._renameat2_noreplace
        injected = {"done": False}

        def racing_rename(old_dir_fd, old, new_dir_fd, new):
            # Fires exactly once: rollback's directory-restore step
            # renaming the quarantined "dir.*" node back to "nested"
            # (under "datasets/"). Occupy "nested" with unrelated
            # content first, so the no-replace restore rename fails
            # closed on EEXIST.
            if not injected["done"] and old.startswith("dir.") and new == "nested":
                injected["done"] = True
                os.mkdir(new, 0o755, dir_fd=new_dir_fd)
                sub_fd = os.open(new, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=new_dir_fd)
                try:
                    marker_fd = os.open(
                        "marker.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=sub_fd
                    )
                    try:
                        os.write(marker_fd, b"UNRELATED")
                    finally:
                        os.close(marker_fd)
                finally:
                    os.close(sub_fd)
            return original_rename(old_dir_fd, old, new_dir_fd, new)

        os.write = failing_write
        intake_module._renameat2_noreplace = racing_rename
        try:
            with pytest.raises(OSError):
                intake_add(source, "OccupiedAncestorStudy")
        finally:
            os.write = real_write
            intake_module._renameat2_noreplace = original_rename

        assert injected["done"]

        nested_dir = study_dir / "datasets" / "nested"
        assert nested_dir.is_dir()
        assert stat.S_IMODE(nested_dir.stat().st_mode) == 0o755  # replacement mode untouched
        marker_path = nested_dir / "marker.txt"
        assert marker_path.read_bytes() == b"UNRELATED"  # marker untouched
        assert [p.name for p in nested_dir.iterdir()] == ["marker.txt"]  # nothing adopted into it

        quarantine_root = workspace / ".intake_quarantine"
        retained_dirs = list(quarantine_root.glob("dir.*"))
        retained_links = list(quarantine_root.glob("link.*"))
        assert len(retained_dirs) == 1  # the pruned nested/ dir stays retained -- restore failed closed
        assert len(retained_links) == 1  # the pruned a.csv link stays retained too -- never adopted


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



def test_generated_collision_with_ready_different_source_never_reconciles(tmp_path: Path) -> None:
    """A freshly allocated generated token that happens to collide with
    an unrelated, already-``ready`` different-source study must fail
    ``study-name-collision`` before ever reconciling -- never adopting
    that tree, never pruning its links, never touching a single byte of
    either source or the existing study's manifest/tree."""
    source_a = tmp_path / "source_a"
    _make_canonical_source(source_a)
    source_b = tmp_path / "source_b"
    (source_b / "datasets").mkdir(parents=True)
    (source_b / "forms").mkdir(parents=True)
    (source_b / "dictionary_mapping").mkdir(parents=True)
    (source_b / "datasets" / "other.csv").write_text("X,Y\n1,2\n", encoding="utf-8")
    (source_b / "forms" / "other.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    (source_b / "dictionary_mapping" / "other.csv").write_text("var,label\nX,Xval\n", encoding="utf-8")

    with _workspace(tmp_path) as workspace:
        import phi_engine.pipeline.intake_naming as intake_naming_module
        from phi_engine.pipeline.intake import IntakeManifestError, intake_add

        first = intake_add(source_a)
        study_a = first["study"]
        assert first["study_name_source"] == "generated"

        manifest_path = workspace / "intake" / study_a / "intake_manifest.json"
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw["status"] = "ready"
        raw["review_items"] = []
        raw["errors"] = []
        tmp = manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, manifest_path)

        before_a_manifest = manifest_path.read_bytes()
        before_a_tree = _snapshot_tree(workspace / "intake" / study_a)
        before_source_a = _snapshot_tree(source_a)
        before_source_b = _snapshot_tree(source_b)

        original_generate = intake_naming_module._generate_study_name
        intake_naming_module._generate_study_name = lambda: study_a
        try:
            with pytest.raises(IntakeManifestError, match="study-name-collision"):
                intake_add(source_b)
        finally:
            intake_naming_module._generate_study_name = original_generate

        assert manifest_path.read_bytes() == before_a_manifest
        assert _snapshot_tree(workspace / "intake" / study_a) == before_a_tree
        assert _snapshot_tree(source_a) == before_source_a
        assert _snapshot_tree(source_b) == before_source_b
        assert sorted(p.name for p in (workspace / "intake").iterdir()) == [study_a]


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


# --- hostile-namespace check/use races (atomic quarantine/rename) --------------------------------


def test_promotion_race_destination_created_just_before_atomic_rename_fails_closed(tmp_path: Path) -> None:
    """Deterministic injection of the exact race the security review
    found: a destination directory materializes in the instant between
    a caller's absence check and the promotion rename itself. The
    atomic no-replace rename must fail closed with study-name-collision
    -- never silently replace the racing directory, never leave the old
    generated tree half-renamed."""
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        import phi_engine.pipeline.intake as intake_module
        from phi_engine.pipeline.intake import IntakeManifestError, intake_add

        generated = intake_add(source, support_confirmed_no_phi=False)
        study_a = generated["study"]

        original_rename = intake_module._renameat2_noreplace
        injected = {"done": False}

        def racing_rename(old_dir_fd, old, new_dir_fd, new):
            if not injected["done"] and old == study_a and new == "RacedPromotion":
                injected["done"] = True
                os.mkdir(new, 0o700, dir_fd=new_dir_fd)  # adversary wins the race first
            return original_rename(old_dir_fd, old, new_dir_fd, new)

        intake_module._renameat2_noreplace = racing_rename
        try:
            with pytest.raises(IntakeManifestError, match="study-name-collision"):
                intake_add(source, "RacedPromotion")
        finally:
            intake_module._renameat2_noreplace = original_rename

        assert injected["done"]
        assert (workspace / "intake" / study_a).is_dir()
        assert (workspace / "intake" / "RacedPromotion").is_dir()
        assert list((workspace / "intake" / "RacedPromotion").iterdir()) == []


def test_renameat2_noreplace_maps_shared_unavailable_to_intake_tree_unsafe() -> None:
    """``intake._renameat2_noreplace`` is the one wrapper every intake
    call site above depends on to translate the shared primitive's
    platform-independent :class:`AtomicRenameUnavailable` into intake's
    own fixed, value-free ``intake-tree-unsafe`` code -- never a raw
    ``AtomicRenameUnavailable`` escaping into a caller that only knows
    about ``IntakeManifestError``. The mapping must also chain with
    ``from None`` -- a caller inspecting ``__cause__`` must never see
    the underlying platform-availability exception."""
    import phi_engine.pipeline.intake as intake_module
    from phi_engine.pipeline.intake import IntakeManifestError
    from phi_engine.utils.atomic_fs import AtomicRenameUnavailable

    def _always_unavailable(old_dir_fd, old, new_dir_fd, new):
        raise AtomicRenameUnavailable()

    original = intake_module._shared_renameat2_noreplace
    intake_module._shared_renameat2_noreplace = _always_unavailable
    try:
        with pytest.raises(IntakeManifestError) as excinfo:
            intake_module._renameat2_noreplace(0, "old", 0, "new")
    finally:
        intake_module._shared_renameat2_noreplace = original

    assert excinfo.value.code == "intake-tree-unsafe"
    assert excinfo.value.__cause__ is None


def test_renameat2_noreplace_passes_through_other_shared_outcomes_unchanged() -> None:
    """Every other outcome the shared primitive can raise (a specific
    ``FileExistsError``/``FileNotFoundError``, or an opaque ``OSError``)
    must reach the caller completely unchanged -- only
    ``AtomicRenameUnavailable`` gets remapped."""
    import phi_engine.pipeline.intake as intake_module

    for outcome in (FileExistsError(17, "exists"), FileNotFoundError(2, "absent"), OSError(5, "io error")):

        def _raises(old_dir_fd, old, new_dir_fd, new, _outcome=outcome):
            raise _outcome

        original = intake_module._shared_renameat2_noreplace
        intake_module._shared_renameat2_noreplace = _raises
        try:
            with pytest.raises(type(outcome)) as excinfo:
                intake_module._renameat2_noreplace(0, "old", 0, "new")
        finally:
            intake_module._shared_renameat2_noreplace = original
        assert excinfo.value is outcome


def test_promotion_fails_closed_as_intake_tree_unsafe_when_rename_primitive_unavailable(tmp_path: Path) -> None:
    """End-to-end: when the shared atomic-rename primitive reports the
    platform cannot provide the no-replace guarantee at all (not merely
    ``EEXIST`` from a racing occupant, which is the distinct
    ``study-name-collision`` outcome exercised elsewhere), promotion of
    an already-generated study to a user-chosen name must fail closed
    with ``intake-tree-unsafe`` and leave the generated tree exactly as
    it was -- never a raw ``AtomicRenameUnavailable`` escaping
    ``intake_add``, never a partial rename."""
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        import phi_engine.pipeline.intake as intake_module
        from phi_engine.pipeline.intake import IntakeManifestError, intake_add
        from phi_engine.utils.atomic_fs import AtomicRenameUnavailable

        generated = intake_add(source, support_confirmed_no_phi=False)
        study_a = generated["study"]

        original_shared_rename = intake_module._shared_renameat2_noreplace
        injected = {"done": False}

        def unavailable_for_promotion(old_dir_fd, old, new_dir_fd, new):
            if not injected["done"] and old == study_a and new == "UnavailablePromotion":
                injected["done"] = True
                raise AtomicRenameUnavailable()
            return original_shared_rename(old_dir_fd, old, new_dir_fd, new)

        intake_module._shared_renameat2_noreplace = unavailable_for_promotion
        try:
            with pytest.raises(IntakeManifestError, match="intake-tree-unsafe"):
                intake_add(source, "UnavailablePromotion")
        finally:
            intake_module._shared_renameat2_noreplace = original_shared_rename

        assert injected["done"]
        assert (workspace / "intake" / study_a).is_dir()
        assert not (workspace / "intake" / "UnavailablePromotion").exists()


def test_stale_link_race_swaps_content_just_before_quarantine_leaves_unrelated_untouched(tmp_path: Path) -> None:
    """Deterministic injection of the exact race the security review
    found: a hostile actor swaps the stale symlink for unrelated
    content in the instant before this attempt's quarantine rename.
    The quarantine must still grab and verify whatever is actually
    there, discover the mismatch, and restore the unrelated content
    untouched rather than deleting it."""
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        import phi_engine.pipeline.intake as intake_module
        from phi_engine.pipeline.intake import intake_add

        first = intake_add(source, "RaceStaleStudy")
        by_rel = _entries_by_rel(first)
        dataset_key = by_rel["datasets/labs.csv"]["intake_path"]
        basename = dataset_key.rsplit("/", 1)[-1]
        study_dir = workspace / "intake" / "RaceStaleStudy"
        link_path = study_dir / dataset_key

        (source / "datasets" / "labs.csv").unlink()  # makes the entry stale

        original_rename = intake_module._renameat2_noreplace
        injected = {"done": False}

        def racing_rename(old_dir_fd, old, new_dir_fd, new):
            if not injected["done"] and old == basename and new.startswith("link."):
                injected["done"] = True
                os.unlink(old, dir_fd=old_dir_fd)
                fd = os.open(old, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=old_dir_fd)
                try:
                    os.write(fd, b"UNRELATED")
                finally:
                    os.close(fd)
            return original_rename(old_dir_fd, old, new_dir_fd, new)

        intake_module._renameat2_noreplace = racing_rename
        try:
            second = intake_add(source, "RaceStaleStudy")
        finally:
            intake_module._renameat2_noreplace = original_rename

        assert injected["done"]
        assert second["status"] == "failed"
        assert any(e["reason"] == "intake-tree-unsafe" for e in second["errors"])
        assert link_path.is_file() and not link_path.is_symlink()
        assert link_path.read_bytes() == b"UNRELATED"


def test_created_link_rollback_race_leaves_swapped_content_untouched(tmp_path: Path) -> None:
    """A manifest-write failure triggers rollback of every symlink this
    attempt created; a hostile actor swapping in unrelated content at
    one of those link paths in the instant before rollback's quarantine
    rename must survive untouched -- rollback deletes only by proven
    identity, never by name alone."""
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        import phi_engine.pipeline.intake as intake_module
        from phi_engine.pipeline.intake import intake_add

        real_write = os.write

        def failing_write(fd, data):
            if data.startswith(b'{\n  "entries"'):
                raise OSError("simulated disk failure during initial manifest write")
            return real_write(fd, data)

        original_rename = intake_module._renameat2_noreplace
        injected = {"done": False}

        def racing_rename(old_dir_fd, old, new_dir_fd, new):
            if not injected["done"] and old.endswith("labs.csv") and new.startswith("link."):
                injected["done"] = True
                os.unlink(old, dir_fd=old_dir_fd)
                fd = os.open(old, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=old_dir_fd)
                try:
                    os.write(fd, b"UNRELATED")
                finally:
                    os.close(fd)
            return original_rename(old_dir_fd, old, new_dir_fd, new)

        os.write = failing_write
        intake_module._renameat2_noreplace = racing_rename
        try:
            with pytest.raises(OSError):
                intake_add(source, "RaceRollbackStudy")
        finally:
            os.write = real_write
            intake_module._renameat2_noreplace = original_rename

        assert injected["done"]
        study_dir = workspace / "intake" / "RaceRollbackStudy"
        assert not (study_dir / "intake_manifest.json").exists()
        matches = list(study_dir.rglob("*labs.csv"))
        assert len(matches) == 1
        assert matches[0].is_file() and not matches[0].is_symlink()
        assert matches[0].read_bytes() == b"UNRELATED"


def test_review_note_restore_race_leaves_swapped_content_untouched(tmp_path: Path) -> None:
    """A manifest-write failure AFTER the review note already committed
    triggers restore-to-absence; a hostile actor swapping in unrelated
    content at the note path in the instant before that restore's
    quarantine rename must survive untouched rather than be deleted."""
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "datasets" / "unsupported.json").write_text("{}", encoding="utf-8")

    with _workspace(tmp_path) as workspace:
        import phi_engine.pipeline.intake as intake_module
        from phi_engine.pipeline.intake import intake_add

        real_write = os.write

        def failing_write(fd, data):
            if data.startswith(b'{\n  "entries"'):
                raise OSError("simulated disk failure during manifest write after note committed")
            return real_write(fd, data)

        original_rename = intake_module._renameat2_noreplace
        injected = {"done": False}

        def racing_rename(old_dir_fd, old, new_dir_fd, new):
            if not injected["done"] and old == "intake_review.md" and new.startswith("file."):
                injected["done"] = True
                os.unlink(old, dir_fd=old_dir_fd)
                fd = os.open(old, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=old_dir_fd)
                try:
                    os.write(fd, b"UNRELATED NOTE")
                finally:
                    os.close(fd)
            return original_rename(old_dir_fd, old, new_dir_fd, new)

        os.write = failing_write
        intake_module._renameat2_noreplace = racing_rename
        try:
            with pytest.raises(OSError):
                intake_add(source, "RaceNoteStudy")
        finally:
            os.write = real_write
            intake_module._renameat2_noreplace = original_rename

        assert injected["done"]
        note_path = workspace / "output" / "RaceNoteStudy" / "audit" / "human_review" / "intake" / "intake_review.md"
        assert note_path.is_file()
        assert note_path.read_bytes() == b"UNRELATED NOTE"


def test_fresh_reservation_race_injected_after_absence_check_fails_closed_untouched(tmp_path: Path) -> None:
    """The exact security-review race: a foreign, unrelated intake tree
    for a DIFFERENT source materializes at the chosen destination name
    in the instant between this call's absence check and its actual
    reservation. The reservation must be one atomic state transition --
    a bare, check-free ``mkdir`` treating any resulting EEXIST as
    study-name-collision -- never adopt whatever is found there, never
    reconcile it, never touch a single byte of the foreign tree or
    either source."""
    source_a = tmp_path / "source_a"
    _make_canonical_source(source_a)
    foreign_source = tmp_path / "foreign_source"
    (foreign_source / "datasets").mkdir(parents=True)
    (foreign_source / "forms").mkdir(parents=True)
    (foreign_source / "dictionary_mapping").mkdir(parents=True)
    (foreign_source / "datasets" / "f.csv").write_text("A,B\n1,2\n", encoding="utf-8")
    (foreign_source / "forms" / "f.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    (foreign_source / "dictionary_mapping" / "f.csv").write_text("var,label\nA,Aval\n", encoding="utf-8")

    with _workspace(tmp_path) as workspace:
        import phi_engine.pipeline.intake as intake_module
        from phi_engine.pipeline.intake import IntakeManifestError, intake_add

        foreign = intake_add(foreign_source, "ForeignTemplate")
        assert foreign["study"] == "ForeignTemplate"
        template_dir = workspace / "intake" / "ForeignTemplate"
        before_source_a = _snapshot_tree(source_a)
        before_foreign_source = _snapshot_tree(foreign_source)

        original_absent = intake_module._study_dir_absent
        injected = {"done": False}

        def racing_absent(study_name: str) -> bool:
            answer = original_absent(study_name)
            if not injected["done"] and study_name == "TargetStudy" and answer:
                injected["done"] = True
                shutil.copytree(template_dir, workspace / "intake" / "TargetStudy", symlinks=True)
                os.chmod(workspace / "intake" / "TargetStudy", 0o700)
            return answer

        intake_module._study_dir_absent = racing_absent
        try:
            with pytest.raises(IntakeManifestError, match="study-name-collision"):
                intake_add(source_a, "TargetStudy")
        finally:
            intake_module._study_dir_absent = original_absent

        assert injected["done"]
        injected_dir = workspace / "intake" / "TargetStudy"
        assert injected_dir.is_dir()
        assert (injected_dir / "intake_manifest.json").read_bytes() == (
            template_dir / "intake_manifest.json"
        ).read_bytes()
        assert sorted(p.name for p in (workspace / "intake").iterdir()) == sorted(["ForeignTemplate", "TargetStudy"])
        assert _snapshot_tree(source_a) == before_source_a
        assert _snapshot_tree(foreign_source) == before_foreign_source


def test_reused_generated_match_source_swap_between_scan_and_pin_fails_closed(tmp_path: Path) -> None:
    """The registry scan finds a sole generated match by its
    source_root at scan time; if a hostile actor swaps that
    destination's manifest source_root before the pinned descriptor is
    actually read during reconcile, the reuse must be rejected as
    study-name-collision -- never silently adopted -- because the
    'reusable' guarantee has to be reproven on the pinned descriptor,
    not trusted from the earlier scan alone."""
    source_a = tmp_path / "source_a"
    _make_canonical_source(source_a)
    source_c = tmp_path / "source_c"
    (source_c / "datasets").mkdir(parents=True)
    (source_c / "forms").mkdir(parents=True)
    (source_c / "dictionary_mapping").mkdir(parents=True)
    (source_c / "datasets" / "c.csv").write_text("A,B\n1,2\n", encoding="utf-8")
    (source_c / "forms" / "c.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    (source_c / "dictionary_mapping" / "c.csv").write_text("var,label\nA,Aval\n", encoding="utf-8")

    with _workspace(tmp_path) as workspace:
        import phi_engine.pipeline.intake as intake_module
        from phi_engine.pipeline.intake import IntakeManifestError, intake_add

        first = intake_add(source_a)
        study_a = first["study"]
        assert first["study_name_source"] == "generated"
        manifest_path = workspace / "intake" / study_a / "intake_manifest.json"

        canonical_c = intake_module.intake_naming.canonical_source_root(source_c)

        original_scan = intake_module._scan_generated_manifests_for_source
        swapped = {"done": False}

        def racing_scan(canonical_source_arg):
            result = original_scan(canonical_source_arg)
            if not swapped["done"] and [m.study for m in result] == [study_a]:
                swapped["done"] = True
                raw = json.loads(manifest_path.read_text(encoding="utf-8"))
                raw["source_root"] = canonical_c
                for entry in raw["entries"].values():
                    entry["original_path"] = f"{canonical_c}/{entry['relative_path']}"
                tmp = manifest_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
                tmp.chmod(0o600)
                os.replace(tmp, manifest_path)
            return result

        intake_module._scan_generated_manifests_for_source = racing_scan
        try:
            with pytest.raises(IntakeManifestError, match="study-name-collision"):
                intake_add(source_a)
        finally:
            intake_module._scan_generated_manifests_for_source = original_scan

        assert swapped["done"]
        after_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert after_raw["source_root"] == canonical_c  # the hostile swap itself -- our code never touched it further
        assert sorted(p.name for p in (workspace / "intake").iterdir()) == [study_a]


def test_reused_generated_match_directory_identity_swap_between_scan_and_pin_fails_closed(tmp_path: Path) -> None:
    """The registry scan pins the exact scanned study-directory AND
    manifest device/inode identity, not merely its declared
    source_root string. If a hostile actor replaces the destination
    directory with a byte-for-byte identical same-source copy (a fresh
    inode, unchanged source_root) in the instant before the pinned
    descriptor is actually opened during reconcile, the reuse must be
    rejected as study-name-collision -- never silently adopted --
    because the scanned directory itself has to be reproven on the
    pinned descriptor, never trusted from the earlier scan alone."""
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        import phi_engine.pipeline.intake as intake_module
        from phi_engine.pipeline.intake import IntakeManifestError, intake_add

        first = intake_add(source)
        study_a = first["study"]
        assert first["study_name_source"] == "generated"
        study_dir = workspace / "intake" / study_a
        manifest_path = study_dir / "intake_manifest.json"
        before_bytes = manifest_path.read_bytes()
        before_ino = study_dir.stat().st_ino

        original_scan = intake_module._scan_generated_manifests_for_source
        swapped: dict[str, Any] = {"done": False}

        def racing_scan(canonical_source_arg):
            result = original_scan(canonical_source_arg)
            if not swapped["done"] and [m.study for m in result] == [study_a]:
                swapped["done"] = True
                shadow = workspace / "intake" / f"{study_a}.shadow"
                shutil.copytree(study_dir, shadow, symlinks=True)
                shutil.rmtree(study_dir)
                shadow.rename(study_dir)
                os.chmod(study_dir, 0o700)
                swapped["after_ino"] = study_dir.stat().st_ino
            return result

        intake_module._scan_generated_manifests_for_source = racing_scan
        try:
            with pytest.raises(IntakeManifestError, match="study-name-collision"):
                intake_add(source)
        finally:
            intake_module._scan_generated_manifests_for_source = original_scan

        assert swapped["done"]
        assert swapped["after_ino"] != before_ino  # the swap really did replace the directory's identity
        assert sorted(p.name for p in (workspace / "intake").iterdir()) == [study_a]
        assert manifest_path.read_bytes() == before_bytes  # the swapped-in replacement was never mutated


def test_stale_link_quarantine_is_retained_not_deleted_lifecycle(tmp_path: Path) -> None:
    """Pruning a legitimately stale symlink (the ordinary, non-hostile
    case) never unlinks it by name -- POSIX has no conditional-unlink
    primitive that could prove, at the instant of deletion, that a
    mutable name still holds the exact object just verified. Instead
    the verified symlink is atomically retained in the shared protected
    quarantine directory (BASE_DIR/.intake_quarantine), completely
    outside both the registry scan's and the study's own tree-
    invariant walk, so it can never resurface as an unexpected node."""
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import intake_add, load_intake_manifest

        first = intake_add(source, "QuarantineLifecycleStudy")
        assert first["status"] == "ready"
        expected_target = _entries_by_rel(first)["datasets/labs.csv"]["original_path"]

        (source / "datasets" / "labs.csv").unlink()
        second = intake_add(source, "QuarantineLifecycleStudy")
        assert second["status"] == "review_required"

        study_dir = workspace / "intake" / "QuarantineLifecycleStudy"
        assert not (study_dir / "datasets").exists()

        quarantine_root = workspace / ".intake_quarantine"
        assert quarantine_root.is_dir()
        assert stat.S_IMODE(quarantine_root.stat().st_mode) == 0o700
        retained_links = [p for p in quarantine_root.iterdir() if p.name.startswith("link.")]
        assert len(retained_links) == 1
        assert retained_links[0].is_symlink()
        assert os.readlink(retained_links[0]) == expected_target

        reloaded = load_intake_manifest("QuarantineLifecycleStudy")
        assert reloaded == second


def test_stale_directory_swap_just_before_quarantine_leaves_unrelated_untouched(tmp_path: Path) -> None:
    """The exact security-review race for stale directory pruning: a
    hostile actor swaps the verified-empty, owned-and-recorded
    ``forms/`` directory for unrelated content in the instant before
    this attempt's quarantine rename. The quarantine must still grab
    and verify whatever is actually there against the descriptor-
    recorded identity, discover the mismatch, and restore the unrelated
    content untouched rather than deleting or losing it."""
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "forms").mkdir(parents=True)
    (source / "dictionary_mapping").mkdir(parents=True)
    (source / "datasets" / "labs.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    (source / "forms" / "consent.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    (source / "dictionary_mapping" / "dict.csv").write_text("var,label\nSUBJID,Subject\n", encoding="utf-8")

    with _workspace(tmp_path) as workspace:
        import phi_engine.pipeline.intake as intake_module
        from phi_engine.pipeline.intake import intake_add

        first = intake_add(source, "StaleDirRaceStudy")
        assert first["status"] == "ready"

        (source / "forms" / "consent.pdf").unlink()  # empties forms/, triggering directory prune

        original_rename = intake_module._renameat2_noreplace
        injected = {"done": False}

        def racing_rename(old_dir_fd, old, new_dir_fd, new):
            if not injected["done"] and old == "forms" and new.startswith("dir."):
                injected["done"] = True
                os.rmdir(old, dir_fd=old_dir_fd)
                fd = os.open(old, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=old_dir_fd)
                try:
                    os.write(fd, b"UNRELATED-DIR-SWAP")
                finally:
                    os.close(fd)
            return original_rename(old_dir_fd, old, new_dir_fd, new)

        intake_module._renameat2_noreplace = racing_rename
        try:
            second = intake_add(source, "StaleDirRaceStudy")
        finally:
            intake_module._renameat2_noreplace = original_rename

        assert injected["done"]
        assert second["status"] == "failed"
        assert any(e["reason"] == "intake-tree-unsafe" for e in second["errors"])
        forms_path = workspace / "intake" / "StaleDirRaceStudy" / "forms"
        assert forms_path.is_file() and not forms_path.is_symlink()
        assert forms_path.read_bytes() == b"UNRELATED-DIR-SWAP"


def test_created_directory_rollback_race_leaves_swapped_content_untouched(tmp_path: Path) -> None:
    """A manifest-write failure triggers rollback of every directory
    this attempt freshly created; a hostile actor swapping in unrelated
    content at one of those directory paths in the instant before
    rollback's quarantine rename must survive untouched -- rollback
    removes only by proven DEVICE/INODE identity, never by name alone."""
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        import phi_engine.pipeline.intake as intake_module
        from phi_engine.pipeline.intake import intake_add

        first = intake_add(source, "CreatedDirRaceStudy")
        assert first["status"] == "ready"
        study_dir = workspace / "intake" / "CreatedDirRaceStudy"
        manifest_path = study_dir / "intake_manifest.json"
        prior_bytes = manifest_path.read_bytes()

        (source / "datasets" / "nested").mkdir()
        (source / "datasets" / "nested" / "extra.csv").write_text("A,B\n1,2\n", encoding="utf-8")

        real_write = os.write

        def failing_write(fd, data):
            if data.startswith(b'{\n  "entries"'):
                raise OSError("simulated disk failure during update manifest write")
            return real_write(fd, data)

        original_rename = intake_module._renameat2_noreplace
        injected = {"done": False}

        def racing_rename(old_dir_fd, old, new_dir_fd, new):
            if not injected["done"] and old == "nested" and new.startswith("dir."):
                injected["done"] = True
                os.rmdir(old, dir_fd=old_dir_fd)
                fd = os.open(old, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=old_dir_fd)
                try:
                    os.write(fd, b"UNRELATED-CREATED-DIR")
                finally:
                    os.close(fd)
            return original_rename(old_dir_fd, old, new_dir_fd, new)

        os.write = failing_write
        intake_module._renameat2_noreplace = racing_rename
        try:
            with pytest.raises(OSError):
                intake_add(source, "CreatedDirRaceStudy")
        finally:
            os.write = real_write
            intake_module._renameat2_noreplace = original_rename

        assert injected["done"]
        assert manifest_path.read_bytes() == prior_bytes
        nested_path = study_dir / "datasets" / "nested"
        assert nested_path.is_file() and not nested_path.is_symlink()
        assert nested_path.read_bytes() == b"UNRELATED-CREATED-DIR"


def test_manifest_rollback_same_bytes_different_inode_impostor_is_untouched(tmp_path: Path) -> None:
    """A hostile actor can replace this attempt's own just-committed
    manifest with a byte-for-byte IDENTICAL copy at a fresh inode in
    the instant before a later failure triggers rollback. Because the
    rollback gate proves identity by DEVICE/INODE -- captured at this
    attempt's own commit time -- never by content, the impostor is
    recognized as NOT this attempt's own write and is left completely
    untouched, never blindly replaced with the prior manifest bytes."""
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        import phi_engine.pipeline.intake as intake_module
        from phi_engine.pipeline.intake import intake_add

        first = intake_add(source, "ImpostorStudy")
        assert first["status"] == "ready"
        manifest_path = workspace / "intake" / "ImpostorStudy" / "intake_manifest.json"
        prior_bytes = manifest_path.read_bytes()

        (source / "dictionary_mapping" / "extra.csv").write_text("x,y\n1,2\n", encoding="utf-8")

        original_atomic_write = intake_module._atomic_write_in_dir
        impostor_holder: dict[str, bytes] = {}

        def swap_after_commit(dir_fd, filename, payload, mode, *, on_committed=None):
            if filename != "intake_manifest.json":
                return original_atomic_write(dir_fd, filename, payload, mode, on_committed=on_committed)
            original_atomic_write(dir_fd, filename, payload, mode, on_committed=on_committed)
            impostor_holder["bytes"] = payload
            tmp_name = ".impostor.tmp"
            fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dir_fd)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
            os.rename(tmp_name, filename, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            raise OSError("simulated failure after impostor swap")

        intake_module._atomic_write_in_dir = swap_after_commit
        try:
            with pytest.raises(OSError):
                intake_add(source, "ImpostorStudy")
        finally:
            intake_module._atomic_write_in_dir = original_atomic_write

        after_bytes = manifest_path.read_bytes()
        assert after_bytes == impostor_holder["bytes"]
        assert after_bytes != prior_bytes


def test_manifest_rollback_prior_absent_impostor_content_is_preserved_in_quarantine_not_destroyed(
    tmp_path: Path,
) -> None:
    """Concrete reproduction from the security review: a hostile actor
    replaces the just-committed fresh manifest with unrelated content
    (different bytes AND a fresh inode) in the instant before a later
    failure triggers rollback-to-absence. The impostor is proven not to
    be this attempt's own write and is preserved EXACTLY where the
    hostile actor put it -- never destroyed by a blind ``os.unlink``.
    Because the reservation directory is consequently NOT empty (the
    preserved impostor still occupies it), the directory itself is left
    in place too -- rollback fails closed without clobber rather than
    forcing a "clean" removal that would require moving or hiding the
    unrelated content. Every directory THIS attempt itself created
    (now genuinely empty again, since only ITS OWN links lived there)
    is still safely reclaimed into the retained quarantine directory."""
    source = tmp_path / "source"
    _make_canonical_source(source)

    with _workspace(tmp_path) as workspace:
        import phi_engine.pipeline.intake as intake_module
        from phi_engine.pipeline.intake import intake_add

        original_atomic_write = intake_module._atomic_write_in_dir
        impostor_bytes = b'{"impostor": "UNRELATED-MANIFEST-CONTENT"}'

        def swap_after_commit(dir_fd, filename, payload, mode, *, on_committed=None):
            if filename != "intake_manifest.json":
                return original_atomic_write(dir_fd, filename, payload, mode, on_committed=on_committed)
            original_atomic_write(dir_fd, filename, payload, mode, on_committed=on_committed)
            tmp_name = ".impostor.tmp"
            fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dir_fd)
            try:
                os.write(fd, impostor_bytes)
            finally:
                os.close(fd)
            os.rename(tmp_name, filename, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            raise OSError("simulated failure after impostor swap on fresh reservation")

        intake_module._atomic_write_in_dir = swap_after_commit
        try:
            with pytest.raises(OSError):
                intake_add(source, "FreshImpostorStudy")
        finally:
            intake_module._atomic_write_in_dir = original_atomic_write

        study_dir = workspace / "intake" / "FreshImpostorStudy"
        retained_manifest = study_dir / "intake_manifest.json"
        assert retained_manifest.is_file()
        assert retained_manifest.read_bytes() == impostor_bytes  # preserved, never destroyed by unlink

        # this attempt's own datasets/forms/dictionary_mapping directories
        # -- genuinely empty once their own just-created links were
        # quarantined -- are still safely reclaimed
        quarantine_root = workspace / ".intake_quarantine"
        retained_dirs = list(quarantine_root.glob("dir.*"))
        assert len(retained_dirs) == 3


def test_review_note_rollback_same_bytes_different_inode_impostor_is_untouched(tmp_path: Path) -> None:
    """A hostile actor can replace this attempt's own just-committed
    review note with a byte-for-byte IDENTICAL copy at a fresh inode in
    the instant before a later manifest-write failure triggers note
    rollback. Because the rollback gate proves identity by DEVICE/INODE
    -- captured at this attempt's own commit time -- never by content,
    the impostor is recognized as NOT this attempt's own write and is
    left completely untouched, never blindly overwritten with the
    prior note content."""
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "datasets" / "unsupported.json").write_text("{}", encoding="utf-8")

    with _workspace(tmp_path) as workspace:
        import phi_engine.pipeline.intake as intake_module
        from phi_engine.pipeline.intake import intake_add

        first = intake_add(source, "NoteImpostorStudy")
        assert first["status"] == "review_required"
        note_path = (
            workspace / "output" / "NoteImpostorStudy" / "audit" / "human_review" / "intake" / "intake_review.md"
        )
        prior_note_bytes = note_path.read_bytes()

        (source / "datasets" / "unsupported2.json").write_text("{}", encoding="utf-8")

        real_write = os.write

        def failing_write(fd, data):
            if data.startswith(b'{\n  "entries"'):
                raise OSError("simulated disk failure during manifest write after note committed")
            return real_write(fd, data)

        original_atomic_write = intake_module._atomic_write_in_dir
        impostor_holder: dict[str, bytes] = {}

        def swap_note_after_commit(dir_fd, filename, payload, mode, *, on_committed=None):
            if filename != "intake_review.md":
                return original_atomic_write(dir_fd, filename, payload, mode, on_committed=on_committed)
            original_atomic_write(dir_fd, filename, payload, mode, on_committed=on_committed)
            impostor_holder["bytes"] = payload
            tmp_name = ".impostor_note.tmp"
            fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dir_fd)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
            os.rename(tmp_name, filename, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            return None

        os.write = failing_write
        intake_module._atomic_write_in_dir = swap_note_after_commit
        try:
            with pytest.raises(OSError):
                intake_add(source, "NoteImpostorStudy")
        finally:
            os.write = real_write
            intake_module._atomic_write_in_dir = original_atomic_write

        after_note_bytes = note_path.read_bytes()
        assert after_note_bytes == impostor_holder["bytes"]
        assert after_note_bytes != prior_note_bytes


# --- retained-quarantine hard bound -------------------------------------------------------------


def test_quarantine_entry_count_hard_bound_fails_closed(tmp_path: Path) -> None:
    """The shared retained-quarantine root enforces a fixed maximum
    entry count, checked descriptor-relatively BEFORE accepting any new
    move. Once the bound is reached, ordinary stale-link pruning fails
    closed with the fixed quarantine-limit-exceeded code instead of
    silently growing past it -- the bound is a hard ceiling, never a
    best-effort target -- and (removing the stale file without changing
    any other candidate, so the failing attempt writes nothing new to
    roll back) the prior manifest remains exactly loadable afterward."""
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "forms").mkdir(parents=True)
    (source / "dictionary_mapping").mkdir(parents=True)
    (source / "datasets" / "a.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    (source / "datasets" / "b.csv").write_text("SUBJID,AGE\n2,41\n", encoding="utf-8")
    (source / "forms" / "consent.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    (source / "dictionary_mapping" / "dict.csv").write_text("var,label\nSUBJID,Subject\n", encoding="utf-8")

    with _workspace(tmp_path) as workspace:
        import phi_engine.pipeline.intake as intake_module
        from phi_engine.pipeline.intake import IntakeManifestError, intake_add, load_intake_manifest

        original_max_entries = intake_module._QUARANTINE_MAX_ENTRIES
        intake_module._QUARANTINE_MAX_ENTRIES = 1
        try:
            first = intake_add(source, "QuotaEntryStudy")
            assert first["status"] == "ready"

            # removing a.csv (b.csv, forms, and dictionary_mapping untouched)
            # makes only its intake-tree link stale; pruning it consumes
            # the single allowed quarantine slot
            (source / "datasets" / "a.csv").unlink()
            second = intake_add(source, "QuotaEntryStudy")
            assert second["status"] == "ready"

            quarantine_root = workspace / ".intake_quarantine"
            assert len(list(quarantine_root.iterdir())) == 1

            # removing the last remaining dataset must prune ANOTHER
            # stale link, but the bound is already exhausted -- fails
            # closed, never adopted. Every other candidate (forms/,
            # dictionary_mapping/) is unchanged and idempotently verified,
            # never recreated, so this attempt writes nothing new and
            # its rollback is a clean no-op.
            (source / "datasets" / "b.csv").unlink()
            with pytest.raises(IntakeManifestError, match="quarantine-limit-exceeded"):
                intake_add(source, "QuotaEntryStudy")

            assert len(list(quarantine_root.iterdir())) == 1  # never grew past the bound
            reloaded = load_intake_manifest("QuotaEntryStudy")
            assert reloaded == second  # the failed attempt made no lasting change
        finally:
            intake_module._QUARANTINE_MAX_ENTRIES = original_max_entries


def test_quarantine_byte_bound_hard_fails_closed_before_first_entry(tmp_path: Path) -> None:
    """The shared retained-quarantine root also enforces a fixed maximum
    allocated-byte footprint, checked descriptor-relatively BEFORE
    accepting any new move -- a bound of zero available bytes means
    even the FIRST prune must fail closed with the fixed
    quarantine-limit-exceeded code rather than ever writing into
    quarantine at all. Removing the stale file without changing any
    other candidate means the failing attempt writes nothing new, so
    the prior manifest remains exactly loadable afterward."""
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "forms").mkdir(parents=True)
    (source / "dictionary_mapping").mkdir(parents=True)
    (source / "datasets" / "a.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    (source / "datasets" / "b.csv").write_text("SUBJID,AGE\n2,41\n", encoding="utf-8")
    (source / "forms" / "consent.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    (source / "dictionary_mapping" / "dict.csv").write_text("var,label\nSUBJID,Subject\n", encoding="utf-8")

    with _workspace(tmp_path) as workspace:
        import phi_engine.pipeline.intake as intake_module
        from phi_engine.pipeline.intake import IntakeManifestError, intake_add, load_intake_manifest

        first = intake_add(source, "QuotaByteStudy")
        assert first["status"] == "ready"

        (source / "datasets" / "a.csv").unlink()  # b.csv, forms, and dictionary_mapping untouched

        original_max_bytes = intake_module._QUARANTINE_MAX_BYTES
        intake_module._QUARANTINE_MAX_BYTES = 0
        try:
            with pytest.raises(IntakeManifestError, match="quarantine-limit-exceeded"):
                intake_add(source, "QuotaByteStudy")
        finally:
            intake_module._QUARANTINE_MAX_BYTES = original_max_bytes

        quarantine_root = workspace / ".intake_quarantine"
        assert list(quarantine_root.iterdir()) == []
        reloaded = load_intake_manifest("QuotaByteStudy")
        assert reloaded == first  # the failed attempt made no lasting change


def test_quarantine_root_stays_mode_0700_after_bound_rejection(tmp_path: Path) -> None:
    """Hitting the hard bound never weakens the quarantine root's
    private mode -- it stays a private ``0700`` directory even on the
    fail-closed path, exactly as it is on every successful retain."""
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "forms").mkdir(parents=True)
    (source / "dictionary_mapping").mkdir(parents=True)
    (source / "datasets" / "a.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    (source / "forms" / "consent.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    (source / "dictionary_mapping" / "dict.csv").write_text("var,label\nSUBJID,Subject\n", encoding="utf-8")

    with _workspace(tmp_path) as workspace:
        import phi_engine.pipeline.intake as intake_module
        from phi_engine.pipeline.intake import IntakeManifestError, intake_add

        intake_add(source, "QuotaModeStudy")
        (source / "datasets" / "a.csv").rename(source / "datasets" / "b.csv")

        original_max_bytes = intake_module._QUARANTINE_MAX_BYTES
        intake_module._QUARANTINE_MAX_BYTES = 0
        try:
            with pytest.raises(IntakeManifestError, match="quarantine-limit-exceeded"):
                intake_add(source, "QuotaModeStudy")
        finally:
            intake_module._QUARANTINE_MAX_BYTES = original_max_bytes

        quarantine_root = workspace / ".intake_quarantine"
        assert stat.S_IMODE(quarantine_root.stat().st_mode) == 0o700


def test_quarantine_byte_bound_rejects_incoming_symlink_exceeding_remaining_capacity(tmp_path: Path) -> None:
    """The hard byte bound must reject the incoming node whenever ITS
    OWN descriptor-relative size alone exceeds available capacity, not
    merely when prior usage had already fully exhausted the bound. A
    bound strictly between zero and the incoming symlink's exact byte
    footprint (``0 < max < incoming``) must still fail closed before
    any rename -- the original link stays exactly where it was,
    quarantine usage stays exactly at zero, and the quarantine root's
    private mode stays ``0700``."""
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "forms").mkdir(parents=True)
    (source / "dictionary_mapping").mkdir(parents=True)
    (source / "datasets" / "a.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    (source / "datasets" / "b.csv").write_text("SUBJID,AGE\n2,41\n", encoding="utf-8")
    (source / "forms" / "consent.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    (source / "dictionary_mapping" / "dict.csv").write_text("var,label\nSUBJID,Subject\n", encoding="utf-8")

    with _workspace(tmp_path) as workspace:
        import phi_engine.pipeline.intake as intake_module
        from phi_engine.pipeline.intake import IntakeManifestError, intake_add, load_intake_manifest

        first = intake_add(source, "QuotaByteBoundaryStudy")
        assert first["status"] == "ready"
        study_dir = workspace / "intake" / "QuotaByteBoundaryStudy"
        a_entry = next(e for e in first["entries"].values() if e["relative_path"] == "datasets/a.csv")
        link_path = study_dir / a_entry["intake_path"]
        assert link_path.is_symlink()
        incoming_bytes = len(os.readlink(link_path))
        assert incoming_bytes > 1

        (source / "datasets" / "a.csv").unlink()  # b.csv, forms, and dictionary_mapping untouched

        quarantine_root = workspace / ".intake_quarantine"
        assert list(quarantine_root.iterdir()) == []

        original_max_bytes = intake_module._QUARANTINE_MAX_BYTES
        intake_module._QUARANTINE_MAX_BYTES = incoming_bytes - 1  # 0 < max < incoming
        assert 0 < intake_module._QUARANTINE_MAX_BYTES < incoming_bytes
        try:
            with pytest.raises(IntakeManifestError, match="quarantine-limit-exceeded"):
                intake_add(source, "QuotaByteBoundaryStudy")
        finally:
            intake_module._QUARANTINE_MAX_BYTES = original_max_bytes

        assert link_path.is_symlink()  # original stays -- never renamed into quarantine
        assert os.readlink(link_path) == a_entry["original_path"]
        assert list(quarantine_root.iterdir()) == []  # usage unchanged
        assert stat.S_IMODE(quarantine_root.stat().st_mode) == 0o700  # root 0700
        reloaded = load_intake_manifest("QuotaByteBoundaryStudy")
        assert reloaded == first  # the failed attempt made no lasting change


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
    (source / "dictionary_mapping" / "extra.csv").write_text("x,y\n1,2\n", encoding="utf-8")
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


def test_load_v3_schema_manifest_raises_invalid(tmp_path: Path) -> None:
    # Deliberate exception to the repo-wide v3->v4 rename: this fixture
    # intentionally keeps the OLD "intake-manifest/v3" schema string and
    # its "v3" test name, because it exists specifically to prove that
    # string is now rejected. Clean cutover: no migrator, no dual reader
    # -- a v3 manifest on disk fails exactly the same fixed
    # "intake_manifest_invalid" way a v2 manifest already does, through
    # the unchanged `raw.get("schema") != _MANIFEST_SCHEMA` check, never a
    # special-cased v2/v3 detection path.
    with _workspace(tmp_path) as workspace:
        from phi_engine.pipeline.intake import IntakeManifestError, load_intake_manifest

        study_dir = workspace / "intake" / "LegacyV3"
        study_dir.mkdir(parents=True)
        study_dir.chmod(0o700)
        manifest_path = study_dir / "intake_manifest.json"
        manifest_path.write_text(
            json.dumps({
                "schema": "intake-manifest/v3", "study": "LegacyV3", "study_name_source": "user",
                "status": "ready", "source_root": "/src", "entries": {}, "review_items": [],
                "errors": [], "removals": [],
            }),
            encoding="utf-8",
        )
        manifest_path.chmod(0o600)

        with pytest.raises(IntakeManifestError, match="intake_manifest_invalid"):
            load_intake_manifest("LegacyV3")


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
