"""Header-only PHI review support for study dataset intake.

This module deliberately works from study privacy configuration and dataset
headers only. It must not read row values, emit synthetic values, or execute
generated transform code.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
try:
    from enum import StrEnum
except ImportError:
    class StrEnum(str, Enum): pass  # py3.9 compat
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import yaml

from phi_engine.utils.logging_system import get_logger

logger = get_logger(__name__)

__all__ = [
    "Action",
    "FormReviewApproval",
    "HeaderClassification",
    "HeldReason",
    "OfficialSourceRejected",
    "PureTransformValidation",
    "RuleBundle",
    "StudyPrivacyConfig",
    "classify_headers",
    "is_phi_risky_header",
    "load_sot_variable_signals",
    "load_study_privacy_config",
    "refresh_jurisdiction_rules",
    "review_form_headers",
    "validate_official_source_url",
    "validate_pure_transform_source",
    "verify_approval_payload",
]


class OfficialSourceRejected(ValueError):  # noqa: N818 - public test contract.
    """Raised when a jurisdiction rule source is not an official source."""


class Action(StrEnum):
    """Allowed PHI handling actions, ordered by review strictness.

    Dual interpretation of SUPPRESS (A2 note): SUPPRESS on free-text headers
    (comment, note, narrative, specify, explain) is realized as a priority-0
    force-drop in the scrubber (phi_scrub.py line ~1768), overriding any keep
    rule. SUPPRESS on numeric fields (contacts, counts) is realized as
    small-cell clamping via suppress_small_cell_fields config. In both cases,
    the principle is "do not publish raw" — the scrubber chooses the appropriate
    method by field type. The audit ledger records the applied action
    (drop or small_cell_clamp), NOT the decision (SUPPRESS).
    """

    KEEP = "keep"
    SUPPRESS = "suppress"
    CAP = "cap"
    GENERALIZE = "generalize"
    JITTER_DATE = "jitter_date"
    PSEUDONYMIZE = "pseudonymize"
    DROP = "drop"


_ACTION_RANK: dict[Action, int] = {
    Action.KEEP: 0,
    Action.SUPPRESS: 1,
    Action.CAP: 2,
    Action.GENERALIZE: 3,
    Action.JITTER_DATE: 4,
    Action.PSEUDONYMIZE: 5,
    Action.DROP: 6,
}

# Best-practice METHOD per action (Note 8): the classification record names the
# technique the scrub will apply (e.g. SANT for date jitter) so the IRB ledger
# shows WHICH method protects each column. Header/metadata only — never config
# parameters (that would couple classification to scrub config / row values).
# Keep these NAMES in sync with phi_scrub._method_for_action.
_ACTION_METHOD: dict[Action, str | None] = {
    Action.KEEP: None,
    Action.SUPPRESS: "small_cell_clamp",
    Action.CAP: "threshold_cap",
    Action.GENERALIZE: "generalization_map",
    Action.JITTER_DATE: "SANT_date_jitter",
    Action.PSEUDONYMIZE: "HMAC-SHA256",
    Action.DROP: "field_removal",
}


@dataclass(frozen=True)
class StudyPrivacyConfig:
    """Maintainer-owned study privacy review configuration."""

    study_dir: Path
    jurisdictions: tuple[str, ...]
    rule_refresh: str
    conflict_policy: str
    max_synthetic_attempts: int
    approval_mode: str
    parallelism_mode: str
    # ISO date (YYYY-MM-DD) the maintainer asserts the study data is current as
    # of (Note 15). OPTIONAL — existing studies lack it; absent => ``None`` with
    # a logged warning. A maintainer must set it in ``_study_privacy.yaml``; it
    # is a factual data-recency claim and is never fabricated by the loader.
    data_as_of: str | None = None
    # kanon_publish_gate: optional parsed block; publish-time pyCANON DEFERRED (2026-06).
    kanon_publish_gate: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HeaderRule:
    """One header-name classification rule."""

    id: str
    jurisdiction: str
    action: Action
    patterns: tuple[re.Pattern[str], ...]
    reason: str


@dataclass(frozen=True)
class RuleBundle:
    """Resolved jurisdiction rules and their official-source provenance."""

    source_mode: str
    rules_sha256: str
    sources: tuple[dict[str, str], ...]
    rules: tuple[HeaderRule, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "source_mode": self.source_mode,
            "rules_sha256": self.rules_sha256,
            "sources": list(self.sources),
            "rules": [
                {
                    "id": rule.id,
                    "jurisdiction": rule.jurisdiction,
                    "action": rule.action.value,
                    "reason": rule.reason,
                }
                for rule in self.rules
            ],
        }


@dataclass(frozen=True)
class HeaderClassification:
    """Classification for a single dataset header."""

    header: str
    action: Action
    matched_rules: tuple[str, ...]
    jurisdictions: tuple[str, ...]
    reasons: tuple[str, ...]
    # Note 8: the best-practice method that will protect this column (e.g.
    # SANT_date_jitter), or None for KEEP. Populated from _ACTION_METHOD.
    method: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Return audit-safe JSON with header metadata only, never values."""
        return {
            "header": self.header,
            "action": self.action.value,
            "matched_rules": list(self.matched_rules),
            "jurisdictions": list(self.jurisdictions),
            "reasons": list(self.reasons),
            "method": self.method,
        }


@dataclass(frozen=True)
class PureTransformValidation:
    """Static validation outcome for generated transform source."""

    ok: bool
    errors: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": list(self.errors)}


@dataclass(frozen=True)
class HeldReason:
    """Structured note written when a form is held (e.g. an adversarial probe fails).

    All three fields are required by the operator-review contract:
    - ``what_was_tried`` — description of the classification operations performed
    - ``what_was_ambiguous`` — which aspect of the header/rule set was not resolved
    - ``what_would_resolve`` — concrete information or action that would unblock the form

    Header-name metadata only; never includes row values.
    """

    what_was_tried: str
    what_was_ambiguous: str
    what_would_resolve: str

    def to_json(self) -> dict[str, str]:
        return {
            "what_was_tried": self.what_was_tried,
            "what_was_ambiguous": self.what_was_ambiguous,
            "what_would_resolve": self.what_would_resolve,
        }


@dataclass(frozen=True)
class FormReviewApproval:
    """Form-level review decision safe to serialize into audit ledgers."""

    form_name: str
    status: str
    # ``attempts`` = number of adversarial-probe evaluations performed. The probe
    # is a DETERMINISTIC correctness check on the rule bundle, so it is evaluated
    # exactly ONCE (retrying could never change a deterministic result) — this
    # value is therefore always 1. Retained as a stable audit-schema field;
    # ``max_synthetic_attempts`` in _study_privacy.yaml no longer drives a loop.
    attempts: int
    actions: dict[str, str]
    classifications: tuple[HeaderClassification, ...]
    reasons: tuple[str, ...]
    rule_bundle_sha256: str
    source_mode: str
    held_reason: HeldReason | None = None
    # Direct-identifier columns the scrub must DROP even though the name-rules /
    # broad keeps would publish them raw — signatures, initials, free-text notes,
    # and any column the PDF-aware SoT flags PHI. The scrub force-drops these
    # (see phi_scrub _scrub_row priority-0). Column NAMES only — never values.
    force_drop_headers: tuple[str, ...] = ()
    # Note 9: value-free records of AI header→rule alignments applied to this
    # form's uncovered headers (each already a verified AlignedRule.to_json()).
    # Empty unless the AI-alignment opt-in is enabled.
    aligned_rules: tuple[dict[str, Any], ...] = ()

    def to_json(self) -> dict[str, Any]:
        """Return a payload with headers/actions only; no row or fake values."""
        payload: dict[str, Any] = {
            "form_name": self.form_name,
            "status": self.status,
            "attempts": self.attempts,
            "actions": dict(self.actions),
            "classifications": [item.to_json() for item in self.classifications],
            "reasons": list(self.reasons),
            "rule_bundle_sha256": self.rule_bundle_sha256,
            "source_mode": self.source_mode,
            "force_drop_headers": list(self.force_drop_headers),
        }
        # Emit aligned_rules ONLY when present, so a default-off run (no AI
        # alignment) produces a byte-identical approval payload to the pre-feature
        # pipeline (Note 9 re-audit finding: avoid an always-empty key).
        if self.aligned_rules:
            payload["aligned_rules"] = [dict(r) for r in self.aligned_rules]
        if self.held_reason is not None:
            payload["held_reason"] = self.held_reason.to_json()
        return payload


