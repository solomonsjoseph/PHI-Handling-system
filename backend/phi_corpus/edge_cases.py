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

from dataclasses import dataclass
from typing import Callable
import random

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
}


# Convenience preset: every edge case that targets a column present in the
# hipaa_max_adversarial_v1 scenario. Used by the "adversarial" preset in
# the corpus catalog UI.
HIPAA_MAX_EDGE_CASE_TAGS: tuple[str, ...] = (
    "age_over_89",
    "dob_indicative_of_age_over_89",
    "restricted_zip3",
    "notes_carry_name",
    "notes_carry_phone",
    "notes_carry_age_over_89",
    "notes_carry_ipv4",
    "notes_carry_email",
    "notes_carry_url",
    "notes_carry_device_serial",
    "notes_carry_license",
    "clinical_hr_90s",
)


def all_tags() -> list[str]:
    return list(EDGE_CASES.keys())


def get(tag: str) -> EdgeCase:
    return EDGE_CASES[tag]
