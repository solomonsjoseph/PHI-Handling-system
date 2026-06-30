#!/usr/bin/env python3
"""Prepare a SoT source pack from a PDF and row-1 dataset headers only."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

from defusedxml import ElementTree


def _add_repo_to_path(repo_root: Path) -> None:
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


def _headers_via_repo(repo_root: Path, dataset: Path, *, run_dir: Path | None = None) -> list[str]:
    _add_repo_to_path(repo_root)
    if run_dir is not None:
        # On the orchestrated path, unconditionally read the shared header_store
        # (Note 6). Fallback to direct row-1 read only if the store is absent/
        # malformed (resolve_headers is fail-soft).
        from scripts.extraction.header_store import load_header_store, resolve_headers

        store = load_header_store(run_dir)
        return resolve_headers(store, dataset.stem, dataset)
    # Standalone path (no run_dir): attempt repo import, fallback to direct read.
    try:
        from scripts.source_truth.study_intake import read_headers_only
    except ImportError:
        return _headers_direct(dataset)

    return read_headers_only(dataset)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _column_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha()).upper()
    value = 0
    for letter in letters:
        value = value * 26 + (ord(letter) - ord("A") + 1)
    return value


def _relationship_target(base: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return str((Path(base).parent / target).as_posix())


def _first_sheet_path(zf: zipfile.ZipFile) -> str:
    workbook = ElementTree.fromstring(zf.read("xl/workbook.xml"))
    sheet = next(elem for elem in workbook.iter() if _local_name(elem.tag) == "sheet")
    relationship_id = sheet.attrib[
        "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    ]

    rels = ElementTree.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    for rel in rels:
        if rel.attrib.get("Id") == relationship_id:
            return _relationship_target("xl/workbook.xml", rel.attrib["Target"])
    raise ValueError("Could not find first worksheet relationship")


def _cell_child_text(cell: Any, child_name: str) -> str | None:
    for child in cell.iter():
        if _local_name(child.tag) == child_name and child.text is not None:
            return child.text
    return None


def _read_shared_strings(zf: zipfile.ZipFile, needed: set[int]) -> dict[int, str]:
    if not needed:
        return {}
    results: dict[int, str] = {}
    max_needed = max(needed)
    index = -1
    with zf.open("xl/sharedStrings.xml") as handle:
        for _event, elem in ElementTree.iterparse(handle, events=("end",)):
            if _local_name(elem.tag) != "si":
                continue
            index += 1
            if index in needed:
                results[index] = "".join(elem.itertext()).strip()
                if len(results) == len(needed):
                    break
            if index >= max_needed and len(results) == len(needed):
                break
            elem.clear()
    return results


def _headers_from_xlsx_first_row(dataset: Path) -> list[str]:
    with zipfile.ZipFile(dataset) as zf:
        sheet_path = _first_sheet_path(zf)
        cells: list[tuple[int, str, str | None]] = []
        shared_indices: set[int] = set()

        with zf.open(sheet_path) as handle:
            for _event, elem in ElementTree.iterparse(handle, events=("end",)):
                if _local_name(elem.tag) != "row":
                    continue
                row_number = elem.attrib.get("r")
                if row_number != "1":
                    elem.clear()
                    continue
                for cell in elem:
                    if _local_name(cell.tag) != "c":
                        continue
                    cell_ref = cell.attrib.get("r", "")
                    cell_type = cell.attrib.get("t")
                    raw_value = _cell_child_text(cell, "v")
                    if cell_type == "inlineStr":
                        raw_value = _cell_child_text(cell, "t")
                    if raw_value is None:
                        continue
                    if cell_type == "s":
                        shared_index = int(raw_value)
                        shared_indices.add(shared_index)
                        cells.append((_column_index(cell_ref), "s", str(shared_index)))
                    else:
                        cells.append((_column_index(cell_ref), "raw", raw_value.strip()))
                elem.clear()
                break

        shared_strings = _read_shared_strings(zf, shared_indices)
        headers = []
        for _col, cell_type, value in sorted(cells, key=lambda item: item[0]):
            header = shared_strings.get(int(value or "0"), "") if cell_type == "s" else value or ""
            if header:
                headers.append(header)
        return headers


def _headers_direct(dataset: Path) -> list[str]:
    suffix = dataset.suffix.lower()
    if suffix == ".csv":
        with dataset.open(newline="", encoding="utf-8-sig") as handle:
            return next(csv.reader(handle))
    if suffix in {".xlsx", ".xlsm"}:
        return _headers_from_xlsx_first_row(dataset)
    raise ValueError(f"Unsupported dataset format without repo helper: {dataset.suffix}")


def _pdf_pages_via_repo(repo_root: Path, pdf: Path) -> list[dict[str, Any]]:
    _add_repo_to_path(repo_root)
    try:
        from scripts.source_truth.study_intake import _read_form_pdf_pages
    except ImportError:
        return _pdf_pages_via_pdfplumber(pdf)

    return _read_form_pdf_pages(pdf)


def _annotation_text(annot: dict[str, Any]) -> str | None:
    for key in ("contents", "Content", "title", "T", "subject", "Subj"):
        value = annot.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    data = annot.get("data")
    if isinstance(data, dict):
        for key in ("Contents", "T", "Subj"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _pdf_pages_via_pdfplumber(pdf: Path) -> list[dict[str, Any]]:
    import pdfplumber

    pages: list[dict[str, Any]] = []
    with pdfplumber.open(pdf) as doc:
        for index, page in enumerate(doc.pages, start=1):
            lines = []
            text = page.extract_text(x_tolerance=1, y_tolerance=3) or ""
            for line in text.splitlines():
                line = line.strip()
                if line:
                    lines.append(line)

            annotations = []
            annotation_details = []
            for annot in getattr(page, "annots", None) or []:
                if not isinstance(annot, dict):
                    continue
                text_value = _annotation_text(annot)
                if text_value:
                    annotations.append(text_value)
                detail = {
                    "text": text_value,
                    "page": index,
                    "x0": annot.get("x0"),
                    "top": annot.get("top"),
                    "x1": annot.get("x1"),
                    "bottom": annot.get("bottom"),
                    "width": annot.get("width"),
                    "height": annot.get("height"),
                    "rotate": (annot.get("data") or {}).get("Rotate"),
                }
                annotation_details.append(detail)
            pages.append(
                {
                    "page": index,
                    "lines": lines,
                    "annotations": annotations,
                    "annotation_details": annotation_details,
                }
            )
    return pages


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _render_with_ghostscript(
    pdf: Path,
    render_dir: Path,
    *,
    page_count: int,
    dpi: int = 600,
) -> list[str]:
    """Render every PDF page to PNG at the given DPI via ghostscript.

    600 DPI is the project default — required for accurate box counting, title
    spacing detection, and raised-numeral identification. Renders below ~400 DPI
    have been observed to mislead visual sweeps on tiny character-boxes
    (e.g., 3-vs-2 box widget calls on Initials, missed title double-spaces).
    """
    if page_count < 1:
        raise ValueError(f"cannot render PDF with page_count={page_count}: {pdf}")
    gs = shutil.which("gs") or shutil.which("gswin64c") or shutil.which("gswin32c")
    if not gs:
        raise RuntimeError("Ghostscript (gs) is required for exhaustive 600 DPI PDF renders")
    render_dir.mkdir(parents=True, exist_ok=True)
    for stale in render_dir.glob(f"{pdf.name}.page-*.png"):
        stale.unlink()
    old_page_one = render_dir / f"{pdf.name}.png"
    if old_page_one.exists():
        old_page_one.unlink()
    out_pattern = render_dir / f"{pdf.name}.page-%03d.png"
    cmd = [
        gs,
        "-dNOPAUSE",
        "-dBATCH",
        "-dQUIET",
        "-sDEVICE=png16m",
        f"-r{dpi}",
        "-dFirstPage=1",
        f"-dLastPage={page_count}",
        f"-sOutputFile={out_pattern}",
        str(pdf),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)  # noqa: S603
    renders = [render_dir / f"{pdf.name}.page-{page:03d}.png" for page in range(1, page_count + 1)]
    missing = [path for path in renders if not path.exists()]
    if missing:
        raise RuntimeError(f"Ghostscript did not render all pages for {pdf}: missing {missing!r}")
    return [str(path) for path in renders]


def _render_pdf(pdf: Path, render_dir: Path, *, page_count: int) -> list[str]:
    """Render every page via ghostscript at 600 DPI."""
    return _render_with_ghostscript(pdf, render_dir, page_count=page_count, dpi=600)


def _compact_pages(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for page in pages:
        lines = []
        for line in page.get("lines", []) or []:
            if isinstance(line, dict):
                text = str(line.get("text", "")).strip()
                if text:
                    lines.append(text)
            elif isinstance(line, str) and line.strip():
                lines.append(line.strip())
        compact.append(
            {
                "page": page.get("page"),
                "lines": lines,
                "annotations": page.get("annotations", []) or [],
                "annotation_details": page.get("annotation_details", []) or [],
            }
        )
    return compact


def _duplicates(values: list[str]) -> dict[str, int]:
    return {value: count for value, count in Counter(values).items() if count > 1}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--pdf", required=True, type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--render-dir", type=Path)
    parser.add_argument(
        "--run-dir",
        type=Path,
        help="Orchestrator run dir with header_extraction.json (Note 6); optional.",
    )
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    pdf = (repo_root / args.pdf).resolve() if not args.pdf.is_absolute() else args.pdf.resolve()
    dataset = (
        (repo_root / args.dataset).resolve()
        if not args.dataset.is_absolute()
        else args.dataset.resolve()
    )

    headers = _headers_via_repo(repo_root, dataset, run_dir=args.run_dir)
    pages = _pdf_pages_via_repo(repo_root, pdf)
    annotations: list[str] = []
    for page in pages:
        annotations.extend(page.get("annotations", []) or [])

    renders: list[str] = []
    if args.render_dir:
        renders = _render_pdf(pdf, args.render_dir.resolve(), page_count=len(pages))

    pack = {
        "source_boundary": "printed_pdf_plus_dataset_row_1_headers_only",
        "dataset_rows_read": "row_1_only",
        "pdf": os.path.relpath(pdf, repo_root),
        "pdf_sha256": _sha256_file(pdf),
        "dataset": os.path.relpath(dataset, repo_root),
        "headers": headers,
        "header_duplicates": _duplicates(headers),
        "annotation_duplicates": _duplicates(annotations),
        "page_count": len(pages),
        "renders": renders,
        "screenshot": renders[0] if renders else None,
        "pages": _compact_pages(pages),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(pack, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"source pack written: {args.out}")
    print(f"headers: {len(headers)}")
    for render in renders:
        print(f"render: {render}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
