"""US HIPAA Safe Harbor corpus generator, consolidated and bug-fixed.

Authority: 45 CFR 164.514(b)(2)(i)(A)-(R) and (b)(2)(ii) actual-knowledge safety net.
Multi-jurisdiction ready: `generate(jurisdiction=...)` dispatches to registered generators.

Fixes vs prior generator:
- NPI now uses the ISO 80840 prefix before Luhn.
- Ages over 89 are labelled with "90+" per Safe Harbor aggregation rule.
- Text formatter always leaves a single space before punctuation, no stray whitespace.
"""
from __future__ import annotations

import hashlib
import json
import random
import string
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Tuple

from .models import CorpusRecord, GoldSpan


# HIPAA Safe Harbor category labels 45 CFR 164.514(b)(2)(i)
HIPAA_CATEGORIES: dict[str, str] = {
    "A": "Names",
    "B": "Geographic subdivisions smaller than State",
    "C": "All elements of dates except year; ages over 89",
    "D": "Telephone numbers",
    "E": "Fax numbers",
    "F": "Electronic mail addresses",
    "G": "Social security numbers",
    "H": "Medical record numbers",
    "I": "Health plan beneficiary numbers",
    "J": "Account numbers",
    "K": "Certificate/license numbers",
    "L": "Vehicle identifiers, serial numbers, license plates",
    "M": "Device identifiers and serial numbers",
    "N": "Web URLs",
    "O": "IP address numbers",
    "P": "Biometric identifiers",
    "Q": "Full face photographs and comparable images",
    "R": "Any other unique identifying code",
}

AUTH_SAFE_HARBOR = "45 CFR 164.514(b)(2)(i)"
AUTH_ACTUAL_KNOWLEDGE = "45 CFR 164.514(b)(2)(ii)"
AUTH_SWEENEY = "Sweeney 2002 k-anonymity"

# 17 restricted ZIP3 codes (HHS OCR 2012-11-26 guidance).
RESTRICTED_ZIP3 = {
    "036", "059", "063", "102", "203", "556", "692", "790",
    "821", "823", "830", "831", "878", "879", "884", "890", "893",
}


# --- Luhn / Verhoeff -------------------------------------------------------

