"""
HIPAA Limited Data Set (LDS) generator.

Authority: 45 CFR 164.514(e)

The Limited Data Set is the THIRD de-identification tier, between identifiable
PHI and Safe Harbor de-identified data. It is distinct from Safe Harbor in
two critical ways:
  1. Full dates (including DOB, dates of service) are RETAINED.
  2. City, state, and ZIP code are RETAINED.
  3. Ages including over-89 are RETAINED.
  4. Clinical data (diagnosis codes, procedures, etc.) is RETAINED.

The 16 direct identifiers that MUST be excluded per 164.514(e)(2)(i)-(xvi):
  (i)   Names
  (ii)  Postal address (except town/city, State, ZIP)
  (iii) Telephone numbers
  (iv)  Fax numbers
  (v)   Electronic mail addresses
  (vi)  Social security numbers
  (vii) Medical record numbers
  (viii) Health plan beneficiary numbers
  (ix)  Account numbers
  (x)   Certificate/license numbers
  (xi)  Vehicle identifiers and serial numbers, including license plates
  (xii) Device identifiers and serial numbers
  (xiii) Web URLs
  (xiv) IP address numbers
  (xv)  Biometric identifiers
  (xvi) Full-face photographic images and comparable images

A Data Use Agreement (DUA) per 164.514(e)(4) is REQUIRED for LDS disclosures.
The LDS recipient may not identify or contact individuals.

Records produced here fall into three test classes:
  - valid_lds: compliant LDS (no excluded identifiers, dates/geography retained)
  - lds_violation: purported LDS that still contains an excluded identifier
  - lds_vs_safeharbor: same clinical event as both LDS and Safe Harbor to show
    the difference in retained PHI between the two tiers
"""
from __future__ import annotations

import random
import string
from typing import List

from .common import (
    AUTH_HIPAA_LDS,
    AUTH_HIPAA_SAFE_HARBOR,
    DETECTION_REGIME_CONFLICT,
    DETECTION_REGIME_NER,
    DETECTION_REGIME_RULE,
    LAYER_HIPAA,
    DeterministicGenerator,
    Record,
)
from .hipaa_safe_harbor import (
    US_CITIES,
    US_FIRST_NAMES,
    US_LAST_NAMES,
    US_STREET_NAMES,
    US_STREET_SUFFIXES,
    us_address,
    us_device_udi,
    us_fax,
    us_mrn,
    us_phone,
    us_ssn,
    us_vin,
    url,
    ipv4,
    health_plan_beneficiary,
)


# Diagnosis codes used in clinical context (ICD-10-CM)
ICD10_CODES = [
    ("Z00.00", "encounter for general adult medical examination without abnormal findings"),
    ("I10", "essential (primary) hypertension"),
    ("E11.9", "type 2 diabetes mellitus without complications"),
    ("J18.9", "pneumonia, unspecified organism"),
    ("M54.5", "low back pain"),
    ("F32.1", "major depressive disorder, single episode, moderate"),
    ("K21.0", "gastro-esophageal reflux disease with esophagitis"),
    ("N18.3", "chronic kidney disease, stage 3 (moderate)"),
    ("C34.10", "malignant neoplasm of upper lobe, unspecified bronchus or lung"),
    ("G43.909", "migraine, unspecified, not intractable, without status migrainosus"),
]


