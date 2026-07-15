from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Mapping
from phi_engine.security.phi_review import Action, RuleBundle, normalize_header
from phi_engine.sot.study_intake import leading_form_code

CODE_TABLE_VERSION = 1
DEPENDENCY_RECOMMENDATIONS_FILENAME = "dependency_recommendations.jsonl"
PRIVATE_DEPENDENCY_RECOMMENDATIONS_RELATIVE_PATH = Path(".protected") / "dependency_recommendations.jsonl"
DEPENDENCY_DECISIONS_FILENAME = "dependency_decisions.jsonl"
_ARTIFACT_ID_RE = re.compile(r"^a_[0-9a-f]{32}$")
_HEADER_ID_RE = re.compile(r"^h_[0-9a-f]{24}$")
_RECOMMENDATION_ID_RE = re.compile(r"^dr_[0-9a-f]{32}$")
_DECISION_ID_RE = re.compile(r"^dd_[0-9a-f]{32}$")
_REQUIREMENT_ID_RE = re.compile(r"^tr_[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_DECIDED_BY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$")


class DependencyKind(str, Enum):
    PDF = "pdf"
    DICTIONARY = "dictionary"
    MAPPING = "mapping"


class DependencyLevel(str, Enum):
    REQUIRED = "required"
    HELPFUL = "helpful"
    IGNORED = "ignored"


class Sensitivity(str, Enum):
    CONFIDENTIAL = "confidential"
    NON_CONFIDENTIAL = "non_confidential"


class RoleSource(str, Enum):
    MANIFEST = "manifest"
    DIRECTORY = "directory"
    INFERRED = "inferred"


class DependencyReasonCode(str, Enum):
    MANIFEST_DECLARED = "manifest_declared"
    SAME_STEM_COMPANION = "same_stem_companion"
    EXACT_HEADER_MATCH = "exact_header_match"
    ONLY_INTERPRETATION = "only_interpretation"
    TRANSFORM_PARAMETERS_MISSING = "transform_parameters_missing"


class SupportFailureCode(str, Enum):
    MISSING = "missing"
    HASH_MISMATCH = "hash_mismatch"
    SOURCE_CHANGED_DURING_READ = "source_changed_during_read"
    UNSUPPORTED_FORMAT = "unsupported_format"
    SOURCE_SIZE_LIMIT = "source_size_limit"
    EXPANDED_SIZE_LIMIT = "expanded_size_limit"
    DECOMPRESSION_RATIO_LIMIT = "decompression_ratio_limit"
    SHEET_LIMIT = "sheet_limit"
    TABLE_LIMIT = "table_limit"
    ROW_LIMIT = "row_limit"
    COLUMN_LIMIT = "column_limit"
    CELL_SIZE_LIMIT = "cell_size_limit"
    JSON_DEPTH_LIMIT = "json_depth_limit"
    PARSE_ERROR = "parse_error"
    NORMALIZED_SCHEMA_INVALID = "normalized_schema_invalid"
    MODEL_UNAVAILABLE = "model_unavailable"
    MODEL_INVALID = "model_invalid"
    SIGNAL_CONFLICT = "signal_conflict"
    STALE_DECISION = "stale_decision"
    RESIDUAL_GATE_FAILED = "residual_gate_failed"


class SupportParseStatus(str, Enum):
    PARSED = "parsed"
    FAILED = "failed"


class StructuredTransformKind(str, Enum):
    CAP = "cap"
    GENERALIZE = "generalize"
    BAND = "band"
    SUPPRESS_SMALL_CELL = "suppress_small_cell"


class TransformRequirementOrigin(str, Enum):
    RULE_CLASSIFICATION = "rule_classification"
    EFFECTIVE_CONFIG = "effective_config"
    CONFIRMED_DEPENDENCY = "confirmed_dependency"

@dataclass(frozen=True)
class DependencyDecisionBasis:
    rulebook_sha256: str
    scrub_config_sha256: str
    support_role_sha256: str

    def __post_init__(self) -> None:
        _require_sha256(self.rulebook_sha256, "rulebook_sha256")
        _require_sha256(self.scrub_config_sha256, "scrub_config_sha256")
        _require_sha256(self.support_role_sha256, "support_role_sha256")

    def to_json(self) -> dict[str, str]:
        return {
            "rulebook_sha256": self.rulebook_sha256,
            "scrub_config_sha256": self.scrub_config_sha256,
            "support_role_sha256": self.support_role_sha256,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any] | str | bytes) -> "DependencyDecisionBasis":
        data = _coerce_mapping(payload, "basis")
        _require_exact_keys(data, {"rulebook_sha256", "scrub_config_sha256", "support_role_sha256"}, "basis")
        return cls(
            rulebook_sha256=_str(data["rulebook_sha256"], "rulebook_sha256"),
            scrub_config_sha256=_str(data["scrub_config_sha256"], "scrub_config_sha256"),
            support_role_sha256=_str(data["support_role_sha256"], "support_role_sha256"),
        )


DatasetDependencyBasis = DependencyDecisionBasis


@dataclass(frozen=True)
class DatasetDependency:
    dataset_path: str
    dataset_artifact_id: str
    dataset_source_sha256: str
    support: str
    support_artifact_id: str | None
    support_source_sha256: str | None
    kind: DependencyKind
    level: DependencyLevel
    sensitivity: Sensitivity
    reason_code: DependencyReasonCode
    recommendation_id: str
    basis: DependencyDecisionBasis
    confirmed_by: str
    confirmed_at: str

    def __post_init__(self) -> None:
        _require_artifact_id(self.dataset_artifact_id, "dataset_artifact_id")
        _require_sha256(self.dataset_source_sha256, "dataset_source_sha256")
        if self.support_artifact_id is not None:
            _require_artifact_id(self.support_artifact_id, "support_artifact_id")
            _require_sha256(self.support_source_sha256, "support_source_sha256")
        elif self.support_source_sha256 is not None:
            raise ValueError("support artifact id/hash must both be null or both concrete")
        _require_recommendation_id(self.recommendation_id, "recommendation_id")
        _require_timestamp_z(self.confirmed_at, "confirmed_at")


