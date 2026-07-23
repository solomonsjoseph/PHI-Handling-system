from __future__ import annotations

import json
import multiprocessing
import os
import queue
import stat
import threading
from types import SimpleNamespace
from pathlib import Path
from typing import Any

import pytest

import phi_engine.config.config as config
import phi_engine.pipeline.run as pipeline_run
import phi_engine.utils.pipeline_lock as pipeline_lock
from phi_engine.pipeline.run import PipelineResult
from phi_engine.utils.pipeline_lock import (
    PipelineBusyError,
    acquire_intake_registry_lock,
    acquire_pipeline_lock,
    held_lock_path,
    intake_registry_lock,
    intake_registry_lock_path,
    is_locally_held,
    lock_path_for,
    release_intake_registry_lock,
    release_pipeline_lock,
)


def _hold_lock_until_released(
    tmp_dir: str,
    study: str,
    ready: Any,
    release: Any,
) -> None:
    import phi_engine.config.config as child_config

    child_config.TMP_DIR = Path(tmp_dir)
    from phi_engine.utils.pipeline_lock import acquire_pipeline_lock, release_pipeline_lock

    acquire_pipeline_lock(study)
    ready.set()
    release.wait(timeout=10)
    release_pipeline_lock(study)


def _hold_intake_registry_lock_until_released(tmp_dir: str, ready: Any, release: Any) -> None:
    import phi_engine.config.config as child_config

    child_config.TMP_DIR = Path(tmp_dir)
    from phi_engine.utils.pipeline_lock import acquire_intake_registry_lock, release_intake_registry_lock

    acquire_intake_registry_lock()
    ready.set()
    release.wait(timeout=10)
    release_intake_registry_lock()


def _acquire_lock_and_crash(tmp_dir: str, study: str, ready: Any) -> None:
    import phi_engine.config.config as child_config

    child_config.TMP_DIR = Path(tmp_dir)
    from phi_engine.utils.pipeline_lock import acquire_pipeline_lock

    acquire_pipeline_lock(study)
    ready.set()
    os._exit(0)


def _report_forked_lock_attempt(study: str, connection: Any) -> None:
    from phi_engine.utils.pipeline_lock import (
        PipelineBusyError,
        acquire_pipeline_lock,
        is_locally_held,
    )

    inherited_owner_visible = is_locally_held()
    try:
        acquire_pipeline_lock(study)
    except PipelineBusyError:
        outcome = "busy"
    else:
        outcome = "acquired"
    connection.send((outcome, inherited_owner_visible))
    connection.close()


def _fork_child_survives_owner_exit(
    tmp_dir: str,
    study: str,
    child_ready: Any,
    child_release: Any,
    child_done: Any,
) -> None:
    import phi_engine.config.config as child_config

    child_config.TMP_DIR = Path(tmp_dir)
    from phi_engine.utils.pipeline_lock import acquire_pipeline_lock

    acquire_pipeline_lock(study)
    child_pid = os.fork()
    if child_pid == 0:
        child_ready.set()
        child_release.wait(timeout=10)
        child_done.set()
        os._exit(0)
    os._exit(0)


@pytest.fixture
def lock_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    release_pipeline_lock()
    release_intake_registry_lock()
    monkeypatch.setattr(config, "TMP_DIR", tmp_path)
    yield tmp_path
    release_pipeline_lock()
    release_intake_registry_lock()


def _spawn_lock_holder(tmp_dir: Path, study: str):
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_lock_until_released,
        args=(str(tmp_dir), study, ready, release),
    )
    process.start()
    assert ready.wait(timeout=10), "child did not acquire the pipeline lock"
    return process, release


def test_lock_contention_is_immediate_and_metadata_only(lock_tmp: Path) -> None:
    process, release = _spawn_lock_holder(lock_tmp, "contention-study")
    try:
        with pytest.raises(PipelineBusyError) as caught:
            acquire_pipeline_lock("contention-study")

        assert caught.value.lock_path == lock_path_for("contention-study")
        assert "pid=" not in str(caught.value)
        assert not is_locally_held()
    finally:
        release.set()
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
    assert process.exitcode == 0


