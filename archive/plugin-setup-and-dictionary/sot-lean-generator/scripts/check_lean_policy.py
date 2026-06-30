#!/usr/bin/env python3
"""Validate SoT policy YAML against a source pack and optional benchmark YAML."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

FORBIDDEN_KEYS = {
    "schema_version",
    "policy_status",
    "source",
    "runtime_binding",
    "source_presence",
    "coverage",
    "pdf_visible_text",
    "evidence_packs",
    "catalog_refs",
    "ledger_expectations",
    "validation",
    "footnote_markers",
}

FORBIDDEN_TEXT = [
    "pii",
    "footnote",
    "superscript",
    "row-1 observation",
    "sample value",
    "sample row",
    "dataset is authoritative",
]

GENERIC_PLACEHOLDER_TEXT = [
    "visible printed field associated with pdf annotation",
    "visible printed widget associated with pdf annotation",
    "printed field associated with pdf annotation",
    "printed widget associated with pdf annotation",
]

NON_VARIABLE_ANNOTATION_KINDS = {
    "pdf_annotation_non_variable_label",
    "pdf_annotation_repeated_option_label",
}

ALIAS_ANNOTATION_KIND = "pdf_annotation_alias_to_dataset_header"

HARD_PDF_MISSING_KIND = "printed_widget_without_dataset_header"
REVIEWED_PDF_MISSING_KIND = "printed_widget_without_dataset_header_reviewed"

PDF_FIELD_COUNT_MISMATCH_KIND = "pdf_field_count_column_count_mismatch"

PRINTED_WIDGET_DISCREPANCY_KINDS = (
    HARD_PDF_MISSING_KIND,
    REVIEWED_PDF_MISSING_KIND,
)

# Duplicate row-1 header names may be documented two ways. Either is valid documentation
# of the duplicate; the checker accepts both, and the orchestrator (generate_lean_outputs)
# decides whether to publish or hold for review based on which kind is present:
#   * combined_binding  — a human approved collapsing the duplicate columns into one variable.
#   * binding_conflict  — the generator flagged the duplicate but did NOT collapse it; the form
#     is held for human review (see generate_lean_outputs._duplicate_binding_review_reason).
DUPLICATE_HEADER_DISCREPANCY_KINDS = (
    "dataset_duplicate_header_combined_binding",
    "dataset_duplicate_header_binding_conflict",
)

EXIT_VALIDATION_FAILURE = 1
EXIT_SOURCE_MISMATCH = 2
EXIT_SCRIPT_GAP = 3

FORM_6_HIV_HEADERS = [
    "SUBJID",
    "HIV_VISIT",
    "ICTC",
    "HIV_HIVND",
    "HIV_HIVDAT",
    "HIV_HIV",
    "HIV_HIVNDOTH",
    "HIV_ARTTX",
    "HIV_ARTDAT",
    "HIV_ARTND",
    "HIV_CD4DONE",
    "HIV_CD4DAT",
    "HIV_CD4",
    "HIV_CD4LY",
    "HIV_CD4ND",
    "HIV_CD4LYND",
    "HIV_SIGN",
    "HIV_INIT",
    "HIV_COMPDAT",
    "Time_Stamp",
]

FORM_6_HIV_MUTEX_PAIRS = [
    ("HIV_ARTDAT", "HIV_ARTND"),
    ("HIV_CD4", "HIV_CD4ND"),
    ("HIV_CD4LY", "HIV_CD4LYND"),
]


def _load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_pdf_sha(source_pack: dict[str, Any], repo_root: Path) -> tuple[int, str] | None:
    expected = source_pack.get("pdf_sha256")
    if expected is None:
        return None
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
        return EXIT_SCRIPT_GAP, "source pack pdf_sha256 is malformed"

    pdf_value = source_pack.get("pdf")
    if not isinstance(pdf_value, str) or not pdf_value:
        return EXIT_SCRIPT_GAP, "source pack includes pdf_sha256 but no pdf path"

    pdf_path = Path(pdf_value)
    if not pdf_path.is_absolute():
        pdf_path = repo_root / pdf_path
    if not pdf_path.exists():
        return EXIT_SCRIPT_GAP, f"source pack PDF path does not exist: {pdf_path}"

    actual = _sha256_file(pdf_path)
    if actual.lower() != expected.lower():
        return (
            EXIT_SOURCE_MISMATCH,
            f"SHA mismatch: source pack pdf_sha256 {expected} != current PDF {actual}",
        )
    return None


def _walk(obj: Any, path: str = "$") -> list[tuple[str, Any]]:
    items = [(path, obj)]
    if isinstance(obj, dict):
        for key, value in obj.items():
            items.extend(_walk(value, f"{path}.{key}"))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            items.extend(_walk(value, f"{path}[{index}]"))
    return items


def _compare(a: Any, b: Any, path: str = "$", out: list[str] | None = None) -> list[str]:
    if out is None:
        out = []
    if type(a) is not type(b):
        out.append(f"{path}: type differs {type(a).__name__} != {type(b).__name__}")
        return out
    if isinstance(a, dict):
        a_keys = list(a.keys())
        b_keys = list(b.keys())
        if a_keys != b_keys:
            out.append(f"{path}: keys/order differ {a_keys!r} != {b_keys!r}")
        for key in a.keys() & b.keys():
            _compare(a[key], b[key], f"{path}.{key}", out)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(f"{path}: length differs {len(a)} != {len(b)}")
        for index, (left, right) in enumerate(zip(a, b)):
            _compare(left, right, f"{path}[{index}]", out)
    elif a != b:
        out.append(f"{path}: value differs {a!r} != {b!r}")
    return out


def _is_form_6_hiv(source_pack: dict[str, Any]) -> bool:
    return source_pack.get("headers") == FORM_6_HIV_HEADERS


def _skip_logic(variables: Any, name: str) -> str:
    if not isinstance(variables, dict):
        return ""
    meta = variables.get(name)
    if not isinstance(meta, dict):
        return ""
    value = meta.get("skip_logic")
    return value if isinstance(value, str) else ""


def _has_mutex(variables: Any, name: str, partner: str) -> bool:
    return f"mutually exclusive with {partner}".lower() in _skip_logic(variables, name).lower()


def _duplicates(values: Any) -> dict[str, int]:
    if not isinstance(values, list):
        return {}
    return {str(value): count for value, count in Counter(values).items() if count > 1}


def _unique_preserving_order(values: list[Any]) -> list[Any]:
    seen: set[Any] = set()
    out: list[Any] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _has_discrepancy_kind(policy: dict[str, Any], kind: str) -> bool:
    discrepancies = policy.get("discrepancies")
    if not isinstance(discrepancies, list):
        return False
    return any(isinstance(entry, dict) and entry.get("kind") == kind for entry in discrepancies)


def _duplicate_header_errors(
    variables: dict[str, Any], headers: Any, policy: dict[str, Any]
) -> list[str]:
    """Errors for the duplicate row-1 header invariant.

    Duplicate header names collapse to one variable key (a YAML mapping cannot hold
    duplicate keys), so the variable keys must equal the de-duplicated header order. When
    the raw headers actually contain duplicates, that collapse must be documented by a
    duplicate-header discrepancy — either a (human-approved) combined_binding or an
    (un-reviewed, held-for-review) binding_conflict. Absence of both, when duplicates
    exist, is a fail-closed error.
    """
    errors: list[str] = []
    expected_headers = _unique_preserving_order(headers)
    if list(variables.keys()) != expected_headers:
        errors.append(
            "variables keys do not match de-duplicated row-1 headers after duplicate-source collapse: "
            f"{list(variables.keys())!r} != {expected_headers!r}"
        )
    if _duplicates(headers) and not any(
        _has_discrepancy_kind(policy, kind) for kind in DUPLICATE_HEADER_DISCREPANCY_KINDS
    ):
        errors.append(
            "dataset row-1 headers contain duplicate binding names; the final policy must document "
            "the source-level collapse with a dataset_duplicate_header_combined_binding (human-approved) "
            "or dataset_duplicate_header_binding_conflict (held for review) discrepancy: "
            f"{_duplicates(headers)!r}"
        )
    return errors


def _flatten_annotation_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_flatten_annotation_values(item))
        return out
    if isinstance(value, dict):
        labels: list[str] = []
        for key in ("label", "pdf_annotation", "pdf_annotation_says"):
            item = value.get(key)
            if isinstance(item, str):
                labels.append(item)
        if not labels:
            labels.extend(str(key) for key in value)
        return labels
    return [str(value)]


def _is_variable_like_annotation(label: str, headers: set[str]) -> bool:
    value = label.strip()
    if not value:
        return False
    low = value.lower()
    non_variable_words = {
        "f",
        "m",
        "yes",
        "no",
        "unknown",
        "not done",
        "absent",
        "other",
        "sample collection",
        "blood volumes",
        "lacks time",
        "no locate",
        "too sick",
        "uncomfortable",
        "uninterested",
        "allowed",
        "never allowed",
        "no rules",
        "exeptions",
        "exceptions",
        "blistering",
        "ulceration",
        "positive",
        "negative",
        "indeterminate",
        "qgit",
        "in-house assay",
    }
    if low in non_variable_words:
        return False
    if value in headers or low in {header.lower() for header in headers}:
        return True
    if value in {"dataset_column", "pdf_annotation_says"}:
        return False
    if re.fullmatch(r"[0-9]+", value):
        return False
    if re.fullmatch(r"[0-9]+\s*TU", value, re.IGNORECASE):
        return False
    if " " in value and "_" not in value and "-" not in value:
        return False
    return "_" in value or re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{2,}", value) is not None


def _annotation_reconciliation(policy: dict[str, Any], source_pack: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    headers_raw = source_pack.get("headers")
    headers = set(headers_raw) if isinstance(headers_raw, list) else set()
    annotations: list[str] = []
    for page in source_pack.get("pages", []) or []:
        if isinstance(page, dict):
            annotations.extend(str(label) for label in page.get("annotations", []) or [])

    accepted_non_variable: set[str] = set()
    accepted_alias: dict[str, str] = {}
    hard_missing: set[str] = set()
    discrepancies = policy.get("discrepancies") or []
    if isinstance(discrepancies, list):
        for entry in discrepancies:
            if not isinstance(entry, dict):
                continue
            kind = entry.get("kind")
            if kind in NON_VARIABLE_ANNOTATION_KINDS:
                accepted_non_variable.update(
                    _flatten_annotation_values(entry.get("pdf_annotation_says"))
                )
            elif kind in PRINTED_WIDGET_DISCREPANCY_KINDS:
                hard_missing.update(_flatten_annotation_values(entry.get("pdf_annotation_says")))
            elif kind == ALIAS_ANNOTATION_KIND:
                values = entry.get("pdf_annotation_says")
                if isinstance(values, list):
                    for item in values:
                        if isinstance(item, dict):
                            label = item.get("label")
                            target = item.get("dataset_column")
                            if isinstance(label, str) and isinstance(target, str):
                                accepted_alias[label] = target
                bindings = entry.get("dataset_column_binding")
                binding_values = [
                    value for value in _flatten_annotation_values(bindings) if value in headers
                ]
                if binding_values:
                    for label in _flatten_annotation_values(entry.get("pdf_annotation_says")):
                        accepted_alias.setdefault(label, binding_values[0])

    for label in sorted(set(annotations)):
        if label in headers:
            continue
        if not _is_variable_like_annotation(label, headers):
            continue
        if label in hard_missing:
            continue
        if label in accepted_non_variable:
            continue
        alias_target = accepted_alias.get(label)
        if alias_target in headers:
            continue
        errors.append(
            f"PDF annotation {label!r} looks like a variable binding but is not an exact dataset header "
            "and is not reconciled as an alias, non-variable label, or printed-widget discrepancy"
        )
    return errors


def _validate_form_6_hiv_calibration(
    policy: dict[str, Any], source_pack: dict[str, Any]
) -> list[str]:
    if not _is_form_6_hiv(source_pack):
        return []

    errors: list[str] = []
    variables = policy.get("variables")

    hiv_result_skip = _skip_logic(variables, "HIV_HIV").lower()
    if not (
        "negative" in hiv_result_skip
        and "i1" in hiv_result_skip
        and ("completion" in hiv_result_skip or "bottom" in hiv_result_skip)
    ):
        errors.append(
            "Form 6 HIV calibration: variables.HIV_HIV.skip_logic must preserve "
            "the printed negative-result skip to completion via instruction I1"
        )

    for left, right in FORM_6_HIV_MUTEX_PAIRS:
        if not _has_mutex(variables, left, right):
            errors.append(
                "Form 6 HIV calibration: "
                f"variables.{left}.skip_logic must say it is mutually exclusive with {right}"
            )
        if not _has_mutex(variables, right, left):
            errors.append(
                "Form 6 HIV calibration: "
                f"variables.{right}.skip_logic must say it is mutually exclusive with {left}"
            )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", "--lean", dest="policy", required=True, type=Path)
    parser.add_argument("--source-pack", required=True, type=Path)
    parser.add_argument("--benchmark", type=Path)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path("."),
        help="Repository root used to resolve source-pack relative PDF paths.",
    )
    args = parser.parse_args()

    errors: list[str] = []
    policy_text = args.policy.read_text(encoding="utf-8")
    policy = _load_yaml(args.policy)
    source_pack = json.loads(args.source_pack.read_text(encoding="utf-8"))
    repo_root = args.repo_root.resolve()
    sha_status = _verify_pdf_sha(source_pack, repo_root)
    if sha_status is not None:
        exit_code, message = sha_status
        print(message, file=sys.stderr)
        return exit_code

    if not isinstance(policy, dict):
        errors.append("policy YAML root must be a mapping")
    else:
        errors.extend(
            f"missing top-level key: {required}"
            for required in ["study", "form", "sections", "variables"]
            if required not in policy
        )

        variables = policy.get("variables")
        headers = source_pack.get("headers")
        duplicate_headers = _duplicates(headers)
        if not isinstance(variables, dict):
            errors.append("variables must be a mapping")
        elif duplicate_headers:
            errors.extend(_duplicate_header_errors(variables, headers, policy))
        elif list(variables.keys()) != headers:
            errors.append(
                f"variables keys do not match row-1 headers: {list(variables.keys())!r} != {headers!r}"
            )

        sections = policy.get("sections")
        if isinstance(variables, dict) and isinstance(sections, dict):
            section_keys = set(sections.keys())
            for name, meta in variables.items():
                if not isinstance(meta, dict):
                    errors.append(f"{name}: variable entry must be a mapping")
                    continue
                if meta.get("section") not in section_keys:
                    errors.append(
                        f"{name}: section {meta.get('section')!r} is not in top-level sections"
                    )
                if "widget" not in meta:
                    errors.append(f"{name}: missing widget")
                if "pii" in meta:
                    errors.append(f"{name}: use phi, not pii")
                for field_name in ["pdf_question", "pdf_label", "widget"]:
                    field_value = meta.get(field_name)
                    if isinstance(field_value, str):
                        low = field_value.lower()
                        if any(token in low for token in GENERIC_PLACEHOLDER_TEXT):
                            errors.append(
                                f"{name}.{field_name}: generic annotation placeholder is not signal; "
                                "use the printed PDF wording/widget or set pdf_question: null with a discrepancy"
                            )

        for path, value in _walk(policy):
            if isinstance(value, dict):
                for key in value:
                    if key in FORBIDDEN_KEYS:
                        errors.append(f"{path}: forbidden key {key!r}")
                    if key == "pii":
                        errors.append(f"{path}: forbidden key 'pii'; use 'phi'")
            if isinstance(value, str):
                low = value.lower()
                errors.extend(
                    f"{path}: forbidden text token {token!r}"
                    for token in FORBIDDEN_TEXT
                    if token in low
                )

        instructions = policy.get("instructions")
        if isinstance(instructions, list):
            allowed_instruction_keys = {"id", "text", "location"}
            for index, entry in enumerate(instructions):
                if not isinstance(entry, dict):
                    continue
                extra = set(entry.keys()) - allowed_instruction_keys
                if extra:
                    errors.append(
                        f"$.instructions[{index}]: forbidden keys {sorted(extra)!r} — "
                        f"policy instructions accept only {sorted(allowed_instruction_keys)!r}; "
                        f"gating belongs on per-variable skip_logic"
                    )

        errors.extend(_annotation_reconciliation(policy, source_pack))
        errors.extend(_validate_form_6_hiv_calibration(policy, source_pack))

    if "¹" in policy_text:
        errors.append("policy text contains unresolved superscript marker '¹'")

    if args.benchmark:
        benchmark = _load_yaml(args.benchmark)
        diffs = _compare(policy, benchmark)
        if diffs:
            errors.append("benchmark mismatch:")
            errors.extend(f"  {diff}" for diff in diffs[:80])
            if len(diffs) > 80:
                errors.append(f"  ... {len(diffs) - 80} more differences")

    if errors:
        print("Policy SoT checks failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return EXIT_VALIDATION_FAILURE

    print("Policy SoT checks passed")

    # ------------------------------------------------------------------
    # Property validator (1.2) — runs only after all existing checks pass
    # ------------------------------------------------------------------
    try:
        import importlib
        import importlib.util

        _spec = importlib.util.find_spec("scripts.ai_assistant.sot_loader")
        if _spec is None:
            raise ImportError("scripts.ai_assistant.sot_loader not importable")
        from scripts.ai_assistant.sot_loader import validate as _validate
    except ImportError as exc:
        print(f"Property validator import failed: {exc}", file=sys.stderr)
        return EXIT_SCRIPT_GAP

    report = _validate(policy)
    if not report.passed:
        print("Property validator failed:", file=sys.stderr)
        for ve in report.errors:
            print(f"- [{ve.code}] {ve.path}: {ve.message}", file=sys.stderr)
        return EXIT_VALIDATION_FAILURE

    print("Property validator passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
