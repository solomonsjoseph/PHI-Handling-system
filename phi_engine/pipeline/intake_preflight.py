"""Deterministic, non-LLM intake preflight for the standalone PHI pipeline.

Inspects an organized study source tree (``datasets/``, ``forms/``,
``data_dictionary/``/``mappings/``) and classifies every discovered file into
a supported :data:`Component`, a blocking review item, or a hard error --
using only path/format inspection and bounded ``.xlsx`` sheet-count metadata.
This module never imports or calls an LLM/model client of any kind, and
never inspects dataset cell values, document text, or row content.

The source root is pinned once (via ``verified_source._open_pinned_root``)
and held for the entire inspection. Traversal uses an explicit,
depth-bounded stack of held directory descriptors -- never Python recursion,
never a discover-then-reopen-by-path split: each entry is verified and, for
files, immediately opened and processed while its parent directory
descriptor (obtained from that same directory's own discovery step) is
still held, and each directory's identity is rechecked against its
discovery-time stat once every child has been processed. No filesystem
object anywhere in the tree is ever looked up by path a second time.
"""

from __future__ import annotations

import hashlib
import lzma
import os
import stat
import struct
import sys
import zlib
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable, Literal

import defusedxml.ElementTree as defused_ET
import defusedxml.common as defused_common
import xml.etree.ElementTree as ET

from phi_engine.pipeline import support_files
from phi_engine.pipeline.verified_source import (
    FileIdentity,
    VerifiedSourceError,
    _open_dir_from_parent_fd,
    _open_from_parent_fd,
    _open_pinned_root,
    _recheck_dir_identity,
)
from phi_engine.utils._extraction_io.file_discovery import DEFAULT_JUNK_FILENAMES

__all__ = [
    "Component",
    "IntakeCandidate",
    "IntakePreflight",
    "inspect_intake_source",
    "count_xlsx_sheets",
]

Component = Literal["datasets", "forms", "data_dictionary", "mappings", "_unclassified"]

_KNOWN_COMPONENTS: tuple[str, ...] = ("datasets", "forms", "data_dictionary", "mappings")
_COMPONENT_SUFFIXES: dict[str, frozenset[str]] = {
    "datasets": frozenset({".csv", ".xls", ".xlsx"}),
    "forms": frozenset({".pdf"}),
    "data_dictionary": frozenset({".csv", ".xlsx"}),
    "mappings": frozenset({".csv", ".xlsx"}),
}
_HASH_CHUNK_SIZE = 1 << 20
_WORKBOOK_MEMBER = "xl/workbook.xml"
_SHEET_QNAME = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheet"
_ROOT_PATH = ""  # fixed string path for whole-source-root review/error records
_MAX_TRAVERSAL_DEPTH = 64  # generous bound; anything deeper fails closed, not a Python RecursionError

# Hostile-input exceptions normalized to the fixed workbook-invalid code.
# zipfile itself remains the authoritative ZIP/XML parser throughout (per
# the approved plan) -- these are the concrete failure classes it and its
# decompressors are documented to raise on malformed/hostile input, never
# reinterpreted ZIP structure of our own.
_HOSTILE_ZIP_EXCEPTIONS = (
    zipfile.BadZipFile,
    OSError,
    EOFError,
    RuntimeError,
    NotImplementedError,
    ValueError,
    struct.error,
    zlib.error,
    lzma.LZMAError,
)

# Bucket: TOCTOU/escape/unreadable source access failures that mean "we could
# not trust this read at all" are "errors". Structural/format issues a human
# fixes by correcting the package are "review" (blocking, retryable).
_ERROR_REASONS = frozenset({"source-unreadable", "source-target-outside-root"})


@dataclass(frozen=True)
class IntakeCandidate:
    relative_path: str
    source_component: str
    component: Component
    identity: FileIdentity
    sha256: str
    sheet_count: int | None


@dataclass(frozen=True)
class IntakePreflight:
    candidates: tuple[IntakeCandidate, ...]
    review_items: tuple[dict[str, Any], ...]
    errors: tuple[dict[str, Any], ...]