@dataclass(frozen=True)
class OrganizedHeader:
    header_id: str
    column_index: int
    raw_name: str
    normalized_name: str

    def __post_init__(self) -> None:
        _require_header_id(self.header_id, "header_id")
        if not isinstance(self.column_index, int) or self.column_index < 0:
            raise ValueError("column_index must be a non-negative integer")
        if not isinstance(self.raw_name, str) or not isinstance(self.normalized_name, str):
            raise ValueError("header names must be strings")


@dataclass(frozen=True)
class OrganizedDataset:
    artifact_id: str
    source_sha256: str
    normalized_rows_path: Path
    normalized_rows_sha256: str
    headers: tuple[OrganizedHeader, ...]

    def __post_init__(self) -> None:
        _require_artifact_id(self.artifact_id, "artifact_id")
        _require_sha256(self.source_sha256, "source_sha256")
        _require_sha256(self.normalized_rows_sha256, "normalized_rows_sha256")
        object.__setattr__(self, "headers", tuple(self.headers))


@dataclass(frozen=True)
class ParsedSupportArtifact:
    artifact_id: str
    source_sha256: str
    kind: DependencyKind
    format: str
    parse_status: SupportParseStatus
    normalized_rows_path: Path | None
    normalized_rows_sha256: str | None
    failure_code: SupportFailureCode | None

    def __post_init__(self) -> None:
        _require_artifact_id(self.artifact_id, "artifact_id")
        _require_sha256(self.source_sha256, "source_sha256")
        _require_enum(self.kind, DependencyKind, "kind")
        _require_enum(self.parse_status, SupportParseStatus, "parse_status")
        if self.parse_status is SupportParseStatus.PARSED:
            if self.normalized_rows_path is None or self.normalized_rows_sha256 is None:
                raise ValueError("parsed support requires normalized rows path and hash")
            _require_sha256(self.normalized_rows_sha256, "normalized_rows_sha256")
            if self.failure_code is not None:
                raise ValueError("parsed support failure_code must be null")
        else:
            if self.normalized_rows_path is not None or self.normalized_rows_sha256 is not None:
                raise ValueError("failed support normalized rows path and hash must be null")
            if self.failure_code is None:
                raise ValueError("failed support requires failure_code")
            _require_enum(self.failure_code, SupportFailureCode, "failure_code")


def load_protected_support_artifacts(organized_root: Path) -> tuple[ParsedSupportArtifact, ...]:
    protected_dir = Path(organized_root) / ".protected" / "support"
    if not protected_dir.is_dir():
        return ()
    artifacts: list[ParsedSupportArtifact] = []
    for path in sorted(protected_dir.glob("*.json")):
        payload = _coerce_mapping(path.read_text(encoding="utf-8"), "protected support metadata")
        artifacts.append(
            ParsedSupportArtifact(
                artifact_id=_str(payload.get("artifact_id"), "artifact_id"),
                source_sha256=_str(payload.get("source_sha256"), "source_sha256"),
                kind=DependencyKind(payload.get("kind")),
                format=_str(payload.get("format"), "format"),
                parse_status=SupportParseStatus(payload.get("parse_status")),
                normalized_rows_path=Path(_str(payload["normalized_rows_path"], "normalized_rows_path"))
                if payload.get("normalized_rows_path") is not None
                else None,
                normalized_rows_sha256=_nullable_str(payload.get("normalized_rows_sha256"), "normalized_rows_sha256"),
                failure_code=SupportFailureCode(payload["failure_code"]) if payload.get("failure_code") is not None else None,
            )
        )
    return tuple(artifacts)


@dataclass(frozen=True)
class TransformRequirement:
    requirement_id: str
    dataset_artifact_id: str
    dataset_sha256: str
    header_id: str
    classification_action: Action
    kind: StructuredTransformKind
    origin: TransformRequirementOrigin
    origin_rule_id: str | None
    required_support_kind: DependencyKind | None

    def __post_init__(self) -> None:
        _require_requirement_id(self.requirement_id, "requirement_id")
        _require_artifact_id(self.dataset_artifact_id, "dataset_artifact_id")
        _require_sha256(self.dataset_sha256, "dataset_sha256")
        _require_header_id(self.header_id, "header_id")
        _require_enum(self.classification_action, Action, "classification_action")
        _require_enum(self.kind, StructuredTransformKind, "kind")
        _require_enum(self.origin, TransformRequirementOrigin, "origin")
        if self.origin_rule_id is not None and (not isinstance(self.origin_rule_id, str) or not self.origin_rule_id):
            raise ValueError("origin_rule_id must be a non-empty string or null")
        if self.required_support_kind is not None:
            _require_enum(self.required_support_kind, DependencyKind, "required_support_kind")


