from __future__ import annotations

import json
import sys
import uuid
from hashlib import sha256
from pathlib import Path

import pytest

from harness.make_stress_fixtures import build_review_required_fixtures, build_stress_fixtures
from tests._workspace_harness import hermetic_phi_workspace


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_source_entries_unchanged(manifest: dict) -> None:
    """Every regular-file entry the fixture manifest recorded at build time
    still hashes the same -- the strongest, cheapest proxy for
    ``harness.spec_check``'s own full source_immutability check (exercised
    directly, against the real ``run_spec_check``, by
    ``test_spec_check_passes_against_the_full_stress_run`` below)."""
    source_root = Path(manifest["source_root"])
    for rel_path, expected in manifest["entries"].items():
        actual_path = source_root / rel_path
        if expected["type"] == "file":
            assert _sha256_file(actual_path) == expected["sha256"], rel_path


def _published_dataset_dir(workspace: Path, study: str) -> Path:
    return workspace / "output" / study / "llm_source" / "datasets"


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _all_published_text(workspace: Path, study: str) -> str:
    dataset_dir = _published_dataset_dir(workspace, study)
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(dataset_dir.glob("*.jsonl"))
    )


def _prepare_stress_source(tmp_path: Path) -> tuple[Path, dict]:
    source = tmp_path / "src"
    return source, build_stress_fixtures(source, seed=42)


def _intake_organize_run(tmp_path: Path, study_prefix: str = "Stress"):
    source, manifest = _prepare_stress_source(tmp_path)
    study = f"{study_prefix}{uuid.uuid4().hex[:8]}"
    ctx = hermetic_phi_workspace(tmp_path, study)
    workspace = ctx.__enter__()
    try:
        from phi_engine.pipeline.intake import intake_add
        from phi_engine.pipeline.organize import organize
        from phi_engine.pipeline.run import run_pipeline

        intake_manifest = intake_add(source, study)
        assert intake_manifest["status"] == "ready", intake_manifest["review_items"]
        organize_manifest = organize(study)
        result = run_pipeline(study, "us")
        return ctx, workspace, study, source, manifest, intake_manifest, organize_manifest, result
    except Exception:
        ctx.__exit__(*sys.exc_info())
        raise


def test_intake_links_everything_and_preserves_source_bytes(tmp_path: Path):
    source, fixture_manifest = _prepare_stress_source(tmp_path)
    study = f"StressIntake{uuid.uuid4().hex[:8]}"

    with hermetic_phi_workspace(tmp_path, study) as workspace:
        from phi_engine.pipeline.intake import intake_add

        intake_manifest = intake_add(source, study)

        assert intake_manifest["status"] == "ready"
        assert intake_manifest["errors"] == []
        assert intake_manifest["review_items"] == []
        # Every regular source file (duplicate bytes and nested/duplicate
        # folders included) becomes its own independent v3 entry -- v3 has
        # no separate "duplicates" bucket, unlike the pre-v3 manifest.
        source_file_count = sum(1 for e in fixture_manifest["entries"].values() if e["type"] == "file")
        assert len(intake_manifest["entries"]) == source_file_count

        intake_study_dir = workspace / "intake" / study
        assert (intake_study_dir / "intake_manifest.json").is_file()
        for entry in intake_study_dir.rglob("*"):
            if entry.is_dir():
                continue
            if entry.name == "intake_manifest.json":
                assert entry.is_file()
                assert not entry.is_symlink()
            else:
                assert entry.is_symlink(), f"{entry} should be an intake symlink"

        _assert_source_entries_unchanged(fixture_manifest)


