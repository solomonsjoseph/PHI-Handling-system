"""
India DPDPA Rule 14 regulatory-specific identifier generator.

Covers the four identifier categories named explicitly in DPDP Rules 2025 Rule 14
that do not map to a standard national document format:
  - DPDPA_CUSTOMER_ID_FILE_NUMBER   (customer ID file number)
  - DPDPA_ACQUISITION_FORM_NUMBER   (acquisition form number)
  - DPDPA_APPLICATION_REFERENCE_NUMBER (application reference number)
  - DPDPA_ENROLMENT_ID              (enrolment ID)

These identifiers are structural gaps in all rule-based PHI detection tools
(Presidio, AWS Comprehend Medical) because they have no fixed format -- they
are defined by context (assigned by a Data Fiduciary) rather than by a national
standard. Detection regime is therefore contextual_ner_required for all four.

Primary authority: DPDP Rules 2025 Rule 14 (G.S.R. 846(E), notified 2025-11-13)
Secondary: DPDPA 2023 Act 22; IT Act SPDI Rules 2011 Rule 3

This file is the DPDPA regulatory layer. Format-defined national identifiers
(Aadhaar, PAN, ABHA, etc.) are in generators/in/in_identifiers.py.
"""
from __future__ import annotations

import random
import string
import sys
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from generators.common import (
    AUTH_DPDPA_RULES_14,
    AUTH_SPDI_RULE_3,
    DETECTION_REGIME_NER,
    LAYER_INDIA,
    DeterministicGenerator,
    Record,
    write_jsonl,
)

AUTH_DPDPA_ACT_22 = "DPDPA 2023 Act 22 (definition of personal data)"

# ---------------------------------------------------------------------------
# Format helpers -- all formats are fiduciary-assigned; no national standard.
# These patterns are illustrative of common enterprise conventions.
# ---------------------------------------------------------------------------

def _customer_id_file_number(rng: random.Random) -> str:
    """Customer ID file number: alphanumeric, 8-14 chars, fiduciary-assigned."""
    prefix = rng.choice(["CIF", "CUST", "KYC", "ACC"])
    digits = "".join(str(rng.randint(0, 9)) for _ in range(rng.randint(7, 10)))
    return f"{prefix}{digits}"


def _acquisition_form_number(rng: random.Random) -> str:
    """Acquisition form number: AF-YYYY-NNNNNN format (common banking convention)."""
    year = rng.randint(2015, 2025)
    seq = rng.randint(100000, 999999)
    return f"AF-{year}-{seq}"


def _application_reference_number(rng: random.Random) -> str:
    """Application reference number: ARN/YYYY/MM/NNNNNN."""
    year = rng.randint(2018, 2025)
    month = rng.randint(1, 12)
    seq = rng.randint(100000, 999999)
    return f"ARN/{year}/{month:02d}/{seq}"


def _enrolment_id(rng: random.Random) -> str:
    """Enrolment ID: ENR-NNNNNNNNNN (10-digit numeric suffix, scheme-assigned)."""
    digits = "".join(str(rng.randint(0, 9)) for _ in range(10))
    return f"ENR-{digits}"


# Sample name pool for realistic text context
_NAMES = [
    "Priya Sharma", "Rahul Mehta", "Anjali Singh", "Vikram Patel",
    "Sunita Reddy", "Arun Kumar", "Deepa Nair", "Suresh Iyer",
    "Kavitha Rao", "Mohan Gupta", "Lakshmi Pillai", "Rajesh Bose",
]

_FIDUCIARIES = [
    "IndiaFirst Life Insurance", "Axis Bank", "HDFC Securities",
    "Reliance Health Insurance", "SBI Life", "ICICI Prudential",
    "Bajaj Allianz General", "Kotak Mahindra Bank",
]

_CONTEXTS_CUSTOMER_ID = [
    "Customer KYC records for {name} (Customer ID File Number: {cif}) have been "
    "flagged for periodic re-verification by {entity} under DPDPA 2023 compliance audit.",
    "Loan application for {name} linked to Customer ID File Number {cif} at {entity} "
    "requires consent refresh per DPDP Rules 2025 Rule 14.",
    "Data principal {name} submitted a nomination change request. Customer ID File "
    "Number {cif} updated in the {entity} system on 2025-03-14.",
    "Credit bureau enquiry for Customer ID File Number {cif} ({name}) initiated by "
    "{entity} pursuant to written consent dated 2025-01-20.",
]

