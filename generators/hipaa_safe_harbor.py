"""
HIPAA Safe Harbor 18-category generator.

Produces test records covering each of the 18 identifier categories at
45 CFR 164.514(b)(2)(i)(A) through (R), with dedicated attention to the
'actual knowledge' safety net at 164.514(b)(2)(ii).

Every record has:
- An identifiable version (PHI present)
- Gold spans with HIPAA category (A-R) tagged
- Authority citation on every span

Categories covered (closing prior-corpus gaps):
- (E) Fax — now distinct from PHONE
- (L) Vehicle identifiers including VIN (17-char) and US plate patterns
- (M) Device identifiers including UDI-DI and serial numbers
- (N) Web URLs
- (O) IP addresses (v4 and v6)
- (P) Biometric identifier references (fingerprint, voice, retinal, DNA)
- (Q) Full-face photograph references (via attachment-like metadata)
- Plus the "no actual knowledge" quasi-identifier combinations
"""
from __future__ import annotations

import string
from typing import List

from .common import (
    AUTH_HIPAA_ACTUAL_KNOWLEDGE,
    AUTH_HIPAA_SAFE_HARBOR,
    AUTH_SWEENEY_K_ANON,
    DETECTION_REGIME_CONFLICT,
    DETECTION_REGIME_NER,
    DETECTION_REGIME_RULE,
    LAYER_CONFLICT,
    LAYER_HIPAA,
    DeterministicGenerator,
    GoldSpan,
    HIPAA_CATEGORIES,
    RESTRICTED_ZIP3,
    Record,
    luhn_make,
)


# -----------------------------------------------------------------------------
# Name pools (US research-ethics convention: common but not real)
# -----------------------------------------------------------------------------

US_FIRST_NAMES = [
    "James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael",
    "Linda", "William", "Elizabeth", "David", "Barbara", "Richard", "Susan",
    "Joseph", "Jessica", "Thomas", "Sarah", "Charles", "Karen", "Christopher",
    "Nancy", "Daniel", "Lisa", "Matthew", "Margaret", "Anthony", "Betty",
    "Mark", "Sandra", "Donald", "Ashley", "Steven", "Dorothy", "Paul", "Kimberly",
]

US_LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez",
    "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
    "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark",
    "Ramirez", "Lewis", "Robinson", "Walker", "Young", "Allen", "King",
]

US_CITIES = [
    ("Springfield", "IL", "627"),
    ("Portland", "OR", "972"),
    ("Madison", "WI", "537"),
    ("Arlington", "VA", "222"),
    ("Fairfax", "VA", "220"),
    ("Cambridge", "MA", "021"),
    ("Pasadena", "CA", "911"),
    ("Evanston", "IL", "602"),
    ("Berkeley", "CA", "947"),
    ("Ann Arbor", "MI", "481"),
    ("Chapel Hill", "NC", "275"),
    ("Boulder", "CO", "803"),
    ("Princeton", "NJ", "085"),
    ("New Haven", "CT", "065"),
    ("Durham", "NC", "277"),
]

US_STREET_NAMES = [
    "Oak", "Pine", "Maple", "Cedar", "Elm", "Birch", "Walnut", "Chestnut",
    "Main", "Washington", "Lincoln", "Jefferson", "Madison", "Adams", "Park",
    "Church", "Mill", "School", "Spring", "River",
]

US_STREET_SUFFIXES = ["St", "Ave", "Rd", "Ln", "Dr", "Blvd", "Way", "Ct"]


def us_name(rng) -> str:
    return f"{rng.choice(US_FIRST_NAMES)} {rng.choice(US_LAST_NAMES)}"


def us_address(rng) -> tuple[str, str, str, str, str]:
    """Returns (street, city, state, zip3, zip_full)."""
    number = rng.randint(100, 9999)
    street_name = rng.choice(US_STREET_NAMES)
    suffix = rng.choice(US_STREET_SUFFIXES)
    street = f"{number} {street_name} {suffix}"
    city, state, zip3 = rng.choice(US_CITIES)
    zip_full = f"{zip3}{rng.randint(10, 99):02d}"
    return street, city, state, zip3, zip_full


