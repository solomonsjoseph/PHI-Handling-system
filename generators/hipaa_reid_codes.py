"""
HIPAA Re-identification Code generator.

Authority: 45 CFR 164.514(c)

164.514(c) permits a covered entity to assign a code or other means of
record identification to allow de-identified information to be re-identified,
PROVIDED THAT:
  (1) The code is NOT derived from or related to information about the
      individual and is NOT capable of being translated to identify the
      individual.
  (2) The covered entity does not use or disclose the code for any other
      purpose and does not disclose the mechanism for re-identification.

PERMITTED re-identification codes:
  - Random UUID assigned post-de-identification
  - Hash with random salt where the salt is not stored with the data
  - Opaque sequential identifiers with no derivation from PHI

FORBIDDEN re-identification codes (violate (c)(1)):
  - Hash of SSN (derived from a direct identifier)
  - Hash of DOB (derived from a date element)
  - Hash of name (derived from a direct identifier)
  - Code containing name initials (partially derived)
  - Sequential code where order matches chronological admission sequence
    AND admission timestamps are also retained (linkable)

Records fall into two test classes:
  - permitted_NNN: code that satisfies 164.514(c)(1) -- should NOT be flagged
  - forbidden_NNN: code that violates 164.514(c)(1) -- MUST be flagged
"""
from __future__ import annotations

import hashlib
import string
from typing import List

from .common import (
    AUTH_HIPAA_REID,
    AUTH_HIPAA_SAFE_HARBOR,
    DETECTION_REGIME_NER,
    DETECTION_REGIME_RULE,
    LAYER_HIPAA,
    DeterministicGenerator,
    Record,
)
from .hipaa_safe_harbor import us_ssn


_ICD10_CODES = [
    ("Z00.00", "general adult medical examination"),
    ("I10", "essential hypertension"),
    ("E11.9", "type 2 diabetes mellitus"),
    ("J18.9", "pneumonia, unspecified organism"),
    ("M54.5", "low back pain"),
    ("F32.1", "major depressive disorder, moderate"),
]


