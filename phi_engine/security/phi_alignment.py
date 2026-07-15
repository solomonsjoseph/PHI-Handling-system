"""AI-assisted PHI header→rule alignment (Note 9 + Note 8 "specify method").

For a column header that NO deterministic pinned rule covers, the AI infers the
variable's nature from its NAME (e.g. ``b_dat`` / ``birdat`` / ``birthDate`` →
birth date), looks up the applicable rule in the value-free rulebook, and ALIGNS
the column to that rule's ACTION while emitting a regex that recognizes the
column. The rulebook (from official guidance) defines the POLICY; the AI only
decides *which* rule a fuzzy-named column maps to and writes the pattern.

Safety (separation of powers):

* **GR-1** — the AI receives ONLY the column NAME + the value-free rulebook JSON.
  Never a dataset row value.
* **AI proposes, determinism disposes** — every proposal is verified by
  :func:`verify_aligned_rule` (action ∈ the existing set; regex compiles, matches
  its own header, is not over-broad; the action agrees with the cited rulebook
  rule; the citation is an official source). Up to ``max_attempts`` tries; on
  failure the header goes to human review (value-free) — it is never silently
  kept.
* **Pinned rules stay the floor** — alignment only runs for the *uncovered* set,
  and a bad proposal can only be rejected, never lower protection.

The verified rules are frozen into the scrub config + captured in the snapshot
(reproducibility); the deterministic ``run_scrub`` engine applies them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from phi_engine.security.phi_review import (
    Action,
    HeldReason,
    _header_match_texts,
    validate_official_source_url,
)

__all__ = [
    "RULE_FIELD_FOR_ACTION",
    "AlignedRule",
    "HeaderAligner",
    "LLMHeaderAligner",
    "align_uncovered_headers",
    "verify_aligned_rule",
]

# Actions the AI may align to — exactly the classification Action enum. (``band``
# and ``birthdate`` are scrub-config rungs, not Action members and not emitted by
# the pinned rulebook rules, so they are intentionally excluded here.)
_ALLOWED_ACTIONS: frozenset[str] = frozenset(a.value for a in Action)

#: Which ``phi_scrub.yaml`` list an aligned action lands in. Restricted to the
#: PATTERN-LIST-realizable actions: an aligned rule is materialized as a regex in
#: one of these lists, so the upgraded classification always has a matching scrub
#: rule (decided == applied, assertion 12). ``generalize``/``cap``/``band`` need a
#: maintainer-authored structured rule (a generalization map / threshold), so an
#: alignment to those is rejected by the verifier and routed to human review.
RULE_FIELD_FOR_ACTION: dict[str, str] = {
    "drop": "drop_fields",
    "jitter_date": "date_fields",
    "pseudonymize": "id_fields",
    "suppress": "suppress_small_cell_fields",
}

# Benign probe headers an aligned pattern must NOT match (catches an over-broad
# regex that matches its own header AND unrelated clinical columns). Several
# diverse names, not one, so a pattern crafted to dodge a single probe still trips.
_BENIGN_PROBES: tuple[str, ...] = (
    "totally_unrelated_benign_measure_value",
    "hemoglobin_result",
    "culture_status_code",
    "visit_number",
    "treatment_arm",
    "weight_kg",
)

_VALUE_MARKERS: tuple[str, ...] = ("raw_value", "sample_value", "synthetic_value", "_phi_scrubbed")


@dataclass(frozen=True)
class AlignedRule:
    """One AI-aligned header→rule binding (value-free; column NAME only)."""

    header: str
    inferred_variable_type: str
    action: str
    regex_pattern: str
    matched_rule_id: str
    rule_citation: str
    jurisdictions: tuple[str, ...] = ()
    reason: str = ""
    confidence: float = 0.0
    attempts: int = 1

    def to_json(self) -> dict[str, Any]:
        return {
            "header": self.header,
            "inferred_variable_type": self.inferred_variable_type,
            "action": self.action,
            "regex_pattern": self.regex_pattern,
            "matched_rule_id": self.matched_rule_id,
            "rule_citation": self.rule_citation,
            "jurisdictions": list(self.jurisdictions),
            "reason": self.reason,
            "confidence": self.confidence,
            "attempts": self.attempts,
        }


class HeaderAligner(Protocol):
    """An injectable header aligner (real LLM client or a test fake)."""

    def align_one(
        self, header: str, rulebook_json: dict[str, Any], jurisdictions: tuple[str, ...]
    ) -> dict[str, Any]: ...


def _is_over_broad(pattern: str) -> bool:
    """True for catch-all patterns that would match (over-scrub) unrelated columns."""
    core = pattern.strip().lstrip("^").rstrip("$").strip()
    return core in {"", ".*", ".+", "(.*)", "(.+)", ".*?", "(.*)?"}


def _has_value_markers(obj: Any) -> bool:
    text = json.dumps(obj)
    return any(m in text for m in _VALUE_MARKERS)


def verify_aligned_rule(
    rule: AlignedRule, rulebook_json: dict[str, Any]
) -> tuple[bool, tuple[str, ...]]:
    """Deterministically verify an AI-proposed alignment (no LLM, no I/O).

    Returns ``(ok, errors)``. ``ok`` only when every check passes.
    """
    errors: list[str] = []

    # 1. action is a real existing classification action (no invented policy).
    if rule.action not in _ALLOWED_ACTIONS:
        errors.append(f"action {rule.action!r} is not an allowed action")

    # 2. regex compiles, matches its OWN header, and is not a catch-all.
    compiled: re.Pattern[str] | None = None
    try:
        compiled = re.compile(rule.regex_pattern, re.I)
    except re.error as exc:
        errors.append(f"regex does not compile: {exc}")
    if compiled is not None:
        if _is_over_broad(rule.regex_pattern):
            errors.append("regex is over-broad (catch-all)")
        elif not any(compiled.search(text) for text in _header_match_texts(rule.header)):
            errors.append("regex does not match its own header")
        elif any(compiled.search(probe) for probe in _BENIGN_PROBES):
            errors.append("regex over-matches an unrelated benign header")

    # 3. action agrees with the cited rulebook rule (the rulebook defines policy).
    by_id = {r.get("id"): r for r in rulebook_json.get("rules", [])}
    target = by_id.get(rule.matched_rule_id)
    if target is None:
        errors.append("matched_rule_id is not in the rulebook")
    elif target.get("action") != rule.action:
        errors.append("action disagrees with the cited rulebook rule")

    # 4. citation is an official source (re-run the rulebook allowlist).
    try:
        validate_official_source_url(rule.rule_citation)
    except Exception:
        errors.append("citation is not an official source")

    # 5. the action maps to a known scrub-config list.
    if RULE_FIELD_FOR_ACTION.get(rule.action) is None:
        errors.append(f"no scrub-config field for action {rule.action!r}")

    # 6. value-free (defense in depth).
    if _has_value_markers(rule.to_json()):
        errors.append("aligned rule contains a value-like marker")

    return (not errors, tuple(errors))


def _coerce_aligned_rule(header: str, raw: dict[str, Any]) -> AlignedRule:
    """Build an AlignedRule from an adapter's dict, defensively (no trust)."""
    juris = raw.get("jurisdictions") or ()
    if isinstance(juris, str):
        juris = (juris,)
    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return AlignedRule(
        header=header,
        inferred_variable_type=str(raw.get("inferred_variable_type", "")),
        action=str(raw.get("action", "")),
        regex_pattern=str(raw.get("regex_pattern", "")),
        matched_rule_id=str(raw.get("matched_rule_id", "")),
        rule_citation=str(raw.get("rule_citation", "")),
        jurisdictions=tuple(str(j) for j in juris),
        reason=str(raw.get("reason", "")),
        confidence=confidence,
    )