@dataclass(frozen=True)
class DependencyRecommendation:
    schema_version: Literal["dependency-recommendation/v1"]
    recommendation_id: str
    dataset_artifact_id: str
    dataset_sha256: str
    support_artifact_id: str | None
    support_sha256: str | None
    normalized_support_sha256: str | None
    kind: DependencyKind
    suggested_level: DependencyLevel
    default_sensitivity: Sensitivity
    reason_code: DependencyReasonCode
    header_ids: tuple[str, ...]
    matched_rule_ids: tuple[str, ...]
    transform_requirement_ids: tuple[str, ...]
    basis: DependencyDecisionBasis

    def __post_init__(self) -> None:
        if self.schema_version != "dependency-recommendation/v1":
            raise ValueError("schema_version must be dependency-recommendation/v1")
        _require_recommendation_id(self.recommendation_id, "recommendation_id")
        _require_artifact_id(self.dataset_artifact_id, "dataset_artifact_id")
        _require_sha256(self.dataset_sha256, "dataset_sha256")
        _validate_support_fields(self.support_artifact_id, self.support_sha256, self.normalized_support_sha256)
        _require_enum(self.kind, DependencyKind, "kind")
        _require_enum(self.suggested_level, DependencyLevel, "suggested_level")
        _require_enum(self.default_sensitivity, Sensitivity, "default_sensitivity")
        _require_enum(self.reason_code, DependencyReasonCode, "reason_code")
        object.__setattr__(self, "header_ids", _validated_tuple(self.header_ids, _require_header_id, "header_ids"))
        object.__setattr__(self, "matched_rule_ids", _string_tuple(self.matched_rule_ids, "matched_rule_ids"))
        object.__setattr__(self, "transform_requirement_ids", _validated_tuple(self.transform_requirement_ids, _require_requirement_id, "transform_requirement_ids"))
        if not isinstance(self.basis, DependencyDecisionBasis):
            raise ValueError("basis must be DependencyDecisionBasis")

    def to_json(self) -> dict[str, Any]:
        return _append_code_table({
            "schema_version": self.schema_version,
            "recommendation_id": self.recommendation_id,
            "dataset_artifact_id": self.dataset_artifact_id,
            "dataset_sha256": self.dataset_sha256,
            "support_artifact_id": self.support_artifact_id,
            "support_sha256": self.support_sha256,
            "normalized_support_sha256": self.normalized_support_sha256,
            "kind": self.kind.value,
            "suggested_level": self.suggested_level.value,
            "default_sensitivity": self.default_sensitivity.value,
            "reason_code": self.reason_code.value,
            "header_ids": list(self.header_ids),
            "matched_rule_ids": list(self.matched_rule_ids),
            "transform_requirement_ids": list(self.transform_requirement_ids),
            "basis": self.basis.to_json(),
        })

    @classmethod
    def from_json(cls, payload: Mapping[str, Any] | str | bytes) -> "DependencyRecommendation":
        data = _coerce_mapping(payload, "DependencyRecommendation")
        _require_exact_keys(data, _RECOMMENDATION_KEYS | {"code_table_version"}, "DependencyRecommendation")
        _require_code_table(data)
        return cls(
            schema_version=_literal(data["schema_version"], "dependency-recommendation/v1", "schema_version"),
            recommendation_id=_str(data["recommendation_id"], "recommendation_id"),
            dataset_artifact_id=_str(data["dataset_artifact_id"], "dataset_artifact_id"),
            dataset_sha256=_str(data["dataset_sha256"], "dataset_sha256"),
            support_artifact_id=_nullable_str(data["support_artifact_id"], "support_artifact_id"),
            support_sha256=_nullable_str(data["support_sha256"], "support_sha256"),
            normalized_support_sha256=_nullable_str(data["normalized_support_sha256"], "normalized_support_sha256"),
            kind=DependencyKind(data["kind"]),
            suggested_level=DependencyLevel(data["suggested_level"]),
            default_sensitivity=Sensitivity(data["default_sensitivity"]),
            reason_code=DependencyReasonCode(data["reason_code"]),
            header_ids=tuple(_list(data["header_ids"], "header_ids")),
            matched_rule_ids=tuple(_list(data["matched_rule_ids"], "matched_rule_ids")),
            transform_requirement_ids=tuple(_list(data["transform_requirement_ids"], "transform_requirement_ids")),
            basis=DependencyDecisionBasis.from_json(_mapping(data["basis"], "basis")),
        )


@dataclass(frozen=True)
class PrivateDependencyRecommendation:
    schema_version: Literal["dependency-recommendation-private/v1"]
    recommendation_id: str
    dataset_artifact_id: str
    dataset_path: str
    support_artifact_id: str | None
    support_path: str | None
    raw_header_names: tuple[str, ...]
    role_source: RoleSource
    organizer_role_version: int
    basis: DependencyDecisionBasis

    def __post_init__(self) -> None:
        if self.schema_version != "dependency-recommendation-private/v1":
            raise ValueError("schema_version must be dependency-recommendation-private/v1")
        _require_recommendation_id(self.recommendation_id, "recommendation_id")
        _require_artifact_id(self.dataset_artifact_id, "dataset_artifact_id")
        _require_safe_relative_path(self.dataset_path, "dataset_path")
        if self.support_artifact_id is not None:
            _require_artifact_id(self.support_artifact_id, "support_artifact_id")
            if self.support_path is None:
                raise ValueError("support_path is required for concrete support")
        if self.support_path is not None:
            _require_safe_relative_path(self.support_path, "support_path")
        object.__setattr__(self, "raw_header_names", _string_tuple(self.raw_header_names, "raw_header_names"))
        _require_enum(self.role_source, RoleSource, "role_source")
        if not isinstance(self.organizer_role_version, int) or isinstance(self.organizer_role_version, bool) or self.organizer_role_version < 1:
            raise ValueError("organizer_role_version must be a positive integer")
        if not isinstance(self.basis, DependencyDecisionBasis):
            raise ValueError("basis must be DependencyDecisionBasis")

    def to_json(self) -> dict[str, Any]:
        return _append_code_table({
            "schema_version": self.schema_version,
            "recommendation_id": self.recommendation_id,
            "dataset_artifact_id": self.dataset_artifact_id,
            "dataset_path": self.dataset_path,
            "support_artifact_id": self.support_artifact_id,
            "support_path": self.support_path,
            "raw_header_names": list(self.raw_header_names),
            "role_source": self.role_source.value,
            "organizer_role_version": self.organizer_role_version,
            "basis": self.basis.to_json(),
        })

    @classmethod
    def from_json(cls, payload: Mapping[str, Any] | str | bytes) -> "PrivateDependencyRecommendation":
        data = _coerce_mapping(payload, "PrivateDependencyRecommendation")
        _require_exact_keys(
            data,
            _PRIVATE_RECOMMENDATION_KEYS | {"code_table_version"},
            "PrivateDependencyRecommendation",
        )
        _require_code_table(data)
        return cls(
            schema_version=_literal(
                data["schema_version"],
                "dependency-recommendation-private/v1",
                "schema_version",
            ),
            recommendation_id=_str(data["recommendation_id"], "recommendation_id"),
            dataset_artifact_id=_str(data["dataset_artifact_id"], "dataset_artifact_id"),
            dataset_path=_str(data["dataset_path"], "dataset_path"),
            support_artifact_id=_nullable_str(data["support_artifact_id"], "support_artifact_id"),
            support_path=_nullable_str(data["support_path"], "support_path"),
            raw_header_names=tuple(_list(data["raw_header_names"], "raw_header_names")),
            role_source=RoleSource(data["role_source"]),
            organizer_role_version=_int(data["organizer_role_version"], "organizer_role_version"),
            basis=DependencyDecisionBasis.from_json(_mapping(data["basis"], "basis")),
        )


