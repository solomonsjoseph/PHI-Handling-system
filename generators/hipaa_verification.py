"""
HIPAA Disclosure Verification audit log generator.

Authority: 45 CFR 164.514(h)

164.514(h) requires covered entities to verify the identity and authority
of persons who request PHI disclosures under 164.512 (uses and disclosures
for which authorization or opportunity to agree/object is not required).

Verification scenarios covered:
  (h)(1) Public health authority
  (h)(2) Law enforcement
  (h)(3) Health oversight agency
  (h)(4) Judicial/administrative proceedings
  (h)(5) Coroner/medical examiner
  (h)(6) Correctional institution or law enforcement custodial officer

Audit log records contain PHI about BOTH the requestor (whose identity
is verified) and the patient (whose records were disclosed). De-id systems
must detect PHI in both roles within audit log format text.

Record classes:
  - audit_NNN:        Audit log entry for a completed verified disclosure
  - denied_NNN:       Audit log for a disclosure that was denied (verification failed)
  - subpoena_NNN:     Court-ordered disclosure (164.512(e))
  - law_enforcement:  Law enforcement request (164.512(f)) with verification steps
"""
from __future__ import annotations

from typing import List

from .common import (
    AUTH_HIPAA_VERIFICATION,
    AUTH_HIPAA_SAFE_HARBOR,
    DETECTION_REGIME_NER,
    DETECTION_REGIME_RULE,
    LAYER_HIPAA,
    DeterministicGenerator,
    Record,
)
from .hipaa_safe_harbor import (
    us_name,
    us_phone,
    us_ssn,
    us_mrn,
    us_address,
)


AGENCIES = [
    ("Centers for Disease Control and Prevention", "CDC", "public_health"),
    ("State Department of Health", "SDH", "public_health"),
    ("County Health Department", "CHD", "public_health"),
    ("Office of Inspector General", "OIG", "health_oversight"),
    ("Centers for Medicare and Medicaid Services", "CMS", "health_oversight"),
    ("State Medical Board", "SMB", "health_oversight"),
    ("City Police Department", "CPD", "law_enforcement"),
    ("Federal Bureau of Investigation", "FBI", "law_enforcement"),
    ("State Attorney General Office", "SAG", "law_enforcement"),
    ("County Sheriff Office", "CSO", "law_enforcement"),
]

COURTS = [
    "Superior Court of the State",
    "United States District Court",
    "County Circuit Court",
    "State Court of Appeals",
]

DISCLOSURE_TYPES = [
    "medical record",
    "treatment history",
    "prescription records",
    "laboratory results",
    "imaging records",
]


