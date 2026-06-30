"""
Australia Privacy Act 1988 + My Health Records Act 2012 -- identifier generator.

Primary authorities:
  Privacy Act 1988 (Cth) -- Australian Privacy Principles (APPs)
  Healthcare Identifiers Act 2010 (Cth) -- IHI 16-digit format
  My Health Records Act 2012 (Cth) -- health record specific obligations
  Health Insurance Act 1973 (Cth) -- Medicare number format and checksum
  Income Tax Assessment Act 1936 (Cth) s.202 -- Tax File Number (TFN)
  Veterans Entitlements Act 1986 (Cth) -- DVA file number format
  Australian Passports Act 2005 (Cth) -- passport number format
  Road Transport Act 2013 (NSW) / Road Transport (General) Act 1999 (ACT) -- driver's licence

Medicare number checksum (Health Insurance Act 1973):
  Weights [1,3,7,9,1,3,7,9] applied to digits 1-8.
  Sum mod 10 = digit 9 (check digit). Digit 10 is individual reference (1-9).

Note: My Health Records Act 2012 creates stronger protections for data held in
the My Health Record system than Privacy Act alone. IHI (Individual Healthcare
Identifier) is the primary link to My Health Record.

Seeded determinism: uses random.Random(seed) only.
Same seed => bitwise-identical output (IRB reproducibility requirement).
"""
from __future__ import annotations

import string
from pathlib import Path
from typing import List

from generators.common import (
    AUTH_AU_MY_HEALTH,
    AUTH_AU_PRIVACY,
    DETECTION_REGIME_NER,
    DETECTION_REGIME_RULE,
    DeterministicGenerator,
    Record,
    write_jsonl,
)

# ---------------------------------------------------------------------------
# Layer and jurisdiction constants for Australia
# ---------------------------------------------------------------------------
LAYER_AUSTRALIA = "australia_specific"
JURISDICTION_AU = "au"

AUTH_AU_MEDICARE = "Health Insurance Act 1973 (Cth) s.19AA (Medicare number)"
AUTH_AU_IHI = "Healthcare Identifiers Act 2010 (Cth) s.9 (IHI)"
AUTH_AU_MY_HEALTH_RECORDS = "My Health Records Act 2012 (Cth)"
AUTH_AU_TFN = "Income Tax Assessment Act 1936 (Cth) s.202 (TFN)"
AUTH_AU_DVA = "Veterans Entitlements Act 1986 (Cth) (DVA file number)"
AUTH_AU_PASSPORT = "Australian Passports Act 2005 (Cth) s.9"
AUTH_AU_DL_NSW = "Road Transport Act 2013 (NSW) (driver's licence)"
AUTH_AU_DL_ACT = "Road Transport (General) Act 1999 (ACT) (driver's licence)"
AUTH_AU_PHONE = "Privacy Act 1988 (Cth) APP 3 (phone as personal information)"

# Medicare checksum weights (positions 1-8, 1-indexed)
MEDICARE_WEIGHTS = [1, 3, 7, 9, 1, 3, 7, 9]

# ---------------------------------------------------------------------------
# Name pools -- Australian given names and surnames (common, non-specific)
# ---------------------------------------------------------------------------
AU_FIRST_NAMES = [
    "Liam", "Olivia", "Noah", "Charlotte", "Jack", "Ava", "William",
    "Mia", "James", "Amelia", "Oliver", "Harper", "Lucas", "Evelyn",
    "Henry", "Abigail", "Ethan", "Emily", "Alexander", "Elizabeth",
    "Mason", "Sofia", "Logan", "Avery", "Jackson", "Ella", "Sebastian",
    "Scarlett", "Aiden", "Grace", "Matthew", "Chloe",
]

AU_LAST_NAMES = [
    "Smith", "Jones", "Williams", "Brown", "Wilson", "Taylor", "Johnson",
    "White", "Martin", "Anderson", "Thompson", "Thomas", "Lee", "Walker",
    "Harris", "Robinson", "Lewis", "Jackson", "Young", "Allen",
    "Mitchell", "King", "Clark", "Scott", "Wright", "Hughes", "Hill",
    "Green", "Adams", "Baker",
]

AU_HOSPITALS = [
    "Royal Melbourne Hospital",
    "Royal Prince Alfred Hospital Sydney",
    "Princess Alexandra Hospital Brisbane",
    "Royal Adelaide Hospital",
    "Sir Charles Gairdner Hospital Perth",
    "Royal Hobart Hospital",
    "Royal Darwin Hospital",
    "Canberra Hospital",
]