@dataclass(frozen=True)
class DependencyDecision:
    schema_version: Literal["dependency-decision/v1"]
    decision_id: str
    recommendation_id: str
    dataset_artifact_id: str
    dataset_sha256: str
    support_artifact_id: str | None
    support_sha256: str | None
    normalized_support_sha256: str | None
    kind: DependencyKind
    level: DependencyLevel
    sensitivity: Sensitivity
    reason_code: DependencyReasonCode
    basis: DependencyDecisionBasis
    decided_by: str
    decided_at: str

    def __post_init__(self) -> None:
        if self.schema_version != "dependency-decision/v1":
            raise ValueError("schema_version must be dependency-decision/v1")
        _require_decision_id(self.decision_id, "decision_id")
        _require_recommendation_id(self.recommendation_id, "recommendation_id")
        _require_artifact_id(self.dataset_artifact_id, "dataset_artifact_id")
        _require_sha256(self.dataset_sha256, "dataset_sha256")
        _validate_support_fields(self.support_artifact_id, self.support_sha256, self.normalized_support_sha256)
        _require_enum(self.kind, DependencyKind, "kind")
        _require_enum(self.level, DependencyLevel, "level")
        _require_enum(self.sensitivity, Sensitivity, "sensitivity")
        _require_enum(self.reason_code, DependencyReasonCode, "reason_code")
        if not isinstance(self.basis, DependencyDecisionBasis):
            raise ValueError("basis must be DependencyDecisionBasis")
        _require_decided_by_identifier(self.decided_by)
        if (
            self.sensitivity is Sensitivity.NON_CONFIDENTIAL
            and (
                self.support_artifact_id is None
                or self.normalized_support_sha256 is None
            )
        ):
            raise ValueError("non_confidential sensitivity requires parsed support")
        _require_timestamp_z(self.decided_at, "decided_at")

    def to_json(self) -> dict[str, Any]:
        return _append_code_table({
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "recommendation_id": self.recommendation_id,
            "dataset_artifact_id": self.dataset_artifact_id,
            "dataset_sha256": self.dataset_sha256,
            "support_artifact_id": self.support_artifact_id,
            "support_sha256": self.support_sha256,
            "normalized_support_sha256": self.normalized_support_sha256,
            "kind": self.kind.value,
            "level": self.level.value,
            "sensitivity": self.sensitivity.value,
            "reason_code": self.reason_code.value,
            "basis": self.basis.to_json(),
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
        })

    @classmethod
    def from_json(cls, payload: Mapping[str, Any] | str | bytes) -> "DependencyDecision":
        data = _coerce_mapping(payload, "DependencyDecision")
        _require_exact_keys(data, _DECISION_KEYS | {"code_table_version"}, "DependencyDecision")
        _require_code_table(data)
        return cls(
            schema_version=_literal(data["schema_version"], "dependency-decision/v1", "schema_version"),
            decision_id=_str(data["decision_id"], "decision_id"),
            recommendation_id=_str(data["recommendation_id"], "recommendation_id"),
            dataset_artifact_id=_str(data["dataset_artifact_id"], "dataset_artifact_id"),
            dataset_sha256=_str(data["dataset_sha256"], "dataset_sha256"),
            support_artifact_id=_nullable_str(data["support_artifact_id"], "support_artifact_id"),
            support_sha256=_nullable_str(data["support_sha256"], "support_sha256"),
            normalized_support_sha256=_nullable_str(data["normalized_support_sha256"], "normalized_support_sha256"),
            kind=DependencyKind(data["kind"]),
            level=DependencyLevel(data["level"]),
            sensitivity=Sensitivity(data["sensitivity"]),
            reason_code=DependencyReasonCode(data["reason_code"]),
            basis=DependencyDecisionBasis.from_json(_mapping(data["basis"], "basis")),
            decided_by=_str(data["decided_by"], "decided_by"),
            decided_at=_str(data["decided_at"], "decided_at"),
        )


_RECOMMENDATION_KEYS = {
    "schema_version", "recommendation_id", "dataset_artifact_id", "dataset_sha256",
    "support_artifact_id", "support_sha256", "normalized_support_sha256", "kind",
    "suggested_level", "default_sensitivity", "reason_code", "header_ids",
    "matched_rule_ids", "transform_requirement_ids", "basis",
}
_PRIVATE_RECOMMENDATION_KEYS = {
    "schema_version", "recommendation_id", "dataset_artifact_id", "dataset_path",
    "support_artifact_id", "support_path", "raw_header_names", "role_source",
    "organizer_role_version", "basis",
}
_DECISION_KEYS = {
    "schema_version", "decision_id", "recommendation_id", "dataset_artifact_id", "dataset_sha256",
    "support_artifact_id", "support_sha256", "normalized_support_sha256", "kind",
    "level", "sensitivity", "reason_code", "basis", "decided_by", "decided_at",
}


def is_artifact_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ARTIFACT_ID_RE.fullmatch(value))


def is_header_id(value: object) -> bool:
    return isinstance(value, str) and bool(_HEADER_ID_RE.fullmatch(value))


def is_recommendation_id(value: object) -> bool:
    return isinstance(value, str) and bool(_RECOMMENDATION_ID_RE.fullmatch(value))


def is_decision_id(value: object) -> bool:
    return isinstance(value, str) and bool(_DECISION_ID_RE.fullmatch(value))


def is_requirement_id(value: object) -> bool:
    return isinstance(value, str) and bool(_REQUIREMENT_ID_RE.fullmatch(value))


def is_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA256_RE.fullmatch(value))


def is_timestamp_z(value: object) -> bool:
    return isinstance(value, str) and bool(_TIMESTAMP_RE.fullmatch(value)) and _parse_timestamp(value) is not None


