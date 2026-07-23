"""Tests for phi_engine.cli.main -- the redacted intake CLI contract
(plan step 5): only ``intake`` makes ``--study`` optional, its positive
``--support-confirmed-no-phi`` consent flag, the exact redacted receipt/
stderr shape, the ``ready``/``review_required``/``failed`` -> ``0``/``8``/
``2`` exit-code mapping, ``organize``'s ``IntakeNotReadyError`` handling,
and ``main()``'s fixed-code (never traceback) typed-exception boundary.

Most contracts are exercised through real ``python -m phi_engine``
subprocesses so argparse wiring, exact stdout/stderr bytes, and process
exit codes are all proven end to end, not merely inferred from calling
Python functions in-process. The handful of typed-exception branches that
are not reachable through any normal CLI flow (a busy pipeline lock, a
``VerifiedSourceError`` escaping ``intake_add``) are exercised in-process
against ``phi_engine.cli.main.main`` with the underlying call monkeypatched,
since they are defensive boundary behavior of this module, not the
pipeline's.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_STUDY_HEX_RE = re.compile(r"\Astudy-[0-9a-f]{8}\Z")


def _compact_receipt(*, study: str, status: str, linked: int, review: int, errors: int, manifest_path: Path) -> str:
    """Exact, byte-for-byte serialization of the intake receipt the plan
    binds: insertion-ordered ``study,status,linked,review,errors,manifest``
    keys, compact separators (no indentation, no key sorting), one line."""
    return (
        json.dumps(
            {
                "study": study,
                "status": status,
                "linked": linked,
                "review": review,
                "errors": errors,
                "manifest": str(manifest_path),
            },
            separators=(",", ":"),
        )
        + "\n"
    )


def _make_canonical_source(root: Path, *, sentinel: str) -> None:
    """A minimal, structurally accepted intake package: one dataset, one
    form, one dictionary file. ``sentinel`` is embedded in dataset content
    so redaction tests can prove it never reaches CLI output."""
    (root / "datasets").mkdir(parents=True)
    (root / "forms").mkdir(parents=True)
    (root / "data_dictionary").mkdir(parents=True)
    (root / "datasets" / "labs.csv").write_text(f"SUBJID,NOTE\n1,{sentinel}\n", encoding="utf-8")
    (root / "forms" / "consent.pdf").write_bytes(b"%PDF-1.4\n%test\n")
    (root / "data_dictionary" / "dict.csv").write_text("var,label\nSUBJID,Subject\n", encoding="utf-8")


def _run_cli(args: list[str], *, workspace: Path, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run ``python -m phi_engine <args>`` as a real subprocess.

    ``data/raw/dummy/datasets/`` is pre-seeded under the workspace so
    ``phi_engine.config.config``'s own import-time ``STUDY_NAME`` fallback
    (``detect_study_name()``) never falls through to its own warning-
    logging branch when a test intentionally omits ``--study`` -- that
    warning is emitted by an out-of-scope module (``config.py``), not by
    the CLI code under test here, and would otherwise make an "exact
    stderr" assertion falsely fail on unrelated noise.
    """
    (workspace / "data" / "raw" / "dummy" / "datasets").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.pop("STUDY_NAME", None)
    env.pop("PHI_WORKSPACE", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "phi_engine", *args],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


# --- help / argparse requiredness -----------------------------------------------------------


def test_top_level_help_lists_every_subcommand() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "phi_engine", "--help"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    for name in ("intake", "organize", "run", "review", "status"):
        assert name in result.stdout


def test_intake_help_shows_study_optional_and_support_flag_no_negative_flag() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "phi_engine", "intake", "--help"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "[--study STUDY]" in result.stdout  # bracketed => optional
    assert "--support-confirmed-no-phi" in result.stdout
    assert "--support-may-contain-phi" not in result.stdout
    assert "--source SOURCE" in result.stdout  # still required (unbracketed)
    usage_line = result.stdout.splitlines()[0] + result.stdout.splitlines()[1]
    assert "--source" in usage_line and "[--source" not in usage_line


