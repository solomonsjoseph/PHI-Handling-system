"""
Tests for Uganda DPPA 2019 and Australia Privacy Act 1988 generators.

Covers format validation, span verification, jurisdiction tags, seed
reproducibility, authority citations, and corpus file output.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from generators.ug.ug_dppa import UgandaDPPAGenerator, generate_corpus as ug_generate_corpus
from generators.au.au_privacy import (
    AustraliaPrivacyGenerator,
    generate_corpus as au_generate_corpus,
    _medicare_checksum,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ug_records():
    gen = UgandaDPPAGenerator(seed=42)
    return gen.generate_batch(count_per_type=4)


@pytest.fixture(scope="module")
def au_records():
    gen = AustraliaPrivacyGenerator(seed=42)
    return gen.generate_batch(count_per_type=4)


# ---------------------------------------------------------------------------
# Uganda tests
# ---------------------------------------------------------------------------

def test_uganda_national_id_format(ug_records):
    """NATIONAL_ID_UG must be 14 chars starting with CM (NIRA format)."""
    national_ids = [
        span.value
        for rec in ug_records
        for span in rec.gold_spans
        if span.category == "NATIONAL_ID_UG"
    ]
    assert len(national_ids) > 0, "No NATIONAL_ID_UG spans found"
    pattern = re.compile(r"^CM[A-Z0-9]{12}$")
    for nid in national_ids:
        assert len(nid) == 14, f"NATIONAL_ID_UG length != 14: {nid}"
        assert pattern.match(nid), f"NATIONAL_ID_UG format invalid: {nid}"


def test_uganda_phone_format(ug_records):
    """PHONE_UG must start with 0[37] and be 10 digits total."""
    phones = [
        span.value
        for rec in ug_records
        for span in rec.gold_spans
        if span.category == "PHONE_UG"
    ]
    assert len(phones) > 0, "No PHONE_UG spans found"
    pattern = re.compile(r"^0[37][0-9]{8}$")
    for phone in phones:
        assert pattern.match(phone), f"PHONE_UG format invalid: {phone}"


def test_uganda_tin_format(ug_records):
    """TIN_UG must be exactly 10 decimal digits."""
    tins = [
        span.value
        for rec in ug_records
        for span in rec.gold_spans
        if span.category == "TIN_UG"
    ]
    assert len(tins) > 0, "No TIN_UG spans found"
    for tin in tins:
        assert len(tin) == 10, f"TIN_UG length != 10: {tin}"
        assert tin.isdigit(), f"TIN_UG non-numeric: {tin}"


def test_uganda_nssf_format(ug_records):
    """NSSF_NUMBER must be exactly 9 decimal digits."""
    nssf_nums = [
        span.value
        for rec in ug_records
        for span in rec.gold_spans
        if span.category == "NSSF_NUMBER"
    ]
    assert len(nssf_nums) > 0, "No NSSF_NUMBER spans found"
    for nssf in nssf_nums:
        assert len(nssf) == 9, f"NSSF_NUMBER length != 9: {nssf}"
        assert nssf.isdigit(), f"NSSF_NUMBER non-numeric: {nssf}"


def test_uganda_passport_format(ug_records):
    """PASSPORT_UG must match [A-Z][0-9]{8}."""
    passports = [
        span.value
        for rec in ug_records
        for span in rec.gold_spans
        if span.category == "PASSPORT_UG"
    ]
    assert len(passports) > 0, "No PASSPORT_UG spans found"
    pattern = re.compile(r"^[A-Z][0-9]{8}$")
    for p in passports:
        assert pattern.match(p), f"PASSPORT_UG format invalid: {p}"


def test_uganda_spans_all_verified(ug_records):
    """All Uganda record spans must pass verify_spans() with no errors."""
    for rec in ug_records:
        errors = rec.verify_spans()
        assert errors == [], f"Span errors in {rec.record_id}: {errors}"


def test_uganda_jurisdiction(ug_records):
    """All Uganda records must have jurisdiction == 'ug'."""
    for rec in ug_records:
        assert rec.jurisdiction == "ug", (
            f"Record {rec.record_id} has jurisdiction '{rec.jurisdiction}', expected 'ug'"
        )


def test_uganda_authority_citations(ug_records):
    """All Uganda records must have at least one authority_citation."""
    for rec in ug_records:
        assert len(rec.authority_citations) >= 1, (
            f"Record {rec.record_id} has no authority_citations"
        )
        # Must always include the primary DPPA authority
        assert any("DPPA" in c or "Data Protection" in c for c in rec.authority_citations), (
            f"Record {rec.record_id} missing DPPA citation"
        )


def test_uganda_seed_reproducibility():
    """Two generators with the same seed must produce bitwise-identical batches."""
    g1 = UgandaDPPAGenerator(seed=7)
    g2 = UgandaDPPAGenerator(seed=7)
    batch1 = g1.generate_batch(count_per_type=2)
    batch2 = g2.generate_batch(count_per_type=2)
    assert len(batch1) == len(batch2)
    for r1, r2 in zip(batch1, batch2):
        assert r1.text == r2.text, "Text differs between same-seed generators"
        assert r1.record_id == r2.record_id


# ---------------------------------------------------------------------------
# Australia tests
# ---------------------------------------------------------------------------

def test_australia_medicare_format(au_records):
    """MEDICARE_NUMBER must be exactly 10 digits with valid checksum."""
    numbers = [
        span.value
        for rec in au_records
        for span in rec.gold_spans
        if span.category == "MEDICARE_NUMBER"
    ]
    assert len(numbers) > 0, "No MEDICARE_NUMBER spans found"
    for m in numbers:
        assert len(m) == 10, f"MEDICARE_NUMBER length != 10: {m}"
        assert m.isdigit(), f"MEDICARE_NUMBER non-numeric: {m}"
        computed = _medicare_checksum(m[:8])
        assert computed == m[8], (
            f"MEDICARE_NUMBER checksum failed: digits={m[:8]}, "
            f"expected check={computed}, got {m[8]}"
        )


def test_australia_ihi_format(au_records):
    """IHI must be 16 digits starting with '80'."""
    ihis = [
        span.value
        for rec in au_records
        for span in rec.gold_spans
        if span.category == "IHI"
    ]
    assert len(ihis) > 0, "No IHI spans found"
    for ihi in ihis:
        assert len(ihi) == 16, f"IHI length != 16: {ihi}"
        assert ihi.isdigit(), f"IHI non-numeric: {ihi}"
        assert ihi.startswith("80"), f"IHI does not start with '80': {ihi}"


def test_australia_tfn_format(au_records):
    """TFN must be exactly 9 digits."""
    tfns = [
        span.value
        for rec in au_records
        for span in rec.gold_spans
        if span.category == "TFN"
    ]
    assert len(tfns) > 0, "No TFN spans found"
    for tfn in tfns:
        assert len(tfn) == 9, f"TFN length != 9: {tfn}"
        assert tfn.isdigit(), f"TFN non-numeric: {tfn}"


def test_australia_dva_format(au_records):
    """DVA_FILE must match [NQV][0-9]{6}[A-Z]."""
    dvas = [
        span.value
        for rec in au_records
        for span in rec.gold_spans
        if span.category == "DVA_FILE"
    ]
    assert len(dvas) > 0, "No DVA_FILE spans found"
    pattern = re.compile(r"^[NQV][0-9]{6}[A-Z]$")
    for dva in dvas:
        assert pattern.match(dva), f"DVA_FILE format invalid: {dva}"


def test_australia_spans_all_verified(au_records):
    """All Australia record spans must pass verify_spans() with no errors."""
    for rec in au_records:
        errors = rec.verify_spans()
        assert errors == [], f"Span errors in {rec.record_id}: {errors}"


def test_australia_jurisdiction(au_records):
    """All Australia records must have jurisdiction == 'au'."""
    for rec in au_records:
        assert rec.jurisdiction == "au", (
            f"Record {rec.record_id} has jurisdiction '{rec.jurisdiction}', expected 'au'"
        )


def test_australia_authority_citations(au_records):
    """All Australia records must have at least one authority_citation."""
    for rec in au_records:
        assert len(rec.authority_citations) >= 1, (
            f"Record {rec.record_id} has no authority_citations"
        )
        assert any("Privacy Act" in c or "Healthcare Identifiers" in c or
                   "Health Insurance Act" in c or "Veterans" in c or
                   "Passports Act" in c or "Tax File" in c or
                   "Income Tax" in c or "Road Transport" in c
                   for c in rec.authority_citations), (
            f"Record {rec.record_id} missing expected authority citation"
        )


def test_australia_seed_reproducibility():
    """Two generators with the same seed must produce bitwise-identical batches."""
    g1 = AustraliaPrivacyGenerator(seed=99)
    g2 = AustraliaPrivacyGenerator(seed=99)
    batch1 = g1.generate_batch(count_per_type=2)
    batch2 = g2.generate_batch(count_per_type=2)
    assert len(batch1) == len(batch2)
    for r1, r2 in zip(batch1, batch2):
        assert r1.text == r2.text
        assert r1.record_id == r2.record_id


# ---------------------------------------------------------------------------
# Corpus file output tests
# ---------------------------------------------------------------------------

def test_uganda_generate_corpus_writes_file(tmp_path, monkeypatch):
    """generate_corpus() for Uganda must write a valid JSONL file."""
    out_file = tmp_path / "ug" / "uganda_identifiers.jsonl"
    out_file.parent.mkdir(parents=True)

    import generators.ug.ug_dppa as ug_mod
    from generators.common import write_jsonl

    original_write = write_jsonl

    def patched_write(records, path):
        return original_write(records, out_file)

    monkeypatch.setattr(ug_mod, "write_jsonl", patched_write)
    count = ug_mod.generate_corpus(seed=42)
    assert count == 28, f"Expected 28 Uganda records, got {count}"
    lines = out_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 28
    first = json.loads(lines[0])
    assert first["jurisdiction"] == "ug"
    assert "authority_citations" in first


def test_australia_generate_corpus_writes_file(tmp_path, monkeypatch):
    """generate_corpus() for Australia must write a valid JSONL file."""
    out_file = tmp_path / "au" / "australia_identifiers.jsonl"
    out_file.parent.mkdir(parents=True)

    import generators.au.au_privacy as au_mod
    from generators.common import write_jsonl

    original_write = write_jsonl

    def patched_write(records, path):
        return original_write(records, out_file)

    monkeypatch.setattr(au_mod, "write_jsonl", patched_write)
    count = au_mod.generate_corpus(seed=42)
    assert count == 28, f"Expected 28 Australia records, got {count}"
    lines = out_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 28
    first = json.loads(lines[0])
    assert first["jurisdiction"] == "au"
    assert "authority_citations" in first
