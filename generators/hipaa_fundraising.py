"""
HIPAA Fundraising context generator.

Authority: 45 CFR 164.514(f)

164.514(f) permits a covered entity to use or disclose the following PHI
for fundraising purposes WITHOUT patient authorization, provided the Notice
of Privacy Practices describes the possibility of such use:
  (1) Demographic information (name, address, contact information)
  (2) Dates of health care provided
  (3) Department of service
  (4) Treating physician
  (5) Outcome information
  (6) Health insurance status

CRITICAL for context-aware de-identification:
A fundraising communication containing a patient's name and address is NOT
a HIPAA violation -- the permitted use applies. However, the SAME name and
address in a clinical record shared for treatment purposes IS PHI requiring
full de-identification if disclosed outside the covered entity.

A context-unaware detector will produce:
  - FALSE POSITIVES on fundraising records (flagging permitted PHI as violations)
  - The gold spans in fundraising records are annotated as PERMITTED, not violations

Test record classes:
  - fundraising_NNN: fundraising letter/communication with permitted PHI
  - treatment_NNN: same PHI fields in treatment context (NOT fundraising-exempt)
  - crosswalk_NNN: demonstrates same identifier in different contexts changing status
"""
from __future__ import annotations

from typing import List

from .common import (
    AUTH_HIPAA_FUNDRAISING,
    AUTH_HIPAA_SAFE_HARBOR,
    DETECTION_REGIME_NER,
    DETECTION_REGIME_RULE,
    LAYER_HIPAA,
    DeterministicGenerator,
    Record,
)
from .hipaa_safe_harbor import (
    US_FIRST_NAMES,
    US_LAST_NAMES,
    US_CITIES,
    US_STREET_NAMES,
    US_STREET_SUFFIXES,
    us_address,
    us_name,
    us_phone,
)


HOSPITAL_NAMES = [
    "Regional Medical Center",
    "University Hospital",
    "St. Michael's Medical Center",
    "Community Health System",
    "Valley General Hospital",
]

DEPARTMENTS = [
    "Oncology", "Cardiology", "Orthopedics", "Pediatrics",
    "Neurology", "Transplant Medicine", "Rehabilitation",
]

OUTCOMES = [
    "successful treatment", "full recovery", "remission",
    "improved quality of life", "disease management",
]

FOUNDATION_NAMES = [
    "Healing Hearts Foundation",
    "Patient Care Fund",
    "Community Wellness Endowment",
    "Medical Research Foundation",
    "Hope for Healing Fund",
]