_CONTEXTS_ACQUISITION_FORM = [
    "New account opening for {name} completed via Acquisition Form Number {afn}. "
    "{entity} processed consent under DPDPA 2023 Rule 5.",
    "Acquisition Form Number {afn} for {name} at {entity} contains sensitive personal "
    "data (health history, income). Retention period: 7 years per Rule 6.",
    "Branch manager at {entity} confirmed Acquisition Form Number {afn} signed by "
    "{name} on 2024-11-03 meets DPDPA 2023 explicit consent requirements.",
    "Data deletion request by {name} references Acquisition Form Number {afn} submitted "
    "to {entity}; right-to-erasure acknowledged under DPDPA 2023 s.13.",
]

_CONTEXTS_ARN = [
    "Application Reference Number {arn} for {name} submitted to {entity}. Status: "
    "pending verification. Processing SLA: 72 hours per Rule 7.",
    "{entity} notified {name} that Application Reference Number {arn} has progressed "
    "to medical underwriting stage. PHI shared with third-party assessor under DUA.",
    "Grievance filed by {name} against Application Reference Number {arn}. {entity} "
    "Data Protection Officer escalated to Appellate Board within 48 hours.",
    "Insurance policy issued for {name}. Application Reference Number {arn} archived "
    "per Rule 6 with 1-year minimum log retention.",
]

_CONTEXTS_ENROLMENT = [
    "Enrolment ID {eid} assigned to {name} during onboarding at {entity}. Biometric "
    "consent captured under DPDPA 2023 Schedule II condition 3.",
    "{name} requested portability of health records linked to Enrolment ID {eid} from "
    "{entity}. Data Principal Rights exercised per DPDPA 2023 s.11.",
    "Enrolment ID {eid} for {name} at {entity} was suspended following breach "
    "notification under DPDP Rules 2025 Rule 7 (72-hour Board notification).",
    "Re-enrolment of {name} completed. Previous Enrolment ID {eid} de-activated in "
    "{entity} system. New ID issued per Rule 14 identifier lifecycle policy.",
]


