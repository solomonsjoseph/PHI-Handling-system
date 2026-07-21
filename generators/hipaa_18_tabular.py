"""Deterministic USA HIPAA Safe Harbor A-R tabular corpus."""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import string
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List, Tuple

from .common import DeterministicGenerator, HIPAA_CATEGORIES
from .hipaa_safe_harbor import (
    biometric_reference,
    health_plan_beneficiary,
    ipv4,
    photo_reference,
    url,
    us_address,
    us_device_udi,
    us_fax,
    us_mrn,
    us_name,
    us_phone,
    us_ssn,
    us_vin,
)

_DATASET_NAME = "hipaa18"
_AUTHORITY_PREFIX = "45 CFR 164.514(b)(2)(i)"
_TEST_AUDIT_HMAC_KEY = b"synthetic-corpus-test-only-not-for-production"


@dataclass(frozen=True)
class IdentifierSpec:
    hipaa_category: str
    source_column: str
    canonical_variable: str
    variable_label: str
    data_type: str
    value_format: str
    hipaa_identifier: str
    entity_type: str
    expected_action: str
    rule_id: str

    @property
    def authority(self) -> str:
        return f"{_AUTHORITY_PREFIX}({self.hipaa_category})"


def _spec(
    category: str,
    column: str,
    label: str,
    data_type: str,
    value_format: str,
    entity_type: str,
    action: str = "drop",
) -> IdentifierSpec:
    return IdentifierSpec(
        hipaa_category=category,
        source_column=column,
        canonical_variable=column,
        variable_label=label,
        data_type=data_type,
        value_format=value_format,
        hipaa_identifier=HIPAA_CATEGORIES[category],
        entity_type=entity_type,
        expected_action=action,
        rule_id=f"US-HIPAA-{category}-001",
    )


HIPAA18_IDENTIFIER_SPECS: Tuple[IdentifierSpec, ...] = (
    _spec("A", "FULL_NAME", "Full name", "text", "given family", "NAME_PATIENT"),
    _spec("B", "STREET_ADDRESS", "Full street address", "text", "street, city, state ZIP", "ADDRESS_FULL"),
    _spec("C", "DATE_OF_BIRTH", "Date of birth", "date", "YYYY-MM-DD", "DATE_DOB", "retain_year"),
    _spec("D", "PHONE_NUMBER", "Telephone number", "text", "(AAA) NNN-NNNN", "PHONE"),
    _spec("E", "FAX_NUMBER", "Fax number", "text", "(AAA) NNN-NNNN", "FAX"),
    _spec("F", "EMAIL_ADDRESS", "Email address", "text", "local@domain", "EMAIL"),
    _spec("G", "SOCIAL_SECURITY_NUMBER", "Social security number", "text", "NNN-NN-NNNN", "SSN"),
    _spec("H", "MEDICAL_RECORD_NUMBER", "Medical record number", "text", "institution-defined", "MRN"),
    _spec("I", "HEALTH_PLAN_BENEFICIARY_NUMBER", "Health plan beneficiary number", "text", "plan-defined", "HEALTH_PLAN_ID"),
    _spec("J", "ACCOUNT_NUMBER", "Account number", "text", "10 digits", "ACCOUNT_NUMBER"),
    _spec("K", "CERTIFICATE_LICENSE_NUMBER", "Certificate or license number", "text", "state-defined", "LICENSE_NUMBER"),
    _spec("L", "VEHICLE_IDENTIFIER", "Vehicle identifier", "text", "17-character VIN", "VIN"),
    _spec("M", "DEVICE_IDENTIFIER", "Device identifier", "text", "GS1 UDI-DI", "DEVICE_UDI"),
    _spec("N", "WEB_URL", "Web URL", "text", "URI", "URL"),
    _spec("O", "IP_ADDRESS", "IP address", "text", "IPv4", "IP_V4"),
    _spec("P", "BIOMETRIC_IDENTIFIER", "Biometric identifier reference", "text", "template reference", "BIOMETRIC"),
    _spec("Q", "FULL_FACE_PHOTO", "Full-face photograph reference", "text", "attachment reference", "PHOTO_FULL_FACE"),
    _spec("R", "OTHER_UNIQUE_IDENTIFIER", "Other unique identifying code", "text", "internal code", "OTHER_UNIQUE_ID"),
)


@dataclass
class HIPAA18Corpus:
    dataset_rows: List[Dict[str, str]]
    dictionary_rows: List[Dict[str, str]]
    expected_user_rows: List[Dict[str, str]]
    audit_events: List[dict]
    gold_entries: List[dict]


