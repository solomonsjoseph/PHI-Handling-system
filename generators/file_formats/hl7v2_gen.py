"""
HL7 v2.x pipe-delimited message PHI corpus generator.

Builds HL7 v2.5.1 ADT_A01 (Admit/Visit Notification) messages containing:
  - MSH: Message Header
  - PID: Patient Identification (name, DOB, sex, address, phone, MRN)
  - NK1: Next of Kin
  - IN1: Insurance

PHI categories covered:
  - PID-3: Patient ID (MRN) -- HIPAA H
  - PID-5: Patient Name (LastName^FirstName) -- HIPAA A
  - PID-7: Date of Birth (YYYYMMDD) -- HIPAA C
  - PID-8: Sex -- not PHI on its own
  - PID-11: Address -- HIPAA B
  - PID-13: Phone -- HIPAA D
  - NK1-2: Next of Kin Name -- HIPAA A
  - NK1-5: NK1 Phone -- HIPAA D
  - IN1-2: Insurance Plan ID -- HIPAA I (health plan beneficiary)
  - IN1-3: Insurance Company Name
  - IN1-16: Name of Insured -- HIPAA A

Authority: HL7 v2.x PID/NK1/IN1 segments
           HIPAA Safe Harbor: 45 CFR 164.514(b)(2)(i)
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

from generators.common import (
    AUTH_HIPAA_SAFE_HARBOR,
    DETECTION_REGIME_RULE,
    DETECTION_REGIME_NER,
    LAYER_HIPAA,
    DeterministicGenerator,
    Record,
    write_jsonl,
)

AUTH_HL7V2 = "HL7 v2.x PID/NK1/IN1 segments"

_FIRST_NAMES = [
    "Alexander", "Beatrice", "Calvin", "Dorothy", "Elliot",
    "Florence", "Gabriel", "Hannah", "Isaac", "Josephine",
    "Kenneth", "Louise", "Maxwell", "Natalie", "Oliver",
    "Penelope", "Quentin", "Rosemary", "Samuel", "Theresa",
]
_LAST_NAMES = [
    "Abbott", "Brennan", "Chambers", "Dixon", "Elliott",
    "Fletcher", "Gregory", "Hawkins", "Irving", "Jensen",
    "Kelley", "Lambert", "Mason", "Norton", "O'Brien",
    "Phillips", "Quinn", "Russell", "Simmons", "Thornton",
]
_INSURANCE_COMPANIES = [
    "BlueCross BlueShield", "Aetna Health", "United Healthcare",
    "Cigna Medical", "Humana Insurance", "Kaiser Permanente",
    "Molina Healthcare", "Centene Corporation", "Anthem Inc",
    "Highmark Health",
]
_STREETS = [
    "100 Elm Street", "250 Maple Avenue", "88 Oak Boulevard",
    "1500 Pine Road", "33 Birch Lane", "420 Cedar Drive",
    "77 Walnut Court", "612 Willow Way", "900 Ash Place",
    "45 Spruce Trail",
]
_CITIES_STATES_ZIPS = [
    ("Springfield", "IL", "62701"),
    ("Riverside", "CA", "92501"),
    ("Franklin", "TN", "37064"),
    ("Clinton", "IA", "52732"),
    ("Madison", "WI", "53701"),
    ("Georgetown", "TX", "78626"),
    ("Salem", "OR", "97301"),
    ("Burlington", "VT", "05401"),
    ("Columbia", "MO", "65201"),
    ("Greenville", "SC", "29601"),
]


def _random_phone(rng: random.Random) -> str:
    area = rng.randint(200, 999)
    prefix = rng.randint(200, 999)
    line = rng.randint(1000, 9999)
    return f"{area}{prefix}{line}"


def _random_dob(rng: random.Random) -> str:
    y = rng.randint(1940, 2000)
    m = rng.randint(1, 12)
    d = rng.randint(1, 28)
    return f"{y:04d}{m:02d}{d:02d}"


def _random_mrn(rng: random.Random) -> str:
    return "".join(str(rng.randint(0, 9)) for _ in range(10))


def _random_plan_id(rng: random.Random) -> str:
    prefix = rng.choice(["BC", "AE", "UH", "CG", "HM"])
    num = "".join(str(rng.randint(0, 9)) for _ in range(9))
    return f"{prefix}{num}"


def _message_timestamp(rng: random.Random) -> str:
    """Synthetic HL7 MSH-7 message timestamp, seeded from *rng* (NOT
    wall-clock time) -- see eml_gen._format_date_header for the same
    determinism rationale (DeterministicGenerator's bitwise-identical-output
    contract).
    """
    base = datetime(2024, 7, 1, tzinfo=timezone.utc)
    offset = timedelta(
        days=rng.randint(0, 729),
        hours=rng.randint(0, 23),
        minutes=rng.randint(0, 59),
        seconds=rng.randint(0, 59),
    )
    return (base + offset).strftime("%Y%m%d%H%M%S")


def _build_hl7v2_message(rng: random.Random, record_index: int) -> tuple[str, dict]:
    """Build an HL7 v2.5.1 ADT^A01 message string and a metadata dict."""
    first = rng.choice(_FIRST_NAMES)
    last = rng.choice(_LAST_NAMES)
    dob = _random_dob(rng)
    sex = rng.choice(["M", "F", "O", "U"])
    mrn = _random_mrn(rng)
    phone = _random_phone(rng)
    street = rng.choice(_STREETS)
    city, state, zipcode = rng.choice(_CITIES_STATES_ZIPS)
    address = f"{street}^^{city}^{state}^{zipcode}^USA"

    nk1_first = rng.choice(_FIRST_NAMES)
    nk1_last = rng.choice(_LAST_NAMES)
    nk1_phone = _random_phone(rng)
    nk1_relationship = rng.choice(["SPO", "MTH", "FTH", "SIB", "CHD"])

    ins_company = rng.choice(_INSURANCE_COMPANIES)
    plan_id = _random_plan_id(rng)
    ins_first = rng.choice(_FIRST_NAMES)
    ins_last = rng.choice(_LAST_NAMES)

    ts = _message_timestamp(rng)
    msg_ctrl = f"MSG{record_index:07d}"

    # Build segments. Fields separated by |. Components separated by ^.
    msh = (
        f"MSH|^~\\&|SENDING_APP|SENDING_FAC|RECEIVING_APP|RECEIVING_FAC|"
        f"{ts}||ADT^A01^ADT_A01|{msg_ctrl}|P|2.5.1|||AL|NE|"
    )
    pid = (
        f"PID|1|{mrn}|{mrn}^^^HOSPITAL^MR||"
        f"{last}^{first}^||"
        f"{dob}|{sex}|||"
        f"{address}||"
        f"{phone}^^^HOME^PH|"
    )
    nk1 = (
        f"NK1|1|{nk1_last}^{nk1_first}^|{nk1_relationship}||"
        f"{nk1_phone}^^^HOME^PH|"
    )
    in1 = (
        f"IN1|1|{plan_id}|{ins_company}||||||||||||"
        f"{ins_last}^{ins_first}^|"
    )

    message = "\r".join([msh, pid, nk1, in1]) + "\r"

    meta = {
        "patient_last": last,
        "patient_first": first,
        "dob": dob,
        "mrn": mrn,
        "phone": phone,
        "city": city,
        "nk1_last": nk1_last,
        "nk1_first": nk1_first,
        "nk1_phone": nk1_phone,
        "plan_id": plan_id,
        "ins_company": ins_company,
        "ins_last": ins_last,
        "ins_first": ins_first,
        "sex": sex,
    }
    return message, meta


class HL7v2Generator(DeterministicGenerator):
    """Generate synthetic HL7 v2.x ADT^A01 message records.

    Authority: HL7 v2.x PID/NK1/IN1 segments
               HIPAA Safe Harbor: 45 CFR 164.514(b)(2)(i)
    """

    def generate_batch(self, count: int = 20) -> List[Record]:
        records: List[Record] = []
        for i in range(count):
            rng = self.fresh(f"hl7v2:{i}")
            text, meta = _build_hl7v2_message(rng, i)

            last = meta["patient_last"]
            first = meta["patient_first"]
            dob = meta["dob"]
            mrn = meta["mrn"]
            phone = meta["phone"]
            nk1_last = meta["nk1_last"]
            nk1_first = meta["nk1_first"]
            nk1_phone = meta["nk1_phone"]
            plan_id = meta["plan_id"]
            ins_last = meta["ins_last"]
            ins_first = meta["ins_first"]

            # Build annotation spec; deduplicate identical values
            raw_specs = [
                (f"{last}^{first}", "NAME", "A", "us", AUTH_HL7V2, DETECTION_REGIME_NER),
                (dob, "DATE", "C", "us", AUTH_HL7V2, DETECTION_REGIME_RULE),
                (mrn, "MRN", "H", "us", AUTH_HL7V2, DETECTION_REGIME_RULE),
                (phone, "PHONE", "D", "us", AUTH_HL7V2, DETECTION_REGIME_RULE),
                (f"{nk1_last}^{nk1_first}", "NAME", "A", "us", AUTH_HL7V2, DETECTION_REGIME_NER),
                (nk1_phone, "PHONE", "D", "us", AUTH_HL7V2, DETECTION_REGIME_RULE),
                (plan_id, "HEALTH_PLAN_ID", "I", "us", AUTH_HL7V2, DETECTION_REGIME_RULE),
                (f"{ins_last}^{ins_first}", "NAME", "A", "us", AUTH_HL7V2, DETECTION_REGIME_NER),
            ]

            seen: set = set()
            specs = []
            for spec in raw_specs:
                v = spec[0]
                if v not in seen:
                    specs.append(spec)
                    seen.add(v)

            gold_spans = self.annotate(text, specs)

            record = Record(
                record_id=self.record_id("hl7v2", i),
                text=text,
                gold_spans=gold_spans,
                layer=LAYER_HIPAA,
                jurisdiction="us",
                detection_regime=DETECTION_REGIME_NER,
                de_id_tier="identifiable",
                risk_tier="minimal",
                context="treatment",
                format="hl7v2",
                authority_citations=[AUTH_HL7V2, AUTH_HIPAA_SAFE_HARBOR],
                metadata={"hl7_version": "2.5.1", "message_type": "ADT^A01"},
            )

            errors = record.verify_spans()
            if errors:
                raise ValueError(f"Record {i} span errors: {errors}")

            records.append(record)
        return records


def generate_corpus(seed: int = 42, count: int = 20) -> List[Record]:
    """Write HL7 v2 message corpus to corpus/file_formats/hl7v2_messages.jsonl.

    Authority: HL7 v2.x PID/NK1/IN1 segments
    """
    gen = HL7v2Generator(seed=seed)
    records = gen.generate_batch(count=count)
    out_path = Path(__file__).parent.parent.parent / "corpus" / "file_formats" / "hl7v2_messages.jsonl"
    write_jsonl(records, out_path)
    return records
