"""
FHIR R4 JSON bundle PHI corpus generator.

Builds FHIR R4 Patient bundles as raw Python dicts (no fhir.resources library
required -- that library requires Python 3.10+; this codebase targets 3.9).
The JSON schema follows HL7 FHIR R4 v4.0.1.

Authority: HL7 FHIR R4 v4.0.1 (AUTH_FHIR_R4 in generators/common.py)
           HIPAA Safe Harbor: 45 CFR 164.514(b)(2)(i)

Each record's text field is json.dumps(fhir_bundle, indent=2). Gold spans
annotate PHI values as they appear in that JSON string.
"""
from __future__ import annotations

import json
import random
import uuid
from pathlib import Path
from typing import Any, Dict, List

from generators.common import (
    AUTH_FHIR_R4,
    AUTH_HIPAA_SAFE_HARBOR,
    DETECTION_REGIME_RULE,
    DETECTION_REGIME_NER,
    LAYER_HIPAA,
    DeterministicGenerator,
    Record,
    write_jsonl,
)

_FIRST_NAMES = [
    "Alice", "Bob", "Carol", "Diana", "Edward", "Frances",
    "George", "Helen", "Ivan", "Julia", "Kevin", "Laura",
    "Marcus", "Nina", "Oscar", "Paula", "Quinn", "Rachel",
    "Steven", "Teresa",
]
_LAST_NAMES = [
    "Adams", "Baker", "Clark", "Davis", "Evans", "Foster",
    "Green", "Harris", "Ingram", "James", "King", "Lee",
    "Morgan", "Nelson", "Owen", "Parker", "Quinn", "Reed",
    "Scott", "Turner",
]
_CITIES = [
    "Atlanta", "Boston", "Chicago", "Denver", "El Paso",
    "Fresno", "Houston", "Indianapolis", "Jacksonville", "Kansas City",
]
_STATES = ["GA", "MA", "IL", "CO", "TX", "CA", "TX", "IN", "FL", "MO"]
_ZIP_CODES = [
    "30301", "02101", "60601", "80201", "79901",
    "93701", "77001", "46201", "32201", "64101",
]
_GENDERS = ["male", "female", "other", "unknown"]
_RELATIONSHIPS = ["spouse", "parent", "child", "sibling", "guardian"]


def _random_date(rng: random.Random, start: int = 1940, end: int = 2000) -> str:
    y = rng.randint(start, end)
    m = rng.randint(1, 12)
    d = rng.randint(1, 28)
    return f"{y:04d}-{m:02d}-{d:02d}"


def _random_phone(rng: random.Random) -> str:
    area = rng.randint(200, 999)
    prefix = rng.randint(200, 999)
    line = rng.randint(1000, 9999)
    return f"({area}) {prefix}-{line}"


def _random_mrn(rng: random.Random) -> str:
    return "MRN-" + "".join(str(rng.randint(0, 9)) for _ in range(8))


def _random_street(rng: random.Random) -> str:
    num = rng.randint(100, 9999)
    streets = ["Main St", "Oak Ave", "Maple Dr", "Elm Blvd", "Cedar Ln",
               "Birch Rd", "Walnut Way", "Pine St", "Willow Ct", "Ash Pl"]
    return f"{num} {rng.choice(streets)}"


def _build_patient_bundle(rng: random.Random, record_index: int) -> Dict[str, Any]:
    """Build a FHIR R4 Bundle containing a Patient resource."""
    first = rng.choice(_FIRST_NAMES)
    last = rng.choice(_LAST_NAMES)
    gender = rng.choice(_GENDERS)
    dob = _random_date(rng)
    phone = _random_phone(rng)
    mrn = _random_mrn(rng)
    idx = rng.randint(0, len(_CITIES) - 1)
    city = _CITIES[idx]
    state = _STATES[idx]
    postal = _ZIP_CODES[idx]
    street = _random_street(rng)
    patient_id = str(uuid.UUID(int=rng.getrandbits(128)))

    # Optional contact
    contact_first = rng.choice(_FIRST_NAMES)
    contact_last = rng.choice(_LAST_NAMES)
    contact_phone = _random_phone(rng)
    relationship = rng.choice(_RELATIONSHIPS)

    bundle: Dict[str, Any] = {
        "resourceType": "Bundle",
        "id": str(uuid.UUID(int=rng.getrandbits(128))),
        "type": "searchset",
        "total": 1,
        "entry": [
            {
                "fullUrl": f"urn:uuid:{patient_id}",
                "resource": {
                    "resourceType": "Patient",
                    "id": patient_id,
                    "identifier": [
                        {
                            "system": "urn:oid:2.16.840.1.113883.4.1",
                            "type": {
                                "coding": [
                                    {
                                        "system": "http://terminology.hl7.org/CodeSystem/v2-0203",
                                        "code": "MR",
                                        "display": "Medical Record Number",
                                    }
                                ]
                            },
                            "value": mrn,
                        }
                    ],
                    "name": [
                        {
                            "use": "official",
                            "family": last,
                            "given": [first],
                        }
                    ],
                    "telecom": [
                        {
                            "system": "phone",
                            "value": phone,
                            "use": "home",
                        }
                    ],
                    "gender": gender,
                    "birthDate": dob,
                    "address": [
                        {
                            "use": "home",
                            "line": [street],
                            "city": city,
                            "state": state,
                            "postalCode": postal,
                            "country": "US",
                        }
                    ],
                    "contact": [
                        {
                            "relationship": [
                                {
                                    "coding": [
                                        {
                                            "system": "http://terminology.hl7.org/CodeSystem/v2-0131",
                                            "code": "N",
                                            "display": relationship,
                                        }
                                    ]
                                }
                            ],
                            "name": {
                                "family": contact_last,
                                "given": [contact_first],
                            },
                            "telecom": [
                                {
                                    "system": "phone",
                                    "value": contact_phone,
                                }
                            ],
                        }
                    ],
                },
            }
        ],
    }

    return bundle


