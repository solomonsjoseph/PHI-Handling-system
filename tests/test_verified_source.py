from __future__ import annotations

import inspect
import os
import time
from pathlib import Path

import pytest

import phi_engine.pipeline.verified_source as verified_source_module
from phi_engine.pipeline.verified_source import FileIdentity, VerifiedSourceError, open_verified_source


def _identity(path: Path) -> FileIdentity:
    info = path.stat()
    return FileIdentity(device=info.st_dev, inode=info.st_ino, size=info.st_size, mtime_ns=info.st_mtime_ns)


@pytest.fixture(params=[False, True], ids=["openat2-fast-path", "descriptor-walk-fallback"])
def force_fallback(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    """Runs every test twice: once with whatever fast path is available on
    this platform, and once with the openat2 fast path disabled so the
    portable descriptor-walk fallback is the code path actually exercised.
    Both must independently produce the identical fail-closed outcome.
    """

    if request.param:
        monkeypatch.setattr(verified_source_module, "_try_openat2", lambda root_fd, parts: None)
    return request.param


def test_public_signature_matches_the_approved_contract() -> None:
    sig = inspect.signature(open_verified_source)
    params = list(sig.parameters.values())
    assert [p.name for p in params] == ["source_root", "relative_path", "required_source_component", "expected_identity"]
    assert params[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert params[1].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert params[2].kind is inspect.Parameter.KEYWORD_ONLY
    assert params[2].default is None
    assert params[3].kind is inspect.Parameter.KEYWORD_ONLY
    assert params[3].default is None
    assert set(verified_source_module.__all__) == {"FileIdentity", "VerifiedSourceError", "open_verified_source"}


def test_openat2_fast_path_actually_used_by_default_on_linux(tmp_path: Path) -> None:
    import platform

    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "aarch64"):
        pytest.skip("openat2 fast path only implemented for Linux x86_64/aarch64")
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / "labs.csv").write_text("x", encoding="utf-8")

    # Capability probe: seccomp policy or an older kernel lacking
    # RESOLVE_BENEATH/RESOLVE_NO_SYMLINKS support can legitimately make
    # openat2 unavailable even on a supported platform/arch -- probe it
    # directly on a real target first so this test skips cleanly on those
    # runners instead of failing; the portable descriptor-walk fallback
    # (exercised independently via the `force_fallback` fixture across the
    # rest of this file) is what the fail-closed guarantee actually rests
    # on, not this opportunistic fast path.
    probe_root_fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        probe_result = verified_source_module._try_openat2(probe_root_fd, ("datasets",))
    finally:
        os.close(probe_root_fd)
    if probe_result is None:
        pytest.skip("openat2 unavailable in this runtime (seccomp/kernel policy)")
    os.close(probe_result)

    calls: list[object] = []
    real = verified_source_module._try_openat2

    def spy(root_fd: int, parts: tuple) -> int | None:
        result = real(root_fd, parts)
        calls.append(result)
        return result

    original = verified_source_module._try_openat2
    verified_source_module._try_openat2 = spy
    try:
        with open_verified_source(tmp_path, "datasets/labs.csv"):
            pass
    finally:
        verified_source_module._try_openat2 = original
    assert calls and calls[0] is not None, "openat2 fast path did not actually succeed on this platform"


def test_open_verified_source_reads_regular_file(tmp_path: Path, force_fallback: bool) -> None:
    (tmp_path / "datasets").mkdir()
    target = tmp_path / "datasets" / "labs.csv"
    target.write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")

    with open_verified_source(tmp_path, "datasets/labs.csv") as fd:
        assert os.read(fd, 4096) == b"SUBJID,AGE\n1,40\n"


def test_open_verified_source_deterministic_across_calls(tmp_path: Path, force_fallback: bool) -> None:
    (tmp_path / "datasets" / "nested").mkdir(parents=True)
    target = tmp_path / "datasets" / "nested" / "deep.csv"
    target.write_text("A,B\n1,2\n", encoding="utf-8")

    reads = []
    for _ in range(3):
        with open_verified_source(tmp_path, "datasets/nested/deep.csv") as fd:
            reads.append(os.read(fd, 4096))
    assert reads == [b"A,B\n1,2\n"] * 3


