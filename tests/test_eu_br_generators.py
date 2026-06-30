"""
Tests for EU GDPR and Brazil LGPD jurisdiction generators.

Covers: format validation, checksum correctness, span verification,
jurisdiction tagging, conflict case metadata, seed reproducibility,
authority citation presence, and corpus file generation.

Authority references:
  EU: GDPR Article 4(1), Article 9(1), Article 89
  BR: LGPD Brazil 2020 Article 5 (Lei 13.709/2018)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from generators.eu.eu_gdpr import (
    EUGDPRGenerator,
    _bsn_check_digit,
    _pesel_check,
    generate_corpus as eu_generate_corpus,
    make_bsn,
    make_dni,
    make_pesel,
    make_personnummer_se,
)
from generators.br.br_lgpd import (
    BrazilLGPDGenerator,
    _cpf_check_digits,
    generate_corpus as br_generate_corpus,
    make_cnpj,
    make_cns,
    make_cpf,
    make_phone_br,
)
from generators.common import (
    DETECTION_REGIME_CONFLICT,
    DETECTION_REGIME_RULE,
    LAYER_CONFLICT,
    LAYER_GDPR,
    LAYER_BRAZIL,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def eu_records():
    gen = EUGDPRGenerator(seed=42)
    return gen.generate_batch(count_per_type=4)


@pytest.fixture(scope="module")
def br_records():
    gen = BrazilLGPDGenerator(seed=42)
    return gen.generate_batch(count_per_type=4)


# ---------------------------------------------------------------------------
# EU: BSN_NL format and checksum
# ---------------------------------------------------------------------------

class TestBSN:
    def test_bsn_is_nine_digits(self):
        import random
        rng = random.Random(1)
        for _ in range(10):
            bsn = make_bsn(rng)
            assert re.fullmatch(r"\d{9}", bsn), f"BSN not 9 digits: {bsn}"

    def test_bsn_checksum_valid(self):
        """Weighted sum with weights [9,8,7,6,5,4,3,2,-1] must be 0 mod 11."""
        import random
        rng = random.Random(7)
        weights = [9, 8, 7, 6, 5, 4, 3, 2, -1]
        for _ in range(20):
            bsn = make_bsn(rng)
            digits = [int(c) for c in bsn]
            total = sum(digits[i] * weights[i] for i in range(9))
            assert total % 11 == 0, f"BSN checksum failed for {bsn}: sum={total}"

    def test_bsn_in_eu_records(self, eu_records):
        bsn_records = [r for r in eu_records if r.metadata.get("identifier_type") == "BSN_NL"]
        assert len(bsn_records) == 4
        for rec in bsn_records:
            bsn_spans = [s for s in rec.gold_spans if s.category == "BSN_NL"]
            assert bsn_spans, "No BSN_NL span found"
            assert re.fullmatch(r"\d{9}", bsn_spans[0].value)


# ---------------------------------------------------------------------------
# EU: CPR_DK format
# ---------------------------------------------------------------------------

class TestCPR:
    def test_cpr_format(self, eu_records):
        cpr_records = [r for r in eu_records if r.metadata.get("identifier_type") == "CPR_DK"]
        assert len(cpr_records) == 4
        for rec in cpr_records:
            cpr_spans = [s for s in rec.gold_spans if s.category == "CPR_DK"]
            assert cpr_spans
            assert re.fullmatch(r"\d{6}-\d{4}", cpr_spans[0].value), (
                f"CPR format mismatch: {cpr_spans[0].value}"
            )


# ---------------------------------------------------------------------------
# EU: CODICE_FISCALE format
# ---------------------------------------------------------------------------

class TestCodiceFiscale:
    def test_codice_fiscale_format(self, eu_records):
        cf_records = [
            r for r in eu_records
            if r.metadata.get("identifier_type") == "CODICE_FISCALE_IT"
        ]
        assert len(cf_records) == 4
        pattern = re.compile(r"[A-Z]{6}[0-9]{2}[A-Z][0-9]{2}[A-Z][0-9]{3}[A-Z]")
        for rec in cf_records:
            cf_spans = [s for s in rec.gold_spans if s.category == "CODICE_FISCALE_IT"]
            assert cf_spans
            assert pattern.fullmatch(cf_spans[0].value), (
                f"CODICE_FISCALE format mismatch: {cf_spans[0].value}"
            )


# ---------------------------------------------------------------------------
# EU: Jurisdiction tagging
# ---------------------------------------------------------------------------

class TestEUJurisdiction:
    def test_all_records_jurisdiction_eu(self, eu_records):
        for rec in eu_records:
            assert rec.jurisdiction == "eu", (
                f"Record {rec.record_id} has jurisdiction={rec.jurisdiction}"
            )

    def test_country_code_metadata_present(self, eu_records):
        for rec in eu_records:
            assert "country_code" in rec.metadata, (
                f"Record {rec.record_id} missing country_code"
            )


# ---------------------------------------------------------------------------
# EU: Conflict cases
# ---------------------------------------------------------------------------

class TestEUConflictCases:
    def test_conflict_records_have_conflict_jurisdictions(self, eu_records):
        conflict_records = [r for r in eu_records if r.layer == LAYER_CONFLICT]
        assert len(conflict_records) >= 4, (
            f"Expected at least 4 conflict records, got {len(conflict_records)}"
        )
        for rec in conflict_records:
            assert "conflict_jurisdictions" in rec.metadata, (
                f"Record {rec.record_id} missing conflict_jurisdictions"
            )
            cj = rec.metadata["conflict_jurisdictions"]
            assert "us" in cj and "eu" in cj, (
                f"Conflict jurisdictions should include us and eu: {cj}"
            )

    def test_conflict_spans_have_conflict_regime(self, eu_records):
        conflict_records = [r for r in eu_records if r.layer == LAYER_CONFLICT]
        for rec in conflict_records:
            conflict_spans = [
                s for s in rec.gold_spans
                if s.detection_regime == DETECTION_REGIME_CONFLICT
            ]
            assert conflict_spans, (
                f"Conflict record {rec.record_id} has no conflict-regime spans"
            )


# ---------------------------------------------------------------------------
# EU: Span verification
# ---------------------------------------------------------------------------

class TestEUSpanVerification:
    def test_all_eu_spans_verify(self, eu_records):
        errors = []
        for rec in eu_records:
            errs = rec.verify_spans()
            if errs:
                errors.append(f"{rec.record_id}: {errs}")
        assert not errors, f"Span verification failures: {errors}"


# ---------------------------------------------------------------------------
# EU: Authority citations
# ---------------------------------------------------------------------------

class TestEUAuthorityCitations:
    def test_authority_citations_present(self, eu_records):
        for rec in eu_records:
            assert rec.authority_citations, (
                f"Record {rec.record_id} has no authority_citations"
            )
        for rec in eu_records:
            for span in rec.gold_spans:
                assert span.authority, (
                    f"Span {span.category} in {rec.record_id} has no authority"
                )


# ---------------------------------------------------------------------------
# EU: Seed reproducibility
# ---------------------------------------------------------------------------

class TestEUSeedReproducibility:
    def test_eu_seed_reproducible(self):
        gen1 = EUGDPRGenerator(seed=42)
        gen2 = EUGDPRGenerator(seed=42)
        recs1 = gen1.generate_batch(count_per_type=2)
        recs2 = gen2.generate_batch(count_per_type=2)
        for r1, r2 in zip(recs1, recs2):
            assert r1.text == r2.text, "Seed reproducibility failed for EU generator"


# ---------------------------------------------------------------------------
# BR: CPF format and checksum
# ---------------------------------------------------------------------------

class TestCPF:
    def test_cpf_format(self, br_records):
        cpf_records = [r for r in br_records if r.metadata.get("identifier_type") == "CPF"]
        assert len(cpf_records) == 4
        pattern = re.compile(r"\d{3}\.\d{3}\.\d{3}-\d{2}")
        for rec in cpf_records:
            cpf_spans = [s for s in rec.gold_spans if s.category == "CPF"]
            assert cpf_spans
            assert pattern.fullmatch(cpf_spans[0].value), (
                f"CPF format mismatch: {cpf_spans[0].value}"
            )

    def test_cpf_checksum_valid(self):
        import random
        rng = random.Random(99)
        for _ in range(20):
            cpf_str = make_cpf(rng)
            digits_clean = re.sub(r"\D", "", cpf_str)
            assert len(digits_clean) == 11
            d9 = [int(c) for c in digits_clean[:9]]
            expected = _cpf_check_digits(d9)
            assert digits_clean[9:] == expected, (
                f"CPF checksum mismatch: {cpf_str}"
            )


# ---------------------------------------------------------------------------
# BR: PHONE_BR format
# ---------------------------------------------------------------------------

class TestPhoneBR:
    def test_phone_br_starts_with_plus55(self, br_records):
        phone_records = [r for r in br_records if r.metadata.get("identifier_type") == "PHONE_BR"]
        assert len(phone_records) == 4
        for rec in phone_records:
            ph_spans = [s for s in rec.gold_spans if s.category == "PHONE_BR"]
            assert ph_spans
            assert ph_spans[0].value.startswith("+55"), (
                f"Phone does not start with +55: {ph_spans[0].value}"
            )

    def test_phone_br_format(self):
        import random
        rng = random.Random(5)
        pattern = re.compile(r"\+55[1-9]{2}[0-9]{9}")
        for _ in range(10):
            phone = make_phone_br(rng)
            assert pattern.fullmatch(phone), f"Phone format mismatch: {phone}"


# ---------------------------------------------------------------------------
# BR: CNS format
# ---------------------------------------------------------------------------

class TestCNS:
    def test_cns_is_15_digits(self, br_records):
        cns_records = [r for r in br_records if r.metadata.get("identifier_type") == "CNS_BR"]
        assert len(cns_records) == 4
        for rec in cns_records:
            cns_spans = [s for s in rec.gold_spans if s.category == "CNS_BR"]
            assert cns_spans
            assert re.fullmatch(r"\d{15}", cns_spans[0].value), (
                f"CNS not 15 digits: {cns_spans[0].value}"
            )

    def test_cns_first_digit_valid(self):
        import random
        rng = random.Random(3)
        valid_first = set("12789")
        for _ in range(20):
            cns = make_cns(rng)
            assert cns[0] in valid_first, f"CNS first digit invalid: {cns}"


# ---------------------------------------------------------------------------
# BR: Jurisdiction tagging
# ---------------------------------------------------------------------------

class TestBRJurisdiction:
    def test_all_records_jurisdiction_br(self, br_records):
        for rec in br_records:
            assert rec.jurisdiction == "br", (
                f"Record {rec.record_id} has jurisdiction={rec.jurisdiction}"
            )

    def test_all_records_layer_brazil(self, br_records):
        for rec in br_records:
            assert rec.layer == LAYER_BRAZIL, (
                f"Record {rec.record_id} has layer={rec.layer}"
            )


# ---------------------------------------------------------------------------
# BR: Span verification
# ---------------------------------------------------------------------------

class TestBRSpanVerification:
    def test_all_br_spans_verify(self, br_records):
        errors = []
        for rec in br_records:
            errs = rec.verify_spans()
            if errs:
                errors.append(f"{rec.record_id}: {errs}")
        assert not errors, f"Span verification failures: {errors}"


# ---------------------------------------------------------------------------
# BR: Authority citations
# ---------------------------------------------------------------------------

class TestBRAuthorityCitations:
    def test_authority_citations_present(self, br_records):
        for rec in br_records:
            assert rec.authority_citations, (
                f"Record {rec.record_id} has no authority_citations"
            )
        for rec in br_records:
            for span in rec.gold_spans:
                assert span.authority, (
                    f"Span {span.category} in {rec.record_id} has no authority"
                )


# ---------------------------------------------------------------------------
# BR: Seed reproducibility
# ---------------------------------------------------------------------------

class TestBRSeedReproducibility:
    def test_br_seed_reproducible(self):
        gen1 = BrazilLGPDGenerator(seed=42)
        gen2 = BrazilLGPDGenerator(seed=42)
        recs1 = gen1.generate_batch(count_per_type=2)
        recs2 = gen2.generate_batch(count_per_type=2)
        for r1, r2 in zip(recs1, recs2):
            assert r1.text == r2.text, "Seed reproducibility failed for BR generator"


# ---------------------------------------------------------------------------
# Corpus file generation
# ---------------------------------------------------------------------------

class TestCorpusFileGeneration:
    def test_eu_generate_corpus_creates_file(self, tmp_path, monkeypatch):
        """generate_corpus() writes corpus/eu/eu_identifiers.jsonl."""
        import generators.eu.eu_gdpr as eu_module

        target = tmp_path / "corpus" / "eu" / "eu_identifiers.jsonl"

        original_write = eu_module.write_jsonl

        def patched_write(records, path):
            return original_write(records, target)

        monkeypatch.setattr(eu_module, "write_jsonl", patched_write)
        records = eu_module.generate_corpus(seed=42, count_per_type=2)
        assert target.exists(), "eu_identifiers.jsonl was not created"
        lines = target.read_text().strip().split("\n")
        assert len(lines) == len(records)
        # Verify each line is valid JSON
        for line in lines:
            obj = json.loads(line)
            assert "record_id" in obj

    def test_br_generate_corpus_creates_file(self, tmp_path, monkeypatch):
        """generate_corpus() writes corpus/br/brazil_identifiers.jsonl."""
        import generators.br.br_lgpd as br_module

        target = tmp_path / "corpus" / "br" / "brazil_identifiers.jsonl"

        original_write = br_module.write_jsonl

        def patched_write(records, path):
            return original_write(records, target)

        monkeypatch.setattr(br_module, "write_jsonl", patched_write)
        records = br_module.generate_corpus(seed=42, count_per_type=2)
        assert target.exists(), "brazil_identifiers.jsonl was not created"
        lines = target.read_text().strip().split("\n")
        assert len(lines) == len(records)
        for line in lines:
            obj = json.loads(line)
            assert "record_id" in obj
