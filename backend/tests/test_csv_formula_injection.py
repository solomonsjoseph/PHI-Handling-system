"""4.18: exported cells beginning with =, +, -, @, tab, or CR must not
survive as live spreadsheet formulas, and the verifier must still score
the escaped value as correctly preserved."""
from __future__ import annotations

import csv
from pathlib import Path

from phi_core.agents.reasoning import apply_column_actions_to_dataset, PseudonymRegistry
from phi_corpus.verify import score_cells


def test_formula_shaped_keep_value_escaped_on_write_and_still_scored_preserved(tmp_path):
    src = tmp_path / "src.csv"
    dst = tmp_path / "dst.csv"
    with src.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["patient_id", "notes_formula"])
        w.writerow(["P001", "=cmd()"])

    decisions = [
        {"file_id": "src.csv", "column": "patient_id", "action": "drop"},
        {"file_id": "src.csv", "column": "notes_formula", "action": "keep"},
    ]
    apply_column_actions_to_dataset(src, dst, "csv", decisions, registry=PseudonymRegistry(salt="t"))

    with dst.open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows[1][1] == "'=cmd()", f"formula not escaped on write: {rows[1][1]!r}"

    ground_truth = {
        "planted": [
            {
                "file_name": "src.csv",
                "row": 1,
                "column": "notes_formula",
                "value": "=cmd()",
                "hipaa_category": "NONE",
                "expected_action": "keep",
                "plant_id": "p0001",
                "row": 2,
                "expectation": {
                    "kind": "literal",
                    "literal": "=cmd()",
                    "survives_verbatim": True,
                },
            },
        ],
    }
    result = score_cells(ground_truth, {"src.csv": str(dst)})
    assert result["preserved"] == 1, result
    assert result["destroyed"] == 0, result


def test_neutralise_formula_only_escapes_formula_lead_chars():
    from phi_core.agents.reasoning import _neutralise_formula
    assert _neutralise_formula("=SUM(A1)") == "'=SUM(A1)"
    assert _neutralise_formula("+1") == "'+1"
    assert _neutralise_formula("-1") == "'-1"
    assert _neutralise_formula("@cmd") == "'@cmd"
    assert _neutralise_formula("normal value") == "normal value"
    assert _neutralise_formula("") == ""
