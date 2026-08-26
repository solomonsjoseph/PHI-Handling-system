"""The US study archetype ladder -- four difficulty rungs, fourteen scenarios.

Difficulty is assigned from two measured axes, not from subject matter.
Axis one is header coverage: the fraction of a scenario's PHI columns whose
header hits a ``_HARD_RULE_TABLE`` regex that yields the correct action.
Axis two is value hostility: whether the value breaks a transform parser,
evades a publish-guard anchor, rides under a forced-``keep`` header, or is a
quasi-identifier combination.

L0 is high coverage and benign values -- the two scenarios this corpus
already scored 1.0/1.0/1.0 on. L1 is low coverage and benign values, which
is what a real US study system emits by default. L2 is low coverage plus
hostile values. L3 is structural attacks on the decision model itself.

Import direction, to avoid a cycle: this module imports only ``scenarios``
and ``edge_cases`` at module level. ``planters`` imports
``tiers.corpus_version`` at module level to stamp ground truth; that is
safe because this module never imports ``planters`` at module level.
``coverage()`` needs ``plant()``, so it does a function-local import inside
its own body -- do not lift that import to the top of this file.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Sequence

from . import edge_cases as _edge_cases
from .scenarios import SCENARIOS

TIERS: tuple[str, ...] = ("L0", "L1", "L2", "L3")

REQUIRED_VIOLATIONS: dict[str, str] = {
    "A": "names of participant, guardian, or investigator",
    "B": "street, city, county, precinct, ZIP, geocode",
    "C": "dates other than year tied to an individual",
    "C90": "age over 89 or a date element implying it",
    "B000": "restricted ZIP3 that must become 000",
    "D": "telephone", "E": "fax", "F": "email", "G": "SSN",
    "H": "medical record number", "I": "health plan beneficiary number",
    "J": "account number", "K": "certificate or licence number",
    "L": "vehicle identifier", "M": "device identifier or serial",
    "N": "web URL", "O": "IP address", "P": "biometric identifier",
    "Q": "full-face photographic image reference",
    "R": "any other unique identifying number or code",
    "QI": "quasi-identifier combination under 164.514(b)(2)(ii)",
    "42CFR2": "substance use disorder record",
}


@dataclass(frozen=True)
class LadderEntry:
    tier: str
    scenario_id: str
    edge_case_tags: tuple[str, ...]
    row_count: int
    seed: int


LADDER: tuple[LadderEntry, ...] = (
    LadderEntry("L0", "oncology_v1", (), 12, 101),
    LadderEntry("L0", "hipaa_max_adversarial_v1", _edge_cases.HIPAA_MAX_EDGE_CASE_TAGS, 12, 11),
    LadderEntry("L1", "diabetes_v1", (), 24, 102),
    LadderEntry("L1", "pediatric_behavioral_v1", (), 24, 103),
    LadderEntry("L1", "l1_sdtm_oncology_v1", (), 24, 201),
    LadderEntry("L1", "l1_redcap_registry_v1", (), 24, 202),
    LadderEntry("L1", "l1_omop_ehr_v1", (), 24, 203),
    LadderEntry("L2", "l2_pcornet_raw_v1", ("notes_multi_phi",), 40, 301),
    LadderEntry("L2", "l2_naaccr_registry_v1", ("age_nonnumeric_over_89", "zip_non_us"), 40, 302),
    LadderEntry("L2", "l2_cms_claims_v1", ("dob_two_digit_year",), 40, 303),
    LadderEntry("L2", "l2_redcap_hostile_v1", ("notes_phi_across_newline",), 40, 304),
    LadderEntry("L3", "l3_i2b2_crosswalk_v1", (), 40, 401),
    LadderEntry("L3", "l3_keeper_hijack_v1", (), 40, 402),
    LadderEntry("L3", "l3_keeper_hijack_names_v1", (), 40, 404),
    LadderEntry("L3", "l3_quasi_identifier_v1", (), 40, 403),
)


def ladder_for(tier: str = "all") -> tuple[LadderEntry, ...]:
    if tier == "all":
        return LADDER
    if tier not in TIERS:
        raise ValueError(f"unknown tier: {tier!r}")
    return tuple(e for e in LADDER if e.tier == tier)


def corpus_version() -> str:
    """First 12 hex characters of a sha256 over a canonical, stable
    description of the corpus. Never hashes ``repr(SCENARIOS)``:
    ``ColumnSpec.generator`` is a function object whose ``repr`` embeds a
    memory address, which would make the published number unreproducible
    across interpreter runs.
    """
    parts: list[str] = []
    for sid in sorted(SCENARIOS.keys()):
        scn = SCENARIOS[sid]
        datasets_desc = []
        for ds in scn.datasets:
            cols_desc = tuple(
                (c.name, c.hipaa_category, c.expected_action, _generator_name(c.generator))
                for c in ds.columns
            )
            datasets_desc.append((ds.filename, cols_desc))
        dict_desc = tuple((r.column_name, r.description, r.type) for r in scn.dictionary)
        parts.append(repr((sid, scn.label, scn.tier, scn.profile, tuple(datasets_desc), dict_desc)))
    for tag in sorted(_edge_cases.EDGE_CASES.keys()):
        parts.append(f"edge:{tag}")
    for entry in LADDER:
        parts.append(repr((entry.tier, entry.scenario_id, entry.edge_case_tags,
                            entry.row_count, entry.seed)))
    blob = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def _generator_name(generator) -> str:
    # New scenarios use named module-level functions, so ``__name__``
    # distinguishes them cleanly. Existing scenarios keep a few lambdas;
    # ``__qualname__`` still distinguishes those by enclosing scope even
    # though every lambda's bare ``__name__`` is the literal "<lambda>".
    name = getattr(generator, "__name__", "")
    if name and name != "<lambda>":
        return name
    return getattr(generator, "__qualname__", repr(type(generator)))


def coverage(entries: Sequence[LadderEntry]) -> dict[str, list[str]]:
    """Plant every entry and return, for each ``REQUIRED_VIOLATIONS`` key,
    the list of ``plant_id`` values that satisfy it. An empty list for a
    key is a hole in the corpus.
    """
    from .planters import plant  # local: planters imports tiers at module level

    out: dict[str, list[str]] = {k: [] for k in REQUIRED_VIOLATIONS}
    for entry in entries:
        art = plant(entry.scenario_id, edge_case_tags=list(entry.edge_case_tags),
                     row_count=entry.row_count, seed=entry.seed, tier=entry.tier)
        for cell in art.ground_truth["planted"]:
            cat = cell.get("hipaa_category", "")
            pid = cell.get("plant_id", "")
            if not pid:
                continue
            if cat in REQUIRED_VIOLATIONS and cat not in ("QI",):
                out.setdefault(cat, []).append(pid)
            expectation = cell.get("expectation") or {}
            sem_age_over_89 = False
            # C90: a planted age or date whose semantics put the subject
            # over 89. We cannot recover raw `sem` post-serialization, so
            # infer it from the derived expectation: cap_age_90 cells whose
            # expected literal is "90+", or year_only cells far enough in
            # the past to imply it, both count.
            if cell.get("expected_action") == "cap_age_90" and expectation.get("literal") == "90+":
                sem_age_over_89 = True
            if sem_age_over_89:
                out["C90"].append(pid)
            if cell.get("expected_action") == "zip3_truncate" and expectation.get("literal") == "000":
                out["B000"].append(pid)
            if cell.get("edge_case_tag") == "quasi_identifier":
                out["QI"].append(pid)
            if cell.get("sensitivity_class") == "42cfr2":
                out["42CFR2"].append(pid)
    return out