def test_organize_routes_every_format_correctly(tmp_path: Path):
    source, _fixture_manifest = _prepare_stress_source(tmp_path)
    study = f"StressOrg{uuid.uuid4().hex[:8]}"

    with hermetic_phi_workspace(tmp_path, study) as workspace:
        from phi_engine.pipeline.intake import intake_add, load_intake_manifest
        from phi_engine.pipeline.organize import organize

        intake_add(source, study)
        organize_manifest = organize(study)

        dataset_outputs = {entry["output"]: entry for entry in organize_manifest["datasets"]}
        review_by_file = {entry["file"]: entry for entry in organize_manifest["review_bucket"]}

        # CSV, single-sheet XLSX, and the PHI-in-unexpected-columns CSV all
        # route as datasets.
        assert "labs.jsonl" in dataset_outputs
        assert "labs_dup.jsonl" in dataset_outputs
        assert "screening__Screening.jsonl" in dataset_outputs
        assert "site_notes.jsonl" in dataset_outputs
        # Duplicate bytes at a nested path stay a fully independent dataset
        # entry: same row VALUES (header_ids are artifact-scoped, so the
        # normalized_rows_sha256 legitimately differs), distinct identity.
        labs_rows = _read_jsonl(workspace / "organized" / study / "datasets" / "labs.jsonl")
        labs_dup_rows = _read_jsonl(workspace / "organized" / study / "datasets" / "labs_dup.jsonl")
        assert [sorted(row.values()) for row in labs_rows] == [sorted(row.values()) for row in labs_dup_rows]
        assert dataset_outputs["labs.jsonl"]["artifact_id"] != dataset_outputs["labs_dup.jsonl"]["artifact_id"]

        # legacy_site.xls: a genuine workbook (xlwt installed) parses as a
        # dataset; otherwise the mislabeled fallback lands in the
        # organizer's own (non-blocking, per-file) review bucket -- .xls
        # carries no intake-time content check, only organize() can tell.
        legacy_outputs = [name for name in dataset_outputs if name.startswith("legacy_site")]
        if legacy_outputs:
            assert dataset_outputs[legacy_outputs[0]]["row_count"] >= 1
        else:
            assert review_by_file["legacy_site.xls"]["reason"] in {"xls-reader-unavailable", "excel-open-error"}

        # forms/consent_table.pdf has an extractable table -> dataset output;
        # forms/screening_form.pdf has none -> non-blocking organizer review,
        # never a row value.
        assert "consent_table__pdftable0.jsonl" in dataset_outputs
        assert review_by_file["screening_form.pdf"]["reason"] == "pdf-no-extractable-table"

        allowed_keys = {"file", "link_name", "reason", "detail", "suffix", "copied_sha", "sheet", "table_index", "failure_code"}
        for entry in organize_manifest["review_bucket"]:
            assert set(entry.keys()) <= allowed_keys
            # Review-bucket entries carry file-level metadata only -- never
            # a row value (the whole point of routing to review).
            assert "SUBJID" not in json.dumps(entry)

        support_kinds = sorted((s["kind"], s.get("format")) for s in organize_manifest["support_artifacts"])
        assert support_kinds == [("dictionary", "csv"), ("dictionary", "csv"), ("mapping", "csv")]

        intake_entries = load_intake_manifest(study)["entries"]
        pdf_roles_by_file = {
            Path(intake_entries[link_name]["relative_path"]).name: role
            for link_name, role in organize_manifest["pdf_roles"].items()
        }
        assert pdf_roles_by_file["consent_table.pdf"]["role"] == "table_extracted"
        assert pdf_roles_by_file["consent_table.pdf"]["tables_extracted"] >= 1
        assert pdf_roles_by_file["screening_form.pdf"]["role"] == "review"
        assert pdf_roles_by_file["screening_form.pdf"]["reason"] == "pdf-no-extractable-table"


def test_run_completes_partial_and_escalates_phi_in_unexpected_columns(tmp_path: Path):
    """The two planted PHI-in-unexpected-columns headers ('NOTES' holding
    SSN-shaped values, 'COMMENT' holding phone-shaped values) must never
    publish raw, however the pipeline arrives at that outcome (name-rule
    suppression, force_drop_headers, or the value-profiler's escalation
    rule elsewhere in the same run is a separate concern and intentionally
    NOT asserted here as an exact count)."""
    ctx, workspace, study, _source, manifest, _intake_manifest, _organize_manifest, result = _intake_organize_run(
        tmp_path, "StressRun"
    )
    try:
        assert result.exit_code == 8

        published_text = _all_published_text(workspace, study)
        for planted_row in manifest["planted_unexpected_phi_rows"]:
            assert planted_row["NOTES"] not in published_text
            assert planted_row["COMMENT"] not in published_text

        site_notes_file = manifest["planted_unexpected_phi_file"]
        for row in _read_jsonl(_published_dataset_dir(workspace, study) / (Path(site_notes_file).stem + ".jsonl")):
            assert "NOTES" not in row
            assert "COMMENT" not in row
    finally:
        ctx.__exit__(None, None, None)


