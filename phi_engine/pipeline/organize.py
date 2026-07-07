"""Organizer: routes messy intake files into normalized dataset JSONL.

Reads ONLY through ``config.INTAKE_DIR/<study>/`` symlinks (never a path
outside intake/) and writes derived artifacts under
``config.ORGANIZED_DIR/<study>/`` -- never back into the source tree.
Re-running wipes and rebuilds ``ORGANIZED_DIR/<study>/`` (derived data only;
intake and source are never touched).

Also drops compatibility symlinks under ``data/raw/<study>/{datasets,
annotated_pdfs}/`` pointing at the organized/annotated files, so existing
engine constants (``config.DATASETS_DIR``, ``config.ANNOTATED_PDFS_DIR``,
``input_fingerprint``, ``cleanup_verifier``'s "must-remain" checks) keep
working unchanged against a workspace that never had a `data/raw/` tree of
its own.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pandas as pd

import phi_engine.config.config as config
from phi_engine.audit.review_paths import organizer_review_path
from phi_engine.pipeline.intake import load_intake_manifest
from phi_engine.utils._extraction_io.sheet_split import promote_header, split_sheet_into_tables

__all__ = ["intake_manifest_sha", "organize"]


def _source_stem(original_name: str) -> str:
    """Bare stem of the ORIGINAL source filename (whatever its real
    extension is) -- the identity a PDF companion's stem is matched against.
    Distinct from an organized OUTPUT filename, which may carry a
    ``__<sheet>``/``__pdftable<N>`` suffix the source file never had."""
    return Path(original_name).stem


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def _dataframe_to_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
    df = df.astype(object).where(pd.notnull(df), "")
    return [{str(k): v for k, v in row.items()} for row in df.to_dict(orient="records")]


def _relink(link_path: Path, target: Path) -> None:
    """(Re)create *link_path* as a symlink to *target* -- organized-tree side only."""
    link_path.parent.mkdir(parents=True, exist_ok=True)
    if link_path.exists() or link_path.is_symlink():
        link_path.unlink()
    os.symlink(target, link_path)


def _unique_stem(base_stem: str, link_name: str, used: dict[str, str]) -> str:
    """Disambiguate a stem collision (two different source files sharing a
    basename, e.g. same name/different content in sibling source dirs) by
    suffixing the intake link's sha8 prefix. Idempotent per *link_name*."""
    prior = used.get(base_stem)
    if prior is None or prior == link_name:
        used[base_stem] = link_name
        return base_stem
    sha8 = link_name.split("__", 1)[0]
    disambiguated = f"{base_stem}__{sha8}"
    used[disambiguated] = link_name
    return disambiguated


