"""Phase 16 evaluation 1/9: Schema interpretation.

Schema (``phi_core.agents.specialists.Schema``) is a code-writing agent
(step 10): it calls an LLM to generate the extraction module that reads
a dataset's headers, but the actual per-header semantic judgment is
still entirely the deterministic HEADER SAFETY GATE, ``phi_core.control.
source_projection.classify_header`` -- applied unchanged, verbatim, to
whatever raw header name the generated extraction reports back. Does
the header TEXT itself constitute or embed a real identifier value
someone typed into a column name by mistake ("sensitive"), an ambiguous
unclassified digit run ("uncertain"), or neither ("safe")? That
disposition is Schema's actual per-header semantic judgment, exercised
here with zero stubbing of ``classify_header`` itself -- Schema's real
gate code, unedited, run end-to-end through a real ``AgentContext``.
The extraction seam (``_extract_via_codegen``, the LLM/Docker-touching
part) is stubbed below to keep this evaluation file offline and fast;
the gate it feeds is not.

Ground truth: 21 synthetic headers hand-labeled with the disposition the
HEADER SAFETY GATE's own documented rules (source_projection.py's
``classify_header`` docstring) say is correct: an embedded SSN/phone/email
shape is "sensitive"; an unclassified 3-9 digit run with no stricter match
is "uncertain"; an ordinary clinical/study column name is "safe".
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest
from phi_core.agents.specialists import Schema
from phi_core.control.source_projection import classify_header
from phi_core.control.testing import MemoryTrace, make_ctx
from phi_core.evaluations.scoring import accuracy, per_label_scores

# (header, correct disposition) -- the label a human reviewer applying
# classify_header's own documented rule would assign.
LABELED_HEADERS: list[tuple[str, str]] = [
    # safe: ordinary clinical/study column names, no embedded value, no
    # ambiguous digit run.
    ("diagnosis_code", "safe"),
    ("study_arm", "safe"),
    ("heart_rate_bpm", "safe"),
    ("hemoglobin", "safe"),
    ("treatment_outcome", "safe"),
    ("sex_at_birth", "safe"),
    ("visit_number", "safe"),
    ("comments", "safe"),
    # sensitive: the header text itself embeds a real identifier shape a
    # study team typed in by mistake (SSN / phone / email patterns the
    # rule-based detector matches). The digit run must sit behind a
    # non-word-character boundary (a hyphen or "@", not an underscore --
    # Python's \b treats "_" as a word character, so an underscore-glued
    # digit run never trips the strict SSN/phone regex; that combination
    # is exercised deliberately below under "uncertain").
    ("ssn-078-05-1120", "sensitive"),
    ("contact_jane.doe@example.com", "sensitive"),
    ("callback-555-201-3456", "sensitive"),
    ("subject-ssn-219-09-9999", "sensitive"),
    ("reachable_at_research.coordinator@studysite.org", "sensitive"),
    # uncertain: an embedded 3-9 digit run that is not itself an SSN/phone/
    # email shape -- ambiguous per classify_header's own docstring example
    # ("site_02139", "id_1234").
    ("site_02139", "uncertain"),
    ("id_1234", "uncertain"),
    ("code_555123", "uncertain"),
    ("ssn_078_05_1120", "uncertain"),  # underscore-glued SSN shape: strict \b never fires, falls to the digit-run rule
    ("cohort_4471", "uncertain"),
    ("form_version_20240501", "uncertain"),
    ("lab_slot_777", "uncertain"),
    ("sequence_12", "safe"),  # a 2-digit run is below the 3-9 digit ambiguous-run floor
]

_LABELS = ("safe", "uncertain", "sensitive")


def _write_csv(tmp_path: Path, headers: list[str]) -> Path:
    path = tmp_path / "dataset.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers)
        writer.writerow(["x"] * len(headers))
    return path


def test_classify_header_matches_the_label_for_every_synthetic_header():
    """Direct measurement of Schema's real per-header classifier
    (``classify_header``, the exact function ``Schema.run`` calls) against
    the hand-labeled ground truth: the primary Schema-interpretation
    accuracy figure for this phase."""
    pairs: list[tuple[str, str]] = []
    for header, label in LABELED_HEADERS:
        disposition, _reasons = classify_header(header)
        pairs.append((disposition, label))
    overall = accuracy(pairs)
    per_label = per_label_scores(pairs, _LABELS)
    print(f"\n[Phase16][schema] overall header-disposition accuracy: {overall} "
          f"over {len(pairs)} labeled headers")
    for label in _LABELS:
        s = per_label[label]
        print(f"[Phase16][schema] {label}: precision={s.precision} recall={s.recall} "
              f"f1={s.f1} support={s.support}")
    assert overall == 1.0, (
        "Schema's real classify_header disagreed with the hand-labeled "
        f"disposition on {sum(1 for p, label in pairs if p != label)}/{len(pairs)} "
        f"header(s): {[h for (h, label), (p, _) in zip(LABELED_HEADERS, pairs, strict=True) if p != label]}"
    )


@pytest.mark.asyncio
async def test_schema_run_end_to_end_propagates_the_same_dispositions(tmp_path):
    """The same labeled headers run through the real, unstubbed header
    safety gate inside ``Schema.run()`` (temp CSV -> real AgentContext
    -> real gate): a "safe" header's literal name must survive into
    Schema's output columns; a "sensitive"/"uncertain" header must
    never appear under its literal text, only as an opaque token --
    proving Schema's per-header classification is not merely correct in
    isolation but is the one actually enforced end-to-end. Only the
    codegen/LLM/Docker extraction seam is stubbed, reading the real
    temp CSV this test wrote via a plain csv reader in place of a
    generated extraction module."""
    from phi_core.agents.extract_model import ExtractedColumn, ExtractedSchema

    class _FileReadingSchema(Schema):
        async def _extract_via_codegen(self, f, sandbox):
            with open(f["stored_path"], newline="", encoding="utf-8") as fh:
                headers = next(csv.reader(fh))
            columns = [
                ExtractedColumn(name=h, position=i, distinct_count=2, null_count=0, inferred_type="string")
                for i, h in enumerate(headers)
            ]
            return ExtractedSchema(columns=columns, row_count=10)

    headers = [h for h, _ in LABELED_HEADERS]
    path = _write_csv(tmp_path, headers)
    trace = MemoryTrace()
    ctx = make_ctx("Schema", trace=trace)
    schema = _FileReadingSchema(ctx)
    result = await schema.run([{"file_id": "f1", "stored_path": str(path)}])
    output_names = {c["name"] for c in result["columns"]}

    mismatches: list[str] = []
    for header, label in LABELED_HEADERS:
        literal_present = header.lower() in output_names
        if label == "safe" and not literal_present:
            mismatches.append(f"{header}: expected literal pass-through, got an opaque token")
        if label in ("sensitive", "uncertain") and literal_present:
            mismatches.append(f"{header}: expected opaque token, literal text leaked instead")
    print(f"\n[Phase16][schema] end-to-end propagation mismatches: {len(mismatches)}/{len(LABELED_HEADERS)}")
    assert not mismatches, "; ".join(mismatches)