def test_feedback_loop_drop_decision_clears_the_flagged_columns(tmp_path: Path):
    ctx, workspace, study, _source, _manifest, _intake_manifest, _organize_manifest, first_result = _intake_organize_run(
        tmp_path, "StressFeedback"
    )
    try:
        from phi_engine.pipeline.review import decide
        from phi_engine.pipeline.run import run_pipeline

        decide(study, header="NOTES", decision="drop")
        decide(study, header="COMMENT", decision="drop")
        second_result = run_pipeline(study, "us")

        site_notes_outputs = sorted(_published_dataset_dir(workspace, study).glob("site_notes*.jsonl"))
        assert site_notes_outputs, "site_notes-derived output should be published"
        for output in site_notes_outputs:
            for row in _read_jsonl(output):
                assert "NOTES" not in row
                assert "COMMENT" not in row

        assert second_result.organizer_review_count == first_result.organizer_review_count
        assert second_result.review_queue_size <= first_result.review_queue_size
    finally:
        ctx.__exit__(None, None, None)


def test_llm_boundary_zero_prompts_in_default_run_and_egress_gate_blocks_contamination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source, manifest = _prepare_stress_source(tmp_path)
    study = f"StressLLM{uuid.uuid4().hex[:8]}"

    with hermetic_phi_workspace(tmp_path, study) as _workspace:
        from phi_engine.config import config
        from phi_engine.pipeline.intake import intake_add
        from phi_engine.pipeline.organize import organize
        from phi_engine.pipeline.run import run_pipeline

        prompts: list[str] = []

        def spy_complete(self, prompt: str) -> str:  # noqa: ANN001 - method signature mirrors production
            prompts.append(prompt)
            raise AssertionError("Default deterministic pipeline must not call LLMClient.complete")

        with monkeypatch.context() as m:
            m.setattr(config.LLMClient, "complete", spy_complete)
            intake_add(source, study)
            organize(study)
            run_pipeline(study, "us")

        assert prompts == []

        contaminated_value = manifest["planted_unexpected_phi_rows"][0]["NOTES"]
        client = config.LLMClient(provider="ollama", model="x", base_url="http://127.0.0.1:1")
        with pytest.raises(PermissionError) as exc_info:
            client.complete(f"Summarize this row note: {contaminated_value}")
        assert type(exc_info.value).__name__ == "PHIEgressBlockedError"
        assert contaminated_value not in str(exc_info.value)


