"""
Tests for all HIPAA jurisdiction generators.

Validates:
  1. All records pass span offset verification (gold spans match text)
  2. All records have at least one authority_citation
  3. All gold spans have detection_regime set
  4. All records have the correct jurisdiction field
  5. Seeded generators are deterministic (same seed => same output)
  6. LDS records correctly classify as valid_lds vs lds_violation
  7. Re-ID code records correctly classify as permitted vs forbidden
  8. Fundraising records have context='fundraising'
  9. Verification audit log records have context='operations'
 10. No record has an empty record_id or empty text
 11. Biometric records cite 164.514(b)(2)(i)(P) and GDPR Art. 4(14)
 12. Device records cover GS1, HIBCC, ICCBBA UDI formats
 13. Fax disambiguation records have both FAX and PHONE spans
 14. Vehicle records contain 17-char VIN patterns
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from generators import (
    HIPAASafeHarborGenerator,
    HIPAAQuasiIdentifierGenerator,
    HIPAALDSGenerator,
    HIPAAReIDCodesGenerator,
    HIPAAFundraisingGenerator,
    HIPAAVerificationGenerator,
    HIPAABiometricGenerator,
    HIPAADeviceGenerator,
    HIPAAFaxGenerator,
    HIPAAVehicleGenerator,
)
from generators.common import DETECTION_REGIME_CONFLICT, DETECTION_REGIME_NER, DETECTION_REGIME_RULE

SEED = 42


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def assert_records_valid(records):
    """Assert span offsets, authority citations, detection_regime, and IDs."""
    assert records, "Generator produced zero records"
    for r in records:
        assert r.record_id, f"Empty record_id in record: {r}"
        assert r.text, f"Empty text in record {r.record_id}"
        assert r.jurisdiction, f"No jurisdiction in {r.record_id}"

        errors = r.verify_spans()
        assert not errors, f"Span errors in {r.record_id}: {errors}"

        assert r.authority_citations, f"No authority_citations in {r.record_id}"

        for span in r.gold_spans:
            assert span.detection_regime in (
                DETECTION_REGIME_RULE,
                DETECTION_REGIME_NER,
                DETECTION_REGIME_CONFLICT,
            ), f"Invalid detection_regime '{span.detection_regime}' in {r.record_id}"

        assert r.detection_regime in (
            DETECTION_REGIME_RULE,
            DETECTION_REGIME_NER,
            DETECTION_REGIME_CONFLICT,
        ), f"Invalid record detection_regime in {r.record_id}"


# ---------------------------------------------------------------------------
# HIPAASafeHarborGenerator
# ---------------------------------------------------------------------------

class TestHIPAASafeHarborGenerator:

    def test_basic_generation(self):
        g = HIPAASafeHarborGenerator(SEED)
        records = g.generate_batch(count_per_category=3)
        # 18 categories x 3 = 54 records
        assert len(records) == 54

    def test_all_records_valid(self):
        g = HIPAASafeHarborGenerator(SEED)
        assert_records_valid(g.generate_batch(count_per_category=2))

    def test_all_us_jurisdiction(self):
        g = HIPAASafeHarborGenerator(SEED)
        for r in g.generate_batch(count_per_category=1):
            assert r.jurisdiction == "us", f"Wrong jurisdiction in {r.record_id}"

    def test_category_A_has_names(self):
        g = HIPAASafeHarborGenerator(SEED)
        records = g.generate_batch(count_per_category=1)
        cat_a = [r for r in records if "hipaa_A" in r.record_id]
        assert cat_a
        for r in cat_a:
            name_spans = [s for s in r.gold_spans if "NAME" in s.category]
            assert name_spans, f"No name spans in {r.record_id}"

    def test_category_B_zip_is_conflict(self):
        g = HIPAASafeHarborGenerator(SEED)
        records = g.generate_batch(count_per_category=2)
        cat_b = [r for r in records if "hipaa_B" in r.record_id]
        assert cat_b
        for r in cat_b:
            zip_spans = [s for s in r.gold_spans if s.category == "ADDRESS_ZIP"]
            for span in zip_spans:
                assert span.detection_regime == DETECTION_REGIME_CONFLICT, (
                    f"ZIP span in {r.record_id} should be conflict_case"
                )

    def test_category_I_mbi_format(self):
        """MBI must be exactly 11 chars, CMS format C A AN N A AN N A A N N."""
        import re
        # CMS MBI pattern: C A AN N A AN N A A N N
        # Valid alpha set: ACDEFGHJKMNPQRTUVWXY (excludes SLOBIZ per CMS spec)
        # Regex class: [AC-HJ-KMNP-RT-WXY]  (J-K not J-N, to exclude L; explicit X, Y)
        alpha = r'[AC-HJ-KMNP-RT-WXY]'
        alnum = r'[AC-HJ-KMNP-RT-WXY0-9]'
        mbi_pattern = re.compile(
            rf'^[1-9]{alpha}{alnum}[0-9]{alpha}{alnum}[0-9]{alpha}{alpha}[0-9][0-9]$'
        )
        g = HIPAASafeHarborGenerator(SEED)
        records = g.generate_batch(count_per_category=10)
        cat_i = [r for r in records if "hipaa_I" in r.record_id]
        assert cat_i, "No category I records generated"
        for r in cat_i:
            mbi_spans = [s for s in r.gold_spans if s.category == "HEALTH_PLAN_ID"]
            assert mbi_spans, f"No HEALTH_PLAN_ID span in {r.record_id}"
            for span in mbi_spans:
                value = r.text[span.start:span.end]
                assert len(value) == 11, (
                    f"MBI in {r.record_id} is {len(value)} chars, expected 11: '{value}'"
                )
                assert mbi_pattern.match(value), (
                    f"MBI in {r.record_id} fails CMS format: '{value}'"
                )

    def test_determinism(self):
        g1 = HIPAASafeHarborGenerator(SEED)
        g2 = HIPAASafeHarborGenerator(SEED)
        records1 = g1.generate_batch(count_per_category=2)
        records2 = g2.generate_batch(count_per_category=2)
        for r1, r2 in zip(records1, records2):
            assert r1.record_id == r2.record_id
            assert r1.text == r2.text


# ---------------------------------------------------------------------------
# HIPAAQuasiIdentifierGenerator
# ---------------------------------------------------------------------------

class TestHIPAAQuasiIdentifierGenerator:

    def test_basic_generation(self):
        g = HIPAAQuasiIdentifierGenerator(SEED)
        records = g.generate_batch(count=10)
        assert len(records) == 10

    def test_all_records_valid(self):
        g = HIPAAQuasiIdentifierGenerator(SEED)
        assert_records_valid(g.generate_batch(count=10))

    def test_safe_harbor_de_id_tier(self):
        g = HIPAAQuasiIdentifierGenerator(SEED)
        for r in g.generate_batch(count=5):
            assert r.de_id_tier == "safe_harbor", (
                f"{r.record_id} should claim safe_harbor (but has quasi-IDs)"
            )

    def test_sweeney_metadata(self):
        g = HIPAAQuasiIdentifierGenerator(SEED)
        for r in g.generate_batch(count=5):
            assert r.metadata.get("sweeney_vulnerable") is True


# ---------------------------------------------------------------------------
# HIPAALDSGenerator
# ---------------------------------------------------------------------------

class TestHIPAALDSGenerator:

    def test_basic_generation(self):
        g = HIPAALDSGenerator(SEED)
        records = g.generate_batch(count=20)
        assert len(records) > 0

    def test_all_records_valid(self):
        g = HIPAALDSGenerator(SEED)
        assert_records_valid(g.generate_batch(count=10))

    def test_valid_lds_tier(self):
        g = HIPAALDSGenerator(SEED)
        records = g.generate_batch(count=10)
        valid = [r for r in records if r.record_id.startswith("valid_lds")]
        assert valid
        for r in valid:
            assert r.de_id_tier == "limited_data_set"
            assert r.metadata.get("dates_retained") is True
            assert r.metadata.get("geography_retained") is True

    def test_lds_violation_records_exist(self):
        g = HIPAALDSGenerator(SEED)
        records = g.generate_batch(count=10)
        violations = [r for r in records if r.record_id.startswith("lds_violation")]
        assert violations
        for r in violations:
            assert r.metadata.get("lds_compliant") is False

    def test_lds_vs_safeharbor_pairs(self):
        g = HIPAALDSGenerator(SEED)
        records = g.generate_batch(count=20)
        lds_tier = [r for r in records if r.record_id.startswith("lds_vs_sh_lds")]
        sh_tier = [r for r in records if r.record_id.startswith("lds_vs_sh_sh")]
        assert len(lds_tier) == len(sh_tier)
        for r in lds_tier:
            assert r.de_id_tier == "limited_data_set"
        for r in sh_tier:
            assert r.de_id_tier == "safe_harbor"

    def test_lds_authority_citations(self):
        g = HIPAALDSGenerator(SEED)
        records = g.generate_batch(count=20)
        # Only records with de_id_tier='limited_data_set' are LDS records
        lds_records = [r for r in records if r.de_id_tier == "limited_data_set"]
        assert lds_records
        for r in lds_records:
            assert any("164.514(e)" in c for c in r.authority_citations), (
                f"{r.record_id} (de_id_tier=limited_data_set) missing 164.514(e) citation"
            )


# ---------------------------------------------------------------------------
# HIPAAReIDCodesGenerator
# ---------------------------------------------------------------------------

class TestHIPAAReIDCodesGenerator:

    def test_basic_generation(self):
        g = HIPAAReIDCodesGenerator(SEED)
        records = g.generate_batch(count=10)
        assert len(records) == 20  # 10 permitted + 10 forbidden

    def test_all_records_valid(self):
        g = HIPAAReIDCodesGenerator(SEED)
        assert_records_valid(g.generate_batch(count=5))

    def test_permitted_and_forbidden_both_present(self):
        g = HIPAAReIDCodesGenerator(SEED)
        records = g.generate_batch(count=10)
        permitted = [r for r in records if r.record_id.startswith("permitted")]
        forbidden = [r for r in records if r.record_id.startswith("forbidden")]
        assert permitted
        assert forbidden

    def test_permitted_compliant_flag(self):
        g = HIPAAReIDCodesGenerator(SEED)
        records = g.generate_batch(count=5)
        for r in records:
            if r.record_id.startswith("permitted"):
                assert r.metadata.get("hipaa_514c_compliant") is True
            elif r.record_id.startswith("forbidden"):
                assert r.metadata.get("hipaa_514c_compliant") is False

    def test_forbidden_span_categories(self):
        g = HIPAAReIDCodesGenerator(SEED)
        records = g.generate_batch(count=10)
        for r in records:
            if r.record_id.startswith("forbidden"):
                code_spans = [s for s in r.gold_spans if s.category == "REID_CODE_FORBIDDEN"]
                assert code_spans, f"No REID_CODE_FORBIDDEN span in {r.record_id}"

    def test_authority_citations(self):
        g = HIPAAReIDCodesGenerator(SEED)
        for r in g.generate_batch(count=5):
            assert any("164.514(c)" in c for c in r.authority_citations), (
                f"{r.record_id} missing 164.514(c) citation"
            )


# ---------------------------------------------------------------------------
# HIPAAFundraisingGenerator
# ---------------------------------------------------------------------------

class TestHIPAAFundraisingGenerator:

    def test_basic_generation(self):
        g = HIPAAFundraisingGenerator(SEED)
        records = g.generate_batch(count=10)
        assert len(records) > 0

    def test_all_records_valid(self):
        g = HIPAAFundraisingGenerator(SEED)
        assert_records_valid(g.generate_batch(count=5))

    def test_fundraising_context(self):
        g = HIPAAFundraisingGenerator(SEED)
        records = g.generate_batch(count=10)
        fr_records = [r for r in records if r.record_id.startswith("fundraising")]
        assert fr_records
        for r in fr_records:
            assert r.context == "fundraising"
            assert r.metadata.get("phi_use_permitted") is True

    def test_treatment_context(self):
        g = HIPAAFundraisingGenerator(SEED)
        records = g.generate_batch(count=10)
        tr_records = [r for r in records if r.record_id.startswith("treatment")]
        assert tr_records
        for r in tr_records:
            assert r.context == "treatment"
            assert r.metadata.get("phi_use_permitted") is False

    def test_crosswalk_pairs(self):
        g = HIPAAFundraisingGenerator(SEED)
        records = g.generate_batch(count=20)
        fr_pairs = [r for r in records if r.record_id.startswith("crosswalk_fr")]
        tr_pairs = [r for r in records if r.record_id.startswith("crosswalk_tr")]
        assert len(fr_pairs) == len(tr_pairs)

    def test_authority_citations(self):
        g = HIPAAFundraisingGenerator(SEED)
        for r in g.generate_batch(count=5):
            assert r.authority_citations, f"{r.record_id} has no authority_citations"


# ---------------------------------------------------------------------------
# HIPAAVerificationGenerator
# ---------------------------------------------------------------------------

class TestHIPAAVerificationGenerator:

    def test_basic_generation(self):
        g = HIPAAVerificationGenerator(SEED)
        records = g.generate_batch(count=10)
        assert len(records) > 0

    def test_all_records_valid(self):
        g = HIPAAVerificationGenerator(SEED)
        assert_records_valid(g.generate_batch(count=5))

    def test_operations_context(self):
        g = HIPAAVerificationGenerator(SEED)
        for r in g.generate_batch(count=5):
            assert r.context == "operations", f"{r.record_id} should be context=operations"

    def test_audit_records_exist(self):
        g = HIPAAVerificationGenerator(SEED)
        records = g.generate_batch(count=10)
        audit = [r for r in records if r.record_id.startswith("audit")]
        assert audit

    def test_denied_records_no_disclosure(self):
        g = HIPAAVerificationGenerator(SEED)
        records = g.generate_batch(count=10)
        denied = [r for r in records if r.record_id.startswith("denied")]
        assert denied
        for r in denied:
            assert r.metadata.get("phi_disclosed") is False

    def test_subpoena_has_case_number_span(self):
        g = HIPAAVerificationGenerator(SEED)
        records = g.generate_batch(count=10)
        subpoenas = [r for r in records if r.record_id.startswith("subpoena")]
        assert subpoenas
        for r in subpoenas:
            case_spans = [s for s in r.gold_spans if s.category == "CASE_NUMBER"]
            assert case_spans, f"No CASE_NUMBER span in {r.record_id}"

    def test_law_enforcement_ssn_span(self):
        g = HIPAAVerificationGenerator(SEED)
        records = g.generate_batch(count=10)
        le_records = [r for r in records if r.record_id.startswith("law_enf")]
        assert le_records
        for r in le_records:
            ssn_spans = [s for s in r.gold_spans if s.category == "SSN"]
            assert ssn_spans, f"No SSN span in law enforcement record {r.record_id}"

    def test_authority_citations(self):
        g = HIPAAVerificationGenerator(SEED)
        for r in g.generate_batch(count=5):
            assert any("164.514(h)" in c for c in r.authority_citations), (
                f"{r.record_id} missing 164.514(h) citation"
            )


# ---------------------------------------------------------------------------
# HIPAABiometricGenerator
# ---------------------------------------------------------------------------

class TestHIPAABiometricGenerator:

    def test_basic_generation(self):
        g = HIPAABiometricGenerator(SEED)
        records = g.generate_batch(count_per_mode=2)
        # 10 modes x 2 = 20
        assert len(records) == 20

    def test_all_records_valid(self):
        g = HIPAABiometricGenerator(SEED)
        assert_records_valid(g.generate_batch(count_per_mode=2))

    def test_authority_hipaa_P(self):
        g = HIPAABiometricGenerator(SEED)
        for r in g.generate_batch(count_per_mode=2):
            assert any("164.514(b)(2)(i)(P)" in c for c in r.authority_citations), (
                f"{r.record_id} missing 164.514(b)(2)(i)(P) citation"
            )

    def test_gdpr_cross_reference(self):
        g = HIPAABiometricGenerator(SEED)
        for r in g.generate_batch(count_per_mode=2):
            assert any("4(14)" in c or "4(13)" in c for c in r.authority_citations), (
                f"{r.record_id} missing GDPR Art. 4(14) or 4(13) cross-reference"
            )

    def test_all_spans_have_biometric_category(self):
        g = HIPAABiometricGenerator(SEED)
        for r in g.generate_batch(count_per_mode=2):
            for span in r.gold_spans:
                assert "BIOMETRIC" in span.category, (
                    f"Span '{span.category}' in {r.record_id} missing BIOMETRIC prefix"
                )

    def test_multi_modality_has_multiple_spans(self):
        g = HIPAABiometricGenerator(SEED)
        records = g.generate_batch(count_per_mode=2)
        multi = [r for r in records if "multi_modality" in r.record_id]
        assert multi
        for r in multi:
            assert len(r.gold_spans) >= 2, (
                f"{r.record_id} should have >= 2 spans (fingerprint + iris)"
            )

    def test_dna_specimens_cite_genetic_data(self):
        g = HIPAABiometricGenerator(SEED)
        records = g.generate_batch(count_per_mode=2)
        dna = [r for r in records if "dna_specimen" in r.record_id]
        assert dna
        for r in dna:
            assert any("4(13)" in c for c in r.authority_citations), (
                f"{r.record_id} DNA record should cite GDPR Art. 4(13)"
            )

    def test_determinism(self):
        g1 = HIPAABiometricGenerator(SEED)
        g2 = HIPAABiometricGenerator(SEED)
        r1 = g1.generate_batch(count_per_mode=1)
        r2 = g2.generate_batch(count_per_mode=1)
        for a, b in zip(r1, r2):
            assert a.text == b.text
            assert a.record_id == b.record_id


# ---------------------------------------------------------------------------
# HIPAADeviceGenerator
# ---------------------------------------------------------------------------

class TestHIPAADeviceGenerator:

    def test_basic_generation(self):
        g = HIPAADeviceGenerator(SEED)
        records = g.generate_batch(count_per_mode=2)
        # 10 modes x 2 = 20
        assert len(records) == 20

    def test_all_records_valid(self):
        g = HIPAADeviceGenerator(SEED)
        assert_records_valid(g.generate_batch(count_per_mode=2))

    def test_authority_hipaa_M(self):
        g = HIPAADeviceGenerator(SEED)
        for r in g.generate_batch(count_per_mode=2):
            assert any("164.514(b)(2)(i)(M)" in c for c in r.authority_citations), (
                f"{r.record_id} missing 164.514(b)(2)(i)(M) citation"
            )

    def test_fda_udi_cited(self):
        g = HIPAADeviceGenerator(SEED)
        for r in g.generate_batch(count_per_mode=2):
            assert any("FDA UDI" in c or "21 CFR 830" in c for c in r.authority_citations), (
                f"{r.record_id} missing FDA UDI citation"
            )

    def test_gs1_udi_format(self):
        g = HIPAADeviceGenerator(SEED)
        records = g.generate_batch(count_per_mode=2)
        cardiac = [r for r in records if "cardiac_implant" in r.record_id]
        assert cardiac
        for r in cardiac:
            gs1_spans = [s for s in r.gold_spans if s.category == "DEVICE_UDI_GS1"]
            assert gs1_spans
            for span in gs1_spans:
                assert span.value.startswith("(01)"), (
                    f"GS1 UDI '{span.value}' must start with '(01)'"
                )

    def test_hibcc_udi_format(self):
        g = HIPAADeviceGenerator(SEED)
        records = g.generate_batch(count_per_mode=2)
        orth = [r for r in records if "orthopedic_implant" in r.record_id]
        assert orth
        for r in orth:
            hibcc = [s for s in r.gold_spans if s.category == "DEVICE_UDI_HIBCC"]
            assert hibcc
            for span in hibcc:
                assert span.value.startswith("+"), (
                    f"HIBCC UDI '{span.value}' must start with '+'"
                )

    def test_iccbba_udi_format(self):
        g = HIPAADeviceGenerator(SEED)
        records = g.generate_batch(count_per_mode=2)
        blood = [r for r in records if "blood_product" in r.record_id]
        assert blood
        for r in blood:
            iccbba = [s for s in r.gold_spans if s.category == "DEVICE_UDI_ICCBBA"]
            assert iccbba
            for span in iccbba:
                assert span.value.startswith("=R"), (
                    f"ICCBBA UDI '{span.value}' must start with '=R'"
                )

    def test_implanted_devices_have_serial(self):
        g = HIPAADeviceGenerator(SEED)
        records = g.generate_batch(count_per_mode=2)
        implanted = [r for r in records
                     if r.metadata.get("implanted") is True]
        assert implanted
        for r in implanted:
            serial_spans = [s for s in r.gold_spans if s.category == "DEVICE_SERIAL"]
            assert serial_spans, f"{r.record_id} implanted device missing DEVICE_SERIAL span"

    def test_determinism(self):
        g1 = HIPAADeviceGenerator(SEED)
        g2 = HIPAADeviceGenerator(SEED)
        r1 = g1.generate_batch(count_per_mode=1)
        r2 = g2.generate_batch(count_per_mode=1)
        for a, b in zip(r1, r2):
            assert a.text == b.text


# ---------------------------------------------------------------------------
# HIPAAFaxGenerator
# ---------------------------------------------------------------------------

class TestHIPAAFaxGenerator:

    def test_basic_generation(self):
        g = HIPAAFaxGenerator(SEED)
        records = g.generate_batch(count_per_mode=2)
        # 10 modes x 2 = 20
        assert len(records) == 20

    def test_all_records_valid(self):
        g = HIPAAFaxGenerator(SEED)
        assert_records_valid(g.generate_batch(count_per_mode=2))

    def test_authority_hipaa_E(self):
        g = HIPAAFaxGenerator(SEED)
        for r in g.generate_batch(count_per_mode=2):
            assert any("164.514(b)(2)(i)(E)" in c for c in r.authority_citations), (
                f"{r.record_id} missing 164.514(b)(2)(i)(E) citation"
            )

    def test_fax_spans_present(self):
        g = HIPAAFaxGenerator(SEED)
        for r in g.generate_batch(count_per_mode=2):
            fax_spans = [s for s in r.gold_spans if "FAX" in s.category]
            assert fax_spans, f"{r.record_id} has no FAX-category spans"

    def test_disambiguation_has_both_fax_and_phone(self):
        g = HIPAAFaxGenerator(SEED)
        records = g.generate_batch(count_per_mode=2)
        disambig = [r for r in records if "fax_phone_disambiguation" in r.record_id]
        assert disambig
        for r in disambig:
            fax_spans = [s for s in r.gold_spans if "FAX" in s.category]
            phone_spans = [s for s in r.gold_spans if "PHONE" in s.category]
            assert fax_spans, f"{r.record_id} disambiguation record missing FAX span"
            assert phone_spans, f"{r.record_id} disambiguation record missing PHONE span"

    def test_disambiguation_cites_both_E_and_D(self):
        g = HIPAAFaxGenerator(SEED)
        records = g.generate_batch(count_per_mode=2)
        disambig = [r for r in records if "fax_phone_disambiguation" in r.record_id]
        assert disambig
        for r in disambig:
            assert any("(E)" in c for c in r.authority_citations)
            assert any("(D)" in c for c in r.authority_citations)

    def test_international_fax_e164_format(self):
        g = HIPAAFaxGenerator(SEED)
        records = g.generate_batch(count_per_mode=2)
        intl = [r for r in records if "international_fax" in r.record_id]
        assert intl
        for r in intl:
            intl_spans = [s for s in r.gold_spans if s.category == "FAX_INTERNATIONAL"]
            assert intl_spans
            for span in intl_spans:
                assert span.value.startswith("+1-"), (
                    f"International fax '{span.value}' should start with '+1-'"
                )

    def test_broadcast_has_three_fax_spans(self):
        g = HIPAAFaxGenerator(SEED)
        records = g.generate_batch(count_per_mode=2)
        broadcast = [r for r in records if "fax_broadcast" in r.record_id]
        assert broadcast
        for r in broadcast:
            fax_spans = [s for s in r.gold_spans if "FAX_BROADCAST" in s.category]
            assert len(fax_spans) == 3, (
                f"{r.record_id} broadcast record should have 3 FAX_BROADCAST spans"
            )

    def test_determinism(self):
        g1 = HIPAAFaxGenerator(SEED)
        g2 = HIPAAFaxGenerator(SEED)
        r1 = g1.generate_batch(count_per_mode=1)
        r2 = g2.generate_batch(count_per_mode=1)
        for a, b in zip(r1, r2):
            assert a.text == b.text


# ---------------------------------------------------------------------------
# HIPAAVehicleGenerator
# ---------------------------------------------------------------------------

class TestHIPAAVehicleGenerator:

    def test_basic_generation(self):
        g = HIPAAVehicleGenerator(SEED)
        records = g.generate_batch(count_per_mode=2)
        # 10 modes x 2 = 20
        assert len(records) == 20

    def test_all_records_valid(self):
        g = HIPAAVehicleGenerator(SEED)
        assert_records_valid(g.generate_batch(count_per_mode=2))

    def test_authority_hipaa_L(self):
        g = HIPAAVehicleGenerator(SEED)
        for r in g.generate_batch(count_per_mode=2):
            assert any("164.514(b)(2)(i)(L)" in c for c in r.authority_citations), (
                f"{r.record_id} missing 164.514(b)(2)(i)(L) citation"
            )

    def test_iso_3779_cited(self):
        g = HIPAAVehicleGenerator(SEED)
        for r in g.generate_batch(count_per_mode=2):
            assert any("ISO 3779" in c for c in r.authority_citations), (
                f"{r.record_id} missing ISO 3779 citation"
            )

    def test_vin_is_17_chars(self):
        g = HIPAAVehicleGenerator(SEED)
        records = g.generate_batch(count_per_mode=2)
        for r in records:
            vin_spans = [s for s in r.gold_spans if s.category == "VIN"]
            for span in vin_spans:
                assert len(span.value) == 17, (
                    f"VIN '{span.value}' in {r.record_id} is not 17 chars"
                )

    def test_vin_no_forbidden_chars(self):
        """VIN must not contain I, O, Q per NHTSA 49 CFR Part 565."""
        g = HIPAAVehicleGenerator(SEED)
        records = g.generate_batch(count_per_mode=4)
        for r in records:
            for span in r.gold_spans:
                if span.category == "VIN":
                    forbidden = set(span.value) & {"I", "O", "Q"}
                    assert not forbidden, (
                        f"VIN '{span.value}' contains forbidden chars {forbidden}"
                    )

    def test_vin_has_rule_applicable_regime(self):
        g = HIPAAVehicleGenerator(SEED)
        records = g.generate_batch(count_per_mode=2)
        for r in records:
            for span in r.gold_spans:
                if span.category == "VIN":
                    assert span.detection_regime == DETECTION_REGIME_RULE, (
                        f"VIN span in {r.record_id} should be rule_applicable"
                    )

    def test_plate_has_ner_regime(self):
        g = HIPAAVehicleGenerator(SEED)
        records = g.generate_batch(count_per_mode=2)
        for r in records:
            for span in r.gold_spans:
                if "LICENSE_PLATE" in span.category:
                    assert span.detection_regime == DETECTION_REGIME_NER, (
                        f"Plate span in {r.record_id} should be contextual_ner_required"
                    )

    def test_ambulance_has_both_vin_and_plate(self):
        g = HIPAAVehicleGenerator(SEED)
        records = g.generate_batch(count_per_mode=2)
        amb = [r for r in records if "ambulance_transport" in r.record_id]
        assert amb
        for r in amb:
            vin_spans = [s for s in r.gold_spans if s.category == "VIN"]
            plate_spans = [s for s in r.gold_spans if "LICENSE_PLATE" in s.category]
            assert vin_spans, f"{r.record_id} ambulance missing VIN span"
            assert plate_spans, f"{r.record_id} ambulance missing plate span"

    def test_vanity_plate_distinct_category(self):
        g = HIPAAVehicleGenerator(SEED)
        records = g.generate_batch(count_per_mode=2)
        vanity = [r for r in records if "vanity_plate" in r.record_id]
        assert vanity
        for r in vanity:
            vp_spans = [s for s in r.gold_spans if s.category == "LICENSE_PLATE_VANITY"]
            assert vp_spans, f"{r.record_id} vanity plate missing LICENSE_PLATE_VANITY span"

    def test_determinism(self):
        g1 = HIPAAVehicleGenerator(SEED)
        g2 = HIPAAVehicleGenerator(SEED)
        r1 = g1.generate_batch(count_per_mode=1)
        r2 = g2.generate_batch(count_per_mode=1)
        for a, b in zip(r1, r2):
            assert a.text == b.text