class _Router:
    """Accumulates organizer outputs for one study; one pass over intake entries."""

    def __init__(self, study: str, intake_dir: Path, datasets_dir: Path) -> None:
        self.study = study
        self.intake_dir = intake_dir
        self.datasets_dir = datasets_dir
        self.datasets: list[dict[str, Any]] = []
        self.pdf_roles: dict[str, dict[str, Any]] = {}
        self.review_bucket: list[dict[str, Any]] = []
        self._used_stems: dict[str, str] = {}
        self._dataset_stems_lower: set[str] = set()

    def _review(self, link_name: str, original_name: str, reason: str, **extra: Any) -> None:
        entry = {"file": original_name, "link_name": link_name, "reason": reason, **extra}
        self.review_bucket.append(entry)

    def _record_dataset(self, output_name: str, rows: list[dict[str, Any]], link_name: str, original_name: str) -> None:
        out_path = self.datasets_dir / output_name
        _write_jsonl(out_path, rows)
        self.datasets.append(
            {
                "output": output_name,
                "row_count": len(rows),
                "source_link": link_name,
                "source_original": original_name,
            }
        )
        self._dataset_stems_lower.add(_source_stem(original_name).lower())
        # Compatibility symlink so config.DATASETS_DIR keeps working unchanged.
        _relink(Path(config.DATASETS_DIR) / output_name, out_path.resolve())

    def route_non_pdf(self, link_name: str, entry: dict[str, Any]) -> None:
        link_path = self.intake_dir / link_name
        original_name = Path(entry.get("original_path", link_name)).name
        if link_path.is_symlink() and not link_path.exists():
            self._review(link_name, original_name, "broken-symlink")
            return
        if not link_path.is_file():
            return  # not a regular file view through the link (shouldn't happen)

        suffix = Path(original_name).suffix.lower()
        stem = self._unique(Path(original_name).stem, link_name)

        if suffix == ".jsonl":
            self._route_jsonl(link_path, stem, link_name, original_name)
        elif suffix == ".csv":
            self._route_csv(link_path, stem, link_name, original_name)
        elif suffix == ".xlsx":
            self._route_excel(link_path, stem, link_name, original_name, engine="openpyxl")
        elif suffix == ".xls":
            self._route_excel(link_path, stem, link_name, original_name, engine="xlrd")
        elif suffix == ".json":
            self._route_json(link_path, stem, link_name, original_name)
        elif suffix == ".pdf":
            return  # handled in the second pass, after dataset stems are known
        else:
            self._review(link_name, original_name, "unrecognized-format", suffix=suffix)

    def _unique(self, base_stem: str, link_name: str) -> str:
        return _unique_stem(base_stem, link_name, self._used_stems)

    def _route_jsonl(self, link_path: Path, stem: str, link_name: str, original_name: str) -> None:
        rows: list[dict[str, Any]] = []
        try:
            text = link_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            self._review(link_name, original_name, "unreadable-jsonl", detail=type(exc).__name__)
            return
        for line_no, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                self._review(link_name, original_name, "invalid-jsonl-line", line_no=line_no)
                return
            if not isinstance(row, dict):
                self._review(link_name, original_name, "invalid-jsonl-row-shape", line_no=line_no)
                return
            rows.append(row)
        self._record_dataset(f"{stem}.jsonl", rows, link_name, original_name)

    def _route_csv(self, link_path: Path, stem: str, link_name: str, original_name: str) -> None:
        try:
            df = pd.read_csv(link_path, dtype=str, keep_default_na=False)
        except Exception as exc:  # noqa: BLE001 -- any parse failure routes to review, not a crash
            self._review(link_name, original_name, "csv-parse-error", detail=type(exc).__name__)
            return
        self._record_dataset(f"{stem}.jsonl", _dataframe_to_rows(df), link_name, original_name)

    def _route_json(self, link_path: Path, stem: str, link_name: str, original_name: str) -> None:
        try:
            data = json.loads(link_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._review(link_name, original_name, "invalid-json", detail=type(exc).__name__)
            return
        if isinstance(data, dict):
            rows = [data]
        elif isinstance(data, list) and all(isinstance(item, dict) for item in data):
            rows = data
        else:
            self._review(link_name, original_name, "unrecognized-json-shape")
            return
        self._record_dataset(f"{stem}.jsonl", rows, link_name, original_name)

    def _route_excel(
        self, link_path: Path, stem: str, link_name: str, original_name: str, *, engine: str
    ) -> None:
        try:
            book = pd.ExcelFile(link_path, engine=engine)
        except ImportError:
            reason = "xls-reader-unavailable" if engine == "xlrd" else "xlsx-reader-unavailable"
            self._review(link_name, original_name, reason)
            return
        except Exception as exc:  # noqa: BLE001 -- malformed/truncated workbook -> review, not a crash
            self._review(link_name, original_name, "excel-open-error", detail=type(exc).__name__)
            return

        wrote_any = False
        for sheet_name in book.sheet_names:
            try:
                raw = book.parse(sheet_name=sheet_name, header=None)
            except Exception as exc:  # noqa: BLE001
                self._review(
                    link_name, original_name, "excel-sheet-parse-error", sheet=str(sheet_name), detail=type(exc).__name__
                )
                continue
            tables = split_sheet_into_tables(raw)
            if tables is None:
                self._review(link_name, original_name, "excel-sheet-structure-error", sheet=str(sheet_name))
                continue
            for idx, table in enumerate(tables):
                try:
                    promoted = promote_header(table)
                except Exception as exc:  # noqa: BLE001
                    self._review(
                        link_name, original_name, "excel-header-promote-error",
                        sheet=str(sheet_name), table_index=idx, detail=type(exc).__name__,
                    )
                    continue
                if promoted.empty:
                    continue
                out_name = f"{stem}__{sheet_name}" + (f"__{idx}" if idx > 0 else "") + ".jsonl"
                self._record_dataset(out_name, _dataframe_to_rows(promoted), link_name, original_name)
                wrote_any = True
        if not wrote_any:
            self._review(link_name, original_name, "excel-no-tables-found")

    def route_pdf(self, link_name: str, entry: dict[str, Any]) -> None:
        link_path = self.intake_dir / link_name
        original_name = Path(entry.get("original_path", link_name)).name
        if link_path.is_symlink() and not link_path.exists():
            self._review(link_name, original_name, "broken-symlink")
            return
        if not link_path.is_file():
            return

        pdf_stem = Path(original_name).stem.lower()
        if pdf_stem in self._dataset_stems_lower:
            target = Path(config.ANNOTATED_PDFS_DIR) / original_name
            _relink(target, link_path.resolve())
            self.pdf_roles[link_name] = {
                "role": "annotated_pdf_companion",
                "matched_dataset_stem": pdf_stem,
                "target": str(target),
            }
            return

        try:
            import pdfplumber
        except ImportError:
            self._review(link_name, original_name, "pdf-reader-unavailable")
            self.pdf_roles[link_name] = {"role": "review", "reason": "pdf-reader-unavailable"}
            return

        stem = self._unique(Path(original_name).stem, link_name)
        table_count = 0
        try:
            with pdfplumber.open(link_path) as pdf:
                for page in pdf.pages:
                    for table in page.extract_tables() or []:
                        if not table or len(table) < 2:
                            continue
                        header = [str(c) if c is not None else "" for c in table[0]]
                        rows = [
                            {header[i]: (cell if cell is not None else "") for i, cell in enumerate(r) if i < len(header)}
                            for r in table[1:]
                        ]
                        self._record_dataset(f"{stem}__pdftable{table_count}.jsonl", rows, link_name, original_name)
                        table_count += 1
        except Exception as exc:  # noqa: BLE001 -- malformed PDF -> review, not a crash
            self._review(link_name, original_name, "pdf-open-error", detail=type(exc).__name__)
            self.pdf_roles[link_name] = {"role": "review", "reason": "pdf-open-error"}
            return

        if table_count == 0:
            self._review(link_name, original_name, "pdf-no-extractable-table")
            self.pdf_roles[link_name] = {"role": "review", "reason": "pdf-no-extractable-table"}
        else:
            self.pdf_roles[link_name] = {"role": "table_extracted", "tables_extracted": table_count}


def intake_manifest_sha(manifest: dict[str, Any]) -> str:
    """Deterministic hash of an intake manifest's entries -- used to detect
    whether ``organize()`` needs to re-run (change-detection chokepoint
    shared with ``phi_engine.pipeline.run``)."""
    encoded = json.dumps(manifest.get("entries", {}), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def organize(study: str) -> dict[str, Any]:
    """Route every intake file for *study* into normalized dataset JSONL.

    Reads ONLY through ``config.INTAKE_DIR/<study>/`` symlinks. Wipes and
    rebuilds ``config.ORGANIZED_DIR/<study>/`` on every call (derived data
    only -- intake and source are never touched).
    """
    intake_dir = Path(config.INTAKE_DIR) / study
    manifest = load_intake_manifest(study)
    entries: dict[str, dict[str, Any]] = manifest.get("entries") or {}

    organized_root = Path(config.ORGANIZED_DIR) / study
    datasets_dir = organized_root / "datasets"
    if organized_root.exists():
        shutil.rmtree(organized_root)
    datasets_dir.mkdir(parents=True, exist_ok=True)

    router = _Router(study, intake_dir, datasets_dir)

    for link_name, entry in sorted(entries.items()):
        if Path(entry.get("original_path", link_name)).suffix.lower() == ".pdf":
            continue
        router.route_non_pdf(link_name, entry)
    for link_name, entry in sorted(entries.items()):
        if Path(entry.get("original_path", link_name)).suffix.lower() == ".pdf":
            router.route_pdf(link_name, entry)

    audit_dir = Path(config.STUDY_AUDIT_DIR) if study == config.STUDY_NAME else (
        Path(config.OUTPUT_DIR) / study / "audit"
    )
    review_path = organizer_review_path(audit_dir)
    review_path.parent.mkdir(parents=True, exist_ok=True)
    with review_path.open("w", encoding="utf-8") as fh:
        for item in router.review_bucket:
            fh.write(json.dumps(item, sort_keys=True) + "\n")

    organize_manifest = {
        "study": study,
        "datasets": router.datasets,
        "pdf_roles": router.pdf_roles,
        "review_bucket": router.review_bucket,
        "source_manifest_sha": intake_manifest_sha(manifest),
    }
    (organized_root / "organize_manifest.json").write_text(
        json.dumps(organize_manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return organize_manifest