def test_spec_check_passes_against_the_full_stress_run(tmp_path: Path):
    ctx, workspace, study, _source, manifest, _intake_manifest, _organize_manifest, result = _intake_organize_run(
        tmp_path, "StressSpec"
    )
    try:
        assert result.exit_code == 8

        source_manifest_path = tmp_path / "source_manifest.json"
        source_manifest_path.write_text(
            json.dumps(
                {"source_root": manifest["source_root"], "entries": manifest["entries"]},
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        from harness.spec_check import run_spec_check

        report = run_spec_check(
            workspace=workspace,
            study=study,
            skip_pytest=True,
            source_manifest=source_manifest_path,
        )
        assert report["all_pass"] is True, report["checks"]
        immutability_check = next(c for c in report["checks"] if c["check"] == "source_immutability")
        assert immutability_check["entries_checked"] == len(manifest["entries"])
        canary_check = next(c for c in report["checks"] if c["check"] == "llm_boundary_canary")
        assert canary_check["violations"] == []

        report_path = workspace / "tmp" / "spec_check_report.json"
        assert report_path.exists(), "spec_check must write a workspace-local report"
        assert json.loads(report_path.read_text(encoding="utf-8")) == report
    finally:
        ctx.__exit__(None, None, None)


_IMMUTABILITY_DRIFT_CASES = [
    ("type", "symlink"),
    ("mode", 0o777),
    ("size", 999999),
    ("mtime_ns", 1),
    ("uid", 999999),
    ("gid", 999999),
    ("sha256", "0" * 64),
    ("symlink_target", "/nonexistent/elsewhere"),
]


@pytest.mark.parametrize("field,tampered_value", _IMMUTABILITY_DRIFT_CASES, ids=[c[0] for c in _IMMUTABILITY_DRIFT_CASES])
def test_source_immutability_check_catches_each_field_drift(tmp_path: Path, field: str, tampered_value):
    """Direct proof the hardened source_immutability check actually
    compares every recorded field (type, mode, size, mtime_ns, uid, gid,
    sha256, symlink_target) -- not merely sha256 -- by tampering the
    RECORDED (manifest-side) value for one real, unmodified source file
    and asserting the comparison surfaces exactly that field's drift.
    Manifest-side tampering (rather than mutating the file itself) is what
    makes uid/gid coverage possible without root/chown privileges, and
    exercises the identical comparison branch a real drifted FILE would."""
    source, manifest = _prepare_stress_source(tmp_path)
    target_rel = "datasets/labs.csv"
    tampered_entries = dict(manifest["entries"])
    tampered_entries[target_rel] = {**tampered_entries[target_rel], field: tampered_value}
    source_manifest_path = tmp_path / "source_manifest.json"
    source_manifest_path.write_text(
        json.dumps({"source_root": manifest["source_root"], "entries": tampered_entries}, sort_keys=True),
        encoding="utf-8",
    )

    from harness.spec_check import run_spec_check

    report = run_spec_check(skip_pytest=True, source_manifest=source_manifest_path)
    immutability = next(c for c in report["checks"] if c["check"] == "source_immutability")
    assert immutability["ok"] is False
    assert any(f"{target_rel}: {field} drift" in v for v in immutability["violations"])


def test_source_immutability_check_catches_new_and_missing_entries(tmp_path: Path):
    """A post-build unexpected new file and a vanished existing entry must
    each surface as their own distinct violation."""
    source, manifest = _prepare_stress_source(tmp_path)
    source_manifest_path = tmp_path / "source_manifest.json"
    source_manifest_path.write_text(
        json.dumps({"source_root": manifest["source_root"], "entries": manifest["entries"]}, sort_keys=True),
        encoding="utf-8",
    )

    from harness.spec_check import run_spec_check

    clean = run_spec_check(skip_pytest=True, source_manifest=source_manifest_path)
    assert next(c for c in clean["checks"] if c["check"] == "source_immutability")["ok"] is True

    (source / "datasets" / "new_uninvited_file.csv").write_text("SUBJID\nX\n", encoding="utf-8")
    (source / "datasets" / "screening.xlsx").unlink()

    drifted = run_spec_check(skip_pytest=True, source_manifest=source_manifest_path)
    immutability = next(c for c in drifted["checks"] if c["check"] == "source_immutability")
    assert immutability["ok"] is False
    violations_text = "\n".join(immutability["violations"])
    assert "datasets/new_uninvited_file.csv: unexpected entry" in violations_text
    assert "datasets/screening.xlsx: source entry vanished" in violations_text


def _symlink_intake_root(tmp_path: Path, workspace: Path) -> str:
    real_root = tmp_path / "elsewhere_root"
    real_root.mkdir()
    (workspace / "intake").symlink_to(real_root)
    return "intake root must not be a symlink"


def _symlink_study_directory(tmp_path: Path, workspace: Path) -> str:
    intake_root = workspace / "intake"
    intake_root.mkdir(parents=True)
    real_study = tmp_path / "elsewhere_study"
    real_study.mkdir()
    (intake_root / "Study").symlink_to(real_study)
    return "study directory must not be a symlink"


def _symlink_component_directory(tmp_path: Path, workspace: Path) -> str:
    study_dir = workspace / "intake" / "Study"
    study_dir.mkdir(parents=True)
    real_component = tmp_path / "elsewhere_component"
    real_component.mkdir()
    (study_dir / "datasets").symlink_to(real_component)
    return "intake component directory must not be a symlink"


@pytest.mark.parametrize(
    "make_symlinked_node",
    [_symlink_intake_root, _symlink_study_directory, _symlink_component_directory],
    ids=["root", "study", "component"],
)
def test_intake_invariant_rejects_symlinked_intake_tree_nodes(tmp_path: Path, make_symlinked_node) -> None:
    """lstat-based regression coverage for the intake root, study
    directory, and component directory: each must be rejected as a
    violation when it is itself a symlink, never silently followed."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    expected_message = make_symlinked_node(tmp_path, workspace)

    from harness.spec_check import run_spec_check

    report = run_spec_check(workspace=workspace, study="Study", skip_pytest=True)
    invariant = next(c for c in report["checks"] if c["check"] == "intake_symlink_invariant")
    assert invariant["ok"] is False
    assert any(expected_message in v for v in invariant["violations"])


_CANARY_CASES = [
    (
        "sanctioned_bare_call",
        "intake_naming.py",
        "def resolve_intake_study():\n"
        "    def get_client():\n"
        "        return new_offline_local_client()\n"
        "    return get_client()\n",
        False,
    ),
    (
        "sanctioned_attribute_call",
        "intake_naming.py",
        "from phi_engine.security import model_routing\n"
        "\n"
        "def _resolve_intake_study():\n"
        "    return model_routing.new_offline_local_client()\n",
        False,
    ),
    (
        "sanctioned_with_legitimate_type_annotation",
        "intake_naming.py",
        "from phi_engine.security.model_routing import OfflineLocalLLMClient, new_offline_local_client\n"
        "\n"
        "def _resolve_intake_study():\n"
        "    client: OfflineLocalLLMClient | None = None\n"
        "    def get_client() -> OfflineLocalLLMClient:\n"
        "        nonlocal client\n"
        "        if client is None:\n"
        "            client = new_offline_local_client()\n"
        "        return client\n"
        "    return get_client()\n",
        False,
    ),
    (
        "unauthorized_bare_call_outside_sanctioned_function",
        "intake_naming.py",
        "def other_function():\n    return new_offline_local_client()\n",
        True,
    ),
    (
        "unauthorized_callsite_other_module",
        "organize.py",
        "def route_dataset():\n    return new_offline_local_client()\n",
        True,
    ),
    (
        "aliased_import",
        "intake_naming.py",
        "from phi_engine.security.model_routing import new_offline_local_client as _factory\n"
        "\n"
        "def resolve_intake_study():\n    return _factory()\n",
        True,
    ),
    (
        "assigned_alias",
        "intake_naming.py",
        "from phi_engine.security.model_routing import new_offline_local_client\n"
        "\n"
        "def resolve_intake_study():\n"
        "    factory = new_offline_local_client\n"
        "    return factory()\n",
        True,
    ),
    (
        "list_alias",
        "intake_naming.py",
        "from phi_engine.security.model_routing import new_offline_local_client\n"
        "\n"
        "def resolve_intake_study():\n"
        "    fns = [new_offline_local_client]\n"
        "    return fns[0]()\n",
        True,
    ),
    (
        "argument_alias",
        "intake_naming.py",
        "from phi_engine.security.model_routing import new_offline_local_client\n"
        "\n"
        "def helper(fn):\n    return fn()\n"
        "\n"
        "def resolve_intake_study():\n    return helper(new_offline_local_client)\n",
        True,
    ),
    (
        "walrus_alias",
        "intake_naming.py",
        "from phi_engine.security.model_routing import new_offline_local_client\n"
        "\n"
        "def resolve_intake_study():\n    return (factory := new_offline_local_client)()\n",
        True,
    ),
    (
        "direct_constructor",
        "intake_naming.py",
        "from phi_engine.security.model_routing import OfflineLocalLLMClient\n"
        "\n"
        "def resolve_intake_study():\n    return OfflineLocalLLMClient(None)\n",
        True,
    ),
]


@pytest.mark.parametrize("filename,source,expect_violation", [c[1:] for c in _CANARY_CASES], ids=[c[0] for c in _CANARY_CASES])
def test_llm_boundary_canary_table(filename: str, source: str, expect_violation: bool) -> None:
    """Table-driven proof of the single unified alias rule: sanctioned
    direct/attribute calls (plus a legitimate type annotation) are
    permitted; aliased imports, assigned/list/argument/walrus aliases,
    direct construction, and any unauthorized callsite are all rejected.
    Pure AST-level probes (no real files written) via a synthetic,
    non-existent path so a bad probe can never touch real source."""
    import ast as ast_module

    from harness.spec_check import REPO_ROOT, _scan_offline_client_canary

    probe_path = REPO_ROOT / "phi_engine" / "pipeline" / filename
    tree = ast_module.parse(source, filename=str(probe_path))
    violations = _scan_offline_client_canary(probe_path, tree)
    assert bool(violations) is expect_violation, violations


def test_llm_boundary_canary_flags_an_unauthorized_offline_client_callsite(tmp_path: Path):
    """End-to-end proof through the real full-repo scan (not just the pure
    per-file unit above): the real repository tree reports clean, and a
    synthetic unauthorized pipeline module is flagged when actually
    scanned via ``_check_llm_boundary``'s ``phi_engine/pipeline/`` walk."""
    from harness.spec_check import REPO_ROOT, _check_llm_boundary

    clean = _check_llm_boundary()
    assert clean["ok"] is True
    assert clean["violations"] == []

    rogue = REPO_ROOT / "phi_engine" / "pipeline" / "_spec_check_canary_probe.py"
    rogue.write_text(
        "from phi_engine.security.model_routing import new_offline_local_client\n"
        "\n"
        "def rogue_caller():\n"
        "    return new_offline_local_client()\n",
        encoding="utf-8",
    )
    try:
        dirty = _check_llm_boundary()
        assert dirty["ok"] is False
        assert any("new_offline_local_client referenced outside" in v for v in dirty["violations"])
    finally:
        rogue.unlink()


def test_intake_review_required_for_each_unsupported_case_never_organizes(tmp_path: Path):
    """Under intake-manifest/v3 a review item anywhere blocks the WHOLE
    study -- there is no per-file partial success. This proves each fixed
    preflight reason still fires correctly (unsupported format including
    the JSON/JSONL demotion cases, invalid xlsx workbook, multi-sheet
    dataset xlsx, a source symlink) and that organize() refuses to run at
    all against a review_required study."""
    source = tmp_path / "src"
    fixture = build_review_required_fixtures(source, seed=43)
    study = f"StressReview{uuid.uuid4().hex[:8]}"

    with hermetic_phi_workspace(tmp_path, study):
        from phi_engine.pipeline.intake import IntakeNotReadyError, intake_add
        from phi_engine.pipeline.organize import organize

        manifest = intake_add(source, study)

        assert manifest["status"] == "review_required"
        assert manifest["errors"] == []
        reasons_by_path = {item["path"]: item["reason"] for item in manifest["review_items"]}
        for path, expected_reason in fixture["expected_review_reasons"].items():
            assert reasons_by_path.get(path) == expected_reason, (path, reasons_by_path)
        assert all(item["blocking"] is True for item in manifest["review_items"])

        by_rel = {e["relative_path"]: e for e in manifest["entries"].values()}
        # JSON/JSONL are explicitly demoted from an accepted dataset format
        # to _unclassified under v3 -- kept only as this review case.
        assert by_rel["datasets/extra_group.json"]["component"] == "_unclassified"
        assert by_rel["datasets/demographics.jsonl"]["component"] == "_unclassified"

        with pytest.raises(IntakeNotReadyError) as exc_info:
            organize(study)
        assert exc_info.value.status == "review_required"

        # The good, fully-accepted files in the same tree are still
        # unclassified-blocked along with everything else -- v3 has no
        # partial "organize the good ones anyway" outcome.
        assert by_rel["datasets/good.csv"]["component"] == "datasets"


def test_stale_staged_file_never_publishes_without_current_approval(tmp_path: Path):
    """Regression test (Phase 7 final-audit finding): a JSONL left sitting in
    tmp/<study>/datasets/ (e.g. residue from a prior run that scrubbed
    successfully but then failed the residual guard gate, so publish was
    skipped and the scrubbed files were never cleaned up) must NEVER be
    published by a LATER run unless it is part of THAT run's own approved
    forms -- publishing it would bypass the current run's
    classification/scrub/approval pipeline entirely."""
    study = f"StaleStaging{uuid.uuid4().hex[:8]}"

    with hermetic_phi_workspace(tmp_path, study) as workspace:
        from phi_engine.pipeline.intake import intake_add
        from phi_engine.pipeline.organize import organize
        from phi_engine.pipeline.run import run_pipeline
        import phi_engine.config.config as config
        from tests._workspace_harness import write_csv, write_pdf_table

        source = tmp_path / "src"
        rows = [[f"S{i}", 30 + i] for i in range(5)]
        write_csv(source / "datasets" / "current.csv", ["SUBJID", "AGE"], rows)
        # An extractable table (not plain text) so this form never lands in
        # the organizer's own non-blocking review bucket; a dictionary that
        # names no dataset column so it never creates a dependency
        # recommendation -- both would otherwise force exit_code 8 and
        # obscure this test's actual subject (stale staging residue).
        write_pdf_table(source / "forms" / "consent.pdf", ["FIELD", "VALUE"], [["consent", "signed"]])
        write_csv(source / "data_dictionary" / "dict.csv", ["reference_code", "reference_label"], [["REF-01", "General study reference material"]])

        intake_manifest = intake_add(source, study)
        assert intake_manifest["status"] == "ready"
        organize(study)

        # Seed a stale file directly into staging -- simulates leftover
        # residue that never went through this run's organizer/classifier.
        staging_dir = Path(config.STAGING_DATASETS_DIR)
        staging_dir.mkdir(parents=True, exist_ok=True)
        (staging_dir / "stale.jsonl").write_text(
            json.dumps({"SSN": "123-45-6789"}) + "\n", encoding="utf-8"
        )

        result = run_pipeline(study, "us")

        assert result.exit_code == 0
        published = sorted(p.name for p in _published_dataset_dir(workspace, study).glob("*.jsonl"))
        # published now legitimately also includes the mandatory forms/
        # PDF's own extracted-table output (forms/ is required content,
        # not test scaffolding) -- the regression this test guards against
        # is specifically the STALE file, not the exact published set.
        assert "current.jsonl" in published
        assert "consent__pdftable0.jsonl" in published
        assert "stale.jsonl" not in published
        assert not (staging_dir / "stale.jsonl").exists()  # staging cleared, not just unpublished


def test_hermetic_workspace_removes_child_first_imported_inside_failed_context(tmp_path: Path):
    """A phi_engine.* child absent before the context and created only
    inside a body that raises must leave no trace afterward: neither its
    sys.modules entry nor its parent-package attribute may survive
    teardown -- restoring sys.modules alone is not enough, since the kept
    `phi_engine` package's attribute for the child would otherwise still
    point at the now-orphaned hermetic module object.

    Uses a synthetic child name (not a real phi_engine submodule) so this
    test cannot disturb the real phi_engine.pipeline.* module tree that
    other tests in this session depend on for stable class identity.
    """
    import types

    import phi_engine

    module_name = "phi_engine._hermetic_absence_probe"
    assert module_name not in sys.modules
    assert not hasattr(phi_engine, "_hermetic_absence_probe")

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with hermetic_phi_workspace(tmp_path, "AbsentChild"):
            probe = types.ModuleType(module_name)
            sys.modules[module_name] = probe
            phi_engine._hermetic_absence_probe = probe  # mirrors real import-system binding

            assert module_name in sys.modules
            assert hasattr(phi_engine, "_hermetic_absence_probe")
            raise _Boom

    assert module_name not in sys.modules
    assert not hasattr(phi_engine, "_hermetic_absence_probe")


def test_hermetic_workspace_restores_preexisting_child_identity(tmp_path: Path):
    """A phi_engine.* module already imported before the context must be the
    exact original object -- both in sys.modules and via its parent's
    attribute -- once the context exits, so downstream collected code keeps
    the same class identity (e.g. Sensitivity, DependencyKind) it started
    with rather than a hermetic workspace's fresh replacement."""
    import phi_engine.pipeline.dependencies as original_dependencies

    module_name = "phi_engine.pipeline.dependencies"
    parent_name, _, leaf = module_name.rpartition(".")
    original_module = sys.modules[module_name]

    with hermetic_phi_workspace(tmp_path, "PreexistingChild"):
        import phi_engine.pipeline.dependencies as fresh_dependencies

        assert fresh_dependencies is not original_module

    assert sys.modules[module_name] is original_module
    assert getattr(sys.modules[parent_name], leaf) is original_module

    import phi_engine.pipeline.dependencies as restored_dependencies

    assert restored_dependencies is original_module
    assert restored_dependencies.Sensitivity is original_dependencies.Sensitivity


def test_hermetic_workspace_rebinds_kept_pipeline_lock_config(tmp_path: Path):
    """phi_engine.utils.pipeline_lock is kept (never evicted), so its own
    `import phi_engine.config.config as config` binding is frozen to
    whichever config module existed the first time it was imported. It must
    be rebound to each workspace's fresh config for the duration of the
    context -- otherwise lock paths resolve outside the hermetic workspace
    and can collide across studies -- and restored to its original binding
    afterward."""
    import phi_engine.utils.pipeline_lock as pipeline_lock

    original_config = pipeline_lock.config

    with hermetic_phi_workspace(tmp_path, "LockCfg") as workspace:
        assert pipeline_lock.config is not original_config
        assert pipeline_lock.lock_path_for("LockCfg").parent == workspace.resolve() / "tmp"

    assert pipeline_lock.config is original_config