def us_ssn(rng) -> str:
    # Avoid obviously invalid area numbers (666, 9xx, 000)
    area = rng.choice([
        rng.randint(100, 299),
        rng.randint(300, 499),
        rng.randint(500, 665),
        rng.randint(667, 699),
        rng.randint(700, 772),
    ])
    group = rng.randint(1, 99)
    serial = rng.randint(1, 9999)
    return f"{area:03d}-{group:02d}-{serial:04d}"


def us_phone(rng) -> str:
    area = rng.randint(200, 989)
    exch = rng.randint(200, 999)
    line = rng.randint(0, 9999)
    return f"({area:03d}) {exch:03d}-{line:04d}"


def us_fax(rng) -> str:
    # Same format as phone; distinguished only by context label
    return us_phone(rng)


def us_mrn(rng) -> str:
    """Medical record number — format varies by institution, we simulate several."""
    fmt = rng.choice(["MRN", "PT", "HAR", ""])
    digits = rng.randint(10_000_000, 99_999_999)
    if fmt:
        return f"{fmt}-{digits}"
    return str(digits)


def us_credit_card(rng) -> str:
    # Start with Visa (4) or MC (5)
    prefix = rng.choice(["4", "5"])
    body = prefix + "".join(str(rng.randint(0, 9)) for _ in range(14))
    return luhn_make(body)


def us_license_plate(rng) -> str:
    """US state license plate — simple 3 letters + 4 digits pattern."""
    letters = "".join(rng.choices(string.ascii_uppercase, k=3))
    digits = "".join(str(rng.randint(0, 9)) for _ in range(4))
    return f"{letters}-{digits}"


def us_vin(rng) -> str:
    """17-character VIN (no I, O, Q to avoid confusion with 1, 0)."""
    allowed = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"
    return "".join(rng.choices(allowed, k=17))


def us_device_udi(rng) -> str:
    """UDI-DI format: GS1 / HIBCC / ICCBBA — we use GS1 (01) + 14-digit GTIN."""
    gtin = "".join(str(rng.randint(0, 9)) for _ in range(14))
    return f"(01){gtin}"


def us_device_serial(rng) -> str:
    """Generic device serial number."""
    return "SN" + "".join(rng.choices(string.digits + string.ascii_uppercase, k=10))


def url(rng) -> str:
    domains = ["example.com", "test.org", "research.edu", "clinic.net", "hospital.io"]
    paths = ["patients", "records", "visits", "labs", "imaging"]
    user_id = rng.randint(10000, 99999)
    return f"https://portal.{rng.choice(domains)}/{rng.choice(paths)}/{user_id}"


def ipv4(rng) -> str:
    return ".".join(str(rng.randint(1, 254)) for _ in range(4))


def ipv6(rng) -> str:
    return ":".join(f"{rng.randint(0, 0xFFFF):04x}" for _ in range(8))


def biometric_reference(rng) -> str:
    """Biometric identifier reference — format-only (actual biometric data is not text)."""
    kind = rng.choice(["fingerprint", "voice", "retinal", "iris", "DNA"])
    ident = "".join(rng.choices(string.digits, k=16))
    return f"{kind}_template_{ident}"


def photo_reference(rng) -> str:
    """Full-face photograph reference."""
    ident = "".join(rng.choices(string.digits, k=10))
    ext = rng.choice(["jpg", "png", "dcm"])
    return f"patient_photo_{ident}.{ext}"


def health_plan_beneficiary(rng) -> str:
    """US Medicare Beneficiary Identifier (MBI) — 11 alphanumeric, specific format."""
    # MBI format: C A AN A AN N A A N N (C=1-9 no 0; A=alpha excl S,L,O,I,B,Z; AN=alpha or numeric)
    c1 = str(rng.randint(1, 9))
    alpha_pool = "ACDEFGHJKMNPQRTUVWXY"  # exclude SLOIBZ
    a1 = rng.choice(alpha_pool)
    an1 = rng.choice(alpha_pool + string.digits)
    a2 = rng.choice(alpha_pool)
    an2 = rng.choice(alpha_pool + string.digits)
    n1 = str(rng.randint(0, 9))
    a3 = rng.choice(alpha_pool)
    a4 = rng.choice(alpha_pool)
    n2 = str(rng.randint(0, 9))
    n3 = str(rng.randint(0, 9))
    return c1 + a1 + an1 + a2 + an2 + n1 + a3 + a4 + n2 + n3


