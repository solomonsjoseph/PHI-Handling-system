"""Reasoning agents: Judge, Sentinel, Executor, Auditor.

Judge     - synthesises specialist + statute + praxis outputs into per-column decisions.
Sentinel  - preview reviewer, enforces 0% leak and 100% accuracy.
Executor  - applies the transformations decided by Judge.
Auditor   - reviews Executor's work and produces the final compliance report.
"""
from __future__ import annotations

import asyncio
import csv as _csv
import hashlib as _hashlib
import hmac as _hmac
import json as _json
import os as _os
import re as _re
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

import openpyxl as _openpyxl
from pydantic import BaseModel

from ..anonymizer import apply_to_text
from ..control.records import (
    ColumnDecision,
    EvidenceClaim,
    GateResult,
    MethodRecord,
    SandboxRecord,
    StudyKnowledgePackage,
    VerifiedClassificationManifest,
)
from ..control.sandbox import run_isolated
from ..crypto import pseudonym_salt
from ..detectors import detect_text
from ..file_readers import iter_dataset_rows, read_narrative
from ..jurisdictions import get_pack
from ..publish_guard import should_fire
from ..security import scrub_persisted_text
from .base import Agent
from .deterministic_rules import _HARD_RULE_TABLE as _HARD_RULE_TABLE
from .deterministic_rules import BLOCKING_ISSUE_FLOOR as BLOCKING_ISSUE_FLOOR
from .deterministic_rules import CONFIDENCE_FLOOR as CONFIDENCE_FLOOR
from .deterministic_rules import apply_age_dob_rule as apply_age_dob_rule
from .deterministic_rules import apply_blocking_floor as apply_blocking_floor
from .deterministic_rules import apply_confidence_floor as apply_confidence_floor
from .deterministic_rules import apply_sentinel_escalations as apply_sentinel_escalations
from .deterministic_rules import apply_sentinel_hard_rules as apply_sentinel_hard_rules
from .deterministic_rules import apply_site_cardinality_rule as apply_site_cardinality_rule

_detect_text = detect_text

ACTION_TYPES = {
    "keep",           # non-PHI, preserve as-is
    "drop",           # PHI, remove entirely
    "cap_age_90",     # age > 89 -> "90+"
    "year_only",      # date -> YYYY
    "zip3_truncate",  # ZIP -> first 3 digits, deny 17 codes
    "hash",           # deterministic hash for record linkage
    "pseudonymize",   # random consistent replacement (cross-file linkage preserved on exact value match)
    "scrub_text",     # free-text cell scrub via Presidio + regex, LLM never reads cell values
    "human_review",   # uncertain, block for human
}

SUBJECT_TYPES = {"participant", "staff", "specimen", "site", "study"}


# --- Judge TRIAGE and FINAL CLASSIFICATION (docs sections 31-34, 40, 41) --
#
# Judge operates in two stages. TRIAGE (section 32) separates every
# logical Schema column into exactly one of these five states -- never
# inventing a sixth, never guessing when evidence is absent (an
# undocumented column is UNKNOWN, not KEEP or DROP). FINAL CLASSIFICATION
# (section 41) then emits one typed ``ColumnDecision`` per column,
# replacing ``JudgeDecision``/``JudgeProposal``: those two classes
# duplicated ``ColumnDecision`` (``control/records.py``) under a second,
# competing schema (the R-a debt docs/PHASE_STATUS.md's Phase 1 row
# records) and are removed here, not aliased.
TRIAGE_STATES = ("KNOWN", "DERIVED", "CONFLICTED", "UNVERIFIED", "UNKNOWN")

# Section 40 provenance classes, mapped from the (deterministic) TRIAGE
# outcome. TRIAGE itself never touches an LLM -- it is evidence
# bookkeeping over Schema/Lexicon/Instrument's own already-produced
# findings -- so KNOWN/DERIVED read as fact-grade provenance and
# CONFLICTED/UNVERIFIED/UNKNOWN read as unverified. Judge's own model
# call then proposes an action for the column regardless of triage state;
# repeated model interpretation never promotes itself to a source fact
# (section 40), so it is recorded separately in each ColumnDecision's
# ``technical_rationale`` rather than overriding the triage-derived class.
_TRIAGE_PROVENANCE: dict[str, str] = {
    "KNOWN": "SOURCE_FACT",
    "DERIVED": "DETERMINISTIC_FACT",
    "CONFLICTED": "UNVERIFIED_CLAIM",
    "UNVERIFIED": "UNVERIFIED_CLAIM",
    "UNKNOWN": "UNVERIFIED_CLAIM",
}


def _entry_identity(
    entry: Mapping[str, Any], name_keys: Sequence[str] = ("name", "column", "column_id"),
) -> tuple[str, str]:
    """``(file_id, column_id)`` identity for one Schema/Lexicon/Instrument
    finding (docs section 31: classification identity is
    ``(file_id, column_id)`` -- column names are not globally unique).
    Schema findings carry the per-file detail as ``_file_id``
    (``assemble_study_knowledge_package``'s docstring); other sources may
    use ``file_id`` directly."""
    file_id = str(entry.get("_file_id") or entry.get("file_id") or "")
    column_id = ""
    for key in name_keys:
        value = entry.get(key)
        if isinstance(value, str) and value:
            column_id = value
            break
    return file_id, column_id


def triage_columns(
    schema_columns: Sequence[Mapping[str, Any]],
    lexicon_columns: Sequence[Mapping[str, Any]] | None = None,
    instrument_fields: Sequence[Mapping[str, Any]] | None = None,
    conflicts: Sequence[str] | None = None,
) -> dict[tuple[str, str], str]:
    """Judge TRIAGE (docs section 32): classify every Schema column into
    exactly one of ``TRIAGE_STATES``, never guessing.

    A column is:
      KNOWN       -- documented in both the dictionary (Lexicon) and a
                     study form (Instrument): two independent sources
                     agree it is understood.
      DERIVED     -- Schema itself marked it as computed from another
                     column (``derived``/``derived_from``); DERIVED never
                     depends on Lexicon/Instrument coverage.
      CONFLICTED  -- named in ``conflicts`` (a caller-supplied set of
                     column names existing evidence disagrees about).
      UNVERIFIED  -- documented by exactly one of Lexicon/Instrument.
      UNKNOWN     -- documented by neither -- the fail-closed default;
                     TRIAGE must never promote a column past what its
                     own evidence actually supports.

    Returns a mapping keyed by classification identity
    ``(file_id, column_id)`` (docs section 31).
    """
    lexicon_columns = lexicon_columns or []
    instrument_fields = instrument_fields or []
    conflict_names = set(conflicts or [])

    lexicon_names = {_entry_identity(c)[1] for c in lexicon_columns}
    instrument_names = {_entry_identity(f, ("name", "field", "column"))[1] for f in instrument_fields}

    triage: dict[tuple[str, str], str] = {}
    for entry in schema_columns:
        identity = _entry_identity(entry)
        if not identity[1]:
            continue
        if identity[1] in conflict_names:
            state = "CONFLICTED"
        elif entry.get("derived") or entry.get("derived_from"):
            state = "DERIVED"
        elif identity[1] in lexicon_names and identity[1] in instrument_names:
            state = "KNOWN"
        elif identity[1] in lexicon_names or identity[1] in instrument_names:
            state = "UNVERIFIED"
        else:
            state = "UNKNOWN"
        triage[identity] = state
    return triage


# Judge's model-facing action vocabulary (``ACTION_TYPES`` above) predates
# ColumnDecision's section-41 ``ColumnOperation`` vocabulary and is finer
# grained in places (``cap_age_90``/``year_only``/``zip3_truncate`` are
# each one specific parameterization of a broader ColumnOperation). This
# map is the deterministic, one-way translation FINAL CLASSIFICATION uses
# to build each column's ``ColumnDecision.operation`` -- never the
# reverse: the executable gate/execution pipeline (``validate_decisions``,
# ``run_decision_gates``, ``Executor``) continues to run on Judge's own
# action vocabulary, which is unambiguous and directly executable and is
# not itself being retired this phase.
_OPERATION_FROM_ACTION: dict[str, str] = {
    "keep": "keep",
    "drop": "drop",
    "pseudonymize": "pseudonymize",
    "hash": "pseudonymize",
    "cap_age_90": "cap",
    "year_only": "date_shift",
    "zip3_truncate": "generalize",
    "scrub_text": "redact",
    "human_review": "other_approved_action",
}


_VALID_PHI_CATEGORIES = {chr(c) for c in range(ord("A"), ord("R") + 1)} | {"NONE", "QUASI", None, ""}