def test_same_thread_recursive_acquisition_is_immediately_busy(lock_tmp: Path) -> None:
    acquire_pipeline_lock("recursive-study")
    try:
        with pytest.raises(PipelineBusyError):
            acquire_pipeline_lock("recursive-study")
        assert held_lock_path() == lock_path_for("recursive-study")
    finally:
        release_pipeline_lock("recursive-study")


def test_same_study_thread_contender_is_immediately_busy(lock_tmp: Path) -> None:
    owner_ready = threading.Event()
    release_owner = threading.Event()
    contender_done = threading.Event()
    outcomes: queue.Queue[str] = queue.Queue()

    def own_lock() -> None:
        acquire_pipeline_lock("thread-study")
        owner_ready.set()
        release_owner.wait(timeout=10)
        release_pipeline_lock("thread-study")

    def contend() -> None:
        try:
            acquire_pipeline_lock("thread-study")
        except PipelineBusyError:
            outcomes.put("busy")
        else:
            outcomes.put("acquired")
            release_pipeline_lock("thread-study")
        finally:
            contender_done.set()

    owner = threading.Thread(target=own_lock)
    contender = threading.Thread(target=contend)
    owner.start()
    assert owner_ready.wait(timeout=5), "owner thread did not acquire the lock"
    contender.start()
    try:
        assert contender_done.wait(timeout=2), "same-study contention blocked"
        assert outcomes.get_nowait() == "busy"
        assert owner.is_alive()
    finally:
        release_owner.set()
        contender.join(timeout=5)
        owner.join(timeout=5)
    assert not owner.is_alive()
    assert not contender.is_alive()


def test_distinct_studies_can_be_owned_concurrently(lock_tmp: Path) -> None:
    release_owners = threading.Event()
    ready = {
        "study-a": threading.Event(),
        "study-b": threading.Event(),
    }
    failures: queue.Queue[BaseException] = queue.Queue()

    def own_lock(study: str) -> None:
        try:
            acquire_pipeline_lock(study)
            ready[study].set()
            release_owners.wait(timeout=10)
            release_pipeline_lock(study)
        except BaseException as exc:
            failures.put(exc)

    owners = [
        threading.Thread(target=own_lock, args=("study-a",)),
        threading.Thread(target=own_lock, args=("study-b",)),
    ]
    for owner in owners:
        owner.start()
    try:
        assert ready["study-a"].wait(timeout=5)
        assert ready["study-b"].wait(timeout=5)
        assert failures.empty()
    finally:
        release_owners.set()
        for owner in owners:
            owner.join(timeout=5)
    assert all(not owner.is_alive() for owner in owners)
    assert failures.empty()


@pytest.mark.skipif(os.name != "posix", reason="POSIX fork inheritance")
def test_fork_child_discards_inherited_owner_and_contends_normally(lock_tmp: Path) -> None:
    context = multiprocessing.get_context("fork")
    read_connection, write_connection = context.Pipe(duplex=False)
    acquire_pipeline_lock("fork-study")
    process = context.Process(
        target=_report_forked_lock_attempt,
        args=("fork-study", write_connection),
    )
    process.start()
    write_connection.close()
    try:
        assert read_connection.poll(5), "fork child did not report lock outcome"
        assert read_connection.recv() == ("busy", False)
    finally:
        read_connection.close()
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        release_pipeline_lock("fork-study")
    assert process.exitcode == 0


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "register_at_fork"),
    reason="POSIX at-fork cleanup",
)
def test_fork_child_does_not_retain_lock_after_owner_death(lock_tmp: Path) -> None:
    context = multiprocessing.get_context("spawn")
    child_ready = context.Event()
    child_release = context.Event()
    child_done = context.Event()
    owner = context.Process(
        target=_fork_child_survives_owner_exit,
        args=(
            str(lock_tmp),
            "fork-release-study",
            child_ready,
            child_release,
            child_done,
        ),
    )
    owner.start()
    assert child_ready.wait(timeout=10), "surviving fork child did not start"
    owner.join(timeout=10)
    assert owner.exitcode == 0

    try:
        acquire_pipeline_lock("fork-release-study")
        release_pipeline_lock("fork-release-study")
    finally:
        child_release.set()
        assert child_done.wait(timeout=10), "surviving fork child did not exit"


