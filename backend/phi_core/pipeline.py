"""Session pipeline: files -> read -> classify -> detect -> review -> anonymize -> export.

Progress is emitted as ProgressEvent instances and persisted on the session. A
session can loop through detection/review multiple times before finalization.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Callable

from .anonymizer import apply_to_dataset, apply_to_text
from .detectors import detect_text, header_phi_columns
from .file_readers import (
    classify_ext, iter_dataset_rows, read_narrative, sha256_of_file,
    read_csv_columns, read_xlsx_columns, read_parquet_columns,
)
from .llm_classifier import classify_dataset_headers, classify_narrative
from .models import DetectedSpan, FileArtifact, ProgressEvent, ReviewDecision


DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
UPLOAD_DIR = DATA_DIR / "uploads"
EXPORT_DIR = DATA_DIR / "exports"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
EXPORT_DIR.mkdir(parents=True, exist_ok=True)


ProgressCb = Callable[[ProgressEvent], Any]


async def ingest_file(session_id: str, upload_path: Path, original_name: str, on_progress: ProgressCb) -> FileArtifact:
    kind, ext = classify_ext(original_name)
    size = upload_path.stat().st_size
    sha = sha256_of_file(upload_path)
    art = FileArtifact(
        original_name=original_name,
        size_bytes=size,
        sha256=sha,
        kind=kind,
        subtype=ext,
        stored_path=str(upload_path),
    )
    await on_progress(ProgressEvent(phase="reading", message=f"Reading {original_name}", payload={"kind": kind, "subtype": ext}))

    if kind == "dataset":
        if ext in ("csv", "tsv"):
            cols, rows = read_csv_columns(upload_path)
        elif ext in ("xlsx", "xls"):
            cols, rows = read_xlsx_columns(upload_path)
        elif ext == "parquet":
            cols, rows = read_parquet_columns(upload_path)
        else:
            cols, rows = [], 0
        art.columns = cols
        art.row_count = rows
        await on_progress(ProgressEvent(
            phase="reading",
            message=f"Dataset parsed: {len(cols)} columns, {rows} rows. Row values withheld from LLM.",
            payload={"columns": cols, "row_count": rows},
        ))
    else:
        text = read_narrative(upload_path, ext)
        art.text_preview = text[:2000]
        # Store full text alongside as .txt for later processing (temporary; still local disk only)
        cache = upload_path.with_suffix(upload_path.suffix + ".fulltext.txt")
        cache.write_text(text, encoding="utf-8")
        await on_progress(ProgressEvent(
            phase="reading",
            message=f"Narrative extracted: {len(text)} chars",
            payload={"preview_chars": len(art.text_preview), "total_chars": len(text)},
        ))
    return art


async def classify_file(art: FileArtifact, on_progress: ProgressCb) -> dict[str, Any]:
    await on_progress(ProgressEvent(phase="classifying", message=f"LLM classifying {art.original_name}"))
    try:
        if art.kind == "dataset":
            result = await classify_dataset_headers(art.columns, art.original_name, art.row_count)
        else:
            fulltext_path = Path(art.stored_path).with_suffix(Path(art.stored_path).suffix + ".fulltext.txt")
            text = fulltext_path.read_text(encoding="utf-8") if fulltext_path.exists() else art.text_preview
            result = await classify_narrative(text, art.original_name)
    except Exception as e:
        result = {"content_type": "unknown", "notes": f"classification failed: {type(e).__name__}"}
    await on_progress(ProgressEvent(phase="classifying", message="Classification complete", payload=result))
    return result


async def detect_file(art: FileArtifact, detectors: list[str], on_progress: ProgressCb) -> list[DetectedSpan]:
    all_spans: list[DetectedSpan] = []
    if art.kind == "dataset":
        # 1. header-based full-column PHI
        hits = header_phi_columns(art.columns)
        for col, (ent, cat) in hits.items():
            all_spans.append(DetectedSpan(
                start=0,
                end=0,
                value=col,
                entity_type=ent,
                hipaa_category=cat,
                detector="header_hint",
                confidence=0.9,
                authority="45 CFR 164.514(b)(2)(i)",
                column=col,
                row_index=None,
            ))
        await on_progress(ProgressEvent(
            phase="detecting",
            message=f"Header hints flagged {len(hits)} PHI columns",
            payload={"columns": list(hits.keys())},
        ))
        # 2. optional cell-level rule scan on remaining columns
        src = Path(art.stored_path)
        cell_count = 0
        for row_idx, row in iter_dataset_rows(src, art.subtype):
            for col, val in row.items():
                if col in hits or not val:
                    continue
                spans = detect_text(val, detectors=detectors)
                for s in spans:
                    s.column = col
                    s.row_index = row_idx
                    cell_count += 1
                    all_spans.append(s)
            if row_idx > 0 and row_idx % 500 == 0:
                await on_progress(ProgressEvent(
                    phase="detecting",
                    message=f"Scanned {row_idx} rows, found {cell_count} additional cell spans",
                    percent=min(90.0, row_idx / max(1, art.row_count) * 100),
                ))
        await on_progress(ProgressEvent(
            phase="detecting",
            message=f"Dataset scan complete: {cell_count} cell-level spans",
            payload={"cell_span_count": cell_count},
            percent=100.0,
        ))
    else:
        fulltext_path = Path(art.stored_path).with_suffix(Path(art.stored_path).suffix + ".fulltext.txt")
        text = fulltext_path.read_text(encoding="utf-8") if fulltext_path.exists() else ""
        spans = detect_text(text, detectors=detectors)
        for s in spans:
            s.file_offset = s.start
        all_spans.extend(spans)
        await on_progress(ProgressEvent(
            phase="detecting",
            message=f"Narrative scan complete: {len(spans)} spans",
            payload={"span_count": len(spans)},
            percent=100.0,
        ))
    return all_spans


def apply_reviews(spans: list[DetectedSpan], decisions: list[ReviewDecision]) -> list[DetectedSpan]:
    by_id = {s.span_id: s for s in spans}
    for d in decisions:
        s = by_id.get(d.span_id)
        if s is None:
            continue
        if d.action == "accept":
            s.review_status = "accepted"
            s.replacement = d.replacement
        elif d.action == "reject":
            s.review_status = "rejected"
        elif d.action == "reclassify":
            s.review_status = "reclassified"
            if d.new_category:
                s.entity_type = d.new_category
                s.hipaa_category = d.new_category if len(d.new_category) == 1 else s.hipaa_category
            s.replacement = d.replacement
        s.review_comment = d.comment
    return spans


async def anonymize_files(files: list[FileArtifact], spans: list[DetectedSpan], on_progress: ProgressCb) -> dict[str, str]:
    export_paths: dict[str, str] = {}
    for art in files:
        await on_progress(ProgressEvent(phase="anonymizing", message=f"Anonymizing {art.original_name}"))
        src = Path(art.stored_path)
        dst = EXPORT_DIR / f"{art.file_id}__{art.original_name}"
        file_spans = [s for s in spans if s.review_status in ("accepted", "reclassified")]
        if art.kind == "dataset":
            # header hint spans: full-column redaction
            full_col: dict[str, DetectedSpan] = {}
            cell_map: dict[tuple[int, str], list[DetectedSpan]] = {}
            for s in file_spans:
                if s.column and s.row_index is None:
                    full_col[s.column] = s
                elif s.column and s.row_index is not None:
                    cell_map.setdefault((s.row_index, s.column), []).append(s)
            apply_to_dataset(src, dst, art.subtype, cell_map, full_col)
        else:
            fulltext_path = src.with_suffix(src.suffix + ".fulltext.txt")
            text = fulltext_path.read_text(encoding="utf-8") if fulltext_path.exists() else ""
            dst = EXPORT_DIR / f"{art.file_id}__{Path(art.original_name).stem}.redacted.txt"
            redacted = apply_to_text(text, file_spans)
            dst.write_text(redacted, encoding="utf-8")
        export_paths[art.file_id] = str(dst)
        await on_progress(ProgressEvent(
            phase="anonymizing",
            message=f"Wrote {dst.name}",
            payload={"file_id": art.file_id, "export_path": str(dst)},
        ))
    return export_paths