def utc_now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def support_role_sha256(
    *,
    recommendation_id: str,
    dataset_artifact_id: str,
    support_artifact_id: str | None,
    kind: DependencyKind,
    role_source: RoleSource,
    organizer_role_version: int,
) -> str:
    _require_recommendation_id(recommendation_id, "recommendation_id")
    _require_artifact_id(dataset_artifact_id, "dataset_artifact_id")
    if support_artifact_id is not None:
        _require_artifact_id(support_artifact_id, "support_artifact_id")
    return canonical_sha256(
        {
            "recommendation_id": recommendation_id,
            "dataset_artifact_id": dataset_artifact_id,
            "support_artifact_id": support_artifact_id,
            "kind": DependencyKind(kind).value,
            "role_source": RoleSource(role_source).value,
            "organizer_role_version": organizer_role_version,
        }
    )


def support_evidence_sha256(records: tuple[Mapping[str, Any], ...], parameter_hashes: tuple[str, ...]) -> str:
    for parameter_hash in parameter_hashes:
        _require_sha256(parameter_hash, "parameter_hash")
    sorted_records = sorted((dict(record) for record in records), key=lambda r: json.dumps(r, sort_keys=True, separators=(",", ":")))
    return canonical_sha256({"records": sorted_records, "parameter_hashes": sorted(parameter_hashes)})


def sot_projection_sha256(projection_bytes: bytes) -> str:
    if not isinstance(projection_bytes, bytes):
        raise ValueError("projection_bytes must be bytes")
    return hashlib.sha256(projection_bytes).hexdigest()


def load_dependency_recommendations(path: Path) -> tuple[DependencyRecommendation, ...]:
    return tuple(
        DependencyRecommendation.from_json(line)
        for line in _load_jsonl_lines(path, "dependency recommendations")
    )


def load_private_dependency_recommendations(path: Path) -> tuple[PrivateDependencyRecommendation, ...]:
    _require_private_mode(path, "private dependency recommendations")
    return tuple(
        PrivateDependencyRecommendation.from_json(line)
        for line in _load_jsonl_lines(path, "private dependency recommendations")
    )


def load_dependency_decisions(path: Path) -> tuple[DependencyDecision, ...]:
    path = Path(path)
    if not path.exists():
        return ()
    return tuple(
        DependencyDecision.from_json(line)
        for line in _load_jsonl_lines(path, "dependency decisions")
    )


def write_dependency_recommendations(
    *,
    run_dir: Path,
    recommendations: tuple[DependencyRecommendation, ...],
    private_records: tuple[PrivateDependencyRecommendation, ...],
) -> tuple[Path, Path]:
    run_dir = Path(run_dir)
    recommendations_by_id = _unique_by_recommendation_id(recommendations, "recommendations")
    private_by_id = _unique_by_recommendation_id(private_records, "private recommendation records")
    if set(recommendations_by_id) != set(private_by_id):
        raise ValueError("ordinary/private recommendation identities mismatch")
    for recommendation_id, recommendation in recommendations_by_id.items():
        private = private_by_id[recommendation_id]
        if (
            private.dataset_artifact_id != recommendation.dataset_artifact_id
            or private.support_artifact_id != recommendation.support_artifact_id
            or private.basis != recommendation.basis
            or len(private.raw_header_names) != len(recommendation.header_ids)
        ):
            raise ValueError("ordinary/private recommendation identity mismatch")

    ordinary_path = run_dir / DEPENDENCY_RECOMMENDATIONS_FILENAME
    private_path = run_dir / PRIVATE_DEPENDENCY_RECOMMENDATIONS_RELATIVE_PATH
    private_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_path.parent.chmod(0o700)
    _atomic_write_jsonl(
        ordinary_path,
        (record.to_json() for record in recommendations),
        mode=0o600,
    )
    _atomic_write_jsonl(
        private_path,
        (record.to_json() for record in private_records),
        mode=0o600,
    )
    return ordinary_path, private_path


def append_dependency_decision(path: Path, decision: DependencyDecision) -> Path:
    if not isinstance(decision, DependencyDecision):
        raise ValueError("decision must be DependencyDecision")
    return _append_jsonl_record(Path(path), decision.to_json(), mode=0o600)


def _effective_scrub_config_sha256() -> str:
    from phi_engine.security import phi_scrub

    value = phi_scrub.effective_scrub_config_hash()
    if value is None:
        raise ValueError("effective scrub config hash is unavailable")
    _require_sha256(value, "scrub_config_sha256")
    return value


def recommendation_identity(
    *,
    dataset_artifact_id: str,
    support_artifact_id: str | None,
    kind: DependencyKind,
    reason_code: DependencyReasonCode,
    header_ids: tuple[str, ...],
    transform_requirement_ids: tuple[str, ...],
) -> str:
    _require_artifact_id(dataset_artifact_id, "dataset_artifact_id")
    if support_artifact_id is not None:
        _require_artifact_id(support_artifact_id, "support_artifact_id")
    for header_id in header_ids:
        _require_header_id(header_id, "header_ids")
    for requirement_id in transform_requirement_ids:
        _require_requirement_id(requirement_id, "transform_requirement_ids")
    return "dr_" + canonical_sha256(
        {
            "dataset_artifact_id": dataset_artifact_id,
            "support_artifact_id": support_artifact_id,
            "kind": DependencyKind(kind).value,
            "reason_code": DependencyReasonCode(reason_code).value,
            "header_ids": sorted(header_ids),
            "transform_requirement_ids": sorted(transform_requirement_ids),
        }
    )[:32]


