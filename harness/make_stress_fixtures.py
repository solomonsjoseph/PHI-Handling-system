"""Build an intentionally messy source tree for the standalone-pipeline stress
test (Phase 6). Corpus-side tooling -- freely imports ``generators/`` and
writes synthetic PHI values only (never real data).

    python -m harness.make_stress_fixtures --out tmp/stress-source [--seed 42]

Writes ``<out>/`` (the messy tree the stress test's ``intake_add`` consumes)
and ``<out>.manifest/stress_manifest.json`` (sha256 of every regular file
under ``<out>/`` at build time, consumed by ``harness/spec_check.py``'s
``source_immutability`` check -- the manifest deliberately lives OUTSIDE
``<out>/`` so it is never itself picked up by ``intake_add``'s walk).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from faker import Faker
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

MANIFEST_FILENAME = "stress_manifest.json"


def _fake(seed: int) -> Faker:
    fk = Faker()
    Faker.seed(seed)
    return fk


def _write_clean_crf_xlsx(path: Path, fk: Faker, n: int = 8) -> None:
    """A realistic CRF-shaped workbook: real column headers, multiple rows --
    exercises sheet_split.split_sheet_into_tables + promote_header properly
    (unlike a single free-text-column dump)."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Screening"
    ws.append(["SUBJID", "AGE", "SEX", "SITE_CODE"])
    for i in range(n):
        ws.append([f"CRF-{i:03d}", fk.random_int(18, 85), fk.random_element(["M", "F"]), f"SITE-{i % 3}"])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(path))


def _write_xlsxgenerator_sidecar(out_dir: Path, seed: int) -> Path | None:
    """Also exercise generators.file_formats.xlsx_gen.generate's own sidecar
    xlsx-writing path (corpus-generator API named in the stress-fixture plan).
    Returns one of the produced xlsx files, or None if openpyxl unavailable."""
    from generators.file_formats.xlsx_gen import generate as xlsx_generate

    sidecar_dir = out_dir / "_xlsxgen_sidecar"
    xlsx_generate(seed=seed, output_dir=sidecar_dir, n_per_tier_a=2)
    xlsx_files = sorted((sidecar_dir / "xlsx_files").glob("*.xlsx"))
    return xlsx_files[0] if xlsx_files else None


def _write_csv(path: Path, fk: Faker, n: int = 6) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["SUBJID,VISITDAT,WEIGHT_KG"]
    for i in range(n):
        lines.append(f"CSV-{i:03d},{fk.date_between(start_date='-2y').isoformat()},{fk.random_int(45, 95)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, fk: Faker, n: int = 6) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"SUBJID": f"JL-{i:03d}", "COLLDAT": fk.date_between(start_date="-1y").isoformat()}
        for i in range(n)
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _write_json_list(path: Path, fk: Faker, n: int = 4) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"SUBJID": f"JS-{i:03d}", "ANALYSIS_GROUP": fk.random_element(["A", "B"])} for i in range(n)]
    path.write_text(json.dumps(rows), encoding="utf-8")


def _write_xls(path: Path, fk: Faker) -> bool:
    """Genuine legacy .xls via xlwt when installable; returns False (caller
    ships a mislabeled fallback file instead) when xlwt is unavailable."""
    try:
        import xlwt
    except ImportError:
        return False
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Legacy")
    for col, header in enumerate(["SUBJID", "SITE_CODE"]):
        ws.write(0, col, header)
    for i in range(5):
        ws.write(i + 1, 0, f"XLS-{i:03d}")
        ws.write(i + 1, 1, f"SITE-{i % 2}")
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(path))
    return True


def _write_mislabeled_xls(path: Path) -> None:
    """A file with a .xls extension that is not real BIFF -- exercises the
    fail-closed 'unreadable/mislabeled .xls' review-bucket routing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"this is not a real xls workbook, just text bytes\n")


def _write_malformed_xlsx(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"PK\x03\x04not-a-real-zip-central-directory")


def _write_pdf_with_table(path: Path, fk: Faker) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(path), pagesize=letter)
    data = [["SUBJID", "RESULT"]] + [
        [f"PDF-{i:03d}", fk.random_element(["Negative", "Positive"])] for i in range(5)
    ]
    table = Table(data)
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 1, colors.black)]))
    doc.build([table])


def _write_annotated_crf_pdf(path: Path, dataset_stem: str) -> None:
    """A PDF whose stem matches an organized dataset's stem -- routes to the
    annotated_pdfs/ companion leg rather than table extraction. Content is a
    simple printed form (no bespoke annotation alignment is exercised here;
    see docs/AUDIT_REPORT note on the SoT producer's Indo-VAP-specific
    annotation tables)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=letter)
    c.drawString(72, 720, f"Annotated CRF for {dataset_stem}")
    c.drawString(72, 700, "Subject ID: ____________")
    c.save()