AU_STATES = ["NSW", "VIC", "QLD", "SA", "WA", "TAS", "NT", "ACT"]

AU_CITIES = [
    "Sydney", "Melbourne", "Brisbane", "Perth", "Adelaide",
    "Hobart", "Darwin", "Canberra",
]


def _medicare_checksum(digits_8: str) -> str:
    """Compute Medicare check digit from first 8 digits.

    Authority: Health Insurance Act 1973 (Cth) -- Medicare number format.
    Weighted sum of digits 1-8 with weights [1,3,7,9,1,3,7,9], mod 10.
    """
    total = sum(int(d) * w for d, w in zip(digits_8, MEDICARE_WEIGHTS))
    return str(total % 10)


class AustraliaPrivacyGenerator(DeterministicGenerator):
    """Generates synthetic Australia Privacy Act 1988 PHI test records.

    Primary authorities: Privacy Act 1988 (Cth), Healthcare Identifiers Act
    2010 (Cth), My Health Records Act 2012 (Cth).
    Each record embeds at least one identifier in realistic administrative or
    clinical prose. Gold spans are verified against text offsets before return.
    """

    def _medicare_number(self, rng) -> str:
        """10-digit Medicare number with weighted checksum.

        Authority: Health Insurance Act 1973 (Cth) s.19AA.
        Format: XXXXXXXXX-Y where X1-X8 are weighted, X9 is check digit,
        Y (digit 10) is individual reference number 1-9.
        Returned as 10 consecutive digits (no separator) matching common
        stored format. The leading digit is 2-6 (valid Medicare ranges).
        """
        leading = str(rng.randint(2, 6))
        mid = "".join(str(rng.randint(0, 9)) for _ in range(7))
        first8 = leading + mid
        check = _medicare_checksum(first8)
        ref = str(rng.randint(1, 9))
        return first8 + check + ref

    def _ihi(self, rng) -> str:
        """16-digit Individual Healthcare Identifier starting with 80.

        Authority: Healthcare Identifiers Act 2010 (Cth) s.9.
        IHI format: 80 followed by 14 digits. Issued by Services Australia.
        """
        suffix = "".join(str(rng.randint(0, 9)) for _ in range(14))
        return f"80{suffix}"

    def _tfn(self, rng) -> str:
        """9-digit Tax File Number.

        Authority: Income Tax Assessment Act 1936 (Cth) s.202.
        TFNs do not have a published algorithmic checksum that is usable for
        synthesis (ATO validation is server-side). Format is 8-9 digits;
        9 digits is the current standard. First digit is never 0.
        """
        first = str(rng.randint(1, 9))
        rest = "".join(str(rng.randint(0, 9)) for _ in range(8))
        return first + rest

    def _dva_file(self, rng) -> str:
        """DVA file number: [NQV] + 6 digits + uppercase letter.

        Authority: Veterans Entitlements Act 1986 (Cth).
        N = Navy, Q = Army, V = Air Force (state prefix historical variant).
        """
        prefix = rng.choice(["N", "Q", "V"])
        digits = "".join(str(rng.randint(0, 9)) for _ in range(6))
        suffix = rng.choice(string.ascii_uppercase)
        return f"{prefix}{digits}{suffix}"

    def _drivers_licence(self, rng) -> str:
        """State-specific driver's licence number.

        Authority: Road Transport Act 2013 (NSW); Road Transport (General)
        Act 1999 (ACT).
        NSW format: one letter + 9 digits (e.g. A123456789).
        ACT format: 2 digits + letter + 5 digits (e.g. 12A34567).
        Returns one of these two formats with equal probability.
        """
        if rng.random() < 0.5:
            # NSW
            letter = rng.choice(string.ascii_uppercase)
            digits = "".join(str(rng.randint(0, 9)) for _ in range(9))
            return f"{letter}{digits}"
        else:
            # ACT
            d2 = "".join(str(rng.randint(0, 9)) for _ in range(2))
            letter = rng.choice(string.ascii_uppercase)
            d5 = "".join(str(rng.randint(0, 9)) for _ in range(5))
            return f"{d2}{letter}{d5}"

    def _passport(self, rng) -> str:
        """Australian passport: one letter + 8 digits, or 2 letters + 7 digits.

        Authority: Australian Passports Act 2005 (Cth) s.9.
        """
        if rng.random() < 0.7:
            letter = rng.choice(string.ascii_uppercase)
            digits = "".join(str(rng.randint(0, 9)) for _ in range(8))
            return f"{letter}{digits}"
        else:
            letters = "".join(rng.choice(string.ascii_uppercase) for _ in range(2))
            digits = "".join(str(rng.randint(0, 9)) for _ in range(7))
            return f"{letters}{digits}"

    def _phone(self, rng) -> str:
        """Australian phone: 0[2-9][0-9]{8} or +61[2-9][0-9]{8}.

        Authority: Privacy Act 1988 (Cth) APP 3.
        """
        area = str(rng.randint(2, 9))
        digits = "".join(str(rng.randint(0, 9)) for _ in range(8))
        if rng.random() < 0.5:
            return f"0{area}{digits}"
        else:
            return f"+61{area}{digits}"

    # -------------------------------------------------------------------------
    # Record builders
    # -------------------------------------------------------------------------

    def _make_medicare_record(self, idx: int, count: int) -> List[Record]:
        records = []
        rng = self.fresh(f"medicare_{idx}")
        for i in range(count):
            first = rng.choice(AU_FIRST_NAMES)
            last = rng.choice(AU_LAST_NAMES)
            medicare = self._medicare_number(rng)
            hospital = rng.choice(AU_HOSPITALS)
            text = (
                f"Medicare claim form -- {hospital}. "
                f"Patient: {first} {last}. "
                f"Medicare Number: {medicare}. "
                f"Service date: 2024-09-10. Item number: 23."
            )
            spans_spec = [
                (f"{first} {last}", "PERSON_NAME", None, JURISDICTION_AU,
                 AUTH_AU_PRIVACY, DETECTION_REGIME_NER),
                (medicare, "MEDICARE_NUMBER", None, JURISDICTION_AU,
                 AUTH_AU_MEDICARE, DETECTION_REGIME_RULE),
            ]
            spans = self.annotate(text, spans_spec)
            rec = Record(
                record_id=self.record_id(f"au_medicare_{idx}", i),
                text=text,
                gold_spans=spans,
                layer=LAYER_AUSTRALIA,
                jurisdiction=JURISDICTION_AU,
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                risk_tier="minimal",
                context="payment",
                format="text",
                authority_citations=[AUTH_AU_PRIVACY, AUTH_AU_MEDICARE],
                metadata={"identifier_type": "MEDICARE_NUMBER"},
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"Span verification failed: {errors}")
            records.append(rec)
        return records

    def _make_ihi_record(self, idx: int, count: int) -> List[Record]:
        records = []
        rng = self.fresh(f"ihi_{idx}")
        for i in range(count):
            first = rng.choice(AU_FIRST_NAMES)
            last = rng.choice(AU_LAST_NAMES)
            ihi = self._ihi(rng)
            hospital = rng.choice(AU_HOSPITALS)
            text = (
                f"My Health Record access log -- {hospital}. "
                f"Patient: {first} {last}. "
                f"Individual Healthcare Identifier (IHI): {ihi}. "
                f"Record accessed: 2024-11-03. Accessing provider: GP."
            )
            spans_spec = [
                (f"{first} {last}", "PERSON_NAME", None, JURISDICTION_AU,
                 AUTH_AU_PRIVACY, DETECTION_REGIME_NER),
                (ihi, "IHI", None, JURISDICTION_AU,
                 AUTH_AU_IHI, DETECTION_REGIME_RULE),
            ]
            spans = self.annotate(text, spans_spec)
            rec = Record(
                record_id=self.record_id(f"au_ihi_{idx}", i),
                text=text,
                gold_spans=spans,
                layer=LAYER_AUSTRALIA,
                jurisdiction=JURISDICTION_AU,
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                risk_tier="minimal",
                context="treatment",
                format="text",
                authority_citations=[AUTH_AU_PRIVACY, AUTH_AU_IHI, AUTH_AU_MY_HEALTH_RECORDS],
                metadata={"identifier_type": "IHI"},
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"Span verification failed: {errors}")
            records.append(rec)
        return records

    def _make_tfn_record(self, idx: int, count: int) -> List[Record]:
        records = []
        rng = self.fresh(f"tfn_{idx}")
        for i in range(count):
            first = rng.choice(AU_FIRST_NAMES)
            last = rng.choice(AU_LAST_NAMES)
            tfn = self._tfn(rng)
            text = (
                f"Private health insurance tax offset application. "
                f"Applicant: {first} {last}. "
                f"Tax File Number: {tfn}. "
                f"Policy number: AHF-2024-00871. Rebate tier: Base."
            )
            spans_spec = [
                (f"{first} {last}", "PERSON_NAME", None, JURISDICTION_AU,
                 AUTH_AU_PRIVACY, DETECTION_REGIME_NER),
                (tfn, "TFN", None, JURISDICTION_AU,
                 AUTH_AU_TFN, DETECTION_REGIME_RULE),
            ]
            spans = self.annotate(text, spans_spec)
            rec = Record(
                record_id=self.record_id(f"au_tfn_{idx}", i),
                text=text,
                gold_spans=spans,
                layer=LAYER_AUSTRALIA,
                jurisdiction=JURISDICTION_AU,
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                risk_tier="minimal",
                context="payment",
                format="text",
                authority_citations=[AUTH_AU_PRIVACY, AUTH_AU_TFN],
                metadata={"identifier_type": "TFN"},
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"Span verification failed: {errors}")
            records.append(rec)
        return records

    def _make_dva_record(self, idx: int, count: int) -> List[Record]:
        records = []
        rng = self.fresh(f"dva_{idx}")
        for i in range(count):
            first = rng.choice(AU_FIRST_NAMES)
            last = rng.choice(AU_LAST_NAMES)
            dva = self._dva_file(rng)
            hospital = rng.choice(AU_HOSPITALS)
            text = (
                f"DVA health card treatment record -- {hospital}. "
                f"Veteran: {first} {last}. "
                f"DVA File Number: {dva}. "
                f"Treatment type: orthopaedic review. Date: 2024-06-18."
            )
            spans_spec = [
                (f"{first} {last}", "PERSON_NAME", None, JURISDICTION_AU,
                 AUTH_AU_PRIVACY, DETECTION_REGIME_NER),
                (dva, "DVA_FILE", None, JURISDICTION_AU,
                 AUTH_AU_DVA, DETECTION_REGIME_RULE),
            ]
            spans = self.annotate(text, spans_spec)
            rec = Record(
                record_id=self.record_id(f"au_dva_{idx}", i),
                text=text,
                gold_spans=spans,
                layer=LAYER_AUSTRALIA,
                jurisdiction=JURISDICTION_AU,
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                risk_tier="minimal",
                context="treatment",
                format="text",
                authority_citations=[AUTH_AU_PRIVACY, AUTH_AU_DVA],
                metadata={"identifier_type": "DVA_FILE"},
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"Span verification failed: {errors}")
            records.append(rec)
        return records

    def _make_drivers_licence_record(self, idx: int, count: int) -> List[Record]:
        records = []
        rng = self.fresh(f"dl_{idx}")
        for i in range(count):
            first = rng.choice(AU_FIRST_NAMES)
            last = rng.choice(AU_LAST_NAMES)
            dl = self._drivers_licence(rng)
            city = rng.choice(AU_CITIES)
            text = (
                f"Identity verification record -- {city} Medical Centre. "
                f"Patient: {first} {last}. "
                f"Driver's licence number: {dl}. "
                f"Verified by receptionist on 2024-08-22."
            )
            spans_spec = [
                (f"{first} {last}", "PERSON_NAME", None, JURISDICTION_AU,
                 AUTH_AU_PRIVACY, DETECTION_REGIME_NER),
                (dl, "DRIVERS_LICENSE_AU", None, JURISDICTION_AU,
                 AUTH_AU_DL_NSW, DETECTION_REGIME_RULE),
            ]
            spans = self.annotate(text, spans_spec)
            rec = Record(
                record_id=self.record_id(f"au_dl_{idx}", i),
                text=text,
                gold_spans=spans,
                layer=LAYER_AUSTRALIA,
                jurisdiction=JURISDICTION_AU,
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                risk_tier="minimal",
                context="operations",
                format="text",
                authority_citations=[AUTH_AU_PRIVACY, AUTH_AU_DL_NSW],
                metadata={"identifier_type": "DRIVERS_LICENSE_AU"},
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"Span verification failed: {errors}")
            records.append(rec)
        return records

    def _make_passport_record(self, idx: int, count: int) -> List[Record]:
        records = []
        rng = self.fresh(f"passport_{idx}")
        for i in range(count):
            first = rng.choice(AU_FIRST_NAMES)
            last = rng.choice(AU_LAST_NAMES)
            passport = self._passport(rng)
            city = rng.choice(AU_CITIES)
            text = (
                f"Overseas health insurance claim -- {city} clinic. "
                f"Patient: {first} {last}. "
                f"Australian passport number: {passport}. "
                f"Country of treatment: Thailand. Incident date: 2024-04-05."
            )
            spans_spec = [
                (f"{first} {last}", "PERSON_NAME", None, JURISDICTION_AU,
                 AUTH_AU_PRIVACY, DETECTION_REGIME_NER),
                (passport, "AU_PASSPORT", None, JURISDICTION_AU,
                 AUTH_AU_PASSPORT, DETECTION_REGIME_RULE),
            ]
            spans = self.annotate(text, spans_spec)
            rec = Record(
                record_id=self.record_id(f"au_passport_{idx}", i),
                text=text,
                gold_spans=spans,
                layer=LAYER_AUSTRALIA,
                jurisdiction=JURISDICTION_AU,
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                risk_tier="minimal",
                context="payment",
                format="text",
                authority_citations=[AUTH_AU_PRIVACY, AUTH_AU_PASSPORT],
                metadata={"identifier_type": "AU_PASSPORT"},
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"Span verification failed: {errors}")
            records.append(rec)
        return records

    def _make_phone_record(self, idx: int, count: int) -> List[Record]:
        records = []
        rng = self.fresh(f"phone_{idx}")
        for i in range(count):
            first = rng.choice(AU_FIRST_NAMES)
            last = rng.choice(AU_LAST_NAMES)
            phone = self._phone(rng)
            hospital = rng.choice(AU_HOSPITALS)
            text = (
                f"Patient contact record -- {hospital}. "
                f"Patient: {first} {last}. "
                f"Contact number: {phone}. "
                f"Preferred contact time: morning. Recorded 2024-10-14."
            )
            spans_spec = [
                (f"{first} {last}", "PERSON_NAME", None, JURISDICTION_AU,
                 AUTH_AU_PRIVACY, DETECTION_REGIME_NER),
                (phone, "PHONE_AU", None, JURISDICTION_AU,
                 AUTH_AU_PHONE, DETECTION_REGIME_RULE),
            ]
            spans = self.annotate(text, spans_spec)
            rec = Record(
                record_id=self.record_id(f"au_phone_{idx}", i),
                text=text,
                gold_spans=spans,
                layer=LAYER_AUSTRALIA,
                jurisdiction=JURISDICTION_AU,
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                risk_tier="minimal",
                context="operations",
                format="text",
                authority_citations=[AUTH_AU_PRIVACY, AUTH_AU_PHONE],
                metadata={"identifier_type": "PHONE_AU"},
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"Span verification failed: {errors}")
            records.append(rec)
        return records

    def generate_batch(self, count_per_type: int = 4) -> List[Record]:
        """Generate count_per_type records for each Australia identifier type.

        Returns a flat list of all records. All spans are verified before return.
        Primary authority: Privacy Act 1988 (Cth); Healthcare Identifiers Act
        2010 (Cth); My Health Records Act 2012 (Cth).
        """
        records: List[Record] = []
        records.extend(self._make_medicare_record(0, count_per_type))
        records.extend(self._make_ihi_record(1, count_per_type))
        records.extend(self._make_tfn_record(2, count_per_type))
        records.extend(self._make_dva_record(3, count_per_type))
        records.extend(self._make_drivers_licence_record(4, count_per_type))
        records.extend(self._make_passport_record(5, count_per_type))
        records.extend(self._make_phone_record(6, count_per_type))
        return records


def generate_corpus(seed: int = 42) -> int:
    """Build Australia corpus and write to corpus/au/australia_identifiers.jsonl.

    Primary authority: Privacy Act 1988 (Cth).
    Returns record count written.
    """
    gen = AustraliaPrivacyGenerator(seed)
    records = gen.generate_batch(count_per_type=4)
    out_path = (
        Path(__file__).resolve().parents[3] / "corpus" / "au" / "australia_identifiers.jsonl"
    )
    return write_jsonl(records, out_path)


if __name__ == "__main__":
    n = generate_corpus()
    print(f"Australia corpus: {n} records written.")