@pytest.mark.parametrize(
    "relative_path",
    [
        "/etc/passwd",
        "..",
        "../outside.csv",
        "datasets/../../outside.csv",
        "",
        "datasets/./labs.csv",
        "datasets//labs.csv",
        "datasets/labs.csv/",
        "datasets/labs.csv\x00/etc/passwd",
        "datasets/x.csv\x00junk",
    ],
)
def test_open_verified_source_rejects_unsafe_relative_paths(
    tmp_path: Path, relative_path: str, force_fallback: bool
) -> None:
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / "labs.csv").write_text("x", encoding="utf-8")

    with pytest.raises(VerifiedSourceError) as excinfo:
        with open_verified_source(tmp_path, relative_path):
            pass
    assert excinfo.value.reason == "source-target-outside-root"


def test_open_verified_source_nul_path_never_opens_a_different_truncated_file(tmp_path: Path, force_fallback: bool) -> None:
    (tmp_path / "datasets").mkdir()
    secret = tmp_path / "datasets" / "x.csv"
    secret.write_text("secret", encoding="utf-8")

    with pytest.raises(VerifiedSourceError) as excinfo:
        with open_verified_source(tmp_path, "datasets/x.csv\x00junk") as fd:
            os.read(fd, 100)  # must never reach here
    assert excinfo.value.reason == "source-target-outside-root"


def test_open_verified_source_rejects_final_symlink_even_in_root(tmp_path: Path, force_fallback: bool) -> None:
    (tmp_path / "datasets").mkdir()
    real = tmp_path / "datasets" / "real.csv"
    real.write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    link = tmp_path / "datasets" / "link.csv"
    link.symlink_to(real)

    with pytest.raises(VerifiedSourceError) as excinfo:
        with open_verified_source(tmp_path, "datasets/link.csv"):
            pass
    assert excinfo.value.reason == "source-symlink-not-allowed"