_SUPPORTED_JURISDICTIONS = frozenset({"USA", "INDIA"})
_SUPPORTED_REFRESH_MODES = frozenset({"online_preferred", "pinned_only"})
_SUPPORTED_CONFLICT_POLICIES = frozenset({"strictest_wins"})

_OFFICIAL_SOURCE_HOSTS = frozenset(
    {
        "hhs.gov",
        "www.hhs.gov",
        "ecfr.gov",
        "www.ecfr.gov",
        "indiacode.nic.in",
        "www.indiacode.nic.in",
        "icmr.gov.in",
        "www.icmr.gov.in",
        "uidai.gov.in",
        "www.uidai.gov.in",
        "meity.gov.in",
        "www.meity.gov.in",
    }
)

_PINNED_SOURCES: tuple[dict[str, str], ...] = (
    {
        "jurisdiction": "USA",
        "title": "eCFR HIPAA de-identification rule",
        "url": "https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-C/part-164/subpart-E/section-164.514",
    },
    {
        "jurisdiction": "USA",
        "title": "HHS HIPAA de-identification guidance",
        "url": "https://www.hhs.gov/hipaa/for-professionals/privacy/special-topics/de-identification/index.html",
    },
    {
        "jurisdiction": "INDIA",
        "title": "Digital Personal Data Protection Act, 2023",
        "url": "https://www.indiacode.nic.in/indiacode/handle/123456789/22037",
    },
    {
        "jurisdiction": "INDIA",
        "title": "ICMR official guidelines index",
        "url": "https://www.icmr.gov.in/guidelines",
    },
    {
        "jurisdiction": "INDIA",
        "title": "Aadhaar Act and UIDAI legal framework",
        "url": "https://uidai.gov.in/en/about-uidai/legal-framework/2033-aadhaar-targeted-delivery-of-financial-and-other-subsidies%2C-benefits-and-services-act%2C-2016.html",
    },
)


def _compile_many(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern, re.I) for pattern in patterns)