def us_npi(rng) -> str:
    """National Provider Identifier — 10-digit with Luhn check. Prefix 80840 for ISO."""
    # Actual NPI algorithm prepends 80840 before Luhn
    body = "".join(str(rng.randint(0, 9)) for _ in range(9))
    # For simplicity, we produce Luhn-valid 10-digit numbers
    return luhn_make(body)


# -----------------------------------------------------------------------------
# Main generator
# -----------------------------------------------------------------------------

class HIPAASafeHarborGenerator(DeterministicGenerator):
    """Produces narrative records with all 18 HIPAA Safe Harbor categories.

    Each generator method produces ONE record focused on one or two categories,
    with other fields present as realistic context.
    """

    def generate_batch(self, count_per_category: int = 10) -> List[Record]:
        """Generate `count_per_category` records for each of the 18 categories."""
        records: List[Record] = []
        generators = [
            ("A", self._gen_names),
            ("B", self._gen_geography),
            ("C", self._gen_dates_ages),
            ("D", self._gen_phone),
            ("E", self._gen_fax),
            ("F", self._gen_email),
            ("G", self._gen_ssn),
            ("H", self._gen_mrn),
            ("I", self._gen_health_plan),
            ("J", self._gen_account),
            ("K", self._gen_license),
            ("L", self._gen_vehicle),
            ("M", self._gen_device),
            ("N", self._gen_url),
            ("O", self._gen_ip),
            ("P", self._gen_biometric),
            ("Q", self._gen_photo),
            ("R", self._gen_other_unique),
        ]
        for cat, fn in generators:
            for i in range(count_per_category):
                rng = self.fresh(f"hipaa_{cat}_{i}")
                records.append(fn(rng, i))
        return records

    # Detection regime per HIPAA category (i2b2 taxonomy, arXiv 2412.10918):
    # rule_applicable: E(FAX), F(EMAIL), G(SSN), I(HEALTH_PLAN), J(ACCOUNT), K(LICENSE), L(VEHICLE/VIN), N(URL), O(IP)
    # contextual_ner_required: A(NAMES), C(DATES), D(PHONE), H(MRN), M(DEVICE), P(BIOMETRIC), Q(PHOTO), R(OTHER)
    # conflict_case: B(GEOGRAPHY/ZIP) -- ZIP is PHI under HIPAA; not enumerated under GDPR without linkage
    _CATEGORY_REGIME = {
        "A": DETECTION_REGIME_NER,
        "B": DETECTION_REGIME_CONFLICT,  # ZIP conflict case: HIPAA PHI vs GDPR not-enumerated
        "C": DETECTION_REGIME_NER,
        "D": DETECTION_REGIME_NER,
        "E": DETECTION_REGIME_RULE,
        "F": DETECTION_REGIME_RULE,
        "G": DETECTION_REGIME_RULE,
        "H": DETECTION_REGIME_NER,
        "I": DETECTION_REGIME_RULE,
        "J": DETECTION_REGIME_RULE,
        "K": DETECTION_REGIME_RULE,
        "L": DETECTION_REGIME_RULE,
        "M": DETECTION_REGIME_NER,
        "N": DETECTION_REGIME_RULE,
        "O": DETECTION_REGIME_RULE,
        "P": DETECTION_REGIME_NER,
        "Q": DETECTION_REGIME_NER,
        "R": DETECTION_REGIME_NER,
    }

    def _record(
        self,
        cat: str,
        index: int,
        text: str,
        spans_spec,
        **kwargs,
    ) -> Record:
        regime = self._CATEGORY_REGIME.get(cat, DETECTION_REGIME_NER)
        layer = LAYER_CONFLICT if regime == DETECTION_REGIME_CONFLICT else LAYER_HIPAA
        r = Record(
            record_id=f"hipaa_{cat}_{index:04d}",
            text=text,
            gold_spans=self.annotate(text, spans_spec),
            layer=layer,
            jurisdiction="us",
            detection_regime=regime,
            de_id_tier="identifiable",
            format="text",
            authority_citations=[AUTH_HIPAA_SAFE_HARBOR],
            metadata={"hipaa_category": cat, "hipaa_category_desc": HIPAA_CATEGORIES[cat]},
            **kwargs,
        )
        return r

    # -- Category A: Names (contextual_ner_required) --
    def _gen_names(self, rng, i):
        patient = us_name(rng)
        physician = "Dr. " + us_name(rng)
        relative = us_name(rng)
        text = (
            f"Patient {patient} was seen today by {physician}. "
            f"Emergency contact: {relative} (brother). "
            f"The patient reports no new symptoms."
        )
        spans = [
            (patient, "NAME_PATIENT", "A", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
            (physician, "NAME_PROVIDER", "A", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
            (relative, "NAME_HOUSEHOLD", "A", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
        ]
        return self._record("A", i, text, spans)

    # -- Category B: Geography (conflict_case: ZIP is PHI under HIPAA, not enumerated under GDPR) --
    def _gen_geography(self, rng, i):
        street, city, state, zip3, zip_full = us_address(rng)
        if i % 5 == 0 and RESTRICTED_ZIP3:
            restricted_zip = sorted(RESTRICTED_ZIP3)[i % len(RESTRICTED_ZIP3)]
            zip_full = f"{restricted_zip}{rng.randint(10, 99):02d}"
        text = (
            f"Patient resides at {street}, {city}, {state} {zip_full}. "
            f"Home visit scheduled. Neighborhood: mid-density residential."
        )
        spans = [
            (street, "ADDRESS_STREET", "B", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
            (city, "ADDRESS_CITY", "B", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
            (zip_full, "ADDRESS_ZIP", "B", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_CONFLICT),
        ]
        return self._record("B", i, text, spans)

    # -- Category C: Dates and ages over 89 (contextual_ner_required; also conflict_case for GDPR) --
    def _gen_dates_ages(self, rng, i):
        year = rng.randint(1920, 2024)
        month = rng.randint(1, 12)
        day = rng.randint(1, 28)
        dob = f"{month:02d}/{day:02d}/{year}"
        age = 2026 - year
        admit_year = rng.randint(2022, 2025)
        admit = f"{rng.randint(1,12):02d}/{rng.randint(1,28):02d}/{admit_year}"
        if age > 89:
            text = (
                f"Patient DOB {dob}, age {age} years (HIPAA: must aggregate to 90+). "
                f"Admitted on {admit} for evaluation."
            )
            spans = [
                (dob, "DATE_DOB", "C", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
                (f"age {age}", "AGE_OVER_89", "C", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
                (admit, "DATE_ADMIT", "C", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
            ]
        else:
            text = f"Patient DOB {dob}, age {age} years. Admitted on {admit} for evaluation."
            spans = [
                (dob, "DATE_DOB", "C", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
                (admit, "DATE_ADMIT", "C", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
            ]
        return self._record("C", i, text, spans)

    # -- Category D: Phone (contextual_ner_required) --
    def _gen_phone(self, rng, i):
        phone = us_phone(rng)
        alt_phone = us_phone(rng)
        text = (
            f"Contact patient at {phone}. "
            f"Alternate work phone: {alt_phone}. "
            f"Confirm appointment next week."
        )
        spans = [
            (phone, "PHONE_HOME", "D", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
            (alt_phone, "PHONE_WORK", "D", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
        ]
        return self._record("D", i, text, spans)

    # -- Category E: Fax (rule_applicable: phone-format pattern) --
    def _gen_fax(self, rng, i):
        fax = us_fax(rng)
        text = (
            f"Records fax to referring physician: fax {fax}. "
            f"Please confirm receipt of the discharge summary."
        )
        spans = [
            (fax, "FAX", "E", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_RULE),
        ]
        return self._record("E", i, text, spans)

    # -- Category F: Email (rule_applicable: RFC 822 pattern) --
    def _gen_email(self, rng, i):
        first = rng.choice(US_FIRST_NAMES).lower()
        last = rng.choice(US_LAST_NAMES).lower()
        domain = rng.choice(["gmail.com", "yahoo.com", "example.edu", "clinic.org"])
        email = f"{first}.{last}{rng.randint(1, 999)}@{domain}"
        text = f"Patient requested portal messages be sent to {email} for follow-up."
        spans = [
            (email, "EMAIL", "F", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_RULE),
        ]
        return self._record("F", i, text, spans)

    # -- Category G: SSN (rule_applicable: NNN-NN-NNNN pattern) --
    def _gen_ssn(self, rng, i):
        ssn = us_ssn(rng)
        text = f"Insurance verification: patient SSN on file as {ssn}. Confirmed with ID."
        spans = [
            (ssn, "SSN", "G", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_RULE),
        ]
        return self._record("G", i, text, spans)

    # -- Category H: MRN (contextual_ner_required: format varies by institution) --
    def _gen_mrn(self, rng, i):
        mrn = us_mrn(rng)
        text = f"Retrieved encounter notes under MRN {mrn}. Prior visits: 3 in the past year."
        spans = [
            (mrn, "MRN", "H", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
        ]
        return self._record("H", i, text, spans)

    # -- Category I: Health plan beneficiary (rule_applicable: MBI has fixed format) --
    def _gen_health_plan(self, rng, i):
        mbi = health_plan_beneficiary(rng)
        text = f"Medicare Beneficiary Identifier: {mbi}. Claim submitted for outpatient visit."
        spans = [
            (mbi, "HEALTH_PLAN_ID", "I", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_RULE),
        ]
        return self._record("I", i, text, spans)

    # -- Category J: Account numbers (rule_applicable: fixed-length numeric) --
    def _gen_account(self, rng, i):
        acct = "".join(str(rng.randint(0, 9)) for _ in range(10))
        bank_acct = "".join(str(rng.randint(0, 9)) for _ in range(9))
        text = (
            f"Patient account #{acct} billed directly. "
            f"ACH setup on bank account {bank_acct} routing 021000021."
        )
        spans = [
            (acct, "ACCOUNT_NUMBER", "J", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_RULE),
            (bank_acct, "BANK_ACCOUNT", "J", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_RULE),
        ]
        return self._record("J", i, text, spans)

    # -- Category K: Certificate/license (rule_applicable: state DL + NPI patterns) --
    def _gen_license(self, rng, i):
        dl_number = "D" + "".join(str(rng.randint(0, 9)) for _ in range(8))
        npi = us_npi(rng)
        text = (
            f"Patient driver's license {dl_number} used for ID verification. "
            f"Treating physician NPI: {npi}."
        )
        spans = [
            (dl_number, "DRIVERS_LICENSE", "K", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_RULE),
            (npi, "NPI", "K", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_RULE),
        ]
        return self._record("K", i, text, spans)

    # -- Category L: Vehicle identifiers (rule_applicable: ISO 3779 VIN + plate patterns) --
    def _gen_vehicle(self, rng, i):
        vin = us_vin(rng)
        plate = us_license_plate(rng)
        text = (
            f"Patient arrived by ambulance; personal vehicle VIN {vin} "
            f"with plate {plate} left at work site."
        )
        spans = [
            (vin, "VIN", "L", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_RULE),
            (plate, "LICENSE_PLATE", "L", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_RULE),
        ]
        return self._record("L", i, text, spans)

    # -- Category M: Device identifiers (contextual_ner_required: UDI varies; clinical context needed) --
    def _gen_device(self, rng, i):
        udi = us_device_udi(rng)
        serial = us_device_serial(rng)
        text = (
            f"Implanted cardiac device UDI {udi} serial {serial}. "
            f"Manufacturer recall check: not affected."
        )
        spans = [
            (udi, "DEVICE_UDI", "M", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
            (serial, "DEVICE_SERIAL", "M", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
        ]
        return self._record("M", i, text, spans)

    # -- Category N: URL (rule_applicable: RFC 3986 pattern) --
    def _gen_url(self, rng, i):
        u = url(rng)
        text = f"Patient portal enrollment at {u}. Credentials mailed separately."
        spans = [
            (u, "URL", "N", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_RULE),
        ]
        return self._record("N", i, text, spans)

    # -- Category O: IP (rule_applicable: CIDR patterns) --
    def _gen_ip(self, rng, i):
        ip4 = ipv4(rng)
        ip6 = ipv6(rng)
        text = (
            f"Patient portal access from IP {ip4} during last visit. "
            f"IPv6 address logged: {ip6}."
        )
        spans = [
            (ip4, "IP_V4", "O", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_RULE),
            (ip6, "IP_V6", "O", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_RULE),
        ]
        return self._record("O", i, text, spans)

    # -- Category P: Biometric (contextual_ner_required: GDPR Art. 4(14) biometric data) --
    def _gen_biometric(self, rng, i):
        bio1 = biometric_reference(rng)
        text = (
            f"Biometric template on file: {bio1}. "
            f"Used for patient identification at check-in."
        )
        spans = [
            (bio1, "BIOMETRIC", "P", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
        ]
        return self._record("P", i, text, spans)

    # -- Category Q: Photo (contextual_ner_required) --
    def _gen_photo(self, rng, i):
        photo = photo_reference(rng)
        text = (
            f"Full-face photograph on record: attachment {photo}. "
            f"Used for patient chart identification."
        )
        spans = [
            (photo, "PHOTO_FULL_FACE", "Q", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
        ]
        return self._record("Q", i, text, spans)

    # -- Category R: Any other unique identifying code (contextual_ner_required) --
    def _gen_other_unique(self, rng, i):
        clinical_trial = f"NCT{rng.randint(10_000_000, 99_999_999)}"
        unique_code = "X" + "".join(rng.choices(string.ascii_uppercase + string.digits, k=15))
        text = (
            f"Enrolled in clinical trial {clinical_trial}; "
            f"internal research code {unique_code}."
        )
        spans = [
            (clinical_trial, "CLINICAL_TRIAL_ID", "R", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
            (unique_code, "INTERNAL_CODE", "R", "us", AUTH_HIPAA_SAFE_HARBOR, DETECTION_REGIME_NER),
        ]
        return self._record("R", i, text, spans)


class HIPAAQuasiIdentifierGenerator(DeterministicGenerator):
    """Tests the (b)(2)(ii) 'no actual knowledge' safety net.

    Each record has Safe Harbor identifiers REMOVED but contains a combination of
    quasi-identifiers that could re-identify the individual. A proper system
    must flag these even without direct identifiers.
    """

    RARE_DISEASES = [
        "Alpha-1 antitrypsin deficiency",
        "Gaucher disease type 3",
        "Progressive supranuclear palsy",
        "Creutzfeldt-Jakob disease",
        "Hereditary angioedema",
        "Fabry disease",
        "Pompe disease",
        "Wilson disease",
        "Idiopathic pulmonary fibrosis",
        "Primary ciliary dyskinesia",
    ]

    PROFESSIONS = [
        "State Supreme Court Justice",
        "Head of university pathology department",
        "Former city mayor",
        "Retired NFL quarterback",
        "Symphony orchestra conductor",
        "Pulitzer Prize winning journalist",
        "Former astronaut",
        "CEO of regional hospital system",
        "Federal judge",
        "Head of state forensics lab",
    ]

    def generate_batch(self, count: int = 50) -> List[Record]:
        records = []
        for i in range(count):
            rng = self.fresh(f"qi_{i}")
            records.append(self._generate_one(rng, i))
        return records

    def _generate_one(self, rng, i):
        disease = rng.choice(self.RARE_DISEASES)
        profession = rng.choice(self.PROFESSIONS)
        age = rng.randint(50, 89)
        _, city, state, _, _ = us_address(rng)
        # Note: names, DOB, SSN, MRN all removed; only quasi-identifiers remain
        text = (
            f"Case study: {age}-year-old {profession} from {city}, {state} "
            f"presenting with {disease}. No direct identifiers retained. "
            f"However, combination of rare disease + unique profession + city is "
            f"re-identifiable per Sweeney 2002 and violates 164.514(b)(2)(ii)."
        )
        spans = [
            (profession, "QUASI_PROFESSION", None, "us", AUTH_HIPAA_ACTUAL_KNOWLEDGE, DETECTION_REGIME_NER),
            (city, "QUASI_CITY", "B", "us", AUTH_HIPAA_ACTUAL_KNOWLEDGE, DETECTION_REGIME_NER),
            (disease, "QUASI_RARE_DISEASE", None, "us", AUTH_SWEENEY_K_ANON, DETECTION_REGIME_NER),
        ]
        r = Record(
            record_id=f"hipaa_qi_{i:04d}",
            text=text,
            gold_spans=self.annotate(text, spans),
            layer="hipaa_quasi_identifier",
            jurisdiction="us",
            detection_regime=DETECTION_REGIME_NER,
            de_id_tier="safe_harbor",  # claims to be de-identified but has QI
            format="text",
            authority_citations=[AUTH_HIPAA_ACTUAL_KNOWLEDGE, AUTH_SWEENEY_K_ANON],
            metadata={
                "sweeney_vulnerable": True,
                "attack_surface": "quasi_identifier_combination",
            },
        )
        return r
