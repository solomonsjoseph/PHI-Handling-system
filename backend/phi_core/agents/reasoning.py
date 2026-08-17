"""Reasoning agents: Judge, Sentinel, Executor, Auditor.

Judge     - synthesises specialist + statute + praxis outputs into per-column decisions.
Sentinel  - preview reviewer, enforces 0% leak and 100% accuracy.
Executor  - applies the transformations decided by Judge.
Auditor   - reviews Executor's work and produces the final compliance report.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..anonymizer import apply_to_text
from ..detectors import detect_text
from ..file_readers import iter_dataset_rows, read_narrative
from ..jurisdictions import get_pack
from ..paths import EXPORT_DIR
from ..publish_guard import should_fire
from .base import Agent


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
        else:
            d["suggested_action"] = None
            d["suggested_confidence"] = None
            d["suggested_reason"] = None
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


def _reviewer_prompt_for(d: dict[str, Any], dictionary_by_column: dict[str, str] | None = None) -> str:
    """Plain-language sentence built in Python from column name, dictionary
    description, and Judge's suggestion -- no LLM call."""
    col = d.get("column") or "this column"
    desc = (dictionary_by_column or {}).get(col, "")
    desc_clause = f' ("{desc}")' if desc else ""
    suggested = d.get("suggested_action")
    reason = d.get("suggested_reason") or d.get("reason") or \
        "the automated classifiers were not confident enough to decide on their own"
    if suggested:
        return (f"I'm not confident enough to decide '{col}'{desc_clause} on my own. "
                f"My best guess is {suggested}: {reason} Does that look right?")
    return (f"I'm not confident enough to decide '{col}'{desc_clause} on my own: {reason} "
            "What should happen to this column?")


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