class IndiaDPDPAGenerator(DeterministicGenerator):
    """DPDPA Rule 14 regulatory-specific identifier records.

    Generates 4 identifier types, each with contextual clinical/administrative
    records embedding the identifier in realistic Data Fiduciary text.
    Detection regime is contextual_ner_required for all four: no fixed format
    standard exists; only context identifies these as Rule 14 identifiers.

    Authority: DPDP Rules 2025 Rule 14 (G.S.R. 846(E), notified 2025-11-13)
    """

    def generate_batch(self, count_per_type: int = 4) -> List[Record]:
        records: List[Record] = []
        records.extend(self._gen_customer_id_file_number(count_per_type))
        records.extend(self._gen_acquisition_form_number(count_per_type))
        records.extend(self._gen_application_reference_number(count_per_type))
        records.extend(self._gen_enrolment_id(count_per_type))
        return records

    def _gen_customer_id_file_number(self, count: int) -> List[Record]:
        rng = self.fresh("customer_id_file_number")
        records = []
        contexts = _CONTEXTS_CUSTOMER_ID
        for i in range(count):
            name = rng.choice(_NAMES)
            entity = rng.choice(_FIDUCIARIES)
            cif = _customer_id_file_number(rng)
            tmpl = contexts[i % len(contexts)]
            text = tmpl.format(name=name, cif=cif, entity=entity)
            spans = self.annotate(text, [
                (cif, "DPDPA_CUSTOMER_ID", None, "in", AUTH_DPDPA_RULES_14, DETECTION_REGIME_NER),
                (name, "NAME_PATIENT", "A", "in", AUTH_DPDPA_RULES_14, DETECTION_REGIME_NER),
            ])
            rec = Record(
                record_id=self.record_id("in_dpdpa_cif", i),
                text=text,
                gold_spans=spans,
                layer=LAYER_INDIA,
                jurisdiction="in",
                detection_regime=DETECTION_REGIME_NER,
                de_id_tier="identifiable",
                context="compliance",
                authority_citations=[AUTH_DPDPA_RULES_14, AUTH_DPDPA_ACT_22, AUTH_SPDI_RULE_3],
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"CIF record {i}: {errors}")
            records.append(rec)
        return records

    def _gen_acquisition_form_number(self, count: int) -> List[Record]:
        rng = self.fresh("acquisition_form_number")
        records = []
        contexts = _CONTEXTS_ACQUISITION_FORM
        for i in range(count):
            name = rng.choice(_NAMES)
            entity = rng.choice(_FIDUCIARIES)
            afn = _acquisition_form_number(rng)
            tmpl = contexts[i % len(contexts)]
            text = tmpl.format(name=name, afn=afn, entity=entity)
            spans = self.annotate(text, [
                (afn, "DPDPA_ACQUISITION_FORM", None, "in", AUTH_DPDPA_RULES_14, DETECTION_REGIME_NER),
                (name, "NAME_PATIENT", "A", "in", AUTH_DPDPA_RULES_14, DETECTION_REGIME_NER),
            ])
            rec = Record(
                record_id=self.record_id("in_dpdpa_afn", i),
                text=text,
                gold_spans=spans,
                layer=LAYER_INDIA,
                jurisdiction="in",
                detection_regime=DETECTION_REGIME_NER,
                de_id_tier="identifiable",
                context="compliance",
                authority_citations=[AUTH_DPDPA_RULES_14, AUTH_DPDPA_ACT_22],
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"AFN record {i}: {errors}")
            records.append(rec)
        return records

    def _gen_application_reference_number(self, count: int) -> List[Record]:
        rng = self.fresh("application_reference_number")
        records = []
        contexts = _CONTEXTS_ARN
        for i in range(count):
            name = rng.choice(_NAMES)
            entity = rng.choice(_FIDUCIARIES)
            arn = _application_reference_number(rng)
            tmpl = contexts[i % len(contexts)]
            text = tmpl.format(name=name, arn=arn, entity=entity)
            spans = self.annotate(text, [
                (arn, "DPDPA_APP_REF", None, "in", AUTH_DPDPA_RULES_14, DETECTION_REGIME_NER),
                (name, "NAME_PATIENT", "A", "in", AUTH_DPDPA_RULES_14, DETECTION_REGIME_NER),
            ])
            rec = Record(
                record_id=self.record_id("in_dpdpa_arn", i),
                text=text,
                gold_spans=spans,
                layer=LAYER_INDIA,
                jurisdiction="in",
                detection_regime=DETECTION_REGIME_NER,
                de_id_tier="identifiable",
                context="compliance",
                authority_citations=[AUTH_DPDPA_RULES_14, AUTH_DPDPA_ACT_22],
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"ARN record {i}: {errors}")
            records.append(rec)
        return records

    def _gen_enrolment_id(self, count: int) -> List[Record]:
        rng = self.fresh("enrolment_id")
        records = []
        contexts = _CONTEXTS_ENROLMENT
        for i in range(count):
            name = rng.choice(_NAMES)
            entity = rng.choice(_FIDUCIARIES)
            eid = _enrolment_id(rng)
            tmpl = contexts[i % len(contexts)]
            text = tmpl.format(name=name, eid=eid, entity=entity)
            spans = self.annotate(text, [
                (eid, "DPDPA_ENROLMENT_ID", None, "in", AUTH_DPDPA_RULES_14, DETECTION_REGIME_NER),
                (name, "NAME_PATIENT", "A", "in", AUTH_DPDPA_RULES_14, DETECTION_REGIME_NER),
            ])
            rec = Record(
                record_id=self.record_id("in_dpdpa_eid", i),
                text=text,
                gold_spans=spans,
                layer=LAYER_INDIA,
                jurisdiction="in",
                detection_regime=DETECTION_REGIME_NER,
                de_id_tier="identifiable",
                context="compliance",
                authority_citations=[AUTH_DPDPA_RULES_14, AUTH_DPDPA_ACT_22],
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"EID record {i}: {errors}")
            records.append(rec)
        return records


def generate_corpus(seed: int = 42, count_per_type: int = 4) -> List[Record]:
    """Generate DPDPA Rule 14 corpus and write to corpus/in/india_dpdpa.jsonl.

    Authority: DPDP Rules 2025 Rule 14 (G.S.R. 846(E), notified 2025-11-13)
    """
    gen = IndiaDPDPAGenerator(seed=seed)
    records = gen.generate_batch(count_per_type=count_per_type)
    repo_root = Path(__file__).resolve().parents[2]
    out_path = repo_root / "corpus" / "in" / "india_dpdpa.jsonl"
    count = write_jsonl(records, out_path)
    total_spans = sum(len(r.gold_spans) for r in records)
    print(f"India DPDPA corpus: {count} records, {total_spans} spans -> {out_path}")
    return records


if __name__ == "__main__":
    generate_corpus()
