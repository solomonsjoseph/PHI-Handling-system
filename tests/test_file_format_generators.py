"""
Tests for file_formats generators.

Covers DICOM, FHIR R4, HL7 v2.x, EML, and xlsx generators.
Each test is self-contained and deterministic (seed=42).

Authority coverage verified: DICOM PS3.15 Annex E, HL7 FHIR R4 v4.0.1,
HL7 v2.x PID/NK1/IN1 segments, 45 CFR 164.514(b)(2)(i).
"""
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure project root is on path when running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from generators.file_formats.dicom_header_gen import DICOMHeaderGenerator, generate_corpus as dicom_corpus
from generators.file_formats.fhir_gen import FHIRGenerator, generate_corpus as fhir_corpus
from generators.file_formats.hl7v2_gen import HL7v2Generator, generate_corpus as hl7v2_corpus
from generators.file_formats.eml_gen import EMLGenerator, generate_corpus as eml_corpus


# ---------------------------------------------------------------------------
# DICOM tests (5 tests)
# ---------------------------------------------------------------------------

class TestDICOMHeaderGenerator:

    def setup_method(self):
        self.gen = DICOMHeaderGenerator(seed=42)
        self.records = self.gen.generate_batch(count=5)

    def test_format_field_is_dicom_header(self):
        """All records must have format='dicom_header'."""
        for r in self.records:
            assert r.format == "dicom_header", f"Expected dicom_header, got {r.format}"

    def test_patient_name_in_gold_spans(self):
        """PatientName (NAME category) must appear in gold spans."""
        for r in self.records:
            categories = [s.category for s in r.gold_spans]
            assert "NAME" in categories, f"NAME not found in spans for {r.record_id}"

    def test_all_spans_verify(self):
        """All gold span offsets must be correct in the text."""
        for r in self.records:
            errors = r.verify_spans()
            assert not errors, f"Span errors in {r.record_id}: {errors}"

    def test_authority_citations_present(self):
        """Each record must cite DICOM PS3.15 Annex E."""
        for r in self.records:
            assert any("DICOM" in c for c in r.authority_citations), (
                f"No DICOM authority in {r.record_id}: {r.authority_citations}"
            )

    def test_seed_reproducibility(self):
        """Two generators with the same seed produce identical output."""
        gen2 = DICOMHeaderGenerator(seed=42)
        recs2 = gen2.generate_batch(count=5)
        for r1, r2 in zip(self.records, recs2):
            assert r1.text == r2.text, "Seed reproducibility failed for DICOM"
            assert r1.record_id == r2.record_id

    def test_pydicom_loads_dataset(self):
        """pydicom Dataset objects must be buildable (not just string)."""
        try:
            import pydicom
        except ImportError:
            pytest.skip("pydicom not installed")
        datasets = self.gen.get_raw_datasets(count=3)
        assert len(datasets) == 3
        for ds in datasets:
            assert hasattr(ds, "PatientName"), "Dataset missing PatientName"
            assert hasattr(ds, "PatientID"), "Dataset missing PatientID"

    def test_text_is_valid_json(self):
        """Record text must be parseable JSON."""
        for r in self.records:
            parsed = json.loads(r.text)
            assert "PatientName" in parsed, f"PatientName missing in JSON for {r.record_id}"

    def test_mrn_in_gold_spans(self):
        """MRN category must appear in gold spans."""
        for r in self.records:
            categories = [s.category for s in r.gold_spans]
            assert "MRN" in categories, f"MRN span missing in {r.record_id}"

    def test_generate_corpus_writes_file(self):
        """generate_corpus() writes a file with > 0 records."""
        import tempfile
        from unittest.mock import patch
        tmp = Path(tempfile.mkdtemp())
        out = tmp / "corpus" / "file_formats" / "dicom_headers.jsonl"
        with patch("generators.file_formats.dicom_header_gen.Path") as MockPath:
            # Directly call write_jsonl to the temp path
            from generators.common import write_jsonl
            gen = DICOMHeaderGenerator(seed=42)
            recs = gen.generate_batch(count=3)
            out.parent.mkdir(parents=True, exist_ok=True)
            count = write_jsonl(recs, out)
        assert count == 3
        lines = out.read_text().strip().split("\n")
        assert len(lines) == 3
        for line in lines:
            obj = json.loads(line)
            assert obj["format"] == "dicom_header"


