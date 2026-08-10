"""Publish Guard tests — the boundary between 'input PHI data' and
'output ready to share publicly'."""
from pathlib import Path

from phi_core.publish_guard import (
    MAX_FINDINGS_PER_FILE,
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


# ---------- Phase B parity: every HIPAA A-R has a guard pattern ----------

def test_url_leak_blocks_export_category_N(tmp_path: Path):
    p = _write_csv(tmp_path, "leaky.csv", [
        ["patient_id", "notes"],
        ["Px1", "See portal at https://portal.example.edu/patient/12345"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "blocked"
    assert any(f["pattern_id"] == "URL" for f in r.findings)


def test_ipv4_leak_blocks_export_category_O(tmp_path: Path):
    p = _write_csv(tmp_path, "leaky.csv", [
        ["patient_id", "ip"],
        ["Px1", "192.168.1.42"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "blocked"
    assert any(f["pattern_id"] == "IPV4" for f in r.findings)


def test_ipv6_leak_blocks_export_category_O(tmp_path: Path):
    p = _write_csv(tmp_path, "leaky.csv", [
        ["patient_id", "ip"],
        ["Px1", "2001:0db8:85a3:0000:0000:8a2e:0370:7334"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "blocked"
    assert any(f["pattern_id"] == "IPV6" for f in r.findings)


def test_license_plate_blocks_export_category_L(tmp_path: Path):
    p = _write_csv(tmp_path, "leaky.csv", [
        ["patient_id", "vehicle"],
        ["Px1", "ABC 1234"],
    ])
    r = scan_export_file("f1", p, column_categories={"vehicle": "L"})
    assert r.status == "blocked"
    assert any(f["pattern_id"] == "LICENSE_PLATE" for f in r.findings)


def test_license_plate_ignored_without_column_or_anchor(tmp_path: Path):
    """Regulator-defensible: a study arm code like 'ARM 001' or 'HB 120'
    must NOT fire LICENSE_PLATE without column semantics or anchor."""
    p = _write_csv(tmp_path, "clean.csv", [
        ["patient_id", "arm_code"],
        ["Px1", "ARM 001"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "clean"


def test_imei_blocks_export_category_M(tmp_path: Path):
    p = _write_csv(tmp_path, "leaky.csv", [
        ["patient_id", "device_imei"],
        ["Px1", "490154203237518"],
    ])
    r = scan_export_file("f1", p, column_categories={"device_imei": "M"})
    assert r.status == "blocked"
    assert any(f["pattern_id"] == "IMEI" for f in r.findings)


def test_imei_ignored_without_column_or_anchor(tmp_path: Path):
    """Long numeric barcodes must not trip IMEI without column context."""
    p = _write_csv(tmp_path, "clean.csv", [
        ["patient_id", "barcode"],
        ["Px1", "490154203237518"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "clean"


def test_device_serial_blocks_export_category_M(tmp_path: Path):
    p = _write_csv(tmp_path, "leaky.csv", [
        ["patient_id", "device"],
        ["Px1", "SN-ABCD-1234"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "blocked"
    assert any(f["pattern_id"] == "DEVICE_SERIAL" for f in r.findings)


def test_image_reference_blocks_export_category_Q(tmp_path: Path):
    p = _write_csv(tmp_path, "leaky.csv", [
        ["patient_id", "photo"],
        ["Px1", "patient_face_0001.jpg"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "blocked"
    assert any(f["pattern_id"] == "IMAGE_REF" for f in r.findings)


def test_biometric_hash_blocks_export_category_P(tmp_path: Path):
    p = _write_csv(tmp_path, "leaky.csv", [
        ["patient_id", "biometric"],
        ["Px1", "fingerprint: a3f5b7c9d1e2f4a6b8c0d2e4f6a8b0c2"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "blocked"
    assert any(f["pattern_id"] == "BIOMETRIC_HASH" for f in r.findings)


def test_dna_profile_blocks_export_category_P(tmp_path: Path):
    p = _write_csv(tmp_path, "leaky.csv", [
        ["patient_id", "genetics"],
        ["Px1", "DNA profile: A1B2C3D4E5F6"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "blocked"
    assert any(f["pattern_id"] == "DNA_PROFILE" for f in r.findings)


def test_npi_blocks_export_category_K(tmp_path: Path):
    p = _write_csv(tmp_path, "leaky.csv", [
        ["patient_id", "provider"],
        ["Px1", "NPI: 1234567893"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "blocked"
    assert any(f["pattern_id"] == "NPI" for f in r.findings)


def test_dea_blocks_export_category_K(tmp_path: Path):
    p = _write_csv(tmp_path, "leaky.csv", [
        ["patient_id", "provider"],
        ["Px1", "DEA: BJ1234567"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "blocked"
    assert any(f["pattern_id"] == "DEA" for f in r.findings)


def test_fax_label_blocks_export_category_E(tmp_path: Path):
    p = _write_csv(tmp_path, "leaky.csv", [
        ["patient_id", "notes"],
        ["Px1", "fax: 415-555-1234"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "blocked"
    assert any(f["pattern_id"] == "FAX_LABEL" for f in r.findings)


def test_mrn_blocks_export_category_H(tmp_path: Path):
    p = _write_csv(tmp_path, "leaky.csv", [
        ["patient_id", "notes"],
        ["Px1", "MRN-12345678"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "blocked"
    assert any(f["pattern_id"] == "MRN" for f in r.findings)


def test_health_plan_id_blocks_export_category_I(tmp_path: Path):
    p = _write_csv(tmp_path, "leaky.csv", [
        ["patient_id", "notes"],
        ["Px1", "Member ID: MID12345"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "blocked"
    assert any(f["pattern_id"] == "HEALTH_PLAN_ID" for f in r.findings)


def test_mbi_blocks_export_category_I(tmp_path: Path):
    p = _write_csv(tmp_path, "leaky.csv", [
        ["patient_id", "notes"],
        ["Px1", "1EG4TE5MK73"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "blocked"
    assert any(f["pattern_id"] == "MBI" for f in r.findings)


def test_account_blocks_export_category_J(tmp_path: Path):
    p = _write_csv(tmp_path, "leaky.csv", [
        ["patient_id", "notes"],
        ["Px1", "Account: ACC123456"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "blocked"
    assert any(f["pattern_id"] == "ACCOUNT" for f in r.findings)


def test_npi_bare_blocks_export_category_K(tmp_path: Path):
    p = _write_csv(tmp_path, "leaky.csv", [
        ["patient_id", "provider_npi"],
        ["Px1", "1234567893"],
    ])
    r = scan_export_file("f1", p, column_categories={"provider_npi": "K"})
    assert r.status == "blocked"
    assert any(f["pattern_id"] == "NPI_BARE" for f in r.findings)


def test_npi_bare_ignored_without_column_or_anchor(tmp_path: Path):
    """A bare 10-digit number without an NPI column or anchor must not
    trip NPI_BARE specifically. (PHONE_US may separately flag the same
    digits as a possible phone number; that is correct and unrelated.)"""
    p = _write_csv(tmp_path, "clean.csv", [
        ["patient_id", "code"],
        ["Px1", "1234567893"],
    ])
    r = scan_export_file("f1", p)
    assert not any(f["pattern_id"] == "NPI_BARE" for f in r.findings)


def test_npi_bare_ignored_when_luhn_invalid(tmp_path: Path):
    """A 10-digit value in an NPI-typed column that fails the Luhn check
    against the 80840 issuer prefix must not trip NPI_BARE. (PHONE_US may
    separately flag the same digits; that is correct and unrelated.)"""
    p = _write_csv(tmp_path, "clean.csv", [
        ["patient_id", "provider_npi"],
        ["Px1", "1234567892"],
    ])
    r = scan_export_file("f1", p, column_categories={"provider_npi": "K"})
    assert not any(f["pattern_id"] == "NPI_BARE" for f in r.findings)


def test_vin_blocks_export_category_L(tmp_path: Path):
    p = _write_csv(tmp_path, "leaky.csv", [
        ["patient_id", "vehicle"],
        ["Px1", "1HGCM82633A004352"],
    ])
    r = scan_export_file("f1", p, column_categories={"vehicle": "L"})
    assert r.status == "blocked"
    assert any(f["pattern_id"] == "VIN" for f in r.findings)


def test_device_udi_blocks_export_category_M(tmp_path: Path):
    p = _write_csv(tmp_path, "leaky.csv", [
        ["patient_id", "device"],
        ["Px1", "(01)12345678901234"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "blocked"
    assert any(f["pattern_id"] == "DEVICE_UDI" for f in r.findings)


def test_unique_code_blocks_export_category_R(tmp_path: Path):
    p = _write_csv(tmp_path, "leaky.csv", [
        ["patient_id", "notes"],
        ["Px1", "Tracking Code: TRK123456"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "blocked"
    assert any(f["pattern_id"] == "UNIQUE_CODE" for f in r.findings)


def test_clinical_trial_id_blocks_export_category_R(tmp_path: Path):
    p = _write_csv(tmp_path, "leaky.csv", [
        ["patient_id", "notes"],
        ["Px1", "enrolled under NCT12345678"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "blocked"
    assert any(f["pattern_id"] == "CLINICAL_TRIAL_ID" for f in r.findings)


def test_step5_patterns_do_not_false_positive_on_common_dictionary_prose(tmp_path: Path):
    """Labeled-id patterns require a digit after the label; plain prose,
    pseudonyms, and hash output must not block a clean export."""
    p = _write_csv(tmp_path, "clean.csv", [
        ["patient_id", "notes"],
        ["Px1", "Account Number required"],
        ["Px2", "Chart ID pending"],
        ["Px3", "MRN of record"],
        ["Px4", "P3a4c96db"],
        ["Px5", "a1b2c3d4e5f6a7b8"],
        ["Px6", "ARM 001"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "clean", r


def test_every_hipaa_letter_has_at_least_one_pattern():
    """Phase B acceptance gate: every HIPAA letter B-R must be represented
    in the guard pattern table. A is the only uncovered letter: names have
    no content shape and are handled by the hard-rule table plus Presidio
    ``PERSON``, not by a Publish Guard regex."""
    from phi_core.jurisdictions import get_pack
    covered = {p.category for p in get_pack("us").patterns}
    for expected in "BCDEFGHIJKLMNOPQR":
        assert expected in covered, f"HIPAA category {expected} missing from Publish Guard"


# ---------- AGE_OVER_89 must not false-positive on pseudonyms ----------

def test_age_over_89_does_not_false_positive_on_pseudonym(tmp_path: Path):
    """Regression: `P3a4c96db`-style pseudonyms carry hex bigrams like
    "96" that used to trip the AGE_OVER_89 guard. With word-boundary anchor
    the guard must ignore them."""
    p = _write_csv(tmp_path, "clean.csv", [
        ["patient_id", "age"],
        ["P3a4c96db", "50"],   # "96" inside hex pseudonym is not an age
        ["Pab262286", "35"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "clean", r


def test_age_over_89_still_catches_real_age(tmp_path: Path):
    """Positive: a standalone age >=90 in an age-classified column must
    still be flagged (HIPAA §164.514(b)(2)(i)(C))."""
    p = _write_csv(tmp_path, "leaky.csv", [
        ["patient_id", "age"],
        ["Pxxx", "95"],   # real age > 89 that was NOT capped
    ])
    r = scan_export_file("f1", p, column_categories={"age": "C"})
    assert r.status == "blocked"
    assert any(f["pattern_id"] == "AGE_OVER_89" for f in r.findings)


def test_age_over_89_ignored_on_clinical_measurement(tmp_path: Path):
    """CR-HIGH regression: a heart rate of 95 or systolic BP of 92 in
    a NON-age column must NOT trip AGE_OVER_89. HIPAA regulates the
    age identifier, not the numeric value 90-99."""
    p = _write_csv(tmp_path, "clean.csv", [
        ["patient_id", "heart_rate_bpm", "systolic_bp"],
        ["Px1", "95", "92"],
        ["Px2", "98", "90"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "clean", r


def test_age_anchor_in_free_text_still_fires(tmp_path: Path):
    """In-cell anchor fallback: notes column carrying 'aged 95' must
    still trip AGE_OVER_89 even without column semantics."""
    p = _write_csv(tmp_path, "leaky.csv", [
        ["patient_id", "notes"],
        ["Px1", "Patient aged 95 admitted with chest pain"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "blocked"
    assert any(f["pattern_id"] == "AGE_OVER_89" for f in r.findings)


def test_age_over_89_ignores_cap_output(tmp_path: Path):
    """`90+` is the correct Safe Harbor output for age > 89 and must not
    itself be flagged."""
    p = _write_csv(tmp_path, "clean.csv", [
        ["patient_id", "age"],
        ["Pxxx", "90+"],
    ])
    r = scan_export_file("f1", p)
    assert r.status == "clean", r