class HIPAAFundraisingGenerator(DeterministicGenerator):
    """Generates records testing context-aware PHI detection per 164.514(f).

    Three record classes:
      fundraising_NNN   -- PHI in fundraising context (use is PERMITTED per (f))
      treatment_NNN     -- same PHI fields in treatment context (NOT fundraising-exempt)
      crosswalk_NNN     -- same patient, fundraising vs. treatment, to test context-awareness
    """

    def generate_batch(self, count: int = 20) -> List[Record]:
        records: List[Record] = []
        for i in range(count):
            rng = self.fresh(f"fundraising_{i}")
            records.append(self._gen_fundraising(rng, i))
        for i in range(count // 2):
            rng = self.fresh(f"treatment_{i}")
            records.append(self._gen_treatment_context(rng, i))
        for i in range(count // 4):
            rng = self.fresh(f"crosswalk_{i}")
            fr, tr = self._gen_crosswalk(rng, i)
            records.append(fr)
            records.append(tr)
        return records

    def _gen_fundraising(self, rng, i: int) -> Record:
        """Fundraising letter. Name/address/dates/dept/physician are PERMITTED per (f).

        Gold spans are annotated with context='fundraising' and authority=AUTH_HIPAA_FUNDRAISING.
        A compliant de-id system should NOT redact these in the fundraising context.
        """
        patient = us_name(rng)
        street, city, state, _, zip_full = us_address(rng)
        physician = "Dr. " + us_name(rng)
        dept = rng.choice(DEPARTMENTS)
        outcome = rng.choice(OUTCOMES)
        hospital = rng.choice(HOSPITAL_NAMES)
        foundation = rng.choice(FOUNDATION_NAMES)
        month = rng.randint(1, 12)
        year = rng.randint(2022, 2025)
        date_of_service = f"{month:02d}/{year}"

        text = (
            f"Dear {patient},\n"
            f"On behalf of the {hospital} {foundation}, we are reaching out because "
            f"you received care in our {dept} department in {date_of_service} "
            f"under the care of {physician}. "
            f"Your {outcome} is an inspiration to our entire team.\n"
            f"Your address on file: {street}, {city}, {state} {zip_full}.\n"
            f"We hope you will consider a gift to support future patients in the "
            f"{dept} program. Contributions are tax-deductible.\n"
            f"[Fundraising use authorized per 45 CFR 164.514(f). "
            f"No authorization required for this disclosure.]"
        )
        spans = [
            (patient, "NAME_PATIENT", "A", "us", AUTH_HIPAA_FUNDRAISING, DETECTION_REGIME_NER),
            (physician, "NAME_PROVIDER", "A", "us", AUTH_HIPAA_FUNDRAISING, DETECTION_REGIME_NER),
            (street, "ADDRESS_STREET", "B", "us", AUTH_HIPAA_FUNDRAISING, DETECTION_REGIME_NER),
            (city, "ADDRESS_CITY", "B", "us", AUTH_HIPAA_FUNDRAISING, DETECTION_REGIME_NER),
            (zip_full, "ADDRESS_ZIP", "B", "us", AUTH_HIPAA_FUNDRAISING, DETECTION_REGIME_NER),
            (date_of_service, "DATE_SERVICE", "C", "us", AUTH_HIPAA_FUNDRAISING, DETECTION_REGIME_NER),
            (dept, "DEPARTMENT", "R", "us", AUTH_HIPAA_FUNDRAISING, DETECTION_REGIME_NER),
        ]
        return Record(
            record_id=f"fundraising_{i:04d}",
            text=text,
            gold_spans=self.annotate(text, spans),
            layer=LAYER_HIPAA,
            jurisdiction="us",
            detection_regime=DETECTION_REGIME_NER,
            de_id_tier="identifiable",
            context="fundraising",
            format="text",
            authority_citations=[AUTH_HIPAA_FUNDRAISING],
            metadata={
                "fundraising_class": "fundraising_letter",
                "phi_use_permitted": True,
                "authorization_required": False,
                "permitted_fields": [
                    "demographic", "dates_of_care", "department",
                    "treating_physician", "outcome", "insurance_status",
                ],
                "note": (
                    "PHI here is PERMITTED per 164.514(f). A context-unaware "
                    "detector that flags name/address/dates in this record "
                    "produces a FALSE POSITIVE."
                ),
            },
        )

    def _gen_treatment_context(self, rng, i: int) -> Record:
        """Same PHI fields in a treatment context -- NOT fundraising-exempt.

        If this record were disclosed outside the covered entity for purposes
        other than treatment/payment/operations, it requires authorization.
        """
        patient = us_name(rng)
        street, city, state, _, zip_full = us_address(rng)
        physician = "Dr. " + us_name(rng)
        dept = rng.choice(DEPARTMENTS)
        month = rng.randint(1, 12)
        day = rng.randint(1, 28)
        year = rng.randint(2023, 2025)
        admit_date = f"{month:02d}/{day:02d}/{year}"

        text = (
            f"CLINICAL RECORD -- TREATMENT CONTEXT\n"
            f"Patient: {patient}\n"
            f"Address: {street}, {city}, {state} {zip_full}\n"
            f"Treating physician: {physician}\n"
            f"Department: {dept}\n"
            f"Admission date: {admit_date}\n"
            f"[This is a clinical record. PHI (name, address, dates, physician, "
            f"department) is IDENTIFIABLE PHI requiring authorization for any "
            f"disclosure outside TPO purposes. 164.514(f) does NOT apply here.]"
        )
        spans = [
            (patient, "NAME_PATIENT", "A", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
            (physician, "NAME_PROVIDER", "A", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
            (street, "ADDRESS_STREET", "B", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
            (city, "ADDRESS_CITY", "B", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
            (zip_full, "ADDRESS_ZIP", "B", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
            (admit_date, "DATE_ADMIT", "C", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
            (dept, "DEPARTMENT", "R", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
        ]
        return Record(
            record_id=f"treatment_{i:04d}",
            text=text,
            gold_spans=self.annotate(text, spans),
            layer=LAYER_HIPAA,
            jurisdiction="us",
            detection_regime=DETECTION_REGIME_NER,
            de_id_tier="identifiable",
            context="treatment",
            format="text",
            authority_citations=[AUTH_HIPAA_SAFE_HARBOR],
            metadata={
                "fundraising_class": "treatment_record",
                "phi_use_permitted": False,
                "authorization_required": True,
                "note": "164.514(f) does NOT apply. Standard PHI handling required.",
            },
        )

    def _gen_crosswalk(self, rng, i: int) -> tuple[Record, Record]:
        """Same patient in fundraising and treatment context.

        The pair shows that context changes whether PHI disclosure is a violation.
        Both records contain the same name/address/dates but different legal status.
        """
        patient = us_name(rng)
        street, city, state, _, zip_full = us_address(rng)
        physician = "Dr. " + us_name(rng)
        dept = rng.choice(DEPARTMENTS)
        month = rng.randint(1, 12)
        year = rng.randint(2023, 2025)
        date_of_service = f"{month}/{year}"
        hospital = rng.choice(HOSPITAL_NAMES)
        foundation = rng.choice(FOUNDATION_NAMES)

        fr_text = (
            f"Dear {patient}, the {hospital} {foundation} thanks you for your "
            f"{dept} care in {date_of_service} with {physician}. "
            f"Sending to: {street}, {city}, {state} {zip_full}. "
            f"[FUNDRAISING -- 164.514(f) permits this disclosure without authorization.]"
        )
        fr_spans = [
            (patient, "NAME_PATIENT", "A", "us", AUTH_HIPAA_FUNDRAISING, DETECTION_REGIME_NER),
            (physician, "NAME_PROVIDER", "A", "us", AUTH_HIPAA_FUNDRAISING, DETECTION_REGIME_NER),
            (city, "ADDRESS_CITY", "B", "us", AUTH_HIPAA_FUNDRAISING, DETECTION_REGIME_NER),
            (zip_full, "ADDRESS_ZIP", "B", "us", AUTH_HIPAA_FUNDRAISING, DETECTION_REGIME_NER),
            (date_of_service, "DATE_SERVICE", "C", "us", AUTH_HIPAA_FUNDRAISING, DETECTION_REGIME_NER),
        ]
        fr_rec = Record(
            record_id=f"crosswalk_fr_{i:04d}",
            text=fr_text,
            gold_spans=self.annotate(fr_text, fr_spans),
            layer=LAYER_HIPAA,
            jurisdiction="us",
            detection_regime=DETECTION_REGIME_NER,
            de_id_tier="identifiable",
            context="fundraising",
            format="text",
            authority_citations=[AUTH_HIPAA_FUNDRAISING],
            metadata={
                "fundraising_class": "crosswalk_fundraising",
                "phi_use_permitted": True,
                "paired_with": f"crosswalk_tr_{i:04d}",
            },
        )

        tr_text = (
            f"DISCHARGE SUMMARY. Patient: {patient}. Address: {street}, "
            f"{city}, {state} {zip_full}. Physician: {physician}. "
            f"Dept: {dept}. Date of service: {date_of_service}. "
            f"[TREATMENT RECORD -- 164.514(f) DOES NOT apply. "
            f"Disclosure requires authorization or TPO basis.]"
        )
        tr_spans = [
            (patient, "NAME_PATIENT", "A", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
            (physician, "NAME_PROVIDER", "A", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
            (street, "ADDRESS_STREET", "B", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
            (city, "ADDRESS_CITY", "B", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
            (zip_full, "ADDRESS_ZIP", "B", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
            (date_of_service, "DATE_SERVICE", "C", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
        ]
        tr_rec = Record(
            record_id=f"crosswalk_tr_{i:04d}",
            text=tr_text,
            gold_spans=self.annotate(tr_text, tr_spans),
            layer=LAYER_HIPAA,
            jurisdiction="us",
            detection_regime=DETECTION_REGIME_NER,
            de_id_tier="identifiable",
            context="treatment",
            format="text",
            authority_citations=[AUTH_HIPAA_SAFE_HARBOR],
            metadata={
                "fundraising_class": "crosswalk_treatment",
                "phi_use_permitted": False,
                "paired_with": f"crosswalk_fr_{i:04d}",
            },
        )
        return fr_rec, tr_rec