def test_open_verified_source_rejects_intermediate_directory_symlink_even_in_root(
    tmp_path: Path, force_fallback: bool
) -> None:
    real_dir = tmp_path / "datasets"
    real_dir.mkdir()
    (real_dir / "labs.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    link_dir = tmp_path / "linked_datasets"
    link_dir.symlink_to(real_dir)

    with pytest.raises(VerifiedSourceError) as excinfo:
        with open_verified_source(tmp_path, "linked_datasets/labs.csv"):
            pass
    assert excinfo.value.reason == "source-symlink-not-allowed"


def test_open_verified_source_distinguishes_non_directory_intermediate_from_symlink(
    tmp_path: Path, force_fallback: bool
) -> None:
    # A regular file blocking an intermediate path segment is not a symlink
    # attack; it must be reported as source-unreadable, not
    # source-symlink-not-allowed.
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("just a file", encoding="utf-8")

    with pytest.raises(VerifiedSourceError) as excinfo:
        with open_verified_source(tmp_path, "not_a_dir/child.csv"):
            pass
    assert excinfo.value.reason == "source-unreadable"


def test_open_verified_source_rejects_symlink_escaping_root(tmp_path: Path, force_fallback: bool) -> None:
    source = tmp_path / "src"
    (source / "datasets").mkdir(parents=True)
    outside = tmp_path / "outside.csv"
    outside.write_text("SUBJID,AGE\n2,41\n", encoding="utf-8")
    link = source / "datasets" / "labs.csv"
    link.symlink_to(outside)

    with pytest.raises(VerifiedSourceError) as excinfo:
        with open_verified_source(source, "datasets/labs.csv"):
            pass
    assert excinfo.value.reason == "source-symlink-not-allowed"


def test_open_verified_source_swapped_final_component_after_listing_is_rejected(
    tmp_path: Path, force_fallback: bool
) -> None:
    # Simulates a TOCTOU intermediate-path swap: caller learned about
    # "datasets/labs.csv" from a directory listing, but by the time it is
    # opened the path has been replaced with a symlink. The descriptor open
    # must still fail closed instead of silently following it.
    (tmp_path / "datasets").mkdir()
    real = tmp_path / "datasets" / "labs.csv"
    real.write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    decoy = tmp_path / "decoy.csv"
    decoy.write_text("SUBJID,AGE\n9,99\n", encoding="utf-8")

    real.unlink()
    real.symlink_to(decoy)

    with pytest.raises(VerifiedSourceError) as excinfo:
        with open_verified_source(tmp_path, "datasets/labs.csv"):
            pass
    assert excinfo.value.reason == "source-symlink-not-allowed"


def test_open_verified_source_rejects_missing_file(tmp_path: Path, force_fallback: bool) -> None:
    (tmp_path / "datasets").mkdir()
    with pytest.raises(VerifiedSourceError) as excinfo:
        with open_verified_source(tmp_path, "datasets/missing.csv"):
            pass
    assert excinfo.value.reason == "source-unreadable"


def test_open_verified_source_rejects_non_regular_file(tmp_path: Path, force_fallback: bool) -> None:
    (tmp_path / "datasets").mkdir()
    with pytest.raises(VerifiedSourceError) as excinfo:
        with open_verified_source(tmp_path, "datasets"):
            pass
    assert excinfo.value.reason == "source-unreadable"


def test_open_verified_source_never_hangs_on_a_fifo(tmp_path: Path, force_fallback: bool) -> None:
    (tmp_path / "datasets").mkdir()
    fifo_path = tmp_path / "datasets" / "blocked.csv"
    os.mkfifo(fifo_path)

    start = time.monotonic()
    with pytest.raises(VerifiedSourceError) as excinfo:
        with open_verified_source(tmp_path, "datasets/blocked.csv"):
            pass
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"open_verified_source blocked for {elapsed}s on a FIFO"
    assert excinfo.value.reason == "source-unreadable"


def test_open_verified_source_required_source_component_mismatch(tmp_path: Path, force_fallback: bool) -> None:
    (tmp_path / "forms").mkdir()
    (tmp_path / "forms" / "consent.pdf").write_bytes(b"%PDF-1.4")

    with pytest.raises(VerifiedSourceError) as excinfo:
        with open_verified_source(tmp_path, "forms/consent.pdf", required_source_component="datasets"):
            pass
    assert excinfo.value.reason == "source-target-outside-root"


def test_open_verified_source_rejects_symlink_ancestor_masked_by_dotdot_collapse(
    tmp_path: Path, force_fallback: bool
) -> None:
    # Regression for a demonstrated bypass: a relative_path/source_root
    # shaped so a lexical ".." component would collapse OVER a symlinked
    # ancestor segment (e.g. "a/link/../pkg") must never let abspath()'s
    # pure string collapsing skip walking "link" -- that would silently
    # inspect a completely different directory than the one the supplied
    # path actually resolves to through the filesystem.
    other = tmp_path / "other"
    (other / "pkg" / "datasets").mkdir(parents=True)
    (other / "pkg" / "datasets" / "labs.csv").write_text("OTHER_TREE_SENTINEL", encoding="utf-8")

    a_dir = tmp_path / "a"
    a_dir.mkdir()
    (a_dir / "link").symlink_to(other)
    (a_dir / "pkg" / "datasets").mkdir(parents=True)
    (a_dir / "pkg" / "datasets" / "labs.csv").write_text("A_TREE_SENTINEL", encoding="utf-8")

    ancestry_source = a_dir / "link" / ".." / "pkg"
    with pytest.raises(VerifiedSourceError) as excinfo:
        with open_verified_source(ancestry_source, "datasets/labs.csv"):
            pass
    assert excinfo.value.reason == "source-target-outside-root"


def test_open_verified_source_required_source_component_match_succeeds(tmp_path: Path, force_fallback: bool) -> None:
    (tmp_path / "forms").mkdir()
    (tmp_path / "forms" / "consent.pdf").write_bytes(b"%PDF-1.4")

    with open_verified_source(tmp_path, "forms/consent.pdf", required_source_component="forms") as fd:
        assert os.read(fd, 16) == b"%PDF-1.4"


def test_open_verified_source_expected_identity_matches(tmp_path: Path, force_fallback: bool) -> None:
    (tmp_path / "datasets").mkdir()
    target = tmp_path / "datasets" / "labs.csv"
    target.write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    expected = _identity(target)

    with open_verified_source(tmp_path, "datasets/labs.csv", expected_identity=expected) as fd:
        assert os.read(fd, 4096) == b"SUBJID,AGE\n1,40\n"


def test_open_verified_source_expected_identity_mismatch_rejected(tmp_path: Path, force_fallback: bool) -> None:
    (tmp_path / "datasets").mkdir()
    target = tmp_path / "datasets" / "labs.csv"
    target.write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    bogus = FileIdentity(device=0, inode=0, size=0, mtime_ns=0)

    with pytest.raises(VerifiedSourceError) as excinfo:
        with open_verified_source(tmp_path, "datasets/labs.csv", expected_identity=bogus):
            pass
    assert excinfo.value.reason == "source-unreadable"


@pytest.mark.parametrize("field", ["device", "inode", "size", "mtime_ns"])
def test_open_verified_source_expected_identity_mismatch_per_field(tmp_path: Path, field: str, force_fallback: bool) -> None:
    (tmp_path / "datasets").mkdir()
    target = tmp_path / "datasets" / "labs.csv"
    target.write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    real = _identity(target)
    kwargs = {"device": real.device, "inode": real.inode, "size": real.size, "mtime_ns": real.mtime_ns}
    kwargs[field] = kwargs[field] + 1
    tampered = FileIdentity(**kwargs)

    with pytest.raises(VerifiedSourceError) as excinfo:
        with open_verified_source(tmp_path, "datasets/labs.csv", expected_identity=tampered):
            pass
    assert excinfo.value.reason == "source-unreadable"


def test_open_verified_source_expected_identity_mutation_during_use_rejected(
    tmp_path: Path, force_fallback: bool
) -> None:
    (tmp_path / "datasets").mkdir()
    target = tmp_path / "datasets" / "labs.csv"
    target.write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    expected = _identity(target)

    with pytest.raises(VerifiedSourceError) as excinfo:
        with open_verified_source(tmp_path, "datasets/labs.csv", expected_identity=expected):
            # Mutate the underlying file out-of-band (a different fd/path),
            # simulating a concurrent writer while this descriptor is held.
            target.write_text("SUBJID,AGE\n1,40\nEXTRA,ROW\n", encoding="utf-8")
    assert excinfo.value.reason == "source-unreadable"


def test_open_verified_source_mutation_during_use_rejected_even_without_expected_identity(
    tmp_path: Path, force_fallback: bool
) -> None:
    # The pre/post identity check is unconditional -- it does not require
    # the caller to already know an expected identity up front.
    (tmp_path / "datasets").mkdir()
    target = tmp_path / "datasets" / "labs.csv"
    target.write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")

    with pytest.raises(VerifiedSourceError) as excinfo:
        with open_verified_source(tmp_path, "datasets/labs.csv"):
            target.write_text("SUBJID,AGE\n1,40\nEXTRA,ROW\n", encoding="utf-8")
    assert excinfo.value.reason == "source-unreadable"


def test_open_verified_source_identity_mismatch_fails_closed_even_when_consumer_also_raises(
    tmp_path: Path, force_fallback: bool
) -> None:
    class _ConsumerError(Exception):
        pass

    (tmp_path / "datasets").mkdir()
    target = tmp_path / "datasets" / "labs.csv"
    target.write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")

    with pytest.raises(VerifiedSourceError) as excinfo:
        with open_verified_source(tmp_path, "datasets/labs.csv"):
            target.write_text("MUTATED", encoding="utf-8")
            raise _ConsumerError("parser exploded")
    assert excinfo.value.reason == "source-unreadable"
    assert isinstance(excinfo.value.__context__, _ConsumerError)


def test_open_verified_source_rendered_traceback_never_leaks_consumer_message_or_context(
    tmp_path: Path, force_fallback: bool
) -> None:
    # Regression: the fixed VerifiedSourceError reason is safe on its own,
    # but the DEFAULT rendered traceback must never surface the consumer
    # exception this identity-mismatch replaces -- neither its own
    # evaluated message/args (which could carry PHI-adjacent runtime
    # content read from the file) nor a "During handling of the above
    # exception..." secondary block. Note: on this interpreter, a
    # context-manager __exit__ that replaces a propagating exception
    # unavoidably has the CALLER's raw source line for its own raise
    # statement appear in the frame list (a documented CPython <3.11
    # with-statement characteristic, not something `from None` or any
    # exception-object-level fix can suppress) -- but that frame rendering
    # is always the literal, unevaluated source text, never an interpolated
    # runtime value, so it carries no PHI. This test asserts what is
    # actually achievable and actually security-relevant: the consumer
    # exception's own evaluated message is a RUNTIME value (built via an
    # f-string, matching how real code embeds file-derived content) and
    # must never appear anywhere in the rendering, and __suppress_context__
    # must be True so no secondary "ConsumerError: ..." block is ever
    # printed by default handlers.
    import traceback

    class _ConsumerError(Exception):
        pass

    (tmp_path / "datasets").mkdir()
    target = tmp_path / "datasets" / "labs.csv"
    target.write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")

    runtime_secret = "RAW_PHI_SENTINEL_RUNTIME_VALUE_9f8e7d"

    with pytest.raises(VerifiedSourceError) as excinfo:
        with open_verified_source(tmp_path, "datasets/labs.csv"):
            target.write_text("MUTATED", encoding="utf-8")
            raise _ConsumerError(f"parser failed on row: {runtime_secret}")
    exc = excinfo.value
    assert exc.reason == "source-unreadable"
    assert exc.__suppress_context__ is True
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    assert runtime_secret not in rendered
    assert "_ConsumerError:" not in rendered


def test_open_verified_source_consumer_exception_propagates_when_no_mutation_occurred(
    tmp_path: Path, force_fallback: bool
) -> None:
    class _ConsumerError(Exception):
        pass

    (tmp_path / "datasets").mkdir()
    target = tmp_path / "datasets" / "labs.csv"
    target.write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")

    with pytest.raises(_ConsumerError):
        with open_verified_source(tmp_path, "datasets/labs.csv"):
            raise _ConsumerError("unrelated parser failure")


def test_open_verified_source_reason_never_leaks_path_or_exception_text(tmp_path: Path, force_fallback: bool) -> None:
    with pytest.raises(VerifiedSourceError) as excinfo:
        with open_verified_source(tmp_path, "../secret/patient.csv"):
            pass
    reason = excinfo.value.reason
    assert "secret" not in reason
    assert "patient" not in reason
    assert str(tmp_path) not in reason
    assert reason == "source-target-outside-root"


def test_open_verified_source_descriptor_closed_after_use(tmp_path: Path, force_fallback: bool) -> None:
    (tmp_path / "datasets").mkdir()
    target = tmp_path / "datasets" / "labs.csv"
    target.write_text("x", encoding="utf-8")

    with open_verified_source(tmp_path, "datasets/labs.csv") as fd:
        held_fd = fd
    with pytest.raises(OSError):
        os.fstat(held_fd)


def test_open_verified_source_descriptor_closed_on_error(tmp_path: Path, force_fallback: bool) -> None:
    (tmp_path / "datasets").mkdir()
    target = tmp_path / "datasets" / "labs.csv"
    target.write_text("x", encoding="utf-8")
    expected = FileIdentity(device=0, inode=0, size=0, mtime_ns=0)

    if not os.path.isdir("/proc/self/fd"):
        pytest.skip("no /proc/self/fd on this platform")
    fds_before = set(os.listdir("/proc/self/fd"))
    with pytest.raises(VerifiedSourceError):
        with open_verified_source(tmp_path, "datasets/labs.csv", expected_identity=expected):
            pass
    fds_after = set(os.listdir("/proc/self/fd"))
    assert fds_after - fds_before == set()


def test_open_verified_source_fails_closed_when_nofollow_capability_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / "labs.csv").write_text("x", encoding="utf-8")
    monkeypatch.setattr(verified_source_module, "_O_NOFOLLOW", 0)

    with pytest.raises(VerifiedSourceError) as excinfo:
        with open_verified_source(tmp_path, "datasets/labs.csv"):
            pass
    assert excinfo.value.reason == "source-unreadable"


