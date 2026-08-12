"""Garbage-collection invariants.

A campaign must leave no scratch directory behind, and the repo sweep must
remove ignored artifacts only: tracked files, untracked-but-unignored work,
and local credentials all survive.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLEANUP = REPO_ROOT / "scripts" / "cleanup.py"


def _stub_worker(seen: list[str]):
    def _run(args):
        entry, workdir_str, _unmatched = args
        seen.append(workdir_str)
        return {"tier": entry.tier, "scenario_id": entry.scenario_id,
                "seed": entry.seed, "elapsed_s": 0.0,
                "report": {"leak": {"status": "clean"}}}
    return _run


def test_run_offline_removes_the_scratch_dir_it_created(monkeypatch):
    from phi_corpus import campaign
    from phi_corpus.tiers import ladder_for

    seen: list[str] = []
    monkeypatch.setattr(campaign, "_run_one", _stub_worker(seen))
    campaign.run_offline(ladder_for("L0")[:1], jobs=1)

    assert seen, "worker never ran"
    assert not Path(seen[0]).exists(), "campaign left its scratch dir behind"


def test_run_offline_keeps_a_caller_supplied_workdir(monkeypatch, tmp_path):
    from phi_corpus import campaign
    from phi_corpus.tiers import ladder_for

    seen: list[str] = []
    monkeypatch.setattr(campaign, "_run_one", _stub_worker(seen))
    wd = tmp_path / "wd"
    campaign.run_offline(ladder_for("L0")[:1], jobs=1, workdir=wd)

    assert wd.exists(), "caller-owned workdir must not be deleted"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args],
                   check=True, capture_output=True, text=True)


def _sandbox(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _git(tmp_path, "init", "-q", "repo")
    _git(repo, "config", "user.email", "gc@test.local")
    _git(repo, "config", "user.name", "gc-test")
    (repo / ".gitignore").write_text(
        "__pycache__/\ntest_reports/\n.env\n.env.*\n", encoding="utf-8")
    (repo / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", ".gitignore", "src/app.py")
    _git(repo, "commit", "-qm", "seed")

    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "app.pyc").write_bytes(b"\x00")
    (repo / "test_reports" / "corpus" / "run1").mkdir(parents=True)
    (repo / "test_reports" / "corpus" / "run1" / "campaign_report.json").write_text(
        "{}", encoding="utf-8")
    (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (repo / "notes.md").write_text("wip\n", encoding="utf-8")
    return repo

def test_cleanup_apply_preserves_nested_credentials(tmp_path):
    repo = _sandbox(tmp_path)
    protected = repo / "test_reports" / "corpus" / "run1" / ".env.local"
    protected.write_text("SECRET=1\n", encoding="utf-8")
    credentials = protected.with_name("credentials.json")
    credentials.write_text('{"token": "secret"}\n', encoding="utf-8")
    environment = protected.with_name(".env")
    environment.write_text("SECRET=1\n", encoding="utf-8")
    certificate = protected.with_name("client.pem")
    certificate.write_text("CERTIFICATE\n", encoding="utf-8")
    private_key = protected.with_name("client.key")
    private_key.write_text("PRIVATE KEY\n", encoding="utf-8")
    editor_settings = protected.parent / ".vscode" / "settings.json"
    editor_settings.parent.mkdir()
    editor_settings.write_text("{}\n", encoding="utf-8")

    _cleanup(repo, "--apply")

    assert protected.exists(), "nested .env.* files are protected"
    assert credentials.exists(), "nested credentials are protected"
    assert environment.exists(), "nested .env files are protected"
    assert certificate.exists(), "nested .pem files are protected"
    assert private_key.exists(), "nested .key files are protected"
    assert not (repo / "test_reports" / "corpus" / "run1" /
                "campaign_report.json").exists()
    assert editor_settings.exists(), "nested editor state is protected"


def _cleanup(repo: Path, *flags: str) -> str:
    proc = subprocess.run([sys.executable, str(CLEANUP), *flags],
                          cwd=str(repo), check=True, capture_output=True, text=True)
    return proc.stdout


def test_cleanup_dry_run_removes_nothing(tmp_path):
    repo = _sandbox(tmp_path)
    out = _cleanup(repo)

    assert "would remove" in out
    assert "test_reports/" in out
    assert (repo / "test_reports").exists()
    assert (repo / "__pycache__").exists()


def test_cleanup_apply_removes_only_ignored_garbage(tmp_path):
    repo = _sandbox(tmp_path)
    _cleanup(repo, "--apply")

    assert not (repo / "test_reports").exists()
    assert not (repo / "__pycache__").exists()
    assert (repo / ".env").exists(), "credentials are protected at every scope"
    assert (repo / "notes.md").exists(), "untracked-but-unignored work must survive"
    assert (repo / "src" / "app.py").exists()
