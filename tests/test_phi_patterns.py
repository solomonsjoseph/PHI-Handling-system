"""Regression tests for the blocking-tier regex gaps closed by the 2026-07-06 audit.

Each case is a false negative the audit found in phi_engine/security/phi_patterns.py:
US phone, unhyphenated SSN, addresses, ages>89, labeled MRNs, and text dates.
"""
from __future__ import annotations

import pytest

from phi_engine.security.phi_gate import phi_gate_check


CASES = [
    ("Call the patient at (415) 555-0134 tomorrow.", "US_PHONE"),
    ("Call the patient at 415-555-0134 tomorrow.", "US_PHONE"),
    ("SSN: 123456789 on file.", "SSN_UNHYPHENATED"),
    ("Patient resides at 742 Evergreen Terrace.", "ADDRESS"),
    ("Patient is 94 years old.", "AGE_OVER_89"),
    ("Patient aged: 91.", "AGE_OVER_89"),
    ("Medical Record Number: 8823914", "MRN_LABELED"),
    ("Visit occurred on March 12, 1985.", "DATE_TEXT"),
]


@pytest.mark.parametrize("text,expected_finding", CASES)
def test_blocking_pattern_catches_gap(text, expected_finding):
    result = phi_gate_check(text)
    assert result.blocked, f"expected block for: {text!r}"
    assert any(expected_finding in f for f in result.findings), result.findings


def test_benign_text_not_blocked():
    result = phi_gate_check("Patient reported mild headache, no known allergies.")
    assert not result.blocked


if __name__ == "__main__":
    for text, expected in CASES:
        r = phi_gate_check(text)
        assert r.blocked and any(expected in f for f in r.findings), (text, r.findings)
    assert not phi_gate_check("Patient reported mild headache.").blocked
    print("phi_patterns gap-closure self-check OK")