class HIPAAReIDCodesGenerator(DeterministicGenerator):
    """Generates records testing re-identification code compliance per 164.514(c).

    Two test classes:
      permitted_NNN -- codes satisfying (c)(1): NOT derived from PHI
      forbidden_NNN -- codes violating (c)(1): derived from or linked to PHI

    The distinction matters for de-id systems: a forbidden code embedded
    in an otherwise de-identified record is itself PHI.
    """

    def generate_batch(self, count: int = 20) -> List[Record]:
        records: List[Record] = []
        for i in range(count):
            rng = self.fresh(f"reid_permitted_{i}")
            records.append(self._gen_permitted(rng, i))
        for i in range(count):
            rng = self.fresh(f"reid_forbidden_{i}")
            records.append(self._gen_forbidden(rng, i))
        return records

    def _gen_permitted(self, rng, i: int) -> Record:
        """Re-identification code that satisfies 164.514(c)(1).

        The code is a random UUID not derived from any PHI field.
        It can legitimately appear in a de-identified dataset.
        """
        # UUID v4 analog: random hex (seeded, deterministic)
        uuid_hex = "".join(rng.choices(string.hexdigits[:16], k=32))
        uuid_code = (
            f"{uuid_hex[:8]}-{uuid_hex[8:12]}-4{uuid_hex[12:15]}-"
            f"{rng.choice('89ab')}{uuid_hex[15:18]}-{uuid_hex[18:]}"
        )
        icd_code, condition = rng.choice(_ICD10_CODES)

        text = (
            f"De-identified research record. Re-ID code: {uuid_code}. "
            f"[Code is randomly assigned, not derived from subject information. "
            f"Satisfies 164.514(c)(1).] "
            f"Diagnosis: {icd_code} ({condition}). Encounter type: outpatient."
        )
        spans = [
            (uuid_code, "REID_CODE_PERMITTED", "R", "us", AUTH_HIPAA_REID, DETECTION_REGIME_RULE),
        ]
        return Record(
            record_id=f"permitted_{i:04d}",
            text=text,
            gold_spans=self.annotate(text, spans),
            layer=LAYER_HIPAA,
            jurisdiction="us",
            detection_regime=DETECTION_REGIME_RULE,
            de_id_tier="safe_harbor",
            context="research",
            format="text",
            authority_citations=[AUTH_HIPAA_REID],
            metadata={
                "reid_class": "permitted",
                "derivation": "none",
                "code_type": "random_uuid",
                "hipaa_514c_compliant": True,
            },
        )

    def _gen_forbidden(self, rng, i: int) -> Record:
        """Re-identification code that violates 164.514(c)(1).

        The code is derived from a PHI field, making it capable of being
        translated back to identify the individual. These MUST be flagged.
        """
        violation_type = i % 5

        if violation_type == 0:
            # Hash of SSN -- forbidden: SSN is a direct identifier
            ssn = us_ssn(rng)
            ssn_clean = ssn.replace("-", "")
            forbidden_code = "SSN-SHA256-" + hashlib.sha256(ssn_clean.encode()).hexdigest()[:16]
            code, condition = rng.choice(_ICD10_CODES)
            text = (
                f"De-identified research record. Re-ID code: {forbidden_code}. "
                f"[VIOLATION: code is SHA-256 of SSN. Derived from PHI per 164.514(b)(2)(i)(G). "
                f"Violates 164.514(c)(1).] "
                f"Diagnosis: {code} ({condition})."
            )
            spans = [(forbidden_code, "REID_CODE_FORBIDDEN", "R", "us", AUTH_HIPAA_REID, DETECTION_REGIME_RULE)]
            derivation = "hash_of_ssn"

        elif violation_type == 1:
            # Hash of DOB -- forbidden: DOB is a date element under (C)
            year = rng.randint(1940, 1980)
            month = rng.randint(1, 12)
            day = rng.randint(1, 28)
            dob = f"{month:02d}/{day:02d}/{year}"
            forbidden_code = "DOB-MD5-" + hashlib.md5(dob.encode()).hexdigest()[:16]
            code, condition = rng.choice(_ICD10_CODES)
            text = (
                f"De-identified research record. Re-ID code: {forbidden_code}. "
                f"[VIOLATION: code is MD5 of date of birth. DOB is PHI under 164.514(b)(2)(i)(C). "
                f"Violates 164.514(c)(1).] "
                f"Diagnosis: {code} ({condition})."
            )
            spans = [(forbidden_code, "REID_CODE_FORBIDDEN", "R", "us", AUTH_HIPAA_REID, DETECTION_REGIME_RULE)]
            derivation = "hash_of_dob"

        elif violation_type == 2:
            # Initials embedded in code -- partially derived from name
            first_initial = rng.choice(string.ascii_uppercase)
            last_initial = rng.choice(string.ascii_uppercase)
            seq = rng.randint(1000, 9999)
            forbidden_code = f"{first_initial}{last_initial}-{seq:04d}"
            code, condition = rng.choice(_ICD10_CODES)
            text = (
                f"De-identified research record. Re-ID code: {forbidden_code}. "
                f"[VIOLATION: code contains patient initials. Partially derived from name "
                f"(164.514(b)(2)(i)(A)). Violates 164.514(c)(1) derivation prohibition.] "
                f"Diagnosis: {code} ({condition})."
            )
            spans = [(forbidden_code, "REID_CODE_FORBIDDEN", "R", "us", AUTH_HIPAA_REID, DETECTION_REGIME_NER)]
            derivation = "initials_embedded"

        elif violation_type == 3:
            # Hash of MRN -- forbidden: MRN is a direct identifier under (H)
            mrn_digits = "".join(str(rng.randint(0, 9)) for _ in range(8))
            forbidden_code = "MRN-SHA256-" + hashlib.sha256(mrn_digits.encode()).hexdigest()[:16]
            code, condition = rng.choice(_ICD10_CODES)
            text = (
                f"De-identified research record. Re-ID code: {forbidden_code}. "
                f"[VIOLATION: code is SHA-256 of medical record number. MRN is PHI under "
                f"164.514(b)(2)(i)(H). Violates 164.514(c)(1).] "
                f"Diagnosis: {code} ({condition})."
            )
            spans = [(forbidden_code, "REID_CODE_FORBIDDEN", "R", "us", AUTH_HIPAA_REID, DETECTION_REGIME_RULE)]
            derivation = "hash_of_mrn"

        else:
            # Zip-prefixed sequential code: re-ID mechanism disclosed in the code itself
            # If the first 5 chars are a ZIP and the rest is a counter,
            # the code reveals geographic origin and sequencing order.
            zip_digits = f"{rng.randint(100, 999):03d}{rng.randint(10, 99):02d}"
            seq = rng.randint(1, 9999)
            forbidden_code = f"ZIP{zip_digits}-{seq:04d}"
            code, condition = rng.choice(_ICD10_CODES)
            text = (
                f"De-identified research record. Re-ID code: {forbidden_code}. "
                f"[VIOLATION: code embeds ZIP prefix (geographic data under 164.514(b)(2)(i)(B)) "
                f"and sequential counter (linkable to admission order). Violates (c)(1).] "
                f"Diagnosis: {code} ({condition})."
            )
            spans = [(forbidden_code, "REID_CODE_FORBIDDEN", "R", "us", AUTH_HIPAA_REID, DETECTION_REGIME_NER)]
            derivation = "zip_prefixed_sequential"

        return Record(
            record_id=f"forbidden_{i:04d}",
            text=text,
            gold_spans=self.annotate(text, spans),
            layer=LAYER_HIPAA,
            jurisdiction="us",
            detection_regime=DETECTION_REGIME_NER,
            de_id_tier="safe_harbor",
            context="research",
            format="text",
            authority_citations=[AUTH_HIPAA_REID],
            metadata={
                "reid_class": "forbidden",
                "derivation": derivation,
                "hipaa_514c_compliant": False,
                "violation_authority": "45 CFR 164.514(c)(1)",
            },
        )
