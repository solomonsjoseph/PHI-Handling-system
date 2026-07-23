"""Organizer: routes intake artifacts into verified normalized outputs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any

import pandas as pd

import phi_engine.config.config as config
from phi_engine.audit.review_paths import organizer_review_path
from phi_engine.pipeline.dependencies import DependencyKind, OrganizedHeader, SupportParseStatus
from phi_engine.pipeline.intake import IntakeNotReadyError, load_intake_manifest
from phi_engine.pipeline.support_files import parse_support_artifact
from phi_engine.pipeline.verified_source import FileIdentity, VerifiedSourceError, _open_from_root_fd, _open_pinned_root
from phi_engine.security.phi_review import normalize_header
from phi_engine.utils._extraction_io.sheet_split import promote_header, split_sheet_into_tables
from phi_engine.utils.pipeline_lock import held_lock_path, lock_path_for, pipeline_lock

__all__ = ["intake_manifest_sha", "organize", "OrganizerLockNotHeldError", "_copy_descriptor_to_verified"]


class OrganizerLockNotHeldError(RuntimeError):
    """Typed, value-free failure: ``_organize_locked`` was invoked without
    the caller already holding ``pipeline_lock(study)``. ``_organize_locked``
    is documented lock-required (see its own docstring); this enforces
    that contract at entry using the existing per-thread lock-ownership
    machinery (``held_lock_path``) instead of trusting caller discipline
    alone -- an unlocked direct call fails closed with this fixed code
    before any read or write, never a raw study name or path."""

    code = "organizer_lock_not_held"

    def __init__(self) -> None:
        super().__init__(self.code)


def _require_pipeline_lock_owned(study: str) -> None:
    """Fail closed unless the CALLING THREAD already holds
    ``pipeline_lock(study)`` -- via ``organize()``'s own wrapper, or via
    run.py's ``run_pipeline`` (which acquires the identical non-reentrant
    per-study lock before ``_run_pipeline_locked`` calls this module's
    lock-required body directly). Reuses the existing per-thread
    lock-ownership check rather than a new bespoke registry."""
    if held_lock_path() != lock_path_for(study):
        raise OrganizerLockNotHeldError()


def _normalized_source_stem(relative_path: str) -> str:
    stem = Path(PurePosixPath(relative_path).name).stem.lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", stem).strip("_")
    return normalized or "support"


def _support_public(parsed: Any) -> dict[str, Any]:
    failure = parsed.failure_code.value if parsed.failure_code is not None else None
    return {
        "artifact_id": parsed.artifact_id,
        "source_sha256": parsed.source_sha256,
        "kind": parsed.kind.value,
        "format": parsed.format,
        "parse_status": parsed.parse_status.value,
        "normalized_rows_sha256": parsed.normalized_rows_sha256,
        "failure_code": failure,
    }


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            line = json.dumps(row, sort_keys=True, default=_json_default, ensure_ascii=False)
            digest.update(line.encode("utf-8"))
            digest.update(b"\n")
            fh.write(line + "\n")
    path.chmod(0o600)
    return digest.hexdigest()


def _relink(link_path: Path, target: Path) -> None:
    link_path.parent.mkdir(parents=True, exist_ok=True)
    if link_path.exists() or link_path.is_symlink():
        link_path.unlink()
    os.symlink(target, link_path)


def _unique_stem(base_stem: str, link_name: str, used: dict[str, str]) -> str:
    if base_stem not in used:
        used[base_stem] = link_name
        return base_stem
    if used[base_stem] == link_name:
        return base_stem
    suffix = hashlib.sha256(link_name.encode("utf-8")).hexdigest()[:8]
    candidate = f"{base_stem}__{suffix}"
    used[candidate] = link_name
    return candidate


def _header_id(artifact_id: str, source_sha256: str, column_index: int) -> str:
    payload = artifact_id.encode() + b"\0" + source_sha256.encode() + b"\0" + str(column_index).encode()
    return "h_" + hashlib.sha256(payload).hexdigest()[:24]


def _headers_for_columns(columns: list[Any], artifact_id: str, source_sha256: str) -> tuple[OrganizedHeader, ...]:
    headers = []
    for index, raw in enumerate(columns):
        raw_name = "" if raw is None else str(raw)
        headers.append(
            OrganizedHeader(
                header_id=_header_id(artifact_id, source_sha256, index),
                column_index=index,
                raw_name=raw_name,
                normalized_name=normalize_header(raw_name),
            )
        )
    return tuple(headers)


def _records_to_normalized_rows(records: list[dict[str, Any]], artifact_id: str, source_sha256: str) -> tuple[list[dict[str, Any]], tuple[OrganizedHeader, ...]]:
    columns: list[str] = []
    seen: set[str] = set()
    for record in records:
        for key in record:
            text = str(key)
            if text not in seen:
                seen.add(text)
                columns.append(text)
    headers = _headers_for_columns(columns, artifact_id, source_sha256)
    id_by_col = {header.raw_name: header.header_id for header in headers}
    rows = [{id_by_col[col]: record.get(col, "") for col in columns} for record in records]
    return rows, headers


def _dataframe_to_normalized_rows(df: pd.DataFrame, artifact_id: str, source_sha256: str) -> tuple[list[dict[str, Any]], tuple[OrganizedHeader, ...]]:
    df = df.astype(object).where(pd.notnull(df), "")
    headers = _headers_for_columns(list(df.columns), artifact_id, source_sha256)
    columns = list(df.columns)
    rows = []
    for raw in df.to_dict(orient="records"):
        rows.append({headers[index].header_id: raw.get(columns[index], "") for index in range(len(columns))})
    return rows, headers


def _copy_descriptor_to_verified(fd: int, dest: Path) -> str:
    verified_dir = dest.parent
    verified_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(verified_dir, 0o700)

    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    tmp_path: Path | None = None
    tmp_fd: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        for _attempt in range(8):
            candidate_path = verified_dir / f".{dest.name}.{os.urandom(8).hex()}.tmp"
            try:
                tmp_fd = os.open(candidate_path, flags, 0o600)
            except FileExistsError:
                continue
            tmp_path = candidate_path
            break
        if tmp_fd is None or tmp_path is None:
            raise OSError("unable to create a private temporary snapshot file")
        os.fchmod(tmp_fd, 0o600)

        with os.fdopen(os.dup(fd), "rb") as src, os.fdopen(tmp_fd, "wb") as out:
            tmp_fd = None  # ownership transferred to the fdopen wrapper
            for chunk in iter(lambda: src.read(1 << 20), b""):
                digest.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())

        os.replace(tmp_path, dest)
        tmp_path = None  # renamed; nothing left to clean up

        dir_fd = os.open(verified_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

        os.lseek(fd, 0, os.SEEK_SET)
        return digest.hexdigest()
    finally:
        if tmp_fd is not None:
            os.close(tmp_fd)
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _same_stat(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and stat.S_ISREG(right.st_mode)
    )


def _verified_snapshot(entry: dict[str, Any], root_fd: int, verified_dir: Path) -> tuple[Path | None, str | None, str | None]:
    relative_path = str(entry["relative_path"])
    try:
        expected_identity = FileIdentity(
            device=int(entry["device"]),
            inode=int(entry["inode"]),
            size=int(entry["size"]),
            mtime_ns=int(entry["mtime_ns"]),
        )
    except (KeyError, TypeError, ValueError):
        return None, None, "source-unreadable"

    # _open_from_root_fd's own pre/post identity check (via the shared
    # _open_verified_core, against expected_identity, and again on exit)
    # can override a pending `return` from inside the `with` block with
    # its own generic source-unreadable VerifiedSourceError.
    # detected_reason/detected_sha survive that override (they are set
    # before the return, in this enclosing scope) so the more specific
    # organizer-detected reason is what actually gets reported, while a
    # genuine identity mismatch this function never noticed itself still
    # fails closed via the except branch below.
    detected_reason: str | None = None
    detected_sha: str | None = None
    dest = verified_dir / str(entry["artifact_id"])
    try:
        with _open_from_root_fd(root_fd, relative_path, expected_identity=expected_identity) as fd:
            pre = os.fstat(fd)
            copied_sha = _copy_descriptor_to_verified(fd, dest)
            post = os.fstat(fd)
            if not _same_stat(pre, post):
                dest.unlink(missing_ok=True)
                detected_reason = "source-mutated-during-copy"
                return None, None, detected_reason
            if copied_sha != entry.get("sha256"):
                dest.unlink(missing_ok=True)
                detected_sha = copied_sha
                detected_reason = "source-hash-mismatch"
                return None, detected_sha, detected_reason
            return dest, copied_sha, None
    except VerifiedSourceError as exc:
        # _open_from_root_fd's own post-yield identity re-check (fired
        # after the `with` block already returned normally, i.e. a race
        # this function's own pre/post fstat comparison never caught) can
        # land here even though _copy_descriptor_to_verified already wrote
        # `dest`. Never leave that write behind: a source-mutation race
        # detected only after copy completion still means what got copied
        # cannot be trusted.
        dest.unlink(missing_ok=True)
        if detected_reason is not None:
            return None, detected_sha, detected_reason
        return None, None, exc.reason
    except OSError:
        # fstat/dup/lseek/read/write/fsync/rename failures during the
        # snapshot copy itself -- distinct from anything _open_from_root_fd
        # already normalizes. Never leak the raw OSError text; clean up any
        # artifact the copy may have partially produced before this point.
        dest.unlink(missing_ok=True)
        if detected_reason is not None:
            return None, detected_sha, detected_reason
        return None, None, "source-unreadable"


_COMPONENT_ROLES: dict[str, str] = {
    "datasets": "dataset",
    "data_dictionary": "dictionary",
    "mappings": "mapping",
    "forms": "pdf",
}


class _Router:
    def __init__(
        self,
        study: str,
        intake_dir: Path,
        datasets_dir: Path,
        organized_root: Path,
        root_fd: int,
    ) -> None:
        self.study = study
        self.intake_dir = intake_dir
        self.datasets_dir = datasets_dir
        self.organized_root = organized_root
        self.verified_dir = organized_root / ".verified_sources"
        self.protected_headers_dir = organized_root / ".protected" / "headers"
        self.protected_support_dir = organized_root / ".protected" / "support"
        self.datasets: list[dict[str, Any]] = []
        self.support_artifacts: list[dict[str, Any]] = []
        self.pdf_roles: dict[str, dict[str, Any]] = {}
        self.review_bucket: list[dict[str, Any]] = []
        self._used_stems: dict[str, str] = {}
        # Held for the router's entire lifetime -- never re-derived from
        # source_root's lexical pathname; see _organize_locked's own
        # docstring for why this single pinned descriptor is threaded
        # through every verified snapshot instead.
        self.root_fd = root_fd

    def _role_for(self, entry: dict[str, Any]) -> str:
        component = str(entry.get("component", "_unclassified"))
        return _COMPONENT_ROLES.get(component, "_unclassified")

    def _review(self, link_name: str, entry: dict[str, Any], reason: str, **extra: Any) -> None:
        self.review_bucket.append({"file": Path(str(entry.get("relative_path", link_name))).name, "link_name": link_name, "reason": reason, **extra})

    def _unique(self, base_stem: str, link_name: str) -> str:
        return _unique_stem(base_stem, link_name, self._used_stems)

    def _snapshot(self, link_name: str, entry: dict[str, Any]) -> Path | None:
        snapshot, copied_sha, reason = _verified_snapshot(entry, self.root_fd, self.verified_dir)
        if reason is not None:
            self._review(link_name, entry, reason, copied_sha=copied_sha)
            return None
        return snapshot

    def _record_dataset(self, output_name: str, rows: list[dict[str, Any]], headers: tuple[OrganizedHeader, ...], link_name: str, entry: dict[str, Any]) -> None:
        out_path = self.datasets_dir / output_name
        normalized_sha = _write_jsonl(out_path, rows)
        artifact_id = str(entry["artifact_id"])
        header_payload = {
            "artifact_id": artifact_id,
            "source_sha256": entry["sha256"],
            "headers": [asdict(header) for header in headers],
            "source_relative_path": entry["relative_path"],
        }
        self.protected_headers_dir.mkdir(parents=True, exist_ok=True)
        protected_path = self.protected_headers_dir / f"{artifact_id}.json"
        protected_path.write_text(json.dumps(header_payload, indent=2, sort_keys=True), encoding="utf-8")
        protected_path.chmod(0o600)
        self.datasets.append(
            {
                "artifact_id": artifact_id,
                "source_sha256": entry["sha256"],
                "output": output_name,
                "normalized_rows_sha256": normalized_sha,
                "row_count": len(rows),
                "headers": [
                    {
                        "header_id": header.header_id,
                        "column_index": header.column_index,
                        "normalized_name": header.normalized_name,
                    }
                    for header in headers
                ],
            }
        )
        _relink(Path(config.DATASETS_DIR) / output_name, out_path.resolve())

    def route_dataset(self, link_name: str, entry: dict[str, Any]) -> None:
        snapshot = self._snapshot(link_name, entry)
        if snapshot is None:
            return
        rel = str(entry.get("relative_path", link_name))
        suffix = Path(rel).suffix.lower()
        stem = self._unique(Path(rel).stem, link_name)
        if suffix == ".csv":
            self._route_csv(snapshot, stem, link_name, entry)
        elif suffix == ".xlsx":
            self._route_excel(snapshot, stem, link_name, entry, engine="openpyxl")
        elif suffix == ".xls":
            self._route_excel(snapshot, stem, link_name, entry, engine="xlrd")
        else:
            self._review(link_name, entry, "unrecognized-format", suffix=suffix)

    def route_support(self, link_name: str, entry: dict[str, Any], kind: DependencyKind) -> None:
        snapshot = self._snapshot(link_name, entry)
        if snapshot is None:
            return
        rel = str(entry.get("relative_path", link_name))
        logical_format = PurePosixPath(rel).suffix.lower().lstrip(".")
        normalized_stem = _normalized_source_stem(rel)
        parsed = parse_support_artifact(
            artifact_id=str(entry["artifact_id"]),
            source_sha256=str(entry["sha256"]),
            kind=kind,
            source_path=snapshot,
            output_dir=self.organized_root / "support" / kind.value,
            logical_format=logical_format,
            normalized_source_stem=normalized_stem,
        )
        self.protected_support_dir.mkdir(parents=True, exist_ok=True)
        protected_payload = {
            **_support_public(parsed),
            "normalized_rows_path": str(parsed.normalized_rows_path) if parsed.normalized_rows_path is not None else None,
            "source_relative_path": rel,
            "normalized_source_stem": normalized_stem,
        }
        protected_path = self.protected_support_dir / f"{entry['artifact_id']}.json"
        protected_path.write_text(json.dumps(protected_payload, indent=2, sort_keys=True), encoding="utf-8")
        protected_path.chmod(0o600)
        self.support_artifacts.append(_support_public(parsed))
        if parsed.parse_status is SupportParseStatus.FAILED:
            self._review(link_name, entry, "support-parse-failed", failure_code=parsed.failure_code.value if parsed.failure_code else None)

    def _route_csv(self, path: Path, stem: str, link_name: str, entry: dict[str, Any]) -> None:
        try:
            df = pd.read_csv(path, dtype=str, keep_default_na=False)
        except Exception as exc:
            self._review(link_name, entry, "csv-parse-error", detail=type(exc).__name__)
            return
        rows, headers = _dataframe_to_normalized_rows(df, str(entry["artifact_id"]), str(entry["sha256"]))
        self._record_dataset(f"{stem}.jsonl", rows, headers, link_name, entry)

    def _route_excel(self, path: Path, stem: str, link_name: str, entry: dict[str, Any], *, engine: str) -> None:
        try:
            book = pd.ExcelFile(path, engine=engine)
        except ImportError:
            self._review(link_name, entry, "xls-reader-unavailable" if engine == "xlrd" else "xlsx-reader-unavailable")
            return
        except Exception as exc:
            self._review(link_name, entry, "excel-open-error", detail=type(exc).__name__)
            return
        wrote_any = False
        for sheet_name in book.sheet_names:
            try:
                raw = book.parse(sheet_name=sheet_name, header=None)
                tables = split_sheet_into_tables(raw)
            except Exception as exc:
                self._review(link_name, entry, "excel-sheet-parse-error", sheet=str(sheet_name), detail=type(exc).__name__)
                continue
            if tables is None:
                self._review(link_name, entry, "excel-sheet-structure-error", sheet=str(sheet_name))
                continue
            for idx, table in enumerate(tables):
                try:
                    promoted = promote_header(table)
                except Exception as exc:
                    self._review(link_name, entry, "excel-header-promote-error", sheet=str(sheet_name), table_index=idx, detail=type(exc).__name__)
                    continue
                if promoted.empty:
                    continue
                rows, headers = _dataframe_to_normalized_rows(promoted, str(entry["artifact_id"]), str(entry["sha256"]))
                out_name = f"{stem}__{sheet_name}" + (f"__{idx}" if idx > 0 else "") + ".jsonl"
                self._record_dataset(out_name, rows, headers, link_name, entry)
                wrote_any = True
        if not wrote_any:
            self._review(link_name, entry, "excel-no-tables-found")

    def route_pdf(self, link_name: str, entry: dict[str, Any]) -> None:
        snapshot = self._snapshot(link_name, entry)
        if snapshot is None:
            return
        original_name = Path(str(entry.get("relative_path", link_name))).name
        try:
            import pdfplumber
        except ImportError:
            self._review(link_name, entry, "pdf-reader-unavailable")
            self.pdf_roles[link_name] = {"role": "review", "reason": "pdf-reader-unavailable"}
            return
        stem = self._unique(Path(original_name).stem, link_name)
        table_count = 0
        try:
            with pdfplumber.open(snapshot) as pdf:
                for page in pdf.pages:
                    for table in page.extract_tables() or []:
                        if not table or len(table) < 2:
                            continue
                        header = [str(c) if c is not None else "" for c in table[0]]
                        records = [{header[i]: (cell if cell is not None else "") for i, cell in enumerate(r) if i < len(header)} for r in table[1:]]
                        rows, headers = _records_to_normalized_rows(records, str(entry["artifact_id"]), str(entry["sha256"]))
                        self._record_dataset(f"{stem}__pdftable{table_count}.jsonl", rows, headers, link_name, entry)
                        table_count += 1
        except Exception as exc:
            self._review(link_name, entry, "pdf-open-error", detail=type(exc).__name__)
            self.pdf_roles[link_name] = {"role": "review", "reason": "pdf-open-error"}
            return
        if table_count == 0:
            self._review(link_name, entry, "pdf-no-extractable-table")
            self.pdf_roles[link_name] = {"role": "review", "reason": "pdf-no-extractable-table"}
        else:
            self.pdf_roles[link_name] = {"role": "table_extracted", "tables_extracted": table_count}


def intake_manifest_sha(manifest: dict[str, Any]) -> str:
    encoded = json.dumps(manifest.get("entries", {}), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def organize(study: str) -> dict[str, Any]:
    """Public standalone entrypoint. Holds the per-study pipeline lock for
    the ENTIRE operation -- manifest load/status gate through every write
    -- so a concurrent re-intake can never mutate the intake decision this
    call is acting on: whichever of organize()/intake_add() acquires the
    non-reentrant per-study lock first serializes the other out with an
    immediate PipelineBusyError, never a stale in-flight read. See
    ``_organize_locked`` for the lock-required body; ``_run_pipeline_locked``
    (which already owns this same lock for the whole run) calls that body
    directly, since re-entering this wrapper would itself raise
    PipelineBusyError."""
    with pipeline_lock(study):
        return _organize_locked(study)


def _dataset_component_basenames(entries: dict[str, dict[str, Any]]) -> tuple[str, ...]:
    """Recreate the exact basename set check_forms_manifest previously
    derived from an ordinary (symlink-following) top-level directory
    listing of source_root/datasets -- from the intake manifest's
    already-verified 'datasets' component entries instead, so this
    metadata read never touches source_root's filesystem at all. Only
    entries directly under datasets/ (not a nested subdirectory) are
    included, matching the prior non-recursive Path.iterdir() exactly;
    'datasets' component entries are already restricted to the same
    accepted suffixes check_forms_manifest's own scan filtered for, by
    intake's closed source-format matrix."""
    names: list[str] = []
    for entry in entries.values():
        if entry.get("component") != "datasets":
            continue
        rel = PurePosixPath(str(entry.get("relative_path", "")))
        if rel.parent == PurePosixPath("datasets"):
            names.append(rel.name)
    return tuple(sorted(names))


