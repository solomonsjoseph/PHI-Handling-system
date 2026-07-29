"""Jurisdiction packs — first-class regulation-awareness.

What counts as PHI / PII depends on the regulation that applies. HIPAA
(45 CFR 164.514) enumerates 18 categorized identifiers and mandates
age-90+ aggregation; GDPR (EU/UK) uses a broad "personal data" definition
with no numeric-age rule; DPDPA (India), PIPEDA (Canada), and LGPD (Brazil)
each carry their own specific numeric identifiers (Aadhaar, PAN, SIN, CPF).

Every session is tagged with a jurisdiction (``session.jurisdiction``).
The classifier prompt, the Publish Guard, and the attestation all read
their rules from the corresponding ``JurisdictionPack`` so a pattern that
is meaningful under HIPAA (e.g. AGE_OVER_89) does not fire under GDPR
where age is not itself an identifier.

Only ``us`` (US-HIPAA) is fully populated today. Other packs are declared
as stubs so ``GET /api/jurisdictions`` surfaces the roadmap and so an
operator who picks a stub jurisdiction receives a clear "not-yet-supported"
signal instead of silently getting HIPAA rules applied.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class GuardPattern:
    """One deterministic PHI/PII detector.

    Non-conditional patterns fire on any cell they match. Conditional
    patterns fire only when the column's identifier category matches
    ``column_categories`` OR the cell text contains a ``cell_anchors`` token.
    Conditional gating exists because HIPAA (and every other regulation)
    describes identifier TYPES, not shapes: a heart-rate of 95 is not an
    identifier under any regulation, so the guard must not treat a bare
    "95" as one.
    """
    pid: str
    category: str
    regex: re.Pattern[str]
    conditional: bool = False
    column_categories: frozenset[str] = frozenset()
    cell_anchors: re.Pattern[str] | None = None


@dataclass(frozen=True)
class JurisdictionPack:
    """Full regulation-aware ruleset for a jurisdiction."""
    id: str                     # e.g. "us", "eu", "in", "ca", "br"
    label: str                  # human-readable label
    regulation: str             # e.g. "HIPAA Privacy Rule 45 CFR 164.514(b)(2)(i)"
    supported: bool             # False for stubs on the roadmap
    identifier_categories: dict[str, str]  # letter -> description
    age_aggregation_threshold: int | None  # HIPAA: 89. None = not required
    restricted_zip3_prefixes: frozenset[str] = frozenset()
    patterns: tuple[GuardPattern, ...] = field(default_factory=tuple)
    notes: str = ""

    def pattern_ids(self) -> list[str]:
        return [p.pid for p in self.patterns]


# ---- Shared universal patterns ------------------------------------------
#
# These fire under every jurisdiction because their shapes are effectively
# unique to the identifier they represent (email, URL, IP, SSN-shaped
# numbers embedded in text). They are safe to include even in stubs.

_UNIVERSAL_EMAIL = GuardPattern(
    pid="EMAIL", category="F",
    regex=re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[A-Za-z]{2,}\b"),
)
_UNIVERSAL_URL = GuardPattern(
    pid="URL", category="N",
    regex=re.compile(r"\bhttps?://[^\s,\"']{3,}", re.IGNORECASE),
)
_UNIVERSAL_IPV4 = GuardPattern(
    pid="IPV4", category="O",
    regex=re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
)
_UNIVERSAL_IPV6 = GuardPattern(
    pid="IPV6", category="O",
    regex=re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b"),
)
_UNIVERSAL_IMAGE_REF = GuardPattern(
    pid="IMAGE_REF", category="Q",
    regex=re.compile(r"\b[A-Za-z0-9_\-/.]+\.(?:jpe?g|png|bmp|tiff|heic|heif)\b", re.IGNORECASE),
)
_UNIVERSAL_BIOMETRIC_HASH = GuardPattern(
    pid="BIOMETRIC_HASH", category="P",
    regex=re.compile(
        r"\b(?:fingerprint|iris|biometric|voice[_ ]?print)[:=\s]*[A-Fa-f0-9]{16,}\b",
        re.IGNORECASE,
    ),
)
_UNIVERSAL_DNA_PROFILE = GuardPattern(
    pid="DNA_PROFILE", category="P",
    regex=re.compile(
        r"\b(?:dna|str)[_ ]?(?:profile|locus)[:=\s]*[A-Z0-9\-]{8,}\b", re.IGNORECASE
    ),
)
_UNIVERSAL_DEVICE_SERIAL = GuardPattern(
    # Prefix-anchored ("SN...") so it cannot collide with generic study codes.
    pid="DEVICE_SERIAL", category="M",
    regex=re.compile(r"\b[Ss][Nn][:\- ]*[A-Z0-9\-]{6,}\b"),
)


# ---- US / HIPAA pack (fully populated) ----------------------------------

_US_HIPAA_PATTERNS: tuple[GuardPattern, ...] = (
    GuardPattern(
        pid="SSN", category="G",
        regex=re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    ),
    GuardPattern(
        pid="PHONE_US", category="D",
        regex=re.compile(
            r"\b(?:\+?1[\s\-.]?)?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}\b"
        ),
    ),
    _UNIVERSAL_EMAIL,
    GuardPattern(
        pid="DATE_FULL_ISO", category="C",
        regex=re.compile(r"\b(19|20)\d{2}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b"),
    ),
    GuardPattern(
        pid="DATE_FULL_US", category="C",
        regex=re.compile(r"\b(0[1-9]|1[0-2])/(0[1-9]|[12]\d|3[01])/(19|20)\d{2}\b"),
    ),
    GuardPattern(
        # HIPAA-restricted ZIP3 prefixes (17 low-population ZIP prefixes)
        pid="RESTRICTED_ZIP3", category="B",
        regex=re.compile(
            r"\b(036|059|063|102|203|556|692|790|821|823|830|831|878|879|884|890|893)\d{2}\b"
        ),
    ),
    # AGE_OVER_89 — conditional. HIPAA §164.514(b)(2)(i)(C) mandates
    # aggregating ages 90+ into a single "90 or older" category. The
    # guard should only fire on cells that are actually ages, not on
    # arbitrary numbers 90-99 that happen to be heart rates, blood
    # pressures, glucose readings, etc.
    GuardPattern(
        pid="AGE_OVER_89", category="C",
        regex=re.compile(r"\b9[0-9]\b(?![\+\-])"),
        conditional=True,
        column_categories=frozenset({"C"}),
        cell_anchors=re.compile(
            r"\b(?:age[sd]?|y/?o|yrs?|years?\s+old|elderly)\b", re.IGNORECASE
        ),
    ),
    _UNIVERSAL_URL,
    _UNIVERSAL_IPV4,
    _UNIVERSAL_IPV6,
    # LICENSE_PLATE — conditional. HIPAA [L] regulates vehicle identifiers.
    # Study arm codes ("ARM 001") share the shape so gate on column [L] or
    # in-cell "plate"/"vehicle" anchor.
    GuardPattern(
        pid="LICENSE_PLATE", category="L",
        regex=re.compile(r"\b[A-Z]{2,3}[- ]?\d{3,4}\b"),
        conditional=True,
        column_categories=frozenset({"L"}),
        cell_anchors=re.compile(
            r"\b(?:plate|license|licence|tag|vehicle)\b", re.IGNORECASE
        ),
    ),
    # IMEI — conditional. HIPAA [M] regulates device identifiers. Long
    # barcodes and study IDs share the 15-digit shape so gate on column
    # [M] or in-cell "imei"/"device id" anchor.
    GuardPattern(
        pid="IMEI", category="M",
        regex=re.compile(r"(?<!\d)\d{15}(?!\d)"),
        conditional=True,
        column_categories=frozenset({"M"}),
        cell_anchors=re.compile(
            r"\b(?:imei|device[_ ]?id|handset)\b", re.IGNORECASE
        ),
    ),
    _UNIVERSAL_DEVICE_SERIAL,
    _UNIVERSAL_IMAGE_REF,
    _UNIVERSAL_BIOMETRIC_HASH,
    _UNIVERSAL_DNA_PROFILE,
    GuardPattern(
        pid="NPI", category="K",
        regex=re.compile(r"\bNPI[:\- ]*\d{10}\b"),
    ),
    GuardPattern(
        pid="DEA", category="K",
        regex=re.compile(r"\bDEA[:\- ]*[A-Z]{2}\d{7}\b"),
    ),
)


US_HIPAA = JurisdictionPack(
    id="us",
    label="United States — HIPAA Safe Harbor",
    regulation="HIPAA Privacy Rule 45 CFR 164.514(b)(2)(i)",
    supported=True,
    identifier_categories={
        "A": "Names of individuals or relatives",
        "B": "Geographic subdivisions smaller than state (ZIP3 restricted)",
        "C": "Dates directly related to individual + ages >89",
        "D": "Telephone numbers",
        "E": "Fax numbers",
        "F": "Email addresses",
        "G": "Social Security numbers",
        "H": "Medical record numbers",
        "I": "Health plan beneficiary numbers",
        "J": "Account numbers",
        "K": "Certificate / license numbers (NPI, DEA)",
        "L": "Vehicle identifiers and serial numbers, including license plate numbers",
        "M": "Device identifiers and serial numbers",
        "N": "Web URLs",
        "O": "IP addresses",
        "P": "Biometric identifiers (fingerprints, voice prints, DNA profiles)",
        "Q": "Full-face photographs and comparable images",
        "R": "Any other unique identifying number, characteristic, or code",
    },
    age_aggregation_threshold=89,
    restricted_zip3_prefixes=frozenset({
        "036", "059", "063", "102", "203", "556", "692", "790", "821", "823",
        "830", "831", "878", "879", "884", "890", "893",
    }),
    patterns=_US_HIPAA_PATTERNS,
    notes=(
        "Ages 0-89 retained as exact integers; ages 90+ aggregated to '90 or "
        "older' per §164.514(b)(2)(i)(C). Dates keep year only. ZIP truncates "
        "to 3 digits (all 5 digits allowed if population > 20,000 and not on "
        "the 17-prefix restricted list)."
    ),
)


# ---- EU / UK GDPR pack (stub) -------------------------------------------
#
# GDPR Art. 4(1) treats "any information relating to an identified or
# identifiable natural person" as personal data. There is no numeric-age
# aggregation rule (age is data, not itself categorized). IP addresses,
# device identifiers, and online identifiers ARE personal data. Only the
# universally-shaped patterns fire; a full pack would need contextual
# anonymisation policy (k-anonymity, DPIA-driven suppression) rather
# than fixed regex rules.

_EU_UK_STUB_PATTERNS: tuple[GuardPattern, ...] = (
    _UNIVERSAL_EMAIL,
    _UNIVERSAL_URL,
    _UNIVERSAL_IPV4,
    _UNIVERSAL_IPV6,
    _UNIVERSAL_IMAGE_REF,
    _UNIVERSAL_BIOMETRIC_HASH,
    _UNIVERSAL_DNA_PROFILE,
    _UNIVERSAL_DEVICE_SERIAL,
)


EU_GDPR = JurisdictionPack(
    id="eu",
    label="European Union — GDPR",
    regulation="Regulation (EU) 2016/679 (GDPR) Art. 4(1) & Art. 9",
    supported=False,
    identifier_categories={
        "personal_data": "Any information relating to an identified or identifiable natural person (Art. 4(1))",
        "special_category": "Racial/ethnic origin, health, genetic, biometric data (Art. 9)",
        "online_identifier": "IP address, cookie, device fingerprint (Recital 30)",
    },
    age_aggregation_threshold=None,  # no 90+ rule
    patterns=_EU_UK_STUB_PATTERNS,
    notes=(
        "Full pack requires k-anonymity / L-diversity policy engine + DPIA-"
        "driven suppression. Only shape-unambiguous patterns are enforced "
        "in this stub. Age is NOT itself an identifier under GDPR."
    ),
)


UK_GDPR = JurisdictionPack(
    id="uk", label="United Kingdom — UK GDPR + Data Protection Act 2018",
    regulation="UK GDPR (retained EU law) + DPA 2018",
    supported=False,
    identifier_categories=EU_GDPR.identifier_categories,
    age_aggregation_threshold=None,
    patterns=_EU_UK_STUB_PATTERNS,
    notes="Post-Brexit UK GDPR mirrors EU GDPR; identical identifier basis.",
)


# ---- India DPDPA 2023 pack (stub) ---------------------------------------
#
# DPDPA 2023 uses a broad "personal data" definition. India-specific
# identifiers regulated separately: Aadhaar (UIDAI Act — 12-digit ID),
# PAN (Income-Tax Act — AAAAA9999A shape).

_IN_DPDPA_STUB_PATTERNS: tuple[GuardPattern, ...] = (
    _UNIVERSAL_EMAIL,
    _UNIVERSAL_URL,
    _UNIVERSAL_IPV4,
    _UNIVERSAL_IPV6,
    _UNIVERSAL_IMAGE_REF,
    _UNIVERSAL_BIOMETRIC_HASH,
    _UNIVERSAL_DNA_PROFILE,
    _UNIVERSAL_DEVICE_SERIAL,
    GuardPattern(
        # Aadhaar: 12 digits, often shown 4-4-4 grouped or unspaced.
        pid="AADHAAR", category="in_national_id",
        regex=re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"),
    ),
    GuardPattern(
        # PAN: 5 letters + 4 digits + 1 letter (fixed shape).
        pid="PAN_IN", category="in_tax_id",
        regex=re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),
    ),
)


IN_DPDPA = JurisdictionPack(
    id="in", label="India — DPDPA 2023",
    regulation="Digital Personal Data Protection Act, 2023",
    supported=False,
    identifier_categories={
        "personal_data": "Any data about an identifiable individual",
        "in_national_id": "Aadhaar (12-digit UIDAI ID)",
        "in_tax_id": "PAN (Permanent Account Number)",
    },
    age_aggregation_threshold=None,
    patterns=_IN_DPDPA_STUB_PATTERNS,
    notes="Aadhaar and PAN patterns added. Full pack pending India-specific classifier prompt.",
)


# ---- Canada PIPEDA pack (stub) ------------------------------------------

_CA_PIPEDA_STUB_PATTERNS: tuple[GuardPattern, ...] = (
    _UNIVERSAL_EMAIL, _UNIVERSAL_URL, _UNIVERSAL_IPV4, _UNIVERSAL_IPV6,
    _UNIVERSAL_IMAGE_REF, _UNIVERSAL_BIOMETRIC_HASH, _UNIVERSAL_DNA_PROFILE,
    _UNIVERSAL_DEVICE_SERIAL,
    GuardPattern(
        # SIN: 9 digits, often shown 3-3-3 grouped.
        pid="SIN_CA", category="ca_national_id",
        regex=re.compile(r"\b\d{3}[- ]?\d{3}[- ]?\d{3}\b"),
    ),
)

CA_PIPEDA = JurisdictionPack(
    id="ca", label="Canada — PIPEDA", regulation="Personal Information Protection and Electronic Documents Act",
    supported=False,
    identifier_categories={
        "personal_information": "Information about an identifiable individual",
        "ca_national_id": "Social Insurance Number (SIN)",
    },
    age_aggregation_threshold=None,
    patterns=_CA_PIPEDA_STUB_PATTERNS,
    notes="SIN pattern added; full pack pending Canada-specific classifier prompt.",
)


# ---- Brazil LGPD pack (stub) --------------------------------------------

_BR_LGPD_STUB_PATTERNS: tuple[GuardPattern, ...] = (
    _UNIVERSAL_EMAIL, _UNIVERSAL_URL, _UNIVERSAL_IPV4, _UNIVERSAL_IPV6,
    _UNIVERSAL_IMAGE_REF, _UNIVERSAL_BIOMETRIC_HASH, _UNIVERSAL_DNA_PROFILE,
    _UNIVERSAL_DEVICE_SERIAL,
    GuardPattern(
        # CPF: 11 digits, often shown 3.3.3-2 grouped.
        pid="CPF_BR", category="br_national_id",
        regex=re.compile(r"\b\d{3}[.\s]?\d{3}[.\s]?\d{3}[- ]?\d{2}\b"),
    ),
)

BR_LGPD = JurisdictionPack(
    id="br", label="Brazil — LGPD", regulation="Lei Geral de Proteção de Dados (Lei 13.709/2018)",
    supported=False,
    identifier_categories={
        "dado_pessoal": "Personal data — any information about an identifiable natural person",
        "br_national_id": "CPF (Cadastro de Pessoas Físicas)",
    },
    age_aggregation_threshold=None,
    patterns=_BR_LGPD_STUB_PATTERNS,
    notes="CPF pattern added; full pack pending Brazil-specific classifier prompt.",
)


# ---- Registry -----------------------------------------------------------

REGISTRY: dict[str, JurisdictionPack] = {
    "us": US_HIPAA,
    "eu": EU_GDPR,
    "uk": UK_GDPR,
    "in": IN_DPDPA,
    "ca": CA_PIPEDA,
    "br": BR_LGPD,
}


def get_pack(jurisdiction: str | None) -> JurisdictionPack:
    """Return the jurisdiction pack for ``jurisdiction`` (defaults to US)."""
    key = (jurisdiction or "us").strip().lower()
    return REGISTRY.get(key, US_HIPAA)


def list_packs() -> list[dict[str, Any]]:
    """Summary suitable for ``GET /api/jurisdictions``."""
    return [
        {
            "id": p.id,
            "label": p.label,
            "regulation": p.regulation,
            "supported": p.supported,
            "age_aggregation_threshold": p.age_aggregation_threshold,
            "pattern_count": len(p.patterns),
            "identifier_categories": p.identifier_categories,
            "notes": p.notes,
        }
        for p in REGISTRY.values()
    ]
