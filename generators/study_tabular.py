"""
Clinical-study tabular corpus generator -- synthetic, seeded, IRB-audit-ready.

Unlike every other generator in this package (which emits narrative
``text`` + ``gold_spans`` records for the span-annotated canonical corpus),
this module emits header-keyed TABULAR rows -- the exact shape
``phi_engine.security.phi_scrub._scrub_file`` reads (flat ``dict[str, str]``
per row, one JSONL file per CRF form). This is the "corpus based on the type
of information collected in a clinical study" the phi_engine scrub pipeline
is built to run against (Case Report Form exports), as opposed to narrative
free text.

Two generator classes, one per pinned rulebook jurisdiction:
  - :class:`IndiaStudyTabularGenerator` -- DPDPA 2023, DPDP Rules 2025 Rule 14,
    SPDI Rules 2011 Rule 3, ICMR 2017 s2.3.5.
  - :class:`USStudyTabularGenerator` -- 45 CFR 164.514(b)(2).

Column contract (binds to ``phi_engine/config/_defaults/phi_scrub.yaml``
default rules -- verified interactively 2026-07-07, see
``docs/JURISDICTION_EVIDENCE_REPORT_IN.md`` / ``_US.md`` "Column binding"):
  SUBJID        -> id_fields  (pattern ``^SUBJID$``, label SUBJ) -> pseudonymize
  IC_SCRNNUM    -> id_fields  (pattern ``^I[CS]_SCRNNUM$``, label SCRN) -> pseudonymize
  VISITDAT      -> date_fields (generic ``_?DAT\\d*$`` catch-all) -> jitter_date
  COLLDAT       -> date_fields (specific catalog entry) -> jitter_date
  TBTXDT        -> date_fields (specific catalog entry) -> jitter_date
  IS_BIRTHDAT   -> birthdate_field -> drop (safe_harbor posture, form has AGE)
  AGE           -> cap_fields (``^(?:[A-Z]{1,4}[-_])?AGE$``, threshold 89) -> cap
  SEX           -> no rule match -> published unchanged (kept clinical variable)
  WEIGHT        -> no rule match -> published unchanged (kept clinical variable)
  CBC_HGB       -> keep_fields (``^CBC_(?!INIT|SIGN)``) -> keep
  India: AADHAAR_NUM, PAN_NUM, MOBILE_NUM -> drop_fields (packaged defaults).
         ABHA_NUM -> NOT covered by packaged defaults; requires the per-study
         addition documented in Step 3.1 (ABDM HDMP 2020 identifier).
  USA:   SSN, MRN, PHONE_NUM, EMAIL -> drop_fields (packaged defaults; no
         per-study addition needed).

All output is fully synthetic. No real individual's data is used or implied.
Seed is required; unseeded random is never used (IRB reproducibility
requirement) -- see :class:`generators.common.DeterministicGenerator`.
"""
from __future__ import annotations

import datetime
import random
import string
import sys
from pathlib import Path
from typing import Dict, List

# Allow running as __main__ from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from generators.common import (
    AUTH_DPDPA_ACT,
    AUTH_DPDPA_RULES_14,
    AUTH_HIPAA_SAFE_HARBOR,
    AUTH_ICMR_CODING,
    AUTH_SPDI_RULE_3,
    DeterministicGenerator,
)

# ---------------------------------------------------------------------------
# CRF form filenames (fixed across both jurisdictions)
# ---------------------------------------------------------------------------

FORM_SCREENING = "1A_Screening.jsonl"
FORM_DEMOGRAPHICS = "2_Demographics.jsonl"
FORM_LABS = "3_Labs.jsonl"

# ---------------------------------------------------------------------------
# Column-level expected-action legend (shared baseline; see module docstring).
# Keyed by (form, column) -> expected_action. Jurisdiction identifier columns
# are appended per-subclass in ``_column_legend``.
# ---------------------------------------------------------------------------

_BASE_COLUMN_LEGEND: Dict[str, Dict[str, str]] = {
    FORM_SCREENING: {
        "SUBJID": "pseudonymize",
        "IC_SCRNNUM": "pseudonymize",
        "VISITDAT": "jitter_date",
        "SEX": "keep",
    },
    FORM_DEMOGRAPHICS: {
        "SUBJID": "pseudonymize",
        "IS_BIRTHDAT": "drop",
        "AGE": "cap",
        "WEIGHT": "keep",
    },
    FORM_LABS: {
        "SUBJID": "pseudonymize",
        "COLLDAT": "jitter_date",
        "TBTXDT": "jitter_date",
        "CBC_HGB": "keep",
    },
}