class HIPAAVerificationGenerator(DeterministicGenerator):
    """Generates audit log records for HIPAA 164.514(h) verification scenarios.

    Four record classes:
      audit_NNN         -- completed, verified disclosure
      denied_NNN        -- denial (verification failed)
      subpoena_NNN      -- court-ordered disclosure (164.512(e))
      law_enf_NNN       -- law enforcement request (164.512(f))
    """

    def generate_batch(self, count: int = 20) -> List[Record]:
        records: List[Record] = []
        for i in range(count):
            rng = self.fresh(f"audit_{i}")
            records.append(self._gen_audit(rng, i))
        for i in range(count // 2):
            rng = self.fresh(f"denied_{i}")
            records.append(self._gen_denied(rng, i))
        for i in range(count // 4):
            rng = self.fresh(f"subpoena_{i}")
            records.append(self._gen_subpoena(rng, i))
        for i in range(count // 4):
            rng = self.fresh(f"law_enf_{i}")
            records.append(self._gen_law_enforcement(rng, i))
        return records

    def _gen_audit(self, rng, i: int) -> Record:
        """Completed verified disclosure audit log entry."""
        agency_name, agency_abbr, category = rng.choice(AGENCIES)
        requestor = us_name(rng)
        requestor_phone = us_phone(rng)
        patient = us_name(rng)
        patient_dob_month = rng.randint(1, 12)
        patient_dob_year = rng.randint(1940, 1990)
        patient_dob = f"{patient_dob_month:02d}/{rng.randint(1,28):02d}/{patient_dob_year}"
        mrn = us_mrn(rng)
        disclosure_type = rng.choice(DISCLOSURE_TYPES)
        log_month = rng.randint(1, 12)
        log_day = rng.randint(1, 28)
        log_year = rng.randint(2023, 2025)
        log_date = f"{log_month:02d}/{log_day:02d}/{log_year}"

        text = (
            f"HIPAA DISCLOSURE AUDIT LOG -- 164.514(h)\n"
            f"Log date: {log_date}\n"
            f"Requestor name: {requestor}\n"
            f"Requestor agency: {agency_name} ({agency_abbr})\n"
            f"Requestor contact: {requestor_phone}\n"
            f"Verification method: official agency identification presented\n"
            f"Verification outcome: APPROVED\n"
            f"Patient name: {patient}\n"
            f"Patient DOB: {patient_dob}\n"
            f"Patient MRN: {mrn}\n"
            f"Disclosed: {disclosure_type}\n"
            f"Legal basis: 164.512 ({category})\n"
            f"Authority: {AUTH_HIPAA_VERIFICATION}"
        )
        spans = [
            (requestor, "NAME_REQUESTOR", "A", "us", AUTH_HIPAA_VERIFICATION, DETECTION_REGIME_NER),
            (requestor_phone, "PHONE_REQUESTOR", "D", "us", AUTH_HIPAA_VERIFICATION, DETECTION_REGIME_NER),
            (patient, "NAME_PATIENT", "A", "us", AUTH_HIPAA_VERIFICATION, DETECTION_REGIME_NER),
            (patient_dob, "DATE_DOB", "C", "us", AUTH_HIPAA_VERIFICATION, DETECTION_REGIME_NER),
            (mrn, "MRN", "H", "us", AUTH_HIPAA_VERIFICATION, DETECTION_REGIME_NER),
        ]
        return Record(
            record_id=f"audit_{i:04d}",
            text=text,
            gold_spans=self.annotate(text, spans),
            layer=LAYER_HIPAA,
            jurisdiction="us",
            detection_regime=DETECTION_REGIME_NER,
            de_id_tier="identifiable",
            context="operations",
            format="text",
            authority_citations=[AUTH_HIPAA_VERIFICATION],
            metadata={
                "audit_class": "completed_disclosure",
                "verification_outcome": "approved",
                "disclosure_basis": category,
                "legal_basis_section": "164.512",
            },
        )

    def _gen_denied(self, rng, i: int) -> Record:
        """Disclosure denied because verification failed.

        The requestor's identity or authority could not be confirmed.
        Records still contain PHI (requestor contact info, patient identifiers
        used during the attempted verification).
        """
        requestor = us_name(rng)
        requestor_phone = us_phone(rng)
        patient = us_name(rng)
        mrn = us_mrn(rng)
        log_month = rng.randint(1, 12)
        log_day = rng.randint(1, 28)
        log_year = rng.randint(2023, 2025)
        log_date = f"{log_month:02d}/{log_day:02d}/{log_year}"
        denial_reason = rng.choice([
            "Unable to verify official agency credentials",
            "Requestor could not confirm proper legal authority",
            "Official badge number not confirmed with issuing agency",
            "No valid court order presented",
            "Requestor refused to identify employing agency",
        ])

        text = (
            f"HIPAA DISCLOSURE AUDIT LOG -- 164.514(h) -- DENIAL\n"
            f"Log date: {log_date}\n"
            f"Requestor name: {requestor}\n"
            f"Requestor contact phone: {requestor_phone}\n"
            f"Verification outcome: DENIED\n"
            f"Denial reason: {denial_reason}\n"
            f"Patient name queried: {patient}\n"
            f"MRN queried: {mrn}\n"
            f"No PHI disclosed. Record retained per 164.530(j) documentation requirements."
        )
        spans = [
            (requestor, "NAME_REQUESTOR", "A", "us", AUTH_HIPAA_VERIFICATION, DETECTION_REGIME_NER),
            (requestor_phone, "PHONE_REQUESTOR", "D", "us", AUTH_HIPAA_VERIFICATION, DETECTION_REGIME_NER),
            (patient, "NAME_PATIENT", "A", "us", AUTH_HIPAA_VERIFICATION, DETECTION_REGIME_NER),
            (mrn, "MRN", "H", "us", AUTH_HIPAA_VERIFICATION, DETECTION_REGIME_NER),
        ]
        return Record(
            record_id=f"denied_{i:04d}",
            text=text,
            gold_spans=self.annotate(text, spans),
            layer=LAYER_HIPAA,
            jurisdiction="us",
            detection_regime=DETECTION_REGIME_NER,
            de_id_tier="identifiable",
            context="operations",
            format="text",
            authority_citations=[AUTH_HIPAA_VERIFICATION],
            metadata={
                "audit_class": "denied_disclosure",
                "verification_outcome": "denied",
                "phi_disclosed": False,
            },
        )

    def _gen_subpoena(self, rng, i: int) -> Record:
        """Court-ordered disclosure per 164.512(e).

        A subpoena or court order removes the verification requirement but
        requires the covered entity to document the legal instrument.
        """
        patient = us_name(rng)
        patient_dob_month = rng.randint(1, 12)
        patient_dob_year = rng.randint(1940, 1990)
        patient_dob = f"{patient_dob_month:02d}/{rng.randint(1,28):02d}/{patient_dob_year}"
        mrn = us_mrn(rng)
        court = rng.choice(COURTS)
        case_number = f"CV-{rng.randint(2020, 2025)}-{rng.randint(10000, 99999)}"
        judge = "Judge " + us_name(rng)
        disclosure_type = rng.choice(DISCLOSURE_TYPES)
        log_month = rng.randint(1, 12)
        log_day = rng.randint(1, 28)
        log_year = rng.randint(2023, 2025)
        log_date = f"{log_month:02d}/{log_day:02d}/{log_year}"

        text = (
            f"HIPAA DISCLOSURE AUDIT LOG -- 164.512(e) COURT ORDER\n"
            f"Log date: {log_date}\n"
            f"Court: {court}\n"
            f"Case number: {case_number}\n"
            f"Presiding judge: {judge}\n"
            f"Patient name: {patient}\n"
            f"Patient DOB: {patient_dob}\n"
            f"Patient MRN: {mrn}\n"
            f"Disclosed: {disclosure_type}\n"
            f"Verification: Court order reviewed and confirmed valid.\n"
            f"164.514(h) verification requirement satisfied by court order.\n"
            f"Authority: 45 CFR 164.512(e)(1)(ii)"
        )
        spans = [
            (judge, "NAME_PROVIDER", "A", "us", AUTH_HIPAA_VERIFICATION, DETECTION_REGIME_NER),
            (patient, "NAME_PATIENT", "A", "us", AUTH_HIPAA_VERIFICATION, DETECTION_REGIME_NER),
            (patient_dob, "DATE_DOB", "C", "us", AUTH_HIPAA_VERIFICATION, DETECTION_REGIME_NER),
            (mrn, "MRN", "H", "us", AUTH_HIPAA_VERIFICATION, DETECTION_REGIME_NER),
            (case_number, "CASE_NUMBER", "R", "us", AUTH_HIPAA_VERIFICATION, DETECTION_REGIME_RULE),
        ]
        return Record(
            record_id=f"subpoena_{i:04d}",
            text=text,
            gold_spans=self.annotate(text, spans),
            layer=LAYER_HIPAA,
            jurisdiction="us",
            detection_regime=DETECTION_REGIME_NER,
            de_id_tier="identifiable",
            context="operations",
            format="text",
            authority_citations=[AUTH_HIPAA_VERIFICATION, "45 CFR 164.512(e)(1)(ii)"],
            metadata={
                "audit_class": "court_ordered_disclosure",
                "verification_mechanism": "court_order",
                "legal_basis_section": "164.512(e)",
            },
        )

    def _gen_law_enforcement(self, rng, i: int) -> Record:
        """Law enforcement request per 164.512(f).

        164.512(f) permits disclosures for law enforcement purposes under
        specific conditions. Verification of law enforcement status is required.
        """
        officer = us_name(rng)
        badge = f"BADGE-{rng.randint(1000, 9999)}"
        dept_name, dept_abbr, _ = rng.choice([
            a for a in AGENCIES if a[2] == "law_enforcement"
        ])
        officer_phone = us_phone(rng)
        patient = us_name(rng)
        patient_dob_month = rng.randint(1, 12)
        patient_dob_year = rng.randint(1940, 1990)
        patient_dob = f"{patient_dob_month:02d}/{rng.randint(1,28):02d}/{patient_dob_year}"
        ssn = us_ssn(rng)
        log_month = rng.randint(1, 12)
        log_day = rng.randint(1, 28)
        log_year = rng.randint(2023, 2025)
        log_date = f"{log_month:02d}/{log_day:02d}/{log_year}"

        text = (
            f"HIPAA DISCLOSURE AUDIT LOG -- 164.512(f) LAW ENFORCEMENT\n"
            f"Log date: {log_date}\n"
            f"Officer: {officer}, Badge: {badge}\n"
            f"Agency: {dept_name} ({dept_abbr})\n"
            f"Officer phone: {officer_phone}\n"
            f"Verification: Badge confirmed with {dept_abbr} dispatch.\n"
            f"Subject name: {patient}\n"
            f"Subject DOB: {patient_dob}\n"
            f"Subject SSN provided by officer: {ssn}\n"
            f"Disclosed: treatment dates and facility (164.512(f)(2)(i)).\n"
            f"Authority: 45 CFR 164.512(f)"
        )
        spans = [
            (officer, "NAME_PROVIDER", "A", "us", AUTH_HIPAA_VERIFICATION, DETECTION_REGIME_NER),
            (badge, "BADGE_NUMBER", "R", "us", AUTH_HIPAA_VERIFICATION, DETECTION_REGIME_RULE),
            (officer_phone, "PHONE", "D", "us", AUTH_HIPAA_VERIFICATION, DETECTION_REGIME_NER),
            (patient, "NAME_PATIENT", "A", "us", AUTH_HIPAA_VERIFICATION, DETECTION_REGIME_NER),
            (patient_dob, "DATE_DOB", "C", "us", AUTH_HIPAA_VERIFICATION, DETECTION_REGIME_NER),
            (ssn, "SSN", "G", "us", AUTH_HIPAA_VERIFICATION, DETECTION_REGIME_RULE),
        ]
        return Record(
            record_id=f"law_enf_{i:04d}",
            text=text,
            gold_spans=self.annotate(text, spans),
            layer=LAYER_HIPAA,
            jurisdiction="us",
            detection_regime=DETECTION_REGIME_NER,
            de_id_tier="identifiable",
            context="operations",
            format="text",
            authority_citations=[AUTH_HIPAA_VERIFICATION, "45 CFR 164.512(f)"],
            metadata={
                "audit_class": "law_enforcement_disclosure",
                "verification_mechanism": "badge_confirmation",
                "legal_basis_section": "164.512(f)",
            },
        )