def test_process_death_releases_lock_without_inode_cleanup(lock_tmp: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    process = context.Process(
        target=_acquire_lock_and_crash,
        args=(str(lock_tmp), "crash-study", ready),
    )
    process.start()
    assert ready.wait(timeout=10), "child did not acquire the pipeline lock"
    process.join(timeout=10)
    assert process.exitcode == 0

    path = lock_path_for("crash-study")
    inode_after_crash = path.stat().st_ino
    acquire_pipeline_lock("crash-study")
    try:
        assert is_locally_held()
        assert path.stat().st_ino == inode_after_crash
    finally:
        release_pipeline_lock("crash-study")


def test_stale_informational_content_is_overwritten(lock_tmp: Path) -> None:
    path = lock_path_for("stale-study")
    path.write_text("pid=999999\nstudy=stale-study\nrun=abandoned\n", encoding="utf-8")

    acquire_pipeline_lock("stale-study")
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
        assert metadata == {"pid": os.getpid(), "study": "stale-study"}
    finally:
        release_pipeline_lock("stale-study")


@pytest.mark.parametrize(
    "study",
    [
        "",
        ".",
        "..",
        ".hidden",
        "../escape",
        "/absolute",
        "nested/study",
        r"nested\study",
        r"C:\absolute",
        "C:relative",
        " leading",
        "trailing ",
        "trailing.",
        "bad:name",
        "bad?name",
        'bad"name',
        "bad<name",
        "bad>name",
        "bad|name",
        "bad*name",
        "bad\nname",
        "bad\rname",
        "bad\x1fname",
        "bad\x00study",
        "CON",
        "con.txt",
        "NUL",
        "A" * 129,
    ],
)
def test_lock_path_rejects_non_plain_study_names(lock_tmp: Path, study: str) -> None:
    with pytest.raises(ValueError, match="plain folder name"):
        lock_path_for(study)
    with pytest.raises(ValueError, match="plain folder name"):
        acquire_pipeline_lock(study)


@pytest.mark.parametrize(
    "study",
    ["Study", "study-01", "Study_01.alpha", "A" * 128],
)
def test_lock_path_accepts_conservative_portable_study_names(
    lock_tmp: Path,
    study: str,
) -> None:
    assert lock_path_for(study) == lock_tmp / f".{study}.pipeline.lock"


@pytest.mark.skipif(os.name != "posix", reason="POSIX hardlink identity checks")
def test_lock_rejects_hardlinked_entry_without_modifying_target(lock_tmp: Path) -> None:
    target = lock_tmp / "service-owned-target"
    target.write_text("do-not-modify", encoding="utf-8")
    target.chmod(0o640)
    path = lock_path_for("hardlink-study")
    os.link(target, path)

    try:
        with pytest.raises(OSError):
            acquire_pipeline_lock("hardlink-study")
    finally:
        release_pipeline_lock("hardlink-study")

    assert target.read_text(encoding="utf-8") == "do-not-modify"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert target.stat().st_nlink == 2


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink safety")
def test_lock_rejects_symlink_entry_without_modifying_target(lock_tmp: Path) -> None:
    target = lock_tmp / "symlink-target"
    target.write_text("do-not-modify", encoding="utf-8")
    path = lock_path_for("symlink-study")
    path.symlink_to(target)

    with pytest.raises(OSError):
        acquire_pipeline_lock("symlink-study")

    assert target.read_text(encoding="utf-8") == "do-not-modify"


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink safety")
def test_lock_rejects_symlinked_parent(
    lock_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_parent = lock_tmp / "real-parent"
    real_parent.mkdir()
    symlink_parent = lock_tmp / "symlink-parent"
    symlink_parent.symlink_to(real_parent, target_is_directory=True)
    monkeypatch.setattr(config, "TMP_DIR", symlink_parent)

    with pytest.raises(OSError):
        acquire_pipeline_lock("parent-symlink-study")

    assert list(real_parent.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink safety")
def test_lock_rejects_symlinked_grandparent_ancestry(
    lock_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A symlinked ANCESTOR above the immediate lock-parent directory
    (not the immediate parent itself) must also fail closed -- proving
    the shared descriptor-walk helper checks every segment, not merely
    the leaf ``TMP_DIR`` node."""
    real_root = lock_tmp / "real_root"
    real_root.mkdir()
    alias = lock_tmp / "alias_root"
    alias.symlink_to(real_root, target_is_directory=True)
    monkeypatch.setattr(config, "TMP_DIR", alias / "tmp")

    with pytest.raises(OSError):
        acquire_pipeline_lock("grandparent-symlink-study")

    assert list(real_root.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX canonical inode checks")
def test_lock_checks_canonical_entry_identity_before_truncate(
    lock_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = lock_path_for("replacement-study")
    path.write_text("original-content", encoding="utf-8")
    displaced = lock_tmp / "displaced-lock"
    real_try_lock = pipeline_lock._try_advisory_lock

    def lock_then_replace(descriptor: Any, lock_path: Path) -> None:
        real_try_lock(descriptor, lock_path)
        lock_path.rename(displaced)
        lock_path.write_text("replacement-content", encoding="utf-8")

    monkeypatch.setattr(pipeline_lock, "_try_advisory_lock", lock_then_replace)

    with pytest.raises(OSError):
        acquire_pipeline_lock("replacement-study")

    assert displaced.read_text(encoding="utf-8") == "original-content"
    assert path.read_text(encoding="utf-8") == "replacement-content"


def test_lock_permissions_and_release_never_unlink_inode(lock_tmp: Path) -> None:
    path = lock_path_for("persistent-study")

    acquire_pipeline_lock("persistent-study")
    assert held_lock_path() == path
    inode = path.stat().st_ino
    if os.name == "posix":
        assert stat.S_IMODE(lock_tmp.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    release_pipeline_lock("persistent-study")

    assert path.is_file()
    assert path.stat().st_ino == inode

    acquire_pipeline_lock("persistent-study")
    try:
        assert path.stat().st_ino == inode
    finally:
        release_pipeline_lock("persistent-study")


def test_release_close_failure_preserves_local_owner_state(
    lock_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acquire_pipeline_lock("close-failure-study")
    real_close = os.close

    def fail_close(_descriptor: int) -> None:
        raise OSError("injected close failure")

    monkeypatch.setattr(pipeline_lock.os, "close", fail_close)
    with pytest.raises(OSError, match="injected close failure"):
        release_pipeline_lock("close-failure-study")
    assert held_lock_path() == lock_path_for("close-failure-study")

    monkeypatch.setattr(pipeline_lock.os, "close", real_close)
    release_pipeline_lock("close-failure-study")
    assert not is_locally_held()


def test_run_pipeline_owns_the_lock_for_the_operational_body(
    lock_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_locked_run(study: str, jurisdiction: str) -> PipelineResult:
        observed["locally_held"] = is_locally_held()
        observed["held_path"] = held_lock_path()
        return PipelineResult(
            study=study,
            jurisdiction=jurisdiction,
            run_id="run-under-lock",
            exit_code=0,
            message="complete",
        )

    monkeypatch.setattr(pipeline_run, "_run_pipeline_locked", fake_locked_run)

    result = pipeline_run.run_pipeline("owned-study", "us")

    assert result.exit_code == 0
    assert observed == {
        "locally_held": True,
        "held_path": lock_path_for("owned-study"),
    }
    assert not is_locally_held()
    assert lock_path_for("owned-study").is_file()


def test_run_pipeline_releases_lock_when_operational_body_raises(
    lock_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OperationalBodyError(RuntimeError):
        pass

    def fail_locked_run(_study: str, _jurisdiction: str) -> PipelineResult:
        raise OperationalBodyError("body failed")

    monkeypatch.setattr(pipeline_run, "_run_pipeline_locked", fail_locked_run)

    with pytest.raises(OperationalBodyError):
        pipeline_run.run_pipeline("exception-study", "us")

    acquire_pipeline_lock("exception-study")
    release_pipeline_lock("exception-study")


def test_run_pipeline_maps_release_failure_without_exception_text(
    lock_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_locked_run(study: str, jurisdiction: str) -> PipelineResult:
        return PipelineResult(
            study=study,
            jurisdiction=jurisdiction,
            run_id="release-failure-run",
            exit_code=0,
            message="complete",
        )

    def fail_release(_study: str) -> None:
        raise OSError("sensitive injected close detail")

    real_release = pipeline_run.release_pipeline_lock
    monkeypatch.setattr(pipeline_run, "_run_pipeline_locked", fake_locked_run)
    monkeypatch.setattr(pipeline_run, "release_pipeline_lock", fail_release)

    try:
        result = pipeline_run.run_pipeline("release-failure-study", "us")
    finally:
        real_release("release-failure-study")

    assert result.exit_code == 1
    assert result.run_id == "release-failure-run"
    assert result.message == "study pipeline lock release infrastructure failure"
    assert "sensitive" not in result.message


def test_recursive_run_pipeline_call_is_busy(
    lock_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inner_results: list[PipelineResult] = []

    def fake_locked_run(study: str, jurisdiction: str) -> PipelineResult:
        inner_results.append(pipeline_run.run_pipeline(study, jurisdiction))
        return PipelineResult(
            study=study,
            jurisdiction=jurisdiction,
            run_id="outer-run",
            exit_code=0,
            message="complete",
        )

    monkeypatch.setattr(pipeline_run, "_run_pipeline_locked", fake_locked_run)

    outer_result = pipeline_run.run_pipeline("recursive-run-study", "us")

    assert outer_result.exit_code == 0
    assert len(inner_results) == 1
    assert inner_results[0].exit_code == 1
    assert inner_results[0].run_id is None
    assert inner_results[0].message == "study pipeline lock is busy"


def test_run_pipeline_maps_busy_lock_to_study_wide_exit_one_before_source_access(
    lock_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process, release = _spawn_lock_holder(lock_tmp, "busy-study")
    monkeypatch.setattr(
        pipeline_run,
        "load_intake_manifest",
        lambda _study: pytest.fail("mutable study source accessed before lock acquisition"),
    )
    try:
        result = pipeline_run.run_pipeline("busy-study", "us")
    finally:
        release.set()
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)

    assert process.exitcode == 0
    assert result.exit_code == 1
    assert result.run_id is None
    assert result.forms_processed == []
    assert result.forms_held == []
    assert result.published_count == 0
    assert "lock" in result.message.lower()


def test_run_pipeline_maps_lock_infrastructure_failure_to_study_wide_exit_one(
    lock_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_lock(_study: str) -> None:
        raise PermissionError("lock parent is inaccessible")

    monkeypatch.setattr(
        pipeline_run,
        "acquire_pipeline_lock",
        fail_lock,
        raising=False,
    )
    monkeypatch.setattr(
        pipeline_run,
        "load_intake_manifest",
        lambda _study: pytest.fail("source accessed after lock infrastructure failure"),
    )

    result = pipeline_run.run_pipeline("infrastructure-failure-study", "us")

    assert result.exit_code == 1
    assert result.run_id is None
    assert result.forms_processed == []
    assert result.forms_held == []
    assert result.published_count == 0
    assert "lock infrastructure failure" in result.message.lower()


def test_run_pipeline_rejects_invalid_study_before_lock_or_source_access(
    lock_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pipeline_run,
        "load_intake_manifest",
        lambda _study: pytest.fail("source accessed for malformed study"),
    )

    result = pipeline_run.run_pipeline("../escape", "us")

    assert result.exit_code == 2
    assert result.run_id is None
    assert list(lock_tmp.iterdir()) == []


def test_locked_body_creates_run_and_resolves_rulebook_before_mutable_intake(
    lock_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    real_datetime = pipeline_run.datetime

    class RecordingDateTime:
        @classmethod
        def now(cls, tz: object):
            calls.append("create_run_id")
            return real_datetime(2026, 7, 14, 10, 11, 12, tzinfo=tz)

    def record_bootstrap(_study: str, _jurisdiction: str) -> None:
        calls.append("bootstrap_privacy")

    def record_load_privacy(_study_root: Path) -> object:
        calls.append("load_privacy")
        return SimpleNamespace(rule_refresh="pinned_only")

    def record_resolve(_privacy: object, **_kwargs: object) -> object:
        calls.append("resolve_rulebook")
        return SimpleNamespace(bundle=object(), protection_weakened=False)

    def record_load_manifest(_study: str) -> dict[str, object]:
        calls.append("load_intake_manifest")
        return {"status": "ready"}

    def record_organize(_study: str) -> dict[str, object]:
        calls.append("organize")
        return {"datasets": [], "review_bucket": []}

    monkeypatch.setattr(pipeline_run, "datetime", RecordingDateTime)
    monkeypatch.setattr(pipeline_run, "bootstrap_study_privacy", record_bootstrap)
    monkeypatch.setattr(pipeline_run, "load_study_privacy_config", record_load_privacy)
    monkeypatch.setattr(pipeline_run, "resolve_rulebook", record_resolve)
    monkeypatch.setattr(pipeline_run, "load_intake_manifest", record_load_manifest)
    monkeypatch.setattr(pipeline_run, "organize", record_organize)
    monkeypatch.setattr(config, "ORGANIZED_DIR", tmp_path / "organized")
    monkeypatch.setattr(config, "RAW_DATA_DIR", tmp_path / "raw")

    result = pipeline_run._run_pipeline_locked("call-order-study", "us")

    assert calls == [
        "create_run_id",
        "load_intake_manifest",
        "bootstrap_privacy",
        "load_privacy",
        "resolve_rulebook",
        "organize",
    ]
    assert result.exit_code == 2
    assert result.run_id == "20260714T101112Z"


def test_intake_registry_lock_path_is_fixed_and_never_collides_with_study_names(lock_tmp: Path) -> None:
    registry_path = intake_registry_lock_path()
    assert registry_path == lock_tmp / ".__intake_registry__.pipeline.lock"
    # No valid study name can ever produce this exact path: any candidate
    # containing an underscore is rejected outright by lock_path_for
    # (_STUDY_NAME_PATTERN forbids "_"); any candidate that IS a valid
    # study name necessarily produces a different path.
    for candidate in ("intake_registry", "intake-registry", "__intake_registry__", "a", "Study.01"):
        try:
            candidate_path = lock_path_for(candidate)
        except ValueError:
            continue
        assert candidate_path != registry_path


def test_intake_registry_lock_contention_is_immediate(lock_tmp: Path) -> None:
    acquire_intake_registry_lock()
    try:
        with pytest.raises(PipelineBusyError):
            acquire_intake_registry_lock()
    finally:
        release_intake_registry_lock()


def test_intake_registry_lock_then_study_lock_nest_in_required_order(lock_tmp: Path) -> None:
    with intake_registry_lock():
        assert is_locally_held()
        acquire_pipeline_lock("registry-order-study")
        try:
            assert is_locally_held()
        finally:
            release_pipeline_lock("registry-order-study")
    assert not is_locally_held()


def test_intake_registry_lock_release_is_independent_of_study_lock_release(lock_tmp: Path) -> None:
    acquire_intake_registry_lock()
    acquire_pipeline_lock("independent-study")
    try:
        release_pipeline_lock("independent-study")
        # registry lock must still be held after releasing only the study lock
        with pytest.raises(PipelineBusyError):
            acquire_intake_registry_lock()
    finally:
        release_intake_registry_lock()
    # now fully released -- reacquire must succeed
    acquire_intake_registry_lock()
    release_intake_registry_lock()


def test_intake_registry_lock_cross_process_contention_is_immediate(lock_tmp: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_intake_registry_lock_until_released,
        args=(str(lock_tmp), ready, release),
    )
    process.start()
    try:
        assert ready.wait(timeout=10), "child did not acquire the intake registry lock"
        with pytest.raises(PipelineBusyError):
            acquire_intake_registry_lock()
    finally:
        release.set()
        process.join(timeout=10)
    assert process.exitcode == 0
