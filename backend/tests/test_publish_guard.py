"""Publish Guard tests — the boundary between 'input PHI data' and
'output ready to share publicly'."""
from pathlib import Path

from phi_core.publish_guard import (
    GuardReport, GuardResult, MAX_FINDINGS_PER_FILE,
    scan_all_exports, scan_export_file,
)


def _write_csv(tmp_path: Path, name: str, rows: list[list[str]]) -> Path:
    p = tmp_path / name
    import csv as _csv
    with p.open("w", newline="") as f:
        w = _csv.writer(f)
        for r in rows:
            w.writerow(r)
    return p


# ---------- CLEAN cases (must return 'clean') -----------------------------

def test_clean_export_passes(tmp_path: Path):
    p = _write_csv(tmp_path, "enrollment.csv", [
        ["patient_id", "dob", "zip", "age", "notes"],
        ["Pd7c5f7f2", "1975", "941", "50", "Enrolled at [A]. Call [D]."],
        ["Pa4d648bd", "1982", "941", "43", "Screened; contact [A] via [D]."],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "clean", r


def test_header_row_names_do_not_trip_guard(tmp_path: Path):
    # A column literally named 'phone_number' must NOT count as a phone match.
    p = _write_csv(tmp_path, "meta.csv", [
        ["phone_number", "email", "ssn"],
        ["[D]", "[F]", "[G]"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "clean"


def test_pdf_extension_is_skipped(tmp_path: Path):
    p = tmp_path / "consent.pdf"
    p.write_bytes(b"%PDF-1.4\n")
    r = scan_export_file("f1", p)
    assert r.status == "skipped"


# ---------- BLOCKED cases (must return 'blocked' with findings) ------------

def test_ssn_leak_blocks_export(tmp_path: Path):
    p = _write_csv(tmp_path, "leaky.csv", [
        ["name", "ssn"],
        ["[A]", "111-22-3333"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "blocked"
    assert any(f["pattern_id"] == "SSN" for f in r.findings)


def test_phone_leak_blocks_export(tmp_path: Path):
    p = _write_csv(tmp_path, "leaky.csv", [
        ["name", "phone"],
        ["[A]", "415-555-1234"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "blocked"
    assert any(f["pattern_id"] == "PHONE_US" for f in r.findings)


def test_email_leak_blocks_export(tmp_path: Path):
    p = _write_csv(tmp_path, "leaky.csv", [
        ["name", "email"],
        ["[A]", "james.smith@example.edu"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "blocked"
    assert any(f["pattern_id"] == "EMAIL" for f in r.findings)


def test_full_dob_iso_blocks_export(tmp_path: Path):
    p = _write_csv(tmp_path, "leaky.csv", [
        ["patient_id", "dob"],
        ["Pd7c5f7f2", "1975-03-15"],  # should have been year-only "1975"
    ])
    r = scan_export_file("f1", p)
    assert r.status == "blocked"
    assert any(f["pattern_id"] == "DATE_FULL_ISO" for f in r.findings)


def test_full_dob_us_slash_blocks(tmp_path: Path):
    p = _write_csv(tmp_path, "leaky.csv", [
        ["patient_id", "dob"],
        ["Pa4d648bd", "07/22/1982"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "blocked"
    assert any(f["pattern_id"] == "DATE_FULL_US" for f in r.findings)


def test_findings_are_masked_not_raw(tmp_path: Path):
    p = _write_csv(tmp_path, "leaky.csv", [
        ["name", "email"],
        ["[A]", "james.smith@example.edu"],
    ])
    r = scan_export_file("f1", p)
    for f in r.findings:
        # The masked sample must not equal the original email
        assert f["sample"] != "james.smith@example.edu"


def test_findings_bounded(tmp_path: Path):
    rows = [["email"]] + [[f"user{i}@example.com"] for i in range(200)]
    p = _write_csv(tmp_path, "many.csv", rows)
    r = scan_export_file("f1", p)
    assert r.status == "blocked"
    assert len(r.findings) <= MAX_FINDINGS_PER_FILE


# ---------- Aggregate scan -----------------------------------------------

def test_scan_all_exports_mixes_clean_and_blocked(tmp_path: Path):
    good = _write_csv(tmp_path, "good.csv", [["a"], ["ok"]])
    bad = _write_csv(tmp_path, "bad.csv", [["ssn"], ["111-22-3333"]])
    rep = scan_all_exports({"f_good": str(good), "f_bad": str(bad)})
    assert rep.status == "blocked"
    assert rep.scanned == 2
    assert rep.blocked == 1


def test_scan_all_empty_export_map():
    rep = scan_all_exports({})
    assert rep.status == "clean"
    assert rep.scanned == 0
    assert rep.blocked == 0