_PINNED_RULE_SPECS: tuple[dict[str, object], ...] = (
    {
        "id": "usa_safe_harbor_direct_identifiers",
        "jurisdiction": "USA",
        "action": Action.DROP,
        "reason": "HIPAA Safe Harbor direct identifier header.",
        "patterns": (
            r"\b(name|first[_ -]?name|last[_ -]?name|full[_ -]?name)\b",
            r"\b(email|e[-_ ]?mail)\b",
            r"\b(phone|telephone|mobile|cell|fax)\b",
            r"\b(ssn|social[_ -]?security)\b",
            r"\b(address|street|city|county|precinct|zip|postal)\b",
            r"\b(url|uri|ip[_ -]?address|photo|image|biometric|finger|voice)\b",
            r"\b(account|license|certificate|vehicle|plate|device[_ -]?serial)\b",
            r"\b(mrn|medical[_ -]?record|health[_ -]?plan|beneficiary)\b",
            # Synthetic-benchmark-driven coverage (2026-06-26, Note 34): direct
            # identifiers the token-boundary rules above missed — person-name
            # suffixes (FNAME/LNAME/MNAME/EMERGNAME/SURNAME), US financial / govt
            # IDs, device/biometric tokens, and contact-info columns. Every pattern
            # below was verified collision-free against all 1,702 Indo-VAP columns
            # (TC_CARD/TC_CARDLOC, ST_LACCNUM excluded by anchoring).
            r"(^|[_ -])[a-z]{0,12}names?\d*$",
            r"\bpassport\b",
            r"\b(medicare|medicaid)\b",
            r"\b(insurance|insur)\b|(^|[_ -])ins[_ -]?id\b|\bmember[_ -]?id\b|\bpolicy[_ -]?(?:no|num|number|id)\b",
            r"(^|[_ -])bank\w*",
            r"\bcredit[_ -]?card\b|(^|[_ -])cc[_ -]?num\d*$|\bcard[_ -]?(?:no|num|number)\b",
            r"(^|[_ -])vin\d*$",
            r"\bdevice\b|\bserial\b|\bfinger\w*",
            r"(^|[_ -])(?:driving[_ -]?licen\w*|dl(?:[_ -]?(?:no|num|number))?)\d*$",
            r"\bcontact[_ -]?(?:line|info|details|phone|number|no)\b",
            r"(^|[_ -])alt[_ -]?(?:mobile|phone|cell|fax)\d*$",
            # A1 (Note 28): staff initials / signatures / lab-technician names /
            # clinic NAMES and data-entry/system artefacts the scrub drops. These
            # are direct identifiers or junk — DROP matches phi_scrub.yaml's
            # surviving drop_fields. PROCSIG (specimen processing-ID signatures)
            # are deliberately EXCLUDED — they pseudonymize as IDs (see the
            # unique-identifier rule). The socioeconomic option-'specify' columns
            # (JOBSP/LANGUAGESP/RELIGIONSP) are KEPT and are NOT matched here.
            r"(^|[_ -])init(?:ial)?s?(?:[_ -]?\d+)?$",
            r"(^|[_ -])sign?(?:[_ -]?\d+)?$",
            r"(?:collsig|linsig|loutsig|reposinsig|collsign)(?:[_ -]?\d+)?$",
            r"(?:linttech|louttech|reposintech)$",
            r"(?:clinname|clinicname|dotsclinname)$",
            r"(^|[_ -])batch",
            r"^remote[_ -]?(?:phn|user|fax)$",
            # NB: classify_headers splits CamelCase on case/digit boundaries, so
            # OrigPgSeq→'orig_pg_seq', FormIDMthd→'form_idmthd' — match the
            # normalized token shape, not the raw CamelCase.
            r"^(?:route[_ -]?to|suspense[_ -]?file|verify[_ -]?wks|orig[_ -]?file|orig[_ -]?pg[_ -]?seq|form[_ -]?id(?:[_ -]?mthd)?)$",
        ),
    },
    {
        "id": "usa_safe_harbor_dates",
        "jurisdiction": "USA",
        "action": Action.JITTER_DATE,
        "reason": "HIPAA Safe Harbor §164.514(b)(2)(i)(C) date element header.",
        "patterns": (
            r"\b(date|datetime|timestamp|time[_ -]?stamp)\b",
            r"(^|[_ -])(dob|dod)([_ -]|$)",
            r"\b(birth|admission|discharge|death)[_ -]?date\b",
            # Date-suffix columns: DAT/DATE (e.g. CBC_VISDAT, ST_COMPDATE) and
            # DTE (e.g. CXR_COMPDTE, HHC_COMPDTE). DTE/DATE were missing and
            # mis-classified those completion-date columns as KEEP — a date leak
            # and a decided-vs-applied mismatch against the scrub's jitter.
            r"(^|[_ -])[a-z0-9]*(date|dat|dte)\d*$",
            # Bare "dt" only as its own token / separator-prefixed (VISIT_DT, DT),
            # NOT as a word ending — otherwise it falsely matches RESPNDT
            # (respondent), VERDICT, etc. and decides jitter for a non-date.
            r"(^|[_ -])dt\d*$",
            # A1 (Note 28): date tokens the generic dat-suffix rule misses —
            # index/treatment-start dates with no trailing 'dat' (ICFDT/TBTXDT),
            # dedup-suffixed dates (…DAT_2), 'Not Done' date flags (…DATND).
            # (Specimen TIMES-of-day and child YEARS-of-birth are NOT here — they
            # are kept, not jittered: a bare time/year is not a jitterable date
            # and not a Safe Harbor date element. Maintainer A1 decision.)
            r"(?:icfdt|tbtxdt)$",
            r"(?:pregoutdat|compdat|procdat|mbrecdat|colldat|lindat|loutdat)(?:_?\d+)?$",
            r"(?:datnd)\d*$",
        ),
    },
    {
        "id": "usa_safe_harbor_age",
        "jurisdiction": "USA",
        "action": Action.CAP,
        "reason": "HIPAA Safe Harbor age-over-89 aggregation header.",
        "patterns": (r"\b(age|years[_ -]?old)\b",),
    },
    {
        "id": "usa_geography_generalization",
        "jurisdiction": "USA",
        "action": Action.GENERALIZE,
        "reason": "HIPAA geography header requiring sub-state generalization.",
        "patterns": (r"\b(village|district|state|country|geo|location|site[_ -]?address)\b",),
    },
    {
        "id": "usa_free_text_suppression",
        "jurisdiction": "USA",
        "action": Action.SUPPRESS,
        "reason": "Free-text header may contain identifiers and needs suppression review.",
        "patterns": (
            r"\b(comments?|notes?|remarks?|narrative|free[_ -]?text|describe|description|specify|other)\b",
            # A1 (Note 28): abbreviated 'specify'/'other'/'explain' free-text
            # write-ins + death-source/cause narrative the scrub drops. The
            # socioeconomic option-'specify' columns (job/language/religion SP)
            # AND the case-control clinical 'specify' columns kept by the ^CC_
            # allowlist (CC_CNCTNDSP/CC_NOPREGTESTSP/CC_HIVLOCSP) are NOT matched.
            r"(?:clinicsp|centersp|clinicoth|otdotsloc|nocardsp)$",
            r"(?:ic_hivlocsp|hc_hivlocsp)$",
            r"(?:dthsrc|dthsrcsp|dthcaussp|fucompadcsp)$",
            r"(?:withdrawexplain|withdraw_?explain|tbtrtsp)$",
            # Household / contact COUNT columns → small-cell suppression.
            # (IS_CONTACTS_6YRS normalizes to 'is_contacts_6_yrs'.)
            r"(?:^|[_ -])contacts(?:[_ -](?:total|6[_ -]?yrs))?$",
        ),
    },
    {
        "id": "usa_unique_study_identifier",
        "jurisdiction": "USA",
        "action": Action.PSEUDONYMIZE,
        "reason": "HIPAA Safe Harbor §164.514(b)(2)(i)(R) unique identifier header.",
        "patterns": (
            r"\b(participant|subject|patient|person|study|record|case)[_ -]?(id|code|key|number|no)\b",
            r"\b(id|identifier|uuid|guid)\b",
            r"(^|[_ -])(?:subj(?:id)?|fid|pid|ptid|hhid|recordid)$",
            # A1 (Note 28): study / specimen / lab / linkage IDs recovered from the
            # over-broad drop rules → PSEUDONYMIZE. Mirrors phi_scrub.yaml id_fields
            # so the decider's PSEUDONYMIZE decision matches the cleaner's HMAC
            # pseudonymization (an ID is pseudonymized, never dropped).
            r"(^|[_ -])(?:tuid|dmcid)(?:chg)?$",
            r"(?:colltid|colltnum|colltmg|procid|lbaccid|laccnum)$",
            # Specimen ACCESSION number (full word; anchored so it cannot match the
            # already-handled ST_LACCNUM "…accnum") → pseudonymize (unique id, like
            # the sibling laccnum). Note 34.
            r"(^|[_ -])accession\d*$",
            # Specimen processing-ID signatures (PAX/PBMC/PLASMA/QTF/SLV/URN/GENO
            # *PROCSIG) → pseudonymize. The bare SC_PROCSIG is a kept clinical
            # flag (^SC_…PROCSIG allowlist), so the prefix is required here.
            r"(?:geno|pax|pbmc|plasma|qtfgit|qtf|slv|urn)procsig$",
            # NB: mbrefid (the reference-lab accession #) is a near-unique
            # per-specimen identifier (cardinality ~0.7-0.9) → pseudonymize. The
            # sibling mblabid is NOT included: despite a shared SoT label
            # "Laboratory accession #", its cardinality is low (7-63 distinct over
            # ~3000 rows, k-anonymous) → a lab code, not a unique identifier, and
            # is correctly KEPT via the phi_scrub keep allowlist.
            r"(?:mbrefid|artidn|diaidn|ocmidn|tbproidn|tbincid|coenrlid)\d*$",
            r"(?:idna|idchgna)$",
            r"(^|[_ -])(?:phcid|ictc|csid)$",
            r"^hhc\d+$",
            r"^remote[_ -]?bid$",
            r"^st_louttrac$",
            r"coe[_ -]?(?:protocol|sponsor)\d*$",
            # Secondary subject / family IDs — the scrub resolver pseudonymizes the
            # distinct ones and drops exact-duplicate re-entries; deciding
            # PSEUDONYMIZE here suppresses a keep_decision under either outcome.
            r"^subjid(?:\d+_?\d*|_\d+)$",
            r"^(?:[a-z]{1,4}[_ -])?fid\d+$",
        ),
    },
    {
        "id": "india_dpdpa_contact_identifiers",
        "jurisdiction": "INDIA",
        "action": Action.DROP,
        "reason": "India personal-data direct contact identifier header.",
        "patterns": (
            r"\b(email|e[-_ ]?mail|phone|telephone|mobile|cell|address|postal|pin[_ -]?code)\b",
            r"\b(passport|voter|pan|bank|account)\b",
            # Ration-card NUMBER is a government ID → drop. The bare ration
            # CATEGORY (APL/BPL/None, e.g. IC_RATION) is socioeconomic, not an
            # identifier — require card/no/number so the category is not flagged.
            r"\bration[_ -]?(?:card|no|num|number)\b",
            # Note 34: India identity numbers the boundary rules above missed —
            # voter EPIC (VOTERID), driving licence (DL), ABHA health-account id,
            # UHID, GSTIN, and a bare PIN/PINCODE postal field. Verified
            # collision-free against Indo-VAP (bare RATION category untouched).
            r"\bvoter|\bepic[_ -]?(?:no|number)?\b",
            r"\babha\b|\buhid\b|\bgst(?:in)?\b",
            r"(^|[_ -])(?:driving[_ -]?licen\w*|dl(?:[_ -]?(?:no|num|number))?)\d*$",
            r"(^|[_ -])pin(?:[_ -]?code)?$",
        ),
    },
    {
        "id": "india_aadhaar_identifier",
        "jurisdiction": "INDIA",
        "action": Action.DROP,
        "reason": "Aadhaar identity number header.",
        "patterns": (
            r"\b(aadhaar|adhar|aadhar|uidai)\b",
            r"\buid[_ -]?(no|number|id)?\b",
        ),
    },
    {
        "id": "india_date_identifier",
        "jurisdiction": "INDIA",
        "action": Action.JITTER_DATE,
        "reason": "Date-like personal-data header.",
        "patterns": (
            r"\b(date|datetime|timestamp|time[_ -]?stamp)\b",
            r"(^|[_ -])(dob|dod)([_ -]|$)",
            # DTE/DATE suffixes included so completion-date columns (CXR_COMPDTE,
            # HHC_COMPDTE) classify as dates, matching the USA rule above.
            r"(^|[_ -])[a-z0-9]*(date|dat|dte)\d*$",
            # Bare "dt" only separator-prefixed (VISIT_DT) — not as a word ending
            # (RESPNDT respondent), matching the USA rule above.
            r"(^|[_ -])dt\d*$",
        ),
    },
    {
        "id": "india_free_text_suppression",
        "jurisdiction": "INDIA",
        "action": Action.SUPPRESS,
        "reason": "Free-text personal-data header needs suppression review.",
        "patterns": (
            r"\b(comments?|notes?|remarks?|narrative|free[_ -]?text|describe|description|specify|other)\b",
        ),
    },
    {
        "id": "india_unique_person_identifier",
        "jurisdiction": "INDIA",
        "action": Action.PSEUDONYMIZE,
        "reason": "Unique person or study identifier header.",
        "patterns": (
            r"\b(participant|subject|patient|person|study|record|case)[_ -]?(id|code|key|number|no)\b",
            r"\b(id|identifier|uuid|guid)\b",
            r"(^|[_ -])(?:subj(?:id)?|fid|pid|ptid|hhid|recordid)$",
        ),
    },
)


