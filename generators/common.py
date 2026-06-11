"""
Common utilities for deterministic PHI corpus generation.

Every generator in this package derives from DeterministicGenerator and uses
seeded PRNG state. The same seed produces bitwise-identical corpora -- a
requirement for IRB reproducibility attestation.

Authority: See authorities/AUTHORITY_MATRIX.md for the full citation index.
Multi-jurisdiction architecture: See authorities/06_regulatory_philosophy_comparison.md
Detection regime taxonomy: See AUTHORITY_MATRIX.md Table F (arXiv 2412.10918)
"""
from __future__ import annotations

import hashlib
import json
import random
import string
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


# -----------------------------------------------------------------------------
# Authority citation constants
# -----------------------------------------------------------------------------

AUTH_HIPAA_SAFE_HARBOR = "45 CFR 164.514(b)(2)(i)"
AUTH_HIPAA_LDS = "45 CFR 164.514(e)"
AUTH_HIPAA_REID = "45 CFR 164.514(c)"
AUTH_HIPAA_FUNDRAISING = "45 CFR 164.514(f)"
AUTH_HIPAA_VERIFICATION = "45 CFR 164.514(h)"
AUTH_HIPAA_ACTUAL_KNOWLEDGE = "45 CFR 164.514(b)(2)(ii)"
AUTH_HIPAA_MINIMUM_NECESSARY = "45 CFR 164.514(d)"

AUTH_DPDPA_ACT = "DPDPA 2023 Act 22"
AUTH_DPDPA_RULES_6 = "DPDP Rules 2025 Rule 6 (Security Safeguards)"
AUTH_DPDPA_RULES_7 = "DPDP Rules 2025 Rule 7 (Breach Notification)"
AUTH_DPDPA_RULES_10 = "DPDP Rules 2025 Rule 10 (Verifiable Parental Consent)"
AUTH_DPDPA_RULES_13 = "DPDP Rules 2025 Rule 13 (SDF Obligations)"
AUTH_DPDPA_RULES_14 = "DPDP Rules 2025 Rule 14 (Data Principal Rights; Identifier)"
AUTH_DPDPA_RULES_16 = "DPDP Rules 2025 Rule 16 (Research Exemption)"
AUTH_DPDPA_SECOND_SCHEDULE = "DPDP Rules 2025 Second Schedule"
AUTH_DPDPA_FOURTH_SCHEDULE_A = "DPDP Rules 2025 Fourth Schedule Part A"

AUTH_SPDI_RULE_3 = "IT Act SPDI Rules 2011 Rule 3"
AUTH_IT_ACT_43A = "Section 43A IT Act 2000"
AUTH_IT_ACT_72A = "Section 72A IT Act 2000"

AUTH_ICMR_PRIVACY = "ICMR 2017 Section 1.1.5"
AUTH_ICMR_RISK_TIER = "ICMR 2017 Section 2.1 Table 2.1"
AUTH_ICMR_CODING = "ICMR 2017 Section 2.3.5"
AUTH_ICMR_VULNERABILITY = "ICMR 2017 Section 2.9.1"
AUTH_ICMR_DATA_OWNERSHIP = "ICMR 2017 Section 3.3.2"
AUTH_ICMR_HMSC = "ICMR 2017 Section 3.8.3"
AUTH_ICMR_REVIEW_TYPES = "ICMR 2017 Section 4.8 Table 4.2"

AUTH_DICOM_BACP = "DICOM PS3.15 Annex E Basic Confidentiality Profile"
AUTH_FHIR_R4 = "HL7 FHIR R4 v4.0.1"

AUTH_SWEENEY_K_ANON = "Sweeney 2002 k-anonymity"
AUTH_OWASP_LLM_01 = "OWASP LLM Top 10 2025 LLM01 Prompt Injection"
AUTH_MIA_NATURE_2024 = "Nature Sci Rep 2024 Membership Inference"

