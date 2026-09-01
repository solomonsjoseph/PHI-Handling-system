"""Rewrite plan step 5: the hardened Docker execution boundary for
model-generated code (``control/runner.py::ContainerRunner``). Requires
Docker and the ``phi-sandbox-runner:local`` image built from
``backend/sandbox-runner/Dockerfile``:

    cd backend/sandbox-runner && docker build -t phi-sandbox-runner:local .

Skips entirely (not a failure) when the ``docker`` CLI is not on PATH or
the image has not been built -- this module is exercised on a host that
actually has Docker (this dev machine, CI's Docker-enabled runners),
never assumed present everywhere ``pytest tests -q`` runs.

Required coverage (plan step 5 and its Verification section): network is
unreachable from inside; a write outside the workspace fails; the
provider key is absent from the child environment; the PID limit stops a
fork bomb; the timeout kills a hanging script.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time

import pytest
from phi_core.control.runner import (
    DEFAULT_IMAGE,
    ContainerRunner,
    ContainerRunnerError,
    ContainerTimeout,
    ContainerWorkerFailure,
    gvisor_runtime_available,
)
from phi_core.control.sandbox import get_sandbox_error_detail


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(["docker", "info"], capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _image_available() -> bool:
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", DEFAULT_IMAGE],
            capture_output=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


needs_docker = pytest.mark.skipif(
    not _docker_available(), reason="docker not reachable on this host"
)
needs_image = pytest.mark.skipif(
    _docker_available() and not _image_available(),
    reason=f"{DEFAULT_IMAGE} not built -- run `docker build` in backend/sandbox-runner/",
)


@pytest.fixture()
def runner() -> ContainerRunner:
    return ContainerRunner()


# ---------------------------------------------------------------------------
# Basic execution: all four return kinds actually round-trip.
# ---------------------------------------------------------------------------


@needs_docker
@needs_image
def test_count_kind_round_trips(runner):
    result = runner.run(
        source_files={"entry.py": "def run():\n    return 42\n"},
        entrypoint="entry.py", inputs={}, return_kind="count", timeout_s=15,
    )
    try:
        assert result.payload == 42
        assert result.runtime_used
        assert result.memory_ceiling_enforced is True
        assert result.wall_seconds >= 0
    finally:
        result.cleanup()


@needs_docker
@needs_image
def test_status_kind_round_trips(runner):
    result = runner.run(
        source_files={"entry.py": "def run():\n    return 'clean'\n"},
        entrypoint="entry.py", inputs={}, return_kind="status", timeout_s=15,
    )
    try:
        assert result.payload == "clean"
    finally:
        result.cleanup()


@needs_docker
@needs_image
def test_json_kind_round_trips(runner):
    result = runner.run(
        source_files={"entry.py": "import json\ndef run():\n    return json.dumps({'a': 1, 'b': [1, 2, 3]})\n"},
        entrypoint="entry.py", inputs={}, return_kind="json", timeout_s=15,
    )
    try:
        assert json.loads(result.payload) == {"a": 1, "b": [1, 2, 3]}
    finally:
        result.cleanup()


@needs_docker
@needs_image
def test_path_kind_round_trips_and_survives_until_cleanup(runner):
    result = runner.run(
        source_files={
            "entry.py": (
                "from pathlib import Path\n"
                "def run():\n"
                "    Path('/workspace/out.csv').write_text('col\\n1\\n')\n"
                "    return 'out.csv'\n"
            )
        },
        entrypoint="entry.py", inputs={}, return_kind="path", timeout_s=15,
    )
    try:
        assert result.payload == "out.csv"
        written = result.workspace_path / result.payload
        assert written.is_file()
        assert written.read_text() == "col\n1\n"
    finally:
        result.cleanup()
    assert not written.exists(), "cleanup() must remove the staging tree"


@needs_docker
@needs_image
def test_dataset_input_is_mounted_read_only_at_a_fixed_path(runner, tmp_path):
    dataset = tmp_path / "dataset.csv"
    dataset.write_text("age\n45\n")
    result = runner.run(
        source_files={
            "entry.py": (
                "import json\n"
                "def run():\n"
                "    with open('/data/dataset.csv') as f:\n"
                "        return json.dumps(f.read())\n"
            )
        },
        entrypoint="entry.py", inputs={"dataset.csv": str(dataset)},
        return_kind="json", timeout_s=15,
    )
    try:
        assert json.loads(result.payload) == "age\n45\n"
    finally:
        result.cleanup()


@needs_docker
@needs_image
def test_dataset_input_mount_is_read_only(runner, tmp_path):
    dataset = tmp_path / "dataset.csv"
    dataset.write_text("age\n45\n")
    try:
        result = runner.run(
            source_files={
                "entry.py": "def run():\n    open('/data/dataset.csv', 'w').write('tampered')\n    return 'wrote'\n"
            },
            entrypoint="entry.py", inputs={"dataset.csv": str(dataset)},
            return_kind="status", timeout_s=15,
        )
        pytest.fail(f"writing to a read-only dataset mount must fail, got {result.payload!r}")
    except ContainerWorkerFailure as exc:
        assert exc.kind in ("OSError", "PermissionError")
    assert dataset.read_text() == "age\n45\n", "the host-side dataset file must be untouched"


# ---------------------------------------------------------------------------
# Required scenario: network is unreachable from inside.
# ---------------------------------------------------------------------------


@needs_docker
@needs_image
def test_network_is_unreachable_from_inside(runner):
    result = runner.run(
        source_files={
            "entry.py": (
                "import json, socket\n"
                "def run():\n"
                "    try:\n"
                "        socket.create_connection(('8.8.8.8', 53), timeout=3)\n"
                "        return json.dumps('connected')\n"
                "    except OSError as e:\n"
                "        return json.dumps(repr(e))\n"
            )
        },
        entrypoint="entry.py", inputs={}, return_kind="json", timeout_s=15,
    )
    try:
        outcome = json.loads(result.payload)
        assert outcome != "connected"
        assert "PermissionError" in outcome or "Network" in outcome or "unreachable" in outcome.lower()
    finally:
        result.cleanup()


@needs_docker
@needs_image
def test_no_dns_resolution_possible(runner):
    result = runner.run(
        source_files={
            "entry.py": (
                "import json, socket\n"
                "def run():\n"
                "    try:\n"
                "        socket.gethostbyname('example.com')\n"
                "        return json.dumps('resolved')\n"
                "    except OSError as e:\n"
                "        return json.dumps(repr(e))\n"
            )
        },
        entrypoint="entry.py", inputs={}, return_kind="json", timeout_s=15,
    )
    try:
        assert json.loads(result.payload) != "resolved"
    finally:
        result.cleanup()


# ---------------------------------------------------------------------------
# Required scenario: a write outside the workspace fails.
# ---------------------------------------------------------------------------


@needs_docker
@needs_image
def test_write_outside_workspace_fails(runner):
    with pytest.raises(ContainerWorkerFailure) as excinfo:
        runner.run(
            source_files={
                "entry.py": "def run():\n    open('/etc/phi-escape-test', 'w').write('escaped')\n    return 'wrote'\n"
            },
            entrypoint="entry.py", inputs={}, return_kind="status", timeout_s=15,
        )
    assert excinfo.value.kind in ("OSError", "PermissionError")


@needs_docker
@needs_image
def test_write_to_read_only_input_mount_fails(runner):
    with pytest.raises(ContainerWorkerFailure) as excinfo:
        runner.run(
            source_files={"entry.py": "def run():\n    open('/input/entry.py', 'a').write('x')\n    return 'wrote'\n"},
            entrypoint="entry.py", inputs={}, return_kind="status", timeout_s=15,
        )
    assert excinfo.value.kind in ("OSError", "PermissionError")


# ---------------------------------------------------------------------------
# Required scenario: the provider key (and every other credential) is
# absent from the child environment; only a controlled PATH is present.
# ---------------------------------------------------------------------------


@needs_docker
@needs_image
def test_no_credentials_and_only_a_controlled_path_reach_the_container(runner):
    result = runner.run(
        source_files={
            "entry.py": (
                "import json, os\n"
                "def run():\n"
                "    return json.dumps({\n"
                "        'ANTHROPIC_API_KEY': os.environ.get('ANTHROPIC_API_KEY'),\n"
                "        'OPENAI_API_KEY': os.environ.get('OPENAI_API_KEY'),\n"
                "        'MONGO_URL': os.environ.get('MONGO_URL'),\n"
                "        'APP_ENCRYPTION_KEY': os.environ.get('APP_ENCRYPTION_KEY'),\n"
                "        'ATTESTATION_SIGNING_KEY': os.environ.get('ATTESTATION_SIGNING_KEY'),\n"
                "        'HOME': os.environ.get('HOME'),\n"
                "        'PATH': os.environ.get('PATH'),\n"
                "    })\n"
            )
        },
        entrypoint="entry.py", inputs={}, return_kind="json", timeout_s=15,
    )
    try:
        snapshot = json.loads(result.payload)
    finally:
        result.cleanup()
    for credential_key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MONGO_URL", "APP_ENCRYPTION_KEY", "ATTESTATION_SIGNING_KEY"):
        assert snapshot[credential_key] is None, f"{credential_key} must never reach the container"
    # HOME cannot be made to disappear from os.environ entirely once
    # --user resolves to a real /etc/passwd entry (see runner.py's
    # _build_run_command docstring) -- /nonexistent is the practical
    # equivalent to "absent" here.
    assert snapshot["HOME"] == "/nonexistent"
    assert snapshot["PATH"]
    import os as _os
    assert snapshot["PATH"] != _os.environ.get("PATH"), "PATH must be runner-controlled, never the host process's own"


# ---------------------------------------------------------------------------
# Required scenario: the PID limit stops a fork bomb.
# ---------------------------------------------------------------------------


@needs_docker
@needs_image
def test_pids_limit_stops_a_fork_bomb(runner):
    result = runner.run(
        source_files={
            "entry.py": (
                "import json, os\n"
                "def run():\n"
                "    forked = 0\n"
                "    pids = []\n"
                "    try:\n"
                "        for _ in range(1000):\n"
                "            pid = os.fork()\n"
                "            if pid == 0:\n"
                "                os._exit(0)\n"
                "            pids.append(pid)\n"
                "            forked += 1\n"
                "    except OSError:\n"
                "        pass\n"
                "    for p in pids:\n"
                "        try:\n"
                "            os.waitpid(p, 0)\n"
                "        except OSError:\n"
                "            pass\n"
                "    return json.dumps({'forked': forked})\n"
            )
        },
        entrypoint="entry.py", inputs={}, return_kind="json", timeout_s=20,
    )
    try:
        outcome = json.loads(result.payload)
    finally:
        result.cleanup()
    from phi_core.control import limits as limits_module

    assert 0 < outcome["forked"] < 1000, (
        "the fork bomb must be stopped by --pids-limit well short of the "
        f"requested 1000 (got {outcome['forked']}, ceiling is {limits_module.MAX_CONTAINER_PIDS})"
    )


# ---------------------------------------------------------------------------
# Required scenario: the timeout kills a hanging script.
# ---------------------------------------------------------------------------


@needs_docker
@needs_image
def test_timeout_kills_a_hanging_script(runner):
    start = time.monotonic()
    with pytest.raises(ContainerTimeout):
        runner.run(
            source_files={"entry.py": "import time\ndef run():\n    time.sleep(300)\n    return 'never'\n"},
            entrypoint="entry.py", inputs={}, return_kind="status", timeout_s=3,
        )
    elapsed = time.monotonic() - start
    assert elapsed < 15, f"the container must be killed promptly at the 3s budget, took {elapsed:.1f}s"


# ---------------------------------------------------------------------------
# Worker exceptions surface as structured, PHI-safe ContainerWorkerFailure.
# ---------------------------------------------------------------------------

@needs_docker
@needs_image
def test_worker_exception_never_forwards_free_text_but_detail_ref_recovers_it(runner):
    with pytest.raises(ContainerWorkerFailure) as excinfo:
        runner.run(
            source_files={
                "entry.py": (
                    "def run():\n"
                    "    raise ValueError(\n"
                    "        'row parse failed for Amelia Cross, SSN 555-19-2231, '\n"
                    "        'DOB 1972-11-03, MRN MR7743211'\n"
                    "    )\n"
                )
            },
            entrypoint="entry.py", inputs={}, return_kind="status", timeout_s=15,
        )
    exc = excinfo.value
    top_level = str(exc)
    assert "Amelia Cross" not in top_level
    assert "555-19-2231" not in top_level
    assert exc.diagnostic["kind"] == "ValueError"
    assert exc.diagnostic["code"] == "runtime_error"

    detail = get_sandbox_error_detail(exc.diagnostic["detail_ref"])
    assert detail is not None
    assert "555-19-2231" not in detail
    assert "1972-11-03" not in detail
    assert "MR7743211" not in detail


@needs_docker
@needs_image
def test_worker_returning_wrong_return_kind_is_caught_and_reported_safely(runner):
    """The shim's own in-container pre-check (runner_shim.py::_validate,
    which cannot import phi_core.control.sandbox at all -- see that
    file's module docstring) catches this before the result even reaches
    the host's authoritative re-validation, and reports it through the
    same {"ok": false, "type": "ValueError", ...} protocol a genuine bug
    in the worker's own code would use -- so this surfaces as a
    ContainerWorkerFailure, not a host-side SandboxReturnContractViolation,
    unlike the multiprocessing path where the exact exception type
    survives the boundary (see sandbox.py's _OWN_EXCEPTION_TYPES)."""
    with pytest.raises(ContainerWorkerFailure) as excinfo:
        runner.run(
            source_files={"entry.py": "def run():\n    return 'this is not a lowercase status token, it has SPACES'\n"},
            entrypoint="entry.py", inputs={}, return_kind="status", timeout_s=15,
        )
    assert excinfo.value.kind == "ValueError"


# ---------------------------------------------------------------------------
# Infrastructure-level failures: missing image, no run() function.
# ---------------------------------------------------------------------------


@needs_docker
@needs_image
def test_entrypoint_with_no_run_function_is_a_container_runner_error(runner):
    with pytest.raises(ContainerWorkerFailure) as excinfo:
        runner.run(
            source_files={"entry.py": "x = 1\n"},
            entrypoint="entry.py", inputs={}, return_kind="status", timeout_s=15,
        )
    assert excinfo.value.kind == "AttributeError"


@needs_docker
def test_missing_image_raises_container_runner_error():
    runner = ContainerRunner(image="phi-sandbox-runner:does-not-exist")
    with pytest.raises(ContainerRunnerError):
        runner.run(
            source_files={"entry.py": "def run():\n    return 1\n"},
            entrypoint="entry.py", inputs={}, return_kind="count", timeout_s=15,
        )


# ---------------------------------------------------------------------------
# gVisor detection is recorded, never assumed either way.
# ---------------------------------------------------------------------------


def test_gvisor_availability_is_a_plain_bool():
    assert isinstance(gvisor_runtime_available(), bool)


@needs_docker
@needs_image
def test_runtime_used_is_recorded_on_every_result(runner):
    result = runner.run(
        source_files={"entry.py": "def run():\n    return 1\n"},
        entrypoint="entry.py", inputs={}, return_kind="count", timeout_s=15,
    )
    try:
        if gvisor_runtime_available():
            assert result.runtime_used == "runsc"
        else:
            assert "runc" in result.runtime_used
            assert "not available" in result.runtime_used
    finally:
        result.cleanup()