class _XlsxWorkbookError(Exception):
    """Private fixed-reason control-flow signal internal to this module."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _identity(info: os.stat_result) -> FileIdentity:
    return FileIdentity(device=info.st_dev, inode=info.st_ino, size=info.st_size, mtime_ns=info.st_mtime_ns)


class _Skip:
    """Engine action: ignore this entry entirely (no descent, not a file)."""

    __slots__ = ()


class _AsFile:
    """Engine action: hand this entry to ``on_file`` with ``extra`` attached."""

    __slots__ = ("extra",)

    def __init__(self, extra: Any = None) -> None:
        self.extra = extra


class _Descend:
    """Engine action: push a new frame for this already-opened, already-
    scanned subdirectory. ``entries`` must already be the sorted scandir
    result for ``handle`` -- the engine never scans a child itself, so a
    caller's own open/scan failure handling (which fixed reason a failed
    directory open produces, or -- for legacy -- silently skipping it) is
    entirely the caller's policy, not the engine's.
    """

    __slots__ = ("handle", "entries", "extra")

    def __init__(self, handle: Any, entries: list["os.DirEntry[str]"], extra: Any = None) -> None:
        self.handle = handle
        self.entries = entries
        self.extra = extra


_SKIP = _Skip()


class _WalkFrame:
    """One held frame on the explicit traversal stack: a directory handle
    (an int fd for the descriptor-relative caller, a ``Path`` for the
    pathname-based legacy caller -- ``os.scandir`` accepts either), its
    already-sorted discovery listing, its position in that listing, and
    whatever per-frame state (``extra``) the caller attached when it
    descended into this directory.
    """

    __slots__ = ("handle", "prefix", "entries", "index", "extra", "owns_handle")

    def __init__(
        self,
        handle: Any,
        prefix: tuple[str, ...],
        entries: list["os.DirEntry[str]"],
        extra: Any,
        owns_handle: bool,
    ) -> None:
        self.handle = handle
        self.prefix = prefix
        self.entries = entries
        self.index = 0
        self.extra = extra
        self.owns_handle = owns_handle


def _walk_tree(
    root_handle: Any,
    root_extra: Any,
    *,
    classify: Callable[[Any, "os.DirEntry[str]", tuple[str, ...], Any, int], Any],
    on_file: Callable[[Any, "os.DirEntry[str]", tuple[str, ...], Any, Any], None],
    close_handle: Callable[[Any], None],
    on_dir_exit: Callable[[Any, tuple[str, ...], Any], bool] | None = None,
    max_depth: int = _MAX_TRAVERSAL_DEPTH,
) -> bool:
    """The one shared, explicit-stack (never Python recursion), depth-bounded
    directory-tree traversal engine in this module. Both active source-tree
    walks -- preflight's descriptor-relative, process-while-parent-fd-held
    scan and legacy ``intake.py``'s pathname-based file discovery -- are
    built on this single engine, so stack/depth/ordering mechanics can no
    longer drift between two independent implementations. The two callers
    intentionally still apply their own, different per-entry policy
    (preflight rejects every symlink; legacy intentionally still includes
    in-root symlinked files, unchanged ahead of the manifest-v3 symlink
    cutover) entirely through ``classify``/``on_file``/``close_handle`` --
    the engine itself has no symlink or hidden/junk opinion of its own.

    ``classify(parent_handle, entry, prefix, parent_extra, depth)`` inspects
    one discovered entry and returns ``_SKIP``, an ``_AsFile(extra)`` to
    hand to ``on_file``, or an ``_Descend(child_handle, child_entries,
    extra)`` -- having already opened and scanned the child itself, so any
    open/scan failure is handled entirely by the caller's own ``classify``
    (which fixed reason to record, or, for legacy, silently skipping it).
    ``depth`` is the current stack depth (frames already pushed, not
    counting a directory this call might itself cause to be pushed) so a
    caller can apply its own too-deep policy before ever opening a child.

    ``close_handle`` releases a handle this engine is popping that it did
    not receive as ``root_handle`` (which the caller always owns and this
    engine never closes). ``on_dir_exit``, if given, runs once every child
    of a directory has been visited (frame about to be popped) and may
    return ``False`` to mark the whole walk untrustworthy -- propagated as
    this function's own return value; the walk still completes.
    """

    try:
        root_entries = sorted(os.scandir(root_handle), key=lambda e: e.name)
    except OSError:
        return False

    stack: list[_WalkFrame] = [_WalkFrame(root_handle, (), root_entries, root_extra, False)]
    tree_ok = True
    while stack:
        frame = stack[-1]
        if frame.index >= len(frame.entries):
            if on_dir_exit is not None and not on_dir_exit(frame.handle, frame.prefix, frame.extra):
                tree_ok = False
            if frame.owns_handle:
                close_handle(frame.handle)
            stack.pop()
            continue

        entry = frame.entries[frame.index]
        frame.index += 1
        prefix = frame.prefix + (entry.name,)

        action = classify(frame.handle, entry, prefix, frame.extra, len(stack))
        if action is _SKIP:
            continue
        if isinstance(action, _AsFile):
            on_file(frame.handle, entry, prefix, frame.extra, action.extra)
            continue
        # _Descend: classify() already opened and scanned this child.
        if len(stack) >= max_depth:
            close_handle(action.handle)
            continue
        stack.append(_WalkFrame(action.handle, prefix, action.entries, action.extra, True))

    return tree_ok


def _iter_source_files(root: Path) -> list[Path]:
    """Recursively list files under ``root``, filtering hidden/junk entries.

    Deterministic: sorted by POSIX path relative to ``root``. The single
    traversal convention ``intake.py``'s legacy, unchanged v2 flow depends
    on -- a private helper moved here rather than duplicated, and now built
    on the same :func:`_walk_tree` engine :func:`inspect_intake_source`
    uses (see its own module docstring), just with pathname-based handles
    and legacy's own, intentionally different symlink policy: a symlinked
    file is still included (matching ``os.walk(followlinks=False)``, which
    lists but never descends into a symlinked directory while still
    reporting symlinked files -- this legacy flow is deliberately
    unchanged pending the manifest-v3 symlink cutover in a later step).
    """

    root = Path(root)
    found: list[Path] = []

    def classify_legacy(parent_path: Path, entry: "os.DirEntry[str]", prefix: tuple[str, ...], _extra: None, depth: int) -> Any:
        if entry.name.startswith(".") or entry.name in DEFAULT_JUNK_FILENAMES:
            return _SKIP
        try:
            is_dir = entry.is_dir(follow_symlinks=True)
        except OSError:
            return _SKIP
        if not is_dir:
            return _AsFile(None)
        try:
            is_link = entry.is_symlink()
        except OSError:
            return _SKIP
        if is_link:
            # Matches os.walk(followlinks=False): a symlinked directory is
            # never descended into (its contents never appear), but it is
            # also not itself reported as a file.
            return _SKIP
        child_path = parent_path / entry.name
        try:
            child_entries = sorted(os.scandir(child_path), key=lambda e: e.name)
        except OSError:
            return _SKIP
        return _Descend(child_path, child_entries, None)

    def on_file_legacy(parent_path: Path, entry: "os.DirEntry[str]", _prefix: tuple[str, ...], _parent_extra: None, _extra: None) -> None:
        found.append(parent_path / entry.name)

    _walk_tree(
        root,
        None,
        classify=classify_legacy,
        on_file=on_file_legacy,
        close_handle=lambda _handle: None,
        max_depth=sys.maxsize,
    )
    found.sort(key=lambda p: p.relative_to(root).as_posix())
    return found


def _stream_size(fileobj: BinaryIO) -> int:
    try:
        current = fileobj.tell()
        fileobj.seek(0, os.SEEK_END)
        size = fileobj.tell()
        fileobj.seek(current, os.SEEK_SET)
        return size
    except (OSError, AttributeError, ValueError):
        raise _XlsxWorkbookError("xlsx-workbook-invalid") from None


class _BoundedReader:
    """Enforces an actual-bytes-read cap, independent of any declared/attacker-
    controlled ZIP central-directory size metadata. Every delegated read is
    itself clamped to at most the remaining budget (including negative
    "read everything" requests), so no single call can pull an unbounded
    amount from the underlying stream/decompressor before the cap is
    checked. Also usable to wrap the raw archive stream itself (with
    ``seek``/``tell`` passthrough) so zipfile's own central-directory parse
    cannot allocate an unbounded amount before we ever see it; ``rearm``
    lets the SAME wrapper instance -- and therefore the same ``ZipFile``
    object built on it -- switch to a different budget for a later phase
    (e.g. once central-directory parsing has finished) without ever
    reconstructing or reparsing the archive.
    """

    def __init__(self, stream: BinaryIO, limit: int) -> None:
        self._stream = stream
        self._limit = limit
        self._read = 0

    def rearm(self, limit: int) -> None:
        self._limit = limit
        self._read = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self._limit - self._read + 1  # +1 so overage is observable
        if remaining <= 0:
            raise _XlsxWorkbookError("xlsx-workbook-invalid")
        request = remaining if (size is None or size < 0 or size > remaining) else size
        chunk = self._stream.read(request)
        self._read += len(chunk)
        if self._read > self._limit:
            raise _XlsxWorkbookError("xlsx-workbook-invalid")
        return chunk

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._stream.seek(offset, whence)

    def tell(self) -> int:
        return self._stream.tell()

    def seekable(self) -> bool:
        seek_check = getattr(self._stream, "seekable", None)
        return True if seek_check is None else bool(seek_check())


def _count_sheet_elements(stream: BinaryIO, max_sheets: int) -> int:
    count = 0
    try:
        for _event, elem in defused_ET.iterparse(stream, events=("end",)):
            if elem.tag == _SHEET_QNAME:
                count += 1
            elem.clear()
            if count > max_sheets:
                break
    except (ET.ParseError, defused_common.DefusedXmlException):
        raise _XlsxWorkbookError("xlsx-workbook-invalid") from None
    return count


def count_xlsx_sheets(fileobj: BinaryIO) -> int:
    """Count SpreadsheetML ``<sheet>`` entries in ``xl/workbook.xml`` via a
    bounded streaming parse.

    zipfile remains the sole, authoritative ZIP parser throughout -- this
    never reimplements EOCD/central-directory/ZIP64 parsing. Exactly one
    ``zipfile.ZipFile`` instance is constructed, on a ``_BoundedReader``
    wrapping the archive stream that initially caps actual bytes read at
    ``max_zip_directory_bytes`` (bounding the allocation ``ZipFile.__init__``
    performs while eagerly parsing the complete central directory). Once
    construction succeeds and the declared member count/duplicate-workbook
    check/per-member/aggregate/ratio bounds have all been validated from
    that single parse, the SAME wrapper is rearmed to a much larger budget
    before the workbook member is opened and streamed from that SAME
    ``ZipFile`` instance -- never a second construction/reparse -- so a
    valid compressed member larger than the small directory-parsing budget
    is governed only by the already-approved source/member/expanded/ratio
    limits, not by the directory cap. ``max_zip_members`` is still enforced
    immediately after construction as an explicit defense-in-depth count
    check. Raises :class:`_XlsxWorkbookError` with a fixed reason on any
    malformed, encrypted, oversized, or pathological input; never raw
    exception text.
    """

    limits = support_files.DEFAULT_LIMITS
    source_size = _stream_size(fileobj)
    if source_size > limits["max_source_bytes"]:
        raise _XlsxWorkbookError("xlsx-workbook-invalid")

    try:
        fileobj.seek(0, os.SEEK_SET)
        guarded = _BoundedReader(fileobj, limits["max_zip_directory_bytes"])
        with zipfile.ZipFile(guarded) as zf:
            infolist = zf.infolist()
            if len(infolist) > limits["max_zip_members"]:
                raise _XlsxWorkbookError("xlsx-workbook-invalid")

            expanded_total = 0
            compressed_total = 0
            workbook_member_count = 0
            for info in infolist:
                if int(info.file_size) > limits["max_zip_member_bytes"]:
                    raise _XlsxWorkbookError("xlsx-workbook-invalid")
                expanded_total += int(info.file_size)
                compressed_total += int(info.compress_size)
                if info.filename == _WORKBOOK_MEMBER:
                    workbook_member_count += 1
            if workbook_member_count != 1:
                raise _XlsxWorkbookError("xlsx-workbook-invalid")
            if expanded_total > limits["max_expanded_workbook_bytes"]:
                raise _XlsxWorkbookError("xlsx-workbook-invalid")
            compressed_total = compressed_total or 1
            if (expanded_total / compressed_total) > limits["max_decompression_ratio"]:
                raise _XlsxWorkbookError("xlsx-workbook-invalid")

            # Central-directory parsing is done; the construction-only
            # allocation risk is behind us. Rearm the SAME wrapper (still
            # backing this SAME ZipFile instance) to a generous budget so a
            # legitimately large compressed member read is governed by the
            # limits already validated above, not by the tiny directory cap.
            guarded.rearm(limits["max_source_bytes"])

            member_cap = limits["max_zip_member_bytes"]
            with zf.open(_WORKBOOK_MEMBER) as member_stream:
                bounded_member_stream = _BoundedReader(member_stream, member_cap)
                return _count_sheet_elements(bounded_member_stream, limits["max_sheets"])
    except _XlsxWorkbookError:
        raise
    except _HOSTILE_ZIP_EXCEPTIONS:
        raise _XlsxWorkbookError("xlsx-workbook-invalid") from None


def _hash_from_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    with os.fdopen(os.dup(fd), "rb") as stream:
        for chunk in iter(lambda: stream.read(_HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest()


def _final_component_present(candidates: list[IntakeCandidate], component: str) -> bool:
    return any(c.component == component for c in candidates)


def _check_required_component(
    name: str, known_top_dirs: frozenset[str], final_candidates: list[IntakeCandidate], review: list[dict[str, Any]]
) -> None:
    if name not in known_top_dirs:
        review.append({"path": name, "reason": "missing-component-directory", "blocking": True})
    elif not _final_component_present(final_candidates, name):
        review.append({"path": name, "reason": "missing-component-content", "blocking": True})


def _check_support_group(
    known_top_dirs: frozenset[str], final_candidates: list[IntakeCandidate], review: list[dict[str, Any]]
) -> None:
    states: dict[str, bool] = {}
    satisfied_any = False
    for name in ("data_dictionary", "mappings"):
        present = name in known_top_dirs
        states[name] = present
        if present and _final_component_present(final_candidates, name):
            satisfied_any = True
    if satisfied_any:
        return
    for name, present in states.items():
        if not present:
            review.append({"path": name, "reason": "missing-component-directory", "blocking": True})
        else:
            review.append({"path": name, "reason": "missing-component-content", "blocking": True})


def _process_entry_file(
    parent_fd: int,
    name: str,
    relative_path: str,
    source_component: str,
    *,
    classify: bool,
    expected_identity: FileIdentity,
    candidates: list[IntakeCandidate],
    review: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> None:
    """Open ``name`` directly under ``parent_fd`` (the descriptor discovery
    of this very entry already produced and is still holding) and process
    it. ``expected_identity`` -- captured from the same discovery step's
    no-follow ``DirEntry.stat()`` -- must match the freshly-opened
    descriptor before it is ever read, closing the discovery-to-open race
    for this specific entry. Staged locally and committed to the shared
    lists only after the verified-source context exits WITHOUT raising, so
    a file mutated during hashing or workbook inspection never produces a
    stale committed candidate.
    """

    staged_candidate: IntakeCandidate | None = None
    staged_review: dict[str, Any] | None = None
    try:
        with _open_from_parent_fd(parent_fd, name, expected_identity=expected_identity) as fd:
            info = os.fstat(fd)
            identity = _identity(info)
            sha256_hex = _hash_from_fd(fd)

            component: Component = "_unclassified"
            sheet_count: int | None = None

            if classify:
                suffix = PurePosixPath(relative_path).suffix.lower()
                allowed = _COMPONENT_SUFFIXES[source_component]
                if suffix not in allowed:
                    staged_review = {"path": relative_path, "reason": "unsupported-format", "blocking": True}
                elif suffix == ".xlsx":
                    os.lseek(fd, 0, os.SEEK_SET)
                    dup_fd = os.dup(fd)
                    try:
                        with os.fdopen(dup_fd, "rb") as stream:
                            count = count_xlsx_sheets(stream)
                    except _XlsxWorkbookError:
                        staged_review = {"path": relative_path, "reason": "xlsx-workbook-invalid", "blocking": True}
                    else:
                        os.lseek(fd, 0, os.SEEK_SET)
                        if source_component == "datasets":
                            if count == 1:
                                component, sheet_count = "datasets", count
                            elif count > 1:
                                sheet_count = count
                                staged_review = {
                                    "path": relative_path,
                                    "reason": "dataset-xlsx-multiple-sheets",
                                    "blocking": True,
                                }
                            else:
                                staged_review = {"path": relative_path, "reason": "xlsx-workbook-invalid", "blocking": True}
                        else:
                            max_sheets = support_files.DEFAULT_LIMITS["max_sheets"]
                            if 1 <= count <= max_sheets:
                                component, sheet_count = source_component, count  # type: ignore[assignment]
                            elif count > max_sheets:
                                sheet_count = count
                                staged_review = {
                                    "path": relative_path,
                                    "reason": "support-xlsx-sheet-limit",
                                    "blocking": True,
                                }
                            else:
                                staged_review = {"path": relative_path, "reason": "xlsx-workbook-invalid", "blocking": True}
                else:
                    component = source_component  # type: ignore[assignment]

            staged_candidate = IntakeCandidate(
                relative_path=relative_path,
                source_component=source_component,
                component=component,
                identity=identity,
                sha256=sha256_hex,
                sheet_count=sheet_count,
            )
        # Reached only on a fully-consistent verified read.
        if staged_review is not None:
            review.append(staged_review)
        if staged_candidate is not None:
            candidates.append(staged_candidate)
    except VerifiedSourceError as exc:
        record = {"path": relative_path, "reason": exc.reason}
        if exc.reason in _ERROR_REASONS:
            errors.append(record)
        else:
            review.append({**record, "blocking": True})
    except OSError:
        # os.dup/os.lseek/os.fstat/read failures during hashing or workbook
        # duplication -- a filesystem-level failure distinct from anything
        # the verified-source primitive itself already normalizes.
        errors.append({"path": relative_path, "reason": "source-unreadable"})


def _dirent_identity(entry: "os.DirEntry[str]") -> FileIdentity | None:
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return None
    return _identity(info)


def _walk_and_process(
    root_fd: int, root_identity: FileIdentity
) -> tuple[list[IntakeCandidate], list[dict[str, Any]], list[dict[str, Any]], frozenset[str], bool, bool]:
    """Classify every entry under ``root_fd`` on the shared :func:`_walk_tree`
    engine. Every directory descriptor is held for its entire subtree
    (``_Descend`` only fires after a successful open+scan); every file is
    opened and processed immediately while its discovery-time parent
    descriptor is still held -- no path is ever retained and reopened
    later.

    Returns ``(candidates, review_items, errors, known_top_dirs, is_flat, tree_ok)``.
    ``tree_ok`` is False if any directory's identity changed between being
    opened and having all of its children processed (or the root itself was
    unreadable).
    """

    candidates: list[IntakeCandidate] = []
    review: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    known_top_dirs: set[str] = set()
    unknown_top_dirs_seen: set[str] = set()
    stray_root_file_seen = False

    def classify_preflight(
        parent_fd: int, entry: "os.DirEntry[str]", prefix: tuple[str, ...], parent_extra: Any, depth: int
    ) -> Any:
        nonlocal stray_root_file_seen
        parent_source_component, parent_classify, _parent_baseline = parent_extra
        relative_path = "/".join(prefix)
        depth0 = len(prefix) == 1

        try:
            is_link = entry.is_symlink()
        except OSError:
            errors.append({"path": relative_path, "reason": "source-unreadable"})
            return _SKIP
        if is_link:
            review.append({"path": relative_path, "reason": "source-symlink-not-allowed", "blocking": True})
            return _SKIP
        if entry.name.startswith(".") or entry.name in DEFAULT_JUNK_FILENAMES:
            return _SKIP

        dirent_identity = _dirent_identity(entry)
        if dirent_identity is None:
            errors.append({"path": relative_path, "reason": "source-unreadable"})
            return _SKIP

        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            errors.append({"path": relative_path, "reason": "source-unreadable"})
            return _SKIP

        if not is_dir:
            if depth0:
                stray_root_file_seen = True
                source_component = entry.name
                classify_flag = False
            else:
                source_component = parent_source_component
                classify_flag = parent_classify
            return _AsFile((source_component, classify_flag, dirent_identity))

        if depth0:
            if entry.name in _KNOWN_COMPONENTS:
                known_top_dirs.add(entry.name)
            else:
                unknown_top_dirs_seen.add(entry.name)
            child_source_component = entry.name
            child_classify = entry.name in _KNOWN_COMPONENTS
        else:
            child_source_component = parent_source_component
            child_classify = parent_classify
        if depth >= _MAX_TRAVERSAL_DEPTH:
            errors.append({"path": relative_path, "reason": "source-unreadable"})
            return _SKIP
        try:
            child_fd = _open_dir_from_parent_fd(parent_fd, entry.name, dirent_identity)
        except VerifiedSourceError as exc:
            record = {"path": relative_path, "reason": exc.reason}
            if exc.reason in _ERROR_REASONS:
                errors.append(record)
            else:
                review.append({**record, "blocking": True})
            return _SKIP
        try:
            child_entries = sorted(os.scandir(child_fd), key=lambda e: e.name)
        except OSError:
            errors.append({"path": relative_path, "reason": "source-unreadable"})
            try:
                os.close(child_fd)
            except OSError:
                pass
            return _SKIP
        return _Descend(child_fd, child_entries, (child_source_component, child_classify, dirent_identity))

    def on_file_preflight(
        parent_fd: int, entry: "os.DirEntry[str]", prefix: tuple[str, ...], _parent_extra: Any, extra: Any
    ) -> None:
        source_component, classify_flag, dirent_identity = extra
        relative_path = "/".join(prefix)
        _process_entry_file(
            parent_fd,
            entry.name,
            relative_path,
            source_component,
            classify=classify_flag,
            expected_identity=dirent_identity,
            candidates=candidates,
            review=review,
            errors=errors,
        )

    def on_dir_exit_preflight(fd: int, _prefix: tuple[str, ...], extra: Any) -> bool:
        _source_component, _classify_flag, baseline = extra
        return _recheck_dir_identity(fd, baseline)

    def close_fd(fd: int) -> None:
        try:
            os.close(fd)
        except OSError:
            pass

    tree_ok = _walk_tree(
        root_fd,
        (None, False, root_identity),
        classify=classify_preflight,
        on_file=on_file_preflight,
        close_handle=close_fd,
        on_dir_exit=on_dir_exit_preflight,
    )

    for name in sorted(unknown_top_dirs_seen):
        review.append({"path": name, "reason": "unknown-top-level-directory", "blocking": True})

    is_flat = not known_top_dirs
    # A root-level regular file is retained as _unclassified under the
    # umbrella "flat-source-layout" finding -- it is not a directory, so it
    # is never "unknown-top-level-directory". Reported once, not per file.
    if is_flat or stray_root_file_seen:
        review.append({"path": _ROOT_PATH, "reason": "flat-source-layout", "blocking": True})

    return candidates, review, errors, frozenset(known_top_dirs), is_flat, tree_ok


def inspect_intake_source(source: Path) -> IntakePreflight:
    """Deterministically classify every file under ``source`` into a
    :class:`Component`, a blocking review item, or an error.

    Never opens a source file for write, never calls an LLM/model client,
    and never inspects cell values, document text, or row content -- only
    paths, formats, and bounded ``.xlsx`` sheet-count metadata. The source
    root (including every ancestor path segment, not only its final
    component) is pinned once via a no-follow descriptor and held for the
    entire inspection; every directory descriptor discovered beneath it is
    likewise held for its whole subtree and every file is opened while its
    discovery-time parent descriptor is still held, so nothing anywhere in
    the tree is ever looked up by path a second time. Each directory's
    identity is rechecked after all of its children are processed; any
    drift anywhere in the tree discards the whole result in favor of a
    single fixed error rather than returning a partially-mixed snapshot.
    """

    try:
        root_fd = _open_pinned_root(source)
    except VerifiedSourceError as exc:
        return IntakePreflight(candidates=(), review_items=(), errors=({"path": _ROOT_PATH, "reason": exc.reason},))
    except OSError:
        return IntakePreflight(candidates=(), review_items=(), errors=({"path": _ROOT_PATH, "reason": "source-unreadable"},))

    try:
        try:
            root_info = os.fstat(root_fd)
        except OSError:
            return IntakePreflight(candidates=(), review_items=(), errors=({"path": _ROOT_PATH, "reason": "source-unreadable"},))
        if not stat.S_ISDIR(root_info.st_mode):
            return IntakePreflight(candidates=(), review_items=(), errors=({"path": _ROOT_PATH, "reason": "source-unreadable"},))
        root_identity = _identity(root_info)

        candidates, review, errors, known_top_dirs, is_flat, tree_ok = _walk_and_process(root_fd, root_identity)

        if not tree_ok:
            return IntakePreflight(candidates=(), review_items=(), errors=({"path": _ROOT_PATH, "reason": "source-unreadable"},))

        # Cross-component hardlink quarantine: based on lexical source_component
        # (physical location under datasets/), regardless of whether the file was
        # already classified _unclassified for an unrelated reason (unsupported
        # format, invalid workbook, etc.) -- the aliasing itself is the finding.
        dataset_identities = {
            (candidate.identity.device, candidate.identity.inode) for candidate in candidates if candidate.source_component == "datasets"
        }
        final_candidates: list[IntakeCandidate] = []
        for candidate in candidates:
            if candidate.source_component in ("forms", "data_dictionary", "mappings") and (
                candidate.identity.device,
                candidate.identity.inode,
            ) in dataset_identities:
                review.append({"path": candidate.relative_path, "reason": "cross-component-hardlink", "blocking": True})
                final_candidates.append(
                    IntakeCandidate(
                        relative_path=candidate.relative_path,
                        source_component=candidate.source_component,
                        component="_unclassified",
                        identity=candidate.identity,
                        sha256=candidate.sha256,
                        sheet_count=candidate.sheet_count,
                    )
                )
            else:
                final_candidates.append(candidate)

        # Required-content checks run AFTER final classification and hardlink
        # quarantine, so a malformed/multi-sheet/symlinked/hardlinked file
        # never counts as satisfying "at least one accepted file".
        if not is_flat:
            _check_required_component("datasets", known_top_dirs, final_candidates, review)
            _check_required_component("forms", known_top_dirs, final_candidates, review)
            _check_support_group(known_top_dirs, final_candidates, review)

        final_candidates.sort(key=lambda c: c.relative_path)
        review.sort(key=lambda item: (item["path"], item["reason"]))
        errors.sort(key=lambda item: (item["path"], item["reason"]))

        return IntakePreflight(candidates=tuple(final_candidates), review_items=tuple(review), errors=tuple(errors))
    finally:
        try:
            os.close(root_fd)
        except OSError:
            pass
