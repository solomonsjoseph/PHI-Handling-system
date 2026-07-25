"""Detection: Presidio + rule-based, merged.

Rule detectors close known Presidio gaps documented in AUTHORITY_MATRIX.md:
  - MRN, MBI, NPI (Luhn), VIN, UDI, device serials, biometric templates,
    photo file references, clinical trial IDs (NCTxxx), fax numbers.
Every rule attaches its 45 CFR 164.514 authority citation.

Presidio maps to HIPAA A-R via _presidio_to_hipaa.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

from presidio_analyzer import AnalyzerEngine, RecognizerResult

from .models import DetectedSpan


AUTH_SAFE_HARBOR = "45 CFR 164.514(b)(2)(i)"


@lru_cache(maxsize=1)
def _analyzer() -> AnalyzerEngine:
    return AnalyzerEngine()


_PRESIDIO_TO_HIPAA: dict[str, tuple[str, str]] = {
    # entity -> (hipaa_category, normalized_entity_type)
    "PERSON": ("A", "NAME"),
    "LOCATION": ("B", "ADDRESS"),
    "DATE_TIME": ("C", "DATE"),
    "PHONE_NUMBER": ("D", "PHONE"),
    "EMAIL_ADDRESS": ("F", "EMAIL"),
    "US_SSN": ("G", "SSN"),
    "US_ITIN": ("G", "ITIN"),
    "US_BANK_NUMBER": ("J", "ACCOUNT"),
    "US_DRIVER_LICENSE": ("K", "LICENSE"),
    "US_PASSPORT": ("K", "LICENSE"),
    "URL": ("N", "URL"),
    "IP_ADDRESS": ("O", "IP_ADDRESS"),
    "CREDIT_CARD": ("J", "CREDIT_CARD"),
    "MEDICAL_LICENSE": ("K", "LICENSE"),
    "IBAN_CODE": ("J", "ACCOUNT"),
}


def _presidio_to_hipaa(entity: str) -> tuple[str | None, str]:
    return _PRESIDIO_TO_HIPAA.get(entity, (None, entity))


# --- Rule detectors --------------------------------------------------------

@dataclass
class Rule:
    name: str
    pattern: re.Pattern
    entity_type: str
    hipaa_category: str
    authority: str = AUTH_SAFE_HARBOR
    validator: object = None  # optional callable(str) -> bool


def _luhn(digits: str) -> bool:
    ds = [int(d) for d in digits if d.isdigit()]
    if len(ds) < 2:
        return False
    total = 0
    for i, d in enumerate(reversed(ds)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


RULES: list[Rule] = [
    Rule("MRN", re.compile(r"\bMRN[-:\s]*([A-Z0-9\-]{6,20})\b"), "MRN", "H"),
    Rule("MBI", re.compile(r"\b[1-9][ACDEFGHJKMNPQRTUVWXY][ACDEFGHJKMNPQRTUVWXY0-9][ACDEFGHJKMNPQRTUVWXY][ACDEFGHJKMNPQRTUVWXY0-9][0-9][ACDEFGHJKMNPQRTUVWXY][ACDEFGHJKMNPQRTUVWXY][0-9]{2}\b"), "MBI", "I"),
    Rule("NPI", re.compile(r"\b\d{10}\b"), "NPI", "K",
         validator=lambda s: _luhn("80840" + s)),
    Rule("SSN", re.compile(r"\b(?!000|666|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"), "SSN", "G"),
    Rule("US_PHONE", re.compile(r"\(?\b[2-9]\d{2}\)?[\s\-\.]?\d{3}[\s\-\.]?\d{4}\b"), "PHONE", "D"),
    Rule("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "EMAIL", "F"),
    Rule("VIN", re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b"), "VIN", "L"),
    Rule("LICENSE_PLATE", re.compile(r"\b[A-Z]{3}-\d{4}\b"), "LICENSE_PLATE", "L"),
    Rule("DEVICE_UDI", re.compile(r"\(01\)\d{14}\b"), "DEVICE_UDI", "M"),
    Rule("DEVICE_SERIAL", re.compile(r"\bSN[0-9A-Z]{8,14}\b"), "DEVICE_SERIAL", "M"),
    Rule("BIOMETRIC", re.compile(r"\b(?:fingerprint|voice|retinal|iris|DNA)_template_\d{10,20}\b"), "BIOMETRIC", "P"),
    Rule("PHOTO_FULL_FACE", re.compile(r"\bpatient_photo_\d{6,12}\.(?:jpg|png|dcm)\b"), "PHOTO_FULL_FACE", "Q"),
    Rule("CLINICAL_TRIAL_ID", re.compile(r"\bNCT\d{8}\b"), "CLINICAL_TRIAL_ID", "R"),
    Rule("FAX_LABEL", re.compile(r"\bfax[:\s]+\(?\d{3}\)?[\s\-]?\d{3}-\d{4}\b", re.IGNORECASE), "FAX", "E"),
    Rule("AGE_OVER_89", re.compile(r"\bage\s+(?:90\+|9\d|1\d{2})\b", re.IGNORECASE), "AGE_OVER_89", "C"),
    # ZIP requires either explicit label "ZIP" nearby, or preceding US state code.
    Rule("US_ZIP_LABELED", re.compile(r"(?:(?<=ZIP\s)|(?<=ZIP:\s)|(?<=[A-Z][A-Z]\s))\d{5}(?:-\d{4})?\b"), "ZIP", "B"),
]


def rule_detect(text: str) -> list[DetectedSpan]:
    out: list[DetectedSpan] = []
    for rule in RULES:
        for m in rule.pattern.finditer(text):
            value = m.group(0)
            if rule.validator is not None:
                # for NPI: the whole match is the 10-digit body
                if not rule.validator(m.group(0)):
                    continue
            out.append(DetectedSpan(
                start=m.start(),
                end=m.end(),
                value=value,
                entity_type=rule.entity_type,
                hipaa_category=rule.hipaa_category,
                detector="rule",
                confidence=0.99,
                authority=rule.authority,
            ))
    return out


def presidio_detect(text: str, language: str = "en") -> list[DetectedSpan]:
    if not text.strip():
        return []
    results: list[RecognizerResult] = _analyzer().analyze(text=text, language=language)
    out: list[DetectedSpan] = []
    for r in results:
        hipaa, ent = _presidio_to_hipaa(r.entity_type)
        out.append(DetectedSpan(
            start=r.start,
            end=r.end,
            value=text[r.start:r.end],
            entity_type=ent,
            hipaa_category=hipaa,
            detector="presidio",
            confidence=float(r.score),
            authority=AUTH_SAFE_HARBOR if hipaa else "",
        ))
    return out


def merge_spans(spans: list[DetectedSpan]) -> list[DetectedSpan]:
    """Deduplicate overlapping spans. Prefer higher confidence, then rule > presidio.

    Spans with different hipaa_category are allowed to coexist unless their
    overlap exceeds 60 percent of the shorter span (in which case only one is kept).
    """
    if not spans:
        return []
    ordered = sorted(spans, key=lambda s: (s.start, -s.end))
    result: list[DetectedSpan] = []
    for s in ordered:
        overlap = None
        for r in result:
            inter = max(0, min(s.end, r.end) - max(s.start, r.start))
            if inter == 0:
                continue
            shorter = min(s.end - s.start, r.end - r.start) or 1
            frac = inter / shorter
            same_cat = (s.hipaa_category == r.hipaa_category)
            # merge only when same category, or when overlap dominates the shorter span
            if same_cat or frac >= 0.6:
                overlap = r
                break
        if overlap is None:
            result.append(s)
            continue
        prefer_s = (s.confidence, s.detector == "rule") > (overlap.confidence, overlap.detector == "rule")
        if prefer_s:
            result.remove(overlap)
            result.append(s)
    return sorted(result, key=lambda s: s.start)


def detect_text(text: str, detectors: Iterable[str] = ("presidio", "rule")) -> list[DetectedSpan]:
    spans: list[DetectedSpan] = []
    if "presidio" in detectors:
        spans.extend(presidio_detect(text))
    if "rule" in detectors:
        spans.extend(rule_detect(text))
    merged = merge_spans(spans)
    for m in merged:
        m.detector = "merged" if len(detectors) > 1 else next(iter(detectors))
    return merged


# --- Dataset (cell-level) --------------------------------------------------

# Column-header hints that mark whole columns as PHI without inspecting values.
HEADER_HINTS: dict[str, tuple[str, str]] = {
    "name": ("NAME", "A"), "patient_name": ("NAME", "A"), "first_name": ("NAME", "A"), "last_name": ("NAME", "A"), "full_name": ("NAME", "A"),
    "dob": ("DATE", "C"), "date_of_birth": ("DATE", "C"), "birth_date": ("DATE", "C"), "admit_date": ("DATE", "C"), "discharge_date": ("DATE", "C"),
    "ssn": ("SSN", "G"), "social_security": ("SSN", "G"), "social_security_number": ("SSN", "G"),
    "mrn": ("MRN", "H"), "medical_record_number": ("MRN", "H"),
    "phone": ("PHONE", "D"), "phone_number": ("PHONE", "D"), "mobile": ("PHONE", "D"),
    "email": ("EMAIL", "F"),
    "address": ("ADDRESS", "B"), "street": ("ADDRESS", "B"), "zip": ("ADDRESS", "B"), "postal_code": ("ADDRESS", "B"), "city": ("ADDRESS", "B"),
    "npi": ("NPI", "K"), "provider_npi": ("NPI", "K"),
    "vin": ("VIN", "L"),
    "ip": ("IP_ADDRESS", "O"), "ip_address": ("IP_ADDRESS", "O"),
    "mbi": ("MBI", "I"),
}


def header_phi_columns(columns: list[str]) -> dict[str, tuple[str, str]]:
    """Return {column_name: (entity_type, hipaa_category)} for headers that
    unambiguously indicate PHI. Case-insensitive, underscore/space tolerant.
    """
    hits: dict[str, tuple[str, str]] = {}
    for col in columns:
        key = re.sub(r"[^a-z0-9]+", "_", col.lower()).strip("_")
        if key in HEADER_HINTS:
            hits[col] = HEADER_HINTS[key]
    return hits
