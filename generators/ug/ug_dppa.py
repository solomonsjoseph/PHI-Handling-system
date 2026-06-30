"""
Uganda Data Protection and Privacy Act 2019 -- identifier generator.

Primary authority: Uganda Data Protection and Privacy Act 2019 (DPPA 2019).
The DPPA 2019 is GDPR-inspired and does not enumerate an explicit identifier
list equivalent to HIPAA Safe Harbor. Detection of most identifiers therefore
defaults to contextual_ner_required. Rule-applicable regime is noted only where
format is formally standardised by sector legislation.

Identifier authorities cited per category:
  NATIONAL_ID_UG  -- Registration of Persons Act 2015 (CM/NIRA 14-char format)
  HEALTH_ID_UG    -- National Health Policy 2010, Ministry of Health Uganda
  NSSF_NUMBER     -- National Social Security Fund Act 1985 (Cap 222)
  TIN_UG          -- Tax Procedure Code Act 2014, Uganda Revenue Authority
  PASSPORT_UG     -- Passports Act (Cap 63), Government of Uganda
  PHONE_UG        -- Uganda Communications Commission (MTN/Airtel prefixes 03x/07x)
  HEALTH_INS_UG   -- DPPA 2019 + National Health Insurance Bill 2019 (NHIS)

Seeded determinism: uses random.Random(seed) only -- never module-level random.
Same seed => bitwise-identical output (IRB reproducibility requirement).
"""
from __future__ import annotations

import string
from pathlib import Path
from typing import List

from generators.common import (
    DETECTION_REGIME_NER,
    DETECTION_REGIME_RULE,
    DeterministicGenerator,
    Record,
    write_jsonl,
)

# ---------------------------------------------------------------------------
# Layer and jurisdiction constants for Uganda
# Layer uses LAYER_GDPR because DPPA 2019 is explicitly GDPR-inspired.
# ---------------------------------------------------------------------------
LAYER_UGANDA = "uganda_specific"
JURISDICTION_UG = "ug"

AUTH_UG_DPPA = "Uganda Data Protection and Privacy Act 2019"
AUTH_UG_NIRA = "Registration of Persons Act 2015 (Uganda NIRA)"
AUTH_UG_MoH = "Uganda National Health Policy 2010 (Ministry of Health)"
AUTH_UG_NSSF = "NSSF Act 1985 Cap 222 (Uganda)"
AUTH_UG_TIN = "Tax Procedure Code Act 2014 (Uganda Revenue Authority)"
AUTH_UG_PASSPORTS = "Passports Act Cap 63 (Government of Uganda)"
AUTH_UG_UCC = "Uganda Communications Commission licensing (MTN/Airtel prefixes)"
AUTH_UG_NHIS = "National Health Insurance Bill 2019 (Uganda NHIS)"

# ---------------------------------------------------------------------------
# Name pools -- Ugandan given names and surnames (common, non-specific)
# Ethnically representative across Buganda, Acholi, Ankole, Busoga regions.
# ---------------------------------------------------------------------------
UG_FIRST_NAMES = [
    "Amara", "Blessing", "Charity", "David", "Emmanuel", "Florence",
    "Geoffrey", "Hannah", "Isaac", "Josephine", "Kenneth", "Lydia",
    "Moses", "Naomi", "Oliver", "Patience", "Richard", "Sarah",
    "Timothy", "Viola", "William", "Yvonne", "Zedekiah", "Annet",
    "Bonny", "Christine", "Denis", "Esther", "Frank", "Grace",
]

UG_LAST_NAMES = [
    "Achola", "Byaruhanga", "Chebet", "Ddamulira", "Eperu", "Funyida",
    "Gidudu", "Hatanga", "Isoke", "Juuko", "Kafeero", "Lalobo",
    "Mugisha", "Nabukenya", "Okello", "Patel", "Rugasira", "Ssebunya",
    "Tumwebaze", "Uwitonze", "Wasswa", "Ximba", "Yiga", "Zikusooka",
    "Akello", "Baluka", "Candia", "Ddungu", "Emorut", "Funa",
]

