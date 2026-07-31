"""Study data element specialist agents.

Lexicon    - dictionary / mapping (xlsx / csv codebooks) specialist
Schema     - dataset (CSV/XLSX headers only, never rows)
Instrument - forms (PDF collection instruments)

Each specialist reads its assigned artifact and produces a normalised
per-column knowledge record consumed by Judge.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import openpyxl

try:
    from pypdf import PdfReader
except Exception:  # pragma: no cover
    PdfReader = None

from .base import Agent


class Lexicon(Agent):
    NAME = "Lexicon"
    PROMPT = (
        "You are Lexicon, a specialist in data dictionaries and code maps for clinical study "
        "datasets. Given the full text of a dictionary or mapping table, produce a JSON object: "
        '{"columns": [{"name": str, "description": str, "phi_flag_hint": bool|null, '
        '"clinical_utility": "low|medium|high", "notes": str}], "notes": str}. '
        "phi_flag_hint reflects only what the dictionary itself indicates, not your own judgement. "
        "Never invent columns. Cite 45 CFR 164.514 in notes when applicable."
    )

    async def run(self, dict_files: list[dict[str, Any]]) -> dict[str, Any]:
        aggregated: list[dict[str, Any]] = []
        for f in dict_files:
            text = _read_table_flat(Path(f["stored_path"]))
            reply = await self.call_json(
                f"Filename: {f['original_name']}\nComponent: {f.get('component')}\n"
                f"Full content (rows are metadata about a schema; no patient PHI expected):\n{text[:8000]}\n"
                "Respond with JSON only.",
                phase=f"lexicon.read:{f['file_id']}",
                default={"columns": [], "notes": ""},
            )
            aggregated.extend(reply.get("columns", []))
        return {"columns": aggregated}


class Schema(Agent):
    NAME = "Schema"
    PROMPT = (
        "You are Schema, a specialist in reading DATASET COLUMN HEADERS ONLY. You must NEVER "
        "receive or infer row values. Given a list of column headers and optional dictionary "
        "context, produce JSON: "
        '{"columns": [{"name": str, "inferred_meaning": str, "candidate_phi_category": '
        '"A|B|C|D|E|F|G|H|I|J|K|L|M|N|O|P|Q|R|QUASI|NONE", "confidence": 0..1}]}. '
        "Use HIPAA Safe Harbor 45 CFR 164.514(b)(2)(i) categories A-R."
    )

    async def run(self, dataset_files: list[dict[str, Any]], lexicon_columns: list[dict[str, Any]]) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        lex_map = {c.get("name", "").lower(): c for c in lexicon_columns}
        for f in dataset_files:
            headers = f.get("columns", [])
            if not headers:
                # Fail loud instead of hallucinating - orchestrator must have populated columns before us.
                await self._log(f"schema.error:{f['file_id']}", "info",
                                {"error": "no headers provided", "file": f.get("original_name")})
                continue
            # Enrich each header with any dictionary hint (no row values ever sent)
            enrichment = []
            for h in headers:
                lex = lex_map.get(h.lower())
                if lex:
                    enrichment.append({"name": h, "dict_hint": lex.get("description", ""), "phi_flag_hint": lex.get("phi_flag_hint")})
                else:
                    enrichment.append({"name": h})
            reply = await self.call_json(
                f"Dataset: {f['original_name']} (rows are withheld; you only see headers).\n"
                f"Enriched headers: {enrichment}\n"
                "Respond with JSON only.",
                phase=f"schema.classify:{f['file_id']}",
                default={"columns": []},
            )
            for c in reply.get("columns", []):
                c["_file_id"] = f["file_id"]
            results.extend(reply.get("columns", []))
        return {"columns": results}


class Instrument(Agent):
    NAME = "Instrument"
    PROMPT = (
        "You are Instrument, a specialist in PDF data-collection forms used in clinical studies. "
        "Given the extracted text of a form, identify which fields collect PHI and their categories. "
        "Return JSON: "
        '{"fields": [{"label": str, "collected_variable": str|null, '
        '"phi_category": "A|B|C|D|E|F|G|H|I|J|K|L|M|N|O|P|Q|R|QUASI|NONE"}]}. '
        "Cite the exact form section text in `context` if helpful."
    )

    async def run(self, form_files: list[dict[str, Any]]) -> dict[str, Any]:
        aggregated: list[dict[str, Any]] = []
        for f in form_files:
            path = Path(f["stored_path"])
            text = ""
            if PdfReader is not None and path.suffix.lower() == ".pdf":
                try:
                    reader = PdfReader(str(path))
                    text = "\n\n".join((p.extract_text() or "") for p in reader.pages)
                except Exception:
                    text = ""
            reply = await self.call_json(
                f"Form: {f['original_name']}\nExtracted text (first 6000 chars):\n{text[:6000]}\n"
                "Respond with JSON only.",
                phase=f"instrument.read:{f['file_id']}",
                default={"fields": []},
            )
            aggregated.extend(reply.get("fields", []))
        return {"fields": aggregated}


# --- deterministic helpers ------------------------------------------------


_DOCX_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _read_docx_tables(path: Path) -> str:
    """Extract every table from a .docx file as CSV-shaped text.

    A .docx is a ZIP that stores the document body at ``word/document.xml``.
    We walk that XML and pull every ``<w:tbl>`` element out row-by-row so
    the LLM sees a flat, header + rows shape that mirrors what it would
    see for a CSV dictionary. Non-table paragraphs (title, intro prose)
    are concatenated after the tables so the LLM still gets any framing
    text the data steward may have written above the table.
    """
    import xml.etree.ElementTree as ET
    import zipfile as _zip

    lines: list[str] = []
    prose: list[str] = []
    try:
        with _zip.ZipFile(path) as z:
            with z.open("word/document.xml") as f:
                tree = ET.parse(f)
    except (KeyError, _zip.BadZipFile, ET.ParseError):
        return ""

    root = tree.getroot()
    body = root.find(f"{_DOCX_W_NS}body")
    if body is None:
        return ""

    def _cell_text(cell: ET.Element) -> str:
        parts = [t.text or "" for t in cell.iter(f"{_DOCX_W_NS}t")]
        return " ".join(x for x in parts if x).strip()

    table_index = 0
    for child in body:
        tag = child.tag
        if tag == f"{_DOCX_W_NS}p":
            # paragraph text -- capture short framing prose only
            text = " ".join((t.text or "") for t in child.iter(f"{_DOCX_W_NS}t")).strip()
            if text:
                prose.append(text)
        elif tag == f"{_DOCX_W_NS}tbl":
            table_index += 1
            lines.append(f"# table {table_index}")
            for tr in child.iter(f"{_DOCX_W_NS}tr"):
                cells = [_cell_text(tc) for tc in tr.iter(f"{_DOCX_W_NS}tc")]
                # emit CSV-shaped row; escape any embedded commas
                lines.append(",".join(
                    '"' + c.replace('"', '""') + '"' if ("," in c or '"' in c) else c
                    for c in cells
                ))
    if prose:
        lines.append("# narrative context")
        lines.extend(prose[:40])   # cap at 40 paragraphs to keep prompt bounded
    return "\n".join(lines)


def _read_xls_tables(path: Path) -> str:
    """Extract .xls (legacy Excel) sheets. Uses openpyxl if the file is
    actually .xlsx-shaped (some data stewards mis-rename), otherwise
    falls back to xlrd where available. Returns empty on unsupported
    binary .xls; caller treats absence as "no dictionary text"."""
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception:
        try:
            import xlrd  # type: ignore
        except ImportError:
            return ""
        try:
            book = xlrd.open_workbook(str(path))
        except Exception:
            return ""
        lines: list[str] = []
        for sh in book.sheets():
            lines.append(f"# sheet: {sh.name}")
            for r in range(sh.nrows):
                lines.append(",".join(str(sh.cell_value(r, c)) for c in range(sh.ncols)))
        return "\n".join(lines)
    lines = []
    for ws in wb.worksheets:
        lines.append(f"# sheet: {ws.title}")
        for row in ws.iter_rows(values_only=True):
            lines.append(",".join("" if v is None else str(v) for v in row))
    wb.close()
    return "\n".join(lines)


def _read_table_flat(path: Path) -> str:
    """Flatten a small dictionary/mapping table to text for the LLM.

    Supports .csv/.tsv (raw read), .xlsx (openpyxl), .xls (openpyxl for
    mis-named files, xlrd for real BIFF), and .docx (Word tables via
    stdlib zipfile + ET). Sir Q "dictionary is word doc here but if
    large then it would be excel workbook -- align it accordingly".
    """
    ext = path.suffix.lower()
    if ext in {".csv", ".tsv"}:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""
    if ext == ".xlsx":
        try:
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            lines = []
            for ws in wb.worksheets:
                lines.append(f"# sheet: {ws.title}")
                for row in ws.iter_rows(values_only=True):
                    lines.append(",".join("" if v is None else str(v) for v in row))
            wb.close()
            return "\n".join(lines)
        except Exception:
            return ""
    if ext == ".xls":
        return _read_xls_tables(path)
    if ext == ".docx":
        return _read_docx_tables(path)
    return ""
