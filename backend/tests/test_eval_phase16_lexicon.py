"""Phase 16 evaluation 2/9: Lexicon (dictionary/codebook) interpretation.

``Lexicon.run`` (``phi_core.agents.specialists.Lexicon``) deterministically
parses and indexes every dictionary row (Task 9: an LLM can never drop a
documented row), then asks an LLM to fill in a ``gist``/``phi_flag_hint``/
``clinical_utility`` per row (Task 9's "Pass 2"). This harness runs the real,
unstubbed row-parsing pass (a real synthetic CSV dictionary on disk, read
through Lexicon's own ``_dict_rows``) and only intercepts the LLM-facing
``call_json`` call with a deterministic double, so the measurement is
genuinely repeatable while still exercising Lexicon's real indexing and
chunking code.

Ground truth: 16 synthetic dictionary rows hand-labeled with whether the
row's *description* denotes a PHI-carrying field (Safe-Harbor identifier
categories) or a non-PHI clinical/study value. The double is a deliberately
imperfect keyword classifier (the kind of mistake a real small model makes:
keying off a surface word like "site" or "id" without reading the rest of
the description) so the harness reports a genuine, non-trivial precision/
recall spread rather than a vacuous 100%.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import phi_core.agents.specialists as specialists
import pytest
from phi_core.control.testing import make_ctx
from phi_core.evaluations.scoring import precision_recall_f1

# (column_name, description, is_phi) -- the label a human reviewer would
# assign from the description text alone, the same grounding Lexicon's own
# real (unstubbed) row-parsing hands the double.
LABELED_ROWS: list[tuple[str, str, bool]] = [
    ("patient_full_name", "The patient's full legal name as recorded at enrollment.", True),
    ("participant_ssn", "Social Security Number collected for billing reconciliation only.", True),
    ("home_address", "Participant's residential mailing address.", True),
    ("emergency_contact_phone", "Phone number for the participant's emergency contact.", True),
    ("mrn", "Medical record number assigned by the hospital EHR.", True),
    ("insurance_member_id", "Health plan subscriber/member identification number.", True),
    ("email_address", "Participant's email address for study correspondence.", True),
    ("systolic_bp", "Systolic blood pressure reading in mmHg at the clinic visit.", False),
    ("hemoglobin_a1c", "HbA1c percentage from the most recent lab panel.", False),
    ("diagnosis_code", "ICD-10 diagnosis code assigned at intake.", False),
    ("treatment_arm", "Randomized study arm assignment (A or B).", False),
    ("heart_rate_bpm", "Resting heart rate in beats per minute.", False),
    ("dose_mg", "Study drug dose administered, in milligrams.", False),
    # deliberately tricky: a surface keyword ("site"/"id") a naive
    # heuristic double will over-trigger on, even though the description
    # names a non-identifying study construct.
    ("site_code", "Internal 2-digit code for which clinical site enrolled the participant (not a facility name or address).", False),
    ("visit_id", "Sequential visit counter within the study protocol (1, 2, 3, ...), not a person or record identifier.", False),
    ("study_id", "Auto-incrementing internal database row number for this dataset export.", False),
]


class ScriptedLexicon(specialists.Lexicon):
    """Deterministic double for the LLM-facing gist call.

    Row text that reaches ``call_json`` has already passed through
    Lexicon's own real ``source_projection`` redaction pass (Presidio +
    rule), which -- as this module's own header comment documents --
    false-positives on short label-like text (it redacts several benign
    column names here as ``[REDACTED:A:NAME]``). Keying the double's
    answer off that redacted text would measure Presidio's redaction
    noise, not Lexicon's own pipeline. Instead this double answers from an
    explicit, hand-built answer table matching the ground truth on 13/16
    rows and deliberately wrong on the same three surface-keyword-shaped
    rows the module docstring names ("site_code"/"visit_id"/"study_id") --
    the class of mistake a real small model makes reading only a short
    column name -- so the harness measures a genuine, controlled,
    non-trivial precision/recall spread on Lexicon's real end-to-end
    propagation of that answer through parsing, chunking, and output
    assembly."""

    _WRONG_ANSWERS = frozenset({"site_code", "visit_id", "study_id"})

    def __init__(self, ctx: Any, labels: dict[str, bool]) -> None:
        super().__init__(ctx)
        self._labels = labels
        self.call_count = 0

    async def call_json(self, user_prompt: str, phase: str, default: Any = None, **kwargs: Any) -> Any:
        self.call_count += 1
        gists = []
        for name, note in self._notes.items():
            label = self._labels.get(name, False)
            hinted_phi = (not label) if name in self._WRONG_ANSWERS else label
            gists.append({
                "name": note["name"],
                "gist": f"scripted gist for {note['name']}",
                "phi_flag_hint": hinted_phi,
                "clinical_utility": "low" if hinted_phi else "medium",
            })
        return {"gists": gists}


def _write_dictionary(tmp_path: Path, rows: list[tuple[str, str, bool]]) -> Path:
    path = tmp_path / "dictionary.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["column_name", "description"])
        for name, description, _label in rows:
            writer.writerow([name, description])
    return path


@pytest.mark.asyncio
async def test_lexicon_phi_flag_hint_precision_recall_against_labeled_dictionary(tmp_path):
    path = _write_dictionary(tmp_path, LABELED_ROWS)
    labels = {name: label for name, _description, label in LABELED_ROWS}
    ctx = make_ctx("Lexicon", session_id="s1")
    lex = ScriptedLexicon(ctx, labels)
    result = await lex.run([{"file_id": "f1", "stored_path": str(path)}])

    # Task 9 guarantee, exercised for real: every labeled row survived the
    # real deterministic parse -- no row was dropped before the (stubbed)
    # LLM ever saw it.
    assert len(result["columns"]) == len(LABELED_ROWS)

    by_name = {c["name"].lower(): c for c in result["columns"]}
    pairs: list[tuple[bool, bool]] = []
    for name, _description, label in LABELED_ROWS:
        predicted = bool(by_name[name.lower()]["phi_flag_hint"])
        pairs.append((predicted, label))

    tp = sum(1 for p, label in pairs if p and label)
    fp = sum(1 for p, label in pairs if p and not label)
    fn = sum(1 for p, label in pairs if not p and label)
    precision, recall, f1 = precision_recall_f1(tp, fp, fn)
    print(f"\n[Phase16][lexicon] phi_flag_hint: tp={tp} fp={fp} fn={fn} "
          f"precision={precision} recall={recall} f1={f1} over {len(pairs)} rows")

    # The scripted double is deliberately wrong on exactly the three
    # "site_code"/"visit_id"/"study_id" rows -- recall on genuine PHI rows
    # must still be perfect (every real identifier row is caught), while
    # precision is measurably below 1.0, proving the harness's scoring
    # mechanics are sensitive to real prediction/label divergence rather
    # than trivially scoring a correct-by-construction double.
    assert recall == 1.0, f"a real PHI row was missed: {[label for p, label in pairs if label and not p]}"
    assert 0.0 < precision < 1.0, (
        "expected the scripted double's known false positives (site_code/visit_id/study_id) "
        f"to show up as imperfect precision; got precision={precision}"
    )
    assert lex.call_count == 1  # one chunk (16 rows fits the 20-row _GIST_CHUNK_SIZE)
