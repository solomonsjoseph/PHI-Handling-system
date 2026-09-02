"""OCR fallback tests for image-only PDFs (Phase C).

Generates a synthetic image-only PDF at test time and asserts:

1. ``read_pdf`` returns non-empty text (i.e. OCR path fires) even though
   the PDF has no digital text layer.
2. The OCR output flowing through ``_scrub_text_cell`` produces the
   expected PHI category tags.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from phi_core.file_readers import OCR_TEXT_THRESHOLD, read_pdf


def _tesseract_available() -> bool:
    """Skip if the tesseract binary or pytesseract module isn't installed
    on this deployment."""
    try:
        # binary check
        from shutil import which

        import pdf2image  # noqa: F401
        import pytesseract  # noqa: F401
        if not which("tesseract") or not which("pdftoppm"):
            return False
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _tesseract_available(), reason="tesseract / pdf2image not available"
)


def _make_image_only_pdf(tmp_path: Path, text: str) -> Path:
    """Render ``text`` into a JPEG then wrap it in a PDF via Pillow so the
    resulting PDF has no digital text layer."""
    from PIL import Image, ImageDraw, ImageFont
    W, H = 1200, 800
    img = Image.new("RGB", (W, H), color=(255, 255, 255))
    d = ImageDraw.Draw(img)
    # Use a large default font so tesseract can pick it up reliably.
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 42)
    except Exception:
        font = ImageFont.load_default()
    y = 60
    for line in text.split("\n"):
        d.text((60, y), line, fill=(0, 0, 0), font=font)
        y += 70
    pdf_path = tmp_path / "scanned_form.pdf"
    img.save(pdf_path, "PDF", resolution=200.0)
    return pdf_path


def test_read_pdf_ocr_fallback_extracts_text(tmp_path: Path):
    text = "Patient James Smith\nPhone 415-555-1234\nDOB 1975-03-15"
    pdf = _make_image_only_pdf(tmp_path, text)
    extracted = read_pdf(pdf)
    # OCR is not perfect, but with a big DejaVu font it recovers most words.
    joined = extracted.lower()
    assert len(extracted.strip()) >= OCR_TEXT_THRESHOLD, extracted
    # Names + phone should both survive OCR at 200dpi.
    assert "james" in joined or "smith" in joined
    assert "555" in joined and "1234" in joined


def test_ocr_output_flows_through_scrubber(tmp_path: Path):
    """The critical Phase C invariant: OCR text becomes safe after
    ``_scrub_text_cell`` — same detector as digital-text PDFs."""
    from phi_core.control.transform_primitives import _scrub_text_cell

    text = "Patient contact 415-555-1234 for James Smith"
    pdf = _make_image_only_pdf(tmp_path, text)
    extracted = read_pdf(pdf)
    scrubbed = _scrub_text_cell(extracted)
    # Phone (D) should be redacted; a raw 415-555 substring must be gone.
    assert "415-555-1234" not in scrubbed
    assert "[D]" in scrubbed or "[G]" in scrubbed or "[A]" in scrubbed
