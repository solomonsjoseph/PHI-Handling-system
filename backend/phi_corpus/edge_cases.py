"""Edge case library — deliberate torture tests.

Each edge case is a **column-level rewrite** applied on top of a base
scenario. The planter runs the edge case's ``mutate`` function during
row generation to swap the default cell value for a variant that
exercises a specific pipeline weak-spot.

The current pipeline weak-spots we want to torture:

* AGE_OVER_89 guard false-positive on clinical 90-99 (regression from CR-HIGH)
* HIPAA-restricted ZIP3 prefixes (17 low-population)
* Full DOB embedded in a screening_date column (Judge must still detect)
* Name / phone leaking into free-text notes (Scout / Scrubber)
* Long numeric barcode overlapping IMEI shape (LICENSE_PLATE / IMEI guard)
* Study arm codes overlapping license plate shape (LICENSE_PLATE guard)
* International national IDs (Aadhaar / PAN / SIN / CPF for future jurisdictions)
* Ages that CAN be raw (89) vs those that CANNOT (90+)
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable

from . import realism as _realism
from . import scenarios as _S


@dataclass(frozen=True)
class EdgeCase:
    """One torture test."""
    tag: str
    label: str
    applies_to_column: str
    mutate: Callable[[random.Random], str]
    # Whether the mutated cell is EXPECTED to still be caught / handled
    # correctly by the pipeline. Almost always True (that's the point).
    should_be_handled: bool = True
    # Optional override of the column's expected_action when the mutation
    # implies a stricter transform (e.g. name-in-notes must be scrubbed
    # even though the notes column normally rides on scrub_text already).
    override_expected_action: str = ""

_AGE_NONNUMERIC_POOL: tuple[tuple[str, int], ...] = (
    ("ninety-two", 92),
    (">89", 92),
    ("92 yrs", 92),
    ("90+", 90),
    ("nonagenarian", 91),
)


def _mutate_age_nonnumeric_over_89(rng: random.Random) -> tuple[str, dict]:
    value, age = rng.choice(_AGE_NONNUMERIC_POOL)
    return value, {"age": age}


def _mutate_dob_two_digit_year(rng: random.Random) -> tuple[str, dict]:
    year = rng.randint(1930, 2015)
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)
    value = _realism.render_date(year, month, day, "us_short")
    return value, {"year": year}


_NON_US_POSTCODES: tuple[str, ...] = ("K1A 0B1", "SW1A 1AA", "2000 NSW")


def _mutate_zip_non_us(rng: random.Random) -> tuple[str, dict]:
    return rng.choice(_NON_US_POSTCODES), {"non_us": True}


def _mutate_notes_multi_phi(rng: random.Random) -> tuple[str, dict]:
    name = _S.gen_name(rng)
    phone = _S.gen_phone_us(rng)
    email = _S.gen_email(rng)
    value = f"Payer follow-up for {name}, call {phone} or email {email} to confirm eligibility."
    return value, {"literals": (name, phone, email)}


def _mutate_notes_phi_across_newline(rng: random.Random) -> tuple[str, dict]:
    name = _S.gen_name(rng)
    parts = name.split(" ", 1)
    first_part = parts[0]
    last_part = parts[1] if len(parts) > 1 else ""
    value = (f'Patient presented with, "acute" symptoms.\r\n'
             f"Contact name: {first_part}\n{last_part} confirmed follow-up.")
    # The rendered text splits the name across a bare newline, not a space,
    # so the full space-joined `name` never appears verbatim -- register
    # each half separately; that is what actually needs to be absent from
    # the scrubbed export.
    literals = tuple(p for p in (first_part, last_part) if p)
    return value, {"literals": literals, "clinical_fragment": "acute"}



EDGE_CASES: dict[str, EdgeCase] = {
    # ---- HIPAA §164.514(b)(2)(i)(C) age edge cases ---------------------
    "age_over_89": EdgeCase(
        tag="age_over_89",
        label="Age > 89 in an age column (must aggregate to 90+)",
        applies_to_column="age",
        mutate=_S.gen_age_over_89,
    ),
    "age_over_89_diabetes": EdgeCase(
        tag="age_over_89_diabetes",
        label="Age > 89 in the diabetes cohort's age_years column",
        applies_to_column="age_years",
        mutate=_S.gen_age_over_89,
    ),
    "dob_indicative_of_age_over_89": EdgeCase(
        tag="dob_indicative_of_age_over_89",
        label="Birth year that implies age > 89 (year mask insufficient)",
        applies_to_column="dob",
        mutate=_S.gen_dob_over_89,
    ),

    # ---- HIPAA §164.514(b)(2)(i)(B) restricted ZIP3 --------------------
    "restricted_zip3": EdgeCase(
        tag="restricted_zip3",
        label="ZIP whose first 3 digits are in the 17-prefix denylist",
        applies_to_column="zip",
        mutate=_S.gen_zip5_restricted,
    ),

    # ---- Free-text PHI leakage ----------------------------------------
    "notes_carry_name": EdgeCase(
        tag="notes_carry_name",
        label="Patient name embedded in the free-text notes column",
        applies_to_column="notes",
        mutate=_S.gen_notes_with_name,
    ),
    "notes_carry_phone": EdgeCase(
        tag="notes_carry_phone",
        label="Phone number embedded in the free-text notes column",
        applies_to_column="notes",
        mutate=_S.gen_notes_with_phone,
    ),
    "notes_carry_age_over_89": EdgeCase(
        tag="notes_carry_age_over_89",
        label="Age > 89 embedded in the free-text notes column",
        applies_to_column="notes",
        mutate=_S.gen_notes_with_age_over_89,
    ),
    "notes_carry_ipv4": EdgeCase(
        tag="notes_carry_ipv4",
        label="IPv4 (HIPAA cat O) embedded in the free-text notes column",
        applies_to_column="notes",
        mutate=_S.gen_notes_with_ipv4,
    ),
    "notes_carry_email": EdgeCase(
        tag="notes_carry_email",
        label="Email embedded in the free-text notes column",
        applies_to_column="notes",
        mutate=_S.gen_notes_with_email,
    ),

    # ---- Guard-shape torture tests (these MUST NOT trigger the guard) --
    "clinical_hr_90s": EdgeCase(
        tag="clinical_hr_90s",
        label="Heart rate 90-99 (clinical, not an age — must NOT be flagged)",
        applies_to_column="heart_rate_bpm",
        mutate=lambda r: str(r.randint(90, 99)),
        # This is a defensive test: the value SHOULD survive to the export
        # unchanged, so the "expected_action" is `keep` (already default).
    ),

    # ---- Notes carrying (K/M/N) identifiers --------------------------
    "notes_carry_url": EdgeCase(
        tag="notes_carry_url",
        label="Personal URL (HIPAA cat N) embedded in the notes column",
        applies_to_column="notes",
        mutate=_S.gen_notes_with_url,
    ),
    "notes_carry_device_serial": EdgeCase(
        tag="notes_carry_device_serial",
        label="Device serial (HIPAA cat M) embedded in the notes column",
        applies_to_column="notes",
        mutate=_S.gen_notes_with_device_serial,
    ),
    "notes_carry_license": EdgeCase(
        tag="notes_carry_license",
        label="Provider licence (HIPAA cat K) embedded in the notes column",
        applies_to_column="notes",
        mutate=_S.gen_notes_with_license,
    ),

    # ---- New adversarial value shapes (US ladder L2/L3) ----------------
    #
    # Each `applies_to_column` is bound to the ACTUAL header the target
    # scenario uses, not a generic placeholder name, so the ladder table's
    # scenario -> edge_case_tags assignment is functional.
    "age_nonnumeric_over_89": EdgeCase(
        tag="age_nonnumeric_over_89",
        label="Non-numeric age > 89 (word form, inequality, unit suffix)",
        applies_to_column="Age at Diagnosis",
        mutate=lambda r: _mutate_age_nonnumeric_over_89(r),
    ),
    "dob_two_digit_year": EdgeCase(
        tag="dob_two_digit_year",
        label="Birth date rendered with a 2-digit year (MM/DD/YY)",
        applies_to_column="BENE_BIRTH_DT",
        mutate=lambda r: _mutate_dob_two_digit_year(r),
    ),
    "zip_non_us": EdgeCase(
        tag="zip_non_us",
        label="Foreign postcode in a ZIP column (must not fabricate a US ZIP3)",
        applies_to_column="Addr at DX--Postal Code",
        mutate=lambda r: _mutate_zip_non_us(r),
    ),
    "notes_multi_phi": EdgeCase(
        tag="notes_multi_phi",
        label="Three PHI categories (name, phone, email) in one free-text cell",
        applies_to_column="RAW_PAYER_NAME_PRIMARY",
        override_expected_action="scrub_text",
        mutate=lambda r: _mutate_notes_multi_phi(r),
    ),
    "notes_phi_across_newline": EdgeCase(
        tag="notes_phi_across_newline",
        label="Patient name split across an embedded CRLF, plus quote hazards",
        applies_to_column="comments_text",
        override_expected_action="scrub_text",
        mutate=lambda r: _mutate_notes_phi_across_newline(r),
    ),
    "notes_prompt_injection": EdgeCase(
        tag="notes_prompt_injection",
        label="Prompt-injection instruction smuggled inside free-text notes",
        applies_to_column="PATIENT_BLOB",
        override_expected_action="scrub_text",
        mutate=_S.gen_notes_with_injection,
    ),
}


# Convenience preset: every edge case that targets a column present in the
# hipaa_max_adversarial_v1 scenario, EXCEPT the 7 other "notes_carry_*"
# free-text variants -- all 8 target the scenario's single "notes" column,
# and a column holds one value per row, so only one can apply per planting
# (see the collision guard in planters._generate_dataset_matrix). The other
# 7 stay individually selectable via EDGE_CASES / the corpus catalog UI.
# Used by the "adversarial" preset in the corpus catalog UI.
HIPAA_MAX_EDGE_CASE_TAGS: tuple[str, ...] = (
    "age_over_89",
    "dob_indicative_of_age_over_89",
    "restricted_zip3",
    "notes_carry_name",
    "clinical_hr_90s",
)


# Which edge case tags exercise which ladder tier's characteristic value
# hostility. Informational, used by report/UI code; does not gate planting.
TIER_EDGE_CASE_TAGS: dict[str, tuple[str, ...]] = {
    "L0": HIPAA_MAX_EDGE_CASE_TAGS,
    "L1": (),
    "L2": ("age_nonnumeric_over_89", "zip_non_us", "dob_two_digit_year",
           "notes_multi_phi", "notes_phi_across_newline"),
    "L3": ("notes_prompt_injection",),
}


def all_tags() -> list[str]:
    return list(EDGE_CASES.keys())


def get(tag: str) -> EdgeCase:
    return EDGE_CASES[tag]
