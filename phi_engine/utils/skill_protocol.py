"""Skill subprocess contract (Wave 4 B3.x / D3).

The consolidated pipeline is an **orchestrator that invokes 9 skills as
file-path subprocesses** (D3 — hyphenated skill dir names are not importable
module paths, so subprocess-by-path is the invocation mechanism). The
orchestrator and every skill ``run.py`` must agree on ONE result format or they
silently drift; this module is that single contract, mirroring the codebase's
other anti-drift invariants (e.g. ``effective_scrub_config_hash``).

**Wire format.** A skill prints a single marker line to stdout:

    RPLN_SKILL_RESULT:{"skill": "...", "ok": true, "summary": "...", "data": {...}}

plus whatever human/log output it likes. The orchestrator scans stdout for the
*last* marker line (so a child's own subprocess banners can't shadow it) and
pairs it with the process exit code. If no marker is found, a result is
synthesised from the exit code alone (a skill that crashed before emitting is
still accounted for, fail-closed).

**Value-free.** ``summary`` and ``data`` carry NAMES / COUNTS / CODES only —
never a row value — exactly like every other cross-process artifact here.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import phi_engine.config.config as config

__all__ = [
    "SKILL_RESULT_PREFIX",
    "SkillInvocationError",
    "SkillResult",
    "add_common_skill_args",
    "emit_skill_result",
    "invoke_skill",
    "parse_skill_result",
    "skill_run_script",
    "skills_root",
]

#: stdout marker prefixing the JSON result line.
SKILL_RESULT_PREFIX = "RPLN_SKILL_RESULT:"


class SkillInvocationError(RuntimeError):
    """Raised when a skill subprocess cannot be located or launched."""


@dataclass(frozen=True)
class SkillResult:
    """The value-free outcome of one skill invocation."""

    skill: str
    ok: bool
    exit_code: int = 0
    summary: str = ""
    data: dict = field(default_factory=dict)

    def to_payload(self) -> dict:
        return {
            "skill": self.skill,
            "ok": self.ok,
            "exit_code": self.exit_code,
            "summary": self.summary,
            "data": self.data,
        }


# ── Skill-side helpers (imported by each skill run.py) ─────────────────────────


def add_common_skill_args(parser: argparse.ArgumentParser) -> None:
    """Add the uniform ``--study/--run-id/--run-dir`` args every skill accepts."""
    parser.add_argument("--study", required=True, help="Study name (folder under data/raw/).")
    parser.add_argument("--run-id", dest="run_id", default=None, help="Correlating run id.")
    parser.add_argument(
        "--run-dir",
        dest="run_dir",
        default=None,
        help="Per-run working dir (output/<study>/runs/<run_id>/).",
    )


def emit_skill_result(result: SkillResult) -> None:
    """Print the marker result line to stdout (the orchestrator reads this)."""
    print(SKILL_RESULT_PREFIX + json.dumps(result.to_payload(), sort_keys=True), flush=True)


# ── Orchestrator-side helpers ──────────────────────────────────────────────────


def skills_root() -> Path:
    """Absolute path to ``plugins/report-ai-study-pipeline/skills``."""
    return Path(config.BASE_DIR) / "plugins" / "report-ai-study-pipeline" / "skills"


def skill_run_script(skill_name: str) -> Path:
    """Resolve ``skills/<skill_name>/scripts/run.py`` (raises if absent)."""
    path = skills_root() / skill_name / "scripts" / "run.py"
    if not path.is_file():
        raise SkillInvocationError(f"skill entrypoint not found: {path}")
    return path


def parse_skill_result(stdout: str, *, skill: str, exit_code: int) -> SkillResult:
    """Extract the last marker line from *stdout*; synthesise from exit code if absent."""
    found: dict | None = None
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(SKILL_RESULT_PREFIX):
            try:
                candidate = json.loads(stripped[len(SKILL_RESULT_PREFIX) :])
            except ValueError:
                continue
            if isinstance(candidate, dict):
                found = candidate  # keep scanning → last one wins
    if found is None:
        return SkillResult(
            skill=skill,
            ok=(exit_code == 0),
            exit_code=exit_code,
            summary="no skill-result marker emitted; synthesised from exit code",
        )
    _data = found.get("data")
    return SkillResult(
        skill=str(found.get("skill", skill)),
        ok=bool(found.get("ok", exit_code == 0)),
        exit_code=exit_code,
        summary=str(found.get("summary", "")),
        data=_data if isinstance(_data, dict) else {},
    )


def invoke_skill(
    skill_name: str,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: float | None = None,
) -> SkillResult:
    """Run a skill as a file-path subprocess and return its parsed result.

    Uses ``sys.executable`` (the active venv interpreter) so no ``uv``
    re-resolution is needed inside an already-active environment. The skill's
    full stdout is captured to find the marker; its stderr is streamed to the
    orchestrator's stderr for live visibility.
    """
    script = skill_run_script(skill_name)
    cmd = [sys.executable, str(script), *args]
    proc_env = dict(os.environ if env is None else env)
    try:
        completed = subprocess.run(  # noqa: S603
            cmd,
            env=proc_env,
            cwd=str(cwd) if cwd is not None else str(config.BASE_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except OSError as exc:  # launch failure (missing interpreter, perms, …)
        raise SkillInvocationError(f"failed to launch skill {skill_name!r}: {exc}") from exc
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    return parse_skill_result(completed.stdout, skill=skill_name, exit_code=completed.returncode)
