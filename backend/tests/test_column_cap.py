"""4.21: a study with more dataset columns than MAX_COLUMNS_PER_STUDY must
be refused before any prompt is built, not sent to the Judge unbounded."""
from __future__ import annotations

import pytest


def test_enforce_column_cap_passes_under_limit(monkeypatch):
    import server as srv
    monkeypatch.setattr(srv, "_MAX_COLUMNS_PER_STUDY", 5)
    files = [
        {"kind": "dataset", "columns": ["a", "b", "c"]},
        {"kind": "dataset", "columns": ["d", "e"]},
        {"kind": "metadata", "columns": ["z"] * 100},  # non-dataset files never count
    ]
    assert srv._enforce_column_cap(files) == 5


def test_enforce_column_cap_raises_clear_error_over_limit(monkeypatch):
    import server as srv
    monkeypatch.setattr(srv, "_MAX_COLUMNS_PER_STUDY", 5)
    files = [
        {"kind": "dataset", "columns": ["a", "b", "c"]},
        {"kind": "dataset", "columns": ["d", "e", "f"]},
    ]
    with pytest.raises(ValueError, match=r"6 dataset columns.*exceeding the 5-column-per-study limit"):
        srv._enforce_column_cap(files)


def test_enforce_column_cap_default_is_500():
    import server as srv
    assert srv._MAX_COLUMNS_PER_STUDY == 500


def test_enforce_column_cap_handles_missing_columns_key():
    import server as srv
    files = [{"kind": "dataset"}, {"kind": "dataset", "columns": None}]
    assert srv._enforce_column_cap(files) == 0