UG_HOSPITALS = [
    "Mulago National Referral Hospital",
    "Kiruddu General Hospital",
    "Kawempe National Referral Hospital",
    "Jinja Regional Referral Hospital",
    "Mbarara Regional Referral Hospital",
    "Gulu Regional Referral Hospital",
    "Fort Portal Regional Referral Hospital",
    "Soroti Regional Referral Hospital",
]

UG_DISTRICTS = [
    "Kampala", "Wakiso", "Mukono", "Jinja", "Mbarara",
    "Gulu", "Fort Portal", "Soroti", "Lira", "Mbale",
]


class UgandaDPPAGenerator(DeterministicGenerator):
    """Generates synthetic Uganda DPPA 2019 PHI test records.

    Authority: Uganda Data Protection and Privacy Act 2019.
    Each record embeds at least one identifier in realistic administrative or
    clinical prose. Gold spans are verified against text offsets before return.
    """

    def _national_id(self, rng) -> str:
        """14-char NIRA alphanumeric: CM followed by 12 alphanumeric chars.

        Authority: Registration of Persons Act 2015.
        Format observed from NIRA (National Identification and Registration
        Authority): prefix 'CM' + 12 uppercase alphanumerics.
        """
        chars = string.ascii_uppercase + string.digits
        suffix = "".join(rng.choice(chars) for _ in range(12))
        return f"CM{suffix}"

    def _health_id(self, rng) -> str:
        """Ministry of Health patient number: MoH/ + district code + 7 digits.

        Authority: Uganda National Health Policy 2010.
        Format is contextual -- no single national standard. This represents
        a common district health office pattern.
        """
        district_codes = ["KLA", "WKS", "MKN", "JJA", "MBR", "GUL", "FTP", "SRT"]
        code = rng.choice(district_codes)
        digits = "".join(str(rng.randint(0, 9)) for _ in range(7))
        return f"MoH/{code}/{digits}"

    def _nssf_number(self, rng) -> str:
        """9-digit NSSF member number.

        Authority: National Social Security Fund Act 1985 Cap 222.
        """
        digits = "".join(str(rng.randint(0, 9)) for _ in range(9))
        return digits

    def _tin(self, rng) -> str:
        """10-digit Uganda Revenue Authority TIN.

        Authority: Tax Procedure Code Act 2014 s.5.
        """
        digits = "".join(str(rng.randint(0, 9)) for _ in range(10))
        return digits

    def _passport(self, rng) -> str:
        """Uganda passport: one uppercase letter + 8 digits.

        Authority: Passports Act Cap 63.
        """
        letter = rng.choice(string.ascii_uppercase)
        digits = "".join(str(rng.randint(0, 9)) for _ in range(8))
        return f"{letter}{digits}"

    def _phone(self, rng) -> str:
        """Uganda mobile: 0 followed by 3 or 7, then 8 digits.

        Authority: Uganda Communications Commission (MTN prefix 077/078/076;
        Airtel prefix 070/075/074).
        Simplified to 0[37][0-9]{8} per DPPA 2019 task specification.
        """
        second = rng.choice(["3", "7"])
        digits = "".join(str(rng.randint(0, 9)) for _ in range(8))
        return f"0{second}{digits}"

    def _health_insurance(self, rng) -> str:
        """NHIS card number: NHIS/ + 10 digits.

        Authority: DPPA 2019 + National Health Insurance Bill 2019.
        NHIS numbers are not yet uniformly standardised; this represents the
        proposed format from the NHIS Bill 2019 implementation guidance.
        Detection defaults to contextual_ner_required.
        """
        digits = "".join(str(rng.randint(0, 9)) for _ in range(10))
        return f"NHIS/{digits}"

    # -------------------------------------------------------------------------
    # Record builders
    # -------------------------------------------------------------------------

    def _make_national_id_record(self, idx: int, count: int) -> List[Record]:
        records = []
        rng = self.fresh(f"national_id_{idx}")
        for i in range(count):
            first = rng.choice(UG_FIRST_NAMES)
            last = rng.choice(UG_LAST_NAMES)
            nid = self._national_id(rng)
            district = rng.choice(UG_DISTRICTS)
            text = (
                f"Patient registration form -- {district} District Health Office. "
                f"Full name: {first} {last}. "
                f"National Identification Number: {nid}. "
                f"Date of registration: 2024-03-15."
            )
            spans_spec = [
                (f"{first} {last}", "PERSON_NAME", None, JURISDICTION_UG,
                 AUTH_UG_DPPA, DETECTION_REGIME_NER),
                (nid, "NATIONAL_ID_UG", None, JURISDICTION_UG,
                 AUTH_UG_NIRA, DETECTION_REGIME_RULE),
            ]
            spans = self.annotate(text, spans_spec)
            rec = Record(
                record_id=self.record_id(f"ug_national_id_{idx}", i),
                text=text,
                gold_spans=spans,
                layer=LAYER_UGANDA,
                jurisdiction=JURISDICTION_UG,
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                risk_tier="minimal",
                context="operations",
                format="text",
                authority_citations=[AUTH_UG_DPPA, AUTH_UG_NIRA],
                metadata={"identifier_type": "NATIONAL_ID_UG"},
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"Span verification failed: {errors}")
            records.append(rec)
        return records

    def _make_health_id_record(self, idx: int, count: int) -> List[Record]:
        records = []
        rng = self.fresh(f"health_id_{idx}")
        for i in range(count):
            first = rng.choice(UG_FIRST_NAMES)
            last = rng.choice(UG_LAST_NAMES)
            hid = self._health_id(rng)
            hospital = rng.choice(UG_HOSPITALS)
            text = (
                f"Clinical referral note -- {hospital}. "
                f"Patient: {first} {last}. "
                f"Ministry of Health Patient ID: {hid}. "
                f"Diagnosis: suspected malaria. Refer to outpatient clinic."
            )
            spans_spec = [
                (f"{first} {last}", "PERSON_NAME", None, JURISDICTION_UG,
                 AUTH_UG_DPPA, DETECTION_REGIME_NER),
                (hid, "HEALTH_ID_UG", None, JURISDICTION_UG,
                 AUTH_UG_MoH, DETECTION_REGIME_NER),
            ]
            spans = self.annotate(text, spans_spec)
            rec = Record(
                record_id=self.record_id(f"ug_health_id_{idx}", i),
                text=text,
                gold_spans=spans,
                layer=LAYER_UGANDA,
                jurisdiction=JURISDICTION_UG,
                detection_regime=DETECTION_REGIME_NER,
                de_id_tier="identifiable",
                risk_tier="minimal",
                context="treatment",
                format="text",
                authority_citations=[AUTH_UG_DPPA, AUTH_UG_MoH],
                metadata={"identifier_type": "HEALTH_ID_UG"},
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"Span verification failed: {errors}")
            records.append(rec)
        return records

    def _make_nssf_record(self, idx: int, count: int) -> List[Record]:
        records = []
        rng = self.fresh(f"nssf_{idx}")
        for i in range(count):
            first = rng.choice(UG_FIRST_NAMES)
            last = rng.choice(UG_LAST_NAMES)
            nssf = self._nssf_number(rng)
            text = (
                f"NSSF benefit claim submission. "
                f"Member name: {first} {last}. "
                f"NSSF Membership Number: {nssf}. "
                f"Claim type: medical benefit. Amount claimed: UGX 450,000."
            )
            spans_spec = [
                (f"{first} {last}", "PERSON_NAME", None, JURISDICTION_UG,
                 AUTH_UG_DPPA, DETECTION_REGIME_NER),
                (nssf, "NSSF_NUMBER", None, JURISDICTION_UG,
                 AUTH_UG_NSSF, DETECTION_REGIME_RULE),
            ]
            spans = self.annotate(text, spans_spec)
            rec = Record(
                record_id=self.record_id(f"ug_nssf_{idx}", i),
                text=text,
                gold_spans=spans,
                layer=LAYER_UGANDA,
                jurisdiction=JURISDICTION_UG,
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                risk_tier="minimal",
                context="payment",
                format="text",
                authority_citations=[AUTH_UG_DPPA, AUTH_UG_NSSF],
                metadata={"identifier_type": "NSSF_NUMBER"},
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"Span verification failed: {errors}")
            records.append(rec)
        return records

    def _make_tin_record(self, idx: int, count: int) -> List[Record]:
        records = []
        rng = self.fresh(f"tin_{idx}")
        for i in range(count):
            first = rng.choice(UG_FIRST_NAMES)
            last = rng.choice(UG_LAST_NAMES)
            tin = self._tin(rng)
            text = (
                f"Uganda Revenue Authority tax assessment notice. "
                f"Taxpayer: {first} {last}. "
                f"TIN: {tin}. "
                f"Assessment period: 2023-2024. "
                f"Tax liability assessed: UGX 1,200,000."
            )
            spans_spec = [
                (f"{first} {last}", "PERSON_NAME", None, JURISDICTION_UG,
                 AUTH_UG_DPPA, DETECTION_REGIME_NER),
                (tin, "TIN_UG", None, JURISDICTION_UG,
                 AUTH_UG_TIN, DETECTION_REGIME_RULE),
            ]
            spans = self.annotate(text, spans_spec)
            rec = Record(
                record_id=self.record_id(f"ug_tin_{idx}", i),
                text=text,
                gold_spans=spans,
                layer=LAYER_UGANDA,
                jurisdiction=JURISDICTION_UG,
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                risk_tier="minimal",
                context="operations",
                format="text",
                authority_citations=[AUTH_UG_DPPA, AUTH_UG_TIN],
                metadata={"identifier_type": "TIN_UG"},
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
            first = rng.choice(UG_FIRST_NAMES)
            last = rng.choice(UG_LAST_NAMES)
            passport = self._passport(rng)
            text = (
                f"Travel medical insurance claim. "
                f"Insured person: {first} {last}. "
                f"Passport number: {passport}. "
                f"Country of travel: Kenya. Incident date: 2024-07-12."
            )
            spans_spec = [
                (f"{first} {last}", "PERSON_NAME", None, JURISDICTION_UG,
                 AUTH_UG_DPPA, DETECTION_REGIME_NER),
                (passport, "PASSPORT_UG", None, JURISDICTION_UG,
                 AUTH_UG_PASSPORTS, DETECTION_REGIME_RULE),
            ]
            spans = self.annotate(text, spans_spec)
            rec = Record(
                record_id=self.record_id(f"ug_passport_{idx}", i),
                text=text,
                gold_spans=spans,
                layer=LAYER_UGANDA,
                jurisdiction=JURISDICTION_UG,
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                risk_tier="minimal",
                context="payment",
                format="text",
                authority_citations=[AUTH_UG_DPPA, AUTH_UG_PASSPORTS],
                metadata={"identifier_type": "PASSPORT_UG"},
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
            first = rng.choice(UG_FIRST_NAMES)
            last = rng.choice(UG_LAST_NAMES)
            phone = self._phone(rng)
            hospital = rng.choice(UG_HOSPITALS)
            text = (
                f"Patient contact record -- {hospital}. "
                f"Patient name: {first} {last}. "
                f"Mobile contact: {phone}. "
                f"Emergency contact confirmed 2024-05-20."
            )
            spans_spec = [
                (f"{first} {last}", "PERSON_NAME", None, JURISDICTION_UG,
                 AUTH_UG_DPPA, DETECTION_REGIME_NER),
                (phone, "PHONE_UG", None, JURISDICTION_UG,
                 AUTH_UG_UCC, DETECTION_REGIME_RULE),
            ]
            spans = self.annotate(text, spans_spec)
            rec = Record(
                record_id=self.record_id(f"ug_phone_{idx}", i),
                text=text,
                gold_spans=spans,
                layer=LAYER_UGANDA,
                jurisdiction=JURISDICTION_UG,
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                risk_tier="minimal",
                context="operations",
                format="text",
                authority_citations=[AUTH_UG_DPPA, AUTH_UG_UCC],
                metadata={"identifier_type": "PHONE_UG"},
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"Span verification failed: {errors}")
            records.append(rec)
        return records

    def _make_health_insurance_record(self, idx: int, count: int) -> List[Record]:
        records = []
        rng = self.fresh(f"health_ins_{idx}")
        for i in range(count):
            first = rng.choice(UG_FIRST_NAMES)
            last = rng.choice(UG_LAST_NAMES)
            nhis = self._health_insurance(rng)
            hospital = rng.choice(UG_HOSPITALS)
            text = (
                f"NHIS pre-authorisation request -- {hospital}. "
                f"Member: {first} {last}. "
                f"NHIS Card Number: {nhis}. "
                f"Procedure requested: appendectomy. Pre-auth reference: 2024-PA-00341."
            )
            spans_spec = [
                (f"{first} {last}", "PERSON_NAME", None, JURISDICTION_UG,
                 AUTH_UG_DPPA, DETECTION_REGIME_NER),
                (nhis, "HEALTH_INSURANCE_UG", None, JURISDICTION_UG,
                 AUTH_UG_NHIS, DETECTION_REGIME_NER),
            ]
            spans = self.annotate(text, spans_spec)
            rec = Record(
                record_id=self.record_id(f"ug_health_ins_{idx}", i),
                text=text,
                gold_spans=spans,
                layer=LAYER_UGANDA,
                jurisdiction=JURISDICTION_UG,
                detection_regime=DETECTION_REGIME_NER,
                de_id_tier="identifiable",
                risk_tier="minimal",
                context="payment",
                format="text",
                authority_citations=[AUTH_UG_DPPA, AUTH_UG_NHIS],
                metadata={"identifier_type": "HEALTH_INSURANCE_UG"},
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"Span verification failed: {errors}")
            records.append(rec)
        return records

    def generate_batch(self, count_per_type: int = 4) -> List[Record]:
        """Generate count_per_type records for each Uganda identifier type.

        Returns a flat list of all records. All spans are verified before return.
        Authority: Uganda Data Protection and Privacy Act 2019.
        """
        records: List[Record] = []
        records.extend(self._make_national_id_record(0, count_per_type))
        records.extend(self._make_health_id_record(1, count_per_type))
        records.extend(self._make_nssf_record(2, count_per_type))
        records.extend(self._make_tin_record(3, count_per_type))
        records.extend(self._make_passport_record(4, count_per_type))
        records.extend(self._make_phone_record(5, count_per_type))
        records.extend(self._make_health_insurance_record(6, count_per_type))
        return records


def generate_corpus(seed: int = 42) -> int:
    """Build Uganda corpus and write to corpus/ug/uganda_identifiers.jsonl.

    Authority: Uganda Data Protection and Privacy Act 2019.
    Returns record count written.
    """
    gen = UgandaDPPAGenerator(seed)
    records = gen.generate_batch(count_per_type=4)
    out_path = Path(__file__).resolve().parents[3] / "corpus" / "ug" / "uganda_identifiers.jsonl"
    return write_jsonl(records, out_path)


if __name__ == "__main__":
    n = generate_corpus()
    print(f"Uganda corpus: {n} records written.")