def test_open_verified_source_fails_closed_when_directory_capability_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / "labs.csv").write_text("x", encoding="utf-8")
    monkeypatch.setattr(verified_source_module, "_O_DIRECTORY", 0)

    with pytest.raises(VerifiedSourceError) as excinfo:
        with open_verified_source(tmp_path, "datasets/labs.csv"):
            pass
    assert excinfo.value.reason == "source-unreadable"


def test_open_verified_source_fails_closed_when_nonblock_capability_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / "labs.csv").write_text("x", encoding="utf-8")
    monkeypatch.setattr(verified_source_module, "_O_NONBLOCK", 0)

    with pytest.raises(VerifiedSourceError) as excinfo:
        with open_verified_source(tmp_path, "datasets/labs.csv"):
            pass
    assert excinfo.value.reason == "source-unreadable"


def test_open_verified_source_fails_closed_when_dir_fd_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / "labs.csv").write_text("x", encoding="utf-8")
    monkeypatch.setattr(os, "supports_dir_fd", frozenset())

    with pytest.raises(VerifiedSourceError) as excinfo:
        with open_verified_source(tmp_path, "datasets/labs.csv"):
            pass
    assert excinfo.value.reason == "source-unreadable"


def test_open_verified_source_normalizes_injected_initial_fstat_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / "labs.csv").write_text("x", encoding="utf-8")

    real_fstat = os.fstat

    def broken_fstat(fd, *args, **kwargs):
        raise OSError(5, "SENTINEL_RAW_EIO")

    monkeypatch.setattr(verified_source_module.os, "fstat", broken_fstat)
    try:
        with pytest.raises(VerifiedSourceError) as excinfo:
            with open_verified_source(tmp_path, "datasets/labs.csv"):
                pass
    finally:
        monkeypatch.setattr(verified_source_module.os, "fstat", real_fstat)
    assert excinfo.value.reason == "source-unreadable"
    assert "SENTINEL_RAW_EIO" not in str(excinfo.value)