# Columns whose baseline action is a no-op passthrough (not tracked as gold
# PHI cells -- nothing to redact/transform, so they carry no leak-recall
# signal).
_KEEP_COLUMNS = {"SEX", "WEIGHT", "CBC_HGB"}


def _fmt_date(d: datetime.date) -> str:
    return d.isoformat()


def _rand_digits(rng: random.Random, n: int) -> str:
    return "".join(str(rng.randint(0, 9)) for _ in range(n))


def _rand_upper(rng: random.Random, n: int) -> str:
    return "".join(rng.choice(string.ascii_uppercase) for _ in range(n))


class _StudyTabularGeneratorBase(DeterministicGenerator):
    """Shared CRF-generation logic for both jurisdictions.

    Subclasses supply the jurisdiction-specific identifier columns (added to
    the Screening form) and authority citations; the common-column layout
    (SUBJID / IC_SCRNNUM / VISITDAT / IS_BIRTHDAT / AGE / dates / clinical
    kept-columns) and the three deliberate fail-closed edge cases (Step 2.3)
    are identical across jurisdictions.
    """

    jurisdiction: str = ""
    subj_prefix: str = ""

    def _identifier_columns(self, rng: random.Random) -> Dict[str, str]:
        """Return jurisdiction-specific identifier column values for one subject."""
        raise NotImplementedError

    def identifier_column_names(self) -> List[str]:
        """Column names added by :meth:`_identifier_columns` (drives the ledger)."""
        raise NotImplementedError

    def generate_study(self, n_subjects: int = 60) -> Dict[str, List[dict]]:
        """Generate the three CRF forms for *n_subjects* synthetic subjects.

        Returns ``{filename: [row, ...]}`` keyed by the fixed form filenames.
        Deliberate fail-closed edge cases (Step 2.3, planted at fixed subject
        indices for determinism):
          - subject index 0  -> AGE = 92           (cap path,        1 subject)
          - subject indices 1, 2 -> VISITDAT/COLLDAT = "INVALID-DATE"
                                     (unparseable-date blank path,   2 rows)
          - subject indices 3, 4 -> SUBJID blank in the Labs form only
                                     (orphan-row quarantine path,    2 rows)

        Call :meth:`gold_ledger` after this to retrieve the matching ledger.
        """
        if n_subjects < 5:
            raise ValueError("n_subjects must be >= 5 to fit the planted edge cases")

        screening: List[dict] = []
        demographics: List[dict] = []
        labs: List[dict] = []
        self._ledger: List[dict] = []

        base_visit = datetime.date(2024, 6, 1)

        for i in range(n_subjects):
            subj_rng = self.fresh(f"subject_{i}")
            subjid = f"{self.subj_prefix}-{i + 1:04d}"
            scrnnum = f"SCR-{9000 + i}"

            # -- visit timeline (per-subject, realistic CRF interval structure) --
            visit_offset = subj_rng.randint(0, 400)
            visit_date = base_visit + datetime.timedelta(days=visit_offset)
            coll_date = visit_date + datetime.timedelta(days=subj_rng.randint(0, 3))
            tbtx_date = coll_date + datetime.timedelta(days=subj_rng.randint(1, 14))

            # -- deliberate edge cases (Step 2.3) --
            age = subj_rng.randint(18, 85)
            visitdat_val = _fmt_date(visit_date)
            colldat_val = _fmt_date(coll_date)
            subjid_in_labs = subjid
            row_flags = {"age_cap": False, "unparseable_date": False, "orphan_labs": False}

            if i == 0:
                age = 92
                row_flags["age_cap"] = True
            elif i in (1, 2):
                visitdat_val = "INVALID-DATE"
                row_flags["unparseable_date"] = True
            elif i in (3, 4):
                subjid_in_labs = ""
                row_flags["orphan_labs"] = True

            sex = subj_rng.choice(["M", "F"])
            birth_year = visit_date.year - age
            birth_date = datetime.date(
                birth_year,
                subj_rng.randint(1, 12),
                subj_rng.randint(1, 28),
            )
            weight = str(subj_rng.randint(40, 95))
            hgb = f"{subj_rng.uniform(8.0, 16.5):.1f}"

            identifiers = self._identifier_columns(subj_rng)

            screening_row = {
                "SUBJID": subjid,
                "IC_SCRNNUM": scrnnum,
                "VISITDAT": visitdat_val,
                "SEX": sex,
                **identifiers,
            }
            demographics_row = {
                "SUBJID": subjid,
                "IS_BIRTHDAT": _fmt_date(birth_date),
                "AGE": str(age),
                "WEIGHT": weight,
            }
            labs_row = {
                "SUBJID": subjid_in_labs,
                "COLLDAT": colldat_val,
                "TBTXDT": _fmt_date(tbtx_date),
                "CBC_HGB": hgb,
            }

            screening.append(screening_row)
            demographics.append(demographics_row)
            labs.append(labs_row)

            self._plant_ledger_rows(
                row_index=i,
                screening_row=screening_row,
                demographics_row=demographics_row,
                labs_row=labs_row,
                identifiers=identifiers,
                flags=row_flags,
                raw_visitdat=_fmt_date(visit_date),
                raw_colldat=_fmt_date(coll_date),
                raw_subjid=subjid,
            )

        return {
            FORM_SCREENING: screening,
            FORM_DEMOGRAPHICS: demographics,
            FORM_LABS: labs,
        }

    # -- gold ledger -----------------------------------------------------

    def _column_legend(self) -> Dict[str, Dict[str, str]]:
        legend = {form: dict(cols) for form, cols in _BASE_COLUMN_LEGEND.items()}
        for name in self.identifier_column_names():
            legend[FORM_SCREENING][name] = "drop"
        return legend

    def _plant_ledger_rows(
        self,
        *,
        row_index: int,
        screening_row: dict,
        demographics_row: dict,
        labs_row: dict,
        identifiers: Dict[str, str],
        flags: Dict[str, bool],
        raw_visitdat: str,
        raw_colldat: str,
        raw_subjid: str,
    ) -> None:
        """Append per-PHI-cell ledger entries for one subject's three rows.

        Every cell whose column has a non-"keep" baseline action is a gold
        PHI cell. Planted edge-case cells carry an *overridden*
        ``expected_action`` ("blank" / "quarantine_row") instead of the
        column baseline, matching the actual runtime outcome the scrub
        engine produces for that specific cell (Step 2.3/3.2e).
        """
        legend = self._column_legend()

        def add(
            form: str,
            row: dict,
            ridx: int,
            overrides: Dict[str, str] | None = None,
            skip: set | None = None,
        ) -> None:
            overrides = overrides or {}
            skip = skip or set()
            for column, value in row.items():
                if column in _KEEP_COLUMNS or column in skip:
                    continue
                if column == "SUBJID" and value == "":
                    # Orphan row: SUBJID itself is blank, not a PHI value to
                    # track for redaction-recall (nothing to redact -- it was
                    # never populated).
                    continue
                action = overrides.get(column, legend[form][column])
                self._ledger.append(
                    {
                        "form": form,
                        "row_index": ridx,
                        "column": column,
                        "original_value": value,
                        "expected_action": action,
                    }
                )

        add(FORM_SCREENING, screening_row, row_index)
        # AGE's "cap" rule is threshold-conditional (HIPAA Safe Harbor
        # Sec.164.514(b)(2)(i)(C): only ages > 89 must be aggregated). For
        # the 89-and-under majority the scrub engine correctly leaves AGE
        # unchanged -- that is NOT a redaction failure, so only the one
        # actually-capped subject's AGE cell is tracked as a gold PHI cell;
        # everyone else's AGE is excluded from redaction-recall accounting
        # (same treatment as a "keep" column for those rows).
        demographics_skip = set() if flags["age_cap"] else {"AGE"}
        add(FORM_DEMOGRAPHICS, demographics_row, row_index, skip=demographics_skip)

        if flags["orphan_labs"]:
            # The whole Labs row is quarantined (no resolvable subject id) --
            # every populated cell on it is held, never published.
            overrides = {c: "quarantine_row" for c in labs_row if c != "SUBJID"}
            add(FORM_LABS, labs_row, row_index, overrides=overrides)
        else:
            add(FORM_LABS, labs_row, row_index)

        if flags["unparseable_date"]:
            # VISITDAT already ledgered above via the Screening add() call
            # with the overridden raw value "INVALID-DATE"; here we correct
            # its expected_action to "blank" (unparseable_date_policy) rather
            # than the column baseline "jitter_date".
            for entry in self._ledger:
                if (
                    entry["form"] == FORM_SCREENING
                    and entry["row_index"] == row_index
                    and entry["column"] == "VISITDAT"
                ):
                    entry["expected_action"] = "blank"

    def gold_ledger(self) -> List[dict]:
        """Return the full gold ledger for the most recent :meth:`generate_study` call.

        Two line kinds, per Step 2.4:
          - column legend:  ``{"form", "column", "expected_action"}``
          - PHI cell:       ``{"form", "row_index", "column", "original_value",
                              "expected_action"}`` (superset of the minimal
                              schema -- ``expected_action`` is included on
                              every cell line so the driver can bucket
                              redaction-recall counts, including the planted
                              "blank" / "quarantine_row" overrides, without a
                              second lookup against the column legend).
        """
        if not hasattr(self, "_ledger"):
            raise RuntimeError("gold_ledger() called before generate_study()")
        lines: List[dict] = []
        for form, columns in self._column_legend().items():
            for column, action in columns.items():
                lines.append({"form": form, "column": column, "expected_action": action})
        lines.extend(self._ledger)
        return lines


