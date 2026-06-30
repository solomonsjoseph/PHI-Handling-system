"""
India PHI identifier generator -- synthetic, seeded, IRB-audit-ready.

Generates deterministic test records for 10 India-specific identifier types:
  AADHAAR, PAN, ABHA_NUMBER, ABHA_ADDRESS, VOTER_ID_EPIC, CTRI_ID,
  UAN, DRIVING_LICENSE_IN, MOBILE_IN, IN_PASSPORT

Primary authorities:
  - DPDP Rules 2025 Rule 14 (identifier categories)
  - UIDAI Act 2016 + DPDPA 2023
  - Income Tax Act 1961 s.139A
  - ABDM HDMP 2020
  - ICMR 2017 Section 3.7
  - EPF Act 1952
  - Motor Vehicles Act 1988
  - Passports Act 1967 s.1

All output is fully synthetic. No real individual's data is used or implied.
Seed is required; unseeded random is never used (IRB reproducibility requirement).
"""
from __future__ import annotations

import random
import string
import sys
from pathlib import Path
from typing import List

# Allow running as __main__ from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from generators.common import (
    DETECTION_REGIME_NER,
    DETECTION_REGIME_RULE,
    LAYER_INDIA,
    AUTH_DPDPA_RULES_14,
    AUTH_ICMR_PRIVACY,
    AUTH_SPDI_RULE_3,
    DeterministicGenerator,
    GoldSpan,
    Record,
    write_jsonl,
)


# ---------------------------------------------------------------------------
# Verhoeff tables (local, authoritative copy)
# The common.py verhoeff_make has a placeholder-position bug; use this instead.
# ---------------------------------------------------------------------------

