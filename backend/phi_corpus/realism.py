"""Realism primitives -- messiness profiles for corpus generation.

Real US study data systems do not emit clean, uniformly-formatted values.
Dates arrive in half a dozen different styles across one export, a fraction
of cells hold a missingness code instead of a value, and free text carries
quote hazards (embedded commas, escaped quotes, CRLF). This module gives
scenario authors dials to reproduce that mess so the export oracle (see
``planters.ExportExpectation``) is tested against inputs a real system would
actually produce rather than idealized ones.

Callers are responsible for sequencing: draw ``maybe_missing`` first, and
only run ``jitter`` on a value when it did not come back missing. ``jitter``
itself never inspects the missingness codes, so calling it on a missing cell
would corrupt a token every downstream check treats as sacred.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

MISSING_CODES: tuple[str, ...] = ("", "UNK", "NA", "N/A", ".", "-99")
DATE_STYLES: tuple[str, ...] = ("iso", "us_slash", "eu_slash", "dmon", "long", "us_short")

_MONTHS_ABBR_UPPER = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                      "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_MONTHS_ABBR_LONG = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass(frozen=True)
class RealismProfile:
    name: str
    missing_rate: float = 0.0
    date_styles: tuple[str, ...] = ("iso",)
    whitespace_rate: float = 0.0
    case_noise_rate: float = 0.0
    encoding: str = "utf-8"           # "utf-8" | "utf-8-sig" | "latin-1"
    quote_hazards: bool = False       # embedded commas, double quotes, CRLF in free text


CLEAN = RealismProfile("clean")
MESSY = RealismProfile("messy", missing_rate=0.08,
                        date_styles=("iso", "us_slash", "dmon"),
                        whitespace_rate=0.10, case_noise_rate=0.10)
HOSTILE = RealismProfile("hostile", missing_rate=0.12,
                          date_styles=DATE_STYLES,
                          whitespace_rate=0.15, case_noise_rate=0.15,
                          encoding="utf-8-sig", quote_hazards=True)
PROFILES: dict[str, RealismProfile] = {p.name: p for p in (CLEAN, MESSY, HOSTILE)}


def render_date(year: int, month: int, day: int, style: str) -> str:
    """Render a calendar date in one of ``DATE_STYLES``.

    Unknown styles raise rather than silently falling back, so a scenario
    typo fails at import time instead of producing an untracked format.
    """
    if style == "iso":
        return f"{year:04d}-{month:02d}-{day:02d}"
    if style == "us_slash":
        return f"{month:02d}/{day:02d}/{year:04d}"
    if style == "eu_slash":
        return f"{day:02d}/{month:02d}/{year:04d}"
    if style == "dmon":
        return f"{day:02d}-{_MONTHS_ABBR_UPPER[month - 1]}-{year:04d}"
    if style == "long":
        return f"{_MONTHS_ABBR_LONG[month - 1]} {day}, {year:04d}"
    if style == "us_short":
        return f"{month:02d}/{day:02d}/{year % 100:02d}"
    raise ValueError(f"unknown date style: {style!r}")


def jitter(value: str, profile: RealismProfile, rng: random.Random, *, reorder: bool = True) -> str:
    """Apply whitespace and case noise, each gated on its own draw.

    Order: a trailing space first, then (independently) one of
    ``value.upper()``, ``value.lower()``, or -- when ``reorder`` is true
    and the value has exactly two tokens -- a surname-first reorder. The
    caller must never pass a value it drew from ``maybe_missing``.

    ``reorder=False`` is required for anything that is not an actual
    person name: a two-token controlled term (CDISC ``ARMCD`` "ARM A",
    a license-plate-shaped code) is not a name, and swapping its tokens
    manufactures an invalid term rather than a realistic name variant.
    """
    out = value
    if rng.random() < profile.whitespace_rate:
        out = out + " "
    if rng.random() < profile.case_noise_rate:
        parts = out.strip().split(" ")
        options = ["upper", "lower"]
        if reorder and len(parts) == 2 and all(parts):
            options.append("reorder")
        choice = rng.choice(options)
        if choice == "upper":
            out = out.upper()
        elif choice == "lower":
            out = out.lower()
        else:
            out = f"{parts[1]} {parts[0]}"
    return out


def maybe_missing(profile: RealismProfile, rng: random.Random) -> str | None:
    """Return a missingness code drawn from ``MISSING_CODES``, or ``None``."""
    if rng.random() < profile.missing_rate:
        return rng.choice(MISSING_CODES)
    return None