class IndiaStudyTabularGenerator(_StudyTabularGeneratorBase):
    """Synthetic India clinical-study CRF tabular generator.

    Authority: DPDPA 2023 (identifier categories), DPDP Rules 2025 Rule 14
    (identifier categories + data principal rights), SPDI Rules 2011 Rule 3
    (sensitive personal data), ICMR 2017 Section 2.3.5 (coding of research
    identifiers).
    """

    jurisdiction = "INDIA"
    subj_prefix = "IN"

    AUTHORITY_CITATIONS = [
        AUTH_DPDPA_ACT,
        AUTH_DPDPA_RULES_14,
        AUTH_SPDI_RULE_3,
        AUTH_ICMR_CODING,
    ]

    def identifier_column_names(self) -> List[str]:
        return ["AADHAAR_NUM", "ABHA_NUM", "PAN_NUM", "MOBILE_NUM"]

    def _identifier_columns(self, rng: random.Random) -> Dict[str, str]:
        aadhaar = _rand_digits(rng, 12)
        abha = _rand_digits(rng, 14)
        pan = f"{_rand_upper(rng, 5)}{_rand_digits(rng, 4)}{_rand_upper(rng, 1)}"
        mobile = str(rng.randint(6, 9)) + _rand_digits(rng, 9)
        return {
            "AADHAAR_NUM": aadhaar,
            "ABHA_NUM": abha,
            "PAN_NUM": pan,
            "MOBILE_NUM": mobile,
        }