_V_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
    [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2],
    [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_V_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
    [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5],
    [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]
_V_INV = [0, 4, 3, 2, 1, 5, 6, 7, 8, 9]


def _verhoeff_make(digits_11: str) -> str:
    """Append Verhoeff check digit to an 11-digit string, yielding a valid 12-digit number.

    Uses the standard algorithm: placeholder '0' appended at RIGHT, processed
    right-to-left at positions 0..11 (position 0 = rightmost = check digit placeholder).
    """
    # Append placeholder; process right-to-left
    tmp = digits_11 + "0"
    c = 0
    for i, ch in enumerate(reversed(tmp)):
        c = _V_D[c][_V_P[i % 8][int(ch)]]
    check = _V_INV[c]
    return digits_11 + str(check)


def _verhoeff_check(digits: str) -> bool:
    """Return True if the Verhoeff checksum of all digits equals zero."""
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _V_D[c][_V_P[i % 8][int(ch)]]
    return c == 0

# ---------------------------------------------------------------------------
# Authority constants specific to India PHI identifiers
# ---------------------------------------------------------------------------

AUTH_DPDPA_RULE_14 = "DPDP Rules 2025 Rule 14 (identifier categories)"
AUTH_UIDAI = "UIDAI Act 2016 + DPDPA 2023"
AUTH_INCOME_TAX = "Income Tax Act 1961 s.139A"
AUTH_ABDM = "ABDM HDMP 2020"
AUTH_ICMR_37 = "ICMR 2017 Section 3.7"
AUTH_EPF = "EPF Act 1952"
AUTH_MV_ACT = "Motor Vehicles Act 1988"
AUTH_PASSPORTS_IN = "Passports Act 1967 s.1"
AUTH_RPA_1950 = "Representation of the People Act 1950"

# ---------------------------------------------------------------------------
# Synthetic name pools (no real person names)
# ---------------------------------------------------------------------------

_FIRST_NAMES = [
    "Arjun", "Priya", "Rahul", "Ananya", "Vikram", "Sunita", "Kiran",
    "Deepa", "Suresh", "Kavitha", "Manoj", "Lalitha", "Ravi", "Meena",
    "Gopal", "Usha", "Sanjay", "Rekha", "Ashok", "Geeta",
]

_LAST_NAMES = [
    "Sharma", "Verma", "Patel", "Nair", "Reddy", "Iyer", "Kumar",
    "Singh", "Das", "Rao", "Joshi", "Gupta", "Mehta", "Shah", "Pillai",
]

_STATES_DL = {
    "MH": "Maharashtra",
    "DL": "Delhi",
    "KA": "Karnataka",
    "TN": "Tamil Nadu",
    "UP": "Uttar Pradesh",
}

_DISTRICTS_VOTER = [
    "LKW", "MUM", "DEL", "BLR", "CHN", "HYD", "KOL", "PUN", "JAN",
]

_CLINICAL_CONTEXTS = [
    "outpatient clinic", "district hospital", "primary health centre",
    "tertiary care unit", "telemedicine session", "clinical trial site",
]

_CONDITIONS = [
    "type 2 diabetes mellitus", "hypertension", "chronic kidney disease",
    "pulmonary tuberculosis", "ischaemic heart disease",
    "major depressive disorder", "hepatitis B infection",
]


def _rand_digits(rng: random.Random, n: int) -> str:
    return "".join(str(rng.randint(0, 9)) for _ in range(n))


def _rand_upper(rng: random.Random, n: int) -> str:
    return "".join(rng.choice(string.ascii_uppercase) for _ in range(n))


class IndiaIdentifierGenerator(DeterministicGenerator):
    """Generate deterministic India PHI test records.

    Authority: DPDP Rules 2025 Rule 14; UIDAI Act 2016; Income Tax Act 1961 s.139A;
               ABDM HDMP 2020; ICMR 2017 s.3.7; EPF Act 1952; Motor Vehicles Act 1988;
               Passports Act 1967 s.1; Representation of the People Act 1950.
    """

    # ------------------------------------------------------------------
    # Aadhaar (12-digit, last digit = Verhoeff check digit)
    # Authority: UIDAI Act 2016 + DPDPA 2023
    # ------------------------------------------------------------------

    def _make_aadhaar(self) -> str:
        """Generate a 12-digit Aadhaar with valid Verhoeff check digit.

        First digit must not be 0 or 1 per UIDAI numbering rules.
        Uses _verhoeff_make (local copy) which places the placeholder correctly.
        """
        rng = self.fresh("aadhaar")
        first = str(rng.randint(2, 9))
        body = first + _rand_digits(rng, 10)  # 11 digits
        return _verhoeff_make(body)

    def _aadhaar_records(self, count: int) -> List[Record]:
        records = []
        rng = self.fresh("aadhaar_batch")
        for i in range(count):
            first = rng.choice(_FIRST_NAMES)
            last = rng.choice(_LAST_NAMES)
            name = f"{first} {last}"
            ctx = rng.choice(_CLINICAL_CONTEXTS)
            cond = rng.choice(_CONDITIONS)
            # generate unique aadhaar per record
            sub_rng = self.fresh(f"aadhaar_{i}")
            first_d = str(sub_rng.randint(2, 9))
            body = first_d + _rand_digits(sub_rng, 10)
            aadhaar = _verhoeff_make(body)
            text = (
                f"Patient {name} presented at the {ctx} for management of {cond}. "
                f"Identity verified using Aadhaar number {aadhaar}. "
                f"Consent form signed and biometric authentication completed."
            )
            spans_spec = [
                (name, "PATIENT_NAME", "A", "in", AUTH_UIDAI, DETECTION_REGIME_NER),
                (aadhaar, "AADHAAR", "R", "in", AUTH_UIDAI, DETECTION_REGIME_RULE),
            ]
            gold_spans = self.annotate(text, spans_spec)
            errors = Record(
                record_id=self.record_id("in_aadhaar", i),
                text=text,
                gold_spans=gold_spans,
                layer=LAYER_INDIA,
                jurisdiction="in",
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                context="treatment",
                authority_citations=[AUTH_UIDAI, AUTH_DPDPA_RULE_14, AUTH_SPDI_RULE_3],
            ).verify_spans()
            rec = Record(
                record_id=self.record_id("in_aadhaar", i),
                text=text,
                gold_spans=gold_spans,
                layer=LAYER_INDIA,
                jurisdiction="in",
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                context="treatment",
                authority_citations=[AUTH_UIDAI, AUTH_DPDPA_RULE_14, AUTH_SPDI_RULE_3],
            )
            span_errors = rec.verify_spans()
            if span_errors:
                raise ValueError(f"Aadhaar record {i}: {span_errors}")
            records.append(rec)
        return records

    # ------------------------------------------------------------------
    # PAN -- [A-Z]{5}[0-9]{4}[A-Z]
    # Authority: Income Tax Act 1961 s.139A
    # ------------------------------------------------------------------

    def _make_pan(self, rng: random.Random) -> str:
        alpha5 = _rand_upper(rng, 5)
        digits4 = _rand_digits(rng, 4)
        last = _rand_upper(rng, 1)
        return f"{alpha5}{digits4}{last}"

    def _pan_records(self, count: int) -> List[Record]:
        records = []
        rng = self.fresh("pan_batch")
        for i in range(count):
            first = rng.choice(_FIRST_NAMES)
            last = rng.choice(_LAST_NAMES)
            name = f"{first} {last}"
            pan = self._make_pan(self.fresh(f"pan_{i}"))
            text = (
                f"Health insurance reimbursement claim submitted by {name}. "
                f"PAN: {pan}. "
                f"Amount claimed: INR 45,000 for surgical procedure on 2025-03-15. "
                f"Claim forwarded to TPA for settlement."
            )
            spans_spec = [
                (name, "PATIENT_NAME", "A", "in", AUTH_INCOME_TAX, DETECTION_REGIME_NER),
                (pan, "PAN", "R", "in", AUTH_INCOME_TAX, DETECTION_REGIME_RULE),
            ]
            gold_spans = self.annotate(text, spans_spec)
            rec = Record(
                record_id=self.record_id("in_pan", i),
                text=text,
                gold_spans=gold_spans,
                layer=LAYER_INDIA,
                jurisdiction="in",
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                context="payment",
                authority_citations=[AUTH_INCOME_TAX, AUTH_DPDPA_RULE_14],
            )
            span_errors = rec.verify_spans()
            if span_errors:
                raise ValueError(f"PAN record {i}: {span_errors}")
            records.append(rec)
        return records

    # ------------------------------------------------------------------
    # ABHA Number -- 14 digits (no leading zero; ABDM HDMP 2020)
    # ------------------------------------------------------------------

    def _make_abha_number(self, rng: random.Random) -> str:
        first = str(rng.randint(1, 9))
        rest = _rand_digits(rng, 13)
        return first + rest

    def _abha_number_records(self, count: int) -> List[Record]:
        records = []
        rng = self.fresh("abha_num_batch")
        for i in range(count):
            first = rng.choice(_FIRST_NAMES)
            last = rng.choice(_LAST_NAMES)
            name = f"{first} {last}"
            abha = self._make_abha_number(self.fresh(f"abha_num_{i}"))
            text = (
                f"ABHA health record for {name} (ABHA Number: {abha}) was linked to "
                f"the Ayushman Bharat Digital Mission portal. The patient's vaccination "
                f"records and outpatient visits were consolidated under this identifier."
            )
            spans_spec = [
                (name, "PATIENT_NAME", "A", "in", AUTH_ABDM, DETECTION_REGIME_NER),
                (abha, "ABHA_NUMBER", "R", "in", AUTH_ABDM, DETECTION_REGIME_RULE),
            ]
            gold_spans = self.annotate(text, spans_spec)
            rec = Record(
                record_id=self.record_id("in_abha_number", i),
                text=text,
                gold_spans=gold_spans,
                layer=LAYER_INDIA,
                jurisdiction="in",
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                context="treatment",
                authority_citations=[AUTH_ABDM, AUTH_DPDPA_RULE_14],
            )
            span_errors = rec.verify_spans()
            if span_errors:
                raise ValueError(f"ABHA_NUMBER record {i}: {span_errors}")
            records.append(rec)
        return records

    # ------------------------------------------------------------------
    # ABHA Address -- user@abdm
    # ------------------------------------------------------------------

    def _make_abha_address(self, rng: random.Random) -> str:
        user_part = "".join(rng.choice(string.ascii_lowercase + string.digits) for _ in range(rng.randint(6, 12)))
        return f"{user_part}@abdm"

    def _abha_address_records(self, count: int) -> List[Record]:
        records = []
        rng = self.fresh("abha_addr_batch")
        for i in range(count):
            first = rng.choice(_FIRST_NAMES)
            last = rng.choice(_LAST_NAMES)
            name = f"{first} {last}"
            addr = self._make_abha_address(self.fresh(f"abha_addr_{i}"))
            text = (
                f"Telemedicine referral issued for {name}. "
                f"Health records shared via ABHA Address {addr} with the receiving "
                f"specialist. Patient consent obtained per DPDPA 2023 Section 6."
            )
            spans_spec = [
                (name, "PATIENT_NAME", "A", "in", AUTH_ABDM, DETECTION_REGIME_NER),
                (addr, "ABHA_ADDRESS", "R", "in", AUTH_ABDM, DETECTION_REGIME_RULE),
            ]
            gold_spans = self.annotate(text, spans_spec)
            rec = Record(
                record_id=self.record_id("in_abha_address", i),
                text=text,
                gold_spans=gold_spans,
                layer=LAYER_INDIA,
                jurisdiction="in",
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                context="treatment",
                authority_citations=[AUTH_ABDM, AUTH_DPDPA_RULE_14],
            )
            span_errors = rec.verify_spans()
            if span_errors:
                raise ValueError(f"ABHA_ADDRESS record {i}: {span_errors}")
            records.append(rec)
        return records

    # ------------------------------------------------------------------
    # Voter ID / EPIC -- [A-Z]{3}[0-9]{7}
    # Authority: Representation of the People Act 1950
    # ------------------------------------------------------------------

    def _make_voter_id(self, rng: random.Random) -> str:
        prefix = _rand_upper(rng, 3)
        digits = _rand_digits(rng, 7)
        return f"{prefix}{digits}"

    def _voter_id_records(self, count: int) -> List[Record]:
        records = []
        rng = self.fresh("voter_batch")
        for i in range(count):
            first = rng.choice(_FIRST_NAMES)
            last = rng.choice(_LAST_NAMES)
            name = f"{first} {last}"
            epic = self._make_voter_id(self.fresh(f"voter_{i}"))
            text = (
                f"Patient {name} presented with EPIC card (Voter ID: {epic}) as identity "
                f"proof for enrollment in the government health scheme. "
                f"Card verified against the Electoral Photo Identity Card database."
            )
            spans_spec = [
                (name, "PATIENT_NAME", "A", "in", AUTH_RPA_1950, DETECTION_REGIME_NER),
                (epic, "VOTER_ID_EPIC", "K", "in", AUTH_RPA_1950, DETECTION_REGIME_RULE),
            ]
            gold_spans = self.annotate(text, spans_spec)
            rec = Record(
                record_id=self.record_id("in_voter_id", i),
                text=text,
                gold_spans=gold_spans,
                layer=LAYER_INDIA,
                jurisdiction="in",
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                context="operations",
                authority_citations=[AUTH_RPA_1950, AUTH_DPDPA_RULE_14],
            )
            span_errors = rec.verify_spans()
            if span_errors:
                raise ValueError(f"VOTER_ID record {i}: {span_errors}")
            records.append(rec)
        return records

    # ------------------------------------------------------------------
    # CTRI ID -- CTRI/YYYY/MM/NNNNNN
    # Authority: ICMR 2017 Section 3.7
    # ------------------------------------------------------------------

    def _make_ctri_id(self, rng: random.Random) -> str:
        year = rng.randint(2015, 2025)
        month = rng.randint(1, 12)
        seq = rng.randint(1, 999999)
        return f"CTRI/{year}/{month:02d}/{seq:06d}"

    def _ctri_records(self, count: int) -> List[Record]:
        records = []
        rng = self.fresh("ctri_batch")
        for i in range(count):
            first = rng.choice(_FIRST_NAMES)
            last = rng.choice(_LAST_NAMES)
            name = f"{first} {last}"
            ctri = self._make_ctri_id(self.fresh(f"ctri_{i}"))
            cond = rng.choice(_CONDITIONS)
            text = (
                f"Participant {name} enrolled in clinical trial {ctri} evaluating a "
                f"novel pharmacological intervention for {cond}. "
                f"Written informed consent obtained per ICMR 2017 ethical guidelines. "
                f"Ethics committee registration confirmed."
            )
            spans_spec = [
                (name, "PATIENT_NAME", "A", "in", AUTH_ICMR_37, DETECTION_REGIME_NER),
                (ctri, "CTRI_ID", "R", "in", AUTH_ICMR_37, DETECTION_REGIME_RULE),
            ]
            gold_spans = self.annotate(text, spans_spec)
            rec = Record(
                record_id=self.record_id("in_ctri", i),
                text=text,
                gold_spans=gold_spans,
                layer=LAYER_INDIA,
                jurisdiction="in",
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                context="research",
                authority_citations=[AUTH_ICMR_37, AUTH_DPDPA_RULES_14],
            )
            span_errors = rec.verify_spans()
            if span_errors:
                raise ValueError(f"CTRI record {i}: {span_errors}")
            records.append(rec)
        return records

    # ------------------------------------------------------------------
    # UAN (Universal Account Number) -- 12 digits, EPF Act 1952
    # ------------------------------------------------------------------

    def _make_uan(self, rng: random.Random) -> str:
        # First digit 1-9 per EPFO numbering
        first = str(rng.randint(1, 9))
        rest = _rand_digits(rng, 11)
        return first + rest

    def _uan_records(self, count: int) -> List[Record]:
        records = []
        rng = self.fresh("uan_batch")
        for i in range(count):
            first = rng.choice(_FIRST_NAMES)
            last = rng.choice(_LAST_NAMES)
            name = f"{first} {last}"
            uan = self._make_uan(self.fresh(f"uan_{i}"))
            text = (
                f"Occupational health record for employee {name}, UAN {uan}. "
                f"Pre-employment medical examination completed. "
                f"Record linked to EPFO portal for employer-mandated health benefits "
                f"under the EPF Act 1952."
            )
            spans_spec = [
                (name, "PATIENT_NAME", "A", "in", AUTH_EPF, DETECTION_REGIME_NER),
                (uan, "UAN", "R", "in", AUTH_EPF, DETECTION_REGIME_RULE),
            ]
            gold_spans = self.annotate(text, spans_spec)
            rec = Record(
                record_id=self.record_id("in_uan", i),
                text=text,
                gold_spans=gold_spans,
                layer=LAYER_INDIA,
                jurisdiction="in",
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                context="operations",
                authority_citations=[AUTH_EPF, AUTH_DPDPA_RULE_14],
            )
            span_errors = rec.verify_spans()
            if span_errors:
                raise ValueError(f"UAN record {i}: {span_errors}")
            records.append(rec)
        return records

    # ------------------------------------------------------------------
    # Driving License (India) -- StateCode+DD+YYYY+7digit
    # Five state variants: MH, DL, KA, TN, UP
    # Authority: Motor Vehicles Act 1988
    # ------------------------------------------------------------------

    def _make_dl_in(self, rng: random.Random, state_code: str) -> str:
        dd = rng.randint(1, 30)
        yyyy = rng.randint(2005, 2023)
        seq = _rand_digits(rng, 7)
        return f"{state_code}{dd:02d}{yyyy}{seq}"

    def _dl_records(self, count: int) -> List[Record]:
        records = []
        state_codes = list(_STATES_DL.keys())
        rng = self.fresh("dl_batch")
        for i in range(count):
            first = rng.choice(_FIRST_NAMES)
            last = rng.choice(_LAST_NAMES)
            name = f"{first} {last}"
            sc = state_codes[i % len(state_codes)]
            state_name = _STATES_DL[sc]
            dl = self._make_dl_in(self.fresh(f"dl_{i}"), sc)
            text = (
                f"Road traffic accident victim {name} admitted to emergency department. "
                f"Identity established via {state_name} driving licence {dl}. "
                f"Licence details forwarded to Motor Vehicles Department as required "
                f"under the Motor Vehicles Act 1988."
            )
            spans_spec = [
                (name, "PATIENT_NAME", "A", "in", AUTH_MV_ACT, DETECTION_REGIME_NER),
                (dl, "DRIVING_LICENSE_IN", "K", "in", AUTH_MV_ACT, DETECTION_REGIME_RULE),
            ]
            gold_spans = self.annotate(text, spans_spec)
            rec = Record(
                record_id=self.record_id("in_dl", i),
                text=text,
                gold_spans=gold_spans,
                layer=LAYER_INDIA,
                jurisdiction="in",
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                context="treatment",
                authority_citations=[AUTH_MV_ACT, AUTH_DPDPA_RULE_14],
                metadata={"state_code": sc},
            )
            span_errors = rec.verify_spans()
            if span_errors:
                raise ValueError(f"DL record {i}: {span_errors}")
            records.append(rec)
        return records

    # ------------------------------------------------------------------
    # Mobile number (India) -- [6-9][0-9]{9}
    # Authority: DPDP Rules 2025 Rule 14
    # ------------------------------------------------------------------

    def _make_mobile_in(self, rng: random.Random) -> str:
        first = str(rng.randint(6, 9))
        rest = _rand_digits(rng, 9)
        return first + rest

    def _mobile_records(self, count: int) -> List[Record]:
        records = []
        rng = self.fresh("mobile_batch")
        for i in range(count):
            first = rng.choice(_FIRST_NAMES)
            last = rng.choice(_LAST_NAMES)
            name = f"{first} {last}"
            mobile = self._make_mobile_in(self.fresh(f"mobile_{i}"))
            cond = rng.choice(_CONDITIONS)
            text = (
                f"Follow-up appointment reminder sent to {name} at mobile number "
                f"{mobile} regarding ongoing treatment for {cond}. "
                f"Patient confirmed receipt. Contact stored in hospital CRM per "
                f"DPDPA 2023 consent framework."
            )
            spans_spec = [
                (name, "PATIENT_NAME", "A", "in", AUTH_DPDPA_RULE_14, DETECTION_REGIME_NER),
                (mobile, "MOBILE_IN", "D", "in", AUTH_DPDPA_RULE_14, DETECTION_REGIME_RULE),
            ]
            gold_spans = self.annotate(text, spans_spec)
            rec = Record(
                record_id=self.record_id("in_mobile", i),
                text=text,
                gold_spans=gold_spans,
                layer=LAYER_INDIA,
                jurisdiction="in",
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                context="treatment",
                authority_citations=[AUTH_DPDPA_RULE_14, AUTH_SPDI_RULE_3],
            )
            span_errors = rec.verify_spans()
            if span_errors:
                raise ValueError(f"MOBILE_IN record {i}: {span_errors}")
            records.append(rec)
        return records

    # ------------------------------------------------------------------
    # Passport (India) -- [A-Z][0-9]{7}
    # Authority: Passports Act 1967 s.1
    # ------------------------------------------------------------------

    def _make_passport_in(self, rng: random.Random) -> str:
        letter = _rand_upper(rng, 1)
        digits = _rand_digits(rng, 7)
        return f"{letter}{digits}"

    def _passport_records(self, count: int) -> List[Record]:
        records = []
        rng = self.fresh("passport_batch")
        for i in range(count):
            first = rng.choice(_FIRST_NAMES)
            last = rng.choice(_LAST_NAMES)
            name = f"{first} {last}"
            ppn = self._make_passport_in(self.fresh(f"passport_{i}"))
            text = (
                f"International patient {name} (Passport: {ppn}) presented at the "
                f"travel health clinic for pre-departure vaccination. "
                f"Yellow fever vaccination certificate issued. Records held per "
                f"Passports Act 1967 and DPDPA 2023 cross-border data provisions."
            )
            spans_spec = [
                (name, "PATIENT_NAME", "A", "in", AUTH_PASSPORTS_IN, DETECTION_REGIME_NER),
                (ppn, "IN_PASSPORT", "K", "in", AUTH_PASSPORTS_IN, DETECTION_REGIME_RULE),
            ]
            gold_spans = self.annotate(text, spans_spec)
            rec = Record(
                record_id=self.record_id("in_passport", i),
                text=text,
                gold_spans=gold_spans,
                layer=LAYER_INDIA,
                jurisdiction="in",
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                context="treatment",
                authority_citations=[AUTH_PASSPORTS_IN, AUTH_DPDPA_RULE_14],
            )
            span_errors = rec.verify_spans()
            if span_errors:
                raise ValueError(f"Passport record {i}: {span_errors}")
            records.append(rec)
        return records

    # ------------------------------------------------------------------
    # Public batch interface
    # ------------------------------------------------------------------

    def generate_batch(self, count_per_identifier: int = 4) -> List[Record]:
        """Generate count_per_identifier records per identifier type.

        Returns a flat list of Records. All spans are verified before return.
        Total records = 10 identifier types * count_per_identifier.
        """
        n = count_per_identifier
        all_records: List[Record] = []
        all_records.extend(self._aadhaar_records(n))
        all_records.extend(self._pan_records(n))
        all_records.extend(self._abha_number_records(n))
        all_records.extend(self._abha_address_records(n))
        all_records.extend(self._voter_id_records(n))
        all_records.extend(self._ctri_records(n))
        all_records.extend(self._uan_records(n))
        all_records.extend(self._dl_records(n))
        all_records.extend(self._mobile_records(n))
        all_records.extend(self._passport_records(n))
        return all_records


# ---------------------------------------------------------------------------
# Module-level corpus generation function
# ---------------------------------------------------------------------------

def generate_corpus(seed: int = 42, count_per_identifier: int = 4) -> List[Record]:
    """Generate the India identifier corpus and write to JSONL.

    Writes to corpus/in/india_identifiers.jsonl relative to the repo root
    (two levels above this file). Returns the list of Records.

    Authority: DPDP Rules 2025 Rule 14; UIDAI Act 2016; Income Tax Act 1961 s.139A;
               ABDM HDMP 2020; ICMR 2017 s.3.7; EPF Act 1952; Motor Vehicles Act 1988;
               Passports Act 1967 s.1.
    """
    gen = IndiaIdentifierGenerator(seed=seed)
    records = gen.generate_batch(count_per_identifier=count_per_identifier)

    repo_root = Path(__file__).resolve().parents[2]
    out_path = repo_root / "corpus" / "in" / "india_identifiers.jsonl"
    count = write_jsonl(records, out_path)
    total_spans = sum(len(r.gold_spans) for r in records)
    print(
        f"India identifiers corpus: {count} records, "
        f"{total_spans} spans -> {out_path}"
    )
    return records


if __name__ == "__main__":
    generate_corpus()