def _build_pinned_rules(jurisdictions: tuple[str, ...]) -> tuple[HeaderRule, ...]:
    wanted = set(jurisdictions)
    rules: list[HeaderRule] = []
    for spec in _PINNED_RULE_SPECS:
        jurisdiction = str(spec["jurisdiction"])
        if jurisdiction not in wanted:
            continue
        rules.append(
            HeaderRule(
                id=str(spec["id"]),
                jurisdiction=jurisdiction,
                action=spec["action"],  # type: ignore[arg-type]
                patterns=_compile_many(spec["patterns"]),  # type: ignore[arg-type]
                reason=str(spec["reason"]),
            )
        )
    return tuple(rules)


def _canonical_bundle_payload(
    sources: tuple[dict[str, str], ...],
    rules: tuple[HeaderRule, ...],
) -> dict[str, Any]:
    return {
        "sources": list(sources),
        "rules": [
            {
                "id": rule.id,
                "jurisdiction": rule.jurisdiction,
                "action": rule.action.value,
                "patterns": [pattern.pattern for pattern in rule.patterns],
                "reason": rule.reason,
            }
            for rule in rules
        ],
    }


def _sha256_json(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_header(header: str) -> str:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", header.strip())
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", normalized)
    return normalized.strip("_").lower()


def _header_match_texts(header: str) -> tuple[str, str]:
    normalized = _normalize_header(header)
    return normalized, normalized.replace("_", " ")


# ---------------------------------------------------------------------------
# PHI-risk header detection (audit-completeness coverage hold — Option C)
# ---------------------------------------------------------------------------
# A header that matched NO jurisdiction rule is classified KEEP and would be
# published unscrubbed. Most such headers are benign clinical/derived fields.
# But some PHI-bearing names slip past the jurisdiction rule patterns (e.g.
# "interviewer_remarks", "clinical_notes" — the free-text rule matches "note"
# but not "notes"/"remarks"). These tokens flag a header whose NAME looks like
# PHI so the form is held for human review rather than silently published.
#
# Token-based (split on "_") to avoid substring false positives, plus a small
# set of compound substrings. Because rule-matched headers are already non-KEEP,
# this only ever fires on the escapees — so a broad list cannot over-scrub data,
# it can only route an ambiguous form to a (cheap, non-blocking) human review.
_PHI_RISKY_TOKENS: frozenset[str] = frozenset(
    {
        # person identity
        "name",
        "fname",
        "lname",
        "surname",
        "maiden",
        "initials",
        # free-text / verbatim narrative
        "remark",
        "remarks",
        "note",
        "notes",
        "comment",
        "comments",
        "narrative",
        "verbatim",
        "specify",
        "describe",
        "description",
        # direct contact
        "phone",
        "mobile",
        "telephone",
        "fax",
        "email",
        "whatsapp",
        # geographic subdivisions smaller than state (HIPAA Safe Harbor #2)
        "address",
        "street",
        "village",
        "locality",
        "landmark",
        "pincode",
        "gps",
        "latitude",
        "longitude",
        "geocode",
        # specific government / record identifiers
        "aadhaar",
        "aadhar",
        "pan",
        "passport",
        "voter",
        "mrn",
        "ssn",
        "uid",
        "ration",
        # life-event dates tied to an individual
        "dob",
        "dod",
    }
)
_PHI_RISKY_SUBSTRINGS: tuple[str, ...] = (
    "free_text",
    "freetext",
    "e_mail",
    "phone_number",
    "mobile_number",
    "contact_person",
    "national_id",
    "birth_date",
    "date_of_birth",
    "death_date",
    "date_of_death",
)

# ---------------------------------------------------------------------------
# STRONG direct-identifier subset of the PHI-risk tokens.
#
# These tokens name genuine person identifiers (person name, initials,
# signature, and contact-point identifiers such as phone/fax/email and
# government IDs).  A column in this subset must NOT be cleared benign by
# the mere presence of a printed PDF question ("has_pdf_question=True,
# sot_phi=None"): a real CRF legitimately *asks* for a patient's initials or
# signature, so the SoT will have a pdf_question for it — but that does not
# make the column safe to publish raw.
#
# Weaker/ambiguous tokens (sub-state geography, free-text remarks, life-event
# dates) are NOT in this set; they keep the current clearance behavior where a
# printed clinical question with no SoT PHI action confirms them benign.
# ---------------------------------------------------------------------------
_STRONG_DIRECT_ID_TOKENS: frozenset[str] = frozenset(
    {
        # person names and initials
        "name",
        "fname",
        "lname",
        "surname",
        "maiden",
        "initials",
        # direct contact identifiers
        "phone",
        "mobile",
        "telephone",
        "fax",
        "email",
        "whatsapp",
        # government / biometric identifiers
        "aadhaar",
        "aadhar",
        "pan",
        "passport",
        "voter",
        "mrn",
        "ssn",
        "uid",
    }
)
_STRONG_DIRECT_ID_SUBSTRINGS: tuple[str, ...] = (
    "e_mail",
    "phone_number",
    "mobile_number",
    "national_id",
    # "signature" and "initials" as substrings
    "signature",
    "sign",
)


def _is_strong_direct_id_header(header: str) -> bool:
    """Return True when a header name matches the STRONG direct-identifier subset.

    STRONG direct identifiers (person name/initials/signature, contact identifiers,
    government IDs) must NOT be cleared benign by the mere presence of a printed PDF
    question — a CRF legitimately asks for a patient's initials, but that does not
    make the column safe to publish raw.

    Weaker risk signals (sub-state geography, free-text remarks, life-event dates)
    are deliberately excluded; they keep the normal SoT-question clearance path.
    """
    normalized = _normalize_header(header)
    if not normalized:
        return False
    if any(substr in normalized for substr in _STRONG_DIRECT_ID_SUBSTRINGS):
        return True
    return bool(set(normalized.split("_")) & _STRONG_DIRECT_ID_TOKENS)


def is_phi_risky_header(header: str) -> bool:
    """Return True when a header NAME looks like PHI (name/contact/location/id/free-text).

    Header-name heuristic only — never reads values. Used to hold a form whose
    KEEP-classified column would otherwise be published unscrubbed despite a
    PHI-suspicious name. Errs toward flagging: a false positive costs one human
    glance; a false negative risks publishing PHI.
    """
    normalized = _normalize_header(header)
    if not normalized:
        return False
    if any(substr in normalized for substr in _PHI_RISKY_SUBSTRINGS):
        return True
    return bool(set(normalized.split("_")) & _PHI_RISKY_TOKENS)


# SoT `phi`/`phi_action` values that denote an actual PHI handling (anything but a
# plain keep). Used by the SoT cross-verification to decide whether the SoT — which
# was built from the PDF question + headers — independently considers a variable PHI.
_SOT_PHI_ACTIONS: frozenset[str] = frozenset(
    {
        "pseudonymize",
        "drop",
        "jitter_date",
        "generalize",
        "cap",
        "suppress",
        "band",
        "birthdate_drop",
    }
)


def load_sot_variable_signals(sot_root: Path, form_name: str) -> dict[str, dict[str, object]]:
    """Load per-variable SoT semantic signals for *form_name* — fail-soft to ``{}``.

    The SoT (generated FIRST, from the printed PDF question + dataset headers) is
    an independent source of each variable's MEANING and a PHI recommendation. We
    read the per-form joined query view at
    ``{sot_root}/{stem}/joined/{stem}_joined_query_view.yaml`` (falling back to the
    policy YAML in the audit construction zone) and return
    ``{VAR_UPPER: {"has_pdf_question": bool, "sot_phi":
    str|None, "is_phi": bool}}``. Any missing/unreadable SoT yields ``{}`` so the
    name-only review proceeds unchanged (the SoT is an enhancer, never a hard dep).

    Reads METADATA only (variable names, printed questions, PHI recommendations) —
    never dataset row values.
    """
    stem = Path(form_name).stem
    # Primary: the LLM-facing joined query view in llm_source. Fallback: the policy
    # YAML, which (N3) lives in the AUDIT construction zone, not llm_source — this
    # is trusted pipeline code reading metadata, never an LLM read.
    # sot_root = output/<study>/llm_source/SoT → parents[1] = output/<study>.
    construction_root = sot_root.parents[1] / "audit" / "SoT_construction"
    candidates = (
        sot_root / stem / "joined" / f"{stem}_joined_query_view.yaml",
        construction_root / stem / "pdf" / f"{stem}_policy.yaml",
    )
    for path in candidates:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        variables = data.get("variables", data)
        if not isinstance(variables, dict):
            continue
        signals: dict[str, dict[str, object]] = {}
        for var, entry in variables.items():
            if not isinstance(entry, dict):
                continue
            pdf_raw = entry.get("pdf")
            pdf: dict[str, Any] = pdf_raw if isinstance(pdf_raw, dict) else entry
            ds_raw = entry.get("dataset")
            dataset_blk: dict[str, Any] = ds_raw if isinstance(ds_raw, dict) else {}
            question = pdf.get("pdf_question", pdf.get("question"))
            sot_phi = pdf.get("phi") or dataset_blk.get("phi_action")
            sot_phi_str = str(sot_phi).strip().lower() if sot_phi else None
            signals[str(var).upper()] = {
                "has_pdf_question": bool(question) and str(question).strip().lower() != "null",
                "sot_phi": sot_phi_str,
                "is_phi": sot_phi_str in _SOT_PHI_ACTIONS,
            }
        # Only accept a candidate that actually yielded signals. A present-but-empty
        # joined view (``variables:`` empty, or every entry non-dict) must NOT
        # short-circuit the policy-YAML fallback — otherwise the SoT cross-check
        # silently degrades to a no-op, which (combined with the name-only review's
        # blind spots) can leave a direct-identifier column with no SoT protection.
        if signals:
            return signals
    return {}


def validate_official_source_url(url: str) -> None:
    """Reject non-HTTPS, non-official rule sources."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if parsed.scheme != "https" or host not in _OFFICIAL_SOURCE_HOSTS:
        raise OfficialSourceRejected(f"non-official privacy source rejected: {url}")


def _fetch_source_hash(url: str, *, timeout: float = 2.0) -> str | None:
    """Best-effort official-source freshness probe.

    The downloaded body is not persisted or exposed to the LLM. Only a content
    hash is retained in the run-audit rule bundle.
    """
    try:
        request = Request(url, headers={"User-Agent": "RePORT-AI-Portal/phi-review"})  # noqa: S310 - validated official HTTPS.
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - validated official HTTPS.
            body = response.read(2_000_000)
    except Exception:
        return None
    return hashlib.sha256(body).hexdigest()


def load_study_privacy_config(study_dir: str | Path) -> StudyPrivacyConfig:
    """Load and validate ``_study_privacy.yaml`` from a raw study directory."""
    import phi_engine.config.config as config

    study_path = Path(study_dir)
    # _study_privacy.yaml now lives under config/<study>/ (Note 11), derived from
    # the raw study directory's name and resolved via the config chokepoint.
    config_path = config.study_config_path("_study_privacy.yaml", study=study_path.name)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("_study_privacy.yaml must contain a mapping")

    jurisdictions_raw = raw.get("jurisdictions")
    if not isinstance(jurisdictions_raw, list) or not jurisdictions_raw:
        raise ValueError("jurisdictions must be a non-empty list")
    jurisdictions = tuple(str(item).upper() for item in jurisdictions_raw)
    unsupported = sorted(set(jurisdictions) - _SUPPORTED_JURISDICTIONS)
    if unsupported:
        raise ValueError(f"unsupported jurisdiction(s): {', '.join(unsupported)}")

    rule_refresh = str(raw.get("rule_refresh", "pinned_only"))
    if rule_refresh not in _SUPPORTED_REFRESH_MODES:
        raise ValueError(f"unsupported rule_refresh mode: {rule_refresh}")

    conflict_policy = str(raw.get("conflict_policy", "strictest_wins"))
    if conflict_policy not in _SUPPORTED_CONFLICT_POLICIES:
        raise ValueError(f"unsupported conflict_policy: {conflict_policy}")

    approval = raw.get("approval", {})
    if not isinstance(approval, dict):
        raise ValueError("approval must be a mapping")
    max_attempts = int(approval.get("max_synthetic_attempts", 5))
    if max_attempts < 1:
        raise ValueError("approval.max_synthetic_attempts must be >= 1")

    parallelism = raw.get("parallelism", {})
    if not isinstance(parallelism, dict):
        raise ValueError("parallelism must be a mapping")

    # data_as_of (Note 15): OPTIONAL ISO date. Absent => None + warning (existing
    # studies lack it; do NOT hard-fail). A present value must be a valid
    # YYYY-MM-DD calendar date — a malformed value is a maintainer error and
    # raises.
    data_as_of_raw = raw.get("data_as_of")
    data_as_of: str | None
    if data_as_of_raw is None:
        logger.warning(
            "_study_privacy.yaml for %s has no 'data_as_of' field; data-recency "
            "claim is unset. A maintainer should set it (YYYY-MM-DD).",
            study_path.name,
        )
        data_as_of = None
    else:
        data_as_of = str(data_as_of_raw)
        # Strict YYYY-MM-DD: the regex pins the dashed shape (Python 3.11's
        # date.fromisoformat also accepts basic '20240101'); fromisoformat then
        # validates it is a real calendar date.
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", data_as_of):
            raise ValueError(f"data_as_of must be an ISO date (YYYY-MM-DD); got {data_as_of!r}")
        try:
            date.fromisoformat(data_as_of)
        except ValueError as exc:
            raise ValueError(
                f"data_as_of must be an ISO date (YYYY-MM-DD); got {data_as_of!r}"
            ) from exc

    # kanon_publish_gate: optional; publish-time pyCANON is DEFERRED (2026-06).
    # Parsed when present for future use; absent => {}. Malformed shape raises.
    kanon_raw = raw.get("kanon_publish_gate", {})
    if not isinstance(kanon_raw, dict):
        raise ValueError("kanon_publish_gate must be a mapping")
    kanon_publish_gate: dict[str, Any] = {}
    if kanon_raw:
        enabled = kanon_raw.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("kanon_publish_gate.enabled must be a boolean")
        qis_raw = kanon_raw.get("quasi_identifiers", [])
        if not isinstance(qis_raw, list) or not all(isinstance(q, str) for q in qis_raw):
            raise ValueError("kanon_publish_gate.quasi_identifiers must be a list of column names")
        k_threshold = kanon_raw.get("k_threshold", 5)
        if not isinstance(k_threshold, int) or isinstance(k_threshold, bool) or k_threshold < 1:
            raise ValueError("kanon_publish_gate.k_threshold must be an int >= 1")
        kanon_publish_gate = {
            "enabled": enabled,
            "quasi_identifiers": [str(q) for q in qis_raw],
            "k_threshold": k_threshold,
        }

    return StudyPrivacyConfig(
        study_dir=study_path,
        jurisdictions=jurisdictions,
        rule_refresh=rule_refresh,
        conflict_policy=conflict_policy,
        max_synthetic_attempts=max_attempts,
        approval_mode=str(approval.get("mode", "hybrid")),
        parallelism_mode=str(parallelism.get("mode", "auto")),
        data_as_of=data_as_of,
        kanon_publish_gate=kanon_publish_gate,
    )


def refresh_jurisdiction_rules(
    privacy_config: StudyPrivacyConfig,
    *,
    allow_network: bool = False,
) -> RuleBundle:
    """Resolve jurisdiction rules, falling back to the pinned rule pack.

    ``allow_network`` is intentionally conservative in this first support
    module: official URLs are validated, then the audited pinned rules are used
    when network refresh is disabled or unavailable.
    """
    base_sources = tuple(
        dict(source)
        for source in _PINNED_SOURCES
        if source["jurisdiction"] in set(privacy_config.jurisdictions)
    )
    for source in base_sources:
        validate_official_source_url(source["url"])

    source_mode = "pinned"
    sources: list[dict[str, str]] = []
    if allow_network and privacy_config.rule_refresh == "online_preferred":
        fetched_all = True
        for source in base_sources:
            fetched_hash = _fetch_source_hash(source["url"])
            enriched = dict(source)
            if fetched_hash:
                enriched["fetched_sha256"] = fetched_hash
            else:
                fetched_all = False
            sources.append(enriched)
        source_mode = "latest_official" if fetched_all else "pinned"
        if not fetched_all:
            sources = list(base_sources)
    else:
        sources = list(base_sources)

    rules = _build_pinned_rules(privacy_config.jurisdictions)
    payload = _canonical_bundle_payload(tuple(sources), rules)
    return RuleBundle(
        source_mode=source_mode,
        rules_sha256=_sha256_json(payload),
        sources=tuple(sources),
        rules=rules,
    )


def classify_headers(
    headers: list[str] | tuple[str, ...],
    privacy_config: StudyPrivacyConfig,
    rule_bundle: RuleBundle,
) -> dict[str, HeaderClassification]:
    """Classify headers with strictest-wins conflict handling."""
    if privacy_config.conflict_policy != "strictest_wins":
        raise ValueError(f"unsupported conflict_policy: {privacy_config.conflict_policy}")

    # The set of jurisdictions evaluated for every header in this bundle —
    # used to populate the jurisdictions field on KEEP classifications so the
    # IRB ledger can show *which* regulations were checked and found no PHI rule
    # (an empty jurisdictions list would imply no evaluation took place).
    evaluated_jurisdictions: tuple[str, ...] = tuple(
        dict.fromkeys(rule.jurisdiction for rule in rule_bundle.rules)
    )

    result: dict[str, HeaderClassification] = {}
    for header in headers:
        match_texts = _header_match_texts(header)
        action = Action.KEEP
        matched_rules: list[str] = []
        jurisdictions: list[str] = []
        reasons: list[str] = []

        for rule in rule_bundle.rules:
            if not any(pattern.search(text) for text in match_texts for pattern in rule.patterns):
                continue
            matched_rules.append(rule.id)
            jurisdictions.append(rule.jurisdiction)
            reasons.append(rule.reason)
            if _ACTION_RANK[rule.action] > _ACTION_RANK[action]:
                action = rule.action

        # B-4: for KEEP classifications (no rule matched) populate the jurisdictions
        # field with the full evaluated set so the IRB ledger can show that the header
        # was actively evaluated against every configured jurisdiction and found to be
        # non-PHI under all of them — an empty list would falsely imply no evaluation.
        effective_jurisdictions: tuple[str, ...]
        if action == Action.KEEP:
            effective_jurisdictions = evaluated_jurisdictions
        else:
            effective_jurisdictions = tuple(dict.fromkeys(jurisdictions))

        result[header] = HeaderClassification(
            header=header,
            action=action,
            matched_rules=tuple(matched_rules),
            jurisdictions=effective_jurisdictions,
            reasons=tuple(dict.fromkeys(reasons)),
            method=_ACTION_METHOD.get(action),
        )
    return result


_FORBIDDEN_IMPORT_PREFIXES = (
    "http",
    "logging",
    "os",
    "pathlib",
    "requests",
    "shutil",
    "socket",
    "subprocess",
    "sys",
    "urllib",
)
_FORBIDDEN_CALL_NAMES = {
    "__import__",
    "breakpoint",
    "compile",
    "eval",
    "exec",
    "globals",
    "input",
    "locals",
    "open",
    "print",
}
_FORBIDDEN_ATTR_NAMES = {
    "download",
    "read",
    "run",
    "system",
    "upload",
    "write",
}
_FORBIDDEN_LOGGER_CALLS = {
    "critical",
    "debug",
    "error",
    "exception",
    "info",
    "log",
    "warning",
}


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def validate_pure_transform_source(source: str) -> PureTransformValidation:
    """Statically reject generated transform code with side-effect surfaces."""
    errors: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return PureTransformValidation(ok=False, errors=(f"SyntaxError: {exc.msg}",))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            errors.append("Import statements are not allowed in pure transforms")
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if (
                    alias.name.startswith(_FORBIDDEN_IMPORT_PREFIXES)
                    or root in _FORBIDDEN_IMPORT_PREFIXES
                ):
                    errors.append(f"forbidden import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            errors.append("ImportFrom statements are not allowed in pure transforms")
            root = module.split(".", 1)[0]
            if module.startswith(_FORBIDDEN_IMPORT_PREFIXES) or root in _FORBIDDEN_IMPORT_PREFIXES:
                errors.append(f"forbidden import: {module}")
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            errors.append("with blocks are not allowed in pure transforms")
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            leaf = name.rsplit(".", 1)[-1]
            if name in _FORBIDDEN_CALL_NAMES or leaf in _FORBIDDEN_CALL_NAMES:
                errors.append(f"forbidden call: {name}")
            if leaf in _FORBIDDEN_ATTR_NAMES:
                errors.append(f"forbidden side-effect call: {name}")
            if name.startswith(_FORBIDDEN_IMPORT_PREFIXES):
                errors.append(f"forbidden module call: {name}")
            if name.startswith(("logger.", "log.")) and leaf in _FORBIDDEN_LOGGER_CALLS:
                errors.append(f"forbidden logging call: {name}")
        elif isinstance(node, ast.Attribute):
            full_name = _call_name(node)
            if full_name.startswith(_FORBIDDEN_IMPORT_PREFIXES):
                errors.append(f"forbidden module access: {full_name}")

    unique_errors = tuple(dict.fromkeys(errors))
    return PureTransformValidation(ok=not unique_errors, errors=unique_errors)


def _review_blockers(headers: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    reasons: list[str] = []
    seen: set[str] = set()
    for idx, header in enumerate(headers, start=1):
        normalized = _normalize_header(header)
        if not normalized:
            reasons.append(f"blank header at position {idx}")
            continue
        if normalized in seen:
            reasons.append(f"duplicate normalized header: {normalized}")
        seen.add(normalized)
    return tuple(reasons)


def _adversarial_header_validation(
    privacy_config: StudyPrivacyConfig,
    rule_bundle: RuleBundle,
) -> tuple[str, ...]:
    """Run header-only adversarial probes without emitting fake row values.

    Probe set is jurisdiction-aware (bugfix, standalone refactor 2026-07-07):
    ``synthetic_aadhaar_header`` names an India-specific government ID
    (Aadhaar) that only appears in the INDIA rule pack. A single-jurisdiction
    ``("USA",)`` study is CORRECTLY silent on it -- that is not a rule-bundle
    defect, so probing for it unconditionally made every USA-only form hold
    permanently (verified: ``_build_pinned_rules(("USA",))`` classifies
    ``synthetic_aadhaar_header`` as KEEP, never DROP, by design). The other
    four probes (participant id / visit date / email / culture result) are
    genuinely jurisdiction-agnostic -- verified to pass under ``("USA",)``,
    ``("INDIA",)``, and ``("USA", "INDIA")`` alike -- and stay universal.
    """
    universal_probes = {
        "synthetic_participant_id_header": Action.PSEUDONYMIZE,
        "synthetic_visit_date_header": Action.JITTER_DATE,
        "synthetic_email_header": Action.DROP,
        "synthetic_culture_result_header": Action.KEEP,
    }
    jurisdiction_specific_probes: dict[str, dict[str, Action]] = {
        "INDIA": {"synthetic_aadhaar_header": Action.DROP},
    }
    probes = dict(universal_probes)
    for jurisdiction in privacy_config.jurisdictions:
        probes.update(jurisdiction_specific_probes.get(jurisdiction, {}))

    classified = classify_headers(tuple(probes), privacy_config, rule_bundle)
    failures = [
        f"adversarial header probe failed: {header}"
        for header, expected in probes.items()
        if classified[header].action != expected
    ]
    return tuple(failures)


def _build_held_reason_for_adversarial_exhaustion(
    failures: tuple[str, ...],
    attempts: int,
    privacy_config: StudyPrivacyConfig,
) -> HeldReason:
    """Construct the structured hold note when the adversarial probe check fails.

    The adversarial probe is a deterministic correctness check — it evaluates
    the rule bundle once against a fixed set of synthetic headers. A failure
    cannot be resolved by retrying because the probe is pure pattern matching
    with no I/O or randomness. The hold indicates a rule bundle defect that
    requires operator intervention.

    Header-name metadata only; never references row values.
    """
    failing_probes = ", ".join(
        f.removeprefix("adversarial header probe failed: ") for f in failures
    )
    return HeldReason(
        what_was_tried=(
            f"Ran adversarial header classification probes (1 deterministic evaluation) "
            f"against the {privacy_config.conflict_policy} rule bundle "
            f"(jurisdictions: {', '.join(privacy_config.jurisdictions)}). "
            "Probes use synthetic header names only; no dataset row values were read. "
            "The probe is a pure correctness check — it is not retried because "
            "its result is deterministic given the same rule bundle."
        ),
        what_was_ambiguous=(
            f"The following synthetic probe header(s) were not classified as expected: "
            f"{failing_probes}. "
            "This indicates the loaded rule bundle does not satisfy the minimum "
            "correctness invariants required before any form can be approved."
        ),
        what_would_resolve=(
            "Review the loaded jurisdiction rules in the rule bundle "
            "(rules_sha256 will appear in the approval payload) to confirm that "
            "the patterns for the failing probe categories are present and correct. "
            "Increasing max_synthetic_attempts in _study_privacy.yaml will not resolve "
            "a systematic rule gap — the patterns themselves must be corrected."
        ),
    )


def review_form_headers(
    *,
    form_name: str,
    headers: list[str] | tuple[str, ...],
    privacy_config: StudyPrivacyConfig,
    rule_bundle: RuleBundle,
    sot_signals: dict[str, dict[str, object]] | None = None,
    published_raw_headers: frozenset[str] | None = None,
    confirmed_keep_headers: frozenset[str] = frozenset(),
    aligner: Any = None,
) -> FormReviewApproval:
    """Review one form's headers before any row-value extraction is allowed.

    Adversarial classification probes are evaluated ONCE as a deterministic
    correctness check on the rule bundle.  The probe is pure pattern matching
    with no I/O or randomness — it is not retried because retrying the same
    immutable probe against the same immutable rule bundle cannot change the
    result.  If the probe fails the form is held with a structured
    ``HeldReason`` that records what was tried, what was ambiguous, and what
    information would resolve the hold.  ``attempts`` is always 1.

    ``max_synthetic_attempts`` in ``_study_privacy.yaml`` is loaded and
    validated but no longer drives a loop — it is retained for configuration
    compatibility and to communicate to operators that no retry will help.

    Headers-only invariant: no row values are read at any point.
    """
    # ------------------------------------------------------------------
    # Step 1: Classify the real form headers (deterministic, done once).
    # ------------------------------------------------------------------
    classifications_by_header = classify_headers(headers, privacy_config, rule_bundle)

    # ── N9: AI header→rule alignment for the UNCOVERED set (opt-in) ──
    # Only when an aligner is injected (default None → deterministic behavior,
    # byte-identical to before). For each KEEP header that matched NO pinned rule,
    # the AI proposes a rule binding from the column NAME only; a deterministically
    # verified proposal UPGRADES the KEEP classification to the aligned (stronger)
    # action. This can only ADD protection — KEEP is the weakest action — and any
    # header it cannot align stays KEEP, still covered by the force-drop/hold net.
    aligned_rule_records: tuple[dict[str, Any], ...] = ()
    if aligner is not None:
        from dataclasses import replace as _dc_replace

        from phi_engine.security.phi_alignment import align_uncovered_headers

        uncovered = [
            h
            for h, c in classifications_by_header.items()
            if c.action == Action.KEEP and not c.matched_rules
        ]
        if uncovered:
            aligned, _held = align_uncovered_headers(
                uncovered,
                rule_bundle.to_json(),
                tuple(privacy_config.jurisdictions),
                aligner=aligner,
            )
            for ar in aligned:
                act = Action(ar.action)
                prev = classifications_by_header[ar.header]
                classifications_by_header[ar.header] = _dc_replace(
                    prev,
                    action=act,
                    matched_rules=(f"ai_aligned:{ar.matched_rule_id}",),
                    jurisdictions=tuple(ar.jurisdictions) or prev.jurisdictions,
                    reasons=(ar.reason or ar.rule_citation,),
                    method=_ACTION_METHOD.get(act),
                )
            aligned_rule_records = tuple(ar.to_json() for ar in aligned)

    classifications = tuple(classifications_by_header[header] for header in headers)
    actions = {header: item.action.value for header, item in classifications_by_header.items()}

    # ------------------------------------------------------------------
    # Step 2: Adversarial probe — single deterministic evaluation.
    # The probe is pure pattern matching; retrying with the same frozen
    # args cannot change the outcome, so one evaluation is sufficient.
    # ------------------------------------------------------------------
    adversarial_failures: tuple[str, ...] = _adversarial_header_validation(
        privacy_config, rule_bundle
    )
    attempt = 1  # always exactly one evaluation

    # ------------------------------------------------------------------
    # Step 3: Deterministic blockers and coverage holds (no retry needed).
    # ------------------------------------------------------------------
    blockers = _review_blockers(headers)
    # SoT CROSS-VERIFICATION → DIRECT-IDENTIFIER FORCE-DROP.
    #
    # A column only needs handling if the SCRUB actually PUBLISHES IT RAW (its
    # configured action is keep) — a column the scrub already drops/pseudonymizes/
    # jitters is never leaked regardless of the name-regex (e.g. SUBJID2..7,
    # IC_SIGN are KEEP by the name-regex but the scrub drops them).
    # ``published_raw_headers`` is that set; when not supplied (legacy callers)
    # every KEEP is treated as published-raw so behavior is unchanged.
    #
    # Among published-raw columns, a DIRECT IDENTIFIER must be DROPPED (policy:
    # signatures, initials, names, free-text → drop; only the subject ID is
    # pseudonymized, which the scrub's id_fields already handles). A column is a
    # direct identifier when (a) the PDF-aware SoT flags it PHI, OR (b) its name
    # matches a PHI-risk pattern and NOTHING confirms it benign (no printed
    # clinical question in the SoT AND no deliberate documented keep_fields rule
    # like the coded category IC_RATION). These are recorded in
    # ``force_drop_headers`` and removed by the scrub — the form still publishes
    # its remaining columns rather than being held.
    sot = sot_signals or {}
    published_raw = (
        published_raw_headers
        if published_raw_headers is not None
        else frozenset(item.header for item in classifications if item.action == Action.KEEP)
    )

    def _sot_confirms_benign(header: str) -> bool:
        # SoT confirms a name-flagged KEEP is benign when the variable has a printed
        # PDF question (a known clinical question) AND the PDF-aware SoT did not
        # itself recommend a PHI action.
        #
        # EXCEPTION — STRONG direct identifiers (person name/initials/signature,
        # contact IDs, government IDs): a CRF legitimately *asks* for a patient's
        # initials or signature, so "has_pdf_question=True, sot_phi=None" is expected
        # and does NOT confirm the column benign.  Only an explicit affirmative SoT
        # non-PHI classification (``is_phi=False`` AND ``sot_phi`` is something benign
        # rather than absent) OR a deliberate ``confirmed_keep_headers`` entry can
        # clear a STRONG identifier — the latter is checked in the caller
        # (_is_direct_identifier branch c).
        sig = sot.get(header.upper())
        if not sig:
            return False
        if not sig.get("has_pdf_question"):
            return False
        if sig.get("is_phi"):
            return False
        # For STRONG direct identifiers, a printed question with a *silent* (None)
        # sot_phi is ambiguous — we cannot tell whether the SoT builder reviewed
        # the column and decided keep vs. simply left it unclassified.  Require an
        # explicit non-PHI SoT classification (sot_phi is a non-None, non-empty
        # benign value) before clearing.  Weaker risk tokens keep the original
        # "present question + no PHI action = benign" logic.
        if _is_strong_direct_id_header(header):
            return bool(sig.get("sot_phi"))
        return True

    def _is_direct_identifier(item: HeaderClassification) -> bool:
        # Only columns the scrub PUBLISHES RAW can leak; a column it already
        # drops/pseudonymizes/jitters needs no override (subject IDs are
        # pseudonymized here, NOT dropped — they are required for linkage).
        if item.header not in published_raw:
            return False
        sig = sot.get(item.header.upper(), {})
        # (a) the PDF-aware SoT flags it a direct identifier to DROP — overrides
        #     even an explicit scrub keep (a direct identifier must be dropped).
        if sig.get("sot_phi") == "drop":
            return True
        # (b) the regulation classifier itself decided DROP, but a scrub keep
        #     would publish it raw. Honor the stricter DROP decision UNLESS a
        #     deliberate documented keep_fields rule says keep (that human keep
        #     overrides a name-regex over-match; the SoT path (a) still overrides
        #     even a documented keep, since the SoT read the printed question).
        if item.action == Action.DROP and item.header not in confirmed_keep_headers:
            return True
        # (c) a risky-NAMED keep with nothing confirming it benign (no printed
        #     clinical question in the SoT, no documented keep_fields rule) →
        #     a probable direct identifier → drop.
        return (
            item.action == Action.KEEP
            and is_phi_risky_header(item.header)
            and not _sot_confirms_benign(item.header)
            and item.header not in confirmed_keep_headers
        )

    force_drop_headers = tuple(
        item.header for item in classifications if _is_direct_identifier(item)
    )
    reasons = tuple(dict.fromkeys((*blockers, *adversarial_failures)))

    status = "held" if reasons else "approved"

    # ------------------------------------------------------------------
    # Step 4: Build structured held_reason when adversarial probe fails.
    # ------------------------------------------------------------------
    held_reason: HeldReason | None = None
    if adversarial_failures:
        held_reason = _build_held_reason_for_adversarial_exhaustion(
            adversarial_failures, attempt, privacy_config
        )

    return FormReviewApproval(
        form_name=form_name,
        status=status,
        attempts=attempt,
        actions=actions,
        classifications=classifications,
        reasons=reasons,
        rule_bundle_sha256=rule_bundle.rules_sha256,
        source_mode=rule_bundle.source_mode,
        held_reason=held_reason,
        force_drop_headers=force_drop_headers,
        aligned_rules=aligned_rule_records,
    )


def verify_approval_payload(payload: dict[str, Any]) -> None:
    """Validate approval report shape and ensure it contains no value samples."""
    required = {
        "run_id",
        "study",
        "created_utc",
        "jurisdictions",
        "conflict_policy",
        "rule_bundle",
        "forms",
        "approved_forms",
        "held_forms",
        "status",
    }
    missing = required - set(payload)
    if missing:
        raise ValueError(f"approval payload missing required keys: {sorted(missing)}")

    text = json.dumps(payload, sort_keys=True)
    forbidden_markers = (
        "raw_value",
        "sample_value",
        "synthetic_value",
        "Alice",
        "555-",
        "123-45-6789",
    )
    leaked = [marker for marker in forbidden_markers if marker in text]
    if leaked:
        raise ValueError(f"approval payload contains value-like marker(s): {leaked}")

    forms = payload.get("forms")
    if not isinstance(forms, list):
        raise ValueError("approval payload forms must be a list")
    for item in forms:
        if not isinstance(item, dict):
            raise ValueError("approval form item must be a mapping")
        if item.get("status") not in {"approved", "held"}:
            raise ValueError(f"invalid approval status: {item.get('status')!r}")
