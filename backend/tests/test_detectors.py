"""Unit tests for the deterministic rule detector (no Presidio, no Mongo, no HTTP).

Ported from the deleted synthetic-corpus benchmark: pins that ``rule_detect``
fires exactly once per HIPAA content-shape letter it carries, and that the
Luhn NPI validator accepts/rejects the expected checksums.
"""
from __future__ import annotations

import pytest
from phi_core.detectors import luhn, rule_detect

PROBES = {
    "B": "ZIP 94110",
    "C": "age 96",
    "D": "(415) 555-1234",
    "E": "fax: 415-555-1234",
    "F": "a.b@example.edu",
    "G": "123-45-6789",
    "H": "MRN-12345678",
    "K": "1234567893",
    "L": "1HGCM82633A004352",
    "M": "(01)12345678901234",
    "P": "fingerprint_template_12345678901",
    "Q": "patient_photo_123456.jpg",
    "R": "NCT12345678",
}


@pytest.mark.parametrize("letter", sorted(PROBES))
def test_rule_detect_fires_per_hipaa_letter(letter):
    hits = rule_detect(PROBES[letter])
    assert len(hits) >= 1, f"no hit for category {letter} on probe {PROBES[letter]!r}"
    assert any(h.hipaa_category == letter for h in hits), (
        f"expected category {letter}, got {[h.hipaa_category for h in hits]}"
    )


def test_rule_detect_negative_clinical_text():
    assert rule_detect("hemoglobin 13.4 glucose 105") == []


def test_luhn_npi_checksum():
    assert luhn("80840" + "1234567893") is True
    assert luhn("80840" + "1234567892") is False


def test_rule_detect_zip_label_is_case_insensitive():
    """Real dictionary/form text does not always capitalise the label
    ("zip: 86387", not just "ZIP 94110"); the detector must catch both."""
    for probe in ("zip: 86387", "Zip: 86387", "ZIP: 86387", "zip 86387"):
        hits = rule_detect(probe)
        assert any(h.hipaa_category == "B" for h in hits), f"no ZIP hit for {probe!r}"
