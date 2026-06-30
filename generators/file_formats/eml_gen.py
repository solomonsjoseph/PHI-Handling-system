"""
RFC 5322 email message PHI corpus generator.

Builds synthetic email messages with PHI in:
  - From / To / CC headers (patient names + email addresses)
  - Subject (patient name or MRN)
  - X-Patient-ID custom header
  - Body (clinical narrative: patient name, DOB, MRN, phone)

Authority: 45 CFR 164.514(b)(2)(i) HIPAA Safe Harbor
           (emails containing PHI are covered health information)
           See AUTHORITY_MATRIX.md Table A row F (Electronic mail addresses)
           and row A (Names).
"""
from __future__ import annotations

import random
from datetime import datetime, timezone
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

AUTH_EML = "45 CFR 164.514(b)(2)(i) HIPAA Safe Harbor"

_FIRST_NAMES = [
    "Aaron", "Beth", "Carl", "Dana", "Eric",
    "Fiona", "Gary", "Holly", "Ian", "Janet",
    "Kyle", "Lisa", "Mark", "Nancy", "Owen",
    "Pam", "Ray", "Sandra", "Tim", "Uma",
]
_LAST_NAMES = [
    "Archer", "Burke", "Cross", "Drake", "Ellis",
    "Ford", "Grant", "Hall", "Irwin", "Jordan",
    "Knox", "Lowe", "Miles", "Nash", "Orme",
    "Price", "Quinn", "Riley", "Stone", "Todd",
]
_PROVIDER_DOMAINS = [
    "generalhosp.org", "universitymed.edu", "regionalhealth.com",
    "communitymc.net", "stfrancis.org", "memorialhealth.org",
]
_PATIENT_DOMAINS = [
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com",
    "aol.com", "proton.me", "icloud.com",
]
_DIAGNOSES = [
    "Type 2 diabetes mellitus", "Hypertension", "Chronic kidney disease stage 3",
    "Major depressive disorder", "Osteoarthritis of the knee",
    "Atrial fibrillation", "Hypothyroidism", "Asthma",
    "Heart failure with reduced ejection fraction", "Chronic obstructive pulmonary disease",
]
_MEDICATIONS = [
    "metformin 500 mg twice daily", "lisinopril 10 mg once daily",
    "atorvastatin 40 mg at bedtime", "levothyroxine 50 mcg once daily",
    "amlodipine 5 mg once daily", "omeprazole 20 mg once daily",
    "sertraline 50 mg once daily", "albuterol inhaler as needed",
]


def _random_dob(rng: random.Random) -> str:
    y = rng.randint(1940, 2000)
    m = rng.randint(1, 12)
    d = rng.randint(1, 28)
    return f"{m:02d}/{d:02d}/{y:04d}"


def _random_phone(rng: random.Random) -> str:
    area = rng.randint(200, 999)
    prefix = rng.randint(200, 999)
    line = rng.randint(1000, 9999)
    return f"({area}) {prefix}-{line}"


def _random_mrn(rng: random.Random) -> str:
    return "MRN-" + "".join(str(rng.randint(0, 9)) for _ in range(7))


def _email_address(first: str, last: str, domain: str, rng: random.Random) -> str:
    num = rng.randint(1, 99)
    return f"{first.lower()}.{last.lower()}{num}@{domain}"


def _format_date_header() -> str:
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")