def _require_pinned_root_unchanged(source_root: Path, root_fd: int) -> None:
    """Fail closed with the SAME fixed reason open_verified_source uses
    for any other identity drift if source_root's LEXICAL identity no
    longer matches the descriptor _open_pinned_root just pinned -- e.g. a
    rename+symlink-replace race landing in the instant between the
    ancestry walk completing and this check running, a window the walk
    itself cannot observe since it already finished. A single no-follow
    lstat is used purely for identity comparison, never to open or read
    through source_root's pathname; every subsequent read of source
    content in this operation goes through root_fd itself instead."""
    try:
        pinned = os.fstat(root_fd)
        current = os.lstat(source_root)
    except OSError:
        raise VerifiedSourceError("source-unreadable") from None
    if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
        raise VerifiedSourceError("source-unreadable")


def _organize_locked(study: str) -> dict[str, Any]:
    """Lock-required body. Caller MUST already hold pipeline_lock(study)
    -- via this module's own organize() wrapper, or via run.py's
    run_pipeline, which acquires the identical non-reentrant per-study
    lock before _run_pipeline_locked calls this function directly. That
    contract is enforced below, not just documented: an unlocked direct
    call raises OrganizerLockNotHeldError before any read or write."""
    _require_pipeline_lock_owned(study)

    intake_dir = Path(config.INTAKE_DIR) / study
    manifest = load_intake_manifest(study)
    status = manifest.get("status")
    if status != "ready":
        raise IntakeNotReadyError(status)

    entries: dict[str, dict[str, Any]] = manifest.get("entries") or {}

    from scripts.extraction.forms_manifest import check_forms_manifest

    # Pin the source root exactly ONCE and hold that SAME descriptor
    # through the forms-manifest metadata read below and every per-entry
    # verified snapshot in the routing loop -- never Path.resolve()
    # (silently follows a symlink placed anywhere in source_root's
    # ancestry) and never an ordinary Path.exists()/is_dir()/iterdir()/
    # open() on source_root or any descendant. _require_pinned_root_
    # unchanged closes the window between the ancestry walk completing and
    # this check running: a swap landing there fails closed immediately,
    # before check_forms_manifest, before organized/ is deleted/recreated,
    # and before anything else is written.
    source_root = Path(str(manifest["source_root"]))
    root_fd = _open_pinned_root(source_root)
    try:
        _require_pinned_root_unchanged(source_root, root_fd)

        # The forms-manifest gate's dataset inventory is derived from the
        # already-verified intake entries, never from a fresh (symlink-
        # following) directory listing of source_root/datasets.
        manifest_check = check_forms_manifest(
            source_root / "datasets",
            study=study,
            actual_files=_dataset_component_basenames(entries),
        )

        organized_root = Path(config.ORGANIZED_DIR) / study
        datasets_dir = organized_root / "datasets"
        if organized_root.exists():
            shutil.rmtree(organized_root)
        datasets_dir.mkdir(parents=True, exist_ok=True)

        router = _Router(
            study,
            intake_dir,
            datasets_dir,
            organized_root,
            root_fd,
        )

        for link_name, entry in sorted(entries.items(), key=lambda item: item[1].get("relative_path", item[0])):
            role = router._role_for(entry)
            if role == "dataset":
                router.route_dataset(link_name, entry)
            elif role == "dictionary":
                router.route_support(link_name, entry, DependencyKind.DICTIONARY)
            elif role == "mapping":
                router.route_support(link_name, entry, DependencyKind.MAPPING)
            # role in {"pdf", "_unclassified"}: pdf routed in the second pass below;
            # _unclassified is never parsed.
        for link_name, entry in sorted(entries.items(), key=lambda item: item[1].get("relative_path", item[0])):
            if router._role_for(entry) == "pdf":
                router.route_pdf(link_name, entry)
    finally:
        os.close(root_fd)

    audit_dir = Path(config.STUDY_AUDIT_DIR) if study == config.STUDY_NAME else (Path(config.OUTPUT_DIR) / study / "audit")
    review_path = organizer_review_path(audit_dir)
    review_path.parent.mkdir(parents=True, exist_ok=True)
    with review_path.open("w", encoding="utf-8") as fh:
        for item in router.review_bucket:
            fh.write(json.dumps(item, sort_keys=True) + "\n")
    review_path.chmod(0o600)

    organize_manifest = {
        "study": study,
        "datasets": router.datasets,
        "support_artifacts": router.support_artifacts,
        "pdf_roles": router.pdf_roles,
        "review_bucket": router.review_bucket,
        "intake_manifest_sha": intake_manifest_sha(manifest),
    }
    manifest_path = organized_root / "organize_manifest.json"
    manifest_path.write_text(json.dumps(organize_manifest, indent=2, sort_keys=True, default=_json_default), encoding="utf-8")
    manifest_path.chmod(0o600)
    # Attached AFTER the on-disk write above so organize_manifest.json's
    # persisted shape is unchanged; run.py reuses this in-memory result
    # directly instead of performing its own second source-derived read.
    organize_manifest["dependency_relations"] = manifest_check.dependency_relations
    return organize_manifest
