"""Validator for the privacy-gateway research contracts.

This module validates the three privacy-gateway research artifacts
produced by the privacy-gateway research pipeline's supporting evidence
work:

- `research/privacy_gateway/evidence_ledger.jsonl` -- one row per sourced claim.
- `research/privacy_gateway/candidate_registry.jsonl` -- one row per product/method.
- `research/privacy_gateway/dispositions.json` -- exactly one retain/replace/
  wrap/integrate/build decision per required capability.

Also checks that every inline claim-id citation in the rendered research
report passed via `--report` resolves to a real evidence-ledger claim_id.

CLI:
    python -m harness.validate_privacy_research \\
        --evidence research/privacy_gateway/evidence_ledger.jsonl \\
        --candidates research/privacy_gateway/candidate_registry.jsonl \\
        --dispositions research/privacy_gateway/dispositions.json \\
        --report <path/to/rendered/report.md>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Schemas (see local research/privacy_gateway/ schema; mirrors the plan's
# Approach Step 1 field lists verbatim).
# ---------------------------------------------------------------------------

EVIDENCE_REQUIRED_FIELDS = (
    "claim_id", "claim_text", "claim_type", "source_title", "publisher",
    "source_url_or_path", "source_version_or_date", "pinpoint", "accessed_at",
    "jurisdiction", "product_and_version", "primary_source",
    "corroborating_claim_ids", "status", "review_note",
)
CLAIM_TYPES = frozenset({
    "law", "standard", "peer_reviewed", "preprint", "vendor_capability",
    "vendor_claim", "repository_implementation", "repository_measurement",
    "inference",
})
CLAIM_STATUSES = frozenset({"confirmed", "qualified", "unverified", "refuted"})
# claim types that require a primary, dereferenceable, dated source when the
# claim is asserted at "confirmed" status (Verification: "all confirmed
# law/standard/vendor-capability claims have primary sources and access dates").
PRIMARY_SOURCE_REQUIRED_CLAIM_TYPES = frozenset({"law", "standard", "vendor_capability"})

CANDIDATE_REQUIRED_FIELDS = (
    "candidate_id", "category", "vendor", "product", "version_or_release",
    "active_status", "cost_model", "license", "deployment",
    "supported_data_classes", "modalities", "channels", "detect_actions",
    "transform_actions", "custom_policy_support", "input_data_location",
    "retention_and_training", "baa_dpa_status", "regions_and_subprocessors",
    "encryption_and_key_control", "private_networking", "audit_behavior",
    "known_bypasses", "independent_evidence_claim_ids", "vendor_claim_ids",
    "benchmark_status", "benchmark_artifact", "not_run_reason", "score",
)
CANDIDATE_CATEGORIES = frozenset({
    "open_local_phi_pii", "managed_healthcare_engine", "ai_native_gateway",
    "enterprise_dlp", "secrets_detection", "tokenization_privacy_engineering",
})
ACTIVE_STATUSES = frozenset({"active", "eol", "acquired", "renamed", "unknown"})
BENCHMARK_STATUSES = frozenset({"not_attempted", "not_run", "pending_poc", "benchmarked"})

DISPOSITION_REQUIRED_FIELDS = (
    "capability", "current_control", "disposition", "selected_candidate_id",
    "fallback_candidate_id", "hard_gate_results", "weighted_score",
    "rationale_claim_ids",
)
REQUIRED_CAPABILITIES = (
    "phi_pii_detection", "secrets_detection", "proprietary_data_detection",
    "structured_reidentification_risk", "redaction_and_masking",
    "pseudonymization_and_token_vault", "utility_preserving_transforms",
    "multimodal_file_handling", "prompt_input_gate", "model_output_gate",
    "tool_mcp_gate", "logs_traces_telemetry", "storage_discovery",
    "endpoint_browser_sharing", "audit_governance",
)
DISPOSITION_VALUES = frozenset({"retain", "replace", "wrap", "integrate", "build"})
# Sentinel a disposition may use in place of a real candidate_id when the
# selected/fallback component IS the current repository control (retain) or a
# net-new narrow build (build) rather than an external product.
_NO_EXTERNAL_CANDIDATE_SENTINEL = "repository"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _load_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse a JSONL file. Returns (records, parse_errors)."""
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    if not path.is_file():
        return records, [f"{path}: file does not exist"]
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"{path}:{lineno}: invalid JSON ({exc})")
            continue
        if not isinstance(parsed, dict):
            errors.append(f"{path}:{lineno}: row is not a JSON object")
            continue
        records.append(parsed)
    return records, errors


