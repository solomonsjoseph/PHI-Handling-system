"""Instrument planter -- render a flat-text (Tier-2) PDF collection form
for a scenario, plus the ground-truth entries for every value planted on
it.

``phi_core.agents.specialists.Instrument`` reads PDF collection-form files
through two tiers: Tier 1 (``phi_core.file_readers.read_pdf_form_fields``)
reads real fillable AcroForm widgets via ``pypdf`` and returns ``None`` for
a PDF with no such widgets; Tier 2 (``phi_core.file_readers.read_pdf``)
extracts flat/scanned text via ``pypdf`` (with an OCR fallback for
image-only pages) and hands it to an LLM to list the fields. This module
renders a flat, non-AcroForm PDF -- a form title plus a block of
"Field Name: value" lines -- via ``reportlab``, so it always exercises the
real Tier-2 text-extraction path, never fabricates AcroForm widgets, and
is genuinely readable by ``read_pdf`` with no OCR fallback needed (the
rendered digital text comfortably clears ``OCR_TEXT_THRESHOLD``).

Every value written onto the page is drawn from the scenario's own
``ColumnSpec.generator`` -- the same generator functions
``phi_corpus.scenarios`` already uses for the CSV datasets -- never a
hardcoded literal, so a planted form value is exactly as trackable/
deduplicable as any other planted PHI value in the corpus.
"""
from __future__ import annotations

import io
import random

from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas as _canvas

from .scenarios import ColumnSpec, Scenario

# Friendly form-prompt wording for the HIPAA A-R identifier categories --
# matches how a real clinical collection form would label each field.
# Categories with no override fall back to the column's own name.
_CATEGORY_LABELS: dict[str, str] = {
    "A": "Patient Name",
    "B": "Address / ZIP Code",
    "C": "Date of Birth",
    "D": "Phone Number",
    "E": "Fax Number",
    "F": "Email Address",
    "G": "Social Security Number",
    "H": "Medical Record Number (MRN)",
    "I": "Health Plan / Insurance ID",
    "J": "Account Number",
    "K": "License / Certificate Number",
    "L": "Vehicle Identification Number",
    "M": "Device Serial Number",
    "N": "Personal URL",
    "O": "IP Address",
    "P": "Biometric Identifier",
    "Q": "Photo Reference ID",
    "R": "Tracking Code",
}

# Identification-block categories, in the order a real intake form would
# ask for them (name, then DOB, then the study/medical-record id).
_ID_BLOCK_ORDER: dict[str, int] = {"A": 0, "C": 1, "H": 2}

# 3-6 realistic form fields per the corpus design: an identification block
# (name/DOB/MRN, when present) plus a couple of additional identifiers.
_MAX_FIELDS = 5


def _field_label(col: ColumnSpec) -> str:
    override = _CATEGORY_LABELS.get(col.hipaa_category)
    if override:
        return override
    return col.name.replace("_", " ").strip().title()


def _draw_value(col: ColumnSpec, rng: random.Random) -> str:
    out = col.generator(rng)
    if isinstance(out, tuple):
        return out[0]
    return out


def _select_form_columns(scn: Scenario, max_fields: int = _MAX_FIELDS) -> list[ColumnSpec]:
    """Pick a small, realistic set of PHI-bearing columns for the form.

    Walks every dataset the scenario declares (not just the first) so a
    scenario whose primary dataset happens to carry no identifiers still
    yields a usable form. Dedupes by HIPAA category -- a scenario that
    tags ``dob``, ``admission_date``, and ``age`` all category "C" should
    only prompt for one date-of-birth-shaped field, not three near-
    duplicates. The A/C/H identification block (name, DOB, MRN) sorts
    first when present; any other identifier categories fill the
    remaining slots in the order they were declared.
    """
    seen: set[str] = set()
    identification: list[ColumnSpec] = []
    other: list[ColumnSpec] = []
    for ds in scn.datasets:
        for col in ds.columns:
            cat = col.hipaa_category
            if not cat or cat == "NONE" or cat in seen:
                continue
            seen.add(cat)
            (identification if cat in _ID_BLOCK_ORDER else other).append(col)
    identification.sort(key=lambda c: _ID_BLOCK_ORDER[c.hipaa_category])
    return (identification + other)[:max_fields]


def generate_form_pdf(scn: Scenario, rng: random.Random) -> tuple[bytes, list[dict[str, str]]]:
    """Render one flat-text PDF collection form for ``scn``.

    Returns ``(pdf_bytes, form_plants)``. ``form_plants`` is a list of
    ``{"field_label": str, "value": str}`` entries, one per PHI-shaped
    value rendered onto the page, in the same order they appear on the
    form -- the ground-truth analogue of ``dictionary_plants`` for the
    forms component.
    """
    columns = _select_form_columns(scn)
    plants: list[dict[str, str]] = []

    buf = io.BytesIO()
    c = _canvas.Canvas(buf, pagesize=LETTER)
    width, height = LETTER
    y = height - 72

    c.setFont("Helvetica-Bold", 16)
    c.drawString(72, y, f"{scn.label}")
    y -= 20
    c.setFont("Helvetica-Bold", 13)
    c.drawString(72, y, "Patient Enrollment / Collection Form")
    y -= 28

    c.setFont("Helvetica", 10)
    c.drawString(72, y, f"Study ID: {scn.id}")
    y -= 14
    c.drawString(72, y, "Form Version: 1.0    Site: Central Coordinating Site")
    y -= 28

    c.setFont("Helvetica-Bold", 12)
    c.drawString(72, y, "Patient Identification")
    y -= 22

    c.setFont("Helvetica", 11)
    for col in columns:
        label = _field_label(col)
        value = _draw_value(col, rng)
        c.drawString(90, y, f"{label}: {value}")
        y -= 22
        plants.append({"field_label": label, "value": value})

    y -= 14
    c.setFont("Helvetica", 9)
    c.drawString(72, y, "This form was completed as part of routine study data collection procedures.")
    y -= 12
    c.drawString(72, y, "Retain in the study binder per protocol; do not transmit unredacted outside the site.")

    c.showPage()
    c.save()
    return buf.getvalue(), plants