AUTH_GDPR_RECITAL_26 = "GDPR Recital 26 (anonymous data)"
AUTH_GDPR_RECITAL_35 = "GDPR Recital 35 (health data definition)"
AUTH_GDPR_ARTICLE_4 = "GDPR Article 4(1) (personal data)"
AUTH_GDPR_ARTICLE_4_13 = "GDPR Article 4(13) (genetic data)"
AUTH_GDPR_ARTICLE_4_14 = "GDPR Article 4(14) (biometric data)"
AUTH_GDPR_ARTICLE_4_15 = "GDPR Article 4(15) (data concerning health)"
AUTH_GDPR_ARTICLE_9 = "GDPR Article 9(1) (special categories)"
AUTH_GDPR_ARTICLE_89 = "GDPR Article 89 (research safeguards)"
AUTH_EHDS_2024 = "EU EHDS Regulation 2024/3175 Article 3"

AUTH_PIPEDA_S1 = "PIPEDA Schedule 1 Principles"
AUTH_PHIPA_ON = "PHIPA Ontario 2004 (personal health information)"
AUTH_HIA_AB = "HIA Alberta 2000 (health information)"
AUTH_PIPA_BC = "PIPA BC 2003 (personal information)"

AUTH_UK_GDPR = "UK GDPR Article 4(1) (retained EU law)"
AUTH_UK_NHS_NUMBER = "NHS Data Security and Protection Toolkit"

AUTH_AU_PRIVACY = "Privacy Act 1988 (Cth) Australian Privacy Principles"
AUTH_AU_MY_HEALTH = "Healthcare Identifiers Act 2010 (Cth)"

AUTH_PDPA_SG = "PDPA Singapore 2012 (2021 amendments)"
AUTH_APPI_JP = "APPI Japan 2022 amendments"
AUTH_LGPD_BR = "LGPD Brazil 2020 Article 5"
AUTH_PIPL_CN = "PIPL China 2021 (sensitive personal information)"

AUTH_PUTTASWAMY = "Puttaswamy v Union of India (2017) 10 SCC 1"

# -----------------------------------------------------------------------------
# Detection regime constants (i2b2 taxonomy, arXiv 2412.10918 December 2024)
# rule_applicable: regex/pattern matching sufficient (10 structured types)
# contextual_ner_required: transformer NER required (18 contextual types)
# conflict_case: jurisdictions disagree on PHI status (ZIP, dates, re-ID codes)
# -----------------------------------------------------------------------------

DETECTION_REGIME_RULE = "rule_applicable"
DETECTION_REGIME_NER = "contextual_ner_required"
DETECTION_REGIME_CONFLICT = "conflict_case"

# -----------------------------------------------------------------------------
# Corpus layer taxonomy (authorities/06_regulatory_philosophy_comparison.md)
# -----------------------------------------------------------------------------

LAYER_COMMON = "common"
LAYER_HIPAA = "hipaa_specific"
LAYER_INDIA = "india_specific"
LAYER_GDPR = "gdpr_specific"
LAYER_CONFLICT = "conflict_cases"
LAYER_CANADA = "canada_specific"
LAYER_UK = "uk_specific"
LAYER_AUSTRALIA = "australia_specific"
LAYER_SINGAPORE = "singapore_specific"
LAYER_JAPAN = "japan_specific"
LAYER_BRAZIL = "brazil_specific"
LAYER_CHINA_PIPL = "china_pipl"  # STRUCTURALLY SEPARATE -- not comparable to other layers


# -----------------------------------------------------------------------------
# HIPAA Safe Harbor category enumeration (45 CFR 164.514(b)(2)(i)(A) through (R))
# -----------------------------------------------------------------------------

HIPAA_CATEGORIES = {
    "A": "Names",
    "B": "Geographic subdivisions smaller than State",
    "C": "All elements of dates except year; ages >89",
    "D": "Telephone numbers",
    "E": "Fax numbers",
    "F": "Electronic mail addresses",
    "G": "Social security numbers",
    "H": "Medical record numbers",
    "I": "Health plan beneficiary numbers",
    "J": "Account numbers",
    "K": "Certificate/license numbers",
    "L": "Vehicle identifiers, serial numbers, license plates",
    "M": "Device identifiers and serial numbers",
    "N": "Web URLs",
    "O": "IP address numbers",
    "P": "Biometric identifiers",
    "Q": "Full face photographs and comparable images",
    "R": "Any other unique identifying code",
}


