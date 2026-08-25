"""Focused contracts for the legacy translation shims (control/adapters.py)."""
from __future__ import annotations

import pytest
from phi_core.control.adapters import legacy_decision_adapter, legacy_files_adapter


def test_legacy_decision_adapter_accepts_a_bare_list() -> None:
    raw = [{"file_id": "f1", "column": "a"}]

    result = legacy_decision_adapter(raw)

    assert result == raw
    assert result is not raw  # never returns the caller's own list


def test_legacy_decision_adapter_accepts_the_judge_envelope() -> None:
    raw = {"decisions": [{"file_id": "f1", "column": "a"}], "reasoning": "ignored"}

    result = legacy_decision_adapter(raw)

    assert result == [{"file_id": "f1", "column": "a"}]


def test_legacy_decision_adapter_never_mutates_the_input() -> None:
    raw = {"decisions": [{"file_id": "f1", "column": "a"}]}

    result = legacy_decision_adapter(raw)
    result[0]["column"] = "mutated"

    assert raw["decisions"][0]["column"] == "a"


@pytest.mark.parametrize("raw", [None, {}, {"decisions": None}])
def test_legacy_decision_adapter_returns_empty_for_none_or_absent_decisions(raw: object) -> None:
    assert legacy_decision_adapter(raw) == []


def test_legacy_decision_adapter_rejects_an_unsupported_shape() -> None:
    with pytest.raises(TypeError):
        legacy_decision_adapter("not a mapping or a sequence of mappings")  # type: ignore[arg-type]


def test_legacy_files_adapter_normalizes_known_field_aliases() -> None:
    files = [
        {"file_id": "f1", "path": "/data/f1.csv", "column_names": ["a", "b"]},
        {"id": "f2", "stored_path": "/data/f2.csv", "columns": ["c"]},
        {"file_id": "f3", "schema_error": "corrupt zip"},
    ]

    result = legacy_files_adapter(files)

    assert result[0] == {"file_id": "f1", "stored_path": "/data/f1.csv", "columns": ["a", "b"], "unreadable_reason": ""}
    assert result[1] == {"file_id": "f2", "stored_path": "/data/f2.csv", "columns": ["c"], "unreadable_reason": ""}
    assert result[2] == {"file_id": "f3", "stored_path": "", "columns": None, "unreadable_reason": "corrupt zip"}


def test_legacy_files_adapter_handles_none_and_empty_input() -> None:
    assert legacy_files_adapter(None) == []
    assert legacy_files_adapter([]) == []


def test_legacy_files_adapter_output_composes_with_assert_exact_coverage() -> None:
    from phi_core.control.gates import assert_exact_coverage

    files = legacy_files_adapter([{"file_id": "f1", "path": "/data/f1.csv", "columns": ["a"]}])
    decisions = [{"file_id": "f1", "column": "a", "action": "keep"}]

    status, _ = assert_exact_coverage(decisions, files)

    assert status == "pass"