class USHIPAA18TabularCorpusGenerator(DeterministicGenerator):
    """Generate a linked dataset, dictionary, user oracle, audit oracle, and gold ledger."""

    def _value(self, spec: IdentifierSpec, row_index: int) -> str:
        rng = self.fresh(f"hipaa18:{row_index}:{spec.hipaa_category}")
        category = spec.hipaa_category
        if category == "A":
            return us_name(rng)
        if category == "B":
            street, city, state, _zip3, zip_full = us_address(rng)
            return f"{street}, {city}, {state} {zip_full}"
        if category == "C":
            age = rng.randint(18, 89)
            return dt.date(2026 - age, rng.randint(1, 12), rng.randint(1, 28)).isoformat()
        if category == "D":
            return us_phone(rng)
        if category == "E":
            return us_fax(rng)
        if category == "F":
            return f"subject{rng.randint(1000, 9999)}@example.invalid"
        if category == "G":
            return us_ssn(rng)
        if category == "H":
            return us_mrn(rng)
        if category == "I":
            return health_plan_beneficiary(rng)
        if category == "J":
            return "".join(str(rng.randint(0, 9)) for _ in range(10))
        if category == "K":
            return "LIC-" + "".join(rng.choice(string.ascii_uppercase + string.digits) for _ in range(10))
        if category == "L":
            return us_vin(rng)
        if category == "M":
            return us_device_udi(rng)
        if category == "N":
            return url(rng)
        if category == "O":
            return ipv4(rng)
        if category == "P":
            return biometric_reference(rng)
        if category == "Q":
            return photo_reference(rng)
        return "UID-" + "".join(rng.choice(string.ascii_uppercase + string.digits) for _ in range(16))

    def _row_token(self, row_index: int) -> str:
        digest = hashlib.sha256(f"{self.seed}:hipaa18-row:{row_index}".encode()).hexdigest()
        return f"ROW_{digest[:12].upper()}"

    @staticmethod
    def _evidence_hmac(value: str) -> str:
        return hmac.new(_TEST_AUDIT_HMAC_KEY, value.encode(), hashlib.sha256).hexdigest()

    def generate(self, n_subjects: int = 18) -> HIPAA18Corpus:
        if n_subjects < 1:
            raise ValueError("n_subjects must be >= 1")

        dictionary_rows = [
            {
                "dataset_name": _DATASET_NAME,
                "source_column": spec.source_column,
                "canonical_variable": spec.canonical_variable,
                "variable_label": spec.variable_label,
                "data_type": spec.data_type,
                "format": spec.value_format,
                "required": "yes",
                "hipaa_category": spec.hipaa_category,
                "hipaa_identifier": spec.hipaa_identifier,
                "entity_type": spec.entity_type,
                "expected_action": spec.expected_action,
                "rule_id": spec.rule_id,
                "authority": spec.authority,
            }
            for spec in HIPAA18_IDENTIFIER_SPECS
        ]
        dataset_rows: List[Dict[str, str]] = []
        expected_user_rows: List[Dict[str, str]] = []
        audit_events: List[dict] = []
        gold_entries: List[dict] = []

        for row_index in range(n_subjects):
            row = {spec.source_column: self._value(spec, row_index) for spec in HIPAA18_IDENTIFIER_SPECS}
            row_token = self._row_token(row_index)
            expected = {"ROW_TOKEN": row_token}
            for spec in HIPAA18_IDENTIFIER_SPECS:
                value = row[spec.source_column]
                output_value = value[:4] if spec.expected_action == "retain_year" else ""
                expected[spec.source_column] = output_value
                gold_entries.append(
                    {
                        "dataset_name": _DATASET_NAME,
                        "row_index": row_index,
                        "column": spec.source_column,
                        "hipaa_category": spec.hipaa_category,
                        "original_value": value,
                        "expected_action": spec.expected_action,
                        "expected_value": output_value,
                    }
                )
                audit_events.append(
                    {
                        "event_id": f"evt-{row_index:06d}-{spec.hipaa_category}",
                        "row_token": row_token,
                        "dataset_name": _DATASET_NAME,
                        "row_index": row_index,
                        "source_column": spec.source_column,
                        "canonical_variable": spec.canonical_variable,
                        "hipaa_category": spec.hipaa_category,
                        "entity_type": spec.entity_type,
                        "what": {
                            "action": spec.expected_action,
                            "outcome": "generalized" if output_value else "removed",
                        },
                        "why": {
                            "rule_id": spec.rule_id,
                            "authority": spec.authority,
                            "reason": f"HIPAA Safe Harbor category {spec.hipaa_category}: {spec.hipaa_identifier}",
                        },
                        "how": {
                            "method": "year_only" if output_value else "blank_value",
                        },
                        "evidence": {
                            "input_hmac_sha256": self._evidence_hmac(value),
                            "output_hmac_sha256": self._evidence_hmac(output_value),
                        },
                    }
                )
            dataset_rows.append(row)
            expected_user_rows.append(expected)

        return HIPAA18Corpus(
            dataset_rows=dataset_rows,
            dictionary_rows=dictionary_rows,
            expected_user_rows=expected_user_rows,
            audit_events=audit_events,
            gold_entries=gold_entries,
        )


@dataclass(frozen=True, order=True)
class HIPAA18ValidationIssue:
    code: str
    column: str
    detail: str