class HIPAALDSGenerator(DeterministicGenerator):
    """Generates records demonstrating the Limited Data Set tier.

    Three test classes:
      valid_lds_NNN     -- compliant LDS (16 excluded IDs removed, dates/geo retained)
      lds_violation_NNN -- purported LDS containing an excluded identifier (must be flagged)
      lds_vs_sh_NNN     -- paired records: same encounter as LDS and as Safe Harbor
    """

    def generate_batch(self, count: int = 20) -> List[Record]:
        records: List[Record] = []
        for i in range(count):
            rng = self.fresh(f"lds_{i}")
            records.append(self._gen_valid_lds(rng, i))
        for i in range(count // 2):
            rng = self.fresh(f"lds_violation_{i}")
            records.append(self._gen_lds_violation(rng, i))
        for i in range(count // 4):
            rng = self.fresh(f"lds_vs_sh_{i}")
            lds_rec, sh_rec = self._gen_lds_vs_safeharbor(rng, i)
            records.append(lds_rec)
            records.append(sh_rec)
        return records

    def _gen_valid_lds(self, rng: random.Random, i: int) -> Record:
        """Valid LDS: names and direct IDs removed; dates and city/state/ZIP retained."""
        year = rng.randint(1940, 1980)
        month = rng.randint(1, 12)
        day = rng.randint(1, 28)
        dob = f"{month:02d}/{day:02d}/{year}"
        age = 2026 - year

        admit_month = rng.randint(1, 12)
        admit_day = rng.randint(1, 28)
        admit_year = rng.randint(2023, 2025)
        admit_date = f"{admit_month:02d}/{admit_day:02d}/{admit_year}"

        _, city, state, _, zip_full = us_address(rng)
        code, condition = rng.choice(ICD10_CODES)
        dept = rng.choice(["Oncology", "Cardiology", "Nephrology", "Pulmonology", "Neurology"])

        text = (
            f"Limited Data Set record. DOB: {dob} (age {age}). "
            f"Admission date: {admit_date}. Location: {city}, {state} {zip_full}. "
            f"Department: {dept}. Diagnosis: {code} ({condition}). "
            f"[No name, SSN, MRN, phone, fax, email, URL, or IP retained per 164.514(e)(2).]"
        )
        spans = [
            (dob, "DATE_DOB", "C", "us", AUTH_HIPAA_LDS, DETECTION_REGIME_NER),
            (f"age {age}", "AGE", "C", "us", AUTH_HIPAA_LDS, DETECTION_REGIME_NER),
            (admit_date, "DATE_ADMIT", "C", "us", AUTH_HIPAA_LDS, DETECTION_REGIME_NER),
            (city, "ADDRESS_CITY", "B", "us", AUTH_HIPAA_LDS, DETECTION_REGIME_NER),
            (zip_full, "ADDRESS_ZIP", "B", "us", AUTH_HIPAA_LDS, DETECTION_REGIME_CONFLICT),
        ]
        return Record(
            record_id=f"valid_lds_{i:04d}",
            text=text,
            gold_spans=self.annotate(text, spans),
            layer=LAYER_HIPAA,
            jurisdiction="us",
            detection_regime=DETECTION_REGIME_NER,
            de_id_tier="limited_data_set",
            context="research",
            format="text",
            authority_citations=[AUTH_HIPAA_LDS],
            metadata={
                "lds_class": "valid_lds",
                "dua_required": True,
                "identifiers_excluded": list(range(1, 17)),
                "dates_retained": True,
                "geography_retained": True,
            },
        )

    def _gen_lds_violation(self, rng: random.Random, i: int) -> Record:
        """Purported LDS that still contains one excluded identifier.

        These records test whether a system can catch LDS compliance failures.
        The excluded_identifier_type field in metadata names the violation.
        """
        violation_type = i % 8
        _, city, state, _, zip_full = us_address(rng)
        code, condition = rng.choice(ICD10_CODES)

        month = rng.randint(1, 12)
        day = rng.randint(1, 28)
        year = rng.randint(2023, 2025)
        date_str = f"{month:02d}/{day:02d}/{year}"

        if violation_type == 0:
            # Name still present (excluded by (e)(2)(i))
            first = rng.choice(US_FIRST_NAMES)
            last = rng.choice(US_LAST_NAMES)
            name = f"{first} {last}"
            text = (
                f"LDS record (non-compliant). Patient: {name}. "
                f"Admission {date_str}. City: {city}, {state}. "
                f"Diagnosis: {code}. [VIOLATION: name not removed per (e)(2)(i)]"
            )
            spans = [(name, "NAME_PATIENT", "A", "us", AUTH_HIPAA_LDS, DETECTION_REGIME_NER)]
            violation = "name_retained"

        elif violation_type == 1:
            # SSN still present (excluded by (e)(2)(vi))
            ssn = us_ssn(rng)
            text = (
                f"LDS record (non-compliant). SSN: {ssn}. "
                f"Admission {date_str}. Location: {city}, {state} {zip_full}. "
                f"Diagnosis: {code}. [VIOLATION: SSN not removed per (e)(2)(vi)]"
            )
            spans = [(ssn, "SSN", "G", "us", AUTH_HIPAA_LDS, DETECTION_REGIME_RULE)]
            violation = "ssn_retained"

        elif violation_type == 2:
            # Phone still present (excluded by (e)(2)(iii))
            phone = us_phone(rng)
            text = (
                f"LDS record (non-compliant). Contact: {phone}. "
                f"Admission {date_str}. City: {city}. "
                f"Diagnosis: {code}. [VIOLATION: phone not removed per (e)(2)(iii)]"
            )
            spans = [(phone, "PHONE", "D", "us", AUTH_HIPAA_LDS, DETECTION_REGIME_NER)]
            violation = "phone_retained"

        elif violation_type == 3:
            # MRN still present (excluded by (e)(2)(vii))
            mrn = us_mrn(rng)
            text = (
                f"LDS record (non-compliant). MRN: {mrn}. "
                f"Admission {date_str}. City: {city}, {state}. "
                f"Diagnosis: {code}. [VIOLATION: MRN not removed per (e)(2)(vii)]"
            )
            spans = [(mrn, "MRN", "H", "us", AUTH_HIPAA_LDS, DETECTION_REGIME_NER)]
            violation = "mrn_retained"

        elif violation_type == 4:
            # Email still present (excluded by (e)(2)(v))
            first = rng.choice(US_FIRST_NAMES).lower()
            last = rng.choice(US_LAST_NAMES).lower()
            email = f"{first}.{last}{rng.randint(1,99)}@example.com"
            text = (
                f"LDS record (non-compliant). Patient email: {email}. "
                f"Admission {date_str}. City: {city}. "
                f"Diagnosis: {code}. [VIOLATION: email not removed per (e)(2)(v)]"
            )
            spans = [(email, "EMAIL", "F", "us", AUTH_HIPAA_LDS, DETECTION_REGIME_RULE)]
            violation = "email_retained"

        elif violation_type == 5:
            # URL still present (excluded by (e)(2)(xiii))
            u = url(rng)
            text = (
                f"LDS record (non-compliant). Portal: {u}. "
                f"Admission {date_str}. City: {city}, {state}. "
                f"Diagnosis: {code}. [VIOLATION: URL not removed per (e)(2)(xiii)]"
            )
            spans = [(u, "URL", "N", "us", AUTH_HIPAA_LDS, DETECTION_REGIME_RULE)]
            violation = "url_retained"

        elif violation_type == 6:
            # IP still present (excluded by (e)(2)(xiv))
            ip = ipv4(rng)
            text = (
                f"LDS record (non-compliant). Last access IP: {ip}. "
                f"Admission {date_str}. City: {city}. "
                f"Diagnosis: {code}. [VIOLATION: IP not removed per (e)(2)(xiv)]"
            )
            spans = [(ip, "IP_V4", "O", "us", AUTH_HIPAA_LDS, DETECTION_REGIME_RULE)]
            violation = "ip_retained"

        else:
            # Health plan beneficiary number still present (excluded by (e)(2)(viii))
            mbi = health_plan_beneficiary(rng)
            text = (
                f"LDS record (non-compliant). Medicare ID: {mbi}. "
                f"Admission {date_str}. City: {city}, {state}. "
                f"Diagnosis: {code}. [VIOLATION: health plan ID not removed per (e)(2)(viii)]"
            )
            spans = [(mbi, "HEALTH_PLAN_ID", "I", "us", AUTH_HIPAA_LDS, DETECTION_REGIME_RULE)]
            violation = "health_plan_id_retained"

        return Record(
            record_id=f"lds_violation_{i:04d}",
            text=text,
            gold_spans=self.annotate(text, spans),
            layer=LAYER_HIPAA,
            jurisdiction="us",
            detection_regime=DETECTION_REGIME_NER,
            de_id_tier="limited_data_set",
            context="research",
            format="text",
            authority_citations=[AUTH_HIPAA_LDS],
            metadata={
                "lds_class": "lds_violation",
                "lds_compliant": False,
                "excluded_identifier_violation": violation,
                "dua_required": True,
            },
        )

    def _gen_lds_vs_safeharbor(
        self, rng: random.Random, i: int
    ) -> tuple[Record, Record]:
        """Same clinical event as both LDS and Safe Harbor.

        Shows that LDS has more PHI (full dates, city, ZIP) than Safe Harbor
        (year-only, state-only, no ZIP under 20k population).
        """
        _, city, state, _, zip_full = us_address(rng)
        code, condition = rng.choice(ICD10_CODES)
        year = rng.randint(1930, 1990)
        month = rng.randint(1, 12)
        day = rng.randint(1, 28)
        dob = f"{month:02d}/{day:02d}/{year}"
        dob_year_only = str(year)
        age = 2026 - year
        dept = rng.choice(["Oncology", "Cardiology", "Nephrology"])

        # LDS version: full DOB, full city, full ZIP retained
        lds_text = (
            f"LDS record. DOB: {dob} (age {age}). "
            f"City: {city}, {state} {zip_full}. "
            f"Department: {dept}. Diagnosis: {code} ({condition}). "
            f"[DUA required. No name/SSN/MRN/phone/email/device/URL/IP.]"
        )
        lds_spans = [
            (dob, "DATE_DOB", "C", "us", AUTH_HIPAA_LDS, DETECTION_REGIME_NER),
            (city, "ADDRESS_CITY", "B", "us", AUTH_HIPAA_LDS, DETECTION_REGIME_NER),
            (zip_full, "ADDRESS_ZIP", "B", "us", AUTH_HIPAA_LDS, DETECTION_REGIME_CONFLICT),
        ]
        lds_rec = Record(
            record_id=f"lds_vs_sh_lds_{i:04d}",
            text=lds_text,
            gold_spans=self.annotate(lds_text, lds_spans),
            layer=LAYER_HIPAA,
            jurisdiction="us",
            detection_regime=DETECTION_REGIME_NER,
            de_id_tier="limited_data_set",
            context="research",
            format="text",
            authority_citations=[AUTH_HIPAA_LDS],
            metadata={"paired_with": f"lds_vs_sh_sh_{i:04d}", "tier": "lds"},
        )

        # Safe Harbor version: year-only DOB, state-only geography, no ZIP
        if age > 89:
            age_str = "age 90 or older"
        else:
            age_str = f"age {age}"
        sh_text = (
            f"Safe Harbor record. Year of birth: {dob_year_only} ({age_str}). "
            f"State: {state}. "
            f"Department: {dept}. Diagnosis: {code} ({condition}). "
            f"[Full date, city, ZIP suppressed per 164.514(b)(2)(i).]"
        )
        sh_spans = [
            (dob_year_only, "DATE_DOB_YEAR_ONLY", "C", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
        ]
        sh_rec = Record(
            record_id=f"lds_vs_sh_sh_{i:04d}",
            text=sh_text,
            gold_spans=self.annotate(sh_text, sh_spans),
            layer=LAYER_HIPAA,
            jurisdiction="us",
            detection_regime=DETECTION_REGIME_NER,
            de_id_tier="safe_harbor",
            context="research",
            format="text",
            authority_citations=[AUTH_HIPAA_SAFE_HARBOR],
            metadata={"paired_with": f"lds_vs_sh_lds_{i:04d}", "tier": "safe_harbor"},
        )
        return lds_rec, sh_rec