def recommend_dependencies(
    *,
    datasets: tuple[OrganizedDataset, ...],
    support_artifacts: tuple[ParsedSupportArtifact, ...],
    published_raw_headers_by_dataset: Mapping[str, frozenset[str]],
    transform_requirements_by_dataset: Mapping[str, tuple[TransformRequirement, ...]],
    confirmed_links: tuple[DependencyDecision, ...],
    rule_bundle: RuleBundle,
) -> tuple[DependencyRecommendation, ...]:
    scrub_config_sha256 = _effective_scrub_config_sha256()
    rulebook_sha256 = _str(getattr(rule_bundle, "rules_sha256", None), "rule_bundle.rules_sha256")
    _require_sha256(rulebook_sha256, "rulebook_sha256")

    support_by_id = {support.artifact_id: support for support in support_artifacts}
    support_names = _normalized_support_names(support_artifacts)
    support_by_stem = _supports_by_stem(support_artifacts)
    support_by_form_code = _supports_by_form_code(support_artifacts)
    decisions_by_recommendation = {
        decision.recommendation_id: decision
        for decision in confirmed_links
    }
    suppressed_manifest_roles: set[tuple[str, str, DependencyKind]] = set()

    output: dict[str, DependencyRecommendation] = {}
    for dataset in datasets:
        raw_header_ids = published_raw_headers_by_dataset.get(dataset.artifact_id, frozenset())

        for decision in confirmed_links:
            if decision.dataset_artifact_id != dataset.artifact_id:
                continue
            support = support_by_id.get(decision.support_artifact_id or "")
            if support is None:
                continue
            rec = _build_recommendation(
                dataset=dataset,
                support=support,
                kind=decision.kind,
                level=(
                    DependencyLevel.HELPFUL
                    if decision.level is DependencyLevel.IGNORED
                    else decision.level
                ),
                sensitivity=decision.sensitivity,
                reason_code=DependencyReasonCode.MANIFEST_DECLARED,
                header_ids=(),
                matched_rule_ids=(),
                transform_requirement_ids=(),
                rulebook_sha256=rulebook_sha256,
                scrub_config_sha256=scrub_config_sha256,
                role_source=RoleSource.MANIFEST,
            )
            decision_is_current = _decision_matches_recommendation(decision, rec)
            role = (dataset.artifact_id, support.artifact_id, support.kind)
            if decision.level is DependencyLevel.IGNORED:
                if decision_is_current:
                    suppressed_manifest_roles.add(role)
                if (
                    decision_is_current
                    or decision.reason_code is not DependencyReasonCode.MANIFEST_DECLARED
                ):
                    continue
            else:
                # A persisted decision establishes this dataset/support/kind
                # as a manifest role even when its original recommendation
                # identity came from inference.  The canonical manifest item
                # below replaces that inferred item for review.
                suppressed_manifest_roles.add(role)
            if decision_is_current:
                suppressed_manifest_roles.add(role)
            else:
                rec = _build_recommendation(
                    dataset=dataset,
                    support=support,
                    kind=decision.kind,
                    level=(
                        DependencyLevel.REQUIRED
                        if decision.level is DependencyLevel.REQUIRED
                        else DependencyLevel.HELPFUL
                    ),
                    sensitivity=Sensitivity.CONFIDENTIAL,
                    reason_code=DependencyReasonCode.MANIFEST_DECLARED,
                    header_ids=(),
                    matched_rule_ids=(),
                    transform_requirement_ids=(),
                    rulebook_sha256=rulebook_sha256,
                    scrub_config_sha256=scrub_config_sha256,
                    role_source=RoleSource.MANIFEST,
                )
            _merge_recommendation(output, rec)

        for support in _matching_pdf_supports(
            dataset,
            support_by_stem,
            support_by_form_code,
        ):
            if (dataset.artifact_id, support.artifact_id, support.kind) in suppressed_manifest_roles:
                continue
            rec = _build_recommendation(
                dataset=dataset,
                support=support,
                kind=support.kind,
                level=DependencyLevel.HELPFUL,
                sensitivity=Sensitivity.CONFIDENTIAL,
                reason_code=DependencyReasonCode.SAME_STEM_COMPANION,
                header_ids=(),
                matched_rule_ids=(),
                transform_requirement_ids=(),
                rulebook_sha256=rulebook_sha256,
                scrub_config_sha256=scrub_config_sha256,
                role_source=RoleSource.INFERRED,
            )
            if _ignored_exact_recommendation(
                decisions_by_recommendation,
                rec,
            ):
                continue
            _merge_recommendation(output, rec)

        for header in dataset.headers:
            header_name = normalize_header(header.raw_name)
            if header.normalized_name:
                header_name = normalize_header(header.normalized_name)
            matches = support_names.get(header_name, ())
            if not matches:
                continue
            for support in matches:
                if (dataset.artifact_id, support.artifact_id, support.kind) in suppressed_manifest_roles:
                    continue
                rec = _build_recommendation(
                    dataset=dataset,
                    support=support,
                    kind=support.kind,
                    level=DependencyLevel.HELPFUL,
                    sensitivity=Sensitivity.CONFIDENTIAL,
                    reason_code=DependencyReasonCode.EXACT_HEADER_MATCH,
                    header_ids=(header.header_id,),
                    matched_rule_ids=(),
                    transform_requirement_ids=(),
                    rulebook_sha256=rulebook_sha256,
                    scrub_config_sha256=scrub_config_sha256,
                    role_source=RoleSource.INFERRED,
                )
                if _ignored_exact_recommendation(
                    decisions_by_recommendation,
                    rec,
                ):
                    continue
                _merge_recommendation(output, rec)

        for requirement in transform_requirements_by_dataset.get(dataset.artifact_id, ()):
            # A requirement whose parameters already resolve from the effective
            # scrub configuration remains an explicit deterministic input, but
            # it does not request external evidence.
            if requirement.required_support_kind is None:
                continue
            if requirement.dataset_artifact_id != dataset.artifact_id or requirement.dataset_sha256 != dataset.source_sha256:
                continue
            rec = _build_recommendation(
                dataset=dataset,
                support=None,
                kind=requirement.required_support_kind,
                level=DependencyLevel.REQUIRED,
                sensitivity=Sensitivity.CONFIDENTIAL,
                reason_code=DependencyReasonCode.TRANSFORM_PARAMETERS_MISSING,
                header_ids=(requirement.header_id,),
                matched_rule_ids=(requirement.origin_rule_id,) if requirement.origin_rule_id else (),
                transform_requirement_ids=(requirement.requirement_id,),
                rulebook_sha256=rulebook_sha256,
                scrub_config_sha256=scrub_config_sha256,
                role_source=RoleSource.INFERRED,
            )
            _merge_recommendation(output, rec)

    return tuple(sorted(output.values(), key=lambda item: (item.dataset_artifact_id, item.recommendation_id)))