class FHIRGenerator(DeterministicGenerator):
    """Generate synthetic FHIR R4 Patient bundle records.

    Authority: HL7 FHIR R4 v4.0.1
               PHI categories: NAME (A), DATE (C), PHONE (D), MRN (H),
               GEOGRAPHIC (B) per 45 CFR 164.514(b)(2)(i).
    """

    def generate_batch(self, count: int = 20) -> List[Record]:
        records: List[Record] = []
        for i in range(count):
            rng = self.fresh(f"fhir:{i}")
            bundle = _build_patient_bundle(rng, i)
            text = json.dumps(bundle, indent=2)

            # Extract patient resource for annotation
            patient = bundle["entry"][0]["resource"]
            first = patient["name"][0]["given"][0]
            last = patient["name"][0]["family"]
            dob = patient["birthDate"]
            phone = patient["telecom"][0]["value"]
            mrn = patient["identifier"][0]["value"]
            city = patient["address"][0]["city"]

            # Contact fields
            contact_last = patient["contact"][0]["name"]["family"]
            contact_first = patient["contact"][0]["name"]["given"][0]
            contact_phone = patient["contact"][0]["telecom"][0]["value"]

            spans_spec = [
                (last, "NAME", "A", "us", AUTH_FHIR_R4, DETECTION_REGIME_NER),
                (first, "NAME", "A", "us", AUTH_FHIR_R4, DETECTION_REGIME_NER),
                (dob, "DATE", "C", "us", AUTH_FHIR_R4, DETECTION_REGIME_RULE),
                (phone, "PHONE", "D", "us", AUTH_FHIR_R4, DETECTION_REGIME_RULE),
                (mrn, "MRN", "H", "us", AUTH_FHIR_R4, DETECTION_REGIME_RULE),
                (city, "GEOGRAPHIC_SUBDIVISION", "B", "us", AUTH_FHIR_R4, DETECTION_REGIME_NER),
                (contact_last, "NAME", "A", "us", AUTH_FHIR_R4, DETECTION_REGIME_NER),
                (contact_first, "NAME", "A", "us", AUTH_FHIR_R4, DETECTION_REGIME_NER),
                (contact_phone, "PHONE", "D", "us", AUTH_FHIR_R4, DETECTION_REGIME_RULE),
            ]

            # Deduplicate specs where the same value appears multiple times:
            # annotate() uses text.find() which returns first occurrence.
            # If last == contact_last or first == contact_first or phone == contact_phone
            # we need to handle duplicates. Use unique values only and annotate separately
            # when they genuinely differ.
            seen_values: set = set()
            deduped_specs = []
            for spec in spans_spec:
                val = spec[0]
                if val not in seen_values:
                    deduped_specs.append(spec)
                    seen_values.add(val)

            gold_spans = self.annotate(text, deduped_specs)

            record = Record(
                record_id=self.record_id("fhir_json", i),
                text=text,
                gold_spans=gold_spans,
                layer=LAYER_HIPAA,
                jurisdiction="us",
                detection_regime=DETECTION_REGIME_NER,
                de_id_tier="identifiable",
                risk_tier="minimal",
                context="treatment",
                format="fhir_json",
                authority_citations=[AUTH_FHIR_R4, AUTH_HIPAA_SAFE_HARBOR],
                metadata={
                    "fhir_version": "R4",
                    "resource_type": "Bundle",
                },
            )

            errors = record.verify_spans()
            if errors:
                raise ValueError(f"Record {i} span errors: {errors}")

            records.append(record)
        return records


def generate_corpus(seed: int = 42, count: int = 20) -> List[Record]:
    """Write FHIR R4 bundle corpus to corpus/file_formats/fhir_bundles.jsonl.

    Authority: HL7 FHIR R4 v4.0.1
    """
    gen = FHIRGenerator(seed=seed)
    records = gen.generate_batch(count=count)
    out_path = Path(__file__).parent.parent.parent / "corpus" / "file_formats" / "fhir_bundles.jsonl"
    write_jsonl(records, out_path)
    return records