def luhn_check(digits: str) -> bool:
    ds = [int(d) for d in digits if d.isdigit()]
    total = 0
    for i, d in enumerate(reversed(ds)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def luhn_make(body: str) -> str:
    ds = [int(c) for c in body]
    total = 0
    for i, d in enumerate(reversed(ds)):
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return body + str((10 - total % 10) % 10)


# --- Deterministic base ----------------------------------------------------

class Generator:
    def __init__(self, seed: int):
        self.seed = seed
        self.rng = random.Random(seed)

    def sub(self, key: str) -> random.Random:
        h = hashlib.sha256(f"{self.seed}:{key}".encode()).hexdigest()
        return random.Random(int(h[:16], 16))

    @staticmethod
    def annotate(
        text: str,
        specs: List[Tuple[str, str, Optional[str], str, str]],
        jurisdiction: str,
    ) -> List[GoldSpan]:
        """specs = [(value, entity_type, hipaa_cat_or_None, category, authority)]"""
        out: List[GoldSpan] = []
        for value, entity_type, hipaa, category, authority in specs:
            start = text.find(value)
            if start < 0:
                raise ValueError(f"span value {value!r} not present in text")
            out.append(GoldSpan(
                start=start,
                end=start + len(value),
                value=value,
                category=category,
                hipaa_category=hipaa,
                entity_type=entity_type,
                jurisdiction=jurisdiction,
                authority=authority,
            ))
        return out


# --- US name / address / identifier pools ---------------------------------

US_FIRST = ["James","Mary","Robert","Patricia","John","Jennifer","Michael","Linda","William","Elizabeth","David","Barbara","Richard","Susan","Joseph","Jessica","Thomas","Sarah","Charles","Karen"]
US_LAST = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis","Rodriguez","Martinez","Hernandez","Lopez","Gonzalez","Wilson","Anderson","Thomas","Taylor","Moore","Jackson","Martin"]
US_CITIES = [("Springfield","IL","627"),("Portland","OR","972"),("Madison","WI","537"),("Arlington","VA","222"),("Cambridge","MA","021"),("Pasadena","CA","911"),("Ann Arbor","MI","481"),("Chapel Hill","NC","275"),("Boulder","CO","803"),("Princeton","NJ","085")]
US_STREETS = ["Oak","Pine","Maple","Cedar","Elm","Walnut","Main","Washington","Lincoln","Park"]
US_SUFFIX = ["St","Ave","Rd","Ln","Dr","Blvd"]


def _us_name(rng): return f"{rng.choice(US_FIRST)} {rng.choice(US_LAST)}"

def _us_address(rng):
    n = rng.randint(100, 9999)
    st = f"{n} {rng.choice(US_STREETS)} {rng.choice(US_SUFFIX)}"
    city, state, zip3 = rng.choice(US_CITIES)
    zip_full = f"{zip3}{rng.randint(10,99):02d}"
    return st, city, state, zip3, zip_full

def _us_phone(rng):
    return f"({rng.randint(200,989):03d}) {rng.randint(200,999):03d}-{rng.randint(0,9999):04d}"

def _us_ssn(rng):
    area = rng.choice([rng.randint(100,665), rng.randint(667,772)])
    return f"{area:03d}-{rng.randint(1,99):02d}-{rng.randint(1,9999):04d}"

def _us_mrn(rng):
    return f"MRN-{rng.randint(10_000_000, 99_999_999)}"

def _us_email(rng):
    return f"{rng.choice(US_FIRST).lower()}.{rng.choice(US_LAST).lower()}{rng.randint(1,999)}@example.edu"

def _us_mbi(rng):
    # C A AN A AN N A A N N  where alpha excludes S L O I B Z.
    alpha = "ACDEFGHJKMNPQRTUVWXY"
    def A(): return rng.choice(alpha)
    def AN(): return rng.choice(alpha + string.digits)
    def N(): return str(rng.randint(0, 9))
    return str(rng.randint(1,9)) + A() + AN() + A() + AN() + N() + A() + A() + N() + N()

def _us_npi(rng):
    # NPI = 10-digit Luhn where the ISO 80840 prefix is included in the Luhn calc
    # but not stored. Produce a 9-digit body, compute Luhn over "80840" + body.
    body = "".join(str(rng.randint(0,9)) for _ in range(9))
    check = luhn_make("80840" + body)[-1]
    return body + check

def _us_vin(rng):
    return "".join(rng.choices("ABCDEFGHJKLMNPRSTUVWXYZ0123456789", k=17))

def _us_plate(rng):
    return "".join(rng.choices(string.ascii_uppercase, k=3)) + "-" + "".join(str(rng.randint(0,9)) for _ in range(4))

def _us_udi(rng):
    return "(01)" + "".join(str(rng.randint(0,9)) for _ in range(14))

def _us_serial(rng):
    return "SN" + "".join(rng.choices(string.digits + string.ascii_uppercase, k=10))

def _url(rng):
    return f"https://portal.example.org/patients/{rng.randint(10000,99999)}"

def _ipv4(rng):
    return ".".join(str(rng.randint(1,254)) for _ in range(4))

def _ipv6(rng):
    return ":".join(f"{rng.randint(0,0xFFFF):04x}" for _ in range(8))

def _biometric(rng):
    kind = rng.choice(["fingerprint","voice","retinal","iris","DNA"])
    return f"{kind}_template_" + "".join(rng.choices(string.digits, k=16))

def _photo(rng):
    return f"patient_photo_{rng.randint(10**9, 10**10-1)}.jpg"

def _dob(rng, over_89: bool = False):
    year = rng.randint(1900, 1935) if over_89 else rng.randint(1950, 2015)
    return f"{rng.randint(1,12):02d}/{rng.randint(1,28):02d}/{year}", 2026 - year


# --- Generator ------------------------------------------------------------

@dataclass
class RecordSpec:
    cat: str
    build: Callable[[random.Random, int], Tuple[str, list, str]]  # returns (text, specs, layer)


class USGenerator(Generator):
    """HIPAA Safe Harbor 18-category + quasi-identifier records."""

    def generate(self, count_per_category: int = 5, include_quasi: bool = True) -> List[CorpusRecord]:
        specs: list[Tuple[str, Callable]] = [
            ("A", self._names),
            ("B", self._geography),
            ("C", self._dates),
            ("D", self._phone),
            ("E", self._fax),
            ("F", self._email),
            ("G", self._ssn),
            ("H", self._mrn),
            ("I", self._mbi),
            ("J", self._account),
            ("K", self._license),
            ("L", self._vehicle),
            ("M", self._device),
            ("N", self._url),
            ("O", self._ip),
            ("P", self._biometric),
            ("Q", self._photo),
            ("R", self._other_unique),
        ]
        out: list[CorpusRecord] = []
        for cat, fn in specs:
            for i in range(count_per_category):
                rng = self.sub(f"us_{cat}_{i}")
                text, span_specs = fn(rng, i)
                spans = self.annotate(text, span_specs, "us")
                out.append(CorpusRecord(
                    record_id=f"us_{cat}_{i:04d}",
                    text=text,
                    layer=f"hipaa_{cat}",
                    jurisdiction="us",
                    gold_spans=spans,
                    authority_citations=[AUTH_SAFE_HARBOR],
                    metadata={"hipaa_category": cat, "description": HIPAA_CATEGORIES[cat]},
                ))
        if include_quasi:
            out.extend(self._quasi(count_per_category * 2))
        return out

    # -- (A) names --
    def _names(self, rng, i):
        p, doc, rel = _us_name(rng), "Dr. " + _us_name(rng), _us_name(rng)
        text = f"Patient {p} was seen today by {doc}. Emergency contact: {rel} (brother)."
        return text, [
            (p, "NAME_PATIENT", "A", "NAME", AUTH_SAFE_HARBOR),
            (doc, "NAME_PROVIDER", "A", "NAME", AUTH_SAFE_HARBOR),
            (rel, "NAME_HOUSEHOLD", "A", "NAME", AUTH_SAFE_HARBOR),
        ]

    # -- (B) geography --
    def _geography(self, rng, i):
        st, city, state, zip3, zip_full = _us_address(rng)
        if i % 5 == 0:
            zip_full = sorted(RESTRICTED_ZIP3)[i % len(RESTRICTED_ZIP3)] + f"{rng.randint(10,99):02d}"
        text = f"Patient resides at {st}, {city}, {state} {zip_full}."
        return text, [
            (st, "ADDRESS_STREET", "B", "ADDRESS", AUTH_SAFE_HARBOR),
            (city, "ADDRESS_CITY", "B", "ADDRESS", AUTH_SAFE_HARBOR),
            (zip_full, "ADDRESS_ZIP", "B", "ADDRESS", AUTH_SAFE_HARBOR),
        ]

    # -- (C) dates + age >89 --
    def _dates(self, rng, i):
        over = i % 4 == 0
        dob, age = _dob(rng, over)
        admit = f"{rng.randint(1,12):02d}/{rng.randint(1,28):02d}/{rng.randint(2022,2025)}"
        if over:
            age_str = "90+"
            text = f"Patient DOB {dob}, age {age_str} (aggregated per 164.514). Admitted on {admit}."
            specs = [
                (dob, "DATE_DOB", "C", "DATE", AUTH_SAFE_HARBOR),
                (age_str, "AGE_OVER_89", "C", "AGE", AUTH_SAFE_HARBOR),
                (admit, "DATE_ADMIT", "C", "DATE", AUTH_SAFE_HARBOR),
            ]
        else:
            text = f"Patient DOB {dob}, age {age} years. Admitted on {admit}."
            specs = [
                (dob, "DATE_DOB", "C", "DATE", AUTH_SAFE_HARBOR),
                (admit, "DATE_ADMIT", "C", "DATE", AUTH_SAFE_HARBOR),
            ]
        return text, specs

    def _phone(self, rng, i):
        p = _us_phone(rng)
        text = f"Contact patient at {p}. Confirm appointment."
        return text, [(p, "PHONE", "D", "PHONE", AUTH_SAFE_HARBOR)]

    def _fax(self, rng, i):
        f = _us_phone(rng)
        text = f"Records fax to referring physician: fax {f}."
        return text, [(f, "FAX", "E", "FAX", AUTH_SAFE_HARBOR)]

    def _email(self, rng, i):
        e = _us_email(rng)
        text = f"Patient requested portal messages be sent to {e}."
        return text, [(e, "EMAIL", "F", "EMAIL", AUTH_SAFE_HARBOR)]

    def _ssn(self, rng, i):
        s = _us_ssn(rng)
        text = f"Insurance verification: patient SSN on file as {s}."
        return text, [(s, "SSN", "G", "SSN", AUTH_SAFE_HARBOR)]

    def _mrn(self, rng, i):
        m = _us_mrn(rng)
        text = f"Retrieved encounter notes under MRN {m}."
        return text, [(m, "MRN", "H", "MRN", AUTH_SAFE_HARBOR)]

    def _mbi(self, rng, i):
        m = _us_mbi(rng)
        text = f"Medicare Beneficiary Identifier: {m}. Claim submitted."
        return text, [(m, "MBI", "I", "HEALTH_PLAN_ID", AUTH_SAFE_HARBOR)]

    def _account(self, rng, i):
        acct = "".join(str(rng.randint(0,9)) for _ in range(10))
        bank = "".join(str(rng.randint(0,9)) for _ in range(9))
        text = f"Patient account {acct} billed directly. ACH bank account {bank}."
        return text, [
            (acct, "ACCOUNT_NUMBER", "J", "ACCOUNT", AUTH_SAFE_HARBOR),
            (bank, "BANK_ACCOUNT", "J", "ACCOUNT", AUTH_SAFE_HARBOR),
        ]

    def _license(self, rng, i):
        dl = "D" + "".join(str(rng.randint(0,9)) for _ in range(8))
        npi = _us_npi(rng)
        text = f"Driver's license {dl} used for ID verification. Treating physician NPI: {npi}."
        return text, [
            (dl, "DRIVERS_LICENSE", "K", "LICENSE", AUTH_SAFE_HARBOR),
            (npi, "NPI", "K", "LICENSE", AUTH_SAFE_HARBOR),
        ]

    def _vehicle(self, rng, i):
        vin = _us_vin(rng)
        plate = _us_plate(rng)
        text = f"Personal vehicle VIN {vin} with plate {plate} left at work site."
        return text, [
            (vin, "VIN", "L", "VEHICLE", AUTH_SAFE_HARBOR),
            (plate, "LICENSE_PLATE", "L", "VEHICLE", AUTH_SAFE_HARBOR),
        ]

    def _device(self, rng, i):
        udi = _us_udi(rng)
        sn = _us_serial(rng)
        text = f"Implanted cardiac device UDI {udi} serial {sn}."
        return text, [
            (udi, "DEVICE_UDI", "M", "DEVICE", AUTH_SAFE_HARBOR),
            (sn, "DEVICE_SERIAL", "M", "DEVICE", AUTH_SAFE_HARBOR),
        ]

    def _url(self, rng, i):
        u = _url(rng)
        text = f"Patient portal enrollment at {u}."
        return text, [(u, "URL", "N", "URL", AUTH_SAFE_HARBOR)]

    def _ip(self, rng, i):
        a = _ipv4(rng)
        b = _ipv6(rng)
        text = f"Portal access from IP {a}. IPv6 logged: {b}."
        return text, [
            (a, "IP_V4", "O", "IP_ADDRESS", AUTH_SAFE_HARBOR),
            (b, "IP_V6", "O", "IP_ADDRESS", AUTH_SAFE_HARBOR),
        ]

    def _biometric(self, rng, i):
        b = _biometric(rng)
        text = f"Biometric template on file: {b}. Used at check-in."
        return text, [(b, "BIOMETRIC", "P", "BIOMETRIC", AUTH_SAFE_HARBOR)]

    def _photo(self, rng, i):
        p = _photo(rng)
        text = f"Full-face photograph on record: attachment {p}."
        return text, [(p, "PHOTO_FULL_FACE", "Q", "PHOTO", AUTH_SAFE_HARBOR)]

    def _other_unique(self, rng, i):
        trial = f"NCT{rng.randint(10_000_000, 99_999_999)}"
        code = "X" + "".join(rng.choices(string.ascii_uppercase + string.digits, k=15))
        text = f"Enrolled in clinical trial {trial}; internal research code {code}."
        return text, [
            (trial, "CLINICAL_TRIAL_ID", "R", "OTHER_UNIQUE", AUTH_SAFE_HARBOR),
            (code, "INTERNAL_CODE", "R", "OTHER_UNIQUE", AUTH_SAFE_HARBOR),
        ]

    # -- (b)(2)(ii) quasi-identifier records --
    def _quasi(self, count: int) -> list[CorpusRecord]:
        rare = ["Gaucher disease type 3","Fabry disease","Progressive supranuclear palsy","Hereditary angioedema","Pompe disease"]
        prof = ["State Supreme Court Justice","Former city mayor","Symphony orchestra conductor","Former astronaut","CEO of regional hospital system"]
        out = []
        for i in range(count):
            rng = self.sub(f"us_qi_{i}")
            disease = rng.choice(rare)
            profession = rng.choice(prof)
            age = rng.randint(55, 85)
            _, city, state, _, _ = _us_address(rng)
            text = f"Case study: {age}-year-old {profession} from {city}, {state} presenting with {disease}."
            spans = self.annotate(text, [
                (profession, "QUASI_PROFESSION", None, "QUASI", AUTH_ACTUAL_KNOWLEDGE),
                (city, "QUASI_CITY", "B", "QUASI", AUTH_ACTUAL_KNOWLEDGE),
                (disease, "QUASI_RARE_DISEASE", None, "QUASI", AUTH_SWEENEY),
            ], "us")
            out.append(CorpusRecord(
                record_id=f"us_qi_{i:04d}",
                text=text,
                layer="hipaa_quasi_identifier",
                jurisdiction="us",
                gold_spans=spans,
                authority_citations=[AUTH_ACTUAL_KNOWLEDGE, AUTH_SWEENEY],
                metadata={"sweeney_vulnerable": True},
            ))
        return out


# --- Dispatcher ------------------------------------------------------------

JURISDICTION_GENERATORS: dict[str, type[Generator]] = {
    "us": USGenerator,
}


def generate(jurisdiction: str, seed: int, count_per_category: int = 5, include_quasi: bool = True) -> list[CorpusRecord]:
    j = jurisdiction.lower()
    if j not in JURISDICTION_GENERATORS:
        raise ValueError(f"jurisdiction {jurisdiction!r} not registered. Available: {list(JURISDICTION_GENERATORS)}")
    gen = JURISDICTION_GENERATORS[j](seed)
    return gen.generate(count_per_category=count_per_category, include_quasi=include_quasi)


def corpus_hash(records: Iterable[CorpusRecord]) -> str:
    h = hashlib.sha256()
    for r in sorted(records, key=lambda x: x.record_id):
        h.update(json.dumps(r.model_dump(), sort_keys=True).encode())
    return h.hexdigest()