class USStudyTabularGenerator(_StudyTabularGeneratorBase):
    """Synthetic USA clinical-study CRF tabular generator.

    Authority: 45 CFR 164.514(b)(2) (HIPAA Safe Harbor de-identification
    standard).
    """

    jurisdiction = "USA"
    subj_prefix = "US"

    AUTHORITY_CITATIONS = [AUTH_HIPAA_SAFE_HARBOR]

    def identifier_column_names(self) -> List[str]:
        return ["SSN", "MRN", "PHONE_NUM", "EMAIL"]

    def _identifier_columns(self, rng: random.Random) -> Dict[str, str]:
        ssn = f"{rng.randint(100, 899):03d}-{rng.randint(10, 99):02d}-{rng.randint(1000, 9999):04d}"
        mrn = f"MRN-{rng.randint(10_000_000, 99_999_999)}"
        phone = f"{rng.randint(200, 989)}-{rng.randint(200, 999)}-{rng.randint(0, 9999):04d}"
        email = f"subject{rng.randint(1000, 9999)}@example-clinicalstudy.org"
        return {
            "SSN": ssn,
            "MRN": mrn,
            "PHONE_NUM": phone,
            "EMAIL": email,
        }


__all__ = [
    "FORM_SCREENING",
    "FORM_DEMOGRAPHICS",
    "FORM_LABS",
    "IndiaStudyTabularGenerator",
    "USStudyTabularGenerator",
]


def _write_evidence_corpus(out_dir: Path, seed: int = 42, n_subjects: int = 60) -> None:
    """CLI helper: write both jurisdictions' output as registry evidence artifacts
    (Step 2.6). Deliberately OUTSIDE corpus/: validators.common.corpus_files()
    recursively globs every corpus/**/*.jsonl with no manifest scoping, so any
    file placed under corpus/ is swept into the narrative-record schema
    validators (BAD_SCHEMA / MISSING_AUTHORITY) regardless of intent -- verified
    empirically 2026-07-07. NOT part of the span-annotated canonical corpus /
    seeded_generator_specs() -- see module docstring."""
    import json

    for jurisdiction, cls in (("in", IndiaStudyTabularGenerator), ("us", USStudyTabularGenerator)):
        gen = cls(seed)
        forms = gen.generate_study(n_subjects)
        jdir = out_dir / jurisdiction
        jdir.mkdir(parents=True, exist_ok=True)
        for filename, rows in forms.items():
            with (jdir / filename).open("w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row, sort_keys=True) + "\n")
        with (jdir / "gold_ledger.jsonl").open("w", encoding="utf-8") as fh:
            for entry in gen.gold_ledger():
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
        print(f"{jurisdiction}: wrote {sum(len(v) for v in forms.values())} rows -> {jdir}")


if __name__ == "__main__":
    _write_evidence_corpus(
        Path(__file__).resolve().parents[1] / "benchmarks" / "results" / "study_tabular_corpus"
    )