# -----------------------------------------------------------------------------
# 17 restricted ZIP3 codes per HHS/OCR 2012 guidance (Census-dependent)
# -----------------------------------------------------------------------------

RESTRICTED_ZIP3 = {
    "036", "059", "063", "102", "203", "556", "692", "790",
    "821", "823", "830", "831", "878", "879", "884", "890", "893",
}


# -----------------------------------------------------------------------------
# Record structure
# -----------------------------------------------------------------------------

@dataclass
class GoldSpan:
    """A single PHI span within a record with verified offsets."""
    start: int
    end: int
    category: str               # HIPAA Safe Harbor A-R, or extended category
    hipaa_category: Optional[str] = None   # A-R or None for non-HIPAA
    jurisdiction: str = "universal"        # "us" | "in" | "eu" | "universal" | ...
    authority: str = ""
    value: str = ""
    entity_type: str = ""       # i2b2 semantic type (PATIENT, MRN, AADHAAR, etc.)
    detection_regime: str = DETECTION_REGIME_NER  # rule_applicable | contextual_ner_required | conflict_case

    def verify(self, text: str) -> bool:
        """Verify the span offsets match the expected value."""
        return text[self.start:self.end] == self.value


@dataclass
class Record:
    """A single corpus record with gold-standard PHI annotations."""
    record_id: str
    text: str
    gold_spans: List[GoldSpan] = field(default_factory=list)
    layer: str = LAYER_COMMON              # corpus layer taxonomy (LAYER_* constants)
    jurisdiction: str = "universal"        # "us" | "in" | "eu" | "universal" | ...
    detection_regime: str = DETECTION_REGIME_NER  # record-level regime: rule_applicable | contextual_ner_required | conflict_case
    de_id_tier: str = "identifiable"       # "identifiable" | "limited_data_set" | "safe_harbor"
    risk_tier: str = "minimal"             # ICMR four-tier: less_than_minimal | minimal | minor_increase | more_than_minimal
    vulnerability_tags: List[str] = field(default_factory=list)
    context: str = ""                      # "research" | "fundraising" | "treatment" | "payment" | "operations"
    format: str = "text"
    authority_citations: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "record_id": self.record_id,
            "text": self.text,
            "gold_spans": [asdict(s) for s in self.gold_spans],
            "layer": self.layer,
            "jurisdiction": self.jurisdiction,
            "detection_regime": self.detection_regime,
            "de_id_tier": self.de_id_tier,
            "risk_tier": self.risk_tier,
            "vulnerability_tags": self.vulnerability_tags,
            "context": self.context,
            "format": self.format,
            "authority_citations": self.authority_citations,
            "metadata": self.metadata,
        }

    def verify_spans(self) -> List[str]:
        """Return list of error messages; empty list means all spans verified."""
        errors = []
        for i, span in enumerate(self.gold_spans):
            if not span.verify(self.text):
                actual = self.text[span.start:span.end]
                errors.append(
                    f"Span {i} offset mismatch: expected '{span.value}' at "
                    f"[{span.start}:{span.end}], got '{actual}'"
                )
        return errors


# -----------------------------------------------------------------------------
# Deterministic generator base
# -----------------------------------------------------------------------------