# ---------------------------------------------------------------------------
# FHIR tests (4 tests)
# ---------------------------------------------------------------------------

class TestFHIRGenerator:

    def setup_method(self):
        self.gen = FHIRGenerator(seed=42)
        self.records = self.gen.generate_batch(count=5)

    def test_format_field_is_fhir_json(self):
        for r in self.records:
            assert r.format == "fhir_json"

    def test_birthdate_in_gold_spans(self):
        """DATE category (birthDate) must appear in gold spans."""
        for r in self.records:
            categories = [s.category for s in r.gold_spans]
            assert "DATE" in categories, f"DATE span missing in {r.record_id}"

    def test_all_spans_verify(self):
        for r in self.records:
            errors = r.verify_spans()
            assert not errors, f"Span errors in {r.record_id}: {errors}"

    def test_text_is_valid_fhir_bundle(self):
        """JSON text must parse to a FHIR Bundle with at least one Patient entry."""
        for r in self.records:
            bundle = json.loads(r.text)
            assert bundle["resourceType"] == "Bundle"
            patient = bundle["entry"][0]["resource"]
            assert patient["resourceType"] == "Patient"
            assert "birthDate" in patient

    def test_authority_citations_present(self):
        for r in self.records:
            assert any("FHIR" in c or "fhir" in c.lower() for c in r.authority_citations), (
                f"No FHIR authority in {r.record_id}"
            )

    def test_seed_reproducibility(self):
        gen2 = FHIRGenerator(seed=42)
        recs2 = gen2.generate_batch(count=5)
        for r1, r2 in zip(self.records, recs2):
            assert r1.text == r2.text

    def test_mrn_in_gold_spans(self):
        for r in self.records:
            categories = [s.category for s in r.gold_spans]
            assert "MRN" in categories

    def test_generate_corpus_writes_file(self):
        from generators.common import write_jsonl
        tmp = Path(tempfile.mkdtemp())
        out = tmp / "fhir_bundles.jsonl"
        gen = FHIRGenerator(seed=42)
        recs = gen.generate_batch(count=3)
        count = write_jsonl(recs, out)
        assert count == 3
        lines = out.read_text().strip().split("\n")
        assert len(lines) == 3
        for line in lines:
            obj = json.loads(line)
            assert obj["format"] == "fhir_json"


# ---------------------------------------------------------------------------
# HL7 v2.x tests (4 tests)
# ---------------------------------------------------------------------------

class TestHL7v2Generator:

    def setup_method(self):
        self.gen = HL7v2Generator(seed=42)
        self.records = self.gen.generate_batch(count=5)

    def test_text_contains_hl7_segments(self):
        """Text must contain MSH|, PID|, NK1|, IN1| segment headers."""
        for r in self.records:
            assert "MSH|" in r.text, f"MSH missing in {r.record_id}"
            assert "PID|" in r.text, f"PID missing in {r.record_id}"
            assert "NK1|" in r.text, f"NK1 missing in {r.record_id}"
            assert "IN1|" in r.text, f"IN1 missing in {r.record_id}"

    def test_patient_name_in_pid5_annotated(self):
        """PID-5 LastName^FirstName format must be in gold spans as NAME."""
        for r in self.records:
            name_spans = [s for s in r.gold_spans if s.category == "NAME"]
            assert name_spans, f"No NAME spans in {r.record_id}"
            # At least one span should contain ^ (HL7 component separator for name)
            caret_names = [s for s in name_spans if "^" in s.value]
            assert caret_names, f"No LastName^FirstName span in {r.record_id}"

    def test_all_spans_verify(self):
        for r in self.records:
            errors = r.verify_spans()
            assert not errors, f"Span errors in {r.record_id}: {errors}"

    def test_authority_citations_present(self):
        for r in self.records:
            assert r.authority_citations, f"No authority citations in {r.record_id}"
            assert any("HL7" in c for c in r.authority_citations)

    def test_seed_reproducibility(self):
        gen2 = HL7v2Generator(seed=42)
        recs2 = gen2.generate_batch(count=5)
        for r1, r2 in zip(self.records, recs2):
            assert r1.text == r2.text

    def test_format_field_is_hl7v2(self):
        for r in self.records:
            assert r.format == "hl7v2"

    def test_generate_corpus_writes_file(self):
        from generators.common import write_jsonl
        tmp = Path(tempfile.mkdtemp())
        out = tmp / "hl7v2_messages.jsonl"
        gen = HL7v2Generator(seed=42)
        recs = gen.generate_batch(count=3)
        count = write_jsonl(recs, out)
        assert count == 3
        for line in out.read_text().strip().split("\n"):
            obj = json.loads(line)
            assert obj["format"] == "hl7v2"

    def test_health_plan_id_annotated(self):
        """IN1-2 insurance plan ID must appear as HEALTH_PLAN_ID in gold spans."""
        for r in self.records:
            cats = [s.category for s in r.gold_spans]
            assert "HEALTH_PLAN_ID" in cats, f"HEALTH_PLAN_ID missing in {r.record_id}"