def align_uncovered_headers(
    headers: list[str] | tuple[str, ...],
    rulebook_json: dict[str, Any],
    jurisdictions: tuple[str, ...],
    *,
    aligner: HeaderAligner,
    max_attempts: int = 3,
) -> tuple[list[AlignedRule], list[HeldReason]]:
    """Align each uncovered header to a rulebook rule; verify; hold on failure.

    For each header, ask *aligner* for a proposal and verify it; retry up to
    ``max_attempts`` times. A header that never produces a verifier-passing rule
    yields a value-free :class:`HeldReason` (it goes to human review — never
    silently kept). Returns ``(aligned, held)``.
    """
    aligned: list[AlignedRule] = []
    held: list[HeldReason] = []
    for header in headers:
        ok = False
        last_errors: tuple[str, ...] = ("no attempt produced a candidate",)
        for attempt in range(1, max_attempts + 1):
            try:
                raw = aligner.align_one(header, rulebook_json, jurisdictions)
                candidate = replace(_coerce_aligned_rule(header, raw), attempts=attempt)
            except Exception as exc:  # adapter/parse failure → treat as a failed attempt
                last_errors = (f"aligner error: {type(exc).__name__}",)
                continue
            good, errors = verify_aligned_rule(candidate, rulebook_json)
            if good:
                aligned.append(candidate)
                ok = True
                break
            last_errors = errors
        if not ok:
            held.append(
                HeldReason(
                    what_was_tried=(
                        f"AI header→rule alignment for column name (header NAMES only), "
                        f"{max_attempts} attempts, each deterministically verified"
                    ),
                    what_was_ambiguous=(
                        "could not produce a verifier-passing rule for this column name "
                        f"(last issues: {'; '.join(last_errors)})"
                    ),
                    what_would_resolve=(
                        "add a deterministic pinned rule (phi_review) or a phi_scrub.yaml "
                        "entry for this column name shape, then re-run"
                    ),
                )
            )
    return aligned, held