def _is_iso_date(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        # Accept plain dates and full ISO timestamps.
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        try:
            date.fromisoformat(value[:10])
            return True
        except ValueError:
            return False


# ---------------------------------------------------------------------------
# validate_evidence
# ---------------------------------------------------------------------------

def validate_evidence(path: Path) -> list[str]:
    """Validate research/privacy_gateway/evidence_ledger.jsonl. Returns error strings (empty == valid)."""
    errors: list[str] = []
    records, parse_errors = _load_jsonl(path)
    errors.extend(parse_errors)

    seen_ids: dict[str, int] = {}
    all_ids: set[str] = set()
    for idx, row in enumerate(records, start=1):
        loc = f"{path}:row{idx}"
        missing = [f for f in EVIDENCE_REQUIRED_FIELDS if f not in row]
        if missing:
            errors.append(f"{loc}: missing required field(s) {missing}")
            continue

        claim_id = row["claim_id"]
        if not isinstance(claim_id, str) or not claim_id.strip():
            errors.append(f"{loc}: claim_id must be a non-empty string")
        else:
            all_ids.add(claim_id)
            seen_ids[claim_id] = seen_ids.get(claim_id, 0) + 1

        if row["claim_type"] not in CLAIM_TYPES:
            errors.append(f"{loc} ({claim_id}): invalid claim_type {row['claim_type']!r}")
        if row["status"] not in CLAIM_STATUSES:
            errors.append(f"{loc} ({claim_id}): invalid status {row['status']!r}")
        if not isinstance(row["pinpoint"], str) or not row["pinpoint"].strip():
            errors.append(f"{loc} ({claim_id}): missing pinpoint (section/page/paragraph anchor)")
        if not _is_iso_date(row["accessed_at"]):
            errors.append(f"{loc} ({claim_id}): missing or invalid accessed_at date")
        if not isinstance(row["source_url_or_path"], str) or not row["source_url_or_path"].strip():
            errors.append(f"{loc} ({claim_id}): missing source_url_or_path")
        if not isinstance(row["primary_source"], bool):
            errors.append(f"{loc} ({claim_id}): primary_source must be a boolean")
        if not isinstance(row["corroborating_claim_ids"], list):
            errors.append(f"{loc} ({claim_id}): corroborating_claim_ids must be a list")

        # Verification contract: confirmed law/standard/vendor-capability
        # claims must carry a primary, dated source -- never a marketing
        # summary or secondary review standing in for the controlling text.
        if row["status"] == "confirmed" and row["claim_type"] in PRIMARY_SOURCE_REQUIRED_CLAIM_TYPES:
            if row.get("primary_source") is not True:
                errors.append(
                    f"{loc} ({claim_id}): confirmed {row['claim_type']} claim requires primary_source=true"
                )
            if not _is_iso_date(row.get("accessed_at")):
                errors.append(f"{loc} ({claim_id}): confirmed claim requires a valid accessed_at date")
            if not isinstance(row["source_url_or_path"], str) or not row["source_url_or_path"].strip():
                errors.append(f"{loc} ({claim_id}): confirmed claim requires a real source_url_or_path")

        # vendor-reported performance/effectiveness numbers must stay tagged
        # vendor_claim (not confirmed as fact) unless independently
        # reproduced -- corroboration by another claim (peer_reviewed /
        # repository_measurement / a second independent vendor) is what
        # "independently reproduced" means here.
        if row["claim_type"] == "vendor_claim" and row["status"] == "confirmed":
            if not row.get("corroborating_claim_ids"):
                errors.append(
                    f"{loc} ({claim_id}): performance/capability marked confirmed from vendor evidence "
                    "alone -- vendor_claim rows require non-empty corroborating_claim_ids to reach "
                    "status=confirmed (independent reproduction), otherwise use 'qualified' or 'unverified'"
                )

    for claim_id, count in seen_ids.items():
        if count > 1:
            errors.append(f"{path}: duplicate claim_id {claim_id!r} ({count} occurrences)")

    status_by_id = {r["claim_id"]: r.get("status") for r in records if isinstance(r.get("claim_id"), str)}

    # Cross-reference corroborating_claim_ids against the file's own id set,
    # and refuse an unverified/refuted claim used unlabelled as support.
    for idx, row in enumerate(records, start=1):
        citing_status = row.get("status")
        for ref in row.get("corroborating_claim_ids") or []:
            if ref not in all_ids:
                errors.append(
                    f"{path}:row{idx} ({row.get('claim_id')}): corroborating_claim_ids references "
                    f"unknown claim_id {ref!r}"
                )
            elif status_by_id.get(ref) in {"unverified", "refuted"} and citing_status in {"confirmed", "qualified"}:
                # A claim itself already labelled unverified/refuted citing another
                # unverified/refuted claim inherits that same caution -- no
                # laundering risk. The violation is a confirmed/qualified claim
                # borrowing credibility from an unlabelled weak source.
                errors.append(
                    f"{path}:row{idx} ({row.get('claim_id')}): corroborating_claim_ids cites {ref!r} "
                    f"which is itself status={status_by_id.get(ref)!r} -- a {citing_status} claim cannot "
                    "borrow support from an unlabelled unverified/refuted claim"
                )
    return errors


# ---------------------------------------------------------------------------
# validate_candidates
# ---------------------------------------------------------------------------

def validate_candidates(path: Path, evidence: Path) -> list[str]:
    """Validate research/privacy_gateway/candidate_registry.jsonl against the evidence ledger."""
    errors: list[str] = []
    records, parse_errors = _load_jsonl(path)
    errors.extend(parse_errors)

    evidence_records, evidence_parse_errors = _load_jsonl(evidence)
    if evidence_parse_errors:
        errors.append(f"{path}: cannot fully cross-check claim_id references -- {evidence} has parse errors")
    evidence_ids = {r["claim_id"] for r in evidence_records if isinstance(r.get("claim_id"), str)}
    evidence_status = {
        r["claim_id"]: r.get("status") for r in evidence_records if isinstance(r.get("claim_id"), str)
    }

    seen_ids: dict[str, int] = {}
    for idx, row in enumerate(records, start=1):
        loc = f"{path}:row{idx}"
        missing = [f for f in CANDIDATE_REQUIRED_FIELDS if f not in row]
        if missing:
            errors.append(f"{loc}: missing required field(s) {missing}")
            continue

        cid = row["candidate_id"]
        if not isinstance(cid, str) or not cid.strip():
            errors.append(f"{loc}: candidate_id must be a non-empty string")
        else:
            seen_ids[cid] = seen_ids.get(cid, 0) + 1

        if row["category"] not in CANDIDATE_CATEGORIES:
            errors.append(f"{loc} ({cid}): invalid category {row['category']!r}")
        if row["active_status"] not in ACTIVE_STATUSES:
            errors.append(f"{loc} ({cid}): invalid active_status {row['active_status']!r}")
        if row["benchmark_status"] not in BENCHMARK_STATUSES:
            errors.append(f"{loc} ({cid}): invalid benchmark_status {row['benchmark_status']!r}")
        if row["benchmark_status"] == "not_run" and not str(row.get("not_run_reason") or "").strip():
            errors.append(f"{loc} ({cid}): benchmark_status=not_run requires a non-empty not_run_reason")

        # "a candidate without cost/license/privacy/benchmark status" --
        # cost_model/active_status/benchmark_status enums are checked above;
        # license and the privacy-posture fields must be non-empty prose.
        if not str(row.get("cost_model") or "").strip() or row["cost_model"] not in {
            "open_source_free", "freemium", "paid_subscription", "usage_based",
            "enterprise_contract", "unknown",
        }:
            errors.append(f"{loc} ({cid}): invalid or missing cost_model {row.get('cost_model')!r}")
        if not str(row.get("license") or "").strip():
            errors.append(f"{loc} ({cid}): missing license")
        if not str(row.get("encryption_and_key_control") or "").strip():
            errors.append(f"{loc} ({cid}): missing encryption_and_key_control (privacy status)")
        if not str(row.get("audit_behavior") or "").strip():
            errors.append(f"{loc} ({cid}): missing audit_behavior (privacy status)")

        for list_field in (
            "supported_data_classes", "modalities", "channels", "detect_actions",
            "transform_actions", "known_bypasses", "independent_evidence_claim_ids",
            "vendor_claim_ids",
        ):
            if not isinstance(row[list_field], list):
                errors.append(f"{loc} ({cid}): {list_field} must be a list")

        for ref in row.get("independent_evidence_claim_ids") or []:
            if ref not in evidence_ids:
                errors.append(f"{loc} ({cid}): independent_evidence_claim_ids references unknown claim_id {ref!r}")
            elif evidence_status.get(ref) in {"unverified", "refuted"}:
                errors.append(
                    f"{loc} ({cid}): independent_evidence_claim_ids cites {ref!r} which is itself "
                    f"status={evidence_status.get(ref)!r} -- an unlabelled unverified/refuted claim "
                    "cannot be used as independent evidence"
                )
        for ref in row.get("vendor_claim_ids") or []:
            if ref not in evidence_ids:
                errors.append(f"{loc} ({cid}): vendor_claim_ids references unknown claim_id {ref!r}")

        score = row.get("score")
        if score is not None and not (isinstance(score, (int, float)) and 0 <= score <= 100):
            errors.append(f"{loc} ({cid}): score must be null or a number in [0, 100]")

    for cid, count in seen_ids.items():
        if count > 1:
            errors.append(f"{path}: duplicate candidate_id {cid!r} ({count} occurrences)")

    return errors


# ---------------------------------------------------------------------------
# validate_dispositions
# ---------------------------------------------------------------------------

def validate_dispositions(path: Path, candidates: Path, evidence: Path) -> list[str]:
    """Validate research/privacy_gateway/dispositions.json against candidates and evidence."""
    errors: list[str] = []
    if not path.is_file():
        return [f"{path}: file does not exist"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"{path}: invalid JSON ({exc})"]
    if not isinstance(data, list):
        return [f"{path}: top-level JSON must be an array of disposition objects"]

    candidate_records, candidate_parse_errors = _load_jsonl(candidates)
    if candidate_parse_errors:
        errors.append(f"{path}: cannot fully cross-check candidate_id references -- {candidates} has parse errors")
    candidate_ids = {r["candidate_id"] for r in candidate_records if isinstance(r.get("candidate_id"), str)}

    evidence_records, evidence_parse_errors = _load_jsonl(evidence)
    if evidence_parse_errors:
        errors.append(f"{path}: cannot fully cross-check claim_id references -- {evidence} has parse errors")
    evidence_ids = {r["claim_id"] for r in evidence_records if isinstance(r.get("claim_id"), str)}
    evidence_status = {
        r["claim_id"]: r.get("status") for r in evidence_records if isinstance(r.get("claim_id"), str)
    }

    seen_capabilities: dict[str, int] = {}
    for idx, row in enumerate(data, start=1):
        loc = f"{path}[{idx}]"
        if not isinstance(row, dict):
            errors.append(f"{loc}: disposition entry must be an object")
            continue
        missing = [f for f in DISPOSITION_REQUIRED_FIELDS if f not in row]
        if missing:
            errors.append(f"{loc}: missing required field(s) {missing}")
            continue

        capability = row["capability"]
        if capability not in REQUIRED_CAPABILITIES:
            errors.append(f"{loc}: capability {capability!r} is not one of the 15 required capabilities")
        else:
            seen_capabilities[capability] = seen_capabilities.get(capability, 0) + 1

        if row["disposition"] not in DISPOSITION_VALUES:
            errors.append(f"{loc} ({capability}): invalid disposition {row['disposition']!r}")

        for id_field in ("selected_candidate_id", "fallback_candidate_id"):
            value = row.get(id_field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{loc} ({capability}): {id_field} must be a non-empty string "
                               f"(use {_NO_EXTERNAL_CANDIDATE_SENTINEL!r} when the component is the "
                               "current repository control rather than an external candidate)")
            elif value != _NO_EXTERNAL_CANDIDATE_SENTINEL and value not in candidate_ids:
                errors.append(f"{loc} ({capability}): {id_field} references unknown candidate_id {value!r}")

        selected = row.get("selected_candidate_id")
        fallback = row.get("fallback_candidate_id")
        if isinstance(selected, str) and isinstance(fallback, str) and selected == fallback and selected != _NO_EXTERNAL_CANDIDATE_SENTINEL:
            errors.append(f"{loc} ({capability}): fallback_candidate_id must differ from selected_candidate_id")

        rationale = row.get("rationale_claim_ids")
        if not isinstance(rationale, list) or not rationale:
            errors.append(f"{loc} ({capability}): rationale_claim_ids must be a non-empty list")
        else:
            for ref in rationale:
                if ref not in evidence_ids:
                    errors.append(f"{loc} ({capability}): rationale_claim_ids references unknown claim_id {ref!r}")
                elif evidence_status.get(ref) in {"unverified", "refuted"}:
                    errors.append(
                        f"{loc} ({capability}): rationale_claim_ids cites {ref!r} which is itself "
                        f"status={evidence_status.get(ref)!r} -- an unlabelled unverified/refuted claim "
                        "cannot be used as disposition support"
                    )

        if not isinstance(row.get("hard_gate_results"), dict):
            errors.append(f"{loc} ({capability}): hard_gate_results must be an object")
        elif row["disposition"] in {"integrate", "wrap", "replace"} and not row["hard_gate_results"]:
            errors.append(f"{loc} ({capability}): {row['disposition']} disposition requires non-empty hard_gate_results")

        score = row.get("weighted_score")
        if score is not None and not (isinstance(score, (int, float)) and 0 <= score <= 100):
            errors.append(f"{loc} ({capability}): weighted_score must be null or a number in [0, 100]")

    missing_capabilities = [c for c in REQUIRED_CAPABILITIES if c not in seen_capabilities]
    if missing_capabilities:
        errors.append(f"{path}: missing disposition(s) for required capability(ies) {missing_capabilities}")
    duplicate_capabilities = [c for c, n in seen_capabilities.items() if n > 1]
    if duplicate_capabilities:
        errors.append(f"{path}: duplicate disposition(s) for capability(ies) {duplicate_capabilities}")

    return errors


# ---------------------------------------------------------------------------
# validate_report -- every inline claim-id tag in the rendered report must
# resolve to a real evidence-ledger claim_id (Verification: "every factual
# report tag resolves to one ledger claim").
# ---------------------------------------------------------------------------

_CLAIM_TAG_RE = re.compile(r"`([a-z]+-[a-z0-9]{2,8}-?\d{2,6})`")


def validate_report(path: Path, evidence: Path, candidates: Path | None = None) -> list[str]:
    """Validate that every backtick-quoted claim/candidate-id tag in *path*
    resolves to a real claim_id or candidate_id. A recommendation report
    legitimately cites both evidence claim_ids (grounding a fact) and
    candidate_ids (naming a selected/fallback component) -- either counts as
    resolved."""
    errors: list[str] = []
    if not path.is_file():
        return [f"{path}: file does not exist"]
    text = path.read_text(encoding="utf-8")

    evidence_records, evidence_parse_errors = _load_jsonl(evidence)
    if evidence_parse_errors:
        errors.append(f"{path}: cannot verify report tags -- {evidence} has parse errors")
        return errors
    evidence_ids = {r["claim_id"] for r in evidence_records if isinstance(r.get("claim_id"), str)}

    candidate_ids: set[str] = set()
    if candidates is not None and candidates.is_file():
        candidate_records, candidate_parse_errors = _load_jsonl(candidates)
        if not candidate_parse_errors:
            candidate_ids = {r["candidate_id"] for r in candidate_records if isinstance(r.get("candidate_id"), str)}

    resolvable_ids = evidence_ids | candidate_ids
    tags_found = set(_CLAIM_TAG_RE.findall(text))
    # Only check tags that actually look like one of the claim_id/candidate_id
    # prefixes present in the ledgers, to avoid false positives on unrelated
    # backtick spans (file paths, code identifiers).
    known_prefixes = {cid.split("-")[0] for cid in resolvable_ids}
    for tag in sorted(tags_found):
        prefix = tag.split("-")[0]
        if prefix not in known_prefixes:
            continue
        if tag not in resolvable_ids:
            errors.append(
                f"{path}: report cites unresolved tag `{tag}` "
                f"(no matching claim_id in {evidence} or candidate_id in {candidates})"
            )
    return errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=_PROJECT_ROOT / "research/privacy_gateway/evidence_ledger.jsonl")
    parser.add_argument("--candidates", type=Path, default=_PROJECT_ROOT / "research/privacy_gateway/candidate_registry.jsonl")
    parser.add_argument("--dispositions", type=Path, default=_PROJECT_ROOT / "research/privacy_gateway/dispositions.json")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)

    all_errors: list[str] = []
    all_errors.extend(validate_evidence(args.evidence))
    all_errors.extend(validate_candidates(args.candidates, args.evidence))
    all_errors.extend(validate_dispositions(args.dispositions, args.candidates, args.evidence))
    if args.report.is_file():
        all_errors.extend(validate_report(args.report, args.evidence, args.candidates))
    else:
        all_errors.append(f"{args.report}: report file does not exist")

    if all_errors:
        print(f"FAIL -- {len(all_errors)} issue(s):")
        for err in all_errors:
            print(f"  - {err}")
        return 1

    print("PASS -- privacy-gateway research artifacts are internally consistent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