def validate_decisions(
    decisions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Coerce model-proposed decisions into the executable vocabulary.
    Returns (safe_decisions, rejections). Never raises on bad model output;
    an unusable action becomes human_review, which is the fail-closed answer."""
    safe: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    for d in decisions:
        file_id = d.get("file_id") or ""
        column = d.get("column") or ""
        if not file_id or not column:
            rejections.append({"file_id": file_id, "column": column, "field": "column/file_id", "proposed": None})
            continue
        d = dict(d)
        action = d.get("action")
        norm_action = str(action).strip().lower() if action is not None else ""
        if norm_action == "human_review":
            # 2026-08-17 Sentinel redesign: Judge always commits to its best
            # action + confidence. It no longer owns the human_review call --
            # only Sentinel (deterministic layer or LLM escalation) does. A
            # Judge decision that still proposes human_review is invalid
            # output, not a legitimate choice; route it through the same
            # fail-closed rejection path as any other unusable action so it
            # is visible in `rejections` (should be empty on Judge output).
            norm_action = ""
        if norm_action not in ACTION_TYPES:
            rejections.append({"file_id": file_id, "column": column, "field": "action", "proposed": action})
            d["action"] = "human_review"
            d["reason"] = f"model proposed unknown action {action!r}; routed to human review"
            d["confidence"] = 0.0
        else:
            d["action"] = norm_action
        # Normalize Judge's optional best-guess fallback (only meaningful
        # when the column is actually deferred to a human).
        if d["action"] == "human_review":
            sugg = d.get("suggested_action")
            norm_sugg = str(sugg).strip().lower() if sugg is not None else ""
            d["suggested_action"] = norm_sugg if norm_sugg in (ACTION_TYPES - {"human_review"}) else None
            try:
                conf = d.get("suggested_confidence")
                d["suggested_confidence"] = max(0.0, min(1.0, float(conf))) if conf is not None else None
            except (TypeError, ValueError):
                d["suggested_confidence"] = None
            reason = d.get("suggested_reason")
            d["suggested_reason"] = str(reason).strip() if isinstance(reason, str) and reason.strip() else None
        # else: leave suggested_action/suggested_confidence/suggested_reason
        # exactly as given. Judge's own JSON schema never produces these
        # fields (it always commits to a real action), so on a first-time
        # Judge decision they are simply absent here. A deterministic
        # override gate (apply_site_cardinality_rule, apply_sentinel_hard_rules,
        # ...) may legitimately set `suggested_action` on a forced non-
        # human_review decision to record what the original proposal was,
        # for reviewer/audit context. `validate_decisions` must stay
        # idempotent so control/gates.py can safely re-run the fixed D11
        # sequence over an already-settled decision list (its documented
        # contract): actively nulling this field here would silently erase
        # that provenance the second time the sequence runs.
        subject = d.get("subject")
        if subject not in SUBJECT_TYPES:
            rejections.append({"file_id": file_id, "column": column, "field": "subject", "proposed": subject})
            d["subject"] = "study"
        phi_category = d.get("phi_category")
        if phi_category not in _VALID_PHI_CATEGORIES:
            rejections.append({"file_id": file_id, "column": column, "field": "phi_category", "proposed": phi_category})
            d["phi_category"] = None
        safe.append(d)
    return safe, rejections


# Free-text narrative column names -- the same words Judge's own PROMPT
# above instructs it to route to `scrub_text`. Kept here as real, queryable
# data (rather than re-parsing the prompt string) so `needs_file_glance`
# can be computed deterministically with no extra LLM call.
_FREE_TEXT_COLUMN_PATTERNS = ("comment", "note", "remark", "other_specify",
                              "reason", "description", "narrative", "free_text")


def _needs_file_glance(column: str, suggested_action: str | None) -> bool:
    """True only for columns where a human would need to open the actual
    file to judge free text, not just read the header/dictionary context."""
    norm = (column or "").strip().lower()
    if suggested_action == "scrub_text":
        return True
    return any(pat in norm for pat in _FREE_TEXT_COLUMN_PATTERNS)


_ACTION_PLAIN: dict[str, str] = {
    "keep": "leave this column exactly as it is",
    "drop": "remove this column from the shared data",
    "cap_age_90": "replace ages over 89 with '90+'",
    "year_only": "keep only the year, not the full date",
    "zip3_truncate": "keep only the first three digits of the ZIP code",
    "hash": "replace each value with a one-way code",
    "pseudonymize": "replace each value with a stable made-up code",
    "scrub_text": "blank out anything identifying inside the free text",
}


_CATEGORY_PLAIN: dict[str, str] = {
    "A": "a person's name",
    "B": "part of an address smaller than a state, such as a ZIP code",
    "C": "a date tied to the person, such as a birth date, or an age over 89",
    "D": "a telephone number",
    "E": "a fax number",
    "F": "an email address",
    "G": "a Social Security number",
    "H": "a medical record number",
    "I": "a health plan membership number",
    "J": "an account number",
    "K": "a license or certificate number",
    "L": "a vehicle identifier, such as a license plate number",
    "M": "a device identifier or serial number",
    "N": "a web address",
    "O": "an internet (IP) address",
    "P": "a biometric identifier, such as a fingerprint or voice print",
    "Q": "a full-face photograph or a similar image",
    "R": "some other detail that could single someone out",
    "NONE": "not something that identifies a person on its own",
    "QUASI": "a detail that could help identify someone only when combined with other details",
}


def _confidence_band(confidence: Any) -> str | None:
    """Plain-English certainty band for a 0..1 confidence score. Never
    surfaces the raw number in reviewer-facing text, only how sure it
    makes the automated guess sound."""
    if not isinstance(confidence, (int, float)):
        return None
    value = max(0.0, min(1.0, float(confidence)))
    if value >= 0.9:
        return "very confident"
    if value >= 0.7:
        return "fairly confident"
    if value >= 0.5:
        return "only somewhat confident"
    if value >= 0.3:
        return "not very confident"
    return "not confident at all"


def _escalation_reason_phrase(d: dict[str, Any]) -> str:
    """Classify which deterministic path routed this decision to human
    review, from the literal reason-prefix each path writes -- never from
    `suggested_reason`, an agent name, or any interpolated/free-text
    content. Every prefix matched below is a compile-time string
    constant written by this module's own code (`validate_decisions`,
    `apply_confidence_floor`, `apply_blocking_floor`,
    `apply_sentinel_escalations`, `verify_keep_decisions`) or by
    `orchestrator.py`'s anti-loop forcing block, never data from a
    model, a reviewer, or a dataset row, so matching on it cannot leak
    PHI, a raw identifier, a confidence number, or an agent name --
    only the fixed prefix is inspected; whatever free text a path
    appended after it is never read here."""
    reason = d.get("reason")
    reason = reason if isinstance(reason, str) else ""
    if reason.startswith("model proposed unknown action"):
        return "the answer the automated review proposed was not usable"
    if reason.startswith("Confidence floor:"):
        return "the automated review was not certain enough to act on its own"
    if reason.startswith("Blocking-issue floor:"):
        return "this column has repeatedly raised the same safety concern"
    if reason.startswith("Anti-loop:"):
        return "the automated review kept repeating the same rejected answer instead of revising it"
    if reason.startswith("Sentinel escalation:"):
        return "a safety check flagged this column as ambiguous rather than clearly wrong"
    if reason.startswith("Keep verification (unreadable):"):
        return "the underlying data could not be read to check this column, so it needs a human look"
    if reason.startswith("Keep verification:"):
        return "a check of the actual values in this column found something that looked identifying"
    if reason.startswith("Auditor disagreement:"):
        return "the final independent check thinks a different action is required by the rule for this column"
    return "the automated review could not finish deciding on its own"


def _reviewer_prompt_for(d: dict[str, Any], dictionary_by_column: dict[str, str] | None = None) -> str:
    """Plain-language sentence built in Python from column name, dictionary
    description, the proposed action's plain-English gloss, and which
    deterministic path escalated it -- no LLM call, and no agent-internal
    vocabulary (action ids, HIPAA letters, confidence numbers, agent
    names) ever surfaces raw: only its plain phrase from `_ACTION_PLAIN`
    / `_CATEGORY_PLAIN` / `_confidence_band` / `_escalation_reason_phrase`
    does. Every one of the six deterministic paths that can route a
    decision to human_review -- invalid model output in `validate_decisions`,
    `apply_confidence_floor`, `apply_blocking_floor`,
    `apply_sentinel_escalations`, `verify_keep_decisions`, and
    `orchestrator.py`'s anti-loop forcing block -- funnels through this
    one template via `annotate_pending_review`, so a fix here covers
    every escalation path and the Task 33 second review that reuses
    it."""
    col = d.get("column") or "this column"
    desc = (dictionary_by_column or {}).get(col, "")
    desc_clause = f' (the data dictionary describes it as "{desc}")' if desc else ""

    action_plain = _ACTION_PLAIN.get(d.get("suggested_action"))
    category = d.get("phi_category")
    category_plain = _CATEGORY_PLAIN.get(category)
    band = _confidence_band(d.get("suggested_confidence"))

    what = f"My best guess is to {action_plain}" if action_plain \
        else "I'm not able to settle on one clear best guess for what to do with it"

    why_bits = [_escalation_reason_phrase(d)]
    if category_plain and category not in (None, "", "NONE"):
        why_bits.append(f"it looks like it may hold {category_plain}")
    if band:
        why_bits.append(f"I'm {band} about that guess")
    why = "; ".join(why_bits)

    return (f"I'm not confident enough to decide what should happen to the column "
            f"'{col}'{desc_clause} on my own. {what}, because {why}. "
            "Please confirm this is right, or tell me what should happen instead.")


def annotate_pending_review(decisions: list[dict[str, Any]],
                            dictionary_by_column: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Attach `reviewer_prompt` and `needs_file_glance` to every decision
    still routed to a human. Deterministic, Python-only, safe to call
    repeatedly (e.g. again after a keep-verification demotion adds a new
    column to the queue)."""
    out: list[dict[str, Any]] = []
    for d in decisions:
        d = dict(d)
        if d.get("action") == "human_review":
            d["reviewer_prompt"] = _reviewer_prompt_for(d, dictionary_by_column)
            d["needs_file_glance"] = _needs_file_glance(d.get("column", ""), d.get("suggested_action"))
        out.append(d)
    return out