def _build_recommendation(
    *,
    dataset: OrganizedDataset,
    support: ParsedSupportArtifact | None,
    kind: DependencyKind,
    level: DependencyLevel,
    sensitivity: Sensitivity,
    reason_code: DependencyReasonCode,
    header_ids: tuple[str, ...],
    matched_rule_ids: tuple[str, ...],
    transform_requirement_ids: tuple[str, ...],
    rulebook_sha256: str,
    scrub_config_sha256: str,
    role_source: RoleSource,
) -> DependencyRecommendation:
    support_id = support.artifact_id if support is not None else None
    recommendation_id = recommendation_identity(
        dataset_artifact_id=dataset.artifact_id,
        support_artifact_id=support_id,
        kind=kind,
        reason_code=reason_code,
        header_ids=header_ids,
        transform_requirement_ids=transform_requirement_ids,
    )
    basis = DependencyDecisionBasis(
        rulebook_sha256=rulebook_sha256,
        scrub_config_sha256=scrub_config_sha256,
        support_role_sha256=support_role_sha256(
            recommendation_id=recommendation_id,
            dataset_artifact_id=dataset.artifact_id,
            support_artifact_id=support_id,
            kind=kind,
            role_source=role_source,
            organizer_role_version=1,
        ),
    )
    return DependencyRecommendation(
        schema_version="dependency-recommendation/v1",
        recommendation_id=recommendation_id,
        dataset_artifact_id=dataset.artifact_id,
        dataset_sha256=dataset.source_sha256,
        support_artifact_id=support_id,
        support_sha256=support.source_sha256 if support is not None else None,
        normalized_support_sha256=support.normalized_rows_sha256 if support is not None else None,
        kind=kind,
        suggested_level=level,
        default_sensitivity=sensitivity,
        reason_code=reason_code,
        header_ids=tuple(sorted(header_ids)),
        matched_rule_ids=tuple(sorted(matched_rule_ids)),
        transform_requirement_ids=tuple(sorted(transform_requirement_ids)),
        basis=basis,
    )


def _merge_recommendation(
    output: dict[str, DependencyRecommendation],
    recommendation: DependencyRecommendation,
) -> None:
    current = output.get(recommendation.recommendation_id)
    if current is None:
        output[recommendation.recommendation_id] = recommendation
        return
    level = (
        DependencyLevel.REQUIRED
        if DependencyLevel.REQUIRED
        in (current.suggested_level, recommendation.suggested_level)
        else DependencyLevel.HELPFUL
    )
    sensitivity = (
        Sensitivity.CONFIDENTIAL
        if Sensitivity.CONFIDENTIAL
        in (current.default_sensitivity, recommendation.default_sensitivity)
        else Sensitivity.NON_CONFIDENTIAL
    )
    output[recommendation.recommendation_id] = replace(
        current,
        suggested_level=level,
        default_sensitivity=sensitivity,
    )


def _normalized_support_names(support_artifacts: tuple[ParsedSupportArtifact, ...]) -> dict[str, tuple[ParsedSupportArtifact, ...]]:
    result: dict[str, list[ParsedSupportArtifact]] = {}
    for support in support_artifacts:
        if support.parse_status is not SupportParseStatus.PARSED or support.normalized_rows_path is None:
            continue
        names: set[str] = set()
        for row in _read_normalized_support_rows(support):
            for cell in row.get("cells", ()):
                if isinstance(cell, Mapping):
                    name = normalize_header(str(cell.get("value", "")))
                    if name:
                        names.add(name)
        for name in names:
            result.setdefault(name, []).append(support)
    return {key: tuple(value) for key, value in result.items()}


def _read_normalized_support_rows(support: ParsedSupportArtifact) -> tuple[Mapping[str, Any], ...]:
    if support.normalized_rows_path is None:
        return ()
    rows: list[Mapping[str, Any]] = []
    for line in support.normalized_rows_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if isinstance(row, Mapping):
            rows.append(row)
    return tuple(rows)


def _supports_by_stem(support_artifacts: tuple[ParsedSupportArtifact, ...]) -> dict[str, tuple[ParsedSupportArtifact, ...]]:
    result: dict[str, list[ParsedSupportArtifact]] = {}
    for support in support_artifacts:
        if support.kind is DependencyKind.PDF and support.normalized_rows_path is not None:
            result.setdefault(_artifact_stem(support.normalized_rows_path), []).append(support)
    return {key: tuple(value) for key, value in result.items()}


def _supports_by_form_code(
    support_artifacts: tuple[ParsedSupportArtifact, ...],
) -> dict[str, tuple[ParsedSupportArtifact, ...]]:
    result: dict[str, list[ParsedSupportArtifact]] = {}
    for support in support_artifacts:
        if support.kind is DependencyKind.PDF and support.normalized_rows_path is not None:
            code = leading_form_code(_artifact_stem(support.normalized_rows_path))
            result.setdefault(code, []).append(support)
    return {key: tuple(value) for key, value in result.items()}


def _matching_pdf_supports(
    dataset: OrganizedDataset,
    support_by_stem: Mapping[str, tuple[ParsedSupportArtifact, ...]],
    support_by_form_code: Mapping[str, tuple[ParsedSupportArtifact, ...]],
) -> tuple[ParsedSupportArtifact, ...]:
    dataset_stem = _logical_dataset_stem(dataset)
    exact = support_by_stem.get(dataset_stem, ())
    if exact:
        return exact
    candidates = support_by_form_code.get(leading_form_code(dataset_stem), ())
    return candidates if len(candidates) == 1 else ()


def _logical_dataset_stem(dataset: OrganizedDataset) -> str:
    return _artifact_stem(dataset.normalized_rows_path)


def _artifact_stem(path: Path) -> str:
    stem = Path(path).stem.lower()
    return stem.split("__", 1)[0]




def _decision_matches_recommendation(
    decision: DependencyDecision,
    recommendation: DependencyRecommendation,
) -> bool:
    return (
        decision.recommendation_id == recommendation.recommendation_id
        and decision.dataset_artifact_id
        == recommendation.dataset_artifact_id
        and decision.dataset_sha256 == recommendation.dataset_sha256
        and decision.support_artifact_id
        == recommendation.support_artifact_id
        and decision.support_sha256 == recommendation.support_sha256
        and decision.normalized_support_sha256
        == recommendation.normalized_support_sha256
        and decision.kind is recommendation.kind
        and decision.sensitivity is recommendation.default_sensitivity
        and decision.reason_code is recommendation.reason_code
        and decision.basis == recommendation.basis
    )


