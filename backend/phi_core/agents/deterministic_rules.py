"""Deterministic decision-shaping rules (docs #91, phase 8 extraction).

Migrated verbatim out of ``phi_core.agents.reasoning`` where they used to
live inline with the (now-retired) ``Sentinel`` LLM role. Every function
here is pure Python: no LLM call, no I/O, safe to call as many times as a
caller likes. ``reasoning.py`` re-imports the whole public surface so
every existing ``from phi_core.agents.reasoning import X`` call site
(``control/gates.py``, ``control/validation.py``, ``server.py``, and the
test suite) keeps working unchanged.

These are the deterministic building blocks Reviewer Preview (docs #43)
and the deterministic Evidence Gate (docs #91) are built from: the
hard-rule table forces obvious direct identifiers off an unsafe action,
the confidence/blocking floors force a shaky or contested decision to
human review before it ever reaches an LLM review call, and
``apply_sentinel_escalations`` converts a reviewer's 'escalate' finding
into human_review. The name keeps its historical 'sentinel' prefix
(matching the phase-tag telemetry strings and existing test assertions
that key off it); the *role* that used to own this logic is gone, not
the vocabulary describing what the logic does.
"""
from __future__ import annotations

import re
from typing import Any

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


_CATEGORY_LETTER_RE = re.compile(r"\(([A-R])\)$")


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
        norm = re.sub(r"\s+", "_", col)
        action = d.get("action", "human_review")
        matched = False
        for pattern, allow, default_action, citation in _HARD_RULE_TABLE:
            if re.match(pattern, norm):
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
_AGE_COL_RE = re.compile(
    r"^(age|age[_ ]?years|age[_ ]?in[_ ]?years|age[_ ]?at[_ ]?enrolment|"
    r"age[_ ]?at[_ ]?enrollment|age[_ ]?at[_ ]?screening|age[_ ]?of[_ ]?onset)$"
)
_DOB_COL_RE = re.compile(
    r"^(dob|date[_ ]?of[_ ]?birth|birth[_ ]?date|birthdate)$"
)


