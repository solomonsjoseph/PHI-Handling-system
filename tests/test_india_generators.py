"""
Tests for generators/in/in_identifiers.py

Validates format correctness, authority citations, span integrity,
seed reproducibility, and corpus output for 10 India PHI identifier types.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import importlib
import importlib.util
import sys

import pytest

# "in" is a Python keyword; use importlib to load the module directly
def _load_in_identifiers():
    spec = importlib.util.spec_from_file_location(
        "in_identifiers",
        str(Path(__file__).resolve().parents[1] / "generators" / "in" / "in_identifiers.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_mod = _load_in_identifiers()
IndiaIdentifierGenerator = _mod.IndiaIdentifierGenerator
generate_corpus = _mod.generate_corpus
# Use the generator module's own local Verhoeff check (correct implementation)
_verhoeff_check = _mod._verhoeff_check

from generators.common import LAYER_INDIA

SEED = 42
COUNT = 4  # records per identifier type
IDENTIFIER_TYPES = 10


@pytest.fixture(scope="module")
def records():
    gen = IndiaIdentifierGenerator(seed=SEED)
    return gen.generate_batch(count_per_identifier=COUNT)


@pytest.fixture(scope="module")
def records_by_type(records):
    buckets: dict[str, list] = {}
    for rec in records:
        layer_key = rec.record_id.split("_")[1]  # e.g. "aadhaar", "pan"
        buckets.setdefault(layer_key, []).append(rec)
    return buckets


# ---------------------------------------------------------------------------
# 1. Record count
# ---------------------------------------------------------------------------

def test_record_count(records):
    assert len(records) == IDENTIFIER_TYPES * COUNT


# ---------------------------------------------------------------------------
# 2. Aadhaar: 12 digits, valid Verhoeff checksum
# ---------------------------------------------------------------------------

def test_aadhaar_format(records):
    aadhaar_recs = [r for r in records if "in_aadhaar" in r.record_id]
    assert len(aadhaar_recs) == COUNT
    for rec in aadhaar_recs:
        spans = [s for s in rec.gold_spans if s.category == "AADHAAR"]
        assert spans, f"No AADHAAR span in {rec.record_id}"
        val = spans[0].value
        assert re.fullmatch(r"[2-9][0-9]{11}", val), f"Invalid Aadhaar: {val}"
        assert _verhoeff_check(val), f"Aadhaar Verhoeff check failed: {val}"


# ---------------------------------------------------------------------------
# 3. PAN: [A-Z]{5}[0-9]{4}[A-Z]
# ---------------------------------------------------------------------------

def test_pan_format(records):
    pan_recs = [r for r in records if "in_pan" in r.record_id]
    assert len(pan_recs) == COUNT
    for rec in pan_recs:
        spans = [s for s in rec.gold_spans if s.category == "PAN"]
        assert spans, f"No PAN span in {rec.record_id}"
        val = spans[0].value
        assert re.fullmatch(r"[A-Z]{5}[0-9]{4}[A-Z]", val), f"Invalid PAN: {val}"


# ---------------------------------------------------------------------------
# 4. ABHA Number: 14 digits, no leading zero
# ---------------------------------------------------------------------------

def test_abha_number_format(records):
    abha_recs = [r for r in records if "in_abha_number" in r.record_id]
    assert len(abha_recs) == COUNT
    for rec in abha_recs:
        spans = [s for s in rec.gold_spans if s.category == "ABHA_NUMBER"]
        assert spans, f"No ABHA_NUMBER span in {rec.record_id}"
        val = spans[0].value
        assert re.fullmatch(r"[1-9][0-9]{13}", val), f"Invalid ABHA_NUMBER: {val}"


# ---------------------------------------------------------------------------
# 5. ABHA Address: user@abdm
# ---------------------------------------------------------------------------

def test_abha_address_format(records):
    addr_recs = [r for r in records if "in_abha_address" in r.record_id]
    assert len(addr_recs) == COUNT
    for rec in addr_recs:
        spans = [s for s in rec.gold_spans if s.category == "ABHA_ADDRESS"]
        assert spans, f"No ABHA_ADDRESS span in {rec.record_id}"
        val = spans[0].value
        assert val.endswith("@abdm"), f"ABHA_ADDRESS must end with @abdm: {val}"
        assert re.fullmatch(r"[a-z0-9]{6,12}@abdm", val), f"Invalid ABHA_ADDRESS: {val}"


# ---------------------------------------------------------------------------
# 6. CTRI ID: CTRI/YYYY/MM/NNNNNN
# ---------------------------------------------------------------------------

def test_ctri_format(records):
    ctri_recs = [r for r in records if "in_ctri" in r.record_id]
    assert len(ctri_recs) == COUNT
    for rec in ctri_recs:
        spans = [s for s in rec.gold_spans if s.category == "CTRI_ID"]
        assert spans, f"No CTRI_ID span in {rec.record_id}"
        val = spans[0].value
        assert re.fullmatch(r"CTRI/\d{4}/\d{2}/\d{6}", val), f"Invalid CTRI_ID: {val}"


# ---------------------------------------------------------------------------
# 7. Mobile IN: starts with 6-9, 10 digits total
# ---------------------------------------------------------------------------

def test_mobile_in_format(records):
    mob_recs = [r for r in records if "in_mobile" in r.record_id]
    assert len(mob_recs) == COUNT
    for rec in mob_recs:
        spans = [s for s in rec.gold_spans if s.category == "MOBILE_IN"]
        assert spans, f"No MOBILE_IN span in {rec.record_id}"
        val = spans[0].value
        assert re.fullmatch(r"[6-9][0-9]{9}", val), f"Invalid MOBILE_IN: {val}"


# ---------------------------------------------------------------------------
# 8. Passport IN: [A-Z][0-9]{7}
# ---------------------------------------------------------------------------

def test_passport_format(records):
    pp_recs = [r for r in records if "in_passport" in r.record_id]
    assert len(pp_recs) == COUNT
    for rec in pp_recs:
        spans = [s for s in rec.gold_spans if s.category == "IN_PASSPORT"]
        assert spans, f"No IN_PASSPORT span in {rec.record_id}"
        val = spans[0].value
        assert re.fullmatch(r"[A-Z][0-9]{7}", val), f"Invalid IN_PASSPORT: {val}"


# ---------------------------------------------------------------------------
# 9. Voter ID: [A-Z]{3}[0-9]{7}
# ---------------------------------------------------------------------------

def test_voter_id_format(records):
    v_recs = [r for r in records if "in_voter_id" in r.record_id]
    assert len(v_recs) == COUNT
    for rec in v_recs:
        spans = [s for s in rec.gold_spans if s.category == "VOTER_ID_EPIC"]
        assert spans, f"No VOTER_ID_EPIC span in {rec.record_id}"
        val = spans[0].value
        assert re.fullmatch(r"[A-Z]{3}[0-9]{7}", val), f"Invalid VOTER_ID: {val}"


# ---------------------------------------------------------------------------
# 10. UAN: 12 digits, no leading zero
# ---------------------------------------------------------------------------

def test_uan_format(records):
    u_recs = [r for r in records if "in_uan" in r.record_id]
    assert len(u_recs) == COUNT
    for rec in u_recs:
        spans = [s for s in rec.gold_spans if s.category == "UAN"]
        assert spans, f"No UAN span in {rec.record_id}"
        val = spans[0].value
        assert re.fullmatch(r"[1-9][0-9]{11}", val), f"Invalid UAN: {val}"


# ---------------------------------------------------------------------------
# 11. Driving License: at least 5 state variants present
# ---------------------------------------------------------------------------

def test_dl_state_variants(records):
    dl_recs = [r for r in records if "in_dl" in r.record_id]
    assert len(dl_recs) == COUNT
    state_codes = {rec.metadata.get("state_code") for rec in dl_recs}
    # With COUNT=4 and 5 states cycling, we expect min(COUNT, 5) distinct codes
    assert len(state_codes) >= min(COUNT, 5), f"Expected state variants, got: {state_codes}"
    for rec in dl_recs:
        spans = [s for s in rec.gold_spans if s.category == "DRIVING_LICENSE_IN"]
        assert spans, f"No DRIVING_LICENSE_IN span in {rec.record_id}"
        val = spans[0].value
        assert re.fullmatch(r"(MH|DL|KA|TN|UP)\d{2}\d{4}\d{7}", val), f"Invalid DL: {val}"


# ---------------------------------------------------------------------------
# 12. All records have authority_citations (non-empty list)
# ---------------------------------------------------------------------------

def test_all_records_have_authority_citations(records):
    for rec in records:
        assert rec.authority_citations, f"Missing authority_citations in {rec.record_id}"
        assert len(rec.authority_citations) >= 1


# ---------------------------------------------------------------------------
# 13. All spans pass verify_spans()
# ---------------------------------------------------------------------------

def test_all_spans_verified(records):
    for rec in records:
        errors = rec.verify_spans()
        assert not errors, f"Span errors in {rec.record_id}: {errors}"


# ---------------------------------------------------------------------------
# 14. Seed reproducibility: two generators with seed=42 produce identical output
# ---------------------------------------------------------------------------

def test_seed_reproducibility():
    gen1 = IndiaIdentifierGenerator(seed=42)
    gen2 = IndiaIdentifierGenerator(seed=42)
    recs1 = gen1.generate_batch(count_per_identifier=COUNT)
    recs2 = gen2.generate_batch(count_per_identifier=COUNT)
    for r1, r2 in zip(recs1, recs2):
        assert r1.text == r2.text, f"Text mismatch for {r1.record_id}"
        assert r1.record_id == r2.record_id
        for s1, s2 in zip(r1.gold_spans, r2.gold_spans):
            assert s1.value == s2.value
            assert s1.start == s2.start


# ---------------------------------------------------------------------------
# 15. Different seeds produce different output
# ---------------------------------------------------------------------------

def test_different_seeds_differ():
    gen_a = IndiaIdentifierGenerator(seed=42)
    gen_b = IndiaIdentifierGenerator(seed=99)
    recs_a = gen_a.generate_batch(count_per_identifier=1)
    recs_b = gen_b.generate_batch(count_per_identifier=1)
    texts_a = {r.text for r in recs_a}
    texts_b = {r.text for r in recs_b}
    assert texts_a != texts_b, "Different seeds must produce different output"


# ---------------------------------------------------------------------------
# 16. jurisdiction = "in" on all records
# ---------------------------------------------------------------------------

def test_jurisdiction_in(records):
    for rec in records:
        assert rec.jurisdiction == "in", f"Wrong jurisdiction in {rec.record_id}: {rec.jurisdiction}"


# ---------------------------------------------------------------------------
# 17. layer = LAYER_INDIA on all records
# ---------------------------------------------------------------------------

def test_layer_india(records):
    for rec in records:
        assert rec.layer == LAYER_INDIA, f"Wrong layer in {rec.record_id}: {rec.layer}"


# ---------------------------------------------------------------------------
# 18. generate_corpus() writes JSONL file and count matches
# ---------------------------------------------------------------------------

def test_generate_corpus_writes_jsonl(tmp_path, monkeypatch):
    """generate_corpus() must write a JSONL file with the correct record count."""
    # Call with defaults; generate_corpus writes to corpus/in/india_identifiers.jsonl
    records_out = generate_corpus(seed=42, count_per_identifier=COUNT)
    repo_root = Path(__file__).resolve().parents[1]
    out_path = repo_root / "corpus" / "in" / "india_identifiers.jsonl"
    assert out_path.exists(), f"JSONL not written: {out_path}"
    lines = out_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == IDENTIFIER_TYPES * COUNT, f"Expected {IDENTIFIER_TYPES * COUNT} lines, got {len(lines)}"


# ---------------------------------------------------------------------------
# 19. JSONL records are valid JSON with required keys
# ---------------------------------------------------------------------------

def test_jsonl_valid_json():
    repo_root = Path(__file__).resolve().parents[1]
    out_path = repo_root / "corpus" / "in" / "india_identifiers.jsonl"
    if not out_path.exists():
        generate_corpus(seed=42, count_per_identifier=COUNT)
    required_keys = {"record_id", "text", "gold_spans", "layer", "jurisdiction",
                     "authority_citations"}
    with open(out_path, encoding="utf-8") as f:
        for line in f:
            obj = json.loads(line)
            missing = required_keys - set(obj.keys())
            assert not missing, f"Missing keys {missing} in record {obj.get('record_id')}"


# ---------------------------------------------------------------------------
# 20. Every span has non-empty value and correct length
# ---------------------------------------------------------------------------

def test_span_values_non_empty(records):
    for rec in records:
        for span in rec.gold_spans:
            assert span.value, f"Empty span value in {rec.record_id}"
            assert span.end > span.start, f"Zero-length span in {rec.record_id}"
            assert len(span.value) == span.end - span.start