def _write_unknown_dat(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not a recognized structured format\n", encoding="utf-8")


def _write_phi_in_unexpected_columns(path: Path, fk: Faker, n: int = 6) -> list[dict[str, str]]:
    """SSN-shaped values under an innocuous 'NOTES' header, phone-shaped
    values under 'COMMENT' -- the value-profiler ESCALATION stress case.
    Returns the planted rows so the test suite can assert none of these
    exact values leak into published output."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for i in range(n):
        rows.append(
            {
                "SUBJID": f"PH-{i:03d}",
                "NOTES": fk.ssn(),
                "COMMENT": fk.numerify("###-###-####"),
            }
        )
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return rows


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_stress_fixtures(out_dir: Path, *, seed: int = 42) -> dict[str, Any]:
    """Build the messy source tree at *out_dir* and its immutability manifest.
    Idempotent: wipes and rebuilds *out_dir* every call."""
    fk = _fake(seed)

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    nested = out_dir / "batch_2026" / "site_04"
    nested.mkdir(parents=True)

    # -- clean, well-formed inputs -------------------------------------------
    _write_clean_crf_xlsx(nested / "1A_Screening.xlsx", fk)
    _write_csv(out_dir / "3_Labs.csv", fk)
    _write_jsonl(nested / "2_Demographics.jsonl", fk)
    _write_json_list(out_dir / "extra_group.json", fk)
    sidecar_xlsx = _write_xlsxgenerator_sidecar(out_dir, seed)

    # -- .xls: genuine if xlwt is installable, else a mislabeled fallback ----
    xls_path = out_dir / "legacy_site.xls"
    if not _write_xls(xls_path, fk):
        _write_mislabeled_xls(xls_path)

    # -- PDFs -----------------------------------------------------------------
    _write_pdf_with_table(out_dir / "lab_results_table.pdf", fk)
    _write_annotated_crf_pdf(nested / "1A_Screening.pdf", "1A_Screening")

    # -- fail-closed / review-bucket cases -------------------------------------
    _write_malformed_xlsx(out_dir / "corrupted_workbook.xlsx")
    _write_unknown_dat(out_dir / "mystery_export.dat")

    # -- PHI-in-unexpected-columns (value-profiler stress case) ---------------
    planted_unexpected_phi_rows = _write_phi_in_unexpected_columns(
        out_dir / "site_notes.jsonl", fk
    )

    # -- duplicates -------------------------------------------------------------
    dup_content = json.dumps({"SUBJID": "DUP-001", "SITE_CODE": "SITE-9"}) + "\n"
    dup_dir_a = out_dir / "dup_a"
    dup_dir_b = out_dir / "dup_b"
    dup_dir_a.mkdir()
    dup_dir_b.mkdir()
    (dup_dir_a / "roster.jsonl").write_text(dup_content, encoding="utf-8")
    (dup_dir_b / "roster.jsonl").write_text(dup_content, encoding="utf-8")  # same content, same name, sibling dirs
    (out_dir / "roster_copy.jsonl").write_text(dup_content, encoding="utf-8")  # same content, different name
    (dup_dir_b / "roster_conflict.jsonl").write_text(
        json.dumps({"SUBJID": "DUP-002", "SITE_CODE": "SITE-1"}) + "\n", encoding="utf-8"
    )
    # same NAME ("roster.jsonl"), DIFFERENT content, in a third sibling dir
    dup_dir_c = out_dir / "dup_c"
    dup_dir_c.mkdir()
    (dup_dir_c / "roster.jsonl").write_text(
        json.dumps({"SUBJID": "DUP-003", "SITE_CODE": "SITE-2"}) + "\n", encoding="utf-8"
    )

    # -- broken symlink inside the source --------------------------------------
    broken_link = out_dir / "vanished_file.jsonl"
    os.symlink(out_dir / "does_not_exist.jsonl", broken_link)

    # -- manifest (sha256 of every REGULAR file at build time; the broken ------
    # -- symlink itself has no target to hash, excluded) -----------------------
    files: dict[str, str] = {}
    for path in sorted(out_dir.rglob("*")):
        if path.is_dir():
            continue
        if path.is_symlink() and not path.exists():
            continue  # the deliberately-broken symlink; nothing to hash
        if not path.is_file():
            continue
        files[str(path.relative_to(out_dir))] = _sha256_file(path)

    manifest = {
        "source_root": str(out_dir.resolve()),
        "seed": seed,
        "files": files,
        "planted_unexpected_phi_rows": planted_unexpected_phi_rows,
        "planted_unexpected_phi_file": "site_notes.jsonl",
        "sidecar_xlsx": str(sidecar_xlsx.relative_to(out_dir)) if sidecar_xlsx else None,
    }
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="Output source directory")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--manifest-out",
        default=None,
        help="Manifest path (default: <out>.manifest/stress_manifest.json, outside --out)",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out).resolve()
    manifest = build_stress_fixtures(out_dir, seed=args.seed)

    manifest_path = (
        Path(args.manifest_out)
        if args.manifest_out
        else out_dir.parent / f"{out_dir.name}.manifest" / MANIFEST_FILENAME
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    print(f"stress source tree: {out_dir} ({len(manifest['files'])} files)")
    print(f"manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