def apply_age_dob_rule(decisions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """If a separate age column is being retained, force any DOB column to
    'drop' rather than a transform like year_only -- the age column already
    covers the research need, and 164.514(b)(2)(i)(C) requires dropping the
    identifier once it is not needed in identifiable or near-identifiable
    form. Leaves DOB decisions untouched when no age column is present."""
    has_age = any(
        _AGE_COL_RE.match(re.sub(r"\s+", "_", (d.get("column") or "").strip().lower()))
        and d.get("action") not in ("drop", "human_review")
        for d in decisions
    )
    if not has_age:
        return decisions, []
    out: list[dict[str, Any]] = []
    overrides: list[dict[str, Any]] = []
    for d in decisions:
        col_norm = re.sub(r"\s+", "_", (d.get("column") or "").strip().lower())
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


# --- Deterministic rule: low-cardinality site/facility columns ------------
#
# Sentinel plan item 4. A confidently wrong 'keep' on a site- or
# sub-geography-shaped column passes both the confidence floor and
# Sentinel's LLM judgment, because the risk isn't in the confidence score,
# it's in the column's shape: a handful of distinct facility names spread
# across many rows is exactly the quasi-identifier pattern
# 164.514(b)(2)(i)(R) covers. Fires only for columns the hard-rule table
# doesn't already own (so a clinical keeper like 'site_of_disease' is never
# touched) and only when Schema's deterministic cardinality stats are
# actually known for that column.
#
# The site term must land on a separator-delimited token, not merely
# appear somewhere inside one: unanchored substring matching would treat
# 'award_id' (contains 'ward'), 'composite_score' (contains 'site') and
# 'subclinical_flag' (contains 'clinic') as site columns, which is a real
# destructive false positive -- those columns would be silently dropped.
# `(?:^|_)...(?:_|$)` requires an underscore or string boundary on both
# sides, so the term matches a whole token (or a run of tokens for the
# `sub_?district` compound) while still finding it anywhere in the column
# name, exactly as the plan's "as a substring, not anchored" describes for
# `treatment_facility_name`.
_SITE_COL_RE = re.compile(
    r"(?:^|_)(facility|site|clinic|hospital|centre|center|ward|catchment"
    r"|district|village|township|sub_?district|taluk|tehsil|mandal)(?:_|$)",
    re.IGNORECASE,
)


def apply_site_cardinality_rule(
    decisions: list[dict[str, Any]],
    stats: dict[tuple[str, str], dict[str, int]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Force 'keep' to 'drop'/category R on a low-cardinality site or
    facility column. Fires only when all four hold: the column name
    contains a `_SITE_COL_RE` token (a site/facility/geography word
    delimited by underscores or the string boundary, not merely a
    substring of an unrelated word), the current action is 'keep', the
    column matches no `_HARD_RULE_TABLE` pattern (so an owned clinical
    keeper such as 'site_of_disease' is left alone), and Schema's known
    distinct-value count satisfies `2 <= distinct <= max(20, 0.05 * rows)`.
    A column with unknown stats is left alone -- there is nothing to force
    a decision from. Runs deterministically before Sentinel, so a column
    it recognises never costs a review call."""
    out: list[dict[str, Any]] = []
    overrides: list[dict[str, Any]] = []
    for d in decisions:
        col = (d.get("column") or "").strip().lower()
        norm = re.sub(r"\s+", "_", col)
        eligible = (
            d.get("action") == "keep"
            and bool(_SITE_COL_RE.search(norm))
            and not any(re.match(pattern, norm) for pattern, *_ in _HARD_RULE_TABLE)
        )
        s = stats.get((d.get("file_id"), col)) if eligible else None
        distinct = s.get("distinct") if s else None
        rows = s.get("rows") if s else None
        if (
            eligible
            and isinstance(distinct, int)
            and isinstance(rows, int)
            and rows > 0
            and 2 <= distinct <= max(20, 0.05 * rows)
        ):
            new_d = dict(d)
            new_d.update(
                action="drop",
                phi_category="R",
                citation="45 CFR 164.514(b)(2)(i)(R)",
                confidence=0.95,
                reason=(
                    f"Site-cardinality rule: column '{d.get('column')}' names a facility or "
                    f"catchment-shaped field with only {distinct} distinct values across "
                    f"{rows} rows, a quasi-identifying pattern under 45 CFR "
                    "164.514(b)(2)(i)(R). Forced from 'keep' to 'drop'."
                ),
                suggested_action=d.get("action"),
            )
            overrides.append({
                "file_id": d.get("file_id"), "column": d.get("column"),
                "from": d.get("action"), "to": "drop",
                "rule": "site_cardinality", "distinct": distinct, "rows": rows,
            })
            out.append(new_d)
        else:
            out.append(d)
    return out, overrides


CONFIDENCE_FLOOR = 0.80


def apply_confidence_floor(decisions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Any decision below CONFIDENCE_FLOOR always goes to human review,
    independent of whether Sentinel agrees with it. Reducing human review is
    not the same as denying it -- a deterministic gate, not an LLM judgment
    call, and it runs before Sentinel's LLM review so a decision that is
    going to human review regardless doesn't cost a review call. Fixed at
    0.80 regardless of iteration_cap/rigor selector."""
    out: list[dict[str, Any]] = []
    overrides: list[dict[str, Any]] = []
    for d in decisions:
        confidence = d.get("confidence")
        if (
            d.get("action") != "human_review"
            and isinstance(confidence, (int, float))
            and confidence < CONFIDENCE_FLOOR
        ):
            new_d = dict(d)
            new_d.update(
                action="human_review",
                reason=(
                    f"Confidence floor: Judge's confidence ({confidence:.2f}) is below the "
                    f"{CONFIDENCE_FLOOR:.2f} floor required to ship a decision unreviewed."
                ),
                suggested_action=d.get("action"),
                suggested_confidence=confidence,
                suggested_reason=(
                    f"Judge proposed {d.get('action')!r} at confidence {confidence:.2f} "
                    f"({d.get('reason') or 'no reason given'}); below the {CONFIDENCE_FLOOR:.2f} floor."
                ),
            )
            overrides.append({
                "file_id": d.get("file_id"), "column": d.get("column"),
                "from": d.get("action"), "to": "human_review",
                "rule": "confidence_floor", "confidence": confidence,
            })
            out.append(new_d)
        else:
            out.append(d)
    return out, overrides


BLOCKING_ISSUE_FLOOR = 3


def apply_blocking_floor(
    decisions: list[dict[str, Any]],
    blocking_attempts: dict[tuple[str, str], int],
    floor: int = BLOCKING_ISSUE_FLOOR,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Force human_review once a (file_id, column) has drawn `floor`
    Sentinel blocking rejections, independent of iteration_cap. A dedicated
    per-column counter -- unlike the confidence floor above, this only
    kicks in when Sentinel has actually raised 'blocking' on that column
    repeatedly, so a low rigor setting can never let a genuinely contested
    column ship without review. Deterministic, so it runs before Sentinel's
    next LLM call rather than costing another review call on a column
    that is going to human review regardless."""
    out: list[dict[str, Any]] = []
    overrides: list[dict[str, Any]] = []
    for d in decisions:
        key = (d.get("file_id"), d.get("column"))
        attempts = blocking_attempts.get(key, 0)
        if d.get("action") != "human_review" and attempts >= floor:
            new_d = dict(d)
            new_d.update(
                action="human_review",
                reason=(
                    f"Blocking-issue floor: Sentinel has raised a blocking issue on this "
                    f"column {attempts} times, at or above the {floor} floor required "
                    f"before a decision ships unreviewed."
                ),
                suggested_action=d.get("action"),
                suggested_confidence=d.get("confidence"),
                suggested_reason=(
                    f"Judge proposed {d.get('action')!r} ({d.get('reason') or 'no reason given'}); "
                    f"Sentinel raised a blocking issue on this column {attempts} times, "
                    f"at or above the {floor} floor."
                ),
            )
            overrides.append({
                "file_id": d.get("file_id"), "column": d.get("column"),
                "from": d.get("action"), "to": "human_review",
                "rule": "blocking_issue_floor", "attempts": attempts,
            })
            out.append(new_d)
        else:
            out.append(d)
    return out, overrides


def apply_sentinel_escalations(
    decisions: list[dict[str, Any]],
    escalations: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert the decisions Sentinel flagged 'escalate' to human_review,
    carrying Judge's last committed action/confidence/reason into
    suggested_*. Judge itself never proposes human_review (2026-08-17
    redesign); this is the one place besides verify_keep_decisions/anti-loop
    where a decision becomes human_review, and all three follow the same
    contract: the reviewer sees what was proposed, not a bare rejection."""
    by_key = {(e.get("file_id"), e.get("column")): e for e in escalations if e.get("column")}
    if not by_key:
        return decisions, []
    out: list[dict[str, Any]] = []
    overrides: list[dict[str, Any]] = []
    for d in decisions:
        key = (d.get("file_id"), d.get("column"))
        issue = by_key.get(key)
        if issue and d.get("action") != "human_review":
            new_d = dict(d)
            new_d.update(
                action="human_review",
                reason=f"Sentinel escalation: {issue.get('problem') or 'genuinely ambiguous under the applicable regulation'}",
                suggested_action=d.get("action"),
                suggested_confidence=d.get("confidence"),
                suggested_reason=(
                    f"Judge proposed {d.get('action')!r} ({d.get('reason') or 'no reason given'}); "
                    f"Sentinel flagged this ambiguous rather than clearly wrong: "
                    f"{issue.get('problem') or 'see Sentinel summary'}"
                ),
            )
            overrides.append({
                "file_id": d.get("file_id"), "column": d.get("column"),
                "from": d.get("action"), "to": "human_review",
                "rule": "sentinel_escalation",
            })
            out.append(new_d)
        else:
            out.append(d)
    return out, overrides
