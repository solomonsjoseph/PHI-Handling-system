"""PHI scrubber — structural-field honest-broker catalog for RePORT AI Portal.

Nine structural-field action classes, evaluated in strict priority order
(first match wins per field):

1. **keep** (``keep_fields``) — allowlist; short-circuits every other rule.
   Used to protect clinical lab / medication / time-of-day / categorical
   indicators from being swept up by broader patterns.
2. **birthdate** (``birthdate_field``) — posture-dependent:

   - ``safe_harbor`` (default) → field dropped entirely per HIPAA
     §164.514(b)(2)(i)(C) + DPDPA. Age fidelity is lost.
   - ``limited_dataset`` → field jittered with the same per-subject offset
     as other dates (SANT method), preserving age-at-event. Requires an
     IRB-approved protocol + DUA; the module refuses to run in this
     mode unless ``authorities/phi_limited_dataset.md`` exists.

3. **drop** (``drop_fields``) — field removed from every row. Covers
   names, initials, signatures, staff identifiers, national IDs (Aadhaar /
   PAN / voter / passport / DL / ration / ESIC / PM-JAY / Nikshay / ABHA),
   contact info, exact geography, free-text narratives, system timestamps,
   and batch/scan artefacts.
4. **cap** (``cap_fields``) — numeric values strictly greater than
   ``threshold`` are replaced with ``label`` (default age > 89 → "90+",
   HIPAA §164.514(b)(2)(i)(C)).
5. **generalize** (``generalize_fields`` + ``generalization_maps``) —
   value-level categorical mapping (e.g. marital status → Married / Single
   / Other; facility type → Government / Private / Other).
6. **band** (``band_fields``) — numeric or categorical values mapped to
   broad ranges or categories (e.g. age groups, income bands). Configured
   via ``band_ranges`` (numeric) and ``band_maps`` (categorical) in
   ``phi_scrub.yaml``. Fail-closed: an unmapped value quarantines the row.
7. **suppress_small_cell** (``suppress_small_cell_fields``) — numeric
   values strictly greater than ``small_cell_threshold`` are clamped to the
   threshold (ICMR §11.7 k-anonymity proxy for household-contact counts).
8. **date** (``date_fields``) — per-subject deterministic offset in
   ``[-max_jitter_days, +max_jitter_days]``. Offset = ``HMAC-SHA256(key,
   subject_id)[:4] as int mod (2*N+1) - N``. SANT-method interval
   preservation for epidemiological survival / incidence / person-time
   analyses.
9. **id** (``id_fields``) — replaced with
   ``"RID_" + label + "_" + alpha12(hmac_sha256(key, label + ":" + raw_id))``.
   Deterministic cross-file linkage preserved; non-reversible without key
   possession. The ``RID_`` envelope plus alphabet-only tag keeps generated
   pseudonyms from matching raw subject-ID, phone, or date regexes in the
   pre-publication leak gate.

Free-text PHI residuals are handled conservatively by dropping narrative
fields wholesale. Current narrative fields like ``*COMMENT``, ``*REMARK``,
``WITHDRAWEXPLAIN``, and ``*SPECIFY`` are removed before publication; the
agent-boundary PHI gate remains defense-in-depth for returned text.

Priority-0 SUPPRESS→DROP force-drop mechanism (A2)
-----
phi_review classifies free-text headers as SUPPRESS (action value="suppress");
the scrubber builds a per-form ``force_drop_by_stem`` set from all SUPPRESS
actions (phi_scrub.py line ~2681: headers where action=="suppress") and the
SoT-flagged direct identifiers. The priority-0 force-drop gate (phi_scrub.py
line ~1768: ``if suppress_headers and field in suppress_headers``) applies
*before* any keep rule, ensuring that free-text fields are force-dropped even
when a broad form-prefix keep rule would otherwise match. This is a
strictest-wins principle: the decider (phi_review) and cleaner (phi_scrub) both
honor SUPPRESS as "do not publish raw" — the scrubber responds by dropping
(for text) or clamping (for numeric contacts counts). Audit ledger records
each force-drop as a "phi-scrub-drop" event with no corresponding keep_decision.

Rule catalog is declared in ``phi_scrub.yaml`` (Indo-VAP-calibrated).

Zone boundary
-------------
* Reads + rewrites ``tmp/{STUDY}/datasets/*.jsonl`` in place (write_zone).
* Optionally writes orphan rows to ``tmp/{STUDY}/quarantine/{file}.jsonl``
  when a row lacks a resolvable subject_id (write_zone).
* Emits a single audit envelope at :data:`config.AUDIT_SCRUB_REPORT_PATH`
  (output_zone). The audit records **counts only** — no raw values, no
  before/after pairs.

Ordering in the host publish path
---------------------------------
Runs as Step 1.6 — AFTER Step 1+3 (raw extraction) and BEFORE Step 1.7
(dataset cleanup). This keeps ``dataset_cleanup_report.json`` free of raw
subject IDs and raw dates, so the dataset-leg audit never contains PHI.

Key management
--------------
The HMAC key is a sidecar file at
``$XDG_CONFIG_HOME/report_ai_portal/phi_key`` (default ``~/.config/report_ai_portal/phi_key``).
Mode must be ``0600``. Missing key = hard-fail for developer/operator host
publish runs. Normal users create it through the web UI's Load Study flow.
Developers can bootstrap explicitly::

    python -m scripts.security.phi_scrub bootstrap-key

Rotating the key invalidates every previously-scrubbed artifact — full
re-ingestion from raw is required. This is a one-way property: deletion of
the key forfeits the ability to re-derive the same pseudonyms.

Idempotency
-----------
Each scrubbed record gets a ``_phi_scrubbed: "v3"`` marker. A second run
with the same key is a no-op (the sentinel file
``tmp/{STUDY}/.phi_scrub_complete`` short-circuits the orchestrator).

Threat-model summary
--------------------
* HMAC-SHA256 with a secret key is non-reversible without key possession.
* 12 alphabet (a-p) chars (48 bits) collision surface is adequate for single-study cohorts
  under 100 000 subjects. Larger cohorts should widen the slice.
* Same (key, subject_id) always yields the same pseudonym → cross-run
  joins remain stable across re-ingestion.
* Different machines with different keys → different pseudonyms → hard
  cross-site joins. This is deliberate: collaborator key distribution is
  an operational, not pipeline, concern.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sys
from collections.abc import Iterable
from datetime import datetime, timezone
UTC = timezone.utc  # py3.9 compat, timedelta
from pathlib import Path
from typing import Any

import yaml

import phi_engine.config.config as config
from phi_engine.audit import is_llm_agent
from phi_engine.audit.ledger import (
    PHI_LEDGER_FILENAME,
    LedgerWriter,
    dataset_phi_ledger_path,
    ensure_no_llm_sentinel,
    remove_dataset_no_llm_sentinels,
)
from phi_engine.utils._extraction_io import atomic_write_json, atomic_write_jsonl, parse_date
from phi_engine.security.phi_patterns import mask_date_shape as _mask_date_shape
from phi_engine.security.secure_env import assert_output_zone, assert_write_zone
from phi_engine.utils.integrity import hash_bytes, hash_file
from phi_engine.utils.logging_system import get_logger

logger = get_logger(__name__)

__all__ = [
    "PHI_SCRUB_SENTINEL_NAME",
    "BandRule",
    "CapRule",
    "GeneralizeRule",
    "IdRule",
    "PHIBandUnmappedError",
    "PHIDateUnshiftableError",
    "PHIGeneralizeUnmappedError",
    "PHIKeyAccessDeniedError",
    "PHIKeyMissingError",
    "PHIKeyPermissionError",
    "PHIPartialThresholdExceededError",
    "PHIQuarantineOverflowError",
    "PHIScrubConfig",
    "PHIScrubError",
    "band_categorical",
    "band_numeric",
    "bootstrap_key",
    "cap_numeric",
    "date_offset_days",
    "effective_scrub_config_hash",
    "generalize_value",
    "load_key",
    "load_scrub_config",
    "pseudo_id",
    "run_scrub",
    "shift_date",
    "suppress_small_cell",
]

# ── Constants ────────────────────────────────────────────────────────────────

# Row-level ``_phi_scrubbed`` marker. Acts as an idempotency guard so a
# second scrub pass over the same staging file is a no-op. The full
# catalog version + rule counts live in the audit report, not the row.
_SCRUB_VERSION = "v3"
# v3: ID pseudonyms carry the semantic category inside an opaque RID envelope.
# Format: ``RID_<LABEL>_<alpha12>`` where the HMAC input is
# ``f"{label}:{raw_value}"``. Same raw value under different labels → different
# pseudonyms (prevents cross-category correlation), same raw value under the
# same label → same pseudonym (preserves in-category longitudinal linkage
# across files). The ``RID_`` prefix and alphabet-only tag prevent generated
# pseudonyms from matching raw subject-ID or phone regexes. Bumping the marker
# forces re-scrub of any row written under the v2 ``<LABEL>_<hmac12>`` scheme.
_SCRUB_MARKER_FIELD = "_phi_scrubbed"
_SENTINEL_NAME = ".phi_scrub_complete"
# Public alias so callers (e.g. main.py) can import a stable name without
# reaching into a private constant.
PHI_SCRUB_SENTINEL_NAME = _SENTINEL_NAME

_DEFAULT_MAX_JITTER_DAYS = 30
_DEFAULT_ORPHAN_THRESHOLD = 10
_DEFAULT_AGE_CAP_THRESHOLD = 89
_DEFAULT_AGE_CAP_LABEL = "90+"
_DEFAULT_SMALL_CELL_THRESHOLD = 5
_DEFAULT_PARTIAL_MAX_QUARANTINE_FRACTION = 0.10
# Future-date placeholder policy.
# Default plausible_max_year=9999 keeps the future-check a NO-OP for any
# config/test that doesn't set it — back-compat, no changed behavior.
_DEFAULT_PLAUSIBLE_MAX_YEAR = 9999
_DEFAULT_FUTURE_DATE_POLICY = "quarantine"
_VALID_FUTURE_DATE_POLICIES = frozenset({"sentinel", "quarantine"})
# Unparseable-date policy (Note 29). A date value that survives the sentinel /
# null-token / future-date checks but still cannot be parsed+shifted after every
# format is exhausted is either:
#   "blank"      — remove that single field (set ""), publish the rest of the row;
#                  strictly safer than today (the raw date is never published) and
#                  preserves the row's other clean data. DEFAULT (autonomy bar).
#   "quarantine" — withhold the whole row (the prior fail-closed behavior).
_DEFAULT_UNPARSEABLE_DATE_POLICY = "blank"
_VALID_UNPARSEABLE_DATE_POLICIES = frozenset({"blank", "quarantine"})
_PSEUDO_TAG_CHARS = 12  # 48-bit HMAC tag encoded as a-p letters
_OFFSET_DIGEST_BYTES = 4  # first N bytes of digest for offset computation
_HEX_TO_ALPHA = str.maketrans("0123456789abcdef", "abcdefghijklmnop")

_POSTURE_SAFE_HARBOR = "safe_harbor"
_POSTURE_LIMITED_DATASET = "limited_dataset"
_VALID_POSTURES = frozenset({_POSTURE_SAFE_HARBOR, _POSTURE_LIMITED_DATASET})

# Recognized missing-data sentinels for DATE fields. A value equal to one of
# these (after .strip().upper()) skips date jitter rather than fail-closing.
# Mirrors the default ``date_null_tokens`` list in phi_scrub.yaml. Kept here
# as a fallback so older configs that predate the key behave sanely.
# Blank/separator-only date string guard.  Values like "/  /", "//", ". ."
# contain no actual date — treat them as empty (missing date) and skip jitter.
# Checked BEFORE the null-token lookup and shift_date call inside _scrub_row.
_DATE_BLANK_RE = re.compile(r"^[\s/.\-:]*$")

# ISO leading-year date (YYYY-MM-DD, optionally followed by time/separator). Used
# by the future-date check to read the UNAMBIGUOUS leading year of an Excel
# datetime cell that parse_date rejected for being beyond its plausible ceiling
# (e.g. a year-2914 placeholder). ISO year is always the leading group — no locale
# ambiguity — so this never reinterprets a DMY/slash value.
_ISO_LEADING_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:\b|$)")

# A date-NAMED column that is actually a "Not Done" / "Not Recorded" status flag
# (suffix ND / NR, optionally with a trailing index) holds free text, NOT a date,
# so it must stay KEPT rather than fall through to the date-jitter rule. Examples:
# CX_PROCDAT_ND, ZN_MBDATNR, ZN_MBDATNR2, CBC_HBAND. Used by the date-leak guard
# in PHIScrubConfig.field_is_keep to distinguish real date columns from these.
_DATE_STATUS_FLAG_RE = re.compile(r"(?:ND|NR)\d*$", re.IGNORECASE)


def _is_date_sentinel(value: object) -> bool:
    """True if a date value is an all-9s or all-0s *placeholder* (ignoring separators)
    that does NOT also parse as a real calendar date.

    Clinical "unknown date" placeholders appear as 99999999, 9999-99-99, 999999,
    a bare 9, or the 0-valued equivalents — sometimes stored as integers (which
    bypass a string token list), so this checks digit content directly. Such
    values skip jitter (kept as-is) rather than fail-closing.

    CRITICAL (PHI): an all-9 digit string can ALSO be a genuine date — e.g.
    ``"9/9/99"`` = 1999-09-09 (digits "9999"). Classifying that as a sentinel
    would skip jitter and publish a real, un-shifted date — a PHI leak (a date
    more specific than year). So an all-9/all-0 value is a sentinel ONLY when it
    does not parse as a real date; a parseable date returns False here and falls
    through to jitter. The genuine sentinels (99999999, 9999-99-99, 999999, 9,
    0…) are all unparseable, so they still classify correctly.
    """
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if not digits or not (digits == "9" * len(digits) or digits == "0" * len(digits)):
        return False
    # All-9/all-0 by digit content — but never swallow a real date.
    try:
        if parse_date(str(value)) is not None:
            return False
    except ValueError:
        # Locale-ambiguous AND all-9/all-0 → a genuine placeholder, not a
        # safely-shiftable date; treat as a sentinel.
        return True
    return True


_DEFAULT_DATE_NULL_TOKENS: frozenset[str] = frozenset(
    {
        "UNK",
        "UNKNOWN",
        "NA",
        "N/A",
        "N.A.",
        "NONE",
        "NIL",
        "NOT DONE",
        "NOT APPLICABLE",
        "NOT AVAILABLE",
        "NOT REPORTED",
        "ND",
        "NR",
        ".",
        "-",
        "--",
        "?",
    }
)

_KEY_FILE_MODE = 0o600
_KEY_HEX_LEN = 64  # 32 bytes = 64 hex chars

_LIMITED_DATASET_AUTHORITY = "authorities/phi_limited_dataset.md"

# Action priority (first match wins when walking a row's fields):
# keep > birthdate > drop > cap > generalize > band > suppress_small_cell > date > id
# The scrub loop emits these action strings inline via ``_bump(...)``.


# ── Exceptions ───────────────────────────────────────────────────────────────


class PHIScrubError(Exception):
    """Base class for PHI scrub errors."""


class PHIKeyMissingError(PHIScrubError):
    """Raised when the sidecar key file is absent."""


class PHIKeyPermissionError(PHIScrubError):
    """Raised when the sidecar key file has unsafe permissions."""


class PHIKeyAccessDeniedError(PHIScrubError):
    """Raised when an ``llm-agent``-role process attempts to read the HMAC key.

    The HMAC key lets its holder forge the deterministic form-scoped pseudonyms
    (``pseudo_id``) and therefore re-link or de-anonymize subjects. An LLM-agent
    process must never obtain it — this is the key-access analogue of the
    ledger-write role gate in :mod:`scripts.audit.ledger`.
    """


class PHIQuarantineOverflowError(PHIScrubError):
    """Raised when orphan-row count exceeds the configured threshold."""


class PHIBandUnmappedError(PHIScrubError):
    """Raised when a band field holds a value the configured band map/ranges
    cannot cover. The band scaffold is fail-closed: an uncoverable socioeconomic
    value is quarantined and the run hard-fails — never leaked, never silently
    dropped."""


class PHIGeneralizeUnmappedError(PHIScrubError):
    """Raised when a generalize field holds a non-empty value the configured
    generalization map does not cover. generalize is fail-closed: an unmapped value
    is never passed through (which would leak raw PHI) and never silently dropped —
    the row is quarantined and the run hard-fails so an operator can curate the map.
    Mirrors :class:`PHIBandUnmappedError` for the band action."""


class PHIDateUnshiftableError(PHIScrubError):
    """Raised when a date field holds a non-empty value that cannot be safely
    jittered — it does not parse as a date (``shift_date`` → ``None``) or its
    slash-date locale is ambiguous (``parse_date`` → ``ValueError``). Date jitter
    is fail-closed: such a value is never passed through (which would leak the raw
    date) and never crashes the run unaccountably — the row is quarantined and the
    run hard-fails so an operator can fix the data or add a ``date_locales`` entry.
    Mirrors :class:`PHIBandUnmappedError` / :class:`PHIGeneralizeUnmappedError`."""


class PHIPartialThresholdExceededError(PHIScrubError):
    """Retained for API/strict-mode compatibility — NO LONGER raised on the
    partial-publish path.

    Historically, partial mode hard-failed when a single file's quarantined
    fraction exceeded ``partial_max_quarantine_fraction``. That aborted the whole
    study and denied the operator every *clean* form's usable data, which
    contradicted the partial-publish contract ("move on with the forms that
    worked"). The over-threshold condition is now surfaced as an ``elevated``
    review flag on the form's ``scrub_outcome.json`` entry instead of an abort —
    every published row remains individually correct (row-level fail-closed
    scrub/date logic), so an elevated form is *incomplete*, never *corrupt*. The
    class is kept defined + exported so existing imports/`__all__` stay stable."""


# ── Config ───────────────────────────────────────────────────────────────────


class CapRule:
    """Compiled cap rule — pattern + threshold + label.

    Each ``cap_fields`` entry yields one ``CapRule``. When a row's field name
    matches ``pattern``, numeric values strictly greater than ``threshold``
    are replaced with ``label``. Values ≤ threshold pass through unchanged.
    """

    __slots__ = ("label", "pattern", "threshold")

    def __init__(self, pattern: re.Pattern[str], threshold: int, label: str) -> None:
        self.pattern = pattern
        self.threshold = threshold
        self.label = label

    def matches(self, name: str) -> bool:
        return bool(self.pattern.search(name))


class IdRule:
    """Compiled id rule — pattern + semantic label.

    Each ``id_fields`` entry yields one ``IdRule``. When a row's field name
    matches ``pattern``, the field value is pseudonymized via
    :func:`pseudo_id` with the attached ``label``. The label is propagated
    both inside the visible output token (``RID_<LABEL>_<alpha12>``) and as
    the HMAC domain-separator, so the same raw value under two different
    labels yields two different pseudonyms.

    Keep the label short (3-5 chars, uppercase). It becomes part of every
    pseudonymized output and of the IRB-facing audit log.
    """

    __slots__ = ("label", "pattern")

    def __init__(self, pattern: re.Pattern[str], label: str) -> None:
        self.pattern = pattern
        # Sanitize to alphanumeric (uppercased, capped) so the visible
        # RID_<LABEL>_<alpha12> token + HMAC domain-separator are ALWAYS
        # well-formed — for config-declared labels and AI-overlay labels alike.
        # Default config labels are already clean, so this is a no-op for them.
        self.label = re.sub(r"[^A-Za-z0-9]", "", str(label)).upper()[:16] or "ID"

    def matches(self, name: str) -> bool:
        return bool(self.pattern.search(name))


class GeneralizeRule:
    """Compiled generalize rule — pattern + named value mapping.

    Each ``generalize_fields`` entry pairs a field-name pattern with the
    name of a value-to-value mapping under ``generalization_maps``. At
    scrub time the value is lower-cased, looked up in the mapping, and
    replaced. As of T2.2 the rule is **fail-closed**: an unmapped non-empty
    value quarantines the row and raises :class:`PHIGeneralizeUnmappedError`
    (see :func:`generalize_value` / :func:`_scrub_row` rung 5) rather than
    falling through unchanged.
    """

    __slots__ = ("mapping", "mapping_name", "pattern")

    def __init__(
        self,
        pattern: re.Pattern[str],
        mapping_name: str,
        mapping: dict[str, str],
    ) -> None:
        self.pattern = pattern
        self.mapping_name = mapping_name
        self.mapping = mapping

    def matches(self, name: str) -> bool:
        return bool(self.pattern.search(name))


class BandRule:
    """Compiled fail-closed band rule — pattern + named categorical map OR numeric ranges.

    ``kind="categorical"`` resolves ``band_maps`` (lower-cased value→label, exact);
    ``kind="numeric"`` resolves ``band_ranges`` (ascending ``(upper_inclusive, label)``
    tuples; an entry with ``upper is None`` is the open-ended catch-all top band).
    Unlike the fail-open :class:`GeneralizeRule`, a value the band cannot cover is
    fail-closed: the row is quarantined and the run hard-fails. Maps/ranges are
    operator-curated TEMPLATES (values deferred) — an empty template quarantines
    every value until filled, by design.
    """

    __slots__ = ("band_name", "kind", "mapping", "pattern", "ranges")

    def __init__(self, pattern, band_name, kind, *, mapping=None, ranges=None):
        self.pattern = pattern
        self.band_name = band_name
        self.kind = kind
        self.mapping = mapping
        self.ranges = ranges

    def matches(self, name: str) -> bool:
        return bool(self.pattern.search(name))


class PHIScrubConfig:
    """Parsed + compiled scrub configuration.

    Regex patterns from YAML are compiled once at load time; config is a
    throwaway struct (not persisted beyond the host publish run).

    Rule priority (first match wins within :func:`_scrub_row`):
        1. ``keep_patterns`` — allowlist, short-circuits every other rule
        2. ``birthdate_pattern`` — posture-dependent drop or jitter
        3. ``drop_patterns`` — field removed from row
        4. ``cap_rules`` — numeric capped to label
        5. ``generalize_rules`` — value mapped to broad category
        6. ``band_rules`` — fail-closed categorical/numeric generalization
        7. ``suppress_small_cell_patterns`` — numeric clamped to threshold
        8. ``date_patterns`` — jitter via SANT
        9. ``id_patterns`` — HMAC-SHA256 pseudonymize

    """

    __slots__ = (
        "age_cap_label",
        "age_cap_threshold",
        "age_field_patterns",
        "band_rules",
        "birthdate_pattern",
        "cap_rules",
        "compliance_posture",
        "date_null_tokens",
        "date_patterns",
        "death_date_patterns",
        "drop_patterns",
        "future_date_policy",
        "generalize_rules",
        "id_patterns",
        "keep_patterns",
        "max_jitter_days",
        "no_subject_id_forms",
        "orphan_quarantine_threshold",
        "partial_max_quarantine_fraction",
        "plausible_max_year",
        "small_cell_threshold",
        "subject_id_fields",
        "suppress_small_cell_patterns",
        "unparseable_date_policy",
    )

    def __init__(
        self,
        *,
        compliance_posture: str,
        subject_id_fields: tuple[str, ...],
        date_patterns: list[re.Pattern[str]],
        id_patterns: list[IdRule],
        birthdate_pattern: re.Pattern[str] | None,
        max_jitter_days: int,
        orphan_quarantine_threshold: int,
        keep_patterns: list[re.Pattern[str]] | None = None,
        drop_patterns: list[re.Pattern[str]] | None = None,
        cap_rules: list[CapRule] | None = None,
        generalize_rules: list[GeneralizeRule] | None = None,
        band_rules: list[BandRule] | None = None,
        suppress_small_cell_patterns: list[re.Pattern[str]] | None = None,
        age_field_patterns: list[re.Pattern[str]] | None = None,
        death_date_patterns: list[re.Pattern[str]] | None = None,
        age_cap_threshold: int = _DEFAULT_AGE_CAP_THRESHOLD,
        age_cap_label: str = _DEFAULT_AGE_CAP_LABEL,
        small_cell_threshold: int = _DEFAULT_SMALL_CELL_THRESHOLD,
        date_null_tokens: frozenset[str] | None = None,
        partial_max_quarantine_fraction: float = _DEFAULT_PARTIAL_MAX_QUARANTINE_FRACTION,
        plausible_max_year: int = _DEFAULT_PLAUSIBLE_MAX_YEAR,
        future_date_policy: str = _DEFAULT_FUTURE_DATE_POLICY,
        unparseable_date_policy: str = _DEFAULT_UNPARSEABLE_DATE_POLICY,
        no_subject_id_forms: frozenset[str] | None = None,
    ) -> None:
        if compliance_posture not in _VALID_POSTURES:
            raise PHIScrubError(
                f"Unknown compliance_posture {compliance_posture!r}. "
                f"Valid values: {sorted(_VALID_POSTURES)}"
            )
        if max_jitter_days < 1:
            raise PHIScrubError(f"max_jitter_days must be >= 1, got {max_jitter_days}")
        if not subject_id_fields:
            raise PHIScrubError("subject_id_fields must contain at least one field name")
        if age_cap_threshold < 0:
            raise PHIScrubError(f"age_cap_threshold must be >= 0, got {age_cap_threshold}")
        if small_cell_threshold < 1:
            raise PHIScrubError(f"small_cell_threshold must be >= 1, got {small_cell_threshold}")
        if not (0 < partial_max_quarantine_fraction <= 1):
            raise PHIScrubError(
                f"partial_max_quarantine_fraction must be in (0, 1], "
                f"got {partial_max_quarantine_fraction!r}"
            )
        if not isinstance(plausible_max_year, int) or not (1900 <= plausible_max_year <= 9999):
            raise PHIScrubError(
                f"plausible_max_year must be an int in [1900, 9999], got {plausible_max_year!r}"
            )
        if future_date_policy not in _VALID_FUTURE_DATE_POLICIES:
            raise PHIScrubError(
                f"future_date_policy must be one of {sorted(_VALID_FUTURE_DATE_POLICIES)}, "
                f"got {future_date_policy!r}"
            )
        if unparseable_date_policy not in _VALID_UNPARSEABLE_DATE_POLICIES:
            raise PHIScrubError(
                "unparseable_date_policy must be one of "
                f"{sorted(_VALID_UNPARSEABLE_DATE_POLICIES)}, got {unparseable_date_policy!r}"
            )
        self.compliance_posture = compliance_posture
        self.subject_id_fields = subject_id_fields
        self.date_patterns = date_patterns
        self.id_patterns = id_patterns
        self.birthdate_pattern = birthdate_pattern
        self.max_jitter_days = max_jitter_days
        self.orphan_quarantine_threshold = orphan_quarantine_threshold
        self.keep_patterns = keep_patterns or []
        self.drop_patterns = drop_patterns or []
        self.cap_rules = cap_rules or []
        self.generalize_rules = generalize_rules or []
        self.band_rules = band_rules or []
        self.age_field_patterns = age_field_patterns or []
        self.death_date_patterns = death_date_patterns or []
        self.suppress_small_cell_patterns = suppress_small_cell_patterns or []
        self.age_cap_threshold = age_cap_threshold
        self.age_cap_label = age_cap_label
        self.small_cell_threshold = small_cell_threshold
        self.partial_max_quarantine_fraction = partial_max_quarantine_fraction
        self.date_null_tokens: frozenset[str] = (
            date_null_tokens if date_null_tokens is not None else _DEFAULT_DATE_NULL_TOKENS
        )
        self.plausible_max_year: int = plausible_max_year
        self.future_date_policy: str = future_date_policy
        self.unparseable_date_policy: str = unparseable_date_policy
        self.no_subject_id_forms: frozenset[str] = no_subject_id_forms or frozenset()

    def field_is_keep(self, name: str) -> bool:
        """Return True if *name* matches any ``keep_fields`` pattern.

        Keep rules short-circuit every other rule — a kept field passes
        through the scrubber unchanged with no audit event recorded.

        DATE-LEAK GUARD: a column whose NAME is a genuine clinical date is
        NEVER kept raw, even when a broad form-prefix keep (``^CBC_``,
        ``^CXR_``, ``^DST_``, ``^CX_(...)``, ``^SC_(...)`` …) also matches it.
        A date is a temporal identifier (HIPAA Safe Harbor §164.514(b)(2)(i)(C))
        and must fall through to the date-jitter rule — otherwise visit /
        collection / result dates (e.g. ``CBC_VISDAT``, ``DST_ISOLATEDAT``,
        ``SC_PAXRECDAT``) would publish un-shifted and trip the pre-publication
        leak gate. This also keeps the keep-rule in agreement with
        ``phi_review``'s ``jitter_date`` decision for those columns
        (decided-vs-applied verifier, assertion 12). EXCEPTION: date-NAMED but
        non-date status flags ("Not Done" / "Not Recorded", suffix ND / NR —
        e.g. ``CX_PROCDAT_ND``, ``ZN_MBDATNR``) are the very reason those
        anchored keep rules exist, so they stay kept.
        """
        if not any(p.search(name) for p in self.keep_patterns):
            return False
        return not self._name_is_jitterable_date(name)

    def _name_is_jitterable_date(self, name: str) -> bool:
        """True when *name* is a real clinical date column that must be jittered:
        it matches a ``date_fields`` pattern, is not a birthdate (handled
        separately via :meth:`field_is_birthdate`), and is not an ND / NR
        'Not Done' / 'Not Recorded' status flag (date-NAMED but holds text)."""
        if self.birthdate_pattern is not None and self.birthdate_pattern.search(name):
            return False
        if not any(p.search(name) for p in self.date_patterns):
            return False
        return not _DATE_STATUS_FLAG_RE.search(name)

    def field_is_drop(self, name: str) -> bool:
        return any(p.search(name) for p in self.drop_patterns)

    def cap_rule_for(self, name: str) -> CapRule | None:
        """Return the first matching :class:`CapRule` for *name*, or None."""
        for rule in self.cap_rules:
            if rule.matches(name):
                return rule
        return None

    def generalize_rule_for(self, name: str) -> GeneralizeRule | None:
        """Return the first matching :class:`GeneralizeRule` for *name*, or None."""
        for rule in self.generalize_rules:
            if rule.matches(name):
                return rule
        return None

    def band_rule_for(self, name: str) -> BandRule | None:
        """Return the first matching :class:`BandRule` for *name*, or None."""
        for rule in self.band_rules:
            if rule.matches(name):
                return rule
        return None

    def field_is_suppress_small_cell(self, name: str) -> bool:
        return any(p.search(name) for p in self.suppress_small_cell_patterns)

    def field_is_date(self, name: str) -> bool:
        """Return True if *name* matches any ``date_fields`` pattern.

        Birthdate fields are excluded here — they are handled separately via
        :meth:`field_is_birthdate` so Safe Harbor drops can be distinguished
        from jitter events.

        Keep fields are also excluded: a field matching ``keep_fields`` is
        never date-processed regardless of whether its name also matches a
        date pattern.  This mirrors the priority-1 keep rule in ``_scrub_row``
        and allows test callers to assert ``field_is_date(kept_col) == False``.
        """
        if self.field_is_keep(name):
            return False
        if self.birthdate_pattern is not None and self.birthdate_pattern.search(name):
            return False
        return any(p.search(name) for p in self.date_patterns)

    def id_label_for(self, name: str) -> str | None:
        """Return the semantic label for *name*, or None if no rule matches.

        First-match wins — the YAML order determines precedence when a
        field name is ambiguous (e.g. a generic ``(?:patient|subject)[-_]?id``
        pattern listed AFTER a specific ``^SUBJID$`` rule keeps the specific
        rule's label).
        """
        for rule in self.id_patterns:
            if rule.matches(name):
                return rule.label
        return None

    def field_is_id(self, name: str) -> bool:
        """Compatibility shim — True when any id rule matches *name*."""
        return self.id_label_for(name) is not None

    def field_is_birthdate(self, name: str) -> bool:
        return self.birthdate_pattern is not None and bool(self.birthdate_pattern.search(name))

    def field_is_age(self, name: str) -> bool:
        """True when *name* is an explicit age column (drives the age-dependent
        birthdate/death-date policy: a form WITH an age field drops them)."""
        return any(p.search(name) for p in self.age_field_patterns)

    def field_is_death_date(self, name: str) -> bool:
        """True when *name* is a death DATE governed by the age-dependent policy
        (jitter when the form has no age field, drop when it does)."""
        return any(p.search(name) for p in self.death_date_patterns)

    def is_date_null_token(self, value: object) -> bool:
        """True if *value* is a recognized not-applicable/unknown date placeholder
        (case-insensitive, stripped). Such values skip date jitter rather than
        fail-closing."""
        if not isinstance(value, str):
            return False
        return value.strip().upper() in self.date_null_tokens


def _deep_merge_scrub(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge *override* on top of *base* for scrub-config dicts.

    MERGE SEMANTICS (deliberate, per Task A7):
    - Nested DICT values recurse (so a per-study override of one sub-key under
      e.g. ``band_ranges`` does not wipe its siblings).
    - LIST values and SCALARS from *override* REPLACE the base value wholesale.
      Rule keys (``keep_fields``, ``date_fields``, ``id_fields``, …) are lists,
      so a per-study file REPLACES that rule list rather than appending — the
      per-study config is the authoritative full list for any rule it declares.
    """
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge_scrub(result[key], value)
        else:
            result[key] = value
    return result


def _resolve_scrub_config_files(path: Path | None, study: str | None) -> list[Path]:
    """Return the ordered scrub-config files to merge (base first).

    Explicit *path* → single-file mode (back-compat for tests and explicit
    callers): exactly that file, no defaults merge. ``path is None`` → per-study
    resolution: the packaged defaults (``config/_defaults/phi_scrub.yaml``) as
    the base, with the per-study override (``config/<study>/phi_scrub.yaml``)
    deep-merged on top when present.
    """
    if path is not None:
        return [Path(path)]
    files: list[Path] = []
    default_path = Path(config.CONFIG_DEFAULTS_DIR) / config.PHI_SCRUB_CONFIG_FILENAME
    if default_path.is_file():
        files.append(default_path)
    study_path = Path(config.study_config_path(config.PHI_SCRUB_CONFIG_FILENAME, study=study))
    if study_path.is_file() and study_path != default_path:
        files.append(study_path)
    # Note 9: a run-scoped AI-aligned overlay (if present) merges LAST and is
    # therefore covered by BOTH _load_merged_scrub_raw and
    # effective_scrub_config_hash automatically — keeping scrub, hash, and
    # assertion 5 consistent. Only present when AI alignment is enabled + wrote it.
    overlay = _generated_overlay_path()
    if overlay is not None:
        files.append(overlay)
    return files


def _generated_overlay_path() -> Path | None:
    """Run-scoped AI-aligned scrub overlay path (Note 9), or None when absent."""
    run_id = os.environ.get("REPORTAL_RUN_ID")
    if not run_id:
        return None
    overlay = Path(config.STUDY_OUTPUT_DIR) / "runs" / run_id / config.PHI_SCRUB_GENERATED_FILENAME
    return overlay if overlay.is_file() else None


def write_generated_scrub_overlay(
    aligned_rules: list[dict[str, Any]],
    *,
    run_dir: Path,
    study: str | None = None,
) -> Path | None:
    """Write the run-scoped AI-aligned scrub overlay (Note 9) — or None if empty.

    The overlay carries COMPLETE lists for any rule key it touches (the merge
    REPLACES lists), so it reads the merged BASE config and appends each aligned
    rule's regex to the matching list: ``date_fields`` (jitter_date),
    ``drop_fields`` (drop), ``suppress_small_cell_fields`` (suppress), and
    ``id_fields`` (pseudonymize → ``{pattern, label}``). The deterministic engine
    then applies them; the overlay is captured in the snapshot for reproducibility.
    """
    from phi_engine.security.phi_alignment import RULE_FIELD_FOR_ACTION

    if not aligned_rules:
        return None
    base = _load_merged_scrub_raw(None, study) or {}
    out: dict[str, Any] = {}
    for rule in aligned_rules:
        field = RULE_FIELD_FOR_ACTION.get(str(rule.get("action")))
        pattern = rule.get("regex_pattern")
        if not field or not pattern:
            continue  # non-realizable action (rejected pre-overlay) or no pattern
        current = out.get(field)
        if current is None:
            current = list(base.get(field, []))
            out[field] = current
        if field == "id_fields":
            # Sanitize the LLM-derived label to alphanumeric so the visible
            # RID_<LABEL>_ pseudonym prefix is always well-formed (the alpha12
            # HMAC tail does the de-identification regardless).
            raw_label = str(rule.get("inferred_variable_type") or "ID")
            label = re.sub(r"[^A-Za-z0-9]", "", raw_label).upper()[:16] or "ID"
            current.append({"pattern": pattern, "label": label})
        else:
            current.append(pattern)
    if not out:
        return None
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    overlay_path = run_dir / config.PHI_SCRUB_GENERATED_FILENAME
    with overlay_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(out, fh, sort_keys=True)
    return overlay_path


def _load_merged_scrub_raw(path: Path | None, study: str | None) -> dict[str, Any] | None:
    """Load + deep-merge the effective scrub config as a raw dict.

    Returns ``None`` when NO config resolves at all (absent defaults AND absent
    per-study override) — the caller (``run_scrub``) treats that fail-closed.
    """
    merged: dict[str, Any] | None = None
    for cfg_file in _resolve_scrub_config_files(path, study):
        if not cfg_file.is_file():
            continue
        with cfg_file.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        if not isinstance(raw, dict):
            raise PHIScrubError(
                f"phi_scrub config at {cfg_file} must be a mapping at the top level"
            )
        merged = raw if merged is None else _deep_merge_scrub(merged, raw)
    return merged


# Separator placed BETWEEN multiple merged config files so a two-file
# concatenation can never collide with a single file whose bytes happen to span
# the join point. A single-file resolution emits NO separator, so its hash
# equals ``sha256(file_bytes)`` (== ``hash_file(file)``) — preserving the prior
# single-file hash for the common defaults-only study.
_SCRUB_HASH_SEPARATOR = b"\x00--phi-scrub-merge--\x00"


def effective_scrub_config_hash(
    study: str | None = None, *, path: Path | None = None
) -> str | None:
    """SHA-256 of the EFFECTIVE scrub config (defaults + per-study override).

    HASH INVARIANT: both the ledger envelope (``run_scrub``) and verifier
    assertion 5 (``extract_to_llm_source``) call THIS helper so they hash the
    SAME effective config and can never drift. The hash is taken over the raw
    bytes of every resolved config file in merge order, joined by a fixed
    separator. Properties:

    - DISCRIMINATION: two studies with different per-study overrides hash
      differently (the per-study bytes differ), so they cannot collide.
    - DETERMINISM: byte-identical inputs hash identically (reproducibility).
    - BACK-COMPAT: a single resolved file (the defaults-only common case, or an
      explicit ``path``) hashes to ``sha256(file_bytes)`` — identical to the
      prior ``hash_file`` behaviour.

    Returns ``None`` when no config resolves at all.
    """
    files = [f for f in _resolve_scrub_config_files(path, study) if f.is_file()]
    if not files:
        return None
    chunks: list[bytes] = []
    for index, cfg_file in enumerate(files):
        if index:
            chunks.append(_SCRUB_HASH_SEPARATOR)
        chunks.append(cfg_file.read_bytes())
    return hash_bytes(b"".join(chunks))


def load_scrub_config(
    path: Path | None = None, *, study: str | None = None
) -> PHIScrubConfig | None:
    """Load + compile the scrub config. Returns ``None`` if no config resolves.

    An absent config is NOT an error here — it means phi_scrub is a no-op for
    this study, and the pipeline continues (``run_scrub`` then fail-closes on
    real data). With ``path is None`` the packaged defaults
    (``config/_defaults/phi_scrub.yaml``) are loaded as the BASE and a per-study
    override (``config/<study>/phi_scrub.yaml``) is deep-merged on top when
    present (per-study keys win; per-study list values REPLACE — see
    ``_deep_merge_scrub``). Passing an explicit ``path`` loads just that single
    file (back-compat).

    When ``compliance_posture: limited_dataset`` is set, the function also
    verifies the authority note exists at :data:`_LIMITED_DATASET_AUTHORITY`.

    Loads the full rule set: keep / drop / cap / generalize / suppress /
    date / id patterns plus generalization_maps, age_cap, and
    small_cell_threshold constants.
    """
    raw = _load_merged_scrub_raw(path, study)
    if raw is None:
        return None

    posture = str(raw.get("compliance_posture", _POSTURE_SAFE_HARBOR))
    if posture == _POSTURE_LIMITED_DATASET:
        authority = Path(config.BASE_DIR) / _LIMITED_DATASET_AUTHORITY
        if not authority.is_file():
            raise PHIScrubError(
                f"compliance_posture is 'limited_dataset' but the required "
                f"authority note is missing: {authority}. Create it to document "
                f"IRB approval + Data Use Agreement before running."
            )

    # Accept either `subject_id_fields` (plural, list) or legacy
    # `subject_id_field` (singular, str). Plural wins when both present.
    _raw_plural = raw.get("subject_id_fields")
    _raw_singular = raw.get("subject_id_field")
    if _raw_plural is not None:
        if not isinstance(_raw_plural, list) or not _raw_plural:
            raise PHIScrubError("subject_id_fields must be a non-empty list of column names")
        subject_id_fields = tuple(str(f) for f in _raw_plural)
    elif _raw_singular is not None:
        subject_id_fields = (str(_raw_singular),)
    else:
        subject_id_fields = ("SUBJID",)

    def _compile_list(key: str) -> list[re.Pattern[str]]:
        patterns = raw.get(key) or []
        if not isinstance(patterns, list):
            raise PHIScrubError(f"{key} must be a list of regex strings")
        return [re.compile(str(p), re.IGNORECASE) for p in patterns]

    date_patterns = _compile_list("date_fields")
    keep_patterns = _compile_list("keep_fields")
    drop_patterns = _compile_list("drop_fields")
    suppress_patterns = _compile_list("suppress_small_cell_fields")
    age_field_patterns = _compile_list("age_fields")
    death_date_patterns = _compile_list("death_date_fields")

    # id_fields is structured: each entry must be a mapping with
    # ``pattern`` (regex) and ``label`` (short semantic category).
    # Plain-string entries are rejected — an unlabelled id field would
    # lose its category in the pseudonym output, which defeats the
    # whole point of the v3 scheme.
    raw_id_rules = raw.get("id_fields") or []
    if not isinstance(raw_id_rules, list):
        raise PHIScrubError("id_fields must be a list of {pattern, label} mappings")
    id_patterns: list[IdRule] = []
    for idx, entry in enumerate(raw_id_rules):
        if not isinstance(entry, dict):
            raise PHIScrubError(
                f"id_fields[{idx}] must be a mapping with 'pattern' + 'label'; "
                f"plain strings are no longer accepted in v3"
            )
        pat_str = entry.get("pattern")
        label = entry.get("label")
        if not pat_str or not label:
            raise PHIScrubError(
                f"id_fields[{idx}] requires both 'pattern' (regex) and 'label' "
                f"(short semantic category such as SUBJ / FAM / LAB)"
            )
        id_patterns.append(
            IdRule(
                pattern=re.compile(str(pat_str), re.IGNORECASE),
                label=str(label),
            )
        )

    birthdate_raw = raw.get("birthdate_field")
    birthdate_pattern = re.compile(str(birthdate_raw), re.IGNORECASE) if birthdate_raw else None

    max_jitter_days = int(raw.get("max_jitter_days", _DEFAULT_MAX_JITTER_DAYS))
    orphan_threshold = int(raw.get("orphan_quarantine_threshold", _DEFAULT_ORPHAN_THRESHOLD))
    small_cell_threshold = int(raw.get("small_cell_threshold", _DEFAULT_SMALL_CELL_THRESHOLD))

    raw_no_subj = raw.get("no_subject_id_forms") or []
    if not isinstance(raw_no_subj, list):
        raise PHIScrubError("no_subject_id_forms must be a list of filename substring patterns")
    no_subject_id_forms: frozenset[str] = frozenset(str(p) for p in raw_no_subj)
    partial_max_quarantine_fraction = float(
        raw.get("partial_max_quarantine_fraction", _DEFAULT_PARTIAL_MAX_QUARANTINE_FRACTION)
    )

    # Age cap — top-level constants, also default for cap_fields entries
    # that do not specify their own threshold/label.
    age_cap_raw = raw.get("age_cap") or {}
    if not isinstance(age_cap_raw, dict):
        raise PHIScrubError("age_cap must be a mapping with threshold + label")
    default_cap_threshold = int(age_cap_raw.get("threshold", _DEFAULT_AGE_CAP_THRESHOLD))
    default_cap_label = str(age_cap_raw.get("label", _DEFAULT_AGE_CAP_LABEL))

    # Cap rules. Each entry is a dict with `pattern` and optional `threshold`/
    # `label` overrides. If no overrides are given, the top-level age_cap
    # defaults apply.
    raw_cap_rules = raw.get("cap_fields") or []
    if not isinstance(raw_cap_rules, list):
        raise PHIScrubError("cap_fields must be a list of mappings")
    cap_rules: list[CapRule] = []
    for idx, entry in enumerate(raw_cap_rules):
        if not isinstance(entry, dict):
            raise PHIScrubError(
                f"cap_fields[{idx}] must be a mapping with at least a 'pattern' key"
            )
        pat_str = entry.get("pattern")
        if not pat_str:
            raise PHIScrubError(f"cap_fields[{idx}] is missing 'pattern'")
        threshold = int(entry.get("threshold", default_cap_threshold))
        label = str(entry.get("label", default_cap_label))
        cap_rules.append(
            CapRule(
                pattern=re.compile(str(pat_str), re.IGNORECASE),
                threshold=threshold,
                label=label,
            )
        )

    # Generalization maps — normalized to lower-case keys so we can do a
    # case-insensitive lookup at scrub time without allocating per-row.
    raw_gen_maps = raw.get("generalization_maps") or {}
    if not isinstance(raw_gen_maps, dict):
        raise PHIScrubError("generalization_maps must be a mapping of name → {value: value}")
    gen_maps: dict[str, dict[str, str]] = {}
    for name, mapping in raw_gen_maps.items():
        if not isinstance(mapping, dict):
            raise PHIScrubError(f"generalization_maps[{name}] must be a mapping of string → string")
        gen_maps[str(name)] = {str(src).strip().lower(): str(dst) for src, dst in mapping.items()}

    # Generalize rules. Each entry references a named mapping above.
    raw_gen_rules = raw.get("generalize_fields") or []
    if not isinstance(raw_gen_rules, list):
        raise PHIScrubError("generalize_fields must be a list of mappings")
    generalize_rules: list[GeneralizeRule] = []
    for idx, entry in enumerate(raw_gen_rules):
        if not isinstance(entry, dict):
            raise PHIScrubError(
                f"generalize_fields[{idx}] must be a mapping with 'pattern' + 'mapping'"
            )
        pat_str = entry.get("pattern")
        mapping_name = entry.get("mapping")
        if not pat_str or not mapping_name:
            raise PHIScrubError(f"generalize_fields[{idx}] requires 'pattern' and 'mapping'")
        mapping = gen_maps.get(str(mapping_name))
        if mapping is None:
            raise PHIScrubError(
                f"generalize_fields[{idx}] references unknown mapping "
                f"{mapping_name!r}; define it under generalization_maps"
            )
        generalize_rules.append(
            GeneralizeRule(
                pattern=re.compile(str(pat_str), re.IGNORECASE),
                mapping_name=str(mapping_name),
                mapping=mapping,
            )
        )

    # Band maps — normalized to lower-case keys like generalization_maps.
    raw_band_maps = raw.get("band_maps") or {}
    if not isinstance(raw_band_maps, dict):
        raise PHIScrubError("band_maps must be a mapping of name → {value: label}")
    band_maps: dict[str, dict[str, str]] = {}
    for bm_name, bm_mapping in raw_band_maps.items():
        if not isinstance(bm_mapping, dict):
            raise PHIScrubError(f"band_maps[{bm_name}] must be a mapping of string → string")
        band_maps[str(bm_name)] = {
            str(src).strip().lower(): str(dst) for src, dst in bm_mapping.items()
        }

    # Band ranges (numeric) — ascending (upper_inclusive, label); a final entry
    # with no 'max' is the open-ended catch-all top band. No inversion.
    raw_band_ranges = raw.get("band_ranges") or {}
    if not isinstance(raw_band_ranges, dict):
        raise PHIScrubError("band_ranges must be a mapping of name → list of {max?, label}")
    band_ranges: dict[str, list[tuple[float | None, str]]] = {}
    for br_name, br_entries in raw_band_ranges.items():
        if not isinstance(br_entries, list):
            raise PHIScrubError(f"band_ranges[{br_name}] must be a list of {{max?, label}} entries")
        compiled: list[tuple[float | None, str]] = []
        for i, entry in enumerate(br_entries):
            if not isinstance(entry, dict):
                raise PHIScrubError(f"band_ranges[{br_name}][{i}] must be a mapping")
            if "label" not in entry:
                raise PHIScrubError(f"band_ranges[{br_name}][{i}] missing 'label'")
            raw_max = entry.get("max")
            if raw_max is None:
                upper: float | None = None
            else:
                try:
                    upper = float(raw_max)
                except (TypeError, ValueError):
                    raise PHIScrubError(
                        f"band_ranges[{br_name}][{i}] 'max' must be a finite number"
                    ) from None
                if not math.isfinite(upper):
                    raise PHIScrubError(
                        f"band_ranges[{br_name}][{i}] 'max' must be a finite number"
                    )
            compiled.append((upper, str(entry["label"])))
        # A no-'max' catch-all (None upper) may only be the LAST entry.
        for u, _lbl in compiled[:-1]:
            if u is None:
                raise PHIScrubError(f"band_ranges[{br_name}] no-'max' catch-all entry must be last")
        # Finite 'max' bounds must be strictly ascending.
        finite_uppers = [u for u, _lbl in compiled if u is not None]
        if finite_uppers != sorted(finite_uppers) or len(set(finite_uppers)) != len(finite_uppers):
            raise PHIScrubError(
                f"band_ranges[{br_name}] 'max' values must be strictly ascending: {finite_uppers!r}"
            )
        band_ranges[str(br_name)] = compiled

    # Band fields — list of {pattern, band, kind}.
    raw_band_fields = raw.get("band_fields") or []
    if not isinstance(raw_band_fields, list):
        raise PHIScrubError("band_fields must be a list of {pattern, band, kind} mappings")
    band_rules: list[BandRule] = []
    for idx, entry in enumerate(raw_band_fields):
        if not isinstance(entry, dict):
            raise PHIScrubError(
                f"band_fields[{idx}] must be a mapping with 'pattern', 'band', and 'kind'"
            )
        bf_pat = entry.get("pattern")
        bf_band = entry.get("band")
        bf_kind = entry.get("kind")
        if not bf_pat or not bf_band or not bf_kind:
            raise PHIScrubError(f"band_fields[{idx}] requires 'pattern', 'band', and 'kind'")
        bf_kind = str(bf_kind)
        if bf_kind not in ("categorical", "numeric"):
            raise PHIScrubError(
                f"band_fields[{idx}] kind must be 'categorical' or 'numeric', got {bf_kind!r}"
            )
        bf_band = str(bf_band)
        if bf_kind == "categorical":
            if bf_band not in band_maps:
                raise PHIScrubError(
                    f"band_fields[{idx}] references band {bf_band!r} with kind='categorical' "
                    f"but {bf_band!r} is not defined in band_maps"
                )
            band_rules.append(
                BandRule(
                    pattern=re.compile(str(bf_pat), re.IGNORECASE),
                    band_name=bf_band,
                    kind="categorical",
                    mapping=band_maps[bf_band],
                )
            )
        else:  # numeric
            if bf_band not in band_ranges:
                raise PHIScrubError(
                    f"band_fields[{idx}] references band {bf_band!r} with kind='numeric' "
                    f"but {bf_band!r} is not defined in band_ranges"
                )
            band_rules.append(
                BandRule(
                    pattern=re.compile(str(bf_pat), re.IGNORECASE),
                    band_name=bf_band,
                    kind="numeric",
                    ranges=band_ranges[bf_band],
                )
            )

    # date_null_tokens — recognized missing-data sentinels for DATE fields.
    # If the key is absent, fall back to the module-level default so older
    # configs continue to behave correctly without any migration.
    raw_null_tokens = raw.get("date_null_tokens")
    if raw_null_tokens is None:
        date_null_tokens = _DEFAULT_DATE_NULL_TOKENS
    elif not isinstance(raw_null_tokens, list):
        raise PHIScrubError("date_null_tokens must be a list of strings")
    else:
        date_null_tokens = frozenset(str(t).strip().upper() for t in raw_null_tokens)

    # plausible_max_year — a date whose resolved year > this is treated as a
    # future placeholder. Absent key → high default (9999) so the check is a
    # NO-OP for configs that don't set it (full back-compat).
    raw_max_year = raw.get("plausible_max_year")
    if raw_max_year is None:
        plausible_max_year = _DEFAULT_PLAUSIBLE_MAX_YEAR
    else:
        if not isinstance(raw_max_year, int) or not (1900 <= raw_max_year <= 9999):
            raise PHIScrubError(
                f"plausible_max_year must be an int in [1900, 9999], got {raw_max_year!r}"
            )
        plausible_max_year = int(raw_max_year)

    # future_date_policy — what to do when a date's year > plausible_max_year.
    # Absent key → "quarantine" (fail-closed default).
    raw_fdp = raw.get("future_date_policy")
    if raw_fdp is None:
        future_date_policy = _DEFAULT_FUTURE_DATE_POLICY
    else:
        future_date_policy = str(raw_fdp)
        if future_date_policy not in _VALID_FUTURE_DATE_POLICIES:
            raise PHIScrubError(
                f"future_date_policy must be one of {sorted(_VALID_FUTURE_DATE_POLICIES)}, "
                f"got {future_date_policy!r}"
            )

    # unparseable_date_policy (Note 29) — blank the field + publish the row, or
    # quarantine the whole row. Absent key → "blank" (the autonomy-bar default).
    raw_udp = raw.get("unparseable_date_policy")
    if raw_udp is None:
        unparseable_date_policy = _DEFAULT_UNPARSEABLE_DATE_POLICY
    else:
        unparseable_date_policy = str(raw_udp)
        if unparseable_date_policy not in _VALID_UNPARSEABLE_DATE_POLICIES:
            raise PHIScrubError(
                "unparseable_date_policy must be one of "
                f"{sorted(_VALID_UNPARSEABLE_DATE_POLICIES)}, got {unparseable_date_policy!r}"
            )

    return PHIScrubConfig(
        compliance_posture=posture,
        subject_id_fields=subject_id_fields,
        date_patterns=date_patterns,
        id_patterns=id_patterns,
        birthdate_pattern=birthdate_pattern,
        max_jitter_days=max_jitter_days,
        orphan_quarantine_threshold=orphan_threshold,
        keep_patterns=keep_patterns,
        drop_patterns=drop_patterns,
        cap_rules=cap_rules,
        generalize_rules=generalize_rules,
        band_rules=band_rules,
        suppress_small_cell_patterns=suppress_patterns,
        age_field_patterns=age_field_patterns,
        death_date_patterns=death_date_patterns,
        age_cap_threshold=default_cap_threshold,
        age_cap_label=default_cap_label,
        small_cell_threshold=small_cell_threshold,
        date_null_tokens=date_null_tokens,
        partial_max_quarantine_fraction=partial_max_quarantine_fraction,
        plausible_max_year=plausible_max_year,
        future_date_policy=future_date_policy,
        unparseable_date_policy=unparseable_date_policy,
        no_subject_id_forms=no_subject_id_forms,
    )


# ── Key management ──────────────────────────────────────────────────────────


def load_key(path: Path | None = None) -> bytes:
    """Load the HMAC key from the sidecar file.

    Raises :class:`PHIKeyAccessDeniedError` if the calling process is in the
    ``llm-agent`` role (the key would let it forge pseudonyms / de-anonymize),
    :class:`PHIKeyMissingError` if the file is absent, and
    :class:`PHIKeyPermissionError` if the file mode is not ``0600``.

    This is the authoritative low-level reader; :class:`PHIKeyStore`
    (``scripts.security.phi_keystore``) wraps it for caching, fingerprinting,
    and zeroizable storage. The role gate lives here at the lowest level so a
    direct ``load_key()`` call cannot bypass it.
    """
    if is_llm_agent():
        raise PHIKeyAccessDeniedError(
            "PHI HMAC key access refused: REPORTAL_PROCESS_ROLE=llm-agent. "
            "The key is restricted to the trusted publish/scrub path."
        )
    path = path or config.PHI_KEY_PATH
    if not path.is_file():
        raise PHIKeyMissingError(
            f"PHI HMAC key not found at {path.name}. Use the web UI Load Study flow, "
            "or ask a developer/operator to provision the sidecar PHI key."
        )

    mode = path.stat().st_mode & 0o777
    if mode != _KEY_FILE_MODE:
        raise PHIKeyPermissionError(
            f"PHI key file {path.name} has mode {oct(mode)}; must be {oct(_KEY_FILE_MODE)}. "
            f"Fix with: chmod 600 {path.name}"
        )

    text = path.read_text(encoding="utf-8").strip()
    if len(text) != _KEY_HEX_LEN:
        raise PHIScrubError(
            f"PHI key at {path.name} must be {_KEY_HEX_LEN} hex chars (32 bytes); got {len(text)}"
        )
    try:
        return bytes.fromhex(text)
    except ValueError as exc:
        raise PHIScrubError(f"PHI key at {path.name} is not valid hex: {exc}") from exc


def bootstrap_key(path: Path | None = None) -> Path:
    """Generate a new 32-byte HMAC key and write it to the sidecar location.

    Refuses to overwrite an existing key (would silently invalidate every
    prior pseudonym). Returns the path on success.
    """
    path = path or config.PHI_KEY_PATH
    if path.exists():
        raise FileExistsError(
            f"PHI key already exists at {path}. Refusing to overwrite. "
            f"To rotate, delete the file explicitly — this will invalidate "
            f"every prior pseudonym and require full re-ingestion."
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    # Restrict parent dir perms best-effort (umask-dependent).
    with contextlib.suppress(OSError):
        path.parent.chmod(0o700)

    key_hex = secrets.token_hex(32)
    with path.open("w", encoding="utf-8") as _kf:
        _kf.write(key_hex)
        _kf.flush()
        os.fsync(_kf.fileno())
    path.chmod(_KEY_FILE_MODE)
    return path


# ── Scrub primitives ────────────────────────────────────────────────────────


def pseudo_id(raw_id: str, *, key: bytes, label: str = "ID") -> str:
    """Return ``RID_<LABEL>_<alpha12>`` with cryptographic domain separation.

    The HMAC input is ``f"{label}:{raw_id}"`` so the same raw value under
    different ``label`` arguments produces different pseudonyms. This
    implements the domain-separation property used by HKDF's ``info``
    parameter (RFC 5869 §3.2): if an adversary obtains two datasets where
    the same person appears under different id categories (e.g. ``FID``
    and ``SUBJID``), they cannot link records by pseudonym equality.

    Same ``(label, raw_id, key)`` always yields the same output → in-category
    longitudinal linkage is preserved across files, which is what the agent
    needs for cohort-level joins. Different ``key`` → disjoint pseudonym
    namespace.

    Args:
        raw_id: the raw identifier string (already stripped by the caller).
        key: 32-byte HMAC key loaded from the sidecar keyfile.
        label: short semantic category (e.g. ``"SUBJ"``, ``"FAM"``, ``"LAB"``).
            Propagated into the HMAC input for domain separation and retained
            inside the opaque ``RID_`` output token for audit clarity.

    Returns:
        ``f"RID_{label}_{alpha12}"`` — the output stays self-describing while
        avoiding raw-ID and phone-like shapes.
    """
    domain_input = f"{label}:{raw_id}".encode()
    raw_tag = hmac.new(key, domain_input, hashlib.sha256).hexdigest()[:_PSEUDO_TAG_CHARS]
    tag = raw_tag.translate(_HEX_TO_ALPHA)
    return f"RID_{label}_{tag}"


def date_offset_days(subject_id: str, *, key: bytes, max_days: int) -> int:
    """Per-subject deterministic offset in ``[-max_days, +max_days]`` inclusive.

    Algorithm: ``int.from_bytes(hmac_sha256(key, subject_id)[:4], 'big') %
    (2*max_days + 1) - max_days``.
    """
    if max_days < 1:
        raise ValueError(f"max_days must be >= 1, got {max_days}")
    digest = hmac.new(key, subject_id.encode("utf-8"), hashlib.sha256).digest()
    raw = int.from_bytes(digest[:_OFFSET_DIGEST_BYTES], "big")
    return (raw % (2 * max_days + 1)) - max_days


def _format_date(dt: datetime, *, fmt: str, has_time: bool, ampm: str | None) -> str:
    """Re-serialize *dt* in the detected source format.

    Preserves ISO / M-D-Y / D-M-Y layout. Two-digit years are promoted to
    four-digit on output (minor, not a correctness concern).

    OUTPUT CONTRACT (PS13): non-ISO dates are emitted as slash-separated,
    non-zero-padded integers (e.g. ``5/3/2014``, not ``05/03/2014``).  The
    row-level PHI redactor regex in phi_patterns.py must match this shape —
    a separate agent owns that side of the contract.
    """
    if fmt == "iso":
        if has_time:
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        return dt.strftime("%Y-%m-%d")

    if fmt == "mdy":
        date_part = f"{dt.month}/{dt.day}/{dt.year:04d}"
    elif fmt == "dmy":
        date_part = f"{dt.day}/{dt.month}/{dt.year:04d}"
    else:
        raise PHIScrubError(f"unsupported date format: {fmt}")

    if not has_time:
        return date_part

    if ampm:
        # Preserve 12-hour AM/PM layout
        hour_12 = dt.hour % 12 or 12
        time_part = f"{hour_12}:{dt.minute:02d}:{dt.second:02d} {ampm}"
    else:
        time_part = f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
    return f"{date_part} {time_part}"


def shift_date(
    value: str,
    offset_days: int,
    *,
    field_name: str | None = None,
    date_locales: dict[str, str] | None = None,
    default_locale: str | None = None,
) -> str | None:
    """Parse *value*, shift by ``offset_days``, re-emit in the same format.

    Returns ``None`` if the string does not parse as a date. Non-string
    inputs must be handled by the caller.

    Parameters
    ----------
    value:
        Raw date string to shift.
    offset_days:
        Number of days to add (may be negative).
    field_name:
        Column name for locale resolution (DMY allowlist + manifest lookup).
    date_locales:
        Per-column locale overrides from the study's ``_forms_manifest.yaml``
        ``date_locales:`` section.  Passed through to :func:`parse_date`.
    default_locale:
        Study-wide origin default (e.g. ``"DMY"`` for an Indian study) applied
        when the column locale is otherwise inconclusive.  Passed to
        :func:`parse_date`.
    """
    parsed = parse_date(
        value, field_name=field_name, date_locales=date_locales, default_locale=default_locale
    )
    if parsed is None:
        return None
    try:
        new_dt = parsed.dt + timedelta(days=offset_days)
    except (OverflowError, ValueError):
        return None
    return _format_date(
        new_dt,
        fmt=parsed.format,
        has_time=parsed.has_time,
        ampm=parsed.ampm,
    )


def _coerce_numeric(value: Any) -> float | None:
    """Return *value* as a float if convertible, else None.

    Accepts int, float, and numeric strings ("89", "89.0", " 89 "). Empty
    strings, None, and un-numeric text return None — caller should leave
    the field unchanged.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None  # guard: bool is an int subclass in Python
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


def cap_numeric(value: Any, *, threshold: int, label: str) -> tuple[Any, bool]:
    """Cap numeric *value* to *label* when strictly greater than *threshold*.

    Returns ``(new_value, was_capped)``. Non-numeric / empty values pass
    through unchanged with ``was_capped=False``. Values ≤ threshold also
    pass through unchanged — capping affects the tail only.

    Used for HIPAA §164.514(b)(2)(i)(C) age-over-89 aggregation and any
    similarly-shaped numeric-tail collapse rule. Because capping runs
    per-cell (not per-distribution), it is safe to apply in a streaming
    scrubber without seeing the rest of the dataset.
    """
    num = _coerce_numeric(value)
    if num is None:
        return value, False
    if num > threshold:
        return label, True
    return value, False


def generalize_value(value: Any, *, mapping: dict[str, str]) -> tuple[Any, bool]:
    """Map *value* to a broader category via *mapping* (case-insensitive).

    Returns ``(new_value, was_generalized)``. Non-string / empty values
    pass through unchanged with ``was_generalized=False``. Strings not
    present in the mapping return ``(value, False)``.

    As of T2.2, the caller (:func:`_scrub_row` rung 5) treats ``False``
    on a non-empty value as fail-closed (quarantine + hard-fail), mirroring
    :func:`band_categorical`. An unmapped non-empty string is never passed
    through to clean output and never silently dropped — the row is
    quarantined and :class:`PHIGeneralizeUnmappedError` is raised so an
    operator can curate the generalization map.
    """
    if value is None:
        return value, False
    if not isinstance(value, str):
        return value, False
    key = value.strip().lower()
    if not key:
        return value, False
    replaced = mapping.get(key)
    if replaced is None:
        return value, False
    return replaced, True


def band_categorical(value: Any, *, mapping: dict[str, str]) -> tuple[Any, bool]:
    """Fail-closed categorical band. ``(label, True)`` when *value* (stripped,
    lower-cased) is in *mapping*; otherwise ``(value, False)`` — caller treats a
    False on a non-empty value as fail-closed (quarantine), unlike
    :func:`generalize_value` which passes misses through."""
    if value is None or not isinstance(value, str):
        return value, False
    key = value.strip().lower()
    if not key:
        return value, False
    label = mapping.get(key)
    if label is None:
        return value, False
    return label, True


def band_numeric(value: Any, *, ranges: list[tuple[float | None, str]]) -> tuple[Any, bool]:
    """Fail-closed numeric range-band.

    *ranges* is an ascending list of ``(upper_inclusive, label)`` pairs. An entry
    whose upper bound is ``None`` is the open-ended catch-all top band and must be
    last. Returns ``(label, True)`` for the FIRST entry where
    ``upper is None or num <= upper``; ``(value, False)`` for non-numeric input or
    a value above every finite band with no catch-all — the caller quarantines on
    ``False``.

    ``load_scrub_config`` builds *ranges* directly from YAML ``{max: N, label}``
    (``max`` → ``upper_inclusive``) plus an optional final no-``max`` entry (the
    catch-all). No bound inversion is performed.
    """
    num = _coerce_numeric(value)
    if num is None:
        return value, False
    for upper, label in ranges:
        if upper is None or num <= upper:
            return label, True
    return value, False


def suppress_small_cell(value: Any, *, threshold: int) -> tuple[Any, bool]:
    """Clamp numeric *value* to at most *threshold*.

    Returns ``(new_value, was_clamped)``. Non-numeric / empty values pass
    through unchanged. Values strictly greater than the threshold collapse
    to the threshold itself (NOT to a label) so downstream numeric
    analyses remain type-stable.

    ICMR §11.7 recommends ``threshold=5`` for household / contact counts
    in cohort studies where unique household demographics could re-identify
    a subject. For counts at or below the threshold, the value passes
    through — small cells here are an analytic concern, not a privacy
    concern.
    """
    num = _coerce_numeric(value)
    if num is None:
        return value, False
    if num > threshold:
        # Preserve original type where possible: int stays int, float stays float.
        if isinstance(value, int | float) and not isinstance(value, bool):
            return type(value)(threshold), True
        return threshold, True
    return value, False


# ── Orchestration ───────────────────────────────────────────────────────────


def _apply_field_only_rules(
    row: dict[str, Any],
    *,
    cfg: PHIScrubConfig,
    suppress_headers: frozenset[str] = frozenset(),
) -> dict[str, int]:
    """Remove fields that can be scrubbed without a subject ID.

    Mutates row in place; returns drop counts (same ``phi-scrub-<scope>:<field>``
    shape as ``_scrub_row``'s second return value).

    Applies in-place to *row*:
    * ``suppress_headers`` (priority-0, Fix A-1/A-2) — force-drop set of
      direct-identifier headers (pre-normalized, same as ``_scrub_row``'s
      priority-0 gate).  Applied first so a direct identifier in the
      force-drop set is never exposed in the quarantine/amber zone even when
      the scrub config would otherwise keep it raw.  Default empty frozenset
      keeps legacy callers working with no behaviour change.
    * ``drop_fields``   — field removed entirely (rule 3 in the main scrub loop).
    * ``birthdate_field`` — field dropped unconditionally regardless of posture.
      Under ``limited_dataset``, jitter (rule 7) requires a subject_id; orphans by
      definition lack one, so the only safe option is drop.

    Deliberately omits rules that need per-subject state: date jitter,
    ID pseudonymization, cap, generalize, and small-cell suppression.
    This is the "partial scrub" applied to orphan rows before quarantine write.
    """
    counts: dict[str, int] = {}

    def _bump(scope: str, field: str) -> None:
        k = f"phi-scrub-{scope}:{field}"
        counts[k] = counts.get(k, 0) + 1

    for field in list(row.keys()):
        if field.startswith("__"):
            continue
        # Priority-0: force-drop direct identifiers (Fix A-1/A-2).
        # A header in the suppress set is dropped unconditionally — it overrides
        # both keep_fields and the absence of a drop_fields rule, mirroring
        # _scrub_row's priority-0 gate so quarantine/orphan writes are as clean
        # as the main published rows.
        norm = _normalize_header_for_lookup(field)
        if norm in suppress_headers:
            del row[field]
            _bump("drop", field)
            continue
        if cfg.field_is_keep(field):
            continue
        if cfg.field_is_birthdate(field):
            del row[field]
            _bump("birthdate-drop", field)
            continue
        # Death date (Note 32): age-dependent jitter needs a per-subject offset, which
        # an orphan lacks — so the only safe option is drop (mirrors birthdate above).
        if cfg.field_is_death_date(field):
            del row[field]
            _bump("drop", field)
            continue
        if cfg.field_is_drop(field):
            del row[field]
            _bump("drop", field)

    return counts


def _quarantine_or_raise(
    rows: list[dict[str, Any]],
    filename: Path,
    error_class: type[PHIScrubError],
    msg: str,
    *,
    quarantine_dir: Path,
    cfg: PHIScrubConfig,
    partial_on_review: bool,
    suppress_headers: frozenset[str] = frozenset(),
) -> None:
    """Partial-scrub, write, and optionally raise for a batch of quarantine rows.

    PS11-simplify6: Consolidates the three near-identical quarantine blocks
    (generalize, band, date) into a single helper:
      1. mkdir + assert_write_zone for the quarantine directory.
      2. Apply _apply_field_only_rules (drop_fields + birthdate + force-drop set)
         to each row in-place BEFORE the quarantine write — identical to the
         original order, with Fix A-1/A-2: ``suppress_headers`` is threaded
         through so direct identifiers are stripped from quarantine/amber rows.
      3. Atomically write the scrubbed rows to ``quarantine_dir / filename``.
      4. Raise ``error_class(msg)`` in strict mode (not partial_on_review).
         In partial mode the rows are silently held in quarantine; the caller
         records them in its per-form tally as usual.

    Behavior is identical to the three inlined blocks it replaces.
    """
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    assert_write_zone(quarantine_dir)
    for _row in rows:
        _apply_field_only_rules(_row, cfg=cfg, suppress_headers=suppress_headers)
    atomic_write_jsonl(quarantine_dir / filename.name, rows)
    if not partial_on_review:
        raise error_class(msg)


def _resolve_subject_id(
    row: dict[str, Any],
    candidates: tuple[str, ...],
    dataset_has_subject_col: bool = True,
) -> str:
    """Resolve a subject ID value from *row* by trying *candidates* in order.

    Matching strategy:
      1. Exact field match on any candidate (e.g. ``SUBJID``, ``FID``).
      2. Suffix match on any candidate — ``SUBJID`` also matches
         form-prefixed variants like ``NC_SUBJID``, ``IS_SUBJID``,
         ``_<PREFIX>_SUBJID`` etc.

    Exact match always wins over suffix match so deterministic date-offset
    keying is preserved across heterogeneous CRF datasets.

    Returns the first non-empty stripped value, or an empty string if the
    row has no resolvable subject identifier (caller quarantines).
    """
    for cand in candidates:
        val = row.get(cand)
        if val is not None:
            s = str(val).strip()
            if s:
                return s
    for cand in candidates:
        suffix = "_" + cand
        for key, val in row.items():
            if not key.endswith(suffix):
                continue
            if val is None:
                continue
            s = str(val).strip()
            if s:
                return s

    if not dataset_has_subject_col:
        return str(row.get("source_file", "SYSTEM"))
    return ""


def _secondary_subject_id_base(field: str, candidates: tuple[str, ...]) -> str | None:
    """If *field* is a SECONDARY subject-ID column return its canonical base, else None.

    A secondary subject-ID is a canonical subject-ID name (``SUBJID`` / ``FID``,
    from ``candidates``) followed by an Excel re-entry suffix — a digit run, an
    ``_<digits>`` sheet-dedup tag, or both. These are the duplicate/linked
    re-entries the A1 resolver handles (Note 28).

        candidates = ("SUBJID", "FID")
        SUBJID2 / SUBJID_2 / SUBJID2_2 / SUBJID_3  -> "SUBJID"
        FID2 / FID5                                -> "FID"
        SUBJID / FID  (canonical — no suffix)      -> None
        SC_PROCID / FIDELITY / RANDID              -> None
    """
    name = field.strip()
    for base in candidates:
        if re.fullmatch(re.escape(base) + r"(?:\d+(?:_\d+)?|_\d+)", name, re.IGNORECASE):
            return base
    return None


def _now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scrub_row(
    row: dict[str, Any],
    *,
    cfg: PHIScrubConfig,
    key: bytes,
    date_locales: dict[str, str] | None = None,
    default_locale: str | None = None,
    dataset_has_subject_col: bool = True,
    suppress_headers: frozenset[str] = frozenset(),
    form_has_age: bool = True,
) -> tuple[dict[str, Any] | None, dict[str, int]]:
    """Scrub a single row. Return (scrubbed_row_or_None, per-field-counts).

    Priority (first match wins per field):
        1. keep_patterns       — allowlist, short-circuits every other rule
        2. birthdate_pattern   — posture-dependent drop or jitter
        3. drop_patterns       — field removed from row entirely
        4. cap_rules           — numeric > threshold → label
        5. generalize_rules    — value looked up in mapping
        6. band_rules          — value mapped to broad range/category
        7. suppress_small_cell — numeric > threshold → threshold
        8. date_patterns       — jitter via SANT per-subject offset
        9. id_patterns         — HMAC-SHA256 pseudonymize

    Returns ``None`` for the row when no resolvable subject_id — caller
    quarantines. Per-field counts are keyed by scope label
    (``phi-scrub-drop:FIELD``, ``phi-scrub-cap:FIELD`` etc.).
    """
    if (
        "_metadata" in row
        and isinstance(row["_metadata"], dict)
        and row["_metadata"].get("type") == "column_structure"
    ):
        row[_SCRUB_MARKER_FIELD] = _SCRUB_VERSION
        return row, {}

    subj_id = _resolve_subject_id(
        row, cfg.subject_id_fields, dataset_has_subject_col=dataset_has_subject_col
    )
    if not subj_id:
        return None, {}

    # Snapshot each canonical subject-ID's ORIGINAL value before the field loop
    # pseudonymizes it in place — the secondary-ID resolver (rung 1.5) must
    # compare re-entry columns against the pre-scrub canonical value, not the
    # RID_… token the id rung writes when SUBJID/FID is processed first.
    _orig_canonical = {
        base: _resolve_subject_id(row, (base,), dataset_has_subject_col=dataset_has_subject_col)
        for base in cfg.subject_id_fields
    }

    offset = date_offset_days(subj_id, key=key, max_days=cfg.max_jitter_days)
    counts: dict[str, int] = {}

    def _bump(scope: str, field: str) -> None:
        k = f"phi-scrub-{scope}:{field}"
        counts[k] = counts.get(k, 0) + 1

    # Iterate a snapshot of keys so we can mutate row in place.
    for field in list(row.keys()):
        # Skip pipeline-internal metadata
        if field.startswith("__"):
            continue

        # 0. FORCE-DROP override — phi_review's review (incl. SoT cross-verification)
        # flagged this column as a DIRECT IDENTIFIER that must be removed even
        # though a broad form-prefix keep would otherwise publish it raw:
        # free-text suppression (comment / note / "other (specify)"), signatures,
        # initials, names, or any column the PDF-aware SoT flags PHI. Dropping is
        # STRICTER than any scrub keep and only ever ADDS protection — it never
        # under-protects. (The subject ID is NOT here — it is pseudonymized by the
        # id rule, since it is required for record linkage.) ``suppress_headers``
        # carries the combined force-drop set (suppress plus SoT direct identifiers).
        if suppress_headers and _normalize_header_for_lookup(field) in suppress_headers:
            del row[field]
            _bump("drop", field)
            continue

        # 1. KEEP — allowlist short-circuits every other rule
        if cfg.field_is_keep(field):
            continue

        # 1.5 SECONDARY SUBJECT-ID RESOLVER (A1 / Note 28) — a SUBJID{n}/FID{n}
        # Excel re-entry column. Trusted-code equality check (the scrub already
        # reads row values): an EXACT duplicate of the row's canonical subject ID
        # is dropped (provably-redundant re-entry); a DISTINCT value is
        # pseudonymized in the SAME label/keyspace as the canonical ID so the
        # case↔household link survives. "An ID is pseudonymized, never dropped" —
        # the drop here only ever removes the proven-redundant copy. Audit is
        # value-free: the scope records identical-drop vs distinct-pseudonymize,
        # never the raw ID.
        sec_base = _secondary_subject_id_base(field, cfg.subject_id_fields)
        if sec_base is not None:
            raw_val = row[field]
            if raw_val is None or (isinstance(raw_val, str) and not raw_val.strip()):
                del row[field]
                _bump("secondary-id-drop", field)
                continue
            canonical = _orig_canonical.get(sec_base, "")
            if canonical and str(raw_val).strip() == canonical:
                del row[field]
                _bump("secondary-id-drop", field)
            else:
                row[field] = pseudo_id(
                    str(raw_val).strip(), key=key, label=(cfg.id_label_for(sec_base) or "SUBJ")
                )
                _bump("secondary-id-pseudonymize", field)
            continue

        # 2. BIRTHDATE / DEATH DATE — age-dependent (Note 32).
        # When the form HAS an age field, these dates are redundant for
        # age-at-event and are DROPPED (Safe Harbor). When the form has NO age
        # field, they fall through to the date rung (7) and are JITTERED with the
        # same per-subject offset so the period/timeline stays derivable (drop is
        # the fallback there when a value cannot be jittered). Limited Dataset
        # always jitters the birthdate (falls through regardless of age).
        if (
            cfg.field_is_birthdate(field)
            and cfg.compliance_posture == _POSTURE_SAFE_HARBOR
            and form_has_age
        ):
            del row[field]
            _bump("birthdate-drop", field)
            continue
        if cfg.field_is_death_date(field) and form_has_age:
            del row[field]
            _bump("drop", field)
            continue
        # A birthdate (Safe Harbor) or death date in a NO-age form falls through to
        # the date rung (7) below and is JITTERED with the per-subject offset, so the
        # period/timeline stays derivable (drop is the fallback if it cannot jitter).

        # 3. DROP — field removed entirely from this row
        if cfg.field_is_drop(field):
            del row[field]
            _bump("drop", field)
            continue

        # 4. CAP — numeric > threshold collapsed to label
        cap_rule = cfg.cap_rule_for(field)
        if cap_rule is not None:
            raw_val = row[field]
            if raw_val is None or (isinstance(raw_val, str) and not raw_val.strip()):
                continue
            new_val, was_capped = cap_numeric(
                raw_val, threshold=cap_rule.threshold, label=cap_rule.label
            )
            if was_capped:
                row[field] = new_val
                _bump("cap", field)
            continue

        # 5. GENERALIZE — value mapped to a broader category. Fail-closed: an unmapped
        # non-empty value quarantines the whole row (return None) rather than leaking it
        # (the pre-T2.2 passthrough) or silently dropping it. Mirrors band (rung 5b).
        gen_rule = cfg.generalize_rule_for(field)
        if gen_rule is not None:
            raw_val = row[field]
            if raw_val is None or (isinstance(raw_val, str) and not raw_val.strip()):
                continue
            new_val, was_generalized = generalize_value(raw_val, mapping=gen_rule.mapping)
            if was_generalized:
                row[field] = new_val
                _bump("generalize", field)
                continue
            return None, {f"phi-scrub-generalize-quarantine:{field}": 1}

        # 5b. BAND — fail-closed categorical/numeric generalization. A value the
        # band cannot cover quarantines the whole row (return None) rather than
        # leaking it or silently dropping it.
        band_rule = cfg.band_rule_for(field)
        if band_rule is not None:
            raw_val = row[field]
            if raw_val is None or (isinstance(raw_val, str) and not raw_val.strip()):
                continue
            if band_rule.kind == "categorical":
                new_val, ok = band_categorical(raw_val, mapping=band_rule.mapping)
            else:
                new_val, ok = band_numeric(raw_val, ranges=band_rule.ranges)
            if ok:
                row[field] = new_val
                _bump("band", field)
                continue
            return None, {f"phi-scrub-band-quarantine:{field}": 1}

        # 6. SUPPRESS_SMALL_CELL — numeric > threshold clamped to threshold
        if cfg.field_is_suppress_small_cell(field):
            raw_val = row[field]
            if raw_val is None or (isinstance(raw_val, str) and not raw_val.strip()):
                continue
            new_val, was_suppressed = suppress_small_cell(
                raw_val, threshold=cfg.small_cell_threshold
            )
            if was_suppressed:
                row[field] = new_val
                _bump("suppress-small-cell", field)
            continue

        # 7. DATE — per-subject constant-offset jitter (includes birthdate when
        # posture = limited_dataset). Fail-closed: a non-empty value that does not
        # parse as a date (shift_date -> None) or whose slash-date locale is ambiguous
        # (parse_date -> ValueError) quarantines the whole row rather than leaking the
        # raw value (the pre-T2.3 passthrough) or crashing the run unaccountably.
        # Mirrors band (5b) / generalize (5).
        # Exception: recognized missing-data sentinels (date_null_tokens) are left
        # as-is (skipped) rather than quarantined — they are not real dates.
        is_birthdate_field = cfg.field_is_birthdate(field)
        # Age-dependent dates (Note 32) jitter here when the form has no age field:
        # a birthdate (Safe Harbor) or a death date that rung 2 let fall through.
        # Death dates usually also match field_is_date, but the explicit clause keeps
        # the routing correct even for a death-date name the date_fields regex misses.
        if (
            cfg.field_is_date(field)
            or (
                is_birthdate_field
                and (cfg.compliance_posture == _POSTURE_LIMITED_DATASET or not form_has_age)
            )
            or (cfg.field_is_death_date(field) and not form_has_age)
        ):
            raw_val = row[field]
            if raw_val is None or (isinstance(raw_val, str) and not raw_val.strip()):
                continue
            # Separator/whitespace-only values (e.g. '/  /', '//', '. .') are
            # missing dates — skip jitter rather than fail-closing.
            # PS7: count blank-separator dates in audit report same as other null tokens.
            if isinstance(raw_val, str) and _DATE_BLANK_RE.match(raw_val):
                _bump("date_null_token", field)
                continue
            # PS-L3: _is_date_sentinel also catches integer-typed sentinels (e.g. bare int
            # 99999999) and separator-form sentinels (9999-99-99) that bypass the string-only
            # is_date_null_token() lookup.
            if cfg.is_date_null_token(raw_val) or _is_date_sentinel(raw_val):
                _bump("date_null_token", field)
                continue
            # Future-dated value = placeholder (a real observation date is never
            # in the future). A date whose resolved year > cfg.plausible_max_year
            # is treated as a missing-data placeholder (sentinel) or quarantined
            # depending on cfg.future_date_policy. This runs BEFORE shift_date so
            # a future placeholder is never jittered and published.
            # Back-compat: plausible_max_year defaults to 9999, so this block is
            # a no-op unless the key is explicitly set in phi_scrub.yaml.
            if cfg.plausible_max_year < 9999:
                _resolved_year: int | None = None
                if isinstance(raw_val, datetime):
                    _resolved_year = raw_val.year
                else:
                    try:
                        _parsed = parse_date(
                            str(raw_val),
                            field_name=field,
                            date_locales=date_locales,
                            default_locale=default_locale,
                        )
                        if _parsed is not None:
                            _resolved_year = _parsed.dt.year
                    except ValueError:
                        # Locale-ambiguous → fall through to the existing
                        # shift_date path which will raise/quarantine correctly.
                        _resolved_year = None
                    if _resolved_year is None:
                        # parse_date rejects years beyond its plausible ceiling
                        # (e.g. 2914), so a far-future date returns None above. An
                        # ISO-leading-year value (YYYY-MM-DD…, the shape Excel
                        # datetime cells serialise to) is UNAMBIGUOUS — the year is
                        # always the leading 4 digits, no locale guessing. If those
                        # digits form a year and the month/day are valid, use it.
                        # Ambiguous DMY/slash/compact values are deliberately NOT
                        # reinterpreted here — they stay quarantined for review.
                        _iso = _ISO_LEADING_DATE_RE.match(str(raw_val).strip())
                        if _iso:
                            _y, _mo, _d = (
                                int(_iso.group(1)),
                                int(_iso.group(2)),
                                int(_iso.group(3)),
                            )
                            if 1 <= _mo <= 12 and 1 <= _d <= 31:
                                _resolved_year = _y
                if _resolved_year is not None and _resolved_year > cfg.plausible_max_year:
                    if cfg.future_date_policy == "sentinel":
                        # Treat as a missing-data placeholder. Unlike a text/digit
                        # null-token (self-evidently not a date, so naturally ignored
                        # by date math), a future DATE is valid-looking ISO and would
                        # silently corrupt interval/age computations if left in place.
                        # Blank it so it reads as genuinely missing in llm_source.
                        row[field] = ""
                        _bump("date_future_sentinel", field)
                        continue
                    else:  # "quarantine" — fail-closed: a future date is implausible
                        logger.warning(
                            "date-future-quarantine field=%s shape=%s",
                            field,
                            _mask_date_shape(str(raw_val)),
                        )
                        return None, {f"phi-scrub-date-quarantine:{field}": 1}
            try:
                shifted = shift_date(
                    str(raw_val),
                    offset,
                    field_name=field,
                    date_locales=date_locales,
                    default_locale=default_locale,
                )
            except ValueError:
                shifted = None
            # PS-simplify5 / Note 29: a value that survived the sentinel/null-token
            # and future-date checks but still cannot be parsed+shifted after every
            # format is exhausted. unparseable_date_policy decides the outcome:
            #   "blank"      → remove this single field (set ""), publish the rest of
            #                  the row. Strictly safe (the raw, un-jitterable date is
            #                  never published → no leak) and preserves the row's other
            #                  clean data. Mirrors the date_future_sentinel blank path;
            #                  count-only audit (no ledger event), like date_null_token.
            #   "quarantine" → withhold the whole row (prior fail-closed behavior).
            if shifted is None:
                logger.warning(
                    "date-unshiftable field=%s shape=%s policy=%s (declare a "
                    "date_null_tokens entry if this is a missing-data placeholder)",
                    field,
                    _mask_date_shape(str(raw_val)),
                    cfg.unparseable_date_policy,
                )
                if cfg.unparseable_date_policy == "blank":
                    row[field] = ""
                    _bump("date_unparseable_blanked", field)
                    continue
                return None, {f"phi-scrub-date-quarantine:{field}": 1}
            # Birthdate fields under limited_dataset fall through to the SAME per-subject
            # SANT offset as every other date — deliberately NOT clamped to force birth-year
            # preservation. Clamping the DOB to its original calendar year would give it a
            # different effective offset than the subject's other dates, breaking interval /
            # age-at-event preservation (the very property limited_dataset exists to provide;
            # see test_age_at_event_preserved_in_limited_dataset). Birth *year* is preserved
            # statistically because the offset (±max_jitter_days, default 30) is far smaller
            # than a year, so it almost never crosses a year boundary — a probabilistic
            # guarantee, not a hard clamp. (Considered and rejected: GAP-4 year-clamp.)
            row[field] = shifted
            _bump("date", field)
            continue

        # 8. ID — HMAC-SHA256 pseudonymize with domain-separated label
        id_label = cfg.id_label_for(field)
        if id_label is not None:
            raw_val = row[field]
            if raw_val is None or (isinstance(raw_val, str) and not raw_val.strip()):
                continue
            row[field] = pseudo_id(str(raw_val).strip(), key=key, label=id_label)
            _bump("id", field)

    row[_SCRUB_MARKER_FIELD] = _SCRUB_VERSION
    return row, counts


def _scrub_file(
    jsonl_path: Path,
    *,
    cfg: PHIScrubConfig,
    key: bytes,
    date_locales: dict[str, str] | None = None,
    default_locale: str | None = None,
    suppress_headers: frozenset[str] = frozenset(),
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, int],
]:
    """Read *jsonl_path*, scrub each row, return (kept, orphans, band_failed, generalize_failed, date_failed, counts).

    * kept              — rows that scrubbed successfully
    * orphans           — rows with no resolvable subject_id (no row_counts)
    * band_failed       — rows where a band rung returned (None, non-empty counts),
                          i.e. a value the band could not cover; caller quarantines
                          and hard-fails (fail-closed)
    * generalize_failed — rows where a generalize rung returned (None, non-empty counts),
                          i.e. a non-empty value not in the generalization map; caller
                          quarantines and hard-fails (fail-closed)
    * date_failed       — rows where the date rung returned (None, non-empty counts),
                          i.e. a non-empty date value that could not be safely jittered;
                          caller quarantines and hard-fails (fail-closed)
    * counts            — merged per-field scope counts for kept rows
    """
    kept: list[dict[str, Any]] = []
    orphans: list[dict[str, Any]] = []
    band_failed: list[dict[str, Any]] = []
    generalize_failed: list[dict[str, Any]] = []
    date_failed: list[dict[str, Any]] = []
    counts: dict[str, int] = {}

    # A dataset is subject-specific unless its filename matches a no_subject_id_forms pattern.
    dataset_has_subject_col = not any(pat in jsonl_path.name for pat in cfg.no_subject_id_forms)
    # Age-dependent date policy (Note 32): does this FORM carry an explicit age column?
    # Computed ONCE from the column set (consistent across rows — extraction pads every
    # row to the same columns), not per-row, so a sparse row never flips the decision.
    form_has_age: bool | None = None

    with jsonl_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            if form_has_age is None:
                form_has_age = any(
                    cfg.field_is_age(k) for k in row if not str(k).startswith("__")
                )

            # Idempotency guard: pre-scrubbed rows pass through unchanged.
            if row.get(_SCRUB_MARKER_FIELD) == _SCRUB_VERSION:
                kept.append(row)
                continue

            scrubbed, row_counts = _scrub_row(
                row,
                cfg=cfg,
                key=key,
                date_locales=date_locales,
                default_locale=default_locale,
                dataset_has_subject_col=dataset_has_subject_col,
                suppress_headers=suppress_headers,
                form_has_age=bool(form_has_age),
            )
            if scrubbed is None:
                if not row_counts:
                    orphans.append(row)
                else:
                    scope = next(iter(row_counts))
                    if scope.startswith("phi-scrub-generalize-quarantine"):
                        generalize_failed.append(row)
                    elif scope.startswith("phi-scrub-date-quarantine"):
                        date_failed.append(row)
                    else:  # band quarantine
                        band_failed.append(row)
                    for scope_k, n in row_counts.items():
                        counts[scope_k] = counts.get(scope_k, 0) + n
            else:
                kept.append(scrubbed)
                for scope, n in row_counts.items():
                    counts[scope] = counts.get(scope, 0) + n

    return kept, orphans, band_failed, generalize_failed, date_failed, counts


def _events_from_counts(
    counts_by_file: dict[str, dict[str, int]],
) -> list[dict[str, Any]]:
    """Flatten per-file count dicts into a sorted list of audit events."""
    events: list[dict[str, Any]] = []
    for file_name in sorted(counts_by_file):
        for scope_field, count in sorted(counts_by_file[file_name].items()):
            scope, _, field = scope_field.partition(":")
            events.append(
                {
                    "scope": scope,
                    "field": field,
                    "file": file_name,
                    "count": count,
                }
            )
    return events


def _emit_audit(
    *,
    study_name: str,
    posture: str,
    events: list[dict[str, Any]],
    orphans: dict[str, int],
    audit_path: Path,
) -> None:
    """Write the single-leg scrub audit atomically under the output zone.

    **Byte-reproducibility**: the primary report (``phi_scrub_report.json``)
    contains no wall-clock timestamps — identical inputs produce an identical
    byte-for-byte report on every re-run.  The ``generated_utc`` wall-clock
    field is written to a parallel sidecar (``phi_scrub_report_timing.json``)
    beside the primary report, mirroring the ``lineage.py`` content-only +
    ``*_timing.json`` pattern used for the lineage manifest.
    """
    assert_output_zone(audit_path.parent)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "study": study_name,
        "leg": "phi-scrub",
        "compliance_posture": posture,
        "scrubbed": events,
        "orphan_rows": orphans,
    }
    atomic_write_json(audit_path, payload)
    # Write the timing sidecar beside the primary report — wall-clock only,
    # never mixed into the content-only primary so re-runs are byte-identical.
    timing_path = audit_path.with_name(audit_path.stem + "_timing.json")
    atomic_write_json(timing_path, {"generated_utc": _now_utc_iso()})


_SCOPE_TO_ACTION: dict[str, str] = {
    "phi-scrub-drop": "drop",
    "phi-scrub-birthdate-drop": "birthdate_drop",
    "phi-scrub-id": "pseudonymize",
    "phi-scrub-date": "jitter_date",
    "phi-scrub-cap": "cap",
    "phi-scrub-generalize": "generalize",
    "phi-scrub-suppress-small-cell": "suppress_small_cell",
    "phi-scrub-band": "band",
    # A1 secondary-subject-ID resolver (Note 28): an exact-duplicate re-entry is
    # dropped; a distinct linked ID is pseudonymized in the canonical keyspace.
    "phi-scrub-secondary-id-drop": "drop",
    "phi-scrub-secondary-id-pseudonymize": "pseudonymize",
    # Quarantine scopes are intentionally absent: rows that were quarantined
    # were never published and must not produce PHI ledger entries for any
    # published dataset. The existing guard in _emit_as_written_ledger skips
    # events whose scope maps to None (unrecognised / not a ledger action).
    # "phi-scrub-band-quarantine" → omitted
    # "phi-scrub-generalize-quarantine" → omitted
    # "phi-scrub-date-quarantine" → omitted
}


def _compute_input_dataset_hash(datasets_dir: Path) -> str:
    """Return a stable SHA-256 over a sorted manifest of *datasets_dir* contents.

    **What.** Hex SHA-256 of a UTF-8 manifest string.
    **Why.** Seals the exact byte-content of every raw input file into the
    audit ledger so drift detection can prove which ``llm_source/`` artifacts
    correspond to which raw input snapshot.
    **How.** Build one line per ``*.jsonl`` file under *datasets_dir*, sorted
    by relative path::

        <relpath>\\t<size_bytes>\\t<sha256_of_file_bytes>

    Concatenate, encode as UTF-8, SHA-256 the result.

    Only ``*.jsonl`` files are included; non-JSONL files (e.g. crash-recovery
    ``.tmp*`` artefacts written by :func:`atomic_write_jsonl`) are excluded so
    transient files do not affect reproducibility across runs.
    """
    lines: list[str] = []
    for fpath in sorted(datasets_dir.rglob("*.jsonl")):
        if not fpath.is_file():
            continue
        relpath = fpath.relative_to(datasets_dir).as_posix()
        size = fpath.stat().st_size
        try:
            file_hash = hash_file(fpath)
        except OSError as exc:
            raise PHIScrubError(f"input manifest unhashable: {fpath} — {exc}") from exc
        lines.append(f"{relpath}\t{size}\t{file_hash}")
    manifest = "\n".join(lines)
    return hashlib.sha256(manifest.encode("utf-8")).hexdigest()


# -- Classification threading helpers ----------------------------------------


def _normalize_header_for_lookup(header: str) -> str:
    """Normalize a column header for approval-lookup (mirrors phi_review._normalize_header)."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", header.strip())
    s = re.sub(r"[^A-Za-z0-9]+", "_", s)
    return s.strip("_").lower()


def _load_approval_classifications(
    runs_dir: Path | None, run_id: str | None
) -> tuple[dict, dict[str, frozenset[str]], str | None]:
    """Load phi_handling_approval.json → (lookup, force_drop_by_stem, rule_bundle_sha256).

    ``force_drop_by_stem`` maps a dataset stem → the set of DIRECT-IDENTIFIER
    headers phi_review's SoT cross-verification decided must be dropped even
    though the scrub config would publish them raw (signatures, initials,
    free-text notes, SoT-flagged PHI). Keys are normalized to the same form
    ``_scrub_row`` compares against.

    Returns ({}, {}, None) when runs_dir/run_id is None, file absent, or JSON malformed.
    """
    if runs_dir is None or run_id is None:
        return {}, {}, None
    path = Path(runs_dir) / run_id / "phi_handling_approval.json"
    if not path.is_file():
        return {}, {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}, {}, None
    bundle_sha = (data.get("rule_bundle") or {}).get("rules_sha256")
    lookup: dict[str, dict[str, dict]] = {}
    force_drop_by_stem: dict[str, frozenset[str]] = {}
    for form in data.get("forms", []):
        stem = Path(str(form.get("form_name", ""))).stem
        per_header: dict[str, dict] = {}
        for cls in form.get("classifications", []):
            per_header[_normalize_header_for_lookup(str(cls.get("header", "")))] = {
                "action": cls.get("action"),
                "matched_rules": list(cls.get("matched_rules", []) or []),
                "jurisdictions": list(cls.get("jurisdictions", []) or []),
                "reasons": list(cls.get("reasons", []) or []),
            }
        if stem:
            lookup[stem] = per_header
            force_drop_by_stem[stem] = frozenset(
                _normalize_header_for_lookup(str(h))
                for h in form.get("force_drop_headers", []) or []
            )
    return lookup, force_drop_by_stem, bundle_sha


def _method_for_action(
    action: str, cfg: PHIScrubConfig | None, field: str
) -> tuple[str | None, dict]:
    """Return (method_name, method_parameters) for a given action + config."""
    if cfg is None:
        return None, {}
    if action == "cap":
        r = cfg.cap_rule_for(field)
        if r is not None:
            return "threshold_cap", {"threshold": r.threshold, "label": r.label}
        return "threshold_cap", {"threshold": cfg.age_cap_threshold, "label": cfg.age_cap_label}
    if action == "jitter_date":
        return "SANT_date_jitter", {"max_jitter_days": cfg.max_jitter_days}
    if action == "pseudonymize":
        return "HMAC-SHA256", {"label": cfg.id_label_for(field)}
    if action == "generalize":
        gen_rule = cfg.generalize_rule_for(field)
        return "generalization_map", {
            "map": gen_rule.mapping_name if gen_rule is not None else None
        }
    if action == "band":
        band_rule = cfg.band_rule_for(field)
        return "band_map", {
            "band": band_rule.band_name if band_rule is not None else None,
            "kind": band_rule.kind if band_rule is not None else None,
        }
    if action == "suppress_small_cell":
        return "small_cell_clamp", {"threshold": cfg.small_cell_threshold}
    if action in ("drop", "birthdate_drop"):
        return "field_removal", {}
    return None, {}


def _emit_as_written_ledger(
    *,
    events: list[dict[str, Any]],
    audit_path: Path,
    study_name: str | None,
    compliance_posture: str | None,
    dataset_files: list[str] | None = None,
    scrub_config_hash: str | None = None,
    input_dataset_hash: str | None = None,
    approval_lookup: dict | None = None,
    rule_bundle_sha256: str | None = None,
    cfg: PHIScrubConfig | None = None,
    force_drop_by_stem: dict[str, frozenset[str]] | None = None,
    pdf_source_by_stem: dict[str, str | None] | None = None,
) -> None:
    """Write one PHI as-written ledger under each dataset audit folder.

    Parameters
    ----------
    force_drop_by_stem:
        Per-stem set of normalized headers that were force-dropped at priority-0
        (direct identifiers from phi_review SoT cross-verification).  A column
        in this set is skipped by the keep-decision tracing loop so the ledger
        does not emit a contradictory "decided: keep / action: drop" pair for
        the same variable — the drop event (already emitted via the scrub loop)
        is the sole authoritative record.
    pdf_source_by_stem:
        Optional per-stem PDF source path for the ``where.pdf_source`` field in
        PHI ledger events.  ``None`` (the default) keeps the existing
        ``pdf_source=None`` behaviour for all stems; a partial dict leaves
        unresolved stems as ``None``.
    """
    audit_dir = audit_path.parent
    assert_output_zone(audit_dir)
    ensure_no_llm_sentinel(audit_dir)
    remove_dataset_no_llm_sentinels(audit_dir)
    (audit_dir / PHI_LEDGER_FILENAME).unlink(missing_ok=True)

    display_names = {Path(name).stem: name for name in dataset_files or []}
    grouped_events: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        action = _SCOPE_TO_ACTION.get(event["scope"])
        if action is None:
            # phi-scrub-keep and any unrecognized scopes are not PHI handling actions
            continue
        dataset_file = event["file"]
        stem = Path(dataset_file).stem
        display_names.setdefault(stem, dataset_file)
        grouped_events.setdefault(stem, []).append(event)

    for stem in sorted(display_names):
        writer = LedgerWriter(
            output_path=dataset_phi_ledger_path(audit_dir, display_names[stem]),
            scrub_config_hash=scrub_config_hash,
            input_dataset_hash=input_dataset_hash,
            study=study_name,
            leg="phi-scrub",
            compliance_posture=compliance_posture,
            sentinel_dir=audit_dir,
        )
        for event in grouped_events.get(stem, []):
            cls = (
                (approval_lookup or {})
                .get(stem, {})
                .get(_normalize_header_for_lookup(event["field"]))
            )
            if cls is not None:
                matched_rules = cls["matched_rules"]
                jurisdictions = cls["jurisdictions"]
                rationale = (
                    "; ".join(cls["reasons"])
                    or "Applied by PHI scrubber per phi_scrub.yaml configuration"
                )
                rule_taxonomy = matched_rules[0] if matched_rules else None
                rule_project_category = "|".join(jurisdictions) if jurisdictions else None
            else:
                matched_rules = []
                jurisdictions = []
                rationale = "Applied by PHI scrubber per phi_scrub.yaml configuration"
                rule_taxonomy = None
                rule_project_category = None
            action = _SCOPE_TO_ACTION[event["scope"]]
            method_name, method_parameters = _method_for_action(action, cfg, event["field"])
            # Fix B-5: populate pdf_source from the per-stem map when available;
            # fall back to None so the ledger is never fabricated.
            stem_pdf_source = (pdf_source_by_stem or {}).get(stem)
            writer.add_phi_event(
                form=Path(event["file"]).stem,
                variable_id=event["field"],
                action=action,
                rule_taxonomy=rule_taxonomy,
                rule_project_category=rule_project_category,
                rationale=rationale,
                dataset_file=event["file"],
                pdf_source=stem_pdf_source,
                count=event["count"],
                matched_rules=matched_rules,
                jurisdictions=jurisdictions,
                rule_bundle_sha256=rule_bundle_sha256,
                method_name=method_name,
                method_parameters=method_parameters,
            )
        # Fix B-3: KEEP tracing — after emitting events for a stem, BEFORE flush.
        # Skip any header that was force-dropped at priority-0: those headers
        # already have a drop event above; emitting a keep_decision for the same
        # variable would produce a contradictory "decided: keep / action: drop"
        # pair in the ledger, which an IRB auditor would flag as inconsistent.
        stem_force_drop = (force_drop_by_stem or {}).get(stem, frozenset())
        for norm_header, c in (approval_lookup or {}).get(stem, {}).items():
            if c.get("action") == "keep":
                if norm_header in stem_force_drop:
                    # Force-drop overrides keep — drop event is the sole record.
                    continue
                writer.add_keep_decision(
                    form=Path(display_names[stem]).stem,
                    variable_id=norm_header,
                    jurisdictions=c["jurisdictions"],
                    matched_rules=c["matched_rules"],
                    rationale=(
                        "; ".join(c["reasons"])
                        or "Retained per jurisdiction review (no PHI rule matched)."
                    ),
                    rule_bundle_sha256=rule_bundle_sha256,
                )
        writer.flush()


def run_scrub(
    study_name: str | None = None,
    *,
    run_id: str | None = None,
    runs_dir: Path | None = None,
    partial_on_review: bool = False,
) -> None:
    """Orchestrate the scrub: load key + config, walk staging, emit audit.

    Pre-conditions:
        * ``tmp/{STUDY}/datasets/*.jsonl`` is populated by Step 1+3.
        * ``PHI_KEY_PATH`` exists and is mode 0600 — else hard-fail.
        * A ``phi_scrub.yaml`` config is present — else the module no-ops and
          writes an empty audit (so downstream audit tooling always finds a
          fourth file).

    Parameters
    ----------
    study_name:
        Override the study name used in the audit report.  Defaults to
        ``config.STUDY_NAME``.
    run_id:
        Optional run identifier.  When provided (together with *runs_dir*),
        a ``scrub.in_progress`` token is written to
        ``runs_dir/{run_id}/scrub.in_progress`` before the scrub loop and
        deleted only on successful completion.  If the process is killed
        mid-loop the token persists, allowing the wrapper CLI to detect the
        partially-scrubbed state and refuse with exit 6.

        **SIGKILL race window:** there is a small window between the
        ``atomic_write_json`` that creates ``scrub.in_progress`` and the
        first call to ``atomic_write_jsonl`` that mutates a dataset row.  A
        SIGKILL in that window leaves the token on disk with *zero* rows
        mutated.  On the next operator retry the wrapper sees the token and
        exits with code 6.

        This is **not** a data-loss condition — no data was written.  If
        ``scrub.in_progress`` is present and the output datasets are still in
        their original pre-scrub state (zero mutations), it is safe for the
        operator to delete the token file and restart the pipeline normally.
    runs_dir:
        Directory under which per-run sidecars are stored
        (e.g. ``output/{STUDY}/runs``).  Required when *run_id* is set.
    partial_on_review:
        When ``False`` (default) the scrub is strictly fail-closed: the FIRST
        form holding a row that cannot be safely jittered/mapped
        (date/band/generalize) raises ``PHIDateUnshiftableError`` /
        ``PHIBandUnmappedError`` / ``PHIGeneralizeUnmappedError`` and aborts the
        whole study.  When ``True`` (set by the host pipeline) the scrub instead
        **quarantines only the failing ROWS** (already field-scrubbed before the
        quarantine write, kept in the AMBER no-LLM zone) and **publishes each
        form's remaining fully-scrubbed rows**, so one bad form never blocks the
        rest of the study.  The per-form quarantine counts are written to a
        ``scrub_outcome.json`` sidecar in the run dir for the wrapper CLI to
        surface as a non-blocking partial-run notice.

        **Elevated-review flag (not an abort)** — in partial mode, a form whose
        held fraction exceeds ``cfg.partial_max_quarantine_fraction`` (default 10%)
        or whose orphan count exceeds ``cfg.orphan_quarantine_threshold`` is marked
        ``elevated`` in its ``scrub_outcome.json`` entry: "published, but review
        recommended — likely a systemic data/config issue, not a small tail." It is
        deliberately NOT a hard abort: halting the whole study would deny the
        operator every *clean* form's usable data, which contradicts the partial-
        publish contract. The elevated flag preserves the systemic-failure signal
        for the operator without blocking the queryable forms. (The
        :class:`PHIPartialThresholdExceededError` class is retained for API/strict-
        mode compatibility; it is no longer raised on the partial path.)

        The security invariant is unchanged either way: a row that cannot be
        safely scrubbed is NEVER published — it is quarantined, never emitted
        to ``llm_source/``. Every PUBLISHED row is individually correct (row-level
        fail-closed scrub/date logic), so an elevated form is incomplete, never
        corrupt.

    Post-conditions:
        * Datasets JSONL rewritten in place with scrubbed values + ``_phi_scrubbed``
          marker.
        * Orphan rows (missing subject_id) land under ``tmp/{STUDY}/quarantine/``.
        * Fourth audit report emitted at :data:`config.AUDIT_SCRUB_REPORT_PATH`.
        * Sentinel ``tmp/{STUDY}/.phi_scrub_complete`` marks the run.
    """
    if study_name is None:
        study_name = config.STUDY_NAME

    audit_path = Path(config.AUDIT_SCRUB_REPORT_PATH)
    staging_root = Path(config.STUDY_STAGING_DIR)
    sentinel = staging_root / _SENTINEL_NAME
    staging_datasets = Path(config.STAGING_DATASETS_DIR)

    cfg = load_scrub_config()
    if cfg is None:
        # Missing scrub config = no rule application = raw PHI flows to
        # ``llm_source/``. That is unsafe for any run against real study data.
        #
        # GAP-5 (AUTOMATED FULL-SECURE): Disabled scrub is refused on real study
        # data, regardless of any operator/attacker env flag. The floor holds by
        # default — no operator flag required to activate it. The ONLY relaxation is
        # automatic test-context detection (is_test_context() == "pytest" loaded),
        # which no pipeline entry point can satisfy, so it cannot be spoofed from the
        # environment. Defense-in-depth: production_mode_enabled() ALSO forces the
        # raise even inside a detected test context, so the production flag can never
        # be overridden by a test signal.
        #
        # Legacy flow: when REPORTALIN_ALLOW_DISABLED_SCRUB=1 IS set AND we are in a
        # genuine (pytest) test context AND not production mode, the disabled-mode path
        # runs (no-op audit, no rule application). Anywhere else the env var has no
        # effect — the floor cannot be lowered.
        if config.production_mode_enabled() or not config.is_test_context():
            raise PHIScrubError(
                "phi_scrub: config not found at "
                f"{config.PHI_SCRUB_CONFIG_PATH}. Refusing to publish without "
                "rule application — raw PHI would flow through unredacted. "
                "Provision the YAML to enable scrubbing. "
                "(REPORTALIN_ALLOW_DISABLED_SCRUB is ignored on real study data.)"
            )
        # In a test context: allow the explicit opt-in env var to pass through.
        allow_disabled = os.environ.get("REPORTALIN_ALLOW_DISABLED_SCRUB", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if not allow_disabled:
            raise PHIScrubError(
                "phi_scrub: config not found at "
                f"{config.PHI_SCRUB_CONFIG_PATH}. Refusing to publish a trio "
                "bundle without rule application — raw PHI would flow through "
                "unredacted. Either provision the YAML or set "
                "``REPORTALIN_ALLOW_DISABLED_SCRUB=1`` to acknowledge the risk "
                "(dev / test only)."
            )
        logger.warning(
            "phi_scrub: config not found at %s — running in DISABLED mode "
            "(REPORTALIN_ALLOW_DISABLED_SCRUB=1). Raw PHI may flow through.",
            config.PHI_SCRUB_CONFIG_PATH,
        )
        _emit_audit(
            study_name=study_name,
            posture="disabled",
            events=[],
            orphans={},
            audit_path=audit_path,
        )
        # No config file → cannot produce a config hash; hashes stay None.
        _emit_as_written_ledger(
            events=[],
            audit_path=audit_path,
            study_name=study_name,
            compliance_posture="disabled",
            dataset_files=sorted(p.name for p in staging_datasets.glob("*.jsonl"))
            if staging_datasets.is_dir()
            else [],
            approval_lookup={},
            rule_bundle_sha256=None,
            cfg=None,
        )
        return

    # Config is present — seal the MERGED EFFECTIVE config hash (defaults +
    # per-study override) into every subsequent ledger write. Hashing the merged
    # dict (not a single file) keeps the hash collision-free across studies with
    # different overrides and matches verifier assertion 5, which calls the same
    # ``effective_scrub_config_hash`` helper.
    _eff_hash = effective_scrub_config_hash()
    scrub_config_hash: str = (
        _eff_hash if _eff_hash is not None else hash_file(Path(config.PHI_SCRUB_CONFIG_PATH))
    )

    # Load approval classifications (no-op when run_id/runs_dir absent or file missing).
    approval_lookup, sot_force_drop_by_stem, rule_bundle_sha256_val = (
        _load_approval_classifications(runs_dir, run_id)
    )

    # Sentinel short-circuit — prevents accidental double-scrub on restart.
    if sentinel.is_file():
        logger.info(
            "phi_scrub: sentinel %s present — staging already scrubbed, skipping",
            sentinel,
        )
        return

    from phi_engine.security.phi_keystore import clear_phi_key, get_phi_key

    # Note 12: the HMAC key must live in zeroizable storage and be wiped after
    # the scrub completes. PHIKeyStore wraps load_key() (same reader -> same key
    # bytes -> identical pseudonyms/date offsets) but holds the master in a
    # bytearray that clear_phi_key() overwrites in place; get_phi_key() returns an
    # immutable bytes COPY for the scrub primitives. The llm-agent role gate and
    # missing/perm/length fail-closed checks run unchanged inside the store.
    key = get_phi_key()
    try:
        # Load per-column date locale overrides from the study's forms manifest.
        # Backward-compatible: returns {} when the manifest is absent or has no
        # date_locales section.  The manifest lives next to the *raw* datasets dir,
        # not the staging dir, so we read it from config.DATASETS_DIR.
        # Import from the shared forms_manifest module (stays in scripts/) rather
        # than dataset_pipeline: after Note-19 consolidation the latter lives in the
        # dataset-to-llm-source skill, so importing it here would be a forbidden
        # skill→skill edge. forms_manifest is the canonical shared gate.
        from scripts.extraction.forms_manifest import check_forms_manifest

        # Reject-listed files are auto-skipped by the extraction leg, so the
        # scrub leg only needs the date_locales mapping here.
        date_locales: dict[str, str] = check_forms_manifest(config.DATASETS_DIR).date_locales

        # Note 29: study-origin default date locale. An Indian-origin study
        # defaults ambiguous/undeclared date columns to day-first (DMY) instead
        # of fail-closing — explicit allowlist/manifest declarations and
        # provably-decisive values (component > 12) still win (see parse_date
        # precedence). Read jurisdictions directly from config (NOT via
        # phi_review, which would be a forbidden skill→skill import; same
        # rationale as the forms_manifest note above). Fail-soft: any problem
        # leaves default_locale unset (None), preserving fail-closed behavior.
        default_locale: str | None = None
        try:
            _priv_path = config.study_config_path(
                "_study_privacy.yaml", study=study_name or config.STUDY_NAME
            )
            _priv = yaml.safe_load(_priv_path.read_text(encoding="utf-8")) or {}
            _juris = {str(j).upper() for j in (_priv.get("jurisdictions") or [])}
            if "INDIA" in _juris:
                default_locale = "DMY"
        except Exception as exc:  # fail-soft; never block the scrub on config issues
            logger.info(
                "phi_scrub: origin default_locale unresolved (%s); falling back to None", exc
            )

        if not staging_datasets.is_dir():
            logger.info(
                "phi_scrub: staging datasets dir missing (%s) — emitting empty audit",
                staging_datasets,
            )
            _emit_audit(
                study_name=study_name,
                posture=cfg.compliance_posture,
                events=[],
                orphans={},
                audit_path=audit_path,
            )
            # No input directory → cannot produce an input hash.
            _emit_as_written_ledger(
                events=[],
                audit_path=audit_path,
                study_name=study_name,
                compliance_posture=cfg.compliance_posture,
                dataset_files=[],
                scrub_config_hash=scrub_config_hash,
                approval_lookup={},
                rule_bundle_sha256=None,
                cfg=cfg,
            )
            return

        # Snapshot the raw input manifest BEFORE any in-place scrub rewrites so
        # the hash reflects the pre-scrub state, not the post-scrub state.
        dataset_files = sorted(p.name for p in staging_datasets.glob("*.jsonl"))
        input_dataset_hash: str = _compute_input_dataset_hash(staging_datasets)

        assert_write_zone(staging_datasets)

        # Write the in-progress token before any row mutation so a mid-loop crash
        # leaves the token on disk.  The wrapper CLI (P3.1) checks for this token
        # at startup and refuses with exit 6 if one is present.
        in_progress_token: Path | None = None
        if run_id is not None and runs_dir is not None:
            in_progress_token = runs_dir / run_id / "scrub.in_progress"
            in_progress_token.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(
                in_progress_token,
                {
                    "run_id": run_id,
                    "study": study_name if study_name is not None else config.STUDY_NAME,
                    "started_utc": datetime.now(UTC).isoformat(),
                    "scrub_yaml_sha256": scrub_config_hash,
                },
            )

        quarantine_dir = staging_root / "quarantine"
        counts_by_file: dict[str, dict[str, int]] = {}
        orphan_totals: dict[str, int] = {}
        # Per-form review-quarantine tally (only populated in partial_on_review mode).
        partial_forms: dict[str, dict[str, Any]] = {}

        # Per-form set of headers the scrub must FORCE-DROP at priority-0 (overriding
        # any keep), pre-normalized to match _scrub_row's comparison. Two sources, both
        # the honor-the-stricter-decision direction (only ever ADD protection):
        #   1. phi_review SUPPRESS decisions (free-text comment/other), and
        #   2. force_drop_headers from phi_review's SoT cross-verification — DIRECT
        #      IDENTIFIERS (signatures, initials, names, free-text notes, SoT-flagged
        #      PHI) that a broad form-prefix keep would otherwise publish raw.
        force_drop_by_stem: dict[str, frozenset[str]] = {
            stem: frozenset(
                h
                for h in (
                    {h for h, c in per_header.items() if (c or {}).get("action") == "suppress"}
                    | sot_force_drop_by_stem.get(stem, frozenset())
                )
                # Resolve the Action.SUPPRESS overload (Note 32): a small-cell COUNT
                # field (e.g. household-contact counts in suppress_small_cell_fields)
                # is CLAMPED — kept + top-coded by the suppress_small_cell rung — never
                # force-dropped. phi_review classifies free-text AND counts as SUPPRESS;
                # the docstring promises the scrubber picks the method by field type, but
                # the priority-0 force-drop was overriding the clamp and deleting counts.
                # Excluding small-cell fields here honors that intent. Free-text SUPPRESS
                # fields (no small-cell config) still force-drop as before.
                if not cfg.field_is_suppress_small_cell(h)
            )
            for stem, per_header in (approval_lookup or {}).items()
        }

        for jsonl_file in sorted(staging_datasets.glob("*.jsonl")):
            kept, orphans, band_failed, generalize_failed, date_failed, counts = _scrub_file(
                jsonl_file,
                cfg=cfg,
                key=key,
                date_locales=date_locales,
                default_locale=default_locale,
                suppress_headers=force_drop_by_stem.get(jsonl_file.stem, frozenset()),
            )

            if orphans:
                orphan_totals[jsonl_file.name] = len(orphans)
                quarantine_dir.mkdir(parents=True, exist_ok=True)
                assert_write_zone(quarantine_dir)
                orphan_counts: dict[str, int] = {}
                # Partial-scrub before write: drop_fields + birthdate + force-drop set
                # (Fix A-1/A-2: suppress_headers ensures direct identifiers flagged by
                # phi_review's SoT cross-verification are stripped from orphan/amber rows
                # just as they are from the main published rows).
                _stem_suppress = force_drop_by_stem.get(jsonl_file.stem, frozenset())
                for _orphan in orphans:
                    for scope_k, n in _apply_field_only_rules(
                        _orphan, cfg=cfg, suppress_headers=_stem_suppress
                    ).items():
                        orphan_counts[scope_k] = orphan_counts.get(scope_k, 0) + n
                if orphan_counts:
                    q_key = f"quarantine/{jsonl_file.name}"
                    counts_by_file[q_key] = orphan_counts
                atomic_write_jsonl(quarantine_dir / jsonl_file.name, orphans)
                if len(orphans) > cfg.orphan_quarantine_threshold and not partial_on_review:
                    # Strict mode only: an orphan-row count over the threshold signals a
                    # likely subject_id_fields misconfiguration → fail-closed-stop.
                    # Partial mode does NOT abort here (that would block every clean form
                    # too); instead the orphan rows are held in quarantine and the
                    # correctly-jittered non-orphan rows are published, with the form
                    # flagged ``elevated`` for review in the unified tally below.
                    raise PHIQuarantineOverflowError(
                        f"{jsonl_file.name}: {len(orphans)} orphan rows exceeds "
                        f"threshold {cfg.orphan_quarantine_threshold}. "
                        f"Check subject_id_fields config."
                    )

            if generalize_failed:
                _quarantine_or_raise(
                    generalize_failed,
                    Path(f"generalize_unmapped_{jsonl_file.name}"),
                    PHIGeneralizeUnmappedError,
                    f"{jsonl_file.name}: {len(generalize_failed)} row(s) hold values not covered "
                    f"by the configured generalization map for their field. generalize is "
                    f"fail-closed — curate the generalize map in phi_scrub.yaml to cover every "
                    f"valid value before these fields can be emitted. Quarantined rows: "
                    f"quarantine/generalize_unmapped_{jsonl_file.name}.",
                    quarantine_dir=quarantine_dir,
                    cfg=cfg,
                    partial_on_review=partial_on_review,
                    suppress_headers=force_drop_by_stem.get(jsonl_file.stem, frozenset()),
                )

            if band_failed:
                _quarantine_or_raise(
                    band_failed,
                    Path(f"band_unmapped_{jsonl_file.name}"),
                    PHIBandUnmappedError,
                    f"{jsonl_file.name}: {len(band_failed)} row(s) hold socioeconomic "
                    f"values not coverable by the configured band_maps/band_ranges. The "
                    f"band scaffold is fail-closed — fill the deferred TEMPLATE values in "
                    f"phi_scrub.yaml before these fields can be emitted. Quarantined rows: "
                    f"quarantine/band_unmapped_{jsonl_file.name}.",
                    quarantine_dir=quarantine_dir,
                    cfg=cfg,
                    partial_on_review=partial_on_review,
                    suppress_headers=force_drop_by_stem.get(jsonl_file.stem, frozenset()),
                )

            if date_failed:
                _quarantine_or_raise(
                    date_failed,
                    Path(f"date_unshiftable_{jsonl_file.name}"),
                    PHIDateUnshiftableError,
                    f"{jsonl_file.name}: {len(date_failed)} row(s) hold date values that cannot be "
                    f"safely jittered (unparseable, or an ambiguous slash-date with no date_locales "
                    f"entry). Date jitter is fail-closed — fix the source value or add a date_locales "
                    f"entry in the study's _forms_manifest.yaml before these fields can be emitted. "
                    f"Quarantined rows: quarantine/date_unshiftable_{jsonl_file.name}.",
                    quarantine_dir=quarantine_dir,
                    cfg=cfg,
                    partial_on_review=partial_on_review,
                    suppress_headers=force_drop_by_stem.get(jsonl_file.stem, frozenset()),
                )

            # Partial-publish mode: publish the form's correctly-scrubbed rows (``kept``)
            # and HOLD every un-scrubbable / un-jitterable / orphan row in the no-LLM
            # quarantine zone, recording a per-form tally so the wrapper can surface a
            # non-blocking partial-run notice.  Each PUBLISHED row is individually
            # correct: date jitter and locale resolution are fail-closed at the row
            # level (an ambiguous date is quarantined, never resolved under a guessed
            # locale), so a row only reaches ``kept`` when it scrubbed cleanly.
            #
            # A held-fraction over ``partial_max_quarantine_fraction`` or an orphan
            # count over ``orphan_quarantine_threshold`` no longer ABORTS the run — that
            # would deny the operator every clean form's usable data.  Instead the form
            # is flagged ``elevated`` ("published, but review recommended — likely a
            # systemic data/config issue") so the UI can distinguish a small tail of
            # bad rows from a systemic failure while keeping both queryable.  The PHI
            # security invariant is unchanged: a held row is NEVER promoted.  Strict
            # mode (``partial_on_review=False``) still aborts on the first failed row.
            review_q = len(date_failed) + len(band_failed) + len(generalize_failed)
            held_count = len(orphans) + review_q
            if partial_on_review and held_count:
                # PS3-M3: The held-fraction denominator counts only the reviewable-quarantine
                # rows (date/band/generalize failures), not orphans.  Orphans are governed by
                # their own orphan_quarantine_threshold guard; mixing them into the fraction
                # denominator inflated the total and could mask a high review-quarantine rate.
                total_for_frac = len(kept) + review_q
                held_frac = review_q / max(total_for_frac, 1)
                elevated = (
                    len(orphans) > cfg.orphan_quarantine_threshold
                    or held_frac > cfg.partial_max_quarantine_fraction
                )
                reasons = [
                    label
                    for label in (
                        f"orphan_no_subject_id:{len(orphans)}" if orphans else None,
                        f"date_unshiftable:{len(date_failed)}" if date_failed else None,
                        f"band_unmapped:{len(band_failed)}" if band_failed else None,
                        f"generalize_unmapped:{len(generalize_failed)}"
                        if generalize_failed
                        else None,
                    )
                    if label
                ]
                if elevated:
                    reasons.append(f"elevated_review:{held_frac:.0%}_held")
                # Variables involved (Note 30 follow-up): the offending column NAMES
                # are carried in the per-row count keys as
                # ``phi-scrub-{date,band,generalize}-quarantine:{field}`` — header
                # names only, never values. Surfacing them lets the human-review note
                # say WHICH variable could not be scrubbed, not just the reason class.
                # Orphan rows have no offending column (whole-row, missing subject_id).
                quarantine_columns = sorted(
                    {
                        k.split(":", 1)[1]
                        for k in counts
                        if ":" in k
                        and k.startswith(
                            (
                                "phi-scrub-date-quarantine:",
                                "phi-scrub-generalize-quarantine:",
                                "phi-scrub-band-quarantine:",
                            )
                        )
                    }
                )
                partial_forms[jsonl_file.name] = {
                    "kept": len(kept),
                    "quarantined": held_count,
                    "reasons": reasons,
                    "elevated": elevated,
                    "columns": quarantine_columns,
                }
                log_fn = logger.warning if elevated else logger.info
                log_fn(
                    "phi_scrub %s: PARTIAL publish — kept=%d, held-for-review=%d (%s)%s",
                    jsonl_file.name,
                    len(kept),
                    held_count,
                    ", ".join(reasons),
                    " [ELEVATED — review recommended]" if elevated else "",
                )

            atomic_write_jsonl(jsonl_file, kept)
            if counts:
                counts_by_file[jsonl_file.name] = counts
            logger.info(
                "phi_scrub %s: kept=%d orphaned=%d scopes=%d",
                jsonl_file.name,
                len(kept),
                len(orphans),
                len(counts),
            )

        events = _events_from_counts(counts_by_file)
        _emit_audit(
            study_name=study_name,
            posture=cfg.compliance_posture,
            events=events,
            orphans=orphan_totals,
            audit_path=audit_path,
        )
        # Fix B-5: Build a fail-soft per-stem → SoT policy YAML path map for the
        # ``where.pdf_source`` ledger field.  The SoT policy YAML is the nearest
        # available provenance artifact (it encodes the printed-PDF question text +
        # annotation geometry); if the file doesn't exist for a given form the stem
        # is simply absent from the map and the ledger falls back to pdf_source=None.
        # This is metadata-only (path existence check, no file reads or value access).
        # N3: the policy YAML lives in the AUDIT construction zone (fenced from the
        # LLM), not llm_source — this is trusted pipeline code, not an LLM read.
        _sot_root = Path(config.STUDY_AUDIT_DIR) / "SoT_construction"
        pdf_source_by_stem: dict[str, str | None] = {}
        for _stem in sorted({Path(f).stem for f in (dataset_files or [])}):
            _candidate = _sot_root / _stem / "pdf" / f"{_stem}_policy.yaml"
            if _candidate.is_file():
                pdf_source_by_stem[_stem] = str(_candidate)
        _emit_as_written_ledger(
            events=events,
            audit_path=audit_path,
            study_name=study_name,
            compliance_posture=cfg.compliance_posture,
            dataset_files=dataset_files,
            scrub_config_hash=scrub_config_hash,
            input_dataset_hash=input_dataset_hash,
            approval_lookup=approval_lookup,
            rule_bundle_sha256=rule_bundle_sha256_val,
            cfg=cfg,
            force_drop_by_stem=force_drop_by_stem,
            pdf_source_by_stem=pdf_source_by_stem if pdf_source_by_stem else None,
        )

        # Partial-run sidecar: record which forms had rows quarantined for review so
        # the wrapper CLI can mark the run partial and the Load Study UI can show a
        # non-blocking notice. Contains form NAMES + COUNTS only — never row values —
        # and lives under runs/ (outside the LLM read zone). Written whenever a run id
        # is available, even with an empty tally, so the wrapper can distinguish
        # "clean run" from "no sidecar / legacy run".
        if run_id is not None and runs_dir is not None:
            outcome_path = runs_dir / run_id / "scrub_outcome.json"
            outcome_path.parent.mkdir(parents=True, exist_ok=True)
            assert_write_zone(outcome_path.parent)  # N1: consistent with every other write site
            atomic_write_json(
                outcome_path,
                {
                    "run_id": run_id,
                    "study": study_name if study_name is not None else config.STUDY_NAME,
                    "partial": bool(partial_forms),
                    "partial_forms": partial_forms,
                },
            )

        with sentinel.open("w", encoding="utf-8") as _sf:
            _sf.write(_SCRUB_VERSION)
            _sf.flush()
            os.fsync(_sf.fileno())

        # Sentinel is written — scrub completed successfully.  Remove the
        # in-progress token so the wrapper does not see a false-positive on the
        # next invocation.  This must happen AFTER the sentinel write so that a
        # crash between the two leaves the token intact (safer direction: the
        # wrapper will still refuse, and the sentinel guarantees re-run is a no-op).
        if in_progress_token is not None:
            in_progress_token.unlink(missing_ok=True)
    finally:
        clear_phi_key()


# ── CLI ─────────────────────────────────────────────────────────────────────


def _cli_bootstrap_key(args: argparse.Namespace) -> int:
    target = Path(args.path) if args.path else Path(config.PHI_KEY_PATH)
    try:
        written = bootstrap_key(target)
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"PHI HMAC key written to: {written}")
    print("File mode: 0600. This key is outside the repo tree and agent scope.")
    print("Rotating (deleting the key) will invalidate every previously-scrubbed")
    print("artifact — downstream consumers must re-ingest from raw.")
    return 0


def _cli_key_path(args: argparse.Namespace) -> int:
    print(config.PHI_KEY_PATH)
    return 0


def _build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="phi_scrub")
    sub = parser.add_subparsers(dest="command", required=True)

    boot = sub.add_parser("bootstrap-key", help="Generate a new sidecar HMAC key")
    boot.add_argument("--path", type=str, default=None, help="Override key path")
    boot.set_defaults(func=_cli_bootstrap_key)

    path_cmd = sub.add_parser("key-path", help="Print the resolved key path")
    path_cmd.set_defaults(func=_cli_key_path)

    return parser


def _main(argv: Iterable[str] | None = None) -> int:
    parser = _build_cli()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