def _build_eml_message(rng: random.Random, record_index: int) -> tuple[str, dict]:
    """Build an RFC 5322 email string and a metadata dict of PHI values."""
    # Patient
    p_first = rng.choice(_FIRST_NAMES)
    p_last = rng.choice(_LAST_NAMES)
    p_dob = _random_dob(rng)
    p_phone = _random_phone(rng)
    p_mrn = _random_mrn(rng)
    p_domain = rng.choice(_PATIENT_DOMAINS)
    p_email = _email_address(p_first, p_last, p_domain, rng)

    # Sending provider
    prov_first = rng.choice(_FIRST_NAMES)
    prov_last = rng.choice(_LAST_NAMES)
    prov_domain = rng.choice(_PROVIDER_DOMAINS)
    prov_email = f"{prov_first.lower()}.{prov_last.lower()}@{prov_domain}"

    # CC provider
    cc_first = rng.choice(_FIRST_NAMES)
    cc_last = rng.choice(_LAST_NAMES)
    cc_email = f"{cc_first.lower()}.{cc_last.lower()}@{prov_domain}"

    diagnosis = rng.choice(_DIAGNOSES)
    medication = rng.choice(_MEDICATIONS)
    appt_date = f"{rng.randint(1, 28):02d}/{rng.randint(1, 12):02d}/{rng.randint(2024, 2026)}"

    subject = f"Patient {p_last}, {p_first} - Follow-up | {p_mrn}"

    body = (
        f"Dear Dr. {prov_last},\r\n"
        f"\r\n"
        f"I am writing regarding your patient {p_first} {p_last} (DOB: {p_dob}, "
        f"MRN: {p_mrn}).\r\n"
        f"\r\n"
        f"Primary diagnosis: {diagnosis}.\r\n"
        f"Current medication: {medication}.\r\n"
        f"\r\n"
        f"The patient's contact number is {p_phone}. They have been scheduled "
        f"for a follow-up appointment on {appt_date}.\r\n"
        f"\r\n"
        f"Please review the attached records and contact the patient directly "
        f"if further information is required.\r\n"
        f"\r\n"
        f"Best regards,\r\n"
        f"Dr. {cc_first} {cc_last}\r\n"
        f"{prov_domain}\r\n"
    )

    headers = (
        f"From: Dr. {prov_first} {prov_last} <{prov_email}>\r\n"
        f"To: {p_first} {p_last} <{p_email}>\r\n"
        f"CC: Dr. {cc_first} {cc_last} <{cc_email}>\r\n"
        f"Subject: {subject}\r\n"
        f"Date: {_format_date_header()}\r\n"
        f"Message-ID: <MSG{record_index:07d}@{prov_domain}>\r\n"
        f"X-Patient-ID: {p_mrn}\r\n"
        f"MIME-Version: 1.0\r\n"
        f"Content-Type: text/plain; charset=UTF-8\r\n"
        f"\r\n"
    )

    message = headers + body

    meta = {
        "p_first": p_first,
        "p_last": p_last,
        "p_email": p_email,
        "p_dob": p_dob,
        "p_phone": p_phone,
        "p_mrn": p_mrn,
        "prov_first": prov_first,
        "prov_last": prov_last,
        "prov_email": prov_email,
        "cc_first": cc_first,
        "cc_last": cc_last,
        "cc_email": cc_email,
        "subject": subject,
        "body_full_name": f"{p_first} {p_last}",
    }
    return message, meta


class EMLGenerator(DeterministicGenerator):
    """Generate synthetic RFC 5322 email message records containing PHI.

    Authority: 45 CFR 164.514(b)(2)(i) HIPAA Safe Harbor
    """

    def generate_batch(self, count: int = 20) -> List[Record]:
        records: List[Record] = []
        for i in range(count):
            rng = self.fresh(f"eml:{i}")
            text, meta = _build_eml_message(rng, i)

            p_first = meta["p_first"]
            p_last = meta["p_last"]
            p_email = meta["p_email"]
            p_dob = meta["p_dob"]
            p_phone = meta["p_phone"]
            p_mrn = meta["p_mrn"]
            prov_email = meta["prov_email"]
            cc_email = meta["cc_email"]

            # Annotate: patient name appears multiple times; find first occurrence
            # in body "p_first p_last"
            body_name = meta["body_full_name"]

            raw_specs = [
                (p_email, "EMAIL", "F", "us", AUTH_EML, DETECTION_REGIME_RULE),
                (p_mrn, "MRN", "H", "us", AUTH_EML, DETECTION_REGIME_RULE),
                (p_dob, "DATE", "C", "us", AUTH_EML, DETECTION_REGIME_RULE),
                (p_phone, "PHONE", "D", "us", AUTH_EML, DETECTION_REGIME_RULE),
                (prov_email, "EMAIL", "F", "us", AUTH_EML, DETECTION_REGIME_RULE),
                (cc_email, "EMAIL", "F", "us", AUTH_EML, DETECTION_REGIME_RULE),
                (body_name, "NAME", "A", "us", AUTH_EML, DETECTION_REGIME_NER),
            ]

            # Deduplicate
            seen: set = set()
            specs = []
            for spec in raw_specs:
                v = spec[0]
                if v not in seen:
                    specs.append(spec)
                    seen.add(v)

            gold_spans = self.annotate(text, specs)

            record = Record(
                record_id=self.record_id("eml", i),
                text=text,
                gold_spans=gold_spans,
                layer=LAYER_HIPAA,
                jurisdiction="us",
                detection_regime=DETECTION_REGIME_NER,
                de_id_tier="identifiable",
                risk_tier="minimal",
                context="treatment",
                format="eml",
                authority_citations=[AUTH_EML, AUTH_HIPAA_SAFE_HARBOR],
                metadata={"rfc": "5322", "mime_version": "1.0"},
            )

            errors = record.verify_spans()
            if errors:
                raise ValueError(f"Record {i} span errors: {errors}")

            records.append(record)
        return records


def generate_corpus(seed: int = 42, count: int = 20) -> List[Record]:
    """Write EML corpus to corpus/file_formats/eml_messages.jsonl.

    Authority: 45 CFR 164.514(b)(2)(i) HIPAA Safe Harbor
    """
    gen = EMLGenerator(seed=seed)
    records = gen.generate_batch(count=count)
    out_path = Path(__file__).parent.parent.parent / "corpus" / "file_formats" / "eml_messages.jsonl"
    write_jsonl(records, out_path)
    return records
