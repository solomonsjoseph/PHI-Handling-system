"""Scenario library — realistic study archetypes with per-column PHI shape.

Each Scenario declares:
  * ``datasets``     A list of dataset specs (CSV files) with columns typed
                     as either PHI (with ``hipaa_category`` + ``expected_action``)
                     or clinical (``expected_action="keep"``).
  * ``dictionary``   Rows for a per-study codebook that the Lexicon agent reads.
  * ``narrative``    Free-text notes that will carry embedded PHI when
                     the ``notes_free_text_phi`` edge case is enabled.

Scenarios are hand-curated and deliberately small; the Corpus Research
agent (Phase C3, later) will augment this with real-world archetypes
mined from ClinicalTrials.gov / PubMed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Any

from phi_core.detectors import luhn
from phi_core.jurisdictions import get_pack as _get_pack

from . import realism as _realism

# Single source of truth for the 17 HIPAA-restricted ZIP3 prefixes, shared
# by every generator below and by planters.expected_for's oracle (which
# imports the same pack directly) -- do not hardcode a second copy.
_RESTRICTED_ZIP3: tuple[str, ...] = tuple(sorted(_get_pack("us").restricted_zip3_prefixes))


# ---- Cell generators ----------------------------------------------------
#
# Every generator returns a plain string. The planter feeds the returned
# value into the CSV verbatim and records it in the ground-truth sidecar
# so the verifier can check it was removed / transformed correctly.

import random
import string


def gen_name(rng: random.Random) -> str:
    firsts = ["James", "Mary", "Peter", "Sarah", "Michael", "Priya", "Aditya",
             "Fatima", "Lin", "Diego"]
    lasts = ["Smith", "Jones", "Wong", "Patel", "García", "O'Brien",
             "Nguyễn", "Silva", "Kaur", "Volkov"]
    return f"{rng.choice(firsts)} {rng.choice(lasts)}"


def gen_dob(rng: random.Random) -> tuple[str, dict]:
    year = rng.randint(1930, 2015)
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)
    return f"{year:04d}-{month:02d}-{day:02d}", {"year": year}


def gen_dob_over_89(rng: random.Random) -> tuple[str, dict]:
    """Edge case: birth year implies age > 89 today (dates indicative of
    such age per §164.514(b)(2)(i)(C)). Must be masked further."""
    year = rng.randint(1900, 1935)
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)
    return f"{year:04d}-{month:02d}-{day:02d}", {"year": year}


def gen_phone_us(rng: random.Random) -> str:
    return f"415-555-{rng.randint(1000, 9999):04d}"


def gen_email(rng: random.Random) -> str:
    return (
        f"{rng.choice(['jsmith', 'mjones', 'pwong', 'ppatel'])}"
        f".{rng.randint(1, 99)}@example.edu"
    )


def gen_zip5(rng: random.Random) -> tuple[str, dict]:
    value = f"{rng.randint(10000, 99999):05d}"
    return value, {"zip3": value[:3]}


def gen_zip5_restricted(rng: random.Random) -> tuple[str, dict]:
    """One of the 17 HIPAA-restricted ZIP3 prefixes plus 2-digit suffix."""
    value = f"{rng.choice(_RESTRICTED_ZIP3)}{rng.randint(10, 99)}"
    return value, {"zip3": value[:3]}


def gen_ssn(rng: random.Random) -> str:
    return f"{rng.randint(100, 999):03d}-{rng.randint(10, 99):02d}-{rng.randint(1000, 9999):04d}"


def gen_mrn(rng: random.Random) -> str:
    return f"MRN{rng.randint(100000, 999999)}"


def gen_age_under_90(rng: random.Random) -> tuple[str, dict]:
    age = rng.randint(18, 89)
    return str(age), {"age": age}


def gen_age_over_89(rng: random.Random) -> tuple[str, dict]:
    """Edge case: age must be aggregated per §164.514(b)(2)(i)(C)."""
    age = rng.randint(90, 105)
    return str(age), {"age": age}


def gen_heart_rate(rng: random.Random) -> str:
    """Clinical measurement 60-110. Deliberately can be 90-99 — critical
    edge case for the AGE_OVER_89 guard false-positive test."""
    return str(rng.randint(60, 110))


def gen_systolic_bp(rng: random.Random) -> str:
    return str(rng.randint(85, 145))


def gen_glucose_mgdl(rng: random.Random) -> str:
    return str(rng.randint(70, 200))


def gen_arm_code(rng: random.Random) -> str:
    """Study arm code shaped like a license plate — critical torture test
    for the LICENSE_PLATE guard."""
    return f"{rng.choice(['ARM', 'HB', 'STD', 'INT'])} {rng.randint(1, 999):03d}"


def gen_barcode(rng: random.Random) -> str:
    """Long numeric barcode overlapping the IMEI shape (15 digits)."""
    return "".join(str(rng.randint(0, 9)) for _ in range(15))


def gen_aadhaar(rng: random.Random) -> str:
    """India national ID (DPDPA-covered) — 12 digits, 4-4-4 grouped."""
    return f"{rng.randint(1000,9999)} {rng.randint(1000,9999)} {rng.randint(1000,9999)}"


def gen_pan(rng: random.Random) -> str:
    """India PAN — 5 letters + 4 digits + 1 letter."""
    letters = string.ascii_uppercase
    prefix = "".join(rng.choice(letters) for _ in range(5))
    suffix = rng.choice(letters)
    return f"{prefix}{rng.randint(1000,9999)}{suffix}"


# ---- Adversarial HIPAA A-R generators ----------------------------------

def gen_fax_us(rng: random.Random) -> str:
    """(E) Fax number."""
    return f"212-555-{rng.randint(1000, 9999):04d}"


def gen_health_plan_id(rng: random.Random) -> str:
    """(I) Health-plan beneficiary number."""
    return f"HP{rng.randint(10000000, 99999999)}"


def gen_account_number(rng: random.Random) -> str:
    """(J) Account / billing number."""
    return f"ACCT-{rng.randint(100000, 999999)}"


def gen_license_number(rng: random.Random) -> str:
    """(K) Certificate / licence number (US-DEA-shaped)."""
    letters = string.ascii_uppercase
    return f"{rng.choice(letters)}{rng.choice(letters)}{rng.randint(1000000, 9999999)}"


def gen_vin(rng: random.Random) -> str:
    """(L) Vehicle identification number — 17-char VIN."""
    chars = string.ascii_uppercase.replace("I", "").replace("O", "").replace("Q", "") + string.digits
    return "".join(rng.choice(chars) for _ in range(17))


def gen_device_serial(rng: random.Random) -> str:
    """(M) Device serial / identifier."""
    letters = string.ascii_uppercase
    return f"DEV-{''.join(rng.choice(letters) for _ in range(3))}-{rng.randint(100000, 999999)}"


def gen_url(rng: random.Random) -> str:
    """(N) Personal URL."""
    slug = "".join(rng.choice(string.ascii_lowercase) for _ in range(6))
    return f"https://patient-portal.example.org/u/{slug}"


def gen_ipv4(rng: random.Random) -> str:
    """(O) IPv4 address."""
    return f"{rng.randint(1, 223)}.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"


def gen_biometric_hash(rng: random.Random) -> str:
    """(P) Biometric identifier — placeholder hash string."""
    chars = string.hexdigits.lower()[:16]
    return "bio_" + "".join(rng.choice(chars) for _ in range(32))


def gen_image_ref(rng: random.Random) -> str:
    """(Q) Full-face photograph reference (URL / storage key)."""
    return f"s3://phi-photos-bucket/patient_{rng.randint(1000, 9999)}.jpg"


def gen_unique_code(rng: random.Random) -> str:
    """(R) Any other unique code — tracking / linkage tag."""
    letters = string.ascii_uppercase
    return f"UID-{''.join(rng.choice(letters) for _ in range(4))}{rng.randint(1000, 9999)}"


def gen_notes_with_url(rng: random.Random) -> tuple[str, dict]:
    url = gen_url(rng)
    return f"Patient uploaded records via {url} on 2024-05-20.", {"literals": (url,)}


def gen_notes_with_device_serial(rng: random.Random) -> tuple[str, dict]:
    serial = gen_device_serial(rng)
    return f"CPAP device serial {serial} configured at last visit.", {"literals": (serial,)}


def gen_notes_with_license(rng: random.Random) -> tuple[str, dict]:
    lic = gen_license_number(rng)
    return (f"Provider license {lic} verified before dispensing controlled substance.",
            {"literals": (lic,)})


def gen_notes_with_name(rng: random.Random) -> tuple[str, dict]:
    name = gen_name(rng)
    return f"Patient {name} enrolled on 2024-03-15; contact via phone.", {"literals": (name,)}


def gen_notes_with_phone(rng: random.Random) -> tuple[str, dict]:
    ph = gen_phone_us(rng)
    return f"Contact at {ph} between 9am and 5pm PST.", {"literals": (ph,)}


def gen_notes_with_age_over_89(rng: random.Random) -> tuple[str, dict]:
    age = rng.randint(90, 102)
    return (f"Elderly patient aged {age} presenting with chest pain.",
            {"literals": (str(age),)})


def gen_notes_with_ipv4(rng: random.Random) -> tuple[str, dict]:
    ip = f"{rng.randint(1,255)}.{rng.randint(0,255)}.{rng.randint(0,255)}.{rng.randint(1,254)}"
    return f"Telehealth session originated from {ip}", {"literals": (ip,)}


def gen_notes_with_email(rng: random.Random) -> tuple[str, dict]:
    email = gen_email(rng)
    return f"Discharge summary sent to {email} at 14:32.", {"literals": (email,)}


def gen_notes_clinical_only(rng: random.Random) -> str:
    return rng.choice([
        "Vitals stable; continue current regimen.",
        "Post-op day 2, wound healing normally.",
        "Reports mild fatigue; no new symptoms.",
        "HbA1c improved from 8.2 to 7.4 over 3 months.",
    ])


# ---- Dataset + Scenario dataclasses ------------------------------------


@dataclass(frozen=True)
class ColumnSpec:
    """A single column in a corpus dataset.

    ``hipaa_category`` uses the HIPAA A-R letter for structured identifiers
    or the sentinel ``"NONE"`` for legitimate clinical / demographic
    non-PHI. The verifier reads ``expected_action`` to score the pipeline's
    actual decision.

    ``expected_action`` must match one of the pipeline's action vocabulary:
    ``drop``, ``keep``, ``year_only``, ``zip3_truncate``, ``cap_age_90``,
    ``pseudonymize``, ``scrub_text``.

    ``generator`` may return a plain ``str`` (legacy shape) or a
    ``tuple[str, dict]`` where the dict carries the semantic facts the
    export oracle in ``planters.expected_for`` needs (see that module for
    the recognized keys). Existing generators keep the legacy shape.

    ``jitterable`` opts a ``keep``-action column into ``realism.jitter``'s
    whitespace/case-noise dial under the messy/hostile profiles. Default
    OFF: a token-count heuristic (multi-word only) is NOT a safe proxy for
    "free narrative text" -- a two-token controlled term (CDISC ARMCD
    "ARM A") is just as invalid lowercased ("arm a") as reordered ("A
    ARM"), so case noise on it manufactures a term that does not exist in
    the controlled vocabulary, not a realistic messiness variant. Set this
    ``True`` only on a column whose values are genuinely free/unstandardized
    text (site labels, an OMOP ``_source_value`` field, narrative diagnosis
    text) where a real system's inconsistent capitalization is plausible.
    """
    name: str
    hipaa_category: str
    expected_action: str
    generator: Callable[[random.Random], Any]
    edge_case_tag: str = ""  # populated when a variant of this column is planted
    sensitivity_class: str = ""  # empty, or e.g. "42cfr2" for 42 CFR Part 2 material
    jitterable: bool = False


@dataclass(frozen=True)
class DatasetSpec:
    filename: str
    columns: tuple[ColumnSpec, ...]
    link_column: str = ""       # column name shared/reused across datasets for linkage
    rows_per_subject: int = 1   # emit N rows per roster subject (visits/events tables)


@dataclass(frozen=True)
class DictionaryRow:
    column_name: str
    description: str
    type: str = "string"


@dataclass(frozen=True)
class Scenario:
    id: str
    label: str
    jurisdictions: frozenset[str]
    datasets: tuple[DatasetSpec, ...]
    dictionary: tuple[DictionaryRow, ...]
    profile: str = "clean"   # "clean" | "messy" | "hostile", see realism.py
    tier: str = "L0"         # "L0" | "L1" | "L2" | "L3", see tiers.py

# ---- Scenario library --------------------------------------------------

_ONCOLOGY = Scenario(
    id="oncology_v1",
    label="Oncology trial baseline enrollment",
    jurisdictions=frozenset({"us"}),
    tier="L0",
    datasets=(
        DatasetSpec(
            filename="enrollment.csv",
            columns=(
                ColumnSpec("patient_id", "H", "pseudonymize", gen_mrn),
                ColumnSpec("name", "A", "drop", gen_name),
                ColumnSpec("dob", "C", "year_only", gen_dob),
                ColumnSpec("phone", "D", "drop", gen_phone_us),
                ColumnSpec("email", "F", "drop", gen_email),
                ColumnSpec("zip", "B", "zip3_truncate", gen_zip5),
                ColumnSpec("age", "C", "cap_age_90", gen_age_under_90),
                # Clinical variables — MUST NOT be flagged
                ColumnSpec("heart_rate_bpm", "NONE", "keep", gen_heart_rate),
                ColumnSpec("systolic_bp", "NONE", "keep", gen_systolic_bp),
                ColumnSpec("arm_code", "NONE", "keep", gen_arm_code),
                ColumnSpec("notes", "NONE", "scrub_text", gen_notes_clinical_only),
            ),
        ),
    ),
    dictionary=(
        DictionaryRow("patient_id", "Study-scoped patient identifier"),
        DictionaryRow("name", "Full patient name (PHI cat A)"),
        DictionaryRow("dob", "Date of birth (PHI cat C)", "date"),
        DictionaryRow("phone", "Phone number (PHI cat D)"),
        DictionaryRow("email", "Email address (PHI cat F)"),
        DictionaryRow("zip", "ZIP code (PHI cat B)"),
        DictionaryRow("age", "Age in years"),
        DictionaryRow("heart_rate_bpm", "Heart rate in beats per minute", "int"),
        DictionaryRow("systolic_bp", "Systolic blood pressure in mmHg", "int"),
        DictionaryRow("arm_code", "Study arm assignment", "string"),
        DictionaryRow("notes", "Free-text clinical notes"),
    ),
)


_DIABETES = Scenario(
    id="diabetes_v1",
    label="Type 2 diabetes cohort — baseline visit",
    jurisdictions=frozenset({"us"}),
    tier="L1",
    datasets=(
        DatasetSpec(
            filename="baseline.csv",
            columns=(
                ColumnSpec("subject_id", "H", "pseudonymize", gen_mrn),
                ColumnSpec("full_name", "A", "drop", gen_name),
                ColumnSpec("date_of_birth", "C", "year_only", gen_dob),
                ColumnSpec("home_phone", "D", "drop", gen_phone_us),
                ColumnSpec("home_zip", "B", "zip3_truncate", gen_zip5),
                ColumnSpec("age_years", "C", "cap_age_90", gen_age_under_90),
                # Clinical
                ColumnSpec("hba1c_percent", "NONE", "keep", lambda r: f"{r.uniform(5.5, 12.5):.1f}"),
                ColumnSpec("glucose_mgdl", "NONE", "keep", gen_glucose_mgdl),
                ColumnSpec("bmi", "NONE", "keep", lambda r: f"{r.uniform(18, 42):.1f}"),
                ColumnSpec("study_visit_notes", "NONE", "scrub_text", gen_notes_clinical_only),
            ),
        ),
    ),
    dictionary=(
        DictionaryRow("subject_id", "Study subject identifier"),
        DictionaryRow("full_name", "Participant full legal name"),
        DictionaryRow("date_of_birth", "Date of birth", "date"),
        DictionaryRow("home_phone", "Home phone contact"),
        DictionaryRow("home_zip", "Home ZIP code"),
        DictionaryRow("age_years", "Age in years", "int"),
        DictionaryRow("hba1c_percent", "HbA1c percentage", "float"),
        DictionaryRow("glucose_mgdl", "Fasting glucose mg/dL", "int"),
        DictionaryRow("bmi", "Body mass index", "float"),
        DictionaryRow("study_visit_notes", "Free-text visit notes"),
    ),
)


def gen_child_age(rng: random.Random) -> tuple[str, dict]:
    age = rng.randint(5, 17)
    return str(age), {"age": age}


_PEDIATRIC = Scenario(
    id="pediatric_behavioral_v1",
    label="Pediatric behavioral study — screening",
    jurisdictions=frozenset({"us"}),
    tier="L1",
    datasets=(
        DatasetSpec(
            filename="screening.csv",
            columns=(
                ColumnSpec("child_id", "H", "pseudonymize", gen_mrn),
                ColumnSpec("child_first_name", "A", "drop", lambda r: r.choice(["Sam","Alex","Riley","Kai"])),
                ColumnSpec("guardian_name", "A", "drop", gen_name),
                ColumnSpec("guardian_phone", "D", "drop", gen_phone_us),
                ColumnSpec("school_name", "A", "drop", lambda r: r.choice(["Roosevelt Elementary","Kennedy Middle","Lincoln Prep"])),
                ColumnSpec("household_zip", "B", "zip3_truncate", gen_zip5),
                ColumnSpec("child_age_years", "C", "cap_age_90", gen_child_age),
                ColumnSpec("cbcl_total_score", "NONE", "keep", lambda r: str(r.randint(20, 90))),
                ColumnSpec("interview_notes", "NONE", "scrub_text", gen_notes_clinical_only),
            ),
        ),
    ),
    dictionary=(
        DictionaryRow("child_id", "Study child identifier"),
        DictionaryRow("child_first_name", "Child's first name"),
        DictionaryRow("guardian_name", "Parent / guardian full name"),
        DictionaryRow("guardian_phone", "Guardian's phone"),
        DictionaryRow("school_name", "School name"),
        DictionaryRow("household_zip", "Household ZIP"),
        DictionaryRow("child_age_years", "Child's age in years"),
        DictionaryRow("cbcl_total_score", "CBCL total problem score"),
        DictionaryRow("interview_notes", "Screening interview notes"),
    ),
)


SCENARIOS: dict[str, Scenario] = {
    _ONCOLOGY.id: _ONCOLOGY,
    _DIABETES.id: _DIABETES,
    _PEDIATRIC.id: _PEDIATRIC,
}


# ---- Maximum-adversarial scenario (all HIPAA A-R + edge cases) ---------
#
# One dataset that violates every identifier category HIPAA §164.514(b)(2)(i)
# calls out. Used as the corpus torture-test proof for IRB review.

_HIPAA_MAX = Scenario(
    id="hipaa_max_adversarial_v1",
    label="Adversarial cohort - every HIPAA A-R identifier + edge cases",
    jurisdictions=frozenset({"us"}),
    tier="L0",
    datasets=(
        DatasetSpec(
            filename="max_adversarial.csv",
            columns=(
                # (A) Names
                ColumnSpec("patient_name", "A", "drop", gen_name),
                # (B) Geography
                ColumnSpec("street_address", "B", "drop",
                           lambda r: f"{r.randint(100,9999)} Main St, Apt {r.randint(1,99)}"),
                ColumnSpec("zip", "B", "zip3_truncate", gen_zip5),
                # (C) Dates + age
                ColumnSpec("dob", "C", "year_only", gen_dob),
                ColumnSpec("admission_date", "C", "year_only", gen_dob),
                ColumnSpec("age", "C", "cap_age_90", gen_age_under_90),
                # (D) Phone
                ColumnSpec("phone", "D", "drop", gen_phone_us),
                # (E) Fax
                ColumnSpec("fax", "E", "drop", gen_fax_us),
                # (F) Email
                ColumnSpec("email", "F", "drop", gen_email),
                # (G) SSN
                ColumnSpec("ssn", "G", "drop", gen_ssn),
                # (H) Medical record / study-scoped id
                ColumnSpec("patient_id", "H", "pseudonymize", gen_mrn),
                # (I) Health-plan beneficiary
                ColumnSpec("insurance_id", "I", "drop", gen_health_plan_id),
                # (J) Account number
                ColumnSpec("account_number", "J", "drop", gen_account_number),
                # (K) Certificate / licence
                ColumnSpec("license_number", "K", "drop", gen_license_number),
                # (L) Vehicle identifier
                ColumnSpec("vehicle_id", "L", "drop", gen_vin),
                # (M) Device serial
                ColumnSpec("device_serial", "M", "drop", gen_device_serial),
                # (N) URL
                ColumnSpec("personal_url", "N", "drop", gen_url),
                # (O) IP address
                ColumnSpec("client_ip", "O", "drop", gen_ipv4),
                # (P) Biometric identifier
                ColumnSpec("biometric_hash", "P", "drop", gen_biometric_hash),
                # (Q) Photographs / images
                ColumnSpec("face_photo", "Q", "drop", gen_image_ref),
                # (R) Any other unique code
                ColumnSpec("tracking_code", "R", "pseudonymize", gen_unique_code),
                # Clinical variables (MUST NOT be flagged)
                ColumnSpec("heart_rate_bpm", "NONE", "keep", gen_heart_rate),
                ColumnSpec("systolic_bp", "NONE", "keep", gen_systolic_bp),
                ColumnSpec("glucose_mgdl", "NONE", "keep", gen_glucose_mgdl),
                ColumnSpec("bmi", "NONE", "keep",
                           lambda r: f"{r.uniform(18, 42):.1f}"),
                ColumnSpec("arm_code", "NONE", "keep", gen_arm_code),
                ColumnSpec("barcode", "NONE", "keep", gen_barcode),
                ColumnSpec("notes", "NONE", "scrub_text", gen_notes_clinical_only),
            ),
        ),
    ),
    dictionary=(
        DictionaryRow("patient_name", "Full patient name"),
        DictionaryRow("street_address", "Street address"),
        DictionaryRow("zip", "ZIP code"),
        DictionaryRow("dob", "Date of birth", "date"),
        DictionaryRow("admission_date", "Admission date", "date"),
        DictionaryRow("age", "Age in years", "int"),
        DictionaryRow("phone", "Contact phone"),
        DictionaryRow("fax", "Contact fax"),
        DictionaryRow("email", "Contact email"),
        DictionaryRow("ssn", "Social security number"),
        DictionaryRow("patient_id", "Study-scoped patient identifier"),
        DictionaryRow("insurance_id", "Health plan beneficiary id"),
        DictionaryRow("account_number", "Billing account number"),
        DictionaryRow("license_number", "Certificate / licence number"),
        DictionaryRow("vehicle_id", "Vehicle identification number"),
        DictionaryRow("device_serial", "Study-issued device serial"),
        DictionaryRow("personal_url", "Personal URL"),
        DictionaryRow("client_ip", "Client IP address"),
        DictionaryRow("biometric_hash", "Biometric hash"),
        DictionaryRow("face_photo", "Face photograph reference"),
        DictionaryRow("tracking_code", "Unique tracking / linkage code"),
        DictionaryRow("heart_rate_bpm", "Heart rate", "int"),
        DictionaryRow("systolic_bp", "Systolic BP", "int"),
        DictionaryRow("glucose_mgdl", "Fasting glucose", "int"),
        DictionaryRow("bmi", "Body mass index", "float"),
        DictionaryRow("arm_code", "Study arm assignment"),
        DictionaryRow("barcode", "Specimen barcode"),
        DictionaryRow("notes", "Free-text clinical notes"),
    ),
)


SCENARIOS[_HIPAA_MAX.id] = _HIPAA_MAX



# ---- L1-L3 ladder generators --------------------------------------------
#
# Named module-level functions (never lambdas) so ``tiers.corpus_version()``
# can hash ``generator.__name__`` and get a stable, distinguishing value.
# Real header names and value shapes are sourced from the US study
# landscape research in the hardening plan; see that document for citations.


def gen_street_address(rng: random.Random) -> str:
    return f"{rng.randint(100, 9999)} {rng.choice(['Main St', 'Oak Ave', 'Elm St', 'Park Blvd'])}"


def gen_city_us(rng: random.Random) -> str:
    return rng.choice(["Springfield", "Riverside", "Fairview", "Georgetown"])


def gen_state_us(rng: random.Random) -> str:
    return rng.choice(["CA", "TX", "NY", "IL", "OH"])


def gen_county_us(rng: random.Random) -> str:
    return rng.choice(["Cook County", "Orange County", "Harris County", "Wayne County"])


def gen_npi_luhn(rng: random.Random) -> str:
    """(K) A bare 10-digit NPI whose Luhn check over ``"80840" + npi``
    actually passes -- exercises the pipeline's NPI_BARE conditional guard
    with a genuinely valid number rather than random digits it would reject.
    """
    prefix9 = "".join(str(rng.randint(0, 9)) for _ in range(9))
    for d in range(10):
        candidate = prefix9 + str(d)
        if luhn("80840" + candidate):
            return candidate
    raise RuntimeError("no valid NPI check digit found")  # unreachable: Luhn always has one


# ---- CDISC SDTM: l1_sdtm_oncology_v1 ------------------------------------

_SDTM_STUDYID = "ONC2024"
_SDTM_SITES = ("101", "102", "103")
_SDTM_INV_FIRST = ("Alicia", "Robert", "Maria", "David", "Susan", "James", "Linda", "Thomas")
_SDTM_INV_LAST = ("Reyes", "Chen", "Patel", "Nguyen", "Okafor", "Martinez", "Kowalski", "Singh")


def gen_studyid_sdtm(rng: random.Random) -> str:
    return _SDTM_STUDYID


def gen_domain_dm(rng: random.Random) -> str:
    return "DM"


def gen_domain_ae(rng: random.Random) -> str:
    return "AE"


def gen_domain_vs(rng: random.Random) -> str:
    return "VS"


def gen_domain_lb(rng: random.Random) -> str:
    return "LB"


def gen_usubjid(rng: random.Random) -> str:
    return f"{_SDTM_STUDYID}-{rng.choice(_SDTM_SITES)}-{rng.randint(1, 9999):04d}"


def gen_subjid(rng: random.Random) -> str:
    return f"{rng.randint(1, 9999):04d}"


def gen_siteid_sdtm(rng: random.Random) -> str:
    return rng.choice(_SDTM_SITES)


def gen_invid(rng: random.Random) -> str:
    return f"INV{rng.randint(100, 999)}"


def gen_invnam(rng: random.Random) -> str:
    return f"{rng.choice(_SDTM_INV_LAST)}, {rng.choice(_SDTM_INV_FIRST)} MD"


def gen_brthdtc(rng: random.Random) -> tuple[str, dict]:
    """SDTM --DTC: ISO 8601, truncated from the right for partial dates. A
    wholly unknown date is empty -- SDTM has no UNK token."""
    year = rng.randint(1935, 2005)
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)
    roll = rng.random()
    if roll < 0.05:
        return "", {"year": year, "missing": True}
    if roll < 0.15:
        return f"{year:04d}", {"year": year}
    if roll < 0.35:
        return f"{year:04d}-{month:02d}", {"year": year}
    return f"{year:04d}-{month:02d}-{day:02d}", {"year": year}


def gen_dtc_iso(rng: random.Random) -> tuple[str, dict]:
    """Any other SDTM --DTC field: full ISO date, occasionally empty."""
    year = rng.randint(2020, 2024)
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)
    if rng.random() < 0.05:
        return "", {"year": year, "missing": True}
    return f"{year:04d}-{month:02d}-{day:02d}", {"year": year}


def gen_age_sdtm(rng: random.Random) -> tuple[str, dict]:
    age = rng.randint(18, 95)
    return str(age), {"age": age}


def gen_ageu(rng: random.Random) -> str:
    return "YEARS"


def gen_sex(rng: random.Random) -> str:
    return rng.choice(["M", "F"])


def gen_race_sdtm(rng: random.Random) -> str:
    return rng.choice(["WHITE", "BLACK OR AFRICAN AMERICAN", "ASIAN", "OTHER"])


def gen_ethnic(rng: random.Random) -> str:
    return rng.choice(["HISPANIC OR LATINO", "NOT HISPANIC OR LATINO"])


def gen_armcd(rng: random.Random) -> str:
    return rng.choice(["ARM A", "ARM B"])


def gen_arm(rng: random.Random) -> str:
    return rng.choice(["Treatment", "Placebo"])


def gen_actarmcd(rng: random.Random) -> str:
    return rng.choice(["ARM A", "ARM B"])


def gen_actarm(rng: random.Random) -> str:
    return rng.choice(["Treatment", "Placebo"])


def gen_country(rng: random.Random) -> str:
    return "USA"


def gen_dthfl(rng: random.Random) -> str:
    return rng.choice(["", "Y"])


def gen_dy(rng: random.Random) -> str:
    return str(rng.randint(1, 400))


def gen_aeseq(rng: random.Random) -> str:
    return str(rng.randint(1, 3))


def gen_aeterm(rng: random.Random) -> str:
    return rng.choice(["Nausea", "Headache", "Fatigue", "Neutropenia", "Rash", "Diarrhea"])


def gen_aedecod(rng: random.Random) -> str:
    return rng.choice(["Nausea", "Headache", "Fatigue", "Neutropenia", "Rash", "Diarrhoea"])


def gen_aebodsys(rng: random.Random) -> str:
    return rng.choice(["Gastrointestinal disorders", "Nervous system disorders",
                        "Blood and lymphatic system disorders", "Skin disorders"])


def gen_aeser(rng: random.Random) -> str:
    return rng.choice(["Y", "N"])


def gen_aesev(rng: random.Random) -> str:
    return rng.choice(["MILD", "MODERATE", "SEVERE"])


def gen_aerel(rng: random.Random) -> str:
    return rng.choice(["RELATED", "NOT RELATED"])


def gen_aeout(rng: random.Random) -> str:
    return rng.choice(["RECOVERED/RESOLVED", "RECOVERING/RESOLVING", "NOT RECOVERED/NOT RESOLVED"])


def gen_visitnum(rng: random.Random) -> str:
    return str(rng.randint(1, 12))


def gen_visit(rng: random.Random) -> str:
    return f"WEEK {rng.randint(1, 24)}"


def gen_vsseq(rng: random.Random) -> str:
    return str(rng.randint(1, 3))


def gen_vstestcd(rng: random.Random) -> str:
    return rng.choice(["SYSBP", "DIABP", "PULSE", "TEMP", "WEIGHT"])


def gen_vstest(rng: random.Random) -> str:
    return rng.choice(["Systolic Blood Pressure", "Diastolic Blood Pressure",
                        "Pulse Rate", "Temperature", "Weight"])


def gen_vsorres(rng: random.Random) -> str:
    return str(rng.randint(60, 160))


def gen_vsorresu(rng: random.Random) -> str:
    return rng.choice(["mmHg", "beats/min", "C", "kg"])


def gen_vsstresn(rng: random.Random) -> str:
    return str(rng.randint(60, 160))


def gen_vsstresu(rng: random.Random) -> str:
    return rng.choice(["mmHg", "beats/min", "C", "kg"])


def gen_lbseq(rng: random.Random) -> str:
    return str(rng.randint(1, 2))


def gen_lbtestcd(rng: random.Random) -> str:
    return rng.choice(["HGB", "WBC", "PLAT", "CREAT"])


def gen_lbtest(rng: random.Random) -> str:
    return rng.choice(["Hemoglobin", "Leukocytes", "Platelets", "Creatinine"])


def gen_lbcat(rng: random.Random) -> str:
    return "HEMATOLOGY"


def gen_lborres(rng: random.Random) -> str:
    return f"{rng.uniform(2, 18):.1f}"


def gen_lborresu(rng: random.Random) -> str:
    return rng.choice(["g/dL", "10^9/L", "mg/dL"])


def gen_lbstresn(rng: random.Random) -> str:
    return f"{rng.uniform(2, 18):.1f}"


def gen_lbstnrlo(rng: random.Random) -> str:
    return f"{rng.uniform(2, 5):.1f}"


def gen_lbstnrhi(rng: random.Random) -> str:
    return f"{rng.uniform(10, 18):.1f}"


def gen_lbnrind(rng: random.Random) -> str:
    return rng.choice(["NORMAL", "HIGH", "LOW"])


def gen_lbspec(rng: random.Random) -> str:
    return rng.choice(["BLOOD", "SERUM", "URINE"])


_L1_SDTM_DM = DatasetSpec(
    filename="dm.csv",
    link_column="USUBJID",
    columns=(
        ColumnSpec("STUDYID", "NONE", "keep", gen_studyid_sdtm),
        ColumnSpec("DOMAIN", "NONE", "keep", gen_domain_dm),
        ColumnSpec("USUBJID", "R", "pseudonymize", gen_usubjid),
        ColumnSpec("SUBJID", "R", "pseudonymize", gen_subjid),
        ColumnSpec("RFSTDTC", "C", "year_only", gen_dtc_iso),
        ColumnSpec("RFENDTC", "C", "year_only", gen_dtc_iso),
        ColumnSpec("RFXSTDTC", "C", "year_only", gen_dtc_iso),
        ColumnSpec("RFXENDTC", "C", "year_only", gen_dtc_iso),
        ColumnSpec("RFICDTC", "C", "year_only", gen_dtc_iso),
        ColumnSpec("RFPENDTC", "C", "year_only", gen_dtc_iso),
        ColumnSpec("DTHDTC", "C", "year_only", gen_dtc_iso),
        ColumnSpec("DTHFL", "NONE", "keep", gen_dthfl),
        ColumnSpec("SITEID", "B", "drop", gen_siteid_sdtm),
        ColumnSpec("INVID", "K", "drop", gen_invid),
        ColumnSpec("INVNAM", "A", "drop", gen_invnam),
        ColumnSpec("BRTHDTC", "C", "year_only", gen_brthdtc),
        ColumnSpec("AGE", "C", "cap_age_90", gen_age_sdtm),
        ColumnSpec("AGEU", "NONE", "keep", gen_ageu),
        ColumnSpec("SEX", "NONE", "keep", gen_sex),
        ColumnSpec("RACE", "NONE", "keep", gen_race_sdtm),
        ColumnSpec("ETHNIC", "NONE", "keep", gen_ethnic),
        ColumnSpec("ARMCD", "NONE", "keep", gen_armcd),
        ColumnSpec("ARM", "NONE", "keep", gen_arm),
        ColumnSpec("ACTARMCD", "NONE", "keep", gen_actarmcd),
        ColumnSpec("ACTARM", "NONE", "keep", gen_actarm),
        ColumnSpec("COUNTRY", "NONE", "keep", gen_country),
        ColumnSpec("DMDTC", "C", "year_only", gen_dtc_iso),
        ColumnSpec("DMDY", "NONE", "keep", gen_dy),
    ),
)

_L1_SDTM_AE = DatasetSpec(
    filename="ae.csv",
    link_column="USUBJID",
    rows_per_subject=3,
    columns=(
        ColumnSpec("STUDYID", "NONE", "keep", gen_studyid_sdtm),
        ColumnSpec("DOMAIN", "NONE", "keep", gen_domain_ae),
        ColumnSpec("USUBJID", "R", "pseudonymize", gen_usubjid),
        ColumnSpec("AESEQ", "NONE", "keep", gen_aeseq),
        ColumnSpec("AETERM", "NONE", "keep", gen_aeterm),
        ColumnSpec("AEDECOD", "NONE", "keep", gen_aedecod),
        ColumnSpec("AEBODSYS", "NONE", "keep", gen_aebodsys),
        ColumnSpec("AESTDTC", "C", "year_only", gen_dtc_iso),
        ColumnSpec("AEENDTC", "C", "year_only", gen_dtc_iso),
        ColumnSpec("AESER", "NONE", "keep", gen_aeser),
        ColumnSpec("AESEV", "NONE", "keep", gen_aesev),
        ColumnSpec("AEREL", "NONE", "keep", gen_aerel),
        ColumnSpec("AEOUT", "NONE", "keep", gen_aeout),
        ColumnSpec("VISITNUM", "NONE", "keep", gen_visitnum),
        ColumnSpec("VISIT", "NONE", "keep", gen_visit),
        ColumnSpec("AEDY", "NONE", "keep", gen_dy),
    ),
)

_L1_SDTM_VS = DatasetSpec(
    filename="vs.csv",
    link_column="USUBJID",
    rows_per_subject=3,
    columns=(
        ColumnSpec("STUDYID", "NONE", "keep", gen_studyid_sdtm),
        ColumnSpec("DOMAIN", "NONE", "keep", gen_domain_vs),
        ColumnSpec("USUBJID", "R", "pseudonymize", gen_usubjid),
        ColumnSpec("VSSEQ", "NONE", "keep", gen_vsseq),
        ColumnSpec("VSTESTCD", "NONE", "keep", gen_vstestcd),
        ColumnSpec("VSTEST", "NONE", "keep", gen_vstest),
        ColumnSpec("VSORRES", "NONE", "keep", gen_vsorres),
        ColumnSpec("VSORRESU", "NONE", "keep", gen_vsorresu),
        ColumnSpec("VSSTRESN", "NONE", "keep", gen_vsstresn),
        ColumnSpec("VSSTRESU", "NONE", "keep", gen_vsstresu),
        ColumnSpec("VISITNUM", "NONE", "keep", gen_visitnum),
        ColumnSpec("VISIT", "NONE", "keep", gen_visit),
        ColumnSpec("VSDTC", "C", "year_only", gen_dtc_iso),
        ColumnSpec("VSDY", "NONE", "keep", gen_dy),
    ),
)

_L1_SDTM_LB = DatasetSpec(
    filename="lb.csv",
    link_column="USUBJID",
    rows_per_subject=2,
    columns=(
        ColumnSpec("STUDYID", "NONE", "keep", gen_studyid_sdtm),
        ColumnSpec("DOMAIN", "NONE", "keep", gen_domain_lb),
        ColumnSpec("USUBJID", "R", "pseudonymize", gen_usubjid),
        ColumnSpec("LBSEQ", "NONE", "keep", gen_lbseq),
        ColumnSpec("LBTESTCD", "NONE", "keep", gen_lbtestcd),
        ColumnSpec("LBTEST", "NONE", "keep", gen_lbtest),
        ColumnSpec("LBCAT", "NONE", "keep", gen_lbcat),
        ColumnSpec("LBORRES", "NONE", "keep", gen_lborres),
        ColumnSpec("LBORRESU", "NONE", "keep", gen_lborresu),
        ColumnSpec("LBSTRESN", "NONE", "keep", gen_lbstresn),
        ColumnSpec("LBSTNRLO", "NONE", "keep", gen_lbstnrlo),
        ColumnSpec("LBSTNRHI", "NONE", "keep", gen_lbstnrhi),
        ColumnSpec("LBNRIND", "NONE", "keep", gen_lbnrind),
        ColumnSpec("LBSPEC", "NONE", "keep", gen_lbspec),
        ColumnSpec("VISITNUM", "NONE", "keep", gen_visitnum),
        ColumnSpec("LBDTC", "C", "year_only", gen_dtc_iso),
        ColumnSpec("LBDY", "NONE", "keep", gen_dy),
    ),
)

_L1_SDTM = Scenario(
    id="l1_sdtm_oncology_v1",
    label="CDISC SDTM industry oncology trial export",
    jurisdictions=frozenset({"us"}),
    tier="L1",
    profile="messy",
    datasets=(_L1_SDTM_DM, _L1_SDTM_AE, _L1_SDTM_VS, _L1_SDTM_LB),
    dictionary=(
        DictionaryRow("STUDYID", "Protocol/study identifier (not a patient identifier)"),
        DictionaryRow("DOMAIN", "SDTM domain code"),
        DictionaryRow("USUBJID", "Unique subject identifier"),
        DictionaryRow("SUBJID", "Subject identifier within site"),
        DictionaryRow("SITEID", "Investigative site identifier"),
        DictionaryRow("INVID", "Investigator identifier"),
        DictionaryRow("INVNAM", "Investigator name"),
        DictionaryRow("BRTHDTC", "Date/time of birth", "date"),
        DictionaryRow("AGE", "Age", "int"),
        DictionaryRow("RFSTDTC", "Subject reference start date/time", "date"),
        DictionaryRow("RFICDTC", "Informed consent date/time", "date"),
        DictionaryRow("DTHDTC", "Date/time of death", "date"),
        DictionaryRow("DMDTC", "Date/time of collection", "date"),
        DictionaryRow("AETERM", "Reported adverse event term"),
        DictionaryRow("AESTDTC", "Start date/time of adverse event", "date"),
        DictionaryRow("AEENDTC", "End date/time of adverse event", "date"),
        DictionaryRow("VSDTC", "Date/time of vital signs measurement", "date"),
        DictionaryRow("LBDTC", "Date/time of lab collection", "date"),
    ),
)

SCENARIOS[_L1_SDTM.id] = _L1_SDTM


# ---- REDCap: l1_redcap_registry_v1 / l2_redcap_hostile_v1 ---------------

REDCAP_DICTIONARY_HEADERS: tuple[str, ...] = (
    "Variable / Field Name", "Form Name", "Section Header", "Field Type", "Field Label",
    "Choices, Calculations, OR Slider Labels", "Field Note",
    "Text Validation Type OR Show Slider Number", "Text Validation Min", "Text Validation Max",
    "Identifier?", "Branching Logic (Show field only if...)", "Required Field?",
    "Custom Alignment", "Question Number (surveys only)", "Matrix Group Name",
    "Matrix Ranking?", "Field Annotation",
)


def _redcap_row(name: str, form: str, field_type: str, label: str, *, choices: str = "",
                 val_type: str = "", identifier: str = "") -> tuple[str, ...]:
    return (name, form, "", field_type, label, choices, "", val_type, "", "",
            identifier, "", "", "", "", "", "", "")


REDCAP_DICTIONARIES: dict[str, tuple[tuple[str, ...], ...]] = {
    "l1_redcap_registry_v1": (
        _redcap_row("record_id", "demographics", "text", "Record ID", identifier="y"),
        _redcap_row("redcap_event_name", "demographics", "text", "Event Name"),
        _redcap_row("first_name", "demographics", "text", "First name", identifier="y"),
        _redcap_row("last_name", "demographics", "text", "Last name", identifier="y"),
        _redcap_row("dob", "demographics", "text", "Date of birth", val_type="date_ymd", identifier="y"),
        _redcap_row("mrn", "demographics", "text", "Medical record number", identifier="y"),
        _redcap_row("phone", "demographics", "text", "Phone", val_type="phone", identifier="y"),
        _redcap_row("email_address", "demographics", "text", "Email", val_type="email", identifier="y"),
        _redcap_row("street_address", "demographics", "text", "Street address", identifier="y"),
        _redcap_row("zip_code", "demographics", "text", "ZIP code", val_type="zipcode", identifier="y"),
        _redcap_row("consent_ip", "demographics", "text", "Consent submission IP address"),
        _redcap_row("portal_url", "demographics", "text", "Participant portal URL"),
        _redcap_row("race", "demographics", "checkbox", "Race",
                    choices="1, White | 2, Black or African American | 3, Asian"),
        _redcap_row("hba1c_percent", "labs", "text", "HbA1c percent", val_type="number"),
        _redcap_row("bmi", "labs", "text", "BMI", val_type="number"),
        _redcap_row("comments_text", "labs", "notes", "Comments"),
        _redcap_row("demographics_complete", "demographics", "text", "Complete?",
                    choices="0, Incomplete | 1, Unverified | 2, Complete"),
        _redcap_row("labs_complete", "labs", "text", "Complete?",
                    choices="0, Incomplete | 1, Unverified | 2, Complete"),
    ),
    "l2_redcap_hostile_v1": (
        _redcap_row("record_id", "demographics", "text", "Record ID"),
        _redcap_row("redcap_event_name", "demographics", "text", "Event Name"),
        _redcap_row("first_name", "demographics", "text", "First name"),
        _redcap_row("last_name", "demographics", "text", "Last name"),
        _redcap_row("dob", "demographics", "text", "Date of birth", val_type="date_ymd"),
        _redcap_row("mrn", "demographics", "text", "Medical record number"),
        _redcap_row("phone", "demographics", "text", "Phone", val_type="phone"),
        _redcap_row("email_address", "demographics", "text", "Email", val_type="email"),
        _redcap_row("street_address", "demographics", "text", "Street address"),
        _redcap_row("zip_code", "demographics", "text", "ZIP code", val_type="zipcode"),
        _redcap_row("consent_ip", "demographics", "text", "Consent submission IP address"),
        _redcap_row("portal_url", "demographics", "text", "Participant portal URL"),
        _redcap_row("emergency_contact_number", "demographics", "text", "Emergency contact number",
                    val_type="integer"),
        _redcap_row("insurance_group", "demographics", "text", "Insurance group number"),
        _redcap_row("race", "demographics", "checkbox", "Race",
                    choices="1, White | 2, Black or African American | 3, Asian"),
        _redcap_row("hba1c_percent", "labs", "text", "HbA1c percent", val_type="number"),
        _redcap_row("bmi", "labs", "text", "BMI", val_type="number"),
        _redcap_row("comments_text", "labs", "notes", "Comments"),
    ),
}


def gen_record_id(rng: random.Random) -> str:
    return str(rng.randint(1000, 9999))


def gen_redcap_event_name(rng: random.Random) -> str:
    return rng.choice(["baseline_arm_1", "month_3_arm_1", "month_6_arm_1"])


def gen_redcap_repeat_instrument(rng: random.Random) -> str:
    return ""


def gen_redcap_repeat_instance(rng: random.Random) -> str:
    return ""


def gen_race_checkbox(rng: random.Random) -> str:
    return rng.choice(["0", "1"])


def gen_complete_status(rng: random.Random) -> str:
    return rng.choice(["0", "1", "2"])


def gen_consent_ip(rng: random.Random) -> str:
    return f"{rng.randint(1, 223)}.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"


def gen_portal_url_redcap(rng: random.Random) -> str:
    slug = "".join(rng.choice(string.ascii_lowercase) for _ in range(8))
    return f"https://redcap.example.edu/surveys/?s={slug}"


def gen_first_name(rng: random.Random) -> str:
    return gen_name(rng).split(" ")[0]


def gen_last_name(rng: random.Random) -> str:
    return gen_name(rng).split(" ")[1]


def gen_hba1c(rng: random.Random) -> str:
    return f"{rng.uniform(5.5, 12.5):.1f}"


def gen_bmi_redcap(rng: random.Random) -> str:
    return f"{rng.uniform(18, 42):.1f}"


def gen_emergency_contact_number(rng: random.Random) -> str:
    return f"{rng.randint(200, 999)}{rng.randint(200, 999)}{rng.randint(1000, 9999)}"


def gen_aux_ref_mrn(rng: random.Random) -> str:
    return f"MRN{rng.randint(1000000, 9999999)}"


_L1_REDCAP_RECORDS = DatasetSpec(
    filename="records.csv",
    columns=(
        ColumnSpec("record_id", "R", "pseudonymize", gen_record_id),
        ColumnSpec("redcap_event_name", "NONE", "keep", gen_redcap_event_name),
        ColumnSpec("redcap_repeat_instrument", "NONE", "keep", gen_redcap_repeat_instrument),
        ColumnSpec("redcap_repeat_instance", "NONE", "keep", gen_redcap_repeat_instance),
        ColumnSpec("first_name", "A", "drop", gen_first_name),
        ColumnSpec("last_name", "A", "drop", gen_last_name),
        ColumnSpec("dob", "C", "year_only", gen_dob),
        ColumnSpec("mrn", "H", "pseudonymize", gen_mrn),
        ColumnSpec("phone", "D", "drop", gen_phone_us),
        ColumnSpec("email_address", "F", "drop", gen_email),
        ColumnSpec("street_address", "B", "drop", gen_street_address),
        ColumnSpec("zip_code", "B", "zip3_truncate", gen_zip5),
        ColumnSpec("consent_ip", "O", "drop", gen_consent_ip),
        ColumnSpec("portal_url", "N", "drop", gen_portal_url_redcap),
        ColumnSpec("race___1", "NONE", "keep", gen_race_checkbox),
        ColumnSpec("race___2", "NONE", "keep", gen_race_checkbox),
        ColumnSpec("race___3", "NONE", "keep", gen_race_checkbox),
        ColumnSpec("hba1c_percent", "NONE", "keep", gen_hba1c),
        ColumnSpec("bmi", "NONE", "keep", gen_bmi_redcap),
        ColumnSpec("comments_text", "NONE", "scrub_text", gen_notes_clinical_only),
        ColumnSpec("demographics_complete", "NONE", "keep", gen_complete_status),
        ColumnSpec("labs_complete", "NONE", "keep", gen_complete_status),
    ),
)

_L1_REDCAP = Scenario(
    id="l1_redcap_registry_v1",
    label="REDCap export, NIH investigator-initiated registry",
    jurisdictions=frozenset({"us"}),
    tier="L1",
    profile="messy",
    datasets=(_L1_REDCAP_RECORDS,),
    dictionary=(
        DictionaryRow("record_id", "Record identifier"),
        DictionaryRow("first_name", "Participant first name"),
        DictionaryRow("last_name", "Participant last name"),
        DictionaryRow("dob", "Date of birth", "date"),
        DictionaryRow("mrn", "Medical record number"),
        DictionaryRow("phone", "Phone number"),
        DictionaryRow("email_address", "Email address"),
        DictionaryRow("street_address", "Street address"),
        DictionaryRow("zip_code", "ZIP code"),
        DictionaryRow("consent_ip", "eConsent submission IP address"),
        DictionaryRow("portal_url", "Participant portal URL"),
        DictionaryRow("comments_text", "Free-text visit comments"),
    ),
)

SCENARIOS[_L1_REDCAP.id] = _L1_REDCAP


_L2_REDCAP_RECORDS = DatasetSpec(
    filename="records.csv",
    columns=(
        ColumnSpec("record_id", "R", "pseudonymize", gen_record_id),
        ColumnSpec("redcap_event_name", "NONE", "keep", gen_redcap_event_name),
        ColumnSpec("redcap_repeat_instrument", "NONE", "keep", gen_redcap_repeat_instrument),
        ColumnSpec("redcap_repeat_instance", "NONE", "keep", gen_redcap_repeat_instance),
        ColumnSpec("first_name", "A", "drop", gen_first_name),
        ColumnSpec("last_name", "A", "drop", gen_last_name),
        ColumnSpec("dob", "C", "year_only", gen_dob),
        ColumnSpec("mrn", "H", "pseudonymize", gen_mrn),
        ColumnSpec("phone", "D", "drop", gen_phone_us),
        ColumnSpec("email_address", "F", "drop", gen_email),
        ColumnSpec("street_address", "B", "drop", gen_street_address),
        ColumnSpec("zip_code", "B", "zip3_truncate", gen_zip5),
        ColumnSpec("consent_ip", "O", "drop", gen_consent_ip),
        ColumnSpec("portal_url", "N", "drop", gen_portal_url_redcap),
        ColumnSpec("emergency_contact_number", "D", "drop", gen_emergency_contact_number),
        ColumnSpec("aux_ref", "H", "pseudonymize", gen_aux_ref_mrn),
        ColumnSpec("race___1", "NONE", "keep", gen_race_checkbox),
        ColumnSpec("race___2", "NONE", "keep", gen_race_checkbox),
        ColumnSpec("race___3", "NONE", "keep", gen_race_checkbox),
        ColumnSpec("hba1c_percent", "NONE", "keep", gen_hba1c),
        ColumnSpec("bmi", "NONE", "keep", gen_bmi_redcap),
        ColumnSpec("comments_text", "NONE", "scrub_text", gen_notes_clinical_only),
        ColumnSpec("demographics_complete", "NONE", "keep", gen_complete_status),
        ColumnSpec("labs_complete", "NONE", "keep", gen_complete_status),
    ),
)

_L2_REDCAP = Scenario(
    id="l2_redcap_hostile_v1",
    label="REDCap export with unflagged identifiers and undocumented columns",
    jurisdictions=frozenset({"us"}),
    tier="L2",
    profile="hostile",
    datasets=(_L2_REDCAP_RECORDS,),
    dictionary=(
        DictionaryRow("record_id", "Record identifier"),
        DictionaryRow("first_name", "Participant first name"),
        DictionaryRow("last_name", "Participant last name"),
        DictionaryRow("dob", "Date of birth", "date"),
        DictionaryRow("mrn", "Medical record number"),
        DictionaryRow("phone", "Phone number"),
        DictionaryRow("email_address", "Email address"),
        DictionaryRow("street_address", "Street address"),
        DictionaryRow("zip_code", "ZIP code"),
        DictionaryRow("emergency_contact_number", "Emergency contact phone (documented as integer)", "int"),
        DictionaryRow("insurance_group", "Insurance group number (documented, absent from export)"),
        DictionaryRow("comments_text", "Free-text visit comments"),
    ),
)

SCENARIOS[_L2_REDCAP.id] = _L2_REDCAP


# ---- OMOP CDM v5.4: l1_omop_ehr_v1 ---------------------------------------

def gen_person_id(rng: random.Random) -> str:
    return str(rng.randint(100000, 999999))


def gen_omop_concept_id(rng: random.Random) -> str:
    return str(rng.randint(1000, 9999999))


def gen_omop_surrogate_id(rng: random.Random) -> str:
    return str(rng.randint(1, 99999))


def gen_person_source_value(rng: random.Random) -> str:
    return f"MRN{rng.randint(1000000, 9999999)}"


def gen_gender_source_value(rng: random.Random) -> str:
    return rng.choice(["M", "F", "U"])


def gen_race_source_value(rng: random.Random) -> str:
    return rng.choice(["White", "Black", "Asian", "Other", "Unknown"])


def gen_ethnicity_source_value(rng: random.Random) -> str:
    return rng.choice(["Hispanic", "Not Hispanic", "Unknown"])


def gen_birth_year(rng: random.Random) -> str:
    return str(rng.randint(1930, 2015))


def gen_birth_month(rng: random.Random) -> str:
    return str(rng.randint(1, 12))


def gen_birth_day(rng: random.Random) -> str:
    return str(rng.randint(1, 28))


def gen_birth_datetime(rng: random.Random) -> tuple[str, dict]:
    year = rng.randint(1930, 2015)
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)
    hour = rng.randint(0, 23)
    return f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:00:00", {"year": year}


def gen_address1(rng: random.Random) -> str:
    return gen_street_address(rng)


def gen_address2(rng: random.Random) -> str:
    return rng.choice(["", f"Apt {rng.randint(1, 40)}", f"Unit {rng.randint(1, 20)}"])


def gen_location_source_value(rng: random.Random) -> str:
    return f"{gen_street_address(rng)}, {gen_city_us(rng)}"


def gen_omop_zip(rng: random.Random) -> tuple[str, dict]:
    value = f"{rng.randint(10000, 99999):05d}"
    return value, {"zip3": value[:3]}


def gen_country_concept_id(rng: random.Random) -> str:
    return "4330435"  # OMOP standard concept id for United States


def gen_country_source_value(rng: random.Random) -> str:
    return "USA"


def gen_latitude(rng: random.Random) -> str:
    return f"{rng.uniform(25.0, 49.0):.5f}"


def gen_longitude(rng: random.Random) -> str:
    return f"{rng.uniform(-124.0, -67.0):.5f}"


def gen_visit_source_value(rng: random.Random) -> str:
    return rng.choice(["Outpatient", "Inpatient", "Emergency"])


def gen_visit_date(rng: random.Random) -> tuple[str, dict]:
    year = rng.randint(2020, 2024)
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)
    return f"{year:04d}-{month:02d}-{day:02d}", {"year": year}


def gen_visit_datetime(rng: random.Random) -> tuple[str, dict]:
    value, sem = gen_visit_date(rng)
    return value + " 09:00:00", sem


_L1_OMOP_PERSON = DatasetSpec(
    filename="person.csv",
    link_column="person_id",
    columns=(
        ColumnSpec("person_id", "NONE", "keep", gen_person_id),
        ColumnSpec("gender_concept_id", "NONE", "keep", gen_omop_concept_id),
        ColumnSpec("year_of_birth", "NONE", "keep", gen_birth_year),
        ColumnSpec("month_of_birth", "C", "drop", gen_birth_month),
        ColumnSpec("day_of_birth", "C", "drop", gen_birth_day),
        ColumnSpec("birth_datetime", "C", "year_only", gen_birth_datetime),
        ColumnSpec("race_concept_id", "NONE", "keep", gen_omop_concept_id),
        ColumnSpec("ethnicity_concept_id", "NONE", "keep", gen_omop_concept_id),
        ColumnSpec("location_id", "NONE", "keep", gen_omop_surrogate_id),
        ColumnSpec("provider_id", "NONE", "keep", gen_omop_surrogate_id),
        ColumnSpec("care_site_id", "NONE", "keep", gen_omop_surrogate_id),
        ColumnSpec("person_source_value", "H", "pseudonymize", gen_person_source_value),
        ColumnSpec("gender_source_value", "NONE", "keep", gen_gender_source_value),
        ColumnSpec("gender_source_concept_id", "NONE", "keep", gen_omop_concept_id),
        ColumnSpec("race_source_value", "NONE", "keep", gen_race_source_value),
        ColumnSpec("race_source_concept_id", "NONE", "keep", gen_omop_concept_id),
        ColumnSpec("ethnicity_source_value", "NONE", "keep", gen_ethnicity_source_value,
                   jitterable=True),
        ColumnSpec("ethnicity_source_concept_id", "NONE", "keep", gen_omop_concept_id),
    ),
)

_L1_OMOP_LOCATION = DatasetSpec(
    filename="location.csv",
    columns=(
        ColumnSpec("location_id", "NONE", "keep", gen_omop_surrogate_id),
        ColumnSpec("address_1", "B", "drop", gen_address1),
        ColumnSpec("address_2", "B", "drop", gen_address2),
        ColumnSpec("city", "B", "drop", gen_city_us),
        ColumnSpec("state", "NONE", "keep", gen_state_us),
        ColumnSpec("zip", "B", "zip3_truncate", gen_omop_zip),
        ColumnSpec("county", "B", "drop", gen_county_us),
        ColumnSpec("location_source_value", "B", "drop", gen_location_source_value),
        ColumnSpec("country_concept_id", "NONE", "keep", gen_country_concept_id),
        ColumnSpec("country_source_value", "NONE", "keep", gen_country_source_value),
        ColumnSpec("latitude", "B", "drop", gen_latitude),
        ColumnSpec("longitude", "B", "drop", gen_longitude),
    ),
)

_L1_OMOP_VISIT = DatasetSpec(
    filename="visit_occurrence.csv",
    link_column="person_id",
    columns=(
        ColumnSpec("visit_occurrence_id", "NONE", "keep", gen_omop_surrogate_id),
        ColumnSpec("person_id", "NONE", "keep", gen_person_id),
        ColumnSpec("visit_concept_id", "NONE", "keep", gen_omop_concept_id),
        ColumnSpec("visit_start_date", "C", "year_only", gen_visit_date),
        ColumnSpec("visit_start_datetime", "C", "year_only", gen_visit_datetime),
        ColumnSpec("visit_end_date", "C", "year_only", gen_visit_date),
        ColumnSpec("visit_type_concept_id", "NONE", "keep", gen_omop_concept_id),
        ColumnSpec("provider_id", "NONE", "keep", gen_omop_surrogate_id),
        ColumnSpec("care_site_id", "NONE", "keep", gen_omop_surrogate_id),
        ColumnSpec("visit_source_value", "NONE", "keep", gen_visit_source_value),
    ),
)

_L1_OMOP = Scenario(
    id="l1_omop_ehr_v1",
    label="OMOP CDM v5.4, EHR-derived observational research export",
    jurisdictions=frozenset({"us"}),
    tier="L1",
    profile="messy",
    datasets=(_L1_OMOP_PERSON, _L1_OMOP_LOCATION, _L1_OMOP_VISIT),
    dictionary=(
        DictionaryRow("person_id", "OMOP surrogate person key"),
        DictionaryRow("person_source_value", "Raw source-system person identifier (verbatim MRN)"),
        DictionaryRow("year_of_birth", "Year of birth (permitted bare year)", "int"),
        DictionaryRow("month_of_birth", "Month of birth", "int"),
        DictionaryRow("day_of_birth", "Day of birth", "int"),
        DictionaryRow("birth_datetime", "Full birth date/time", "date"),
        DictionaryRow("address_1", "Street address line 1"),
        DictionaryRow("address_2", "Street address line 2"),
        DictionaryRow("zip", "ZIP code"),
        DictionaryRow("location_source_value", "Raw source-system address string"),
        DictionaryRow("latitude", "Location latitude"),
        DictionaryRow("longitude", "Location longitude"),
        DictionaryRow("visit_start_date", "Visit start date", "date"),
        DictionaryRow("visit_start_datetime", "Visit start date/time", "date"),
        DictionaryRow("visit_end_date", "Visit end date", "date"),
    ),
)

SCENARIOS[_L1_OMOP.id] = _L1_OMOP


# ---- PCORnet CDM v7.0: l2_pcornet_raw_v1 ---------------------------------

_PCORNET_SEX = ("A", "F", "M", "NI", "UN", "OT")
_PCORNET_RACE = ("01", "02", "03", "04", "05", "06", "07", "NI", "UN", "OT")
_PCORNET_HISPANIC = ("Y", "N", "R", "NI", "UN", "OT")


def gen_patid(rng: random.Random) -> str:
    return f"PAT{rng.randint(100000, 999999)}"


def gen_pcornet_birth_date(rng: random.Random) -> tuple[str, dict]:
    year = rng.randint(1930, 2015)
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)
    return f"{year:04d}-{month:02d}-{day:02d}", {"year": year}


def gen_birth_time(rng: random.Random) -> str:
    return f"{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}"


def gen_pcornet_sex(rng: random.Random) -> str:
    return rng.choice(_PCORNET_SEX)


def gen_sexual_orientation(rng: random.Random) -> str:
    return rng.choice(("STR", "GAY", "BI", "NI", "UN", "OT"))


def gen_gender_identity(rng: random.Random) -> str:
    return rng.choice(("M", "F", "TM", "TF", "NI", "UN", "OT"))


def gen_hispanic(rng: random.Random) -> str:
    return rng.choice(_PCORNET_HISPANIC)


def gen_pcornet_race(rng: random.Random) -> str:
    return rng.choice(_PCORNET_RACE)


def gen_race_eth_flag(rng: random.Random) -> str:
    return rng.choice(("Y", "N"))


def gen_biobank_flag(rng: random.Random) -> str:
    return rng.choice(("Y", "N"))


def gen_pref_language(rng: random.Random) -> str:
    return rng.choice(("eng", "spa", "vie", "zho"))


def gen_raw_sex(rng: random.Random) -> tuple[str, dict]:
    code = rng.choice(_PCORNET_SEX)
    name = gen_name(rng)
    mrn = str(rng.randint(1000000, 9999999))
    value = f"{code} - {name} (MRN {mrn})"
    return value, {"literals": (name, mrn), "clinical_fragment": code}


def gen_raw_siteid(rng: random.Random) -> tuple[str, dict]:
    facility = rng.choice(["Mercy General", "Riverside Medical", "St. Anne Clinic", "Lakeside Health"])
    street = rng.choice(["4th St Clinic", "Main Campus", "West Wing", "North Annex"])
    zip5 = f"{rng.randint(10000, 99999):05d}"
    value = f"{facility} - {street}, {zip5}"
    return value, {"literals": (facility, street, zip5), "zip3": zip5[:3]}


def gen_raw_payer_name(rng: random.Random) -> tuple[str, dict]:
    payer = rng.choice(["BCBS", "Aetna", "UnitedHealth", "Cigna"])
    acct = str(rng.randint(10000000, 99999999))
    name = gen_name(rng)
    value = f"{payer} acct {acct} for {name}"
    return value, {"literals": (acct, name), "clinical_fragment": payer}


def gen_raw_generic_code(rng: random.Random) -> str:
    return rng.choice(("A", "B", "C", "UNK"))


def gen_org_patid(rng: random.Random) -> str:
    return f"ORG{rng.randint(1000, 9999)}"


def gen_managing_org(rng: random.Random) -> str:
    return rng.choice(("SiteA", "SiteB", "SiteC"))


def gen_mrn_pcornet(rng: random.Random) -> str:
    return f"MRN{rng.randint(1000000, 9999999)}"


def gen_pat_first(rng: random.Random) -> str:
    return gen_name(rng).split(" ")[0]


def gen_pat_middle(rng: random.Random) -> str:
    return rng.choice(("", "Ann", "Lee", "James", "Marie"))


def gen_pat_last(rng: random.Random) -> str:
    return gen_name(rng).split(" ")[1]


def gen_pat_maiden(rng: random.Random) -> str:
    return rng.choice(("", "Walsh", "Nguyen", "Kowalski", "Silva"))


def gen_pat_ssn(rng: random.Random) -> str:
    if rng.random() < 0.5:
        return f"XXX-XX-{rng.randint(1000, 9999)}"
    return f"{rng.randint(100, 999):03d}-{rng.randint(10, 99):02d}-{rng.randint(1000, 9999):04d}"


def gen_primary_email(rng: random.Random) -> str:
    return gen_email(rng)


def gen_primary_phone_pcornet(rng: random.Random) -> str:
    return f"{rng.randint(200, 999)}{rng.randint(200, 999)}{rng.randint(1000, 9999)}"


def gen_encounterid(rng: random.Random) -> str:
    return f"ENC{rng.randint(1000000, 9999999)}"


def gen_pcornet_date(rng: random.Random) -> tuple[str, dict]:
    year = rng.randint(2020, 2024)
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)
    return f"{year:04d}-{month:02d}-{day:02d}", {"year": year}


def gen_admit_time(rng: random.Random) -> str:
    return f"{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}"


def gen_providerid(rng: random.Random) -> str:
    return f"PROV{rng.randint(10000, 99999)}"


def gen_facility_location(rng: random.Random) -> tuple[str, dict]:
    zip5 = f"{rng.randint(10000, 99999):05d}"
    return zip5, {"zip3": zip5[:3]}


def gen_enc_type(rng: random.Random) -> str:
    return rng.choice(("EI", "IP", "OA", "ED"))


def gen_facilityid(rng: random.Random) -> str:
    return f"FAC{rng.randint(1000, 9999)}"


def gen_raw_payer_id(rng: random.Random) -> str:
    return f"PAYERID{rng.randint(100000, 999999)}"


_L2_PCORNET_DEMO = DatasetSpec(
    filename="demographic.csv",
    link_column="PATID",
    columns=(
        ColumnSpec("PATID", "R", "pseudonymize", gen_patid),
        ColumnSpec("BIRTH_DATE", "C", "year_only", gen_pcornet_birth_date),
        ColumnSpec("BIRTH_TIME", "C", "drop", gen_birth_time),
        ColumnSpec("SEX", "NONE", "keep", gen_pcornet_sex),
        ColumnSpec("SEXUAL_ORIENTATION", "NONE", "keep", gen_sexual_orientation),
        ColumnSpec("GENDER_IDENTITY", "NONE", "keep", gen_gender_identity),
        ColumnSpec("HISPANIC", "NONE", "keep", gen_hispanic),
        ColumnSpec("RACE", "NONE", "keep", gen_pcornet_race),
        ColumnSpec("RACE_ETH_MISSING", "NONE", "keep", gen_race_eth_flag),
        ColumnSpec("RACE_ETH_AI_AN", "NONE", "keep", gen_race_eth_flag),
        ColumnSpec("RACE_ETH_ASIAN", "NONE", "keep", gen_race_eth_flag),
        ColumnSpec("RACE_ETH_BLACK", "NONE", "keep", gen_race_eth_flag),
        ColumnSpec("RACE_ETH_HISPANIC", "NONE", "keep", gen_race_eth_flag),
        ColumnSpec("RACE_ETH_ME_NA", "NONE", "keep", gen_race_eth_flag),
        ColumnSpec("RACE_ETH_NH_PI", "NONE", "keep", gen_race_eth_flag),
        ColumnSpec("RACE_ETH_WHITE", "NONE", "keep", gen_race_eth_flag),
        ColumnSpec("BIOBANK_FLAG", "NONE", "keep", gen_biobank_flag),
        ColumnSpec("PAT_PREF_LANGUAGE_SPOKEN", "NONE", "keep", gen_pref_language),
        ColumnSpec("RAW_SEX", "A", "scrub_text", gen_raw_sex),
        ColumnSpec("RAW_SEXUAL_ORIENTATION", "NONE", "keep", gen_raw_generic_code),
        ColumnSpec("RAW_GENDER_IDENTITY", "NONE", "keep", gen_raw_generic_code),
        ColumnSpec("RAW_HISPANIC", "NONE", "keep", gen_raw_generic_code),
        ColumnSpec("RAW_RACE", "NONE", "keep", gen_raw_generic_code),
        ColumnSpec("RAW_PAT_PREF_LANGUAGE_SPOKEN", "NONE", "keep", gen_pref_language),
    ),
)

_L2_PCORNET_PRIVATE = DatasetSpec(
    filename="private_demographic.csv",
    link_column="PATID",
    columns=(
        ColumnSpec("PATID", "R", "pseudonymize", gen_patid),
        ColumnSpec("ORG_PATID", "NONE", "keep", gen_org_patid),
        ColumnSpec("MANAGING_ORG", "NONE", "keep", gen_managing_org),
        ColumnSpec("MRN", "H", "pseudonymize", gen_mrn_pcornet),
        ColumnSpec("PAT_FIRSTNAME", "A", "drop", gen_pat_first),
        ColumnSpec("PAT_MIDDLENAME", "A", "drop", gen_pat_middle),
        ColumnSpec("PAT_LASTNAME", "A", "drop", gen_pat_last),
        ColumnSpec("PAT_MAIDENNAME", "A", "drop", gen_pat_maiden),
        ColumnSpec("PAT_SSN", "G", "drop", gen_pat_ssn),
        ColumnSpec("BIRTH_DATE", "C", "year_only", gen_pcornet_birth_date),
        ColumnSpec("BIRTH_TIME", "C", "drop", gen_birth_time),
        ColumnSpec("PRIMARY_EMAIL", "F", "drop", gen_primary_email),
        ColumnSpec("PRIMARY_PHONE", "D", "drop", gen_primary_phone_pcornet),
        ColumnSpec("SEX", "NONE", "keep", gen_pcornet_sex),
    ),
)

_L2_PCORNET_ENC = DatasetSpec(
    filename="encounter.csv",
    link_column="PATID",
    columns=(
        ColumnSpec("ENCOUNTERID", "NONE", "keep", gen_encounterid),
        ColumnSpec("PATID", "R", "pseudonymize", gen_patid),
        ColumnSpec("ADMIT_DATE", "C", "year_only", gen_pcornet_date),
        ColumnSpec("ADMIT_TIME", "NONE", "keep", gen_admit_time),
        ColumnSpec("DISCHARGE_DATE", "C", "year_only", gen_pcornet_date),
        ColumnSpec("PROVIDERID", "R", "pseudonymize", gen_providerid),
        ColumnSpec("FACILITY_LOCATION", "B", "zip3_truncate", gen_facility_location),
        ColumnSpec("ENC_TYPE", "NONE", "keep", gen_enc_type),
        ColumnSpec("FACILITYID", "R", "pseudonymize", gen_facilityid),
        ColumnSpec("RAW_SITEID", "B", "scrub_text", gen_raw_siteid),
        ColumnSpec("RAW_PAYER_NAME_PRIMARY", "A", "scrub_text", gen_raw_payer_name),
        ColumnSpec("RAW_PAYER_ID_PRIMARY", "J", "drop", gen_raw_payer_id),
    ),
)

_L2_PCORNET = Scenario(
    id="l2_pcornet_raw_v1",
    label="PCORnet CDM v7.0 with RAW_* source-value leakage",
    jurisdictions=frozenset({"us"}),
    tier="L2",
    profile="hostile",
    datasets=(_L2_PCORNET_DEMO, _L2_PCORNET_PRIVATE, _L2_PCORNET_ENC),
    dictionary=(
        DictionaryRow("PATID", "Patient identifier"),
        DictionaryRow("MRN", "Medical record number"),
        DictionaryRow("PAT_FIRSTNAME", "Patient first name"),
        DictionaryRow("PAT_LASTNAME", "Patient last name"),
        DictionaryRow("PAT_SSN", "Social security number"),
        DictionaryRow("PRIMARY_EMAIL", "Primary email address"),
        DictionaryRow("PRIMARY_PHONE", "Primary phone number"),
        DictionaryRow("BIRTH_DATE", "Date of birth", "date"),
        DictionaryRow("FACILITY_LOCATION", "5-digit ZIP of care facility"),
        DictionaryRow("RAW_SEX", "Unmapped source sex value (verbatim EHR string)"),
        DictionaryRow("RAW_SITEID", "Unmapped source site value (verbatim EHR string)"),
        DictionaryRow("RAW_PAYER_NAME_PRIMARY", "Unmapped source payer value (verbatim EHR string)"),
    ),
)

SCENARIOS[_L2_PCORNET.id] = _L2_PCORNET


# ---- NAACCR cancer registry: l2_naaccr_registry_v1 -----------------------

def gen_naaccr_last(rng: random.Random) -> str:
    return gen_name(rng).split(" ")[1]


def gen_naaccr_first(rng: random.Random) -> str:
    return gen_name(rng).split(" ")[0]


def gen_naaccr_maiden(rng: random.Random) -> str:
    return rng.choice(("", "Walsh", "Nguyen", "Kowalski", "Silva"))


def gen_naaccr_street(rng: random.Random) -> str:
    return gen_street_address(rng)


def gen_naaccr_state(rng: random.Random) -> str:
    return gen_state_us(rng)


def gen_naaccr_zip(rng: random.Random) -> tuple[str, dict]:
    value = f"{rng.randint(10000, 99999):05d}"
    return value, {"zip3": value[:3]}


def gen_naaccr_ssn(rng: random.Random) -> str:
    return "".join(str(rng.randint(0, 9)) for _ in range(9))


def gen_naaccr_mrn(rng: random.Random) -> str:
    return f"MRN{rng.randint(100000, 99999999)}"


def gen_naaccr_patient_id(rng: random.Random) -> str:
    return f"{rng.randint(1, 99999999):08d}"


def gen_naaccr_dob(rng: random.Random) -> tuple[str, dict]:
    year = rng.randint(1930, 2015)
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)
    return f"{year:04d}{month:02d}{day:02d}", {"year": year}


def gen_naaccr_dx_date(rng: random.Random) -> tuple[str, dict]:
    year = rng.randint(2018, 2024)
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)
    return f"{year:04d}{month:02d}{day:02d}", {"year": year}


def gen_naaccr_phone(rng: random.Random) -> str:
    return f"{rng.randint(200, 999)}-{rng.randint(200, 999)}-{rng.randint(1000, 9999)}"


def gen_naaccr_age(rng: random.Random) -> tuple[str, dict]:
    age = rng.randint(18, 95)
    return str(age), {"age": age}


def gen_primary_site(rng: random.Random) -> str:
    return rng.choice(["C50.9", "C34.1", "C61.9", "C18.7", "C16.0"])


def gen_histologic_type(rng: random.Random) -> str:
    return rng.choice(["8500/3", "8140/3", "8070/3", "8010/3"])


_L2_NAACCR = Scenario(
    id="l2_naaccr_registry_v1",
    label="NAACCR cancer-registry abstract",
    jurisdictions=frozenset({"us"}),
    tier="L2",
    profile="hostile",
    datasets=(
        DatasetSpec(
            filename="naaccr_abstract.csv",
            columns=(
                ColumnSpec("Name--Last", "A", "drop", gen_naaccr_last),
                ColumnSpec("Name--First", "A", "drop", gen_naaccr_first),
                ColumnSpec("Name--Maiden", "A", "drop", gen_naaccr_maiden),
                ColumnSpec("Addr at DX--No & Street", "B", "drop", gen_naaccr_street),
                ColumnSpec("Addr at DX--City", "B", "drop", gen_city_us),
                ColumnSpec("Addr at DX--State", "NONE", "keep", gen_naaccr_state),
                ColumnSpec("Addr at DX--Postal Code", "B", "zip3_truncate", gen_naaccr_zip),
                ColumnSpec("County at DX", "B", "drop", gen_county_us),
                ColumnSpec("Social Security Number", "G", "drop", gen_naaccr_ssn),
                ColumnSpec("Medical Record Number", "H", "pseudonymize", gen_naaccr_mrn),
                ColumnSpec("Patient ID Number", "R", "pseudonymize", gen_naaccr_patient_id),
                ColumnSpec("Date of Birth", "C", "year_only", gen_naaccr_dob),
                ColumnSpec("Date of Diagnosis", "C", "year_only", gen_naaccr_dx_date),
                ColumnSpec("Age at Diagnosis", "C", "cap_age_90", gen_naaccr_age),
                ColumnSpec("Telephone", "D", "drop", gen_naaccr_phone),
                ColumnSpec("Primary Site", "NONE", "keep", gen_primary_site),
                ColumnSpec("Histologic Type ICD-O-3", "NONE", "keep", gen_histologic_type),
            ),
        ),
    ),
    dictionary=(
        DictionaryRow("Name--Last", "Patient last name"),
        DictionaryRow("Name--First", "Patient first name"),
        DictionaryRow("Social Security Number", "Patient SSN, 9 digits no dashes"),
        DictionaryRow("Medical Record Number", "Facility medical record number"),
        DictionaryRow("Patient ID Number", "Registry patient identifier, 8-digit zero-filled"),
        DictionaryRow("Date of Birth", "Date of birth, YYYYMMDD", "date"),
        DictionaryRow("Date of Diagnosis", "Date of diagnosis, YYYYMMDD", "date"),
        DictionaryRow("Age at Diagnosis", "Age in years at diagnosis", "int"),
        DictionaryRow("Addr at DX--Postal Code", "ZIP code at diagnosis"),
        DictionaryRow("Primary Site", "ICD-O-3 primary site code"),
        DictionaryRow("Histologic Type ICD-O-3", "ICD-O-3 histology code"),
    ),
)

SCENARIOS[_L2_NAACCR.id] = _L2_NAACCR


# ---- CMS CCW claims: l2_cms_claims_v1 ------------------------------------

def gen_bene_id(rng: random.Random) -> str:
    return f"{rng.choice(string.ascii_uppercase)}{rng.randint(100000000, 999999999)}"


def gen_desy_sort_key(rng: random.Random) -> str:
    return str(rng.randint(100000, 999999))


def gen_msis_id(rng: random.Random) -> str:
    return str(rng.randint(100000000, 999999999))


def gen_hicno(rng: random.Random) -> str:
    digits = "".join(str(rng.randint(0, 9)) for _ in range(9))
    suffix = "".join(rng.choice(string.ascii_uppercase) for _ in range(rng.choice([1, 2])))
    return digits + suffix


_MBI_ALPHA = "ABCDEFGHJKLMNPQRTUVWXY"
_MBI_ALPHANUM = _MBI_ALPHA + string.digits


def gen_mbi(rng: random.Random) -> str:
    parts = [
        str(rng.randint(1, 9)),
        rng.choice(_MBI_ALPHA),
        rng.choice(_MBI_ALPHANUM),
        str(rng.randint(0, 9)),
        rng.choice(_MBI_ALPHA),
        rng.choice(_MBI_ALPHANUM),
        str(rng.randint(0, 9)),
        rng.choice(_MBI_ALPHA),
        rng.choice(_MBI_ALPHA),
        str(rng.randint(0, 9)),
        str(rng.randint(0, 9)),
    ]
    return "".join(parts)


def gen_bene_zip9(rng: random.Random) -> tuple[str, dict]:
    zip5 = f"{rng.randint(10000, 99999):05d}"
    plus4 = f"{rng.randint(0, 9999):04d}"
    return zip5 + plus4, {"zip3": zip5[:3]}


def gen_bene_birth_dt(rng: random.Random) -> tuple[str, dict]:
    year = rng.randint(1930, 2015)
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)
    return f"{year:04d}{month:02d}{day:02d}", {"year": year}


def gen_bene_death_dt(rng: random.Random) -> tuple[str, dict]:
    year = rng.randint(2015, 2024)
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)
    if rng.random() < 0.7:
        return "", {"year": year, "missing": True}
    return f"{year:04d}{month:02d}{day:02d}", {"year": year}


def gen_clm_id(rng: random.Random) -> str:
    return f"CLM{rng.randint(1000000000, 9999999999)}"


def gen_prvdr_num(rng: random.Random) -> str:
    state_code = f"{rng.randint(1, 50):02d}"
    return f"{state_code}{rng.randint(1000, 9999)}"


def gen_tax_num(rng: random.Random) -> str:
    return f"{rng.randint(10, 99)}-{rng.randint(1000000, 9999999)}"


def gen_dgns_cd(rng: random.Random) -> str:
    return rng.choice(["E11.9", "I10", "J45.909", "M54.5"])


def gen_clm_type(rng: random.Random) -> str:
    return rng.choice(["Inpatient", "Outpatient", "Carrier", "DME"])


_L2_CMS_MBSF = DatasetSpec(
    filename="mbsf.csv",
    link_column="BENE_ID",
    columns=(
        ColumnSpec("BENE_ID", "R", "pseudonymize", gen_bene_id),
        ColumnSpec("DESY_SORT_KEY", "H", "pseudonymize", gen_desy_sort_key),
        ColumnSpec("MSIS_ID", "H", "pseudonymize", gen_msis_id),
        ColumnSpec("HICNO", "G", "drop", gen_hicno),
        ColumnSpec("MBI", "I", "drop", gen_mbi),
        ColumnSpec("BENE_ZIP_CD", "B", "zip3_truncate", gen_bene_zip9),
        ColumnSpec("BENE_BIRTH_DT", "C", "year_only", gen_bene_birth_dt),
        ColumnSpec("BENE_DEATH_DT", "C", "year_only", gen_bene_death_dt),
    ),
)

_L2_CMS_CLAIMS = DatasetSpec(
    filename="claims.csv",
    link_column="BENE_ID",
    rows_per_subject=2,
    columns=(
        ColumnSpec("BENE_ID", "R", "pseudonymize", gen_bene_id),
        ColumnSpec("CLM_ID", "R", "pseudonymize", gen_clm_id),
        ColumnSpec("PRVDR_NUM", "K", "drop", gen_prvdr_num),
        ColumnSpec("NPI", "K", "drop", gen_npi_luhn),
        ColumnSpec("TAX_NUM", "J", "drop", gen_tax_num),
        ColumnSpec("DGNS_CD", "NONE", "keep", gen_dgns_cd),
        ColumnSpec("CLM_TYPE", "NONE", "keep", gen_clm_type),
    ),
)

_L2_CMS = Scenario(
    id="l2_cms_claims_v1",
    label="CMS CCW claims extract",
    jurisdictions=frozenset({"us"}),
    tier="L2",
    profile="hostile",
    datasets=(_L2_CMS_MBSF, _L2_CMS_CLAIMS),
    dictionary=(
        DictionaryRow("BENE_ID", "Beneficiary identifier"),
        DictionaryRow("HICNO", "Legacy Health Insurance Claim Number"),
        DictionaryRow("MBI", "Medicare Beneficiary Identifier"),
        DictionaryRow("BENE_ZIP_CD", "Beneficiary ZIP+4"),
        DictionaryRow("BENE_BIRTH_DT", "Beneficiary date of birth, YYYYMMDD", "date"),
        DictionaryRow("BENE_DEATH_DT", "Beneficiary date of death, YYYYMMDD", "date"),
        DictionaryRow("CLM_ID", "Claim identifier"),
        DictionaryRow("PRVDR_NUM", "CMS Certification Number"),
        DictionaryRow("NPI", "National Provider Identifier"),
        DictionaryRow("TAX_NUM", "Provider tax identifier"),
    ),
)

SCENARIOS[_L2_CMS.id] = _L2_CMS


# ---- i2b2 / ACT: l3_i2b2_crosswalk_v1 ------------------------------------

def gen_patient_num(rng: random.Random) -> str:
    return str(rng.randint(100000, 999999))


def gen_patient_ide(rng: random.Random) -> str:
    return f"MRN{rng.randint(1000000, 9999999)}"


def gen_patient_ide_source(rng: random.Random) -> str:
    return "MRN@RIVERSIDE"


def gen_patient_ide_status(rng: random.Random) -> str:
    return "A"


def gen_project_id(rng: random.Random) -> str:
    return "RIVERSIDE_ACT"


def gen_vital_status_cd(rng: random.Random) -> str:
    return rng.choice(["Y", "N"])


def gen_i2b2_birth_date(rng: random.Random) -> tuple[str, dict]:
    year = rng.randint(1930, 2015)
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)
    return f"{year:04d}-{month:02d}-{day:02d} 00:00:00", {"year": year}


def gen_i2b2_death_date(rng: random.Random) -> tuple[str, dict]:
    year = rng.randint(2015, 2024)
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)
    if rng.random() < 0.7:
        return "", {"year": year, "missing": True}
    return f"{year:04d}-{month:02d}-{day:02d} 00:00:00", {"year": year}


def gen_sex_cd(rng: random.Random) -> str:
    return rng.choice(["M", "F", "U"])


def gen_age_in_years(rng: random.Random) -> tuple[str, dict]:
    age = rng.randint(18, 97)
    return str(age), {"age": age}


def gen_language_cd(rng: random.Random) -> str:
    return rng.choice(["ENG", "SPA", "VIE"])


def gen_race_cd(rng: random.Random) -> str:
    return rng.choice(["WHITE", "BLACK", "ASIAN", "OTHER"])


def gen_marital_status_cd(rng: random.Random) -> str:
    return rng.choice(["MARRIED", "SINGLE", "DIVORCED", "WIDOWED"])


def gen_religion_cd(rng: random.Random) -> str:
    return rng.choice(["", "CATHOLIC", "PROTESTANT", "JEWISH", "OTHER"])


def gen_zip_cd(rng: random.Random) -> tuple[str, dict]:
    value = f"{rng.randint(10000, 99999):05d}"
    return value, {"zip3": value[:3]}


def gen_statecityzip_path(rng: random.Random) -> str:
    zip5 = f"{rng.randint(10000, 99999):05d}"
    return f"\\ILLINOIS\\CHICAGO\\{zip5}\\"


def gen_income_cd(rng: random.Random) -> str:
    return rng.choice(["<25K", "25-50K", "50-100K", ">100K"])


def gen_system_date(rng: random.Random) -> str:
    year = rng.randint(2023, 2025)
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)
    return f"{year:04d}-{month:02d}-{day:02d}"


def gen_sourcesystem_cd(rng: random.Random) -> str:
    return "RIVERSIDE_EHR"


def gen_upload_id(rng: random.Random) -> str:
    return str(rng.randint(1, 999))


def gen_patient_blob(rng: random.Random) -> tuple[str, dict]:
    """An unlabeled MRN embedded in free text -- a distinct plant from
    PATIENT_IDE (not byte-linked to it): the point is that BOTH paths must
    be independently neutralised, not that they carry the identical value.
    """
    mrn = f"MRN{rng.randint(1000000, 9999999)}"
    note = rng.choice([
        f"Chart notes reference {mrn} from prior admission.",
        f"Cross-referenced record {mrn} pending merge review.",
        f"Legacy identifier {mrn} retained for audit trail.",
    ])
    return note, {"literals": (mrn,)}


_L3_I2B2_DIM = DatasetSpec(
    filename="patient_dimension.csv",
    link_column="PATIENT_NUM",
    columns=(
        ColumnSpec("PATIENT_NUM", "NONE", "keep", gen_patient_num),
        ColumnSpec("VITAL_STATUS_CD", "NONE", "keep", gen_vital_status_cd),
        ColumnSpec("BIRTH_DATE", "C", "year_only", gen_i2b2_birth_date),
        ColumnSpec("DEATH_DATE", "C", "year_only", gen_i2b2_death_date),
        ColumnSpec("SEX_CD", "NONE", "keep", gen_sex_cd),
        ColumnSpec("AGE_IN_YEARS_NUM", "C", "cap_age_90", gen_age_in_years),
        ColumnSpec("LANGUAGE_CD", "NONE", "keep", gen_language_cd),
        ColumnSpec("RACE_CD", "NONE", "keep", gen_race_cd),
        ColumnSpec("MARITAL_STATUS_CD", "NONE", "keep", gen_marital_status_cd),
        ColumnSpec("RELIGION_CD", "NONE", "keep", gen_religion_cd),
        ColumnSpec("ZIP_CD", "B", "zip3_truncate", gen_zip_cd),
        ColumnSpec("STATECITYZIP_PATH", "B", "drop", gen_statecityzip_path),
        ColumnSpec("INCOME_CD", "NONE", "keep", gen_income_cd),
        ColumnSpec("PATIENT_BLOB", "H", "scrub_text", gen_patient_blob),
        ColumnSpec("UPDATE_DATE", "NONE", "keep", gen_system_date),
        ColumnSpec("DOWNLOAD_DATE", "NONE", "keep", gen_system_date),
        ColumnSpec("IMPORT_DATE", "NONE", "keep", gen_system_date),
        ColumnSpec("SOURCESYSTEM_CD", "NONE", "keep", gen_sourcesystem_cd),
        ColumnSpec("UPLOAD_ID", "NONE", "keep", gen_upload_id),
    ),
)

_L3_I2B2_MAP = DatasetSpec(
    filename="patient_mapping.csv",
    link_column="PATIENT_NUM",
    columns=(
        ColumnSpec("PATIENT_IDE", "H", "pseudonymize", gen_patient_ide),
        ColumnSpec("PATIENT_IDE_SOURCE", "NONE", "keep", gen_patient_ide_source),
        ColumnSpec("PATIENT_NUM", "NONE", "keep", gen_patient_num),
        ColumnSpec("PATIENT_IDE_STATUS", "NONE", "keep", gen_patient_ide_status),
        ColumnSpec("PROJECT_ID", "NONE", "keep", gen_project_id),
    ),
)

_L3_I2B2 = Scenario(
    id="l3_i2b2_crosswalk_v1",
    label="i2b2 / ACT enterprise cohort discovery, patient crosswalk",
    jurisdictions=frozenset({"us"}),
    tier="L3",
    profile="hostile",
    datasets=(_L3_I2B2_DIM, _L3_I2B2_MAP),
    dictionary=(
        DictionaryRow("PATIENT_NUM", "i2b2 internal surrogate patient key"),
        DictionaryRow("PATIENT_IDE", "External patient identifier (crosswalk to MRN)"),
        DictionaryRow("PATIENT_IDE_SOURCE", "Source system of the external identifier"),
        DictionaryRow("ZIP_CD", "Patient ZIP code"),
        DictionaryRow("STATECITYZIP_PATH", "Geography ontology path"),
        DictionaryRow("PATIENT_BLOB", "Unstructured patient notes"),
        DictionaryRow("BIRTH_DATE", "Date of birth", "date"),
        DictionaryRow("DEATH_DATE", "Date of death", "date"),
        DictionaryRow("AGE_IN_YEARS_NUM", "Age in years", "int"),
    ),
)

SCENARIOS[_L3_I2B2.id] = _L3_I2B2


# ---- Keeper-header hijack: l3_keeper_hijack_v1 ---------------------------

def gen_barcode_mrn(rng: random.Random) -> str:
    return f"MRN{rng.randint(1000000, 9999999)}"


def gen_specimen_ssn(rng: random.Random) -> str:
    return f"{rng.randint(100, 999):03d}-{rng.randint(10, 99):02d}-{rng.randint(1000, 9999):04d}"


def gen_study_arm_name(rng: random.Random) -> str:
    return gen_name(rng)


def gen_state_address(rng: random.Random) -> str:
    return rng.choice(["Chicago, IL 60614", "Boston, MA 02115", "Austin, TX 78701"])


def gen_hr_phone_inversion(rng: random.Random) -> str:
    return f"415-555-{rng.randint(1000, 9999):04d}"


def gen_patient_name_clinical_term(rng: random.Random) -> str:
    return rng.choice(["sinus tachycardia", "atrial fibrillation", "stable angina", "normal sinus rhythm"])


def gen_asset_tag_vin(rng: random.Random) -> str:
    chars = string.ascii_uppercase.replace("I", "").replace("O", "").replace("Q", "") + string.digits
    return "".join(rng.choice(chars) for _ in range(17))


def gen_unit_ref_imei(rng: random.Random) -> str:
    return "".join(str(rng.randint(0, 9)) for _ in range(15))


def gen_referrer_num_npi(rng: random.Random) -> str:
    return gen_npi_luhn(rng)


def gen_device_version(rng: random.Random) -> str:
    return f"{rng.randint(1, 9)}.{rng.randint(0, 9)}.{rng.randint(0, 99)}"


def gen_mac_address(rng: random.Random) -> str:
    return ":".join(f"{rng.randint(0, 255):02X}" for _ in range(6))


def gen_source_device_id(rng: random.Random) -> str:
    return f"SDI-{rng.randint(100000, 999999)}"


def gen_transmitter_id(rng: random.Random) -> str:
    return "".join(rng.choice(string.ascii_uppercase + string.digits) for _ in range(6))


def gen_patient_info_name(rng: random.Random) -> str:
    return gen_name(rng)


_L3_KEEPER_HIJACK = Scenario(
    id="l3_keeper_hijack_v1",
    label="Keeper-header hijack and guard-anchor evasion",
    jurisdictions=frozenset({"us"}),
    tier="L3",
    profile="messy",  # not "hostile": a BOM-prefixed first column would mask
                       # the intended structural leak on "barcode" (see
                       # replay.py header-decoding note) with an unrelated
                       # encoding defect; this scenario's thesis is
                       # header-based classification hijack, not encoding.
    datasets=(
        DatasetSpec(
            filename="hijack.csv",
            columns=(
                ColumnSpec("barcode", "H", "pseudonymize", gen_barcode_mrn),
                ColumnSpec("specimen_id", "G", "drop", gen_specimen_ssn),
                ColumnSpec("study_arm", "A", "drop", gen_study_arm_name),
                ColumnSpec("visit_number", "D", "drop", gen_hr_phone_inversion),
                ColumnSpec("state", "B", "drop", gen_state_address),
                ColumnSpec("heart_rate_bpm", "D", "drop", gen_hr_phone_inversion),
                ColumnSpec("patient_name", "NONE", "keep", gen_patient_name_clinical_term,
                           jitterable=True),
                ColumnSpec("asset_tag", "L", "drop", gen_asset_tag_vin),
                ColumnSpec("unit_ref", "M", "drop", gen_unit_ref_imei),
                ColumnSpec("referrer_num", "K", "drop", gen_referrer_num_npi),
                ColumnSpec("deviceVersion", "M", "drop", gen_device_version),
                ColumnSpec("mac", "M", "drop", gen_mac_address),
                ColumnSpec("Source Device ID", "M", "drop", gen_source_device_id),
                ColumnSpec("Transmitter ID", "M", "drop", gen_transmitter_id),
                ColumnSpec("Patient Info", "A", "drop", gen_patient_info_name),
            ),
        ),
    ),
    dictionary=(
        DictionaryRow("barcode", "Specimen barcode (header forces keep; carries an MRN)"),
        DictionaryRow("specimen_id", "Specimen identifier (header forces keep; carries an SSN)"),
        DictionaryRow("study_arm", "Study arm assignment (header forces keep; carries a name)"),
        DictionaryRow("state", "US state (header forces keep; carries a full address)"),
        DictionaryRow("heart_rate_bpm", "Heart rate (header forces keep; carries a phone number)"),
        DictionaryRow("patient_name", "Header implies PHI; cell carries a clinical term only"),
        DictionaryRow("asset_tag", "Equipment asset tag; carries a bare VIN with no guard anchor"),
        DictionaryRow("unit_ref", "Equipment unit reference; carries a bare 15-digit IMEI"),
        DictionaryRow("referrer_num", "Referring identifier; carries a Luhn-valid bare NPI"),
        DictionaryRow("Patient Info", "Device record patient field; carries a real name"),
    ),
)

SCENARIOS[_L3_KEEPER_HIJACK.id] = _L3_KEEPER_HIJACK


# Categories A/B are not Publish Guard backstops. A names-and-addresses-only
# keeper-header hijack would evade it without deterministic keep verification.
_L3_KEEPER_HIJACK_NAMES = Scenario(
    id="l3_keeper_hijack_names_v1",
    label="Keeper-header names and addresses hijack",
    jurisdictions=frozenset({"us"}),
    tier="L3",
    profile="messy",
    datasets=(
        DatasetSpec(
            filename="hijack_names.csv",
            columns=(
                ColumnSpec("study_arm", "A", "drop", gen_study_arm_name),
                ColumnSpec("treatment_group", "A", "drop", gen_patient_info_name),
                ColumnSpec("state", "B", "drop", gen_state_address),
                ColumnSpec("country", "B", "drop", gen_state_address),
                ColumnSpec("race", "A", "drop", gen_name),
                ColumnSpec("sex", "NONE", "keep", gen_sex),
            ),
        ),
    ),
    dictionary=(
        DictionaryRow("study_arm", "Study arm assignment contains a person name"),
        DictionaryRow("treatment_group", "Treatment group contains a person name"),
        DictionaryRow("state", "State field contains a street address"),
        DictionaryRow("country", "Country field contains a street address"),
        DictionaryRow("race", "Race field contains a person name"),
        DictionaryRow("sex", "Biological sex stratifier"),
    ),
)

SCENARIOS[_L3_KEEPER_HIJACK_NAMES.id] = _L3_KEEPER_HIJACK_NAMES


# ---- Quasi-identifier actual-knowledge test: l3_quasi_identifier_v1 ------

def gen_seqn(rng: random.Random) -> str:
    return str(rng.randint(100000, 199999))


def gen_ridageyr(rng: random.Random) -> str:
    """NCHS top-codes every participant aged 80+ to exactly 80 before
    public release; the pipeline must leave an already top-coded value
    alone rather than re-deriving a fabricated '90+'."""
    age = rng.randint(18, 95)
    return "80" if age >= 80 else str(age)


def gen_ridreth3(rng: random.Random) -> str:
    return rng.choice(["1", "2", "3", "4", "6", "7"])


def gen_dmdborn4(rng: random.Random) -> str:
    return rng.choice(["1", "2", "7", "9"])


def gen_qi_zip3(rng: random.Random) -> tuple[str, dict]:
    value = rng.choice(_RESTRICTED_ZIP3)
    return value, {"zip3": value}


def gen_qi_sex(rng: random.Random) -> str:
    return rng.choice(["M", "F"])


def gen_qi_birth_year(rng: random.Random) -> tuple[str, dict]:
    year = rng.randint(1930, 2006)
    return str(year), {"year": year}


def gen_qi_rare_dx(rng: random.Random) -> str:
    return rng.choice(["E75.4", "Q87.1", "G71.11"])


def gen_sud_dx_flag(rng: random.Random) -> str:
    return rng.choice(["Y", "N"])


_L3_QI = Scenario(
    id="l3_quasi_identifier_v1",
    label="NHANES-style survey extract, quasi-identifier actual-knowledge test",
    jurisdictions=frozenset({"us"}),
    tier="L3",
    profile="messy",
    datasets=(
        DatasetSpec(
            filename="survey.csv",
            columns=(
                ColumnSpec("SEQN", "NONE", "keep", gen_seqn),
                ColumnSpec("RIDAGEYR", "NONE", "keep", gen_ridageyr),
                ColumnSpec("RIDRETH3", "NONE", "keep", gen_ridreth3),
                ColumnSpec("DMDBORN4", "NONE", "keep", gen_dmdborn4),
                ColumnSpec("zip3", "NONE", "human_review", gen_qi_zip3,
                           edge_case_tag="quasi_identifier"),
                ColumnSpec("sex", "NONE", "human_review", gen_qi_sex,
                           edge_case_tag="quasi_identifier"),
                ColumnSpec("birth_year", "NONE", "human_review", gen_qi_birth_year,
                           edge_case_tag="quasi_identifier"),
                ColumnSpec("rare_dx", "NONE", "human_review", gen_qi_rare_dx,
                           edge_case_tag="quasi_identifier"),
                ColumnSpec("sud_dx_flag", "NONE", "drop", gen_sud_dx_flag,
                           sensitivity_class="42cfr2"),
            ),
        ),
    ),
    dictionary=(
        DictionaryRow("SEQN", "Respondent sequence number"),
        DictionaryRow("RIDAGEYR", "Age in years at screening, top-coded at 80", "int"),
        DictionaryRow("RIDRETH3", "Race/Hispanic origin recode"),
        DictionaryRow("DMDBORN4", "Country of birth recode"),
        DictionaryRow("zip3", "3-digit ZIP prefix of residence"),
        DictionaryRow("sex", "Sex"),
        DictionaryRow("birth_year", "Birth year", "int"),
        DictionaryRow("rare_dx", "Rare diagnosis code"),
        DictionaryRow("sud_dx_flag", "Substance use disorder diagnosis flag (42 CFR Part 2)"),
    ),
)

SCENARIOS[_L3_QI.id] = _L3_QI


def list_scenarios() -> list[dict[str, Any]]:
    return [
        {
            "id": s.id,
            "label": s.label,
            "jurisdictions": sorted(s.jurisdictions),
            "dataset_count": len(s.datasets),
            "column_count": sum(len(d.columns) for d in s.datasets),
        }
        for s in SCENARIOS.values()
    ]