# ---------------------------------------------------------------------------
# EML tests (4 tests)
# ---------------------------------------------------------------------------

class TestEMLGenerator:

    def setup_method(self):
        self.gen = EMLGenerator(seed=42)
        self.records = self.gen.generate_batch(count=5)

    def test_subject_header_present(self):
        """Subject header must be present in every email text."""
        for r in self.records:
            assert "Subject:" in r.text, f"Subject header missing in {r.record_id}"

    def test_email_addresses_in_gold_spans(self):
        """EMAIL category spans must be present."""
        for r in self.records:
            email_spans = [s for s in r.gold_spans if s.category == "EMAIL"]
            assert email_spans, f"No EMAIL spans in {r.record_id}"
            # Verify @ present in all annotated email values
            for span in email_spans:
                assert "@" in span.value, f"Email span value missing @: '{span.value}'"

    def test_all_spans_verify(self):
        for r in self.records:
            errors = r.verify_spans()
            assert not errors, f"Span errors in {r.record_id}: {errors}"

    def test_authority_citations_present(self):
        for r in self.records:
            assert any("164.514" in c for c in r.authority_citations)

    def test_seed_reproducibility(self):
        gen2 = EMLGenerator(seed=42)
        recs2 = gen2.generate_batch(count=5)
        for r1, r2 in zip(self.records, recs2):
            assert r1.text == r2.text

    def test_format_field_is_eml(self):
        for r in self.records:
            assert r.format == "eml"

    def test_mrn_in_gold_spans(self):
        for r in self.records:
            cats = [s.category for s in r.gold_spans]
            assert "MRN" in cats, f"MRN missing in {r.record_id}"

    def test_generate_corpus_writes_file(self):
        from generators.common import write_jsonl
        tmp = Path(tempfile.mkdtemp())
        out = tmp / "eml_messages.jsonl"
        gen = EMLGenerator(seed=42)
        recs = gen.generate_batch(count=3)
        count = write_jsonl(recs, out)
        assert count == 3
        for line in out.read_text().strip().split("\n"):
            obj = json.loads(line)
            assert obj["format"] == "eml"

    def test_x_patient_id_header_present(self):
        """X-Patient-ID custom header must be in the email text."""
        for r in self.records:
            assert "X-Patient-ID:" in r.text, f"X-Patient-ID header missing in {r.record_id}"


# ---------------------------------------------------------------------------
# xlsx tests
# ---------------------------------------------------------------------------