def dependency_decision_is_current(
    decision: DependencyDecision, recommendation: DependencyRecommendation
) -> bool:
    """A decision is current for a recommendation when every identity and basis
    field matches (dataset/support hashes, kind, sensitivity, reason, basis)."""
    return _decision_matches_recommendation(decision, recommendation)


def _ignored_exact_recommendation(
    decisions_by_recommendation: Mapping[str, DependencyDecision],
    recommendation: DependencyRecommendation,
) -> bool:
    decision = decisions_by_recommendation.get(
        recommendation.recommendation_id
    )
    return bool(
        decision is not None
        and decision.level is DependencyLevel.IGNORED
        and _decision_matches_recommendation(decision, recommendation)
    )



def _append_code_table(payload: dict[str, Any]) -> dict[str, Any]:
    payload["code_table_version"] = CODE_TABLE_VERSION
    return payload


def _require_code_table(payload: Mapping[str, Any]) -> None:
    if payload.get("code_table_version") != CODE_TABLE_VERSION:
        raise ValueError("unsupported code_table_version")


def _coerce_mapping(payload: Mapping[str, Any] | str | bytes, label: str) -> Mapping[str, Any]:
    if isinstance(payload, (str, bytes)):
        def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate key in {label}: {key}")
                result[key] = value
            return result
        loaded = json.loads(payload, object_pairs_hook=no_duplicates)
        if not isinstance(loaded, Mapping):
            raise ValueError(f"{label} must be a mapping")
        return loaded
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label} must be a mapping")
    return payload


def _require_exact_keys(payload: Mapping[str, Any], keys: set[str], label: str) -> None:
    actual = set(payload)
    if actual != keys:
        raise ValueError(f"{label} keys mismatch: missing={sorted(keys - actual)} unknown={sorted(actual - keys)}")


def _parse_timestamp(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _require_decided_by_identifier(value: object) -> None:
    if not isinstance(value, str) or not _DECIDED_BY_RE.fullmatch(value):
        raise ValueError("invalid decided_by identifier")


def _require_artifact_id(value: object, field: str) -> None:
    if not is_artifact_id(value):
        raise ValueError(f"invalid {field}")


def _require_header_id(value: object, field: str) -> None:
    if not is_header_id(value):
        raise ValueError(f"invalid {field}")


def _require_recommendation_id(value: object, field: str) -> None:
    if not is_recommendation_id(value):
        raise ValueError(f"invalid {field}")


def _require_decision_id(value: object, field: str) -> None:
    if not is_decision_id(value):
        raise ValueError(f"invalid {field}")


def _require_requirement_id(value: object, field: str) -> None:
    if not is_requirement_id(value):
        raise ValueError(f"invalid {field}")


def _require_sha256(value: object, field: str) -> None:
    if not is_sha256(value):
        raise ValueError(f"invalid {field}")


def _require_timestamp_z(value: object, field: str) -> None:
    if not is_timestamp_z(value):
        raise ValueError(f"invalid {field}")


def _require_enum(value: object, enum_type: type[Enum], field: str) -> None:
    if not isinstance(value, enum_type):
        raise ValueError(f"{field} must be {enum_type.__name__}")


def _validate_support_fields(artifact_id: str | None, support_sha256_value: str | None, normalized_sha256_value: str | None) -> None:
    if artifact_id is None:
        if support_sha256_value is not None or normalized_sha256_value is not None:
            raise ValueError("support fields must all be null when support_artifact_id is null")
        return
    _require_artifact_id(artifact_id, "support_artifact_id")
    _require_sha256(support_sha256_value, "support_sha256")
    if normalized_sha256_value is not None:
        _require_sha256(normalized_sha256_value, "normalized_support_sha256")


def _validated_tuple(values: tuple[str, ...], validator, field: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise ValueError(f"{field} must be tuple")
    for value in values:
        validator(value, field)
    return values


def _string_tuple(values: tuple[str, ...], field: str) -> tuple[str, ...]:
    if not isinstance(values, tuple) or not all(isinstance(value, str) and value for value in values):
        raise ValueError(f"{field} must be a tuple of strings")
    return values


def _require_safe_relative_path(value: object, field: str) -> None:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{field} must be a canonical relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise ValueError(f"{field} must be a canonical relative path")


def _int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be integer")
    return value


def _str(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be string")
    return value


def _nullable_str(value: object, field: str) -> str | None:
    return None if value is None else _str(value, field)


def _literal(value: object, expected: str, field: str):
    if value != expected:
        raise ValueError(f"{field} must be {expected}")
    return expected


def _list(value: object, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be list")
    return value


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be mapping")
    return value


def _load_jsonl_lines(path: Path, label: str) -> tuple[str, ...]:
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"{label} file is unavailable")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"{label} file is unreadable") from exc
    if any(not line.strip() for line in lines):
        raise ValueError(f"{label} contains a blank record")
    return tuple(lines)


def _unique_by_recommendation_id(records: tuple[Any, ...], label: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for record in records:
        recommendation_id = getattr(record, "recommendation_id", None)
        _require_recommendation_id(recommendation_id, "recommendation_id")
        if recommendation_id in result:
            raise ValueError(f"duplicate recommendation identity in {label}")
        result[recommendation_id] = record
    return result


def _atomic_write_jsonl(path: Path, records: Any, *, mode: int) -> Path:
    path = Path(path)
    serialized = tuple(
        json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        for record in records
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            os.fchmod(handle.fileno(), mode)
            handle.writelines(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        path.chmod(mode)
        return path
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _append_jsonl_record(path: Path, record: Mapping[str, Any], *, mode: int) -> Path:
    path = Path(path)
    serialized = json.dumps(dict(record), sort_keys=True, separators=(",", ":")) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, mode)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return path


def _require_private_mode(path: Path, label: str) -> None:
    path = Path(path)
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError(f"{label} file is unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError(f"{label} file must be a regular mode-0600 file")
