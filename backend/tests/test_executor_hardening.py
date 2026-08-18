import csv
from pathlib import Path

import pytest

from phi_core.agents.reasoning import (
    PseudonymRegistry,
    _apply_action,
    _scrub_text_cell,
    apply_column_actions_to_dataset,
)


def test_cap_age_90_fails_closed_on_non_numeric_input():
    for garbage in ("ninety-five", "N/A", "unknown"):
        out = _apply_action(garbage, "cap_age_90", "age")
        assert out == ""
        assert garbage not in out


def test_scrub_text_does_not_swallow_adjacent_markup():
    text = "<b>John Smith</b> <a href='mailto:john@x.com'>email</a>"
    out = _scrub_text_cell(text)
    assert "</b>" in out
    assert "John Smith" not in out


def test_duplicate_column_decisions_raise(tmp_path):
    src = tmp_path / "in.csv"
    src.write_text("id,name\n1,John\n", encoding="utf-8")
    dst = tmp_path / "out.csv"
    decisions = [
        {"column": "id", "action": "hash"},
        {"column": "id", "action": "drop"},
    ]
    with pytest.raises(ValueError, match="duplicate decisions"):
        apply_column_actions_to_dataset(src, dst, "csv", decisions, PseudonymRegistry(salt="s"))


def test_dataset_write_failure_leaves_no_partial_file(tmp_path, monkeypatch):
    src = tmp_path / "in.csv"
    src.write_text("id\n1\n2\n", encoding="utf-8")
    dst = tmp_path / "out.csv"
    decisions = [{"column": "id", "action": "keep"}]

    def _boom(*a, **kw):
        raise RuntimeError("simulated mid-write failure")

    monkeypatch.setattr("phi_core.agents.reasoning._apply_action", _boom)
    with pytest.raises(RuntimeError):
        apply_column_actions_to_dataset(src, dst, "csv", decisions, PseudonymRegistry(salt="s"))
    assert not dst.exists()
    assert not (dst.with_name(dst.name + ".tmp")).exists()


def test_atomic_write_produces_correct_output(tmp_path):
    src = tmp_path / "in.csv"
    src.write_text("id,age\n1,45\n2,999\n", encoding="utf-8")
    dst = tmp_path / "out.csv"
    decisions = [{"column": "id", "action": "keep"}, {"column": "age", "action": "cap_age_90"}]
    apply_column_actions_to_dataset(src, dst, "csv", decisions, PseudonymRegistry(salt="s"))
    rows = list(csv.DictReader(dst.open(encoding="utf-8")))
    assert rows[0]["age"] == "45"
    assert rows[1]["age"] == "90+"
    assert not (dst.with_name(dst.name + ".tmp")).exists()