# Default system prompt for the production LLM aligner. The user prompt carries
# the column NAME + the value-free rulebook rules; never a row value.
_ALIGN_SYSTEM_PROMPT = (
    "You align a clinical-study column NAME to a PHI handling rule. You are given "
    "ONLY the column name and a value-free rulebook (the allowed rules, each with "
    "an id, jurisdiction, action, and reason). You never see data values. "
    "Infer the variable's nature from the name (e.g. 'b_dat' -> birth date), pick "
    "the ONE rulebook rule that applies, and return STRICT JSON with keys: "
    "inferred_variable_type, action (exactly the chosen rule's action), "
    "regex_pattern (a case-insensitive Python regex that recognizes THIS column "
    "name and is not a catch-all), matched_rule_id (the chosen rule's id), "
    "rule_citation (an official source URL from the rulebook), jurisdictions "
    "(list), reason (short), confidence (0-1). Output ONLY the JSON object."
)


@dataclass(frozen=True)
class LLMHeaderAligner:
    """Production aligner — wraps an injected ``invoke_json`` client (Note 9).

    Constructed only when alignment is enabled; the deterministic test suite
    injects a fake aligner instead, so no real model is ever built in tests.
    """

    client: Any = field(default=None)

    def _resolved_client(self) -> Any:
        if self.client is not None:
            return self.client
        # This standalone engine ships no bundled LLM adapter. Alignment is
        # opt-in and always injected (a real client in production, a fake in
        # tests); a bare aligner with no client is a misconfiguration, surfaced
        # clearly here rather than as a stale missing-module ImportError.
        raise RuntimeError(
            "LLMHeaderAligner requires an injected `client` exposing "
            "invoke_json(system_prompt, user_prompt); none was provided. Inject a "
            "client or disable alignment (REPORTAL_PHI_ALIGNMENT_ENABLED=0)."
        )

    def align_one(
        self, header: str, rulebook_json: dict[str, Any], jurisdictions: tuple[str, ...]
    ) -> dict[str, Any]:
        rules_view = {
            "rules": rulebook_json.get("rules", []),
            "sources": rulebook_json.get("sources", []),
        }
        user_prompt = (
            f"Column name: {header}\n"
            f"Jurisdictions: {list(jurisdictions)}\n"
            f"Rulebook (value-free): {json.dumps(rules_view, sort_keys=True)}\n"
            "Return the alignment JSON."
        )
        result = self._resolved_client().invoke_json(_ALIGN_SYSTEM_PROMPT, user_prompt)
        if not isinstance(result, dict):
            raise ValueError("aligner did not return a JSON object")
        return result