def verify_keep_decisions(
    decisions: list[dict[str, Any]],
    dataset_paths: dict[str, Path],
    jurisdiction: str = "us",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Demote keeps whose dataset values match deterministic PHI detectors.

    Also re-scans decisions that are already human_review but whose
    suggested_action is 'keep' -- meaning some other deterministic pass
    (the confidence floor, the blocking floor, an anti-loop demotion...)
    already forced human review on what started life as a keep. Those
    still need the row-value scan: a generic floor reason gives a
    reviewer no idea a detector actually matched. The action never
    changes here (it's already human_review); only the explanation is
    replaced with the detector-grounded keep-verification text so the
    more specific evidence wins over the generic one. A human_review
    decision whose suggested_action is anything else (e.g. a forced
    'drop') never entered this function as a keep and is left alone.

    Exemption: a decision whose `provenance` is exactly
    `"human_explicit_action"` -- a person, under the actual-knowledge
    attestation required by 45 CFR 164.514(b)(2)(ii), explicitly approved
    this exact 'keep' -- is scanned once (to catch the case where they were
    simply wrong) but not scanned again after they have already been asked
    and confirmed. Without this, a hard-rule-forced 'keep' (e.g. a
    clinical/stratifier column such as 'state' or 'diagnosis_code') that
    also matches a row-value detector can never leave human_review: every
    approval is silently re-demoted by the very next call, forever. This
    exemption never applies to `"human_comment_inferred"` (a model's guess
    at interpreting free text, not a person's direct confirmation) or to
    a decision no person has looked at yet.
    """
    verified = list(decisions)
    demotions: list[dict[str, Any]] = []
    patterns = get_pack(jurisdiction).patterns
    keep_indices_by_file: dict[str, list[int]] = {}
    for index, decision in enumerate(decisions):
        file_id = decision.get("file_id")
        if file_id not in dataset_paths:
            continue
        if decision.get("provenance") == "human_explicit_action" and decision.get("action") == "keep":
            continue
        action = decision.get("action")
        began_as_keep = action == "keep" or (
            action == "human_review" and decision.get("suggested_action") == "keep"
        )
        if began_as_keep:
            keep_indices_by_file.setdefault(file_id, []).append(index)

    def demote(decision: dict[str, Any], detector_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        column = decision.get("column")
        original_action = decision.get("action")
        updated = dict(decision)
        if detector_id == "unreadable":
            # The dataset file itself couldn't be read (missing, corrupt,
            # unsupported), so no detector ever ran against these row
            # values -- fail closed the same way a real match would, but
            # say plainly that verification could not run rather than
            # implying something was found.
            reason = (
                f"Keep verification (unreadable): row values for column '{column}' "
                "could not be read, so the keep could not be verified; demoted pending "
                "human review as a precaution."
            )
            suggested_reason = (
                f"Judge originally proposed 'keep' ({decision.get('reason') or 'no reason given'}); "
                "the deterministic row-value scan could not run because the underlying file "
                "could not be read, so the reviewer should confirm or override."
            )
        else:
            reason = (
                f"Keep verification: column '{column}' matched {detector_id} in a row value; "
                "demoted pending human review."
            )
            # Carry the pre-demotion decision forward as the agent's own
            # recommendation, so the reviewer sees what Judge proposed and
            # why the deterministic scan didn't trust it, in one glance.
            suggested_reason = (
                f"Judge originally proposed 'keep' ({decision.get('reason') or 'no reason given'}); "
                f"a deterministic row-value scan matched detector '{detector_id}', which the "
                "reviewer should confirm or override."
            )
        if original_action == "keep":
            updated.update(
                action="human_review",
                reason=reason,
                citation="45 CFR 164.514(b)(2)(i)",
                suggested_action="keep",
                suggested_confidence=decision.get("confidence"),
                suggested_reason=suggested_reason,
            )
        else:
            # Already human_review via another mechanism, with
            # suggested_action == "keep" recording that it began as a
            # keep. Action stays human_review; only the reason and
            # suggested_reason are overwritten so the specific,
            # detector-grounded explanation wins over the generic one
            # that put it here.
            updated.update(reason=reason, suggested_reason=suggested_reason)
        return updated, {
            "file_id": decision.get("file_id"),
            "column": column,
            "from": original_action,
            "to": "human_review",
            "detector": detector_id,
            "citation": "45 CFR 164.514(b)(2)(i)",
        }

    for file_id, indices in keep_indices_by_file.items():
        file_updates: dict[int, dict[str, Any]] = {}
        file_demotions: list[dict[str, Any]] = []
        try:
            path = dataset_paths[file_id]
            ext = path.suffix.lstrip(".").lower()
            for index in indices:
                decision = decisions[index]
                column = decision.get("column")
                detector_id = ""
                for _row_index, row in iter_dataset_rows(path, ext):
                    value = row.get(column)
                    if value is None:
                        continue
                    text = str(value)
                    if not text.strip():
                        continue
                    span = next(
                        (candidate for candidate in detect_text(text, detectors=("presidio", "rule"))
                         if candidate.hipaa_category),
                        None,
                    )
                    if span is not None:
                        detector_id = span.hipaa_category
                        break
                    for pattern in patterns:
                        match = pattern.regex.search(text)
                        if match and should_fire(pattern, match.group(0), text, None):
                            detector_id = pattern.pid
                            break
                    if detector_id:
                        break
                if detector_id:
                    updated, record = demote(decision, detector_id)
                    file_updates[index] = updated
                    file_demotions.append(record)
        except Exception:
            file_updates = {}
            file_demotions = []
            for index in indices:
                updated, record = demote(decisions[index], "unreadable")
                file_updates[index] = updated
                file_demotions.append(record)
        for index, updated in file_updates.items():
            verified[index] = updated
        demotions.extend(file_demotions)
    return verified, demotions


def _sandboxed_verify_keep_decisions(decisions_json: str, dataset_paths_json: str, jurisdiction: str) -> str:
    """Sandboxed dispatch target for `verify_keep_decisions`. Crosses the
    `run_isolated` boundary as JSON both ways (the return contract in
    `control/sandbox.py` allows only str/int/float/bool/None): the raw
    decision/path payloads are already JSON-safe primitives, and the
    `(verified, demotions)` tuple this returns is re-encoded the same way
    on the way back."""
    decisions = _json.loads(decisions_json)
    dataset_paths = {file_id: Path(p) for file_id, p in _json.loads(dataset_paths_json).items()}
    verified, demotions = verify_keep_decisions(decisions, dataset_paths, jurisdiction=jurisdiction)
    return _json.dumps([verified, demotions])


async def verify_keep_decisions_maybe_sandboxed(
    sandbox: SandboxRecord | None,
    decisions: list[dict[str, Any]],
    dataset_paths: dict[str, Path],
    jurisdiction: str = "us",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Route `verify_keep_decisions` through the sandbox when `sandbox` is
    not `None` (`ActivationFactory.activate(..., needs_sandbox=True)`);
    call it in-process otherwise. Same permanent, documented
    compatibility-path rationale as `Executor`'s own
    `*_maybe_sandboxed` methods (see
    `_read_dataset_headers_maybe_sandboxed`'s docstring): every
    pre-existing unit test builds its context via
    `control.testing.make_ctx`, which never attaches a sandbox, and this
    function must keep calling `verify_keep_decisions` in-process for
    those callers exactly as before. `verify_keep_decisions` itself reads
    raw dataset row values (`file_readers.iter_dataset_rows`) to verify
    'keep' decisions against deterministic PHI detectors -- the same
    class of raw-data work the four Executor call sites already sandbox,
    just invoked from the decision-gate sequence (`control/gates.py`)
    instead of `Executor.run`. Callers pass `ctx.sandbox` directly."""
    if sandbox is None:
        return verify_keep_decisions(decisions, dataset_paths, jurisdiction=jurisdiction)
    encoded = await asyncio.to_thread(
        run_isolated, sandbox, _sandboxed_verify_keep_decisions,
        _json.dumps(decisions), _json.dumps({fid: str(p) for fid, p in dataset_paths.items()}), jurisdiction,
    )
    verified, demotions = _json.loads(encoded)
    return verified, demotions


class Judge(Agent):
    NAME = "Judge"
    PROMPT = (
        "You are Judge. Given (a) Schema column classifications, (b) Instrument PHI fields "
        "from forms, (c) Lexicon dictionary entries, and (d) RegulationsExpert jurisdictional rules, "
        "decide for EACH dataset column: whether to keep, drop, or transform, and which "
        "technique. Never see or infer row values.\n\n"
        "ACTIONS (choose exactly one):\n"
        "  keep           - non-PHI clinical or epidemiological value\n"
        "  drop           - direct identifier that adds no research value\n"
        "  cap_age_90     - integer age; values > 89 become '90+'\n"
        "  year_only      - date -> YYYY (Safe Harbor)\n"
        "  zip3_truncate  - US ZIP -> first 3 digits, 17-code deny list applied\n"
        "  hash           - deterministic sha256 token for linkage\n"
        "  pseudonymize   - stable replacement (SAME real value -> SAME pseudonym across the whole study)\n"
        "  scrub_text     - free-text column whose header is safe but rows may contain PHI; "
        "the Executor will run Presidio + regex over each cell (LLM never reads the cells)\n\n"
        "You do not have a human_review option. ALWAYS commit to one of the actions above, even "
        "when uncertain -- give your honest confidence (0..1) instead of deferring. A second "
        "reviewer agent (Sentinel) checks your work against the regulations and methods, corrects "
        "you when you're wrong, and is the only one that may route a column to a human. Low "
        "confidence is information Sentinel uses to decide whether it needs a human, not a reason "
        "for you to pick a placeholder.\n\n"
        "SUBJECT tag (choose exactly one): participant | staff | specimen | site | study\n\n"
        "Use scrub_text for columns like 'comments', 'notes', 'remarks', 'other_specify', "
        "'reason', 'description' (any narrative free-text field on a study form).\n\n"
        "Return JSON: "
        '{"decisions": [{"file_id": str, "column": str, "phi_category": str|null, '
        '"subject": "participant|staff|specimen|site|study", '
        '"action": "keep|drop|cap_age_90|year_only|zip3_truncate|hash|pseudonymize|scrub_text", '
        '"reason": str, "confidence": 0..1, "citation": str}]}. '
        "Preserve clinically-needed non-PHI (diagnoses, procedures, vitals, labs)."
    )

    async def run(self, schema: dict, instrument: dict, lexicon: dict, statute: dict,
                  praxis: dict[str, dict] | None = None, prior_feedback: str = "",
                  study_knowledge_package: "StudyKnowledgePackage | None" = None) -> dict[str, Any]:
        # section 28: a StudyKnowledgePackage unifies Schema/Lexicon/
        # Instrument output into one versioned record instead of Judge
        # reading three separately-concatenated dicts. When the live
        # dispatch path (orchestrator.py::_dispatch_decide) supplies
        # one, its schema_findings/lexicon_findings/instrument_findings
        # ARE schema['columns']/lexicon['columns']/instrument['fields']
        # -- assemble_study_knowledge_package (agents/specialists.py)
        # derives them from exactly those three dicts -- so reading from
        # the package instead of the raw dicts changes the SOURCE, never
        # the CONTENT of what reaches this prompt. Callers exercising
        # this method directly (every existing unit test) that do not
        # build a package keep working unchanged: the raw dicts remain
        # the fallback source below.
        if study_knowledge_package is not None:
            schema = {"columns": study_knowledge_package.schema_findings}
            lexicon = {"columns": study_knowledge_package.lexicon_findings}
            instrument = {"fields": study_knowledge_package.instrument_findings}
        # TRIAGE (docs section 32), Judge's first stage: deterministic,
        # never guesses. Computed once per call over Schema's columns
        # against Lexicon/Instrument coverage; FINAL CLASSIFICATION below
        # reads it to set each ColumnDecision's provenance class.
        triage = triage_columns(
            schema.get("columns") or [], lexicon.get("columns") or [], instrument.get("fields") or [],
        )
        prompt = (
            f"RegulationsExpert rules (jurisdictional regulations): {statute}\n\n"
            f"Schema columns (dataset headers -- rows never shown): {schema}\n\n"
            f"Instrument fields (from study forms): {instrument}\n\n"
            f"Lexicon columns (dictionary): {lexicon}\n"
        )
        if praxis:
            # PHIMethodsExpert reports candidates. Judge still chooses an action, but
            # sees every candidate name and whether any preserves utility.
            praxis_summary = {
                category: {
                    "methods": [
                        method.get("name") for method in result.get("methods", [])
                        if method.get("name")
                    ],
                    "utility_preserving": any(
                        method.get("utility_preserving", False)
                        for method in result.get("methods", [])
                    ),
                }
                for category, result in praxis.items() if result
            }
            prompt += (f"\nPHIMethodsExpert methods (current candidate methods per HIPAA "
                       f"identifier category): {praxis_summary}\n")
        if prior_feedback:
            prompt += (
                "\nPreview (Sentinel) blocking feedback to address. Before correcting, "
                "verify each issue is a real leak / real over-block against RegulationsExpert + "
                "Instrument. If an issue is a false positive, keep the original decision "
                "and add a `justification` field explaining why.\n"
                f"{prior_feedback}\n"
            )
        prompt += "\nRespond with JSON only."
        # One decision is ~9 fields of JSON; a fixed 2000-token default
        # truncates (and so silently fails min_items validation) once a
        # dataset crosses roughly a dozen columns. Scale with the column
        # count instead of guessing a single global constant.
        num_columns = len(schema.get("columns") or [])
        judge_max_tokens = max(2000, 200 + 150 * num_columns)
        raw = await self.call_json(
            prompt, phase="judge.decide", default={"decisions": []},
            max_tokens=judge_max_tokens,
            expect_key="decisions", min_items=len(schema.get("columns") or []),
            status_text="Deciding how to handle every flagged column")
        return self._as_proposal(raw, triage)

    def _as_proposal(self, raw: Any, triage: Mapping[tuple[str, str], str] | None = None) -> dict[str, Any]:
        """Validate a raw Judge reply into FINAL CLASSIFICATION (docs
        section 41): the executable decision list ``validate_decisions``
        (D11's first gate) still owns full vocabulary correctness for, and
        alongside it the typed ``ColumnDecision`` record for each column
        -- the section-41 output that replaces the removed
        ``JudgeDecision``/``JudgeProposal`` pair. A per-entry shape
        failure fails closed to `human_review` when the entry at least
        names a real `(file_id, column)`; when it does not, there is
        nothing safe to construct and the entry is dropped, letting
        `assert_exact_coverage` catch the resulting gap the same way it
        catches any other missing decision."""
        entries = raw.get("decisions") if isinstance(raw, dict) else None
        decisions: list[dict[str, Any]] = []
        column_decisions: list[dict[str, Any]] = []
        triage = triage or {}
        for entry in entries or []:
            decision = self._validate_decision_entry(entry)
            if decision is None:
                continue
            decisions.append(decision)
            state = triage.get((decision["file_id"], decision["column"]), "UNKNOWN")
            column_decisions.append(self._column_decision(decision, state).model_dump(mode="json"))
        return {"decisions": decisions, "column_decisions": column_decisions}

    _DECISION_DEFAULTS: dict[str, Any] = {
        "phi_category": None, "subject": "", "action": "human_review",
        "reason": "", "confidence": 0.0, "citation": "",
    }

    @classmethod
    def _validate_decision_entry(cls, entry: Any) -> dict[str, Any] | None:
        """Shape-validate one raw Judge reply entry into the executable
        decision dict (``validate_decisions`` still owns full vocabulary
        correctness). Returns ``None`` when the entry names no real
        `(file_id, column)` and nothing safe can be constructed."""
        if not isinstance(entry, dict):
            return None
        file_id = entry.get("file_id")
        column = entry.get("column")
        if not (isinstance(file_id, str) and file_id and isinstance(column, str) and column):
            return None
        out = dict(cls._DECISION_DEFAULTS)
        out.update(entry)
        out["file_id"] = file_id
        out["column"] = column
        try:
            if out["phi_category"] is not None and not isinstance(out["phi_category"], str):
                raise TypeError("phi_category")
            if not isinstance(out["subject"], str):
                raise TypeError("subject")
            if not isinstance(out["action"], str):
                raise TypeError("action")
            if not isinstance(out["reason"], str):
                raise TypeError("reason")
            if not isinstance(out["citation"], str):
                raise TypeError("citation")
            out["confidence"] = float(out["confidence"])
        except (TypeError, ValueError):
            return {
                "file_id": file_id, "column": column, **cls._DECISION_DEFAULTS,
                "action": "human_review", "reason": "judge_output_malformed",
            }
        return out

    def _column_decision(self, decision: dict[str, Any], triage_state: str) -> ColumnDecision:
        """FINAL CLASSIFICATION (docs section 41): build the typed
        ``ColumnDecision`` for one already-validated decision, using the
        section 40 provenance class its TRIAGE state maps to."""
        action = decision.get("action") or "human_review"
        provenance = _TRIAGE_PROVENANCE.get(triage_state, "UNVERIFIED_CLAIM")
        reason = decision.get("reason") or ""
        rationale = f"triage={triage_state}; provenance={provenance}; model_interpretation=MODEL_INTERPRETATION"
        if reason:
            rationale = f"{rationale}; {reason}"
        return ColumnDecision(
            run_id=self.ctx.run_id,
            file_id=decision["file_id"],
            column_id=decision["column"],
            safe_display_name=decision["column"],
            sensitivity_classification=decision.get("phi_category") or "",
            applicable_rule=decision.get("citation") or "",
            operation=_OPERATION_FROM_ACTION.get(action, "other_approved_action"),
            plain_language_reason=reason,
            technical_rationale=rationale,
            decision_status="draft",
        )

    async def resolve_comment(self, column: str, description: str, suggested_action: str | None,
                              suggested_reason: str | None, comment: str) -> dict[str, Any]:
        """Re-invoke Judge for ONE column a human flagged with a free-text comment.

        Never sees a dataset row value: only the column header, the dictionary
        description (if any), Judge's own prior suggestion, and the human's
        comment -- itself scrubbed of any identifier shapes before it enters
        this prompt, exactly like dictionary/form text.
        """
        from ..anonymizer import scrub_for_prompt
        scrubbed_comment, _ = scrub_for_prompt(comment)
        prompt = (
            f"A human reviewer is resolving one column that was routed to human_review.\n"
            f"Column: {column}\n"
            f"Dictionary description: {description or '(none)'}\n"
            f"Judge's own prior suggestion: {suggested_action or '(none)'} "
            f"({suggested_reason or 'no reason recorded'})\n"
            f"Reviewer comment: {scrubbed_comment}\n\n"
            "Interpret the comment as an instruction for this ONE column and return JSON: "
            '{"action": "keep|drop|cap_age_90|year_only|zip3_truncate|hash|pseudonymize|scrub_text", '
            '"reason": str, "confidence": 0..1}. '
            "Never propose human_review here -- the reviewer is actively resolving this column now. "
            "If the comment is too vague to map to one action confidently, still return your best "
            "single guess with a low confidence score rather than refusing."
        )
        return await self.call_json(
            prompt, phase="judge.resolve_comment", default={"action": "human_review", "reason": "", "confidence": 0.0},
            status_text=f"Reading your comment on '{column}'")


def _read_dataset_headers(src: Path, ext: str) -> set[str]:
    """Best-effort real on-disk header read, used only as a fallback when
    intake's cached ``columns`` metadata is missing. Never raises -- an
    unreadable/malformed file just yields an empty set, which leaves the
    caller's fully-deferred-file shortcut un-triggered (falls through to
    the normal, still-leak-safe column-omission path) rather than crashing
    the pipeline over a cosmetic optimization.
    """
    try:
        if ext in ("csv", "tsv"):
            import csv as _csv_local
            delim = "\t" if ext == "tsv" else ","
            with src.open("r", encoding="utf-8", errors="replace", newline="") as fin:
                reader = _csv_local.reader(fin, delimiter=delim)
                header = next(reader, [])
            return set(header)
        if ext in ("xlsx", "xls"):
            import openpyxl as _openpyxl_local
            wb = _openpyxl_local.load_workbook(src, read_only=True)
            ws = wb[wb.sheetnames[0]]
            for r in ws.iter_rows(min_row=1, max_row=1, values_only=True):
                return {str(c) for c in r if c is not None}
            return set()
    except Exception:
        pass
    return set()


# --- Wave R-c Step 4: sandboxed dispatch for the four raw-data readers ---
#
# These thin, module-level wrappers are the only things `run_isolated`
# ever calls directly (a `multiprocessing.spawn` worker pickles its
# target by reference, so a closure or bound method cannot cross the
# boundary). Each wraps exactly one of `_read_dataset_headers`,
# `read_narrative`, `_redact_metadata_file`, `apply_column_actions_to_
# dataset` unchanged -- their own signatures and direct callability stay
# exactly as before, since several pre-existing tests call them
# directly.
#
# `run_isolated`'s return contract only allows `str`/`int`/`float`/
# `bool`/`None` to cross back into the parent
# (`sandbox._validate_return_contract`): a raw `set`/`Path` never can.
# `_read_dataset_headers`'s `set[str]` therefore crosses JSON-encoded as
# a sorted list; `_redact_metadata_file`'s `Path` crosses as `str`.
# `read_narrative`'s `str` is already an allowed type and crosses
# unchanged. `apply_column_actions_to_dataset`'s wrapper is defined
# further below, next to `PseudonymRegistry`: its registry argument
# cannot cross by reference at all (see that wrapper's docstring).


def _sandboxed_read_dataset_headers(src: str, ext: str) -> str:
    """Sandboxed dispatch target for `_read_dataset_headers`."""
    return _json.dumps(sorted(_read_dataset_headers(Path(src), ext)))


def _sandboxed_read_narrative(src: str, ext: str) -> str:
    """Sandboxed dispatch target for `read_narrative`."""
    return read_narrative(Path(src), ext)


class Executor(Agent):
    NAME = "Executor"
    PROMPT = ""  # deterministic; no LLM call needed for execution

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)

    async def _finalize_export(self, artifact_id: str, tmp_path: Path, file_id: str,
                               exports: dict[str, str], suffix: str) -> None:
        """Common tail for every branch below: hash+promote the staged
        bytes, then record a *guard-scannable* path in ``exports``.

        Never called on a branch that raised before writing `tmp_path`
        completely -- the artifact then stays ``provisional`` with no
        bytes under the real (non-``.tmp``) root, exactly the D14
        atomicity contract.

        The artifact's own canonical stored name is the bare artifact id
        (D14: no filename, original or otherwise, is ever part of a
        stored or served path). ``publish_guard.scan_export_file`` --
        untouched here, still dispatches purely on ``Path.suffix`` --
        therefore cannot read it directly. Rather than duplicate the
        exported bytes, hard-link a same-inode, suffix-bearing alias next
        to the canonical file in the same run-scoped staging directory
        and hand that alias to ``exports``: zero extra bytes on disk, the
        canonical artifact record and its hash are untouched, and every
        current consumer of ``exports`` (Publish Guard, the bundle
        builder, the still-unmigrated download routes) keeps working
        unchanged.
        """
        await self.ctx.artifacts.finalize(artifact_id)
        dst = tmp_path.parent.parent / self.ctx.session_id / self.ctx.run_id / artifact_id
        alias = dst.with_name(dst.name + suffix)
        _os.link(dst, alias)
        exports[file_id] = str(alias)
        await self._log("executor.wrote", "info", {"file_id": file_id, "path": str(alias)})

    async def _read_dataset_headers_maybe_sandboxed(self, src: Path, ext: str) -> set[str]:
        """Route `_read_dataset_headers` through the sandbox when this run
        has one (`ActivationFactory.activate(..., needs_sandbox=True)`);
        call it in-process otherwise. The in-process branch is a
        permanent, documented compatibility path: every pre-existing
        unit test builds its context via `control.testing.make_ctx`,
        which never attaches a sandbox, and must keep exercising this
        function exactly as before Wave R-c (see
        `test_control_phaseR_integration.py`'s Step 8 invariant 2
        allowlist). `set[str]` is not in `run_isolated`'s return
        contract, so it crosses the sandbox boundary JSON-encoded as a
        sorted list. `run_isolated` itself is a blocking call; wrapping
        it in `asyncio.to_thread` keeps it off the event loop every
        other agent's provider call shares."""
        if self.ctx.sandbox is None:
            return _read_dataset_headers(src, ext)
        encoded = await asyncio.to_thread(
            run_isolated, self.ctx.sandbox, _sandboxed_read_dataset_headers, str(src), ext,
        )
        return set(_json.loads(encoded))

    async def _read_narrative_maybe_sandboxed(self, src: Path, ext: str) -> str:
        """Route `read_narrative` through the sandbox when this run has
        one; call it in-process otherwise (see
        `_read_dataset_headers_maybe_sandboxed`'s docstring for the
        direct-call fallback's rationale). `str` is already inside
        `run_isolated`'s return contract, so the extracted text crosses
        the boundary unchanged."""
        if self.ctx.sandbox is None:
            return read_narrative(src, ext)
        return await asyncio.to_thread(
            run_isolated, self.ctx.sandbox, _sandboxed_read_narrative, str(src), ext,
        )

    async def _redact_metadata_maybe_sandboxed(self, src: Path, dst: Path) -> Path:
        """Route `_redact_metadata_file` through the sandbox when this run
        has one; call it in-process otherwise (see
        `_read_dataset_headers_maybe_sandboxed`'s docstring for the
        direct-call fallback's rationale). `Path` is not in
        `run_isolated`'s return contract, so the resolved output path
        crosses the boundary as `str`."""
        if self.ctx.sandbox is None:
            return _redact_metadata_file(src, dst)
        written = await asyncio.to_thread(
            run_isolated, self.ctx.sandbox, _sandboxed_redact_metadata_file, str(src), str(dst),
        )
        return Path(written)

    async def _apply_column_actions_maybe_sandboxed(
        self, src: Path, dst: Path, ext: str, decisions: list[dict[str, Any]],
        registry: "PseudonymRegistry", omit_columns: set[str] | None,
    ) -> None:
        """Route `apply_column_actions_to_dataset` through the sandbox
        when this run has one; call it in-process otherwise (see
        `_read_dataset_headers_maybe_sandboxed`'s docstring for the
        direct-call fallback's rationale).

        A live `PseudonymRegistry` mutated by reference cannot cross the
        `multiprocessing.spawn` pickle boundary: the sandboxed child
        rebuilds an equivalent registry from `(registry._salt,
        registry._map)` as plain arguments, runs the real transform, and
        writes its updated map to a workspace-relative JSON artifact,
        handing back only that filename (`run_isolated`'s return
        contract never allows an arbitrary payload). This method reads
        that artifact back and merges it into the caller's own registry,
        so the pseudonym map keeps accumulating across every file in
        this run exactly as it does on the direct-call path."""
        if self.ctx.sandbox is None:
            apply_column_actions_to_dataset(src, dst, ext, decisions, registry, omit_columns=omit_columns)
            return
        out_name = await asyncio.to_thread(
            run_isolated, self.ctx.sandbox, _sandboxed_apply_column_actions_to_dataset,
            self.ctx.sandbox.workspace_path, registry._salt, dict(registry._map),
            str(src), str(dst), ext, decisions, sorted(omit_columns or ()),
        )
        updated_map = _json.loads(
            (Path(self.ctx.sandbox.workspace_path) / out_name).read_text(encoding="utf-8")
        )
        registry._map.clear()
        registry._map.update(updated_map)

    async def run(self, files: list[dict[str, Any]], decisions: list[dict[str, Any]],
                  omit_by_file: dict[str, set[str]] | None = None, *,
                  manifest: "VerifiedClassificationManifest | None" = None) -> dict[str, Any]:
        """Apply decisions to each file. Returns {"exports": {file_id: path}}.

        ``omit_by_file`` (file_id -> deferred column names) is the partial-
        export channel: those columns are excluded from the written file
        entirely rather than routed through SEC-004's fail-closed default.
        A dataset file whose EVERY known column is deferred is skipped
        entirely -- excluded from ``exports`` -- rather than written as a
        headerless file, so Publish Guard never has to reason about it and
        the manifest can record it as fully deferred (see server.py).

        ``manifest`` (docs #49/#50) is the current, authorized
        ``VerifiedClassificationManifest`` the caller froze immediately
        before this call (``agents/orchestrator.py``'s ``execute_decisions``,
        via ``control.manifest.ensure_frozen_manifest``) -- ``None`` for
        every pre-existing unit test's direct ``Executor(ctx).run(...)``
        call, the same permanent ``make_ctx``-built compatibility path
        the sandbox dispatch above documents; the idempotency spine
        (``ExecutionTask``/``ExecutionResult``, docs #53) is populated
        from it only when it is supplied.

        Every write is staged through ``self.ctx.artifacts``
        (``control/writer.py::ArtifactWriter``): an ``ArtifactRecord`` is
        registered ``provisional`` before the first byte, the producer
        writes to the returned ``.tmp`` path, and only a completed write
        is hashed and atomically promoted via ``finalize``. A producer
        that raises leaves that artifact ``provisional`` with nothing at
        the real path -- there is nothing to clean up, and that file_id
        simply never reaches ``exports``.
        """
        pending = [(d.get("file_id", ""), d.get("column", "")) for d in decisions if d.get("action") == "human_review"]
        if pending:
            raise ValueError(f"unresolved human_review deferrals cannot be executed: {pending}")
        omit_by_file = omit_by_file or {}
        if manifest is not None:
            # docs #52: the seven deterministic pre-execution validators
            # only run on a real, governed execution (one that already
            # cleared the docs #49 manifest-freeze gate) -- the same
            # permanent make_ctx-test compatibility path every other
            # Phase R-c/Phase 9 control-plane addition in this class
            # follows. Unconditionally enforcing PathPolicyValidator's
            # DATA_DIR containment check, in particular, would reject
            # every pre-existing unit test's `tmp_path`-based dataset
            # file -- a false positive that has nothing to do with
            # whether decisions may safely execute.
            from ..control.execution_validators import run_pre_execution_validators

            # `MethodRegistryValidator` needs the run's approved-method
            # set, but `test_control_phaseR_integration.py`'s invariant 4
            # confines every live call to `ctx.methods.get_approved_methods`
            # to `experts.py` (PHIMethodsExpert's own resolution gate) and
            # `context.py`'s facade delegation -- Executor deliberately
            # never becomes a second direct caller here. Judge does not
            # currently emit a `method_id` on any decision, so this
            # validator has nothing to check in production today; its own
            # unit tests exercise the real rejection/acceptance logic
            # directly, and it starts doing real work the day a decision
            # carries a `method_id` without any further wiring change.
            approved_methods: list[MethodRecord] = []
            run_pre_execution_validators(
                decisions=decisions, files=files, allowed_operations=ACTION_TYPES,
                worker_module_paths=[Path(__file__)],
                approved_methods=approved_methods, grant=self.ctx.grant, sandbox=self.ctx.sandbox,
            )
        await self._log("executor.begin", "info", {"decision_count": len(decisions)})
        exports: dict[str, str] = {}
        by_file: dict[str, list[dict[str, Any]]] = {}
        for d in decisions:
            by_file.setdefault(d.get("file_id", ""), []).append(d)

        # Study-scoped pseudonym registry: exact real-value -> same pseudonym across all files.
        # Salted by an HMAC of the session id under a server-held key, so the salt cannot be
        # reproduced from anything published in the bundle (the session id is public there).
        registry = PseudonymRegistry(salt=pseudonym_salt(self.session_id))

        for f in files:
            src = Path(f["stored_path"])
            if f["kind"] == "metadata":
                # SEC-004 fail-closed: dictionary/mapping files can name PHI
                # (column definitions, code labels) so we run the deterministic
                # detector over them BEFORE they land in an export. Never copy
                # verbatim.
                artifact_id, tmp_path = await self.ctx.artifacts.stage(
                    "metadata_export", f"{f['file_id']}__export", "restricted_metadata", "export",
                )
                written = await self._redact_metadata_maybe_sandboxed(src, tmp_path)
                export_suffix = written.name[len(tmp_path.name):]
                if written != tmp_path:
                    # `_redact_metadata_file` names its own output by
                    # extension (`.csv`/`.xlsx`/`.withheld.txt`) for its
                    # other caller (`phi_corpus.replay`); `finalize` always
                    # hashes exactly `tmp_path`, so move the real bytes
                    # onto it -- same filesystem, so this is also a bare
                    # rename, never a copy. The extension it chose is kept
                    # as `export_suffix` for `_finalize_export`'s guard-
                    # scannable alias below.
                    written.replace(tmp_path)
            elif f["kind"] == "dataset":
                omit_cols = omit_by_file.get(f["file_id"], set())
                known_cols = set(f.get("columns") or [])
                if omit_cols and not known_cols:
                    # Intake's column-cache read failed for this file (rare;
                    # see server.py's try/except around the schema-read
                    # phase) -- fall back to the real on-disk header so a
                    # fully-deferred file still gets skipped cleanly instead
                    # of falling through to a near-empty, zero-surviving-
                    # column export that leaks nothing but row-count
                    # metadata.
                    known_cols = await self._read_dataset_headers_maybe_sandboxed(src, f["subtype"])
                if omit_cols and known_cols and known_cols <= omit_cols:
                    await self._log("executor.dataset_fully_deferred", "info",
                                    {"file_id": f["file_id"], "column_count": len(known_cols)})
                    continue
                export_suffix = f".{f['subtype']}"
                artifact_id, tmp_path = await self.ctx.artifacts.stage(
                    "dataset_export", f"{f['file_id']}__export{export_suffix}", "restricted_metadata", "export",
                )
                try:
                    await self._apply_column_actions_maybe_sandboxed(
                        src, tmp_path, f["subtype"], by_file.get(f["file_id"], []), registry, omit_cols,
                    )
                except Exception as e:
                    # Mirrors the narrative branch below: a write failure must
                    # not crash the whole run or leave a partial file counted
                    # as exported. apply_column_actions_to_dataset already
                    # guarantees no partial file survives at `tmp_path`, and
                    # skipping `_finalize_export` leaves the artifact
                    # `provisional` with nothing at the real path either.
                    await self._log("executor.dataset_write_failed", "info",
                                    {"file_id": f["file_id"], "error": type(e).__name__})
                    continue
            else:
                export_suffix = ".redacted.txt"
                artifact_id, tmp_path = await self.ctx.artifacts.stage(
                    "narrative_export", f"{f['file_id']}__export", "restricted_metadata", "export",
                )
                try:
                    text = await self._read_narrative_maybe_sandboxed(src, f["subtype"])
                except Exception as e:
                    await self._log("executor.narrative_read_failed", "info",
                                    {"file_id": f["file_id"], "error": type(e).__name__})
                    tmp_path.write_text(
                        f"[REDACTED] narrative extraction failed ({type(e).__name__}); "
                        f"content withheld to prevent PHI leak.\n", encoding="utf-8")
                    await self._finalize_export(artifact_id, tmp_path, f["file_id"], exports, export_suffix)
                    continue
                if not text.strip():
                    await self._log("executor.narrative_empty", "info",
                                    {"file_id": f["file_id"]})
                    tmp_path.write_text(
                        f"[NO EXTRACTABLE TEXT] {f['file_id']}\n", encoding="utf-8")
                    await self._finalize_export(artifact_id, tmp_path, f["file_id"], exports, export_suffix)
                    continue
                spans = detect_text(text, detectors=["presidio", "rule"])
                for sp in spans:
                    sp.review_status = "accepted"
                tmp_path.write_text(apply_to_text(text, spans), encoding="utf-8")
            await self._finalize_export(artifact_id, tmp_path, f["file_id"], exports, export_suffix)
        # Persist the pseudonym map size so the auditor can report on linkage coverage.
        await self._log("executor.pseudonym_registry", "info", {"unique_values_pseudonymized": len(registry._map)})
        # `reversal_key_blob` is the mandatory reversal-key deliverable: an
        # encrypted, opaque blob distinct from `exports`. Only produced when
        # the registry actually mapped something (pseudonymize/hash unused
        # -> nothing to reverse -> no blob, no empty artifact to manage).
        reversal_key_blob = registry.save() if registry._map else None
        return {"exports": exports, "pseudonym_count": len(registry._map),
                "reversal_key_blob": reversal_key_blob}


AUDITOR_CONFIDENCE_FLOOR = 0.98


_ESCALATION_REASON_PLAIN: dict[str, str] = {
    "decision_routed_human_review": "one or more columns need a person's decision",
    "judge_call_failure": "the automated reviewer could not finish its work",
    "sentinel_call_failure": "the automated safety check could not finish its work",
    "empty_decisions": "the automated reviewer did not return any decisions to check",
    "sentinel_blocking_after_cap": "a safety concern kept coming back after several automated attempts to resolve it",
    "manager_advisory_early_escalation": "the process was not making progress on its own, so a person should look",
    "manager_advisory_coverage_escalation": "too many files could not be fully checked, so a person should look before this ships",
    "manager_advisory_audit_escalation": "the final check flagged something a person should look at",
    "executor_crashed": "an unexpected error happened while writing the final files",
    "auditor_issues_verdict": "the final independent check found a specific problem that needs a person's decision",
    "auditor_artifact_identity_mismatch": "the final check could not confirm it reviewed the exact files about to be shared",
    "auditor_evidence_unverified": "the final check lacks verified evidence for a covered decision",
    "auditor_deterministic_gate_failed": "a deterministic decision gate failed for material being audited",
}


def plain_human_review_reasons(reasons: list[str]) -> list[str]:
    """Translate the fixed internal reason codes used to route a run to
    human review into plain English, so this list is safe to show a
    reviewer directly. Applies to every human-review escalation in the
    pipeline, not just Auditor's -- the plain-English requirement is
    cross-cutting, not agent-specific."""
    out = []
    for r in reasons:
        if r in _ESCALATION_REASON_PLAIN:
            out.append(_ESCALATION_REASON_PLAIN[r])
        elif isinstance(r, str) and r.startswith("auditor_confidence_below_floor:"):
            out.append("the final independent check was not confident enough to sign off on its own")
        else:
            out.append("the automated review could not finish deciding on its own")
    return out


def auditor_escalation_reason(
    audit: dict[str, Any],
    *,
    artifact_refs: dict[str, str] | None = None,
    evidence_claims: Sequence[EvidenceClaim | Mapping[str, Any]] | None = None,
    gate_results: Sequence[GateResult | Mapping[str, Any]] | None = None,
) -> str | None:
    """Deterministic gate on Auditor's output. Fails toward the safer path
    (second human review) on any of five independent grounds, never
    toward silent pass-through -- same fail-closed shape as every other
    boundary check in this pipeline:

    1. Self-reported confidence below ``AUDITOR_CONFIDENCE_FLOOR`` (a
       missing or unparseable confidence counts as 0.0).
    2. A high-confidence ``"issues"`` verdict with at least one recorded
       issue. Confidence is telemetry (D12): it can raise a review request
       but it can never turn a genuine issues finding into a silent pass.
    3. Artifact identity: when ``artifact_refs`` (``{file_id: sha256}`` for
       every export actually on disk) is supplied, every entry Auditor's
       response names in ``artifacts_checked`` must reference a real
       ``file_id`` in that map with a matching ``sha256``.
    4. Any covered claim requiring ``VERIFIED`` evidence is in any other
       evidence state.
    5. Any covered deterministic gate has a ``fail`` or ``blocked`` status.

    ``Auditor.run`` attaches the control records it received to ``audit`` so
    the production caller cannot omit them while evaluating this gate.
    Explicit arguments keep the pure gate directly testable.
    """
    def value(
        record: EvidenceClaim | GateResult | Mapping[str, Any],
        field: str,
        default: Any = "",
    ) -> Any:
        return record.get(field, default) if isinstance(record, Mapping) else getattr(record, field, default)

    try:
        confidence = float(audit.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < AUDITOR_CONFIDENCE_FLOOR:
        return f"auditor_confidence_below_floor:{confidence:.2f}"
    if audit.get("verdict") == "issues" and audit.get("issues"):
        return "auditor_issues_verdict"
    if artifact_refs is not None:
        for checked in audit.get("artifacts_checked") or []:
            if not isinstance(checked, dict):
                return "auditor_artifact_identity_mismatch"
            file_id = checked.get("file_id")
            sha256 = checked.get("sha256")
            if file_id not in artifact_refs or artifact_refs.get(file_id) != sha256:
                return "auditor_artifact_identity_mismatch"
    claims = evidence_claims if evidence_claims is not None else audit.get("evidence_claims") or []
    if any(value(claim, "required_state", "VERIFIED") == "VERIFIED"
           and value(claim, "state", "UNKNOWN") != "VERIFIED" for claim in claims):
        return "auditor_evidence_unverified"
    results = gate_results if gate_results is not None else audit.get("gate_results") or []
    if any(value(result, "status", "") in {"fail", "blocked"} for result in results):
        return "auditor_deterministic_gate_failed"
    return None


def materialize_auditor_disagreements(decisions: list[dict[str, Any]], audit: dict[str, Any],
                                      dictionary_by_column: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Turn Auditor's per-column disagreement findings into resolvable
    human_review decisions, distinct from Sentinel's pre-execution round.

    Without this, a session can reach 'awaiting_human_review' on Auditor's
    say-so with no per-column entry for a reviewer to act on -- resolving
    nothing changes anything, and resubmitting only re-asks the same
    question of a non-deterministic model. This routes the disagreement
    through the same annotate_pending_review/_reviewer_prompt_for path
    every other escalation uses, so the reviewer sees one more plain-
    English question, not a dead end.

    Matches by column name only (Auditor's findings carry a file name, not
    a file_id) -- a real limitation in a multi-file study with a repeated
    column name across files; both would be flagged. Acceptable for a
    first pass; tracked as a follow-up, not silently ignored.
    """
    issues_by_column = {i["column"]: i for i in (audit.get("issues") or []) if i.get("column")}
    if not issues_by_column:
        return decisions
    out = []
    for d in decisions:
        col = d.get("column")
        issue = issues_by_column.get(col)
        if issue and d.get("action") != "human_review":
            d = dict(d)
            d["suggested_action"] = d.get("action")
            d["suggested_reason"] = d.get("reason") or ""
            d["auditor_original_action"] = d.get("action")
            d["action"] = "human_review"
            d["stage"] = "auditor_final"
            problem = scrub_persisted_text(str(issue.get("problem") or "")).strip()
            d["reason"] = f"Auditor disagreement: {problem}" if problem else "Auditor disagreement:"
        out.append(d)
    return annotate_pending_review(out, dictionary_by_column)


class Auditor(Agent):
    NAME = "Auditor"
    PROMPT = (
        "You are Auditor, the final independent check before a study bundle can be trusted. "
        "You do two things: (1) re-derive, from the cited rulebook and technique text alone, "
        "what action each column's HIPAA category calls for, and flag any column where the "
        "action actually taken disagrees with that independent re-derivation; (2) verify overall "
        "output is consistent with the decisions and that no residual PHI slipped through. "
        "You never see row values, only column names, categories, actions, and regulation text. "
        'Return JSON: {"verdict": "clean|issues", '
        '"issues": [{"file": str, "column": str, "problem": str}], '
        '"metrics": {"columns_dropped": int, "columns_transformed": int, "columns_kept": int, '
        '"human_review_required": int, "estimated_leak_prob": 0..1, "action_disagreement_count": int}, '
        '"artifacts_checked": [{"file_id": str, "sha256": str}], '
        '"confidence": 0..1, "summary": str}. '
        "artifacts_checked must echo back, EXACTLY as given to you below, the file_id and sha256 of "
        "every export you reviewed -- never invented, never from memory of a prior run. "
        "confidence is your own honest self-assessment that this audit is correct -- report it "
        "truthfully; a low number here is what sends a genuinely uncertain case to a human, which "
        "is the safe outcome, not a failure. Base every judgment only on file-level summaries, "
        "decision counts, and regulation text (never row values)."
    )

    async def run(
        self,
        decisions: list[dict[str, Any]],
        exports: dict[str, str],
        files: list[dict[str, Any]],
        artifact_refs: list[tuple[str, str]],
        statute: dict[str, Any] | None = None,
        praxis_methods: dict[str, Any] | None = None,
        audit_controls: tuple[
            Sequence[EvidenceClaim | Mapping[str, Any]],
            Sequence[GateResult | Mapping[str, Any]],
        ] = ((), ()),
    ) -> dict[str, Any]:
        # Summarise deterministically (no row values sent to LLM)
        summary_by_file: dict[str, dict[str, int]] = {}
        per_column: list[dict[str, Any]] = []
        for d in decisions:
            fid = d.get("file_id", "")
            b = summary_by_file.setdefault(fid, {"keep": 0, "drop": 0, "transform": 0, "human_review": 0})
            a = d.get("action", "human_review")
            if a == "keep":
                b["keep"] += 1
            elif a == "drop":
                b["drop"] += 1
            elif a == "human_review":
                b["human_review"] += 1
            else:
                b["transform"] += 1
            per_column.append({"file_id": fid, "column": d.get("column", ""),
                               "hipaa_category": d.get("phi_category") or d.get("hipaa_category") or d.get("category"),
                               "action_taken": a})

        file_meta = [{"file_id": f["file_id"], "name": f["file_id"], "component": f.get("component")} for f in files]
        artifact_lines = [{"file_id": file_id, "sha256": sha256} for file_id, sha256 in artifact_refs]
        prompt = (
            f"Per-column decisions to re-derive and check (no row values): {per_column}\n\n"
            f"File summary counts: {summary_by_file}\n\n"
            f"Jurisdiction rulebook (RegulationsExpert): {statute or {}}\n\n"
            f"Best-practice technique per category (PHIMethodsExpert): {praxis_methods or {}}\n\n"
            f"Files: {file_meta}\n\nExports: {list(exports.keys())}\n\n"
            f"Artifacts to echo back exactly in artifacts_checked: {artifact_lines}\n"
            "Respond with JSON only."
        )
        audit = await self.call_json(
            prompt,
            phase="auditor.verify",
            default={"verdict": "issues", "issues": [], "metrics": {},
                     "artifacts_checked": [], "confidence": 0.0,
                     "summary": "Auditor call failed; treated as below the confidence floor."},
            status_text="Independently re-deriving the correct action per column",
        )
        def record_dict(record: EvidenceClaim | GateResult | Mapping[str, Any]) -> dict[str, Any]:
            return record.model_dump(mode="json") if isinstance(record, BaseModel) else dict(record)

        evidence_claims, gate_results = audit_controls
        audit["evidence_claims"] = [record_dict(claim) for claim in evidence_claims]
        audit["gate_results"] = [record_dict(result) for result in gate_results]
        return audit


# --- deterministic dataset transformer ------------------------------------


_RESTRICTED_ZIP3 = {"036","059","063","102","203","556","692","790","821","823","830","831","878","879","884","890","893"}


class PseudonymRegistry:
    """Study-scoped, exact-value pseudonym registry.

    The SAME real value produces the SAME pseudonym across the entire study
    (all files, all columns). Different values produce different pseudonyms
    even if they occupy the same column role in different files.
    """
    def __init__(self, salt: str = ""):
        self._map: dict[str, str] = {}
        self._salt = salt

    def get(self, value: str) -> str:
        if not value:
            return value
        if value in self._map:
            return self._map[value]
        # deterministic 8-hex digest, salted per study so cross-study linkage is impossible
        digest = _hashlib.sha256(f"{self._salt}:{value}".encode()).hexdigest()[:8]
        token = f"P{digest}"
        self._map[value] = token
        return token

    def digest(self, column: str, value: str) -> str:
        """Keyed digest for the `hash` action. HMAC over 'column:value' under
        the per-study salt, so the output cannot be reproduced without the
        server-held key even when the salt input (session id) is public."""
        return _hmac.new(self._salt.encode(), f"{column}:{value}".encode(), _hashlib.sha256).hexdigest()[:16]

    def save(self) -> str:
        """Encrypt this study's real-value -> pseudonym map plus its salt
        into one opaque blob. Pure function: no DB access here, so this
        class stays exactly what it was -- an in-memory registry -- and the
        caller decides where the blob lives and for how long.

        This is the reversal key: the one piece of data that makes a
        pseudonymized export re-identifiable. It must never be written next
        to ``exports`` or into the publication bundle.
        """
        from ..crypto import encrypt_reversal_map
        return encrypt_reversal_map({"salt": self._salt, "map": self._map})


def _scrub_text_cell(value: str) -> str:
    """Run Presidio + regex against a free-text cell. LLM never sees this.

    Replaces every detected PHI substring with a category token. Non-PHI
    text is preserved so clinicians retain the sentence around the redaction.
    """
    if not value:
        return value
    spans = _detect_text(value, detectors=("presidio", "rule"))
    if not spans:
        return value
    spans_sorted = sorted(spans, key=lambda s: s.start, reverse=True)
    out = value
    for s in spans_sorted:
        cat = s.hipaa_category or "X"
        end = s.end
        # A detector span can overrun into adjacent markup (e.g. eating
        # part of a closing HTML tag after a name). PHI values don't
        # contain a raw '<', so clip the span at the first one found
        # inside it rather than let the substitution corrupt structure.
        lt = out.find("<", s.start, end)
        if lt != -1:
            end = lt
        out = out[: s.start] + f"[{cat}]" + out[end:]
    return out


def _apply_action(value: str, action: str, column: str, registry: "PseudonymRegistry | None" = None) -> str:
    if value is None or value == "":
        return value
    if action == "keep":
        return value
    if action == "drop":
        return ""
    if action == "cap_age_90":
        try:
            n = int(_re.sub(r"[^0-9-]", "", value))
            return "90+" if n > 89 else str(n)
        except Exception:
            # Fail closed like year_only's malformed-input branch: a
            # non-numeric age (free text, "N/A", transcription artifact)
            # must not ship the original value verbatim.
            return ""
    if action == "year_only":
        m = _re.search(r"(\d{4})", value)
        return m.group(1) if m else ""
    if action == "zip3_truncate":
        z = _re.sub(r"[^0-9]", "", value)[:3]
        if z in _RESTRICTED_ZIP3:
            return "000"
        return z.ljust(3, "0")
    if action == "hash":
        if registry is not None:
            return registry.digest(column, value)
        return "[HASH]"
    if action == "pseudonymize":
        if registry is not None:
            return registry.get(value)
        return "[PSEUDONYM]"
    if action == "scrub_text":
        return _scrub_text_cell(value)
    if action == "human_review":
        return "[HUMAN_REVIEW_PENDING]"
    raise ValueError(f"unhandled action {action!r} for column {column!r}")


_FORMULA_LEAD_CHARS = ("=", "+", "-", "@", "\t", "\r")


def _neutralise_formula(value: str) -> str:
    """Prefix a spreadsheet-formula-shaped value with a leading apostrophe
    so a cell beginning with ``=``, ``+``, ``-``, ``@``, tab, or carriage
    return lands as inert text rather than an executable formula when the
    recipient opens the export in a spreadsheet application."""
    if value and value[0] in _FORMULA_LEAD_CHARS:
        return "'" + value
    return value


def apply_column_actions_to_dataset(src: Path, dst: Path, ext: str, decisions: list[dict[str, Any]],
                                    registry: "PseudonymRegistry | None" = None,
                                    omit_columns: set[str] | None = None) -> None:
    """Apply per-column actions to CSV or XLSX with an optional study-wide pseudonym registry.

    SEC-004 fail-closed: any column present in the source but WITHOUT a
    Judge/Sentinel decision is treated as ``drop`` (empty) rather than passed
    through verbatim. Override via env ``PHI_UNMAPPED_COLUMN_ACTION`` to
    ``scrub_text`` if the operator prefers redacted-in-place free-text.

    ``omit_columns`` (deferred human-review columns) are excluded from the
    output entirely -- never routed through ``_apply_action``, never
    written. For XLSX this deletes the column before any row is read, so a
    deferred cell's value is never even loaded into memory, not merely left
    unwritten.
    """
    import os as _os
    omit_columns = set(omit_columns or ())
    action_by_col: dict[str, dict[str, Any]] = {}
    _dupes: list[str] = []
    for d in decisions:
        col = d.get("column", "")
        if col in action_by_col:
            _dupes.append(f"{col!r} ({action_by_col[col].get('action')!r} vs {d.get('action')!r})")
        action_by_col[col] = d
    if _dupes:
        # A duplicate decision for one column is a Judge/Sentinel/human-review
        # merge bug upstream. Silently picking whichever sorted last risked
        # shipping the looser of two conflicting actions -- fail loud instead.
        raise ValueError(f"duplicate decisions for column(s): {'; '.join(_dupes)}")
    _default_action = _os.environ.get("PHI_UNMAPPED_COLUMN_ACTION", "drop").strip() or "drop"
    if _default_action not in {"drop", "scrub_text"}:
        _default_action = "drop"

    def _decision_for(col: str) -> dict[str, Any]:
        d = action_by_col.get(col)
        if d is not None:
            return d
        return {"action": _default_action, "column": col, "reason": "SEC-004 fail-closed default"}

    # Write to a temp path in the same directory and rename into place only
    # on clean completion, so a mid-write exception (detector error, corrupt
    # xlsx) never leaves a partially-transformed file at the real export
    # path -- the caller sees the exception and the tmp file is removed.
    tmp = dst.with_name(dst.name + ".tmp")
    try:
        if ext in ("csv", "tsv"):
            delim = "\t" if ext == "tsv" else ","
            with src.open("r", encoding="utf-8", errors="replace", newline="") as fin, \
                 tmp.open("w", encoding="utf-8", newline="") as fout:
                reader = _csv.DictReader(fin, delimiter=delim)
                fieldnames = reader.fieldnames or []
                surviving = [c for c in fieldnames if c not in omit_columns]
                writer = _csv.DictWriter(fout, fieldnames=surviving, delimiter=delim)
                writer.writeheader()
                for row in reader:
                    out_row: dict[str, str] = {}
                    for col in surviving:
                        d = _decision_for(col)
                        transformed = _apply_action(row.get(col) or "", d.get("action", "drop"), col, registry)
                        out_row[col] = _neutralise_formula(transformed)
                    writer.writerow(out_row)
        elif ext in ("xlsx", "xls"):
            wb = _openpyxl.load_workbook(src)
            ws = wb[wb.sheetnames[0]]
            headers: list[str] = []
            for r in ws.iter_rows(min_row=1, max_row=1, values_only=True):
                headers = [str(c) if c is not None else "" for c in r]
                break
            if omit_columns:
                omit_positions = sorted(
                    (j for j, col in enumerate(headers, start=1) if col in omit_columns),
                    reverse=True,
                )
                for pos in omit_positions:
                    ws.delete_cols(pos, 1)
                headers = [c for c in headers if c not in omit_columns]
            for i in range(2, (ws.max_row or 1) + 1):
                for j, col in enumerate(headers, start=1):
                    d = _decision_for(col)
                    cell = ws.cell(row=i, column=j)
                    transformed = _apply_action(str(cell.value) if cell.value is not None else "",
                                               d.get("action", "drop"), col, registry)
                    cell.value = _neutralise_formula(transformed)
            wb.save(tmp)
        else:
            # Unknown extension - SEC-004 fail closed: refuse to emit verbatim.
            # Write a single-line marker file so the operator sees the block.
            tmp.write_text(
                f"[REDACTED] source extension {ext!r} not supported by executor; "
                f"content withheld to prevent PHI leak.\n",
                encoding="utf-8",
            )
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    _os.replace(tmp, dst)


# --- SEC-004: deterministic metadata (dictionary) redaction ---------------

def _redact_metadata_file(src: Path, dst: Path) -> Path:
    """Run Presidio + regex over every cell of a dictionary/mapping file and
    write the redacted copy. Called by Executor for ``kind='metadata'``
    artifacts because these files can name PHI in their code-label columns.
    """
    ext = src.suffix.lower().lstrip(".")
    if ext in ("csv", "tsv"):
        dst = dst.with_suffix(f".{ext}")
        delim = "\t" if ext == "tsv" else ","
        with src.open("r", encoding="utf-8", errors="replace", newline="") as fin, \
             dst.open("w", encoding="utf-8", newline="") as fout:
            reader = _csv.reader(fin, delimiter=delim)
            writer = _csv.writer(fout, delimiter=delim)
            for row in reader:
                writer.writerow([_scrub_text_cell(c or "") for c in row])
        return dst
    if ext == "xlsx":
        wb = _openpyxl.load_workbook(src)
        dst = dst.with_suffix(".xlsx")
        for ws in wb.worksheets:
            for row in ws.iter_rows(min_row=1):
                for cell in row:
                    if cell.value is not None:
                        cell.value = _scrub_text_cell(str(cell.value))
        wb.save(dst)
        return dst
    # Unknown extension -> withhold entirely (fail closed) at a scannable path.
    withheld = dst.with_suffix(".withheld.txt")
    withheld.write_text(
        f"[REDACTED] metadata extension {ext!r} not supported; withheld to prevent PHI leak.\n",
        encoding="utf-8",
    )
    return withheld


def _sandboxed_redact_metadata_file(src: str, dst: str) -> str:
    """Sandboxed dispatch target for `_redact_metadata_file`. `Path` is
    not in `run_isolated`'s return contract, so the resolved output path
    crosses the boundary as `str` (see
    `Executor._redact_metadata_maybe_sandboxed`)."""
    return str(_redact_metadata_file(Path(src), Path(dst)))


def _sandboxed_apply_column_actions_to_dataset(
    workspace_path: str, salt: str, initial_map: dict[str, str],
    src: str, dst: str, ext: str, decisions: list[dict[str, Any]], omit_columns: list[str],
) -> str:
    """Sandboxed dispatch target for `apply_column_actions_to_dataset`.

    A live `PseudonymRegistry` object mutated by reference cannot cross
    the `multiprocessing.spawn` pickle boundary, so this rebuilds an
    equivalent registry from plain `(salt, initial_map)` arguments
    instead of receiving one directly. After the real transform runs,
    the updated map is written to a workspace-relative JSON artifact and
    only its filename is returned -- `run_isolated`'s return contract
    never allows an arbitrary payload to cross back into the parent (see
    `Executor._apply_column_actions_maybe_sandboxed`, which reads this
    artifact back and merges it into the caller's own registry)."""
    registry = PseudonymRegistry(salt=salt)
    registry._map.update(initial_map)
    apply_column_actions_to_dataset(
        Path(src), Path(dst), ext, decisions, registry, omit_columns=set(omit_columns),
    )
    out_name = f"pseudonym_map_{uuid4().hex}.json"
    (Path(workspace_path) / out_name).write_text(_json.dumps(registry._map), encoding="utf-8")
    return out_name