# --- Sentinel hard-rule table ---------------------------------------------
#
# Deterministic column-header -> allow-list mapping for known direct
# identifiers. When Judge picks 'human_review' or an action outside the
# allow-list for an obvious identifier, this rule forces the safest listed
# action. This closes the accuracy gap where the LLM routes obvious PHI to
# human review out of caution. Citations map to 45 CFR 164.514(b)(2)(i).
_HARD_RULE_TABLE: list[tuple[str, list[str], str, str]] = [
    # (regex, allow-list actions, default action, HIPAA citation)
    # (A) Names
    (r"^(patient[_ ]?name|subject[_ ]?name|first[_ ]?name|last[_ ]?name|full[_ ]?name|name|given[_ ]?name|family[_ ]?name|surname|middle[_ ]?name|maiden[_ ]?name|provider[_ ]?name|physician[_ ]?name|attending[_ ]?name|clinician[_ ]?name)$",
     ["drop", "pseudonymize"], "drop", "164.514(b)(2)(i)(A)"),
    # (B) Geography - address / street / city / county / precinct
    (r"^(address|street|street[_ ]?address|mailing[_ ]?address|home[_ ]?address|city|county|precinct)$",
     ["drop"], "drop", "164.514(b)(2)(i)(B)"),
    # (B) Geography - ZIP: keep 3-digit truncation
    (r"^(zip|zipcode|zip[_ ]?code|postal[_ ]?code|postcode)$",
     ["zip3_truncate", "drop"], "zip3_truncate", "164.514(b)(2)(i)(B)"),
    # (C) Dates - directly related to individual
    (r"^(dob|date[_ ]?of[_ ]?birth|birth[_ ]?date|birthdate|admission[_ ]?date|admit[_ ]?date|discharge[_ ]?date|death[_ ]?date|visit[_ ]?date|encounter[_ ]?date|service[_ ]?date|onset[_ ]?date|diagnosis[_ ]?date)$",
     ["year_only", "drop"], "year_only", "164.514(b)(2)(i)(C)"),
    # (C) Ages - cap at 90+
    (r"^(age|age[_ ]?years|age[_ ]?in[_ ]?years|age[_ ]?at[_ ]?enrolment|age[_ ]?at[_ ]?enrollment|age[_ ]?at[_ ]?screening)$",
     ["cap_age_90", "keep", "drop"], "cap_age_90", "164.514(b)(2)(i)(C)"),
    # (D) Telephone
    (r"^(phone|phone[_ ]?number|mobile|cell|telephone|tel|home[_ ]?phone|work[_ ]?phone|contact[_ ]?phone|cell[_ ]?phone)$",
     ["drop"], "drop", "164.514(b)(2)(i)(D)"),
    # (E) Fax
    (r"^(fax|fax[_ ]?number|office[_ ]?fax)$",
     ["drop"], "drop", "164.514(b)(2)(i)(E)"),
    # (F) Email
    (r"^(email|e[_ ]?mail|email[_ ]?address|contact[_ ]?email|study[_ ]?email)$",
     ["drop"], "drop", "164.514(b)(2)(i)(F)"),
    # (G) SSN
    (r"^(ssn|social[_ ]?security(?:[_ ]?number)?|ss[_ ]?number|ss[_ ]?no)$",
     ["drop"], "drop", "164.514(b)(2)(i)(G)"),
    # (H) Medical record number / study-scoped participant record identifier
    (r"^(mrn|medical[_ ]?record(?:[_ ]?number)?|record[_ ]?number|chart[_ ]?number|chart[_ ]?id|patient[_ ]?record[_ ]?id|patient[_ ]?id|subject[_ ]?id|participant[_ ]?id|child[_ ]?id|study[_ ]?id|enrol(?:l)?ment[_ ]?id|record[_ ]?id)$",
     ["pseudonymize", "hash", "drop"], "pseudonymize", "164.514(b)(2)(i)(H)"),
    # (I) Health-plan beneficiary
    (r"^(insurance[_ ]?id|member[_ ]?id|health[_ ]?plan[_ ]?number|subscriber[_ ]?id|hpid|mbi|medicare[_ ]?beneficiary[_ ]?id|policy[_ ]?number)$",
     ["drop"], "drop", "164.514(b)(2)(i)(I)"),
    # (J) Account number
    (r"^(account[_ ]?number|billing[_ ]?account|invoice[_ ]?number|patient[_ ]?account)$",
     ["drop"], "drop", "164.514(b)(2)(i)(J)"),
    # (K) Certificate / licence
    (r"^(license[_ ]?number|licence[_ ]?number|driver[_ ]?license|driver[_ ]?licence|certificate[_ ]?id|certification[_ ]?id|dea[_ ]?number|npi|npi[_ ]?number|provider[_ ]?npi)$",
     ["drop"], "drop", "164.514(b)(2)(i)(K)"),
    # (L) Vehicle identifiers
    (r"^(vehicle[_ ]?id|vehicle[_ ]?number|license[_ ]?plate|plate[_ ]?number|vin)$",
     ["drop"], "drop", "164.514(b)(2)(i)(L)"),
    # (M) Device identifiers / serial numbers
    (r"^(device[_ ]?id|device[_ ]?serial|serial[_ ]?number|imei|device[_ ]?uuid|hardware[_ ]?id)$",
     ["drop"], "drop", "164.514(b)(2)(i)(M)"),
    # (N) URL
    (r"^(url|web[_ ]?url|website|homepage|personal[_ ]?url)$",
     ["drop"], "drop", "164.514(b)(2)(i)(N)"),
    # (O) IP
    (r"^(ip|ip[_ ]?address|ipv4|ipv6|client[_ ]?ip)$",
     ["drop"], "drop", "164.514(b)(2)(i)(O)"),
    # (P) Biometric identifiers
    (r"^(fingerprint|iris[_ ]?scan|biometric[_ ]?hash|voice[_ ]?print|dna[_ ]?profile|retinal[_ ]?scan|palm[_ ]?print)$",
     ["drop"], "drop", "164.514(b)(2)(i)(P)"),
    # (Q) Photographs / comparable images
    (r"^(face[_ ]?photo|patient[_ ]?photo|portrait|portrait[_ ]?url|headshot|patient[_ ]?image|face[_ ]?image)$",
     ["drop"], "drop", "164.514(b)(2)(i)(Q)"),
    # (R) Any other unique identifying number / characteristic / code
    (r"^(patient[_ ]?uuid|subject[_ ]?uuid|unique[_ ]?id|tracking[_ ]?code|study[_ ]?code|linkage[_ ]?id)$",
     ["pseudonymize", "hash", "drop"], "pseudonymize", "164.514(b)(2)(i)(R)"),
    # Free-text (non-listed but must be scrubbed cell-by-cell)
    (r"^(notes|visit[_ ]?notes|clinician[_ ]?notes|provider[_ ]?notes|comments|remarks|observations|free[_ ]?text|note|comment)$",
     ["scrub_text", "drop"], "scrub_text", "164.514(b)(1) — free-text scrub"),
    # Explicit non-PHI keepers — clinical measurements and stratifiers
    (r"^(hemoglobin|bmi|systolic[_ ]?bp|diastolic[_ ]?bp|heart[_ ]?rate|heart[_ ]?rate[_ ]?bpm|temperature|glucose|glucose[_ ]?mgdl|wbc[_ ]?count|hgb[_ ]?a1c|hba1c[_ ]?percent|ldl|hdl|creatinine|spo2|dose|dose[_ ]?mg|sex|gender|race|ethnicity|study[_ ]?arm|treatment[_ ]?group|arm[_ ]?code|visit[_ ]?number|state|country|barcode|specimen[_ ]?barcode|specimen[_ ]?id|cbcl[_ ]?total[_ ]?score|diagnosis[_ ]?code|site[_ ]?of[_ ]?disease|chest[_ ]?xray[_ ]?finding|treatment[_ ]?regimen|drug[_ ]?susceptibility[_ ]?result|sputum[_ ]?smear[_ ]?result|sputum[_ ]?culture[_ ]?result|hiv[_ ]?status|comorbidity[_ ]?other|case[_ ]?definition|treatment[_ ]?outcome)$",
     ["keep"], "keep", "clinical / stratifier"),
]