def validate_corpus(corpus: HIPAA18Corpus) -> List[HIPAA18ValidationIssue]:
    """Validate the linked baseline model and return stable machine-readable issues."""
    issues: List[HIPAA18ValidationIssue] = []

    def add(code: str, column: str, detail: str) -> None:
        issues.append(HIPAA18ValidationIssue(code, column, detail))

    dataset_columns = list(corpus.dataset_rows[0]) if corpus.dataset_rows else []
    dataset_column_set = set(dataset_columns)
    dictionary_sources = [row["source_column"] for row in corpus.dictionary_rows]
    dictionary_source_set = set(dictionary_sources)

    for column in sorted(dataset_column_set - dictionary_source_set):
        add("UNMAPPED_DATASET_COLUMN", column, "dataset column has no dictionary mapping")
    for column in sorted(dictionary_source_set - dataset_column_set):
        add("ORPHAN_DICTIONARY_VARIABLE", column, "dictionary source column is absent from dataset")
    for column, count in sorted(Counter(dictionary_sources).items()):
        if count > 1:
            add("DUPLICATE_MAPPING", column, f"dictionary contains {count} mappings")

    mapped_rows = [
        row for row in corpus.dictionary_rows if row["source_column"] in dataset_column_set
    ]
    category_counts = Counter(row["hipaa_category"] for row in mapped_rows)
    for category in "ABCDEFGHIJKLMNOPQR":
        count = category_counts[category]
        if count == 0:
            add("MISSING_HIPAA_CATEGORY", category, "category has no mapped dataset column")
        elif count > 1:
            add("DUPLICATE_HIPAA_CATEGORY", category, f"category has {count} mappings")

    canonical_specs = {
        spec.canonical_variable: spec for spec in HIPAA18_IDENTIFIER_SPECS
    }
    for row in mapped_rows:
        expected = canonical_specs.get(row["canonical_variable"])
        if expected and row["hipaa_category"] != expected.hipaa_category:
            add(
                "CONFLICTING_HIPAA_CATEGORY",
                row["source_column"],
                f"expected {expected.hipaa_category}, got {row['hipaa_category']}",
            )

    for row_index, row in enumerate(corpus.dataset_rows):
        if list(row) != dataset_columns:
            add(
                "INVALID_LEDGER_REFERENCE",
                str(row_index),
                "dataset row header order differs from first row",
            )
        for mapping in mapped_rows:
            column = mapping["source_column"]
            if mapping["required"] == "yes" and not row.get(column, ""):
                add("EMPTY_REQUIRED_VALUE", column, f"row {row_index} is empty")

    mapping_by_source = {}
    for mapping in mapped_rows:
        mapping_by_source.setdefault(mapping["source_column"], mapping)
    if len(corpus.expected_user_rows) != len(corpus.dataset_rows):
        add(
            "INVALID_EXPECTED_OUTPUT",
            "ROW_TOKEN",
            "expected user row count differs from dataset row count",
        )
    else:
        for row_index, (source, output) in enumerate(
            zip(corpus.dataset_rows, corpus.expected_user_rows)
        ):
            for column, mapping in mapping_by_source.items():
                expected_value = (
                    source[column][:4]
                    if mapping["expected_action"] == "retain_year"
                    else ""
                )
                if output.get(column) != expected_value:
                    add(
                        "INVALID_EXPECTED_OUTPUT",
                        column,
                        f"row {row_index} does not match {mapping['expected_action']}",
                    )

    expected_cell_count = len(corpus.dataset_rows) * len(dataset_columns)
    if len(corpus.gold_entries) != expected_cell_count:
        add(
            "INVALID_LEDGER_REFERENCE",
            "gold",
            f"expected {expected_cell_count} entries, got {len(corpus.gold_entries)}",
        )
    for entry in corpus.gold_entries:
        try:
            actual = corpus.dataset_rows[entry["row_index"]][entry["column"]]
        except (IndexError, KeyError, TypeError):
            add("INVALID_LEDGER_REFERENCE", str(entry.get("column", "")), "gold entry cannot resolve")
            continue
        if actual != entry.get("original_value"):
            add("INVALID_LEDGER_REFERENCE", entry["column"], "gold value differs from dataset cell")

    if len(corpus.audit_events) != expected_cell_count:
        add(
            "INVALID_LEDGER_REFERENCE",
            "audit",
            f"expected {expected_cell_count} events, got {len(corpus.audit_events)}",
        )
    audit_text = str(corpus.audit_events)
    for row in corpus.dataset_rows:
        for column, value in row.items():
            if value and value in audit_text:
                add("PLAINTEXT_AUDIT_VALUE", column, "audit output contains plaintext input")

    return sorted(issues)
__all__ = [
    "HIPAA18_IDENTIFIER_SPECS",
    "HIPAA18Corpus",
    "HIPAA18ValidationIssue",
    "IdentifierSpec",
    "USHIPAA18TabularCorpusGenerator",
    "validate_corpus",
]