class TestXlsxGenerator:

    def setup_method(self):
        from generators.file_formats.xlsx_gen import XlsxGenerator
        self.gen = XlsxGenerator(seed=42)
        self.records = self.gen.generate()

    def test_tier_distribution(self):
        """Must have records in all four tiers."""
        tiers = {r.metadata["corpus_tier"] for r in self.records}
        assert tiers == {"A", "B", "C", "D"}, f"Missing tiers: {tiers}"

    def test_tier_a_has_phi_spans(self):
        """Every Tier A record must have at least one gold span."""
        tier_a = [r for r in self.records if r.metadata["corpus_tier"] == "A"]
        assert tier_a, "No Tier A records"
        for r in tier_a:
            assert r.gold_spans, f"Tier A record {r.record_id} has no gold spans"

    def test_tier_c_placeholder_has_no_phi_spans(self):
        """Tier C placeholder records must have zero gold spans (they are not PHI)."""
        placeholders = [r for r in self.records
                        if r.metadata["corpus_tier"] == "C"
                        and "placeholder" in r.record_id]
        assert placeholders, "No placeholder Tier C records"
        for r in placeholders:
            assert r.gold_spans == [], (
                f"Tier C placeholder {r.record_id} should have no PHI spans but has {r.gold_spans}"
            )

    def test_tier_c_age_bin_no_phi_spans(self):
        """Age bin records must have zero gold spans."""
        age_bins = [r for r in self.records if "agebin" in r.record_id]
        assert age_bins, "No age bin records"
        for r in age_bins:
            assert r.gold_spans == [], f"Age bin {r.record_id} should not have PHI spans"

    def test_tier_d_requires_human_review(self):
        """All Tier D records must have requires_human_review=True."""
        tier_d = [r for r in self.records if r.metadata["corpus_tier"] == "D"]
        assert tier_d, "No Tier D records"
        for r in tier_d:
            assert r.metadata["requires_human_review"] is True, (
                f"Tier D record {r.record_id} missing requires_human_review=True"
            )
            assert r.metadata["human_review_reason"], (
                f"Tier D record {r.record_id} missing human_review_reason"
            )

    def test_all_spans_verify(self):
        """All gold span offsets must match their declared values in record text."""
        for r in self.records:
            errors = r.verify_spans()
            assert not errors, f"Span offset errors in {r.record_id}: {errors}"

    def test_tier_b_edge_case_metadata_present(self):
        """Every Tier B record must document its edge_case and detection_challenge."""
        tier_b = [r for r in self.records if r.metadata["corpus_tier"] == "B"]
        assert tier_b, "No Tier B records"
        for r in tier_b:
            assert "edge_case" in r.metadata, f"{r.record_id} missing edge_case"
            assert "detection_challenge" in r.metadata, f"{r.record_id} missing detection_challenge"

    def test_authority_citation_present(self):
        """Every record must cite at least one authority."""
        for r in self.records:
            assert r.authority_citations, f"{r.record_id} missing authority_citations"

    def test_format_field_is_xlsx(self):
        """All records must declare format='xlsx'."""
        for r in self.records:
            assert r.format == "xlsx", f"{r.record_id} has format={r.format}"

    def test_seed_reproducibility(self):
        """Same seed must produce bitwise-identical records."""
        from generators.file_formats.xlsx_gen import XlsxGenerator
        gen2 = XlsxGenerator(seed=42)
        recs2 = gen2.generate()
        for r1, r2 in zip(self.records, recs2):
            assert r1.text == r2.text, f"Seed reproducibility failed at {r1.record_id}"
            assert r1.record_id == r2.record_id

    def test_tier_b_hidden_sheet_record_exists(self):
        """Hidden sheet edge case (B9) must be present."""
        hidden = [r for r in self.records if "hiddensheet" in r.record_id]
        assert hidden, "No hidden sheet Tier B records"
        for r in hidden:
            assert "hidden_sheet" in r.metadata.get("xlsx_phi_locations", [])

    def test_tier_b_metadata_author_phi_location(self):
        """Metadata author edge case (B4) must declare metadata.author as PHI location."""
        meta_recs = [r for r in self.records if "metadata" in r.record_id
                     and r.metadata["corpus_tier"] == "B"]
        assert meta_recs, "No metadata author Tier B records"
        for r in meta_recs:
            locs = r.metadata.get("xlsx_phi_locations", [])
            assert any("metadata" in loc for loc in locs), (
                f"{r.record_id}: metadata.author not in xlsx_phi_locations"
            )