# NOTE: an earlier version of this fix skipped `verify_keep_decisions` row
# scanning for columns already deemed safe by header name (state, country,
# diagnosis_code, ...). That is a real security hole: `test_corpus_replay.py`
# ::test_keeper_names_hijack_is_leak_clean_in_every_unmatched_mode plants
# real names/addresses under exactly those keeper headers to prove the
# deterministic row scan cannot be bypassed by column naming. The scan stays
# on for every 'keep' decision, no column-name exemptions. The false
# escalations this causes on e.g. a legitimate 'state' column are the
# intended fail-closed behaviour, not a bug -- the fix that actually helps
# is making the escalation cheap to resolve (suggested_action populated
# below), not disabling the check.


import re as _re_module

_CATEGORY_LETTER_RE = _re_module.compile(r"\(([A-R])\)$")


def _category_letter(citation: str) -> str | None:
    """Extract the HIPAA Safe Harbor identifier letter from a hard-rule
    citation like '164.514(b)(2)(i)(A)'. Returns None for citations with no
    trailing subcategory letter (e.g. the free-text/clinical-keeper rows)."""
    m = _CATEGORY_LETTER_RE.search(citation)
    return m.group(1) if m else None


def apply_sentinel_hard_rules(decisions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Force obvious direct-identifier columns off 'human_review' into a safe action.

    Returns (new_decisions, overrides) where overrides describes each change
    applied. A decision's action is overridden ONLY when its column name
    matches a hard-rule pattern AND its current action is either
    'human_review' or not in the rule's allow-list; otherwise Judge's action
    choice is respected. Independently of the action, a matched column's
    `phi_category` is always corrected to the rule's letter when the model
    proposed a different or missing one -- Judge has been observed to pick
    the right action with the wrong category label (e.g. 'ssn' -> action
    drop, category A instead of G), which corrupts the Auditor's
    per-category precision/recall.
    """
    out: list[dict[str, Any]] = []
    overrides: list[dict[str, Any]] = []
    for d in decisions:
        col = (d.get("column") or "").strip().lower()
        norm = _re_module.sub(r"\s+", "_", col)
        action = d.get("action", "human_review")
        matched = False
        for pattern, allow, default_action, citation in _HARD_RULE_TABLE:
            if _re_module.match(pattern, norm):
                matched = True
                letter = _category_letter(citation)
                category_wrong = letter is not None and d.get("phi_category") != letter
                if action not in allow:
                    new_d = dict(d)
                    new_d["action"] = default_action
                    new_d["reason"] = (
                        f"Sentinel hard-rule: column '{d.get('column')}' is a known direct identifier "
                        f"per 45 CFR {citation}. Forced from '{action}' to '{default_action}'."
                    )
                    new_d["citation"] = f"45 CFR {citation}"
                    new_d["confidence"] = max(float(d.get("confidence") or 0), 0.95)
                    if letter is not None:
                        new_d["phi_category"] = letter
                    overrides.append({
                        "column": d.get("column"),
                        "file_id": d.get("file_id"),
                        "from": action,
                        "to": default_action,
                        "citation": citation,
                    })
                    out.append(new_d)
                elif category_wrong:
                    new_d = dict(d)
                    new_d["phi_category"] = letter
                    overrides.append({
                        "column": d.get("column"),
                        "file_id": d.get("file_id"),
                        "from": action,
                        "to": action,
                        "citation": citation,
                        "category_corrected": letter,
                    })
                    out.append(new_d)
                else:
                    out.append(d)
                break
        if not matched:
            out.append(d)
    return out, overrides


# --- Cross-column deterministic rule: age present -> drop DOB -------------
#
# Sir's stated rule: "age present then drop DOB else keep DOB [transformed]".
# Judge reasons about each column in isolation and has no way to see that a
# separate age column already covers the research need, so a deterministic
# post-Judge pass enforces this rather than relying on the LLM to infer a
# cross-column relationship from headers alone.
_AGE_COL_RE = _re_module.compile(
    r"^(age|age[_ ]?years|age[_ ]?in[_ ]?years|age[_ ]?at[_ ]?enrolment|"
    r"age[_ ]?at[_ ]?enrollment|age[_ ]?at[_ ]?screening|age[_ ]?of[_ ]?onset)$"
)
_DOB_COL_RE = _re_module.compile(
    r"^(dob|date[_ ]?of[_ ]?birth|birth[_ ]?date|birthdate)$"
)


def apply_age_dob_rule(decisions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """If a separate age column is being retained, force any DOB column to
    'drop' rather than a transform like year_only -- the age column already
    covers the research need, and 164.514(b)(2)(i)(C) requires dropping the
    identifier once it is not needed in identifiable or near-identifiable
    form. Leaves DOB decisions untouched when no age column is present."""
    has_age = any(
        _AGE_COL_RE.match(_re_module.sub(r"\s+", "_", (d.get("column") or "").strip().lower()))
        and d.get("action") not in ("drop", "human_review")
        for d in decisions
    )
    if not has_age:
        return decisions, []
    out: list[dict[str, Any]] = []
    overrides: list[dict[str, Any]] = []
    for d in decisions:
        col_norm = _re_module.sub(r"\s+", "_", (d.get("column") or "").strip().lower())
        if _DOB_COL_RE.match(col_norm) and d.get("action") != "drop":
            new_d = dict(d)
            new_d.update(
                action="drop",
                phi_category="C",
                reason=(
                    "Cross-column rule: a separate age column is retained for research use, "
                    "so date of birth must be dropped rather than transformed, per "
                    "45 CFR 164.514(b)(2)(i)(C)."
                ),
                citation="45 CFR 164.514(b)(2)(i)(C)",
                confidence=0.98,
                suggested_action=None,
                suggested_confidence=None,
                suggested_reason=None,
            )
            overrides.append({
                "file_id": d.get("file_id"),
                "column": d.get("column"),
                "from": d.get("action"),
                "to": "drop",
                "rule": "age_present_drop_dob",
            })
            out.append(new_d)
        else:
            out.append(d)
    return out, overrides

def verify_keep_decisions(
    decisions: list[dict[str, Any]],
    dataset_paths: dict[str, Path],
    jurisdiction: str = "us",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Demote keeps whose dataset values match deterministic PHI detectors."""
    verified = list(decisions)
    demotions: list[dict[str, Any]] = []
    patterns = get_pack(jurisdiction).patterns
    keep_indices_by_file: dict[str, list[int]] = {}
    for index, decision in enumerate(decisions):
        file_id = decision.get("file_id")
        if decision.get("action") == "keep" and file_id in dataset_paths:
            keep_indices_by_file.setdefault(file_id, []).append(index)

    def demote(decision: dict[str, Any], detector_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        column = decision.get("column")
        updated = dict(decision)
        updated.update(
            action="human_review",
            reason=(
                f"Keep verification: column '{column}' matched {detector_id} in a row value; "
                "demoted pending human review."
            ),
            citation="45 CFR 164.514(b)(2)(i)",
            # Carry the pre-demotion decision forward as the agent's own
            # recommendation, so the reviewer sees what Judge proposed and
            # why the deterministic scan didn't trust it, in one glance.
            suggested_action="keep",
            suggested_confidence=decision.get("confidence"),
            suggested_reason=(
                f"Judge originally proposed 'keep' ({decision.get('reason') or 'no reason given'}); "
                f"a deterministic row-value scan matched detector '{detector_id}', which the "
                "reviewer should confirm or override."
            ),
        )
        return updated, {
            "file_id": decision.get("file_id"),
            "column": column,
            "from": "keep",
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


class Judge(Agent):
    NAME = "Judge"
    PROMPT = (
        "You are Judge. Given (a) Schema column classifications, (b) Instrument PHI fields "
        "from forms, (c) Lexicon dictionary entries, and (d) Statute jurisdictional rules, "
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
        "the Executor will run Presidio + regex over each cell (LLM never reads the cells)\n"
        "  human_review   - uncertain; route to a human\n\n"
        "SUBJECT tag (choose exactly one): participant | staff | specimen | site | study\n\n"
        "Use scrub_text for columns like 'comments', 'notes', 'remarks', 'other_specify', "
        "'reason', 'description' (any narrative free-text field on a study form).\n"
        "Route to human_review whenever confidence < 0.60 or the column looks like a "
        "true quasi-identifier requiring auditor judgement.\n\n"
        "Return JSON: "
        '{"decisions": [{"file_id": str, "column": str, "phi_category": str|null, '
        '"subject": "participant|staff|specimen|site|study", '
        '"action": "keep|drop|cap_age_90|year_only|zip3_truncate|hash|pseudonymize|scrub_text|human_review", '
        '"reason": str, "confidence": 0..1, "citation": str, '
        '"suggested_action": str|null, "suggested_confidence": 0..1|null, "suggested_reason": str|null}]}. '
        "When action='human_review', also fill suggested_action/suggested_confidence/suggested_reason "
        "with your best guess if you HAD to decide directly -- a human reviewer uses this as a "
        "one-click starting point, never as the final decision. Leave all three null when action "
        "is anything other than human_review.\n"
        "Preserve clinically-needed non-PHI (diagnoses, procedures, vitals, labs)."
    )

    async def run(self, schema: dict, instrument: dict, lexicon: dict, statute: dict,
                  praxis: dict[str, dict] | None = None, prior_feedback: str = "") -> dict[str, Any]:
        prompt = (
            f"Statute rules (jurisdictional regulations): {statute}\n\n"
            f"Schema columns (dataset headers -- rows never shown): {schema}\n\n"
            f"Instrument fields (from study forms): {instrument}\n\n"
            f"Lexicon columns (dictionary): {lexicon}\n"
        )
        if praxis:
            # Praxis-recommended technique per HIPAA identifier category,
            # web-sourced where possible. Judge uses these to pick the
            # correct `action` value below rather than reasoning from
            # scratch.
            praxis_summary = {c: {"technique": m.get("technique"),
                                  "utility_preserving": m.get("utility_preserving")}
                              for c, m in praxis.items() if m}
            prompt += (f"\nPraxis methods (current best-practice technique per HIPAA "
                       f"identifier category): {praxis_summary}\n")
        if prior_feedback:
            prompt += (
                "\nPreview (Sentinel) blocking feedback to address. Before correcting, "
                "verify each issue is a real leak / real over-block against Statute + "
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
        return await self.call_json(
            prompt, phase="judge.decide", default={"decisions": []},
            max_tokens=judge_max_tokens,
            expect_key="decisions", min_items=len(schema.get("columns") or []),
            status_text="Deciding how to handle every flagged column")

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


class Sentinel(Agent):
    NAME = "Sentinel"
    PROMPT = (
        "You are Sentinel. Review Judge's decisions with ONE goal: zero PHI leak, 100% accuracy. "
        "Cross-check every 'keep' against Statute rules and Instrument fields. Flag any column "
        "whose action is inconsistent with its PHI category or citation.\n\n"
        "For every issue, set severity honestly:\n"
        "  - 'blocking' ONLY when the current action would leak PHI or over-block clinical signal. "
        "This is the bar for another iteration. Reserve for real leaks (e.g. keep on a phone column, "
        "keep on a name column, drop on a study arm).\n"
        "  - 'advisory' for style, retention-policy nits, or preference between two safe transforms "
        "(e.g. hash vs pseudonymize when both close the leak). Advisory issues NEVER trigger a "
        "re-iteration; they are logged and included in the audit trail.\n\n"
        "Return JSON: "
        '{"verdict": "approved|revise", "issues": [{"file_id": str, "column": str, '
        '"problem": str, "suggested_action": str, "severity": "blocking|advisory"}], "summary": str}. '
        "Set verdict='approved' unless at least one blocking issue remains after your review. "
        "Nitpick sparingly and only where it materially reduces PHI risk."
    )

    async def run(self, decisions: list[dict[str, Any]], statute: dict, instrument: dict,
                  parent_id: str | None = None) -> dict[str, Any]:
        prompt = (
            f"Judge decisions: {decisions}\n\n"
            f"Statute rules: {statute}\n\n"
            f"Instrument fields: {instrument}\n"
            "Respond with JSON only. Remember: only 'blocking' severity triggers another iteration."
        )
        out = await self.call_json(prompt, phase="sentinel.review",
                                   default={"verdict": "approved", "issues": []},
                                   parent_id=parent_id,
                                   status_text="Cross-checking Judge's decisions against Statute and Instrument")
        # Deterministic post-processing: if there are no blocking issues,
        # force verdict='approved' regardless of what the LLM wrote. This
        # closes the "Sentinel nitpicks endlessly" pathology observed on
        # live sessions where the LLM emitted verdict='revise' with only
        # style-level objections.
        issues = out.get("issues") or []
        blocking = [i for i in issues if str(i.get("severity", "")).lower() == "blocking"]
        if not blocking:
            out["verdict"] = "approved"
        return out


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


class Executor(Agent):
    NAME = "Executor"
    PROMPT = ""  # deterministic; no LLM call needed for execution

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)

    async def run(self, files: list[dict[str, Any]], decisions: list[dict[str, Any]],
                  omit_by_file: dict[str, set[str]] | None = None) -> dict[str, Any]:
        """Apply decisions to each file. Returns {"exports": {file_id: path}}.

        ``omit_by_file`` (file_id -> deferred column names) is the partial-
        export channel: those columns are excluded from the written file
        entirely rather than routed through SEC-004's fail-closed default.
        A dataset file whose EVERY known column is deferred is skipped
        entirely -- excluded from ``exports`` -- rather than written as a
        headerless file, so Publish Guard never has to reason about it and
        the manifest can record it as fully deferred (see server.py).
        """
        pending = [(d.get("file_id", ""), d.get("column", "")) for d in decisions if d.get("action") == "human_review"]
        if pending:
            raise ValueError(f"unresolved human_review deferrals cannot be executed: {pending}")
        omit_by_file = omit_by_file or {}
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
            dst = EXPORT_DIR / f"{f['file_id']}__{f['original_name']}"
            if f["kind"] == "metadata":
                # SEC-004 fail-closed: dictionary/mapping files can name PHI
                # (column definitions, code labels) so we run the deterministic
                # detector over them BEFORE they land in exports/. Never copy
                # verbatim.
                dst = _redact_metadata_file(src, dst)
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
                    known_cols = _read_dataset_headers(src, f["subtype"])
                if omit_cols and known_cols and known_cols <= omit_cols:
                    await self._log("executor.dataset_fully_deferred", "info",
                                    {"file_id": f["file_id"], "column_count": len(known_cols)})
                    continue
                apply_column_actions_to_dataset(src, dst, f["subtype"], by_file.get(f["file_id"], []),
                                                registry, omit_columns=omit_cols)
            else:
                dst = EXPORT_DIR / f"{f['file_id']}__{Path(f['original_name']).stem}.redacted.txt"
                try:
                    text = read_narrative(src, f["subtype"])
                except Exception as e:
                    await self._log("executor.narrative_read_failed", "info",
                                    {"file_id": f["file_id"], "error": type(e).__name__})
                    dst.write_text(
                        f"[REDACTED] narrative extraction failed ({type(e).__name__}); "
                        f"content withheld to prevent PHI leak.\n", encoding="utf-8")
                    exports[f["file_id"]] = str(dst)
                    continue
                if not text.strip():
                    await self._log("executor.narrative_empty", "info",
                                    {"file_id": f["file_id"]})
                    dst.write_text(
                        f"[NO EXTRACTABLE TEXT] {f['original_name']}\n", encoding="utf-8")
                    exports[f["file_id"]] = str(dst)
                    continue
                spans = detect_text(text, detectors=["presidio", "rule"])
                for sp in spans:
                    sp.review_status = "accepted"
                dst.write_text(apply_to_text(text, spans), encoding="utf-8")
            exports[f["file_id"]] = str(dst)
            await self._log("executor.wrote", "info", {"file_id": f["file_id"], "path": str(dst)})
        # Persist the pseudonym map size so the auditor can report on linkage coverage.
        await self._log("executor.pseudonym_registry", "info", {"unique_values_pseudonymized": len(registry._map)})
        return {"exports": exports, "pseudonym_count": len(registry._map)}


class Auditor(Agent):
    NAME = "Auditor"
    PROMPT = (
        "You are Auditor. Verify Executor produced outputs consistent with Judge's decisions "
        "and no residual PHI slipped through. Return JSON: "
        '{"verdict": "clean|issues", "issues": [{"file": str, "problem": str}], '
        '"metrics": {"columns_dropped": int, "columns_transformed": int, "columns_kept": int, '
        '"human_review_required": int, "estimated_leak_prob": 0..1}, "summary": str}. '
        "Base metrics only on file-level summaries and decision counts (never row values)."
    )

    async def run(self, decisions: list[dict[str, Any]], exports: dict[str, str], files: list[dict[str, Any]]) -> dict[str, Any]:
        # Summarise deterministically (no row values sent to LLM)
        summary_by_file: dict[str, dict[str, int]] = {}
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

        file_meta = [{"file_id": f["file_id"], "name": f["original_name"], "component": f.get("component")} for f in files]
        prompt = (
            f"File summary counts (no row values): {summary_by_file}\n\n"
            f"Files: {file_meta}\n\nExports: {list(exports.keys())}\n"
            "Respond with JSON only."
        )
        return await self.call_json(prompt, phase="auditor.verify",
                                    default={"verdict": "clean", "issues": [], "metrics": {}, "summary": ""},
                                    status_text="Verifying the executor output against decisions")


# --- deterministic dataset transformer ------------------------------------

import csv as _csv
import openpyxl as _openpyxl
import re as _re
import hashlib as _hashlib
import hmac as _hmac

from ..detectors import detect_text as _detect_text
from ..crypto import pseudonym_salt


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
        out = out[: s.start] + f"[{cat}]" + out[s.end :]
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
            return value
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
    action_by_col: dict[str, dict[str, Any]] = {d.get("column", ""): d for d in decisions}
    _default_action = _os.environ.get("PHI_UNMAPPED_COLUMN_ACTION", "drop").strip() or "drop"
    if _default_action not in {"drop", "scrub_text"}:
        _default_action = "drop"

    def _decision_for(col: str) -> dict[str, Any]:
        d = action_by_col.get(col)
        if d is not None:
            return d
        return {"action": _default_action, "column": col, "reason": "SEC-004 fail-closed default"}

    if ext in ("csv", "tsv"):
        delim = "\t" if ext == "tsv" else ","
        with src.open("r", encoding="utf-8", errors="replace", newline="") as fin, \
             dst.open("w", encoding="utf-8", newline="") as fout:
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
        wb.save(dst)
    else:
        # Unknown extension - SEC-004 fail closed: refuse to emit verbatim.
        # Write a single-line marker file so the operator sees the block.
        dst.write_text(
            f"[REDACTED] source extension {ext!r} not supported by executor; "
            f"content withheld to prevent PHI leak.\n",
            encoding="utf-8",
        )


# --- SEC-004: deterministic metadata (dictionary) redaction ---------------

def _redact_metadata_file(src: Path, dst: Path) -> Path:
    """Run Presidio + regex over every cell of a dictionary/mapping file and
    write the redacted copy. Called by Executor for ``kind='metadata'``
    artifacts because these files can name PHI in their code-label columns.
    """
    ext = src.suffix.lower().lstrip(".")
    if ext in ("csv", "tsv"):
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
