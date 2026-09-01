"""ContainerRunner: the hardened per-execution boundary for MODEL-GENERATED
code (rewrite plan step 5, decision 28). ``control/sandbox.py``'s
``run_isolated`` multiprocessing path stays the boundary for trusted
first-party raw-data helpers; ONLY generated code (Schema/Executor, steps
9-11) is routed through this module -- a genuine OS-level container, not a
same-kernel ``multiprocessing`` child.

One Docker container per execution:

- ``--network none``: no interface at all beyond loopback, which is a
  strictly stronger guarantee than an egress-deny ruleset on a bridge
  network -- there is no routing table capable of reaching
  169.254.169.254, 10.0.0.0/8, 172.16.0.0/12, or 192.168.0.0/16 (or
  anywhere else) in the first place, satisfied by construction rather
  than by rule.
- ``--read-only`` rootfs. The only writable path is ``/workspace`` -- see
  "Why /workspace is a bind mount, not a literal tmpfs" below.
- Non-root ``--user``, ``--cap-drop ALL``, ``--security-opt
  no-new-privileges``, and a custom deny-list ``--security-opt seccomp=``
  profile (``seccomp-profile.json``) closing ptrace/mount/unshare/
  kexec_load and their close relatives, plus the entire socket family.
- cgroup ``--cpus``/``--memory``/``--pids-limit`` ceilings.
- The dataset(s) bind-mounted read-only at a fixed path (``/data/<name>``);
  the generated source (plus this module's zero-dependency
  ``runner_shim.py``, baked into the image) bind-mounted read-only at a
  fixed path (``/input``).
- No environment variables beyond a controlled ``PATH`` (the base image's
  own default) and no ``HOME`` (explicitly unset in the Dockerfile, the
  closest Docker equivalent to ``sandbox.py``'s multiprocessing path
  omitting it from the child env allowlist entirely). No provider key, no
  ``MONGO_URL``, no ``APP_ENCRYPTION_KEY`` -- Docker never inherits the
  host process's environment into a container unless explicitly passed
  with ``-e``/``--env``, which this module never does.
- ``--runtime runsc`` when gVisor is registered with the host's Docker
  daemon, detected once per process and recorded on every
  :class:`ContainerRunResult` either way (decision 28: absence of
  ``runsc`` on macOS Docker Desktop is expected and reported, never
  silently treated as equivalent to a gVisor run).

Why /workspace is a bind mount, not a literal tmpfs
------------------------------------------------------
The plan names ``/workspace`` as a tmpfs mount "discarded when the
container exits." Two independent things rule that out empirically, not
just as a style preference:

1. ``return_kind="path"`` results (step 9's ``run_generated``, and
   eventually Executor's actual transformed-dataset export) fundamentally
   require the *host* to read a real file back out of the workspace after
   execution -- that is the entire point of the "path" contract. A tmpfs
   mount's backing store is purely in-kernel, scoped to the container's
   own mount namespace; it does not exist anywhere the host process can
   reach once the container is gone.
2. Verified directly against this host's Docker daemon while building
   this module: ``docker cp <container>:/workspace/. <dest>`` against a
   ``--tmpfs /workspace`` mount silently returns an EMPTY destination,
   even while the container is still running and ``docker exec ... cat
   /workspace/.container_result.json`` from inside the same container
   proves the file is genuinely there. ``docker cp`` reads through the
   image storage driver's own layered filesystem view, which a tmpfs
   mount -- a pure kernel VFS mount, invisible to that layer -- never
   participates in. There is no supported Docker mechanism to retrieve
   tmpfs contents from the host side at all.

Given that, this module gives ``/workspace`` the *lifecycle property* the
plan is actually after -- fresh per run, never reused, torn down the
moment this module is done with it -- via a real per-run host directory
under ``CONTAINER_STAGING_DIR``, bind-mounted read-write, matching
``control/sandbox.py::create_sandbox``/``destroy_sandbox``'s own real-
host-directory lifecycle exactly. ``ContainerRunner`` enforces the disk-
usage ceiling the tmpfs ``size=`` option would have given for free by
checking the total bytes written after the container exits and refusing
the result if it exceeds ``MAX_CONTAINER_WORKSPACE_MB`` -- a post-hoc
check rather than a preventive one, since a plain bind mount has no
native Linux-side size-cap primitive Docker exposes without additional
host infrastructure (a loopback-mounted, size-capped filesystem) that
would not be portable to this platform. ``--cpus``/``--memory``/
``--pids-limit`` remain fully preventive; the step 9 static AST check
gating what code ever reaches this boundary at all is the layer that
actually keeps a malicious multi-gigabyte write from ever being attempted
in the first place.

Every worker failure surfaces as a structured :class:`ContainerWorkerFailure`
sharing ``control.sandbox``'s ``.diagnostic``/``detail_ref``/
``get_sandbox_error_detail`` mechanism, so a data-bearing exception from
generated code is subject to the exact same scrub-then-store-behind-a-ref
discipline as a multiprocessing sandbox worker's. An infrastructure-level
failure (Docker unreachable, the container never producing a result at
all, a seccomp/cap denial killing the interpreter before it could report
anything) raises :class:`ContainerRunnerError` instead -- distinct from a
worker failure the same way ``SandboxError`` vs ``SandboxWorkerFailure``
already are.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Literal
from uuid import uuid4

from phi_core.paths import CONTAINER_STAGING_DIR

from . import limits
from .records import SandboxRecord
from .sandbox import _classify_error_code, _store_error_detail, _validate_return_contract

_SANDBOX_RUNNER_DIR = Path(__file__).resolve().parents[2] / "sandbox-runner"
SECCOMP_PROFILE_PATH = _SANDBOX_RUNNER_DIR / "seccomp-profile.json"
DEFAULT_IMAGE = "phi-sandbox-runner:local"

# Grace window added on top of the caller's own wall-clock budget before
# this module gives up on the `docker` CLI subprocess itself (daemon RPC
# overhead) -- never the container's own execution time, which `--cpus`
# and the explicit timeout on `docker run` below already bound tightly.
_DOCKER_CLI_OVERHEAD_GRACE_SECONDS = 20
_CONTAINER_UID = 10001
_CONTAINER_GID = 10001
_RESULT_FILENAME = ".container_result.json"


class ContainerRunnerError(RuntimeError):
    """Raised on a container-boundary infrastructure failure: Docker
    unreachable, the image missing, the container producing no result at
    all, or the workspace exceeding its disk-usage ceiling. Distinct from
    a worker-level failure -- see :class:`ContainerWorkerFailure` -- the
    same way ``control.sandbox.SandboxError`` vs ``SandboxWorkerFailure``
    are."""


class ContainerTimeout(ContainerRunnerError):
    """Raised when the container exceeds its wall-clock budget."""


class ContainerWorkerFailure(RuntimeError):
    """Structured, PHI-safe diagnostic mirroring
    ``control.sandbox.SandboxWorkerFailure`` exactly, sharing its
    in-process detail store: ``str(exc)`` never embeds the generated
    code's raw exception text, which lives behind ``.diagnostic
    ["detail_ref"]`` instead, reachable only through
    ``control.sandbox.get_sandbox_error_detail``."""

    def __init__(self, *, kind: str, code: str, detail: str) -> None:
        self.kind = kind
        self.code = code
        self.detail_ref = _store_error_detail(detail)
        self.diagnostic = {"kind": kind, "code": code, "detail_ref": self.detail_ref}
        super().__init__(f"{code}[{kind}] ref={self.detail_ref}")


class ContainerRunResult:
    """The validated payload plus the boundary facts acceptance criterion
    10 requires be recorded on every run, not assumed.

    ``workspace_path`` and everything under it survive past
    :meth:`ContainerRunner.run` returning on success (never on a raised
    failure, where nothing useful survives) specifically so a
    ``return_kind="path"`` caller can read the declared artifact back --
    the entire point of that return kind. Call :meth:`cleanup` once done
    reading it; this mirrors ``control.sandbox.destroy_sandbox`` being a
    separate, explicit, caller-owned call rather than something
    ``run_isolated`` does for you."""

    __slots__ = ("payload", "workspace_path", "runtime_used", "memory_ceiling_enforced", "wall_seconds", "_staging_dir")

    def __init__(
        self, payload: object, *, workspace_path: Path, staging_dir: Path,
        runtime_used: str, memory_ceiling_enforced: bool, wall_seconds: float,
    ) -> None:
        self.payload = payload
        self.workspace_path = workspace_path
        self.runtime_used = runtime_used
        self.memory_ceiling_enforced = memory_ceiling_enforced
        self.wall_seconds = wall_seconds
        self._staging_dir = staging_dir

    def cleanup(self) -> None:
        """Remove this run's host-side staging tree (source + workspace).
        Idempotent: safe to call more than once, or on an already-gone
        directory."""
        shutil.rmtree(self._staging_dir, ignore_errors=True)



def _detect_gvisor_runtime() -> str | None:
    """Whether ``runsc`` (gVisor) is registered with this host's Docker
    daemon. Probed once per process via ``docker info``; the result is
    recorded on every :class:`ContainerRunResult` rather than assumed.
    Returns ``None`` (not an error) when Docker itself cannot be reached
    right now -- the actual ``docker run`` invocation in
    :meth:`ContainerRunner.run` surfaces that failure on its own."""
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{json .Runtimes}}"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        runtimes = json.loads(proc.stdout)
    except ValueError:
        return None
    return "runsc" if isinstance(runtimes, dict) and "runsc" in runtimes else None


_GVISOR_RUNTIME = _detect_gvisor_runtime()


def gvisor_runtime_available() -> bool:
    """Whether this process detected ``runsc`` at import time. Exposed
    for the acceptance-output recorder (criterion 10) and tests."""
    return _GVISOR_RUNTIME is not None


def _directory_size_bytes(path: Path) -> int:
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file() and not entry.is_symlink():
            total += entry.stat().st_size
    return total


def _write_source_file(source_dir: Path, name: str, content: str) -> None:
    dest = (source_dir / name).resolve()
    if source_dir.resolve() not in (dest, *dest.parents):
        raise ContainerRunnerError(f"generated source filename escapes the staging directory: {name!r}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")


class ContainerRunner:
    """One Docker container per :meth:`run` call. Not reused across runs:
    every call creates a fresh host-side staging tree and a fresh
    container, and destroys both before returning."""

    def __init__(self, *, image: str = DEFAULT_IMAGE) -> None:
        self.image = image
        self.runtime = _GVISOR_RUNTIME

    def run(
        self,
        source_files: dict[str, str],
        entrypoint: str,
        inputs: dict[str, str],
        *,
        return_kind: Literal["path", "count", "status", "json"],
        timeout_s: int = limits.MAX_CONTAINER_WALL_SECONDS,
    ) -> ContainerRunResult:
        """Run ``entrypoint``'s ``run()`` function inside a hardened
        container. ``source_files`` maps filename -> source text,
        materialized read-only at ``/input``. ``inputs`` maps a fixed
        logical name -> real host path, each bind-mounted read-only at
        ``/data/<name>``. ``return_kind`` is validated on both sides of
        the container boundary, exactly like ``sandbox.run_isolated``.
        """
        run_id = uuid4().hex
        staging = CONTAINER_STAGING_DIR / run_id
        source_dir = staging / "input"
        workspace_dir = staging / "workspace"
        for d in (source_dir, workspace_dir):
            d.mkdir(parents=True, mode=0o700)
        # Container-owned uid must be able to write into the bind-mounted
        # workspace; the host process (running as whatever uid the
        # backend itself runs under) still owns the directory entry.
        _os_chmod_recursive_open(workspace_dir)

        try:
            for name, content in source_files.items():
                _write_source_file(source_dir, name, content)
            entrypoint_module = Path(entrypoint).stem

            docker_cmd = self._build_run_command(source_dir, workspace_dir, inputs, entrypoint_module, return_kind)
            start = time.monotonic()
            try:
                self._run_detached_with_deadline(docker_cmd, timeout_s)
            finally:
                elapsed = time.monotonic() - start

            workspace_bytes = _directory_size_bytes(workspace_dir)
            max_bytes = limits.MAX_CONTAINER_WORKSPACE_MB * 1024 * 1024
            if workspace_bytes > max_bytes:
                raise ContainerRunnerError(
                    f"container workspace grew to {workspace_bytes} bytes, "
                    f"over the {max_bytes} byte ceiling -- result refused"
                )

            raw_result = self._read_result(workspace_dir)
            payload = self._resolve_payload(raw_result, return_kind, workspace_path=workspace_dir)

            # Success: staging survives past this call (see
            # ContainerRunResult's docstring) -- the caller owns cleanup
            # via result.cleanup(), the same way destroy_sandbox is a
            # separate, explicit call for the multiprocessing path.
            return ContainerRunResult(
                payload=payload,
                workspace_path=workspace_dir,
                staging_dir=staging,
                runtime_used=self.runtime or "runc (default; gVisor not available on this host)",
                # Linux cgroup memory accounting is real and kernel-enforced
                # on every platform Docker itself runs on (natively or,
                # as on macOS, inside Docker Desktop's own Linux VM) --
                # unlike control/sandbox.py's RLIMIT_AS-on-Darwin/XNU gap,
                # there is no platform-dependent probe needed here.
                memory_ceiling_enforced=True,
                wall_seconds=elapsed,
            )
        except BaseException:
            # Nothing useful survives a failed run -- clean up immediately
            # rather than leaning on the caller for a path it never got.
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def _build_run_command(
        self, source_dir: Path, workspace_dir: Path, inputs: dict[str, str],
        entrypoint_module: str, return_kind: str,
    ) -> list[str]:
        cmd = [
            "docker", "run", "--rm", "-d",
            "--network", "none",
            "--read-only",
            "--user", f"{_CONTAINER_UID}:{_CONTAINER_GID}",
            # Verified empirically against this host's Docker daemon:
            # `--user` resolving to a real `/etc/passwd` entry makes the
            # runtime derive HOME from that entry (/home/sandboxrunner)
            # whenever HOME is unset OR set to an empty string --
            # overriding both the Dockerfile's own `ENV HOME=""` and an
            # explicit `-e HOME=` at run time. Only a non-empty explicit
            # value beats it, so `/nonexistent` (a path that resolves to
            # nothing and carries no information) is the practical
            # equivalent here to sandbox.py's multiprocessing path
            # omitting HOME from the child env allowlist entirely.
            "-e", "HOME=/nonexistent",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--security-opt", f"seccomp={SECCOMP_PROFILE_PATH}",
            "--cpus", str(limits.MAX_CONTAINER_CPUS),
            "--memory", f"{limits.MAX_CONTAINER_MEMORY_BYTES}b",
            "--memory-swap", f"{limits.MAX_CONTAINER_MEMORY_BYTES}b",
            "--pids-limit", str(limits.MAX_CONTAINER_PIDS),
            "-v", f"{source_dir}:/input:ro",
            "-v", f"{workspace_dir}:/workspace:rw",
        ]
        if self.runtime:
            cmd += ["--runtime", self.runtime]
        for name, host_path in sorted(inputs.items()):
            cmd += ["-v", f"{host_path}:/data/{name}:ro"]
        cmd += [self.image, entrypoint_module, return_kind]
        return cmd

    def _run_detached_with_deadline(self, docker_cmd: list[str], timeout_s: int) -> None:
        """Starts ``docker_cmd`` (must include ``-d``) and blocks until
        the container exits or ``timeout_s`` elapses, killing it
        immediately in the latter case. ``--rm`` on the create command
        means Docker reaps the container itself once it stops, whether
        that stop was natural or a kill; this method never needs its own
        explicit ``docker rm``. The exit code itself is not consulted --
        ``run()``'s own result-file read decides success/failure, since a
        worker that caught its own exception and reported it through
        ``runner_shim.py``'s protocol may still exit 0."""
        try:
            create_proc = subprocess.run(
                docker_cmd, capture_output=True, text=True, timeout=_DOCKER_CLI_OVERHEAD_GRACE_SECONDS,
            )
        except FileNotFoundError:
            raise ContainerRunnerError("docker CLI not found on PATH") from None
        except subprocess.TimeoutExpired:
            raise ContainerRunnerError("docker run -d did not return promptly") from None
        if create_proc.returncode != 0:
            raise ContainerRunnerError(f"docker run failed to start: {create_proc.stderr.strip()[:500]}")
        container_id = create_proc.stdout.strip()

        try:
            subprocess.run(
                ["docker", "wait", container_id], capture_output=True, text=True, timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            subprocess.run(
                ["docker", "kill", container_id],
                capture_output=True, text=True, timeout=_DOCKER_CLI_OVERHEAD_GRACE_SECONDS,
            )
            raise ContainerTimeout(f"container exceeded {timeout_s}s wall-clock limit") from None

    def _read_result(self, workspace_dir: Path) -> dict:
        result_path = workspace_dir / _RESULT_FILENAME
        if not result_path.is_file():
            raise ContainerRunnerError(
                "container produced no .container_result.json -- it never reached "
                "runner_shim.py's own result write, or was killed before doing so"
            )
        try:
            return json.loads(result_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ContainerRunnerError(f"container result file was not valid JSON: {exc}") from None

    def _resolve_payload(self, raw_result: dict, return_kind: str, *, workspace_path: Path) -> object:
        from phi_core.security import scrub_persisted_text

        if not raw_result.get("ok"):
            kind = str(raw_result.get("type") or "UnknownError")
            message = scrub_persisted_text(str(raw_result.get("message") or ""))[:2000]
            raise ContainerWorkerFailure(kind=kind, code=_classify_error_code(kind), detail=message)
        payload = raw_result.get("payload")
        record = SandboxRecord(
            run_id=uuid4().hex, workspace_path=str(workspace_path),
            max_cpu_seconds=limits.MAX_CONTAINER_WALL_SECONDS,
            max_memory_bytes=limits.MAX_CONTAINER_MEMORY_BYTES,
            max_wall_seconds=limits.MAX_CONTAINER_WALL_SECONDS,
            max_output_bytes=limits.MAX_CONTAINER_OUTPUT_BYTES,
        )
        # Authoritative re-validation on the trusted host side, against
        # the SAME contract sandbox.py's multiprocessing path enforces --
        # never trust the container's own runner_shim.py pre-check alone.
        _validate_return_contract(payload, return_kind, record)
        return payload


def _os_chmod_recursive_open(path: Path) -> None:
    """Make ``path`` world-writable (0777) so the container's fixed,
    non-root numeric uid (which never matches this host process's own
    uid) can write into the bind-mounted workspace. Safe here
    specifically because ``path`` is a fresh, per-run directory under
    ``CONTAINER_STAGING_DIR`` (0700 at the parent level, never reused,
    destroyed immediately after this run) -- not a general-purpose
    permissions relaxation."""
    import os

    os.chmod(path, 0o777)
