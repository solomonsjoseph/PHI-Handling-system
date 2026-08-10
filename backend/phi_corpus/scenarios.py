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


def gen_dob(rng: random.Random) -> str:
    year = rng.randint(1930, 2015)
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)
    return f"{year:04d}-{month:02d}-{day:02d}"


def gen_dob_over_89(rng: random.Random) -> str:
    """Edge case: birth year implies age > 89 today (dates indicative of
    such age per §164.514(b)(2)(i)(C)). Must be masked further."""
    year = rng.randint(1900, 1935)
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)
    return f"{year:04d}-{month:02d}-{day:02d}"


def gen_phone_us(rng: random.Random) -> str:
    return f"415-555-{rng.randint(1000, 9999):04d}"


def gen_email(rng: random.Random) -> str:
    return (
        f"{rng.choice(['jsmith', 'mjones', 'pwong', 'ppatel'])}"
        f".{rng.randint(1, 99)}@example.edu"
    )


def gen_zip5(rng: random.Random) -> str:
    return f"{rng.randint(10000, 99999):05d}"


def gen_zip5_restricted(rng: random.Random) -> str:
    """One of the 17 HIPAA-restricted ZIP3 prefixes plus 2-digit suffix."""
    prefixes = ["036", "059", "063", "102", "203", "556", "692", "790",
                "821", "823", "830", "831", "878", "879", "884", "890", "893"]
    return f"{rng.choice(prefixes)}{rng.randint(10, 99)}"


def gen_ssn(rng: random.Random) -> str:
    return f"{rng.randint(100, 999):03d}-{rng.randint(10, 99):02d}-{rng.randint(1000, 9999):04d}"


def gen_mrn(rng: random.Random) -> str:
    return f"MRN{rng.randint(100000, 999999)}"


def gen_age_under_90(rng: random.Random) -> str:
    return str(rng.randint(18, 89))


def gen_age_over_89(rng: random.Random) -> str:
    """Edge case: age must be aggregated per §164.514(b)(2)(i)(C)."""
    return str(rng.randint(90, 105))


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


def gen_notes_with_url(rng: random.Random) -> str:
    return f"Patient uploaded records via {gen_url(rng)} on 2024-05-20."


def gen_notes_with_device_serial(rng: random.Random) -> str:
    return f"CPAP device serial {gen_device_serial(rng)} configured at last visit."


def gen_notes_with_license(rng: random.Random) -> str:
    return f"Provider license {gen_license_number(rng)} verified before dispensing controlled substance."


def gen_notes_with_name(rng: random.Random) -> str:
    name = gen_name(rng)
    return f"Patient {name} enrolled on 2024-03-15; contact via phone."


def gen_notes_with_phone(rng: random.Random) -> str:
    ph = gen_phone_us(rng)
    return f"Contact at {ph} between 9am and 5pm PST."


def gen_notes_with_age_over_89(rng: random.Random) -> str:
    age = rng.randint(90, 102)
    return f"Elderly patient aged {age} presenting with chest pain."


def gen_notes_with_ipv4(rng: random.Random) -> str:
    return f"Telehealth session originated from {rng.randint(1,255)}.{rng.randint(0,255)}.{rng.randint(0,255)}.{rng.randint(1,254)}"


def gen_notes_with_email(rng: random.Random) -> str:
    return f"Discharge summary sent to {gen_email(rng)} at 14:32."


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
    """
    name: str
    hipaa_category: str
    expected_action: str
    generator: Callable[[random.Random], str]
    edge_case_tag: str = ""  # populated when a variant of this column is planted


@dataclass(frozen=True)
class DatasetSpec:
    filename: str
    columns: tuple[ColumnSpec, ...]
    n_rows: int = 8


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


# ---- Scenario library --------------------------------------------------

_ONCOLOGY = Scenario(
    id="oncology_v1",
    label="Oncology trial baseline enrollment",
    jurisdictions=frozenset({"us"}),
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
            n_rows=8,
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
            n_rows=8,
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


_PEDIATRIC = Scenario(
    id="pediatric_behavioral_v1",
    label="Pediatric behavioral study — screening",
    jurisdictions=frozenset({"us"}),
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
                ColumnSpec("child_age_years", "C", "cap_age_90", lambda r: str(r.randint(5, 17))),
                ColumnSpec("cbcl_total_score", "NONE", "keep", lambda r: str(r.randint(20, 90))),
                ColumnSpec("interview_notes", "NONE", "scrub_text", gen_notes_clinical_only),
            ),
            n_rows=8,
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
            n_rows=8,
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