class DeterministicGenerator:
    """Base class for seeded generators.

    Two generators with the same seed produce bitwise-identical output.
    This is the guarantee required for IRB reproducibility attestation.
    """

    def __init__(self, seed: int):
        self.seed = seed
        self.rng = random.Random(seed)

    def fresh(self, subseed: str) -> random.Random:
        """Derive a fresh PRNG from a subseed string (reproducible)."""
        h = hashlib.sha256(f"{self.seed}:{subseed}".encode()).hexdigest()
        return random.Random(int(h[:16], 16))

    def record_id(self, layer: str, index: int) -> str:
        return f"{layer}_{index:06d}"

    def annotate(
        self,
        text: str,
        spans_spec: List[Tuple[str, str, str, str, str, str]],
    ) -> List[GoldSpan]:
        """Build GoldSpan list from (value, category, hipaa_cat, jurisdiction, authority, detection_regime) tuples.

        Finds the value in text and records exact offsets.
        Returns verified spans only (raises if any spec value not found).
        detection_regime defaults to DETECTION_REGIME_NER if the tuple has only 5 elements (backward compat).
        """
        spans = []
        for spec in spans_spec:
            if len(spec) == 6:
                value, category, hipaa_cat, jurisdiction, authority, regime = spec
            else:
                value, category, hipaa_cat, jurisdiction, authority = spec
                regime = DETECTION_REGIME_NER
            start = text.find(value)
            if start < 0:
                raise ValueError(f"Value '{value}' not found in text: {text[:200]}")
            end = start + len(value)
            spans.append(GoldSpan(
                start=start,
                end=end,
                category=category,
                hipaa_category=hipaa_cat if hipaa_cat else None,
                jurisdiction=jurisdiction,
                authority=authority,
                value=value,
                entity_type=category,
                detection_regime=regime,
            ))
        return spans


# -----------------------------------------------------------------------------
# Corpus hashing and manifest
# -----------------------------------------------------------------------------

def hash_corpus(records: Iterable[Record]) -> str:
    """Stable SHA-256 hash of the corpus content (for MANIFEST.json)."""
    h = hashlib.sha256()
    for r in sorted(records, key=lambda x: x.record_id):
        h.update(json.dumps(r.to_dict(), sort_keys=True).encode())
    return h.hexdigest()


def write_jsonl(records: Iterable[Record], path: Path) -> int:
    """Write records to JSONL. Returns record count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r.to_dict(), sort_keys=True, ensure_ascii=False))
            f.write("\n")
            count += 1
    return count


# -----------------------------------------------------------------------------
# Checksum validators for identifier formats
# -----------------------------------------------------------------------------

def verhoeff_check(digits: str) -> bool:
    """Verhoeff algorithm for Aadhaar validation."""
    d = [
        [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
        [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
        [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
        [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
        [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
        [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
        [6, 5, 9, 8, 7, 1, 0, 4, 3, 2],
        [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
        [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
        [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
    ]
    p = [
        [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
        [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
        [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
        [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
        [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
        [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
        [2, 7, 9, 3, 8, 0, 6, 4, 1, 5],
        [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
    ]
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = d[c][p[i % 8][int(ch)]]
    return c == 0


def verhoeff_make(digits_11: str) -> str:
    """Append Verhoeff check digit to 11-digit string."""
    d = [
        [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
        [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
        [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
        [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
        [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
        [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
        [6, 5, 9, 8, 7, 1, 0, 4, 3, 2],
        [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
        [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
        [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
    ]
    p = [
        [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
        [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
        [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
        [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
        [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
        [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
        [2, 7, 9, 3, 8, 0, 6, 4, 1, 5],
        [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
    ]
    inv = [0, 4, 3, 2, 1, 5, 6, 7, 8, 9]
    c = 0
    # compute over reversed with "0" prepended (placeholder for check digit)
    for i, ch in enumerate(reversed("0" + digits_11)):
        c = d[c][p[i % 8][int(ch)]]
    return digits_11 + str(inv[c])


def luhn_check(digits: str) -> bool:
    """Luhn for credit card validation."""
    digits = [int(d) for d in digits if d.isdigit()]
    checksum = 0
    for i, digit in enumerate(reversed(digits)):
        if i % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def luhn_make(digits_without_check: str) -> str:
    """Append Luhn check digit."""
    digits = [int(d) for d in digits_without_check]
    checksum = 0
    for i, digit in enumerate(reversed(digits)):
        if i % 2 == 0:  # reversed, so doubled positions are the even-indexed ones
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    check = (10 - (checksum % 10)) % 10
    return digits_without_check + str(check)