@pytest.mark.parametrize("command", ["organize", "run", "review", "status"])
def test_other_subcommands_help_still_requires_study(command: str) -> None:
    extra = ["--jurisdiction", "us"] if command == "run" else []
    result = subprocess.run(
        [sys.executable, "-m", "phi_engine", command, "--help", *extra],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    usage = " ".join(result.stdout.splitlines()[:3])
    assert "--study STUDY" in usage  # unbracketed => required
    assert "[--study" not in usage


def test_organize_without_study_fails_argparse_not_pipeline(tmp_path: Path) -> None:
    result = _run_cli(["organize", "--workspace", str(tmp_path / "ws")], workspace=tmp_path / "ws")
    assert result.returncode == 2
    assert "--study" in result.stderr
    assert "Traceback" not in result.stderr


# --- explicit --study ready flow, exact receipt, exact stderr, redaction ---------------------


def test_intake_explicit_study_ready_prints_exact_receipt_and_stderr(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    source = tmp_path / "source"
    sentinel = "SENTINEL_SSN_999-99-9999"
    _make_canonical_source(source, sentinel=sentinel)

    result = _run_cli(
        ["intake", "--study", "ReadyStudy", "--source", str(source), "--workspace", str(workspace)],
        workspace=workspace,
    )

    assert result.returncode == 0
    manifest_path = workspace.resolve() / "intake" / "ReadyStudy" / "intake_manifest.json"
    assert result.stdout == _compact_receipt(
        study="ReadyStudy", status="ready", linked=3, review=0, errors=0, manifest_path=manifest_path
    )
    assert result.stderr == "intake: study=ReadyStudy status=ready linked=3 review=0 errors=0\n"
    assert manifest_path.is_file()

    combined = result.stdout + result.stderr
    assert sentinel not in combined
    assert str(source) not in combined
    assert "entries" not in json.loads(result.stdout)
    assert "review_items" not in json.loads(result.stdout)
    assert "errors" not in json.loads(result.stdout) or isinstance(json.loads(result.stdout)["errors"], int)
    assert set(json.loads(result.stdout)) == {"study", "status", "linked", "review", "errors", "manifest"}


# --- optional --study, generated receipt, review_required flow, redaction --------------------


def test_intake_omitted_study_generates_name_and_review_required(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    source = tmp_path / "source"
    sentinel = "SENTINEL_MRN_ABC123"
    _make_canonical_source(source, sentinel=sentinel)

    result = _run_cli(
        ["intake", "--source", str(source), "--workspace", str(workspace)],
        workspace=workspace,
    )

    assert result.returncode == 8
    receipt = json.loads(result.stdout)
    assert set(receipt) == {"study", "status", "linked", "review", "errors", "manifest"}
    assert _STUDY_HEX_RE.match(receipt["study"]), receipt["study"]
    assert receipt["status"] == "review_required"
    assert receipt["linked"] == 3
    assert receipt["review"] == 1
    assert receipt["errors"] == 0
    manifest_path = workspace.resolve() / "intake" / receipt["study"] / "intake_manifest.json"
    assert receipt["manifest"] == str(manifest_path)
    assert manifest_path.is_file()

    assert result.stdout == _compact_receipt(
        study=receipt["study"], status="review_required", linked=3, review=1, errors=0, manifest_path=manifest_path
    )
    assert result.stderr == (
        f"intake: study={receipt['study']} status=review_required linked=3 review=1 errors=0\n"
    )

    combined = result.stdout + result.stderr
    assert sentinel not in combined
    assert str(source) not in combined
    assert "support-phi-status-required" not in combined  # raw reason never leaked


# --- positive consent flag wiring -------------------------------------------------------------


def test_intake_support_confirmed_no_phi_attempts_ai_naming(tmp_path: Path) -> None:
    """With the positive-consent flag and no local model server available in
    this test environment, naming inspection fails closed with the fixed
    ``study-name-inspection-failed`` code (never a raw exception) and the
    manifest status becomes ``failed`` -- a DIFFERENT outcome than the
    default (consent-absent) ``review_required`" case above, proving the
    flag is actually threaded into ``intake_add(..., support_confirmed_no_phi=True)``
    rather than silently ignored.
    """
    workspace = tmp_path / "ws"
    source = tmp_path / "source"
    sentinel = "SENTINEL_DOB_2000-01-01"
    _make_canonical_source(source, sentinel=sentinel)

    result = _run_cli(
        [
            "intake",
            "--source",
            str(source),
            "--workspace",
            str(workspace),
            "--support-confirmed-no-phi",
        ],
        workspace=workspace,
    )

    receipt = json.loads(result.stdout)
    assert _STUDY_HEX_RE.match(receipt["study"]), receipt["study"]
    assert receipt["status"] == "failed"
    assert receipt["linked"] == 3
    assert receipt["errors"] >= 1
    assert result.returncode == 2
    assert result.stderr == (
        f"intake: study={receipt['study']} status=failed linked=3 "
        f"review={receipt['review']} errors={receipt['errors']}\n"
    )

    combined = result.stdout + result.stderr
    assert sentinel not in combined
    assert str(source) not in combined


# --- explicit failed flow (deterministic source-unreadable error) ----------------------------


def test_intake_explicit_study_failed_maps_to_exit_2(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    source = tmp_path / "source"
    sentinel = "SENTINEL_PHONE_555-0100"
    _make_canonical_source(source, sentinel=sentinel)
    unreadable = source / "datasets" / "noperm.csv"
    unreadable.write_text("x", encoding="utf-8")
    os.chmod(unreadable, 0o000)
    try:
        result = _run_cli(
            ["intake", "--study", "FailStudy", "--source", str(source), "--workspace", str(workspace)],
            workspace=workspace,
        )
    finally:
        os.chmod(unreadable, 0o600)

    assert result.returncode == 2
    manifest_path = workspace.resolve() / "intake" / "FailStudy" / "intake_manifest.json"
    assert result.stdout == _compact_receipt(
        study="FailStudy", status="failed", linked=3, review=0, errors=1, manifest_path=manifest_path
    )
    assert result.stderr == "intake: study=FailStudy status=failed linked=3 review=0 errors=1\n"

    combined = result.stdout + result.stderr
    assert sentinel not in combined
    assert "source-unreadable" not in combined  # raw error reason never leaked
    assert "noperm.csv" not in combined


# --- organize honoring the intake v3 decision (0/8/2 boundary reused end to end) --------------


def test_organize_on_review_required_study_prints_fixed_message_and_exits_8(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    source = tmp_path / "source"
    _make_canonical_source(source, sentinel="unused")

    intake_result = _run_cli(
        ["intake", "--source", str(source), "--workspace", str(workspace)],
        workspace=workspace,
    )
    assert intake_result.returncode == 8
    study = json.loads(intake_result.stdout)["study"]

    organize_result = _run_cli(
        ["organize", "--study", study, "--workspace", str(workspace)],
        workspace=workspace,
    )

    assert organize_result.returncode == 8
    assert organize_result.stdout == ""
    assert organize_result.stderr == "intake_review_required\n"


def test_organize_on_failed_study_prints_fixed_message_and_exits_2(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    source = tmp_path / "source"
    _make_canonical_source(source, sentinel="unused")
    unreadable = source / "datasets" / "noperm2.csv"
    unreadable.write_text("x", encoding="utf-8")
    os.chmod(unreadable, 0o000)
    try:
        intake_result = _run_cli(
            ["intake", "--study", "FailOrg", "--source", str(source), "--workspace", str(workspace)],
            workspace=workspace,
        )
    finally:
        os.chmod(unreadable, 0o600)
    assert intake_result.returncode == 2

    organize_result = _run_cli(
        ["organize", "--study", "FailOrg", "--workspace", str(workspace)],
        workspace=workspace,
    )

    assert organize_result.returncode == 2
    assert organize_result.stdout == ""
    assert organize_result.stderr == "intake_failed\n"


def test_organize_on_never_intaken_study_falls_through_to_main_typed_handler(tmp_path: Path) -> None:
    """A study with no manifest at all raises ``IntakeManifestError``
    (``intake_manifest_missing``), NOT ``IntakeNotReadyError`` --
    ``_cmd_organize`` only catches the latter, so this one is handled by
    ``main()``'s generic typed-exception boundary and prints the manifest
    error's own fixed code, still exit 2, still no traceback."""
    workspace = tmp_path / "ws"
    result = _run_cli(
        ["organize", "--study", "NeverIntaken", "--workspace", str(workspace)],
        workspace=workspace,
    )
    assert result.returncode == 2
    assert result.stderr == "intake_manifest_missing\n"
    assert "Traceback" not in result.stderr


def test_organize_on_malformed_manifest_prints_fixed_code_no_traceback(tmp_path: Path) -> None:
    """A study directory whose ``intake_manifest.json`` exists but fails
    v3 schema validation (here: parses as JSON but is not a v3 manifest at
    all) raises ``IntakeManifestError("intake_manifest_invalid")`` from
    ``load_intake_manifest`` -- like the missing-manifest case above, this
    is NOT ``IntakeNotReadyError``, so it is handled by ``main()``'s
    generic typed-exception boundary, not ``_cmd_organize``'s own catch."""
    workspace = tmp_path / "ws"
    study_dir = workspace / "intake" / "MalformedStudy"
    study_dir.mkdir(parents=True)
    os.chmod(study_dir, 0o700)
    sentinel = "SENTINEL_MALFORMED_CONTENT_42"
    manifest_path = study_dir / "intake_manifest.json"
    manifest_path.write_text(json.dumps({"not": "a v3 manifest", "sentinel": sentinel}), encoding="utf-8")
    os.chmod(manifest_path, 0o600)

    result = _run_cli(
        ["organize", "--study", "MalformedStudy", "--workspace", str(workspace)],
        workspace=workspace,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "intake_manifest_invalid\n"
    assert "Traceback" not in result.stderr
    assert sentinel not in result.stdout + result.stderr


# --- genuine end-to-end IntakeManifestError via a hostile (symlinked) workspace ---------------


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink safety")
def test_symlinked_intake_dir_yields_fixed_code_no_traceback(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (workspace / "intake").symlink_to(elsewhere)
    source = tmp_path / "source"
    _make_canonical_source(source, sentinel="unused")

    result = _run_cli(
        ["intake", "--study", "SymStudy", "--source", str(source), "--workspace", str(workspace)],
        workspace=workspace,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "intake-tree-unsafe\n"
    assert "Traceback" not in result.stderr


# --- main()'s generic typed-exception boundary for branches unreachable via normal CLI flow ---


def _import_main():
    import phi_engine.cli.main as cli_main

    return cli_main


def test_main_pipeline_busy_error_prints_fixed_code_not_raw_lock_path(monkeypatch, tmp_path, capsys) -> None:
    cli_main = _import_main()
    from phi_engine.utils.pipeline_lock import PipelineBusyError

    secret_lock_path = tmp_path / "secret" / ".SomeStudy.pipeline.lock"

    def _raise_busy(*args, **kwargs):
        raise PipelineBusyError(secret_lock_path)

    monkeypatch.setattr("phi_engine.pipeline.intake.intake_add", _raise_busy)
    monkeypatch.setattr(cli_main, "_set_workspace_env", lambda args: None)

    exit_code = cli_main.main(
        ["intake", "--study", "AnyStudy", "--source", str(tmp_path)]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.err == "pipeline_busy\n"
    assert str(secret_lock_path) not in captured.err
    assert "Traceback" not in captured.err


def test_main_verified_source_error_prints_fixed_reason_no_traceback(monkeypatch, tmp_path, capsys) -> None:
    cli_main = _import_main()
    from phi_engine.pipeline.verified_source import VerifiedSourceError

    def _raise_verified(*args, **kwargs):
        raise VerifiedSourceError("source-target-outside-root")

    monkeypatch.setattr("phi_engine.pipeline.intake.intake_add", _raise_verified)
    monkeypatch.setattr(cli_main, "_set_workspace_env", lambda args: None)

    exit_code = cli_main.main(
        ["intake", "--study", "AnyStudy", "--source", str(tmp_path)]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.err == "source-target-outside-root\n"
    assert "Traceback" not in captured.err


def test_main_reraises_unknown_exception_types(monkeypatch, tmp_path) -> None:
    """Only the fixed, named typed-exception set is swallowed -- anything
    else must still surface (never silently downgraded to a generic exit
    code), matching the pre-existing ``main()`` contract for unclassified
    failures."""
    cli_main = _import_main()

    class _SomeOtherBug(RuntimeError):
        pass

    def _raise_other(*args, **kwargs):
        raise _SomeOtherBug("boom")

    monkeypatch.setattr("phi_engine.pipeline.intake.intake_add", _raise_other)
    monkeypatch.setattr(cli_main, "_set_workspace_env", lambda args: None)

    with pytest.raises(_SomeOtherBug):
        cli_main.main(["intake", "--study", "AnyStudy", "--source", str(tmp_path)])


def test_main_does_not_swallow_spoofed_same_named_exception(monkeypatch, tmp_path, capsys) -> None:
    """A same-NAMED but otherwise unrelated exception class (not the real
    ``phi_engine.pipeline.intake.IntakeManifestError``) must never be
    treated as typed: dispatch is by ``isinstance`` against the real
    imported class, never by ``exc.__class__.__name__``. If this ever
    regresses to name-based dispatch, a bug (or an attacker who can
    influence which exception is raised) could smuggle an arbitrary
    ``.code``/``.reason`` string straight to stderr under a fixed-code
    disguise."""
    cli_main = _import_main()

    class IntakeManifestError(RuntimeError):  # noqa: N818 -- deliberately shadows the real name
        def __init__(self, code: str) -> None:
            self.code = code
            super().__init__(code)

    spoofed_payload = "RAW_SECRET_PATH_/should/never/reach/stderr"

    def _raise_spoofed(*args, **kwargs):
        raise IntakeManifestError(spoofed_payload)

    monkeypatch.setattr("phi_engine.pipeline.intake.intake_add", _raise_spoofed)
    monkeypatch.setattr(cli_main, "_set_workspace_env", lambda args: None)

    with pytest.raises(IntakeManifestError):
        cli_main.main(["intake", "--study", "AnyStudy", "--source", str(tmp_path)])

    captured = capsys.readouterr()
    assert spoofed_payload not in captured.err
    assert captured.err == ""


# --- lexical --workspace preservation: symlinked / dotdot-after-symlink CLI cases -------------


def _run_cli_raw(args: list[str]) -> subprocess.CompletedProcess:
    """Like ``_run_cli`` but WITHOUT its ``data/raw/dummy`` pre-seed side
    effect. Every test using this helper always supplies an explicit
    ``--study`` (so config's own STUDY_NAME auto-detection never runs),
    and the workspace argument itself may be a symlink or contain a ``..``
    segment -- a pre-seeding ``mkdir`` through that argument would write
    through the very evidence these tests assert is never followed."""
    env = dict(os.environ)
    env.pop("STUDY_NAME", None)
    env.pop("PHI_WORKSPACE", None)
    return subprocess.run(
        [sys.executable, "-m", "phi_engine", *args],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink safety")
def test_symlinked_workspace_argument_yields_fixed_code_no_write_through(tmp_path: Path) -> None:
    """``--workspace`` pointing AT a symlink (distinct from
    ``test_symlinked_intake_dir_yields_fixed_code_no_traceback`` above,
    where only ``intake/`` inside an already-real workspace is symlinked)
    must fail the same way: the CLI must lexically preserve the argument
    (expand ``~``/prefix cwd only) instead of ``Path.resolve()``-ing it,
    or the symlink evidence is erased before config's descriptor-relative
    NOFOLLOW ancestry walkers ever get a chance to reject it."""
    real_workspace = tmp_path / "real-workspace"
    real_workspace.mkdir()
    workspace_link = tmp_path / "workspace-link"
    workspace_link.symlink_to(real_workspace)
    source = tmp_path / "source"
    _make_canonical_source(source, sentinel="unused")

    result = _run_cli_raw(
        ["intake", "--study", "SymlinkWsStudy", "--source", str(source), "--workspace", str(workspace_link)]
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "intake-tree-unsafe\n"
    assert "Traceback" not in result.stderr
    assert workspace_link.is_symlink()  # never replaced
    assert list(real_workspace.iterdir()) == []  # no write-through


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink safety")
def test_dotdot_after_symlink_workspace_argument_yields_fixed_code_no_write_through(tmp_path: Path) -> None:
    """A ``..`` segment placed AFTER a symlink component in ``--workspace``
    must not be silently collapsed by the CLI -- collapsing it would erase
    the symlink evidence exactly like ``Path.resolve()`` would. It must
    reach the descriptor-relative walker lexically intact and fail the
    same fixed way, never writing through the symlink target."""
    real = tmp_path / "real"
    (real / "sibling").mkdir(parents=True)
    (real / "escape").mkdir()
    link = tmp_path / "link"
    link.symlink_to(real / "sibling")
    source = tmp_path / "source"
    _make_canonical_source(source, sentinel="unused")

    result = _run_cli_raw(
        [
            "intake",
            "--study",
            "DotDotWsStudy",
            "--source",
            str(source),
            "--workspace",
            str(link / ".." / "escape"),
        ]
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "intake-tree-unsafe\n"
    assert "Traceback" not in result.stderr
    assert list((real / "escape").iterdir()) == []  # no write-through


# --- shared study-name validation: explicit --study rejected before STUDY_NAME/config ----------


@pytest.mark.parametrize(
    "study",
    [
        "../escape",
        "nested/study",
        "trailing.",
        "A" * 129,
        "bad name",
        "bad@name",
        "CON",
        "com1.txt",
    ],
    ids=[
        "path-traversal",
        "path-separator",
        "dot-ending",
        "overlong",
        "invalid-char-space",
        "invalid-char-at",
        "windows-reserved-bare",
        "windows-reserved-with-extension",
    ],
)
def test_invalid_explicit_study_yields_fixed_code_before_config_import(tmp_path: Path, study: str) -> None:
    """Every distinct invalid-name category (path-like, dot-ending,
    overlong, invalid-character, Windows-reserved) collapses to the SAME
    fixed ``invalid_study_name`` public code, exit 2, empty stdout, and no
    traceback/path leakage -- validated through the shared dependency-free
    validator BEFORE ``STUDY_NAME`` is set or ``phi_engine.config.config``
    is imported, so config's own defense-in-depth STUDY_NAME check is
    never reached and can never chain a second traceback."""
    workspace = tmp_path / "ws"

    result = _run_cli_raw(["status", "--study", study, "--workspace", str(workspace)])

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "invalid_study_name\n"
    assert "Traceback" not in result.stderr
    assert str(tmp_path) not in result.stderr
    assert not workspace.exists()  # no config-driven directory creation ever ran


def test_valid_study_name_unaffected_by_shared_validator(tmp_path: Path) -> None:
    """A conservative, always-valid study name must still work end to end
    through the CLI -- the shared validator must not have narrowed the
    accepted set relative to the pre-existing contract."""
    workspace = tmp_path / "ws"
    source = tmp_path / "source"
    _make_canonical_source(source, sentinel="unused")

    result = _run_cli_raw(
        ["intake", "--study", "Valid_Study-01.alpha", "--source", str(source), "--workspace", str(workspace)]
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["study"] == "Valid_Study-01.alpha"