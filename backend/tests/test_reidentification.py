"""Tests for the lifted k-anonymity / l-diversity gate.

The gate was lifted verbatim (names and signatures preserved) from
``phi_engine/security/kanon_gate.py`` and ``phi_engine/security/pycanon_gate.py``
into ``phi_core/control/reidentification.py``. Those source modules are deleted
in a later step; these tests are the durable record that the lifted port keeps
the same behaviour.

TDD: this file was written first against the ported public surface, then the
module was created to satisfy it.
"""

from __future__ import annotations

import pytest
from phi_core.control import reidentification as _reid
from phi_core.control.reidentification import (
    PyCanonGateResult,
    check_publish_anonymity,
    kanon_check,
    l_diversity_check,
    mask_small_cell,
    suppress_small_cells,
)

# --- kanon_check -----------------------------------------------------------


def _rows(age: list[int], sex: list[str], extra: list[str] | None = None) -> list[dict]:
    rows = []
    for i, (a, s) in enumerate(zip(age, sex, strict=True)):
        row = {"age": a, "sex": s}
        if extra is not None:
            row["outcome"] = extra[i]
        rows.append(row)
    return rows


def test_kanon_empty_rows_vacuously_not_blocked():
    out = kanon_check([], quasi_identifiers=("age", "sex"))
    assert out.blocked is False
    assert out.smallest_class_size == 0
    assert out.violating_keys == ()


def test_kanon_records_numeric_k():
    """The smallest equivalence-class size is the numeric k acceptance criterion 9 needs."""
    rows = _rows([30] * 5 + [40], ["M"] * 6)
    out = kanon_check(rows, quasi_identifiers=("age", "sex"), k=5)
    assert out.smallest_class_size == 1  # the singleton (40, M) class
    assert out.blocked is True
    # The violating key names the quasi-identifier values, never raw cell data.
    assert out.violating_keys == ("40|M",)


def test_kanon_not_blocked_when_all_classes_above_k():
    rows = _rows([30] * 5, ["M"] * 5)
    out = kanon_check(rows, quasi_identifiers=("age", "sex"), k=5)
    assert out.smallest_class_size == 5
    assert out.blocked is False
    assert out.violating_keys == ()


def test_kanon_rejects_bad_args():
    with pytest.raises(ValueError, match="k must be >= 1"):
        kanon_check([], quasi_identifiers=("age",), k=0)
    with pytest.raises(ValueError, match="quasi_identifiers must be non-empty"):
        kanon_check([], quasi_identifiers=())


# --- l_diversity_check -----------------------------------------------------


def test_l_diversity_blocked_on_homogeneous_class():
    rows = _rows([30] * 5, ["M"] * 5, ["DIED"] * 5)
    out = l_diversity_check(
        rows, quasi_identifiers=("age", "sex"), sensitive_attributes=("outcome",), l_threshold=2
    )
    assert out.blocked is True
    assert out.smallest_diversity == 1
    assert out.violating_classes == (("30|M", "outcome"),)


def test_l_diversity_passes_with_two_distinct_sensitive_values():
    rows = _rows([30] * 4, ["M"] * 4, ["DIED", "LIVED", "DIED", "LIVED"])
    out = l_diversity_check(
        rows, quasi_identifiers=("age", "sex"), sensitive_attributes=("outcome",), l_threshold=2
    )
    assert out.blocked is False
    assert out.smallest_diversity == 2
    assert out.violating_classes == ()


def test_l_diversity_empty_and_bad_args():
    assert l_diversity_check(
        [], quasi_identifiers=("age",), sensitive_attributes=("outcome",)
    ).blocked is False
    with pytest.raises(ValueError, match="l_threshold must be >= 1"):
        l_diversity_check([], quasi_identifiers=("a",), sensitive_attributes=("s",), l_threshold=0)
    with pytest.raises(ValueError, match="sensitive_attributes must be non-empty"):
        l_diversity_check([], quasi_identifiers=("a",), sensitive_attributes=())


# --- small-cell suppression -------------------------------------------------


def test_mask_small_cell():
    assert mask_small_cell(9, k=5) == 9
    assert mask_small_cell(5, k=5) == 5
    assert mask_small_cell(4, k=5) == "<5"  # label derived from k
    assert mask_small_cell(4, k=5, label="suppressed") == "suppressed"


def test_suppress_small_cells():
    out = suppress_small_cells({"A": 9, "B": 2}, k=5)
    assert out == {"A": 9, "B": "<5"}


# --- check_publish_anonymity ------------------------------------------------


def test_check_publish_anonymity_empty_records_is_vacuously_anonymous():
    """Empty record set returns ok=True/k=0 and does NOT import pycanon."""
    out = check_publish_anonymity([], quasi_identifiers=["age", "sex"], k_threshold=5)
    assert isinstance(out, PyCanonGateResult)
    assert out.ok is True
    assert out.k == 0
    assert out.n_records == 0
    assert "vacuously anonymous" in out.reason


def test_check_publish_anonymity_bad_args():
    with pytest.raises(ValueError, match="k_threshold must be >= 1"):
        check_publish_anonymity([], quasi_identifiers=["age"], k_threshold=0)
    with pytest.raises(ValueError, match="quasi_identifiers must be non-empty"):
        check_publish_anonymity([], quasi_identifiers=[], k_threshold=5)


@pytest.mark.skipif(not _reid.PYCANON_AVAILABLE, reason="pycanon not installed on this platform")
def test_check_publish_anonymity_measures_k_when_pycanon_present():
    records = [{"age": 30, "sex": "M"} for _ in range(5)] + [{"age": 40, "sex": "F"}]
    out = check_publish_anonymity(records, quasi_identifiers=["age", "sex"], k_threshold=5)
    assert out.k == 1
    assert out.ok is False
    assert out.n_records == 6


def test_check_publish_anonymity_makes_pycanon_unavailability_explicit():
    """When pycanon is absent, a real (non-empty) measurement is a declared
    unavailability, never a silent skip of the value-free anonymous-empty case."""
    if _reid.PYCANON_AVAILABLE:
        pytest.skip("pycanon present; absence path not exercisable here")
    with pytest.raises(ImportError):
        check_publish_anonymity([{"age": 30, "sex": "M"}], quasi_identifiers=["age", "sex"])
