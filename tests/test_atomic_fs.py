"""Tests for the shared ``phi_engine.utils.atomic_fs`` no-replace rename
primitive: the exact ``renameat2(2)`` ``RENAME_NOREPLACE`` outcomes every
caller (intake's study-directory promotion today, the XLS isolation
boundary's bundle publication later) depends on for its fail-closed
create-or-fail publication guarantee.
"""

from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path

import pytest

from phi_engine.utils import atomic_fs


@pytest.fixture(autouse=True)
def _reset_resolved_symbol() -> None:
    """Every test starts from -- and leaves -- an unresolved cache so a
    platform/loader monkeypatch in one test can never leak a stale
    resolved (or unsupported) symbol into the next."""
    atomic_fs._reset_cache_for_tests()
    yield
    atomic_fs._reset_cache_for_tests()


def _dir_fd(path: Path) -> int:
    return os.open(path, os.O_DIRECTORY)


def test_renameat2_noreplace_moves_file_to_new_name(tmp_path: Path) -> None:
    (tmp_path / "old.txt").write_text("payload", encoding="utf-8")
    fd = _dir_fd(tmp_path)
    try:
        atomic_fs.renameat2_noreplace(fd, "old.txt", fd, "new.txt")
    finally:
        os.close(fd)

    assert not (tmp_path / "old.txt").exists()
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "payload"


def test_renameat2_noreplace_raises_file_exists_and_leaves_both_untouched(tmp_path: Path) -> None:
    (tmp_path / "old.txt").write_text("source", encoding="utf-8")
    (tmp_path / "new.txt").write_text("occupant", encoding="utf-8")
    fd = _dir_fd(tmp_path)
    try:
        with pytest.raises(FileExistsError):
            atomic_fs.renameat2_noreplace(fd, "old.txt", fd, "new.txt")
    finally:
        os.close(fd)

    # No replace, no partial mutation: the kernel's atomicity means
    # a failed no-replace rename leaves BOTH names exactly as they were.
    assert (tmp_path / "old.txt").read_text(encoding="utf-8") == "source"
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "occupant"


def test_renameat2_noreplace_raises_file_not_found_for_missing_source(tmp_path: Path) -> None:
    fd = _dir_fd(tmp_path)
    try:
        with pytest.raises(FileNotFoundError):
            atomic_fs.renameat2_noreplace(fd, "absent.txt", fd, "new.txt")
    finally:
        os.close(fd)


def test_renameat2_noreplace_raises_os_error_for_arbitrary_errno(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-ENOENT/non-EEXIST errno from the real ``renameat2(2)`` call
    path (via ``ctypes.get_errno()``) must surface as a generic
    ``OSError`` carrying that exact errno -- not be mistaken for the two
    specifically-typed outcomes."""
    resolved = atomic_fs._load_renameat2()
    assert resolved is not atomic_fs._RENAMEAT2_UNSUPPORTED

    def _stub_eacces(*_args: object, **_kwargs: object) -> int:
        ctypes.set_errno(errno.EACCES)
        return -1

    monkeypatch.setattr(atomic_fs, "_renameat2_fn", _stub_eacces)
    fd = _dir_fd(tmp_path)
    try:
        with pytest.raises(OSError) as excinfo:
            atomic_fs.renameat2_noreplace(fd, "old.txt", fd, "new.txt")
        assert not isinstance(excinfo.value, (FileExistsError, FileNotFoundError))
        assert excinfo.value.errno == errno.EACCES
    finally:
        os.close(fd)


def test_renameat2_noreplace_fails_closed_when_platform_is_not_linux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(atomic_fs.sys, "platform", "darwin")
    fd = _dir_fd(tmp_path)
    try:
        with pytest.raises(atomic_fs.AtomicRenameUnavailable):
            atomic_fs.renameat2_noreplace(fd, "old.txt", fd, "new.txt")
    finally:
        os.close(fd)


def test_renameat2_noreplace_fails_closed_when_libc_symbol_cannot_be_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _always_fails() -> None:
        raise OSError("simulated: no libc.so.6 in this runtime")

    monkeypatch.setattr(atomic_fs.ctypes, "CDLL", lambda *_a, **_k: (_ for _ in ()).throw(_always_fails()))
    fd = _dir_fd(tmp_path)
    try:
        with pytest.raises(atomic_fs.AtomicRenameUnavailable):
            atomic_fs.renameat2_noreplace(fd, "old.txt", fd, "new.txt")
    finally:
        os.close(fd)


def test_load_renameat2_caches_the_resolved_symbol_across_calls() -> None:
    first = atomic_fs._load_renameat2()
    second = atomic_fs._load_renameat2()
    assert first is second is not atomic_fs._RENAMEAT2_UNSUPPORTED


def test_reset_cache_for_tests_forces_re_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = atomic_fs._load_renameat2()
    assert resolved is not atomic_fs._RENAMEAT2_UNSUPPORTED

    monkeypatch.setattr(atomic_fs.sys, "platform", "darwin")
    # Without the reset, the cached Linux resolution would still win.
    assert atomic_fs._load_renameat2() is resolved

    atomic_fs._reset_cache_for_tests()
    assert atomic_fs._load_renameat2() is atomic_fs._RENAMEAT2_UNSUPPORTED
