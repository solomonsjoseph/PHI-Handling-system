from __future__ import annotations

import hashlib
import http.client
import json
import math
import re
import socket
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, TypeVar
from urllib.parse import urlsplit

from phi_engine.config import config
from phi_engine.pipeline.dependencies import (
    DependencyDecision,
    DependencyDecisionBasis,
    DependencyKind,
    DependencyLevel,
    DependencyReasonCode,
    DependencyRecommendation,
    OrganizedDataset,
    ParsedSupportArtifact,
    Sensitivity,
    SupportParseStatus,
    recommendation_identity,
)
from phi_engine.security.phi_gate import PHIEgressBlockedError, phi_gate_check
from phi_engine.security.phi_review import Action, normalize_header

_MAX_TASK_BYTES = 64 * 1024
_MAX_RESPONSE_BYTES = 256 * 1024
_MAX_RESPONSE_DEPTH = 16
_MAX_RESPONSE_ITEMS = 512
_MAX_STRING_CODEPOINTS = 4096
_MAX_SUPPORT_ROWS = 128
_MAX_SUPPORT_CELLS = 4096
_MAX_SUPPORT_CELL_CODEPOINTS = 256
_MAX_NORMALIZED_SUPPORT_BYTES = 512 * 1024 * 1024
_MAX_NORMALIZED_LINE_BYTES = 64 * 1024
_MAX_HEADER_SAMPLES = 25
_MIN_CONFIDENCE = 0.75
_ARTIFACT_ID_RE = re.compile(r"^a_[0-9a-f]{32}$")
_HEADER_ID_RE = re.compile(r"^h_[0-9a-f]{24}$")
_DECISION_ID_RE = re.compile(r"^dd_[0-9a-f]{32}$")
_REQUIREMENT_ID_RE = re.compile(r"^tr_[0-9a-f]{32}$")
_TRANSFORM_ID_RE = re.compile(r"^tx_[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MODEL_SPEC_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._:/-]{0,127})@(sha256:[0-9a-f]{64})$"
)
_FENCE_RE = re.compile(r"\A```(?:json)?[ \t]*\r?\n(.*)\r?\n```[ \t]*\Z", re.DOTALL)


class ModelFailureCode(str, Enum):
    DISABLED = "disabled"
    OFFLINE_ATTESTATION_MISSING = "offline_attestation_missing"
    PROVIDER_UNSUPPORTED = "provider_unsupported"
    BASE_URL_INVALID = "base_url_invalid"
    BASE_URL_NOT_ALLOWED = "base_url_not_allowed"
    MODEL_ALLOWLIST_EMPTY = "model_allowlist_empty"
    MODEL_NOT_INSTALLED = "model_not_installed"
    MODEL_DIGEST_MISMATCH = "model_digest_mismatch"
    INPUT_TOO_LARGE = "input_too_large"
    CONNECTION_FAILED = "connection_failed"
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    REDIRECT_REJECTED = "redirect_rejected"
    RESPONSE_TOO_LARGE = "response_too_large"
    RESPONSE_TOO_DEEP = "response_too_deep"
    RESPONSE_TOO_MANY_ITEMS = "response_too_many_items"
    STRING_TOO_LONG = "string_too_long"
    INVALID_JSON = "invalid_json"
    INVALID_SCHEMA = "invalid_schema"
    BINDING_MISMATCH = "binding_mismatch"
    RULE_MISMATCH = "rule_mismatch"
    CONFIDENCE_LOW = "confidence_low"
    UNSUPPORTED_ACTION = "unsupported_action"
    PROMPT_GATE_BLOCKED = "prompt_gate_blocked"


class VariableType(str, Enum):
    IDENTIFIER = "identifier"
    DATE = "date"
    QUASI_IDENTIFIER = "quasi_identifier"
    CATEGORICAL = "categorical"
    NUMERIC_COUNT = "numeric_count"
    FREE_TEXT = "free_text"
    OTHER = "other"


class SupportSignalType(str, Enum):
    DEFINITION_BINDING = "definition_binding"
    ACTION_BINDING = "action_binding"
    TRANSFORM_BINDING = "transform_binding"
    EXPLICIT_NON_PHI = "explicit_non_phi"


class _SafeModelError(Exception):
    def __init__(self, code: ModelFailureCode):
        self.code = code
        super().__init__(code.value)


class LocalModelUnavailableError(_SafeModelError):
    pass


class ModelResponseError(_SafeModelError):
    pass


@dataclass(frozen=True)
class ResolutionEvidence:
    """Phase-4 value-free evidence projection, replaced by the phase-5 type."""

    profile_input_sha256: str

    def __post_init__(self) -> None:
        _require_sha256(self.profile_input_sha256)


def _invalid() -> ModelResponseError:
    return ModelResponseError(ModelFailureCode.INVALID_SCHEMA)


def _require_str(value: object, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise _invalid()
    return value


def _require_int(value: object, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise _invalid()
    return value


def _require_artifact_id(value: object) -> str:
    text = _require_str(value)
    if not _ARTIFACT_ID_RE.fullmatch(text):
        raise _invalid()
    return text


def _require_header_id(value: object) -> str:
    text = _require_str(value)
    if not _HEADER_ID_RE.fullmatch(text):
        raise _invalid()
    return text


def _require_sha256(value: object) -> str:
    text = _require_str(value)
    if not _SHA256_RE.fullmatch(text):
        raise _invalid()
    return text


def _require_string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise _invalid()
    result = tuple(_require_str(item) for item in value)
    return result


def _require_confidence(value: object) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise _invalid()
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError):
        raise _invalid() from None
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise _invalid()
    return result


def _require_exact_mapping(payload: object, keys: tuple[str, ...]) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != set(keys):
        raise _invalid()
    if not all(isinstance(key, str) for key in payload):
        raise _invalid()
    return payload


def _parse_action(value: object) -> Action:
    if not isinstance(value, str):
        raise _invalid()
    try:
        return Action(value)
    except ValueError:
        raise ModelResponseError(ModelFailureCode.UNSUPPORTED_ACTION) from None


def _parse_enum(value: object, enum_type: type[Enum]) -> Any:
    if not isinstance(value, str):
        raise _invalid()
    try:
        return enum_type(value)
    except ValueError:
        raise _invalid() from None


@dataclass(frozen=True)
class CandidateRuleView:
    rule_id: str
    action: Action
    citation: str
    jurisdictions: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_str(self.rule_id)
        if not isinstance(self.action, Action):
            raise _invalid()
        _require_str(self.citation)
        if not isinstance(self.jurisdictions, tuple):
            raise _invalid()
        _require_string_tuple(self.jurisdictions)


@dataclass(frozen=True)
class MatchedSupportCell:
    column_index: int
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        _require_int(self.column_index)
        _require_str(self.value, nonempty=False)


@dataclass(frozen=True)
class MatchedSupportRow:
    support_artifact_id: str
    support_sha256: str
    sheet_index: int
    table_index: int
    row_index: int
    matched_column_indices: tuple[int, ...]
    cells: tuple[MatchedSupportCell, ...] = field(repr=False)

    def __post_init__(self) -> None:
        _require_artifact_id(self.support_artifact_id)
        _require_sha256(self.support_sha256)
        _require_int(self.sheet_index)
        _require_int(self.table_index)
        _require_int(self.row_index)
        if not isinstance(self.matched_column_indices, tuple) or not self.matched_column_indices:
            raise _invalid()
        for index in self.matched_column_indices:
            _require_int(index)
        if len(set(self.matched_column_indices)) != len(self.matched_column_indices):
            raise _invalid()
        if not isinstance(self.cells, tuple) or not self.cells or not all(
            isinstance(cell, MatchedSupportCell) for cell in self.cells
        ):
            raise _invalid()
        cell_indices = tuple(cell.column_index for cell in self.cells)
        if len(set(cell_indices)) != len(cell_indices):
            raise _invalid()
        if not set(self.matched_column_indices).issubset(cell_indices):
            raise _invalid()


@dataclass(frozen=True)
class ConfidentialHeaderTask:
    dataset_artifact_id: str
    dataset_sha256: str
    header_id: str
    raw_header: str = field(repr=False)
    samples: tuple[str, ...] = field(repr=False)
    candidate_rules: tuple[CandidateRuleView, ...]
    evidence: ResolutionEvidence = field(repr=False)

    def __post_init__(self) -> None:
        _require_artifact_id(self.dataset_artifact_id)
        _require_sha256(self.dataset_sha256)
        _require_header_id(self.header_id)
        _require_str(self.raw_header)
        if not isinstance(self.samples, tuple):
            raise _invalid()
        distinct: list[str] = []
        seen: set[str] = set()
        for sample in self.samples:
            _require_str(sample, nonempty=False)
            if sample and sample not in seen:
                seen.add(sample)
                distinct.append(sample)
                if len(distinct) == _MAX_HEADER_SAMPLES:
                    break
        object.__setattr__(self, "samples", tuple(distinct))
        _require_candidates(self.candidate_rules)
        if type(self.evidence) is not ResolutionEvidence:
            raise _invalid()
        _bounded_canonical_json(_header_task_payload(self))


@dataclass(frozen=True)
class SupportSignalTask:
    dataset_artifact_id: str
    dataset_sha256: str
    header_ids: tuple[str, ...]
    support_artifact_id: str
    support_sha256: str
    normalized_support_sha256: str
    sensitivity: Sensitivity
    dependency_decision_id: str
    matched_rows: tuple[MatchedSupportRow, ...] = field(repr=False)
    candidate_rules: tuple[CandidateRuleView, ...]

    def __post_init__(self) -> None:
        _require_artifact_id(self.dataset_artifact_id)
        _require_sha256(self.dataset_sha256)
        if not isinstance(self.header_ids, tuple) or not self.header_ids:
            raise _invalid()
        for header_id in self.header_ids:
            _require_header_id(header_id)
        if len(set(self.header_ids)) != len(self.header_ids):
            raise _invalid()
        _require_artifact_id(self.support_artifact_id)
        _require_sha256(self.support_sha256)
        _require_sha256(self.normalized_support_sha256)
        if not isinstance(self.sensitivity, Sensitivity):
            raise _invalid()
        if not isinstance(self.dependency_decision_id, str) or not _DECISION_ID_RE.fullmatch(
            self.dependency_decision_id
        ):
            raise _invalid()
        if not isinstance(self.matched_rows, tuple) or not all(
            isinstance(row, MatchedSupportRow) for row in self.matched_rows
        ):
            raise _invalid()
        _require_candidates(self.candidate_rules)
        _validate_support_bounds(self)


def _require_candidates(candidates: object) -> None:
    if not isinstance(candidates, tuple) or not all(
        isinstance(candidate, CandidateRuleView) for candidate in candidates
    ):
        raise _invalid()


def _candidate_payload(candidate: CandidateRuleView) -> dict[str, Any]:
    return {
        "rule_id": candidate.rule_id,
        "action": candidate.action.value,
        "citation": candidate.citation,
        "jurisdictions": list(candidate.jurisdictions),
    }


def _header_task_payload(task: ConfidentialHeaderTask) -> dict[str, Any]:
    return {
        "dataset_artifact_id": task.dataset_artifact_id,
        "dataset_sha256": task.dataset_sha256,
        "header_id": task.header_id,
        "raw_header": task.raw_header,
        "samples": list(task.samples),
        "candidate_rules": [_candidate_payload(rule) for rule in task.candidate_rules],
        "evidence": {"profile_input_sha256": task.evidence.profile_input_sha256},
    }


def _support_task_payload(task: SupportSignalTask) -> dict[str, Any]:
    return {
        "dataset_artifact_id": task.dataset_artifact_id,
        "dataset_sha256": task.dataset_sha256,
        "header_ids": list(task.header_ids),
        "support_artifact_id": task.support_artifact_id,
        "support_sha256": task.support_sha256,
        "normalized_support_sha256": task.normalized_support_sha256,
        "sensitivity": task.sensitivity.value,
        "dependency_decision_id": task.dependency_decision_id,
        "matched_rows": [
            {
                "support_artifact_id": row.support_artifact_id,
                "support_sha256": row.support_sha256,
                "sheet_index": row.sheet_index,
                "table_index": row.table_index,
                "row_index": row.row_index,
                "matched_column_indices": list(row.matched_column_indices),
                "cells": [
                    {"column_index": cell.column_index, "value": cell.value}
                    for cell in row.cells
                ],
            }
            for row in task.matched_rows
        ],
        "candidate_rules": [_candidate_payload(rule) for rule in task.candidate_rules],
    }


def _bounded_canonical_json(payload: object) -> str:
    try:
        raw = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        encoded = raw.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise _invalid() from None
    if len(encoded) > _MAX_TASK_BYTES:
        raise ModelResponseError(ModelFailureCode.INPUT_TOO_LARGE)
    return raw


def _validate_support_bounds(task: SupportSignalTask) -> None:
    if len(task.matched_rows) > _MAX_SUPPORT_ROWS:
        raise ModelResponseError(ModelFailureCode.INPUT_TOO_LARGE)
    cell_count = 0
    for row in task.matched_rows:
        cell_count += len(row.cells)
        if cell_count > _MAX_SUPPORT_CELLS:
            raise ModelResponseError(ModelFailureCode.INPUT_TOO_LARGE)
        if any(len(cell.value) > _MAX_SUPPORT_CELL_CODEPOINTS for cell in row.cells):
            raise ModelResponseError(ModelFailureCode.INPUT_TOO_LARGE)
    _bounded_canonical_json(_support_task_payload(task))


@dataclass(frozen=True)
class HeaderResolution:
    dataset_artifact_id: str
    header_id: str
    inferred_variable_type: VariableType
    action: Action
    matched_rule_id: str
    rule_citation: str
    jurisdictions: tuple[str, ...]
    confidence: float

    def to_json(self) -> dict[str, Any]:
        return {
            "dataset_artifact_id": self.dataset_artifact_id,
            "header_id": self.header_id,
            "inferred_variable_type": self.inferred_variable_type.value,
            "action": self.action.value,
            "matched_rule_id": self.matched_rule_id,
            "rule_citation": self.rule_citation,
            "jurisdictions": list(self.jurisdictions),
            "confidence": self.confidence,
        }

    @classmethod
    def from_json(cls, payload: object) -> HeaderResolution:
        keys = (
            "dataset_artifact_id",
            "header_id",
            "inferred_variable_type",
            "action",
            "matched_rule_id",
            "rule_citation",
            "jurisdictions",
            "confidence",
        )
        data = _require_exact_mapping(payload, keys)
        return cls(
            dataset_artifact_id=_require_artifact_id(data["dataset_artifact_id"]),
            header_id=_require_header_id(data["header_id"]),
            inferred_variable_type=_parse_enum(
                data["inferred_variable_type"], VariableType
            ),
            action=_parse_action(data["action"]),
            matched_rule_id=_require_str(data["matched_rule_id"]),
            rule_citation=_require_str(data["rule_citation"]),
            jurisdictions=_require_string_tuple(data["jurisdictions"]),
            confidence=_require_confidence(data["confidence"]),
        )


@dataclass(frozen=True)
class SupportSignal:
    dataset_artifact_id: str
    header_id: str
    support_artifact_id: str
    support_sha256: str
    signal_type: SupportSignalType
    action: Action
    matched_rule_id: str
    rule_citation: str
    jurisdictions: tuple[str, ...]
    transform_requirement_id: str | None
    transform_id: str | None
    confidence: float

    def to_json(self) -> dict[str, Any]:
        return {
            "dataset_artifact_id": self.dataset_artifact_id,
            "header_id": self.header_id,
            "support_artifact_id": self.support_artifact_id,
            "support_sha256": self.support_sha256,
            "signal_type": self.signal_type.value,
            "action": self.action.value,
            "matched_rule_id": self.matched_rule_id,
            "rule_citation": self.rule_citation,
            "jurisdictions": list(self.jurisdictions),
            "transform_requirement_id": self.transform_requirement_id,
            "transform_id": self.transform_id,
            "confidence": self.confidence,
        }

    @classmethod
    def from_json(cls, payload: object) -> SupportSignal:
        keys = (
            "dataset_artifact_id",
            "header_id",
            "support_artifact_id",
            "support_sha256",
            "signal_type",
            "action",
            "matched_rule_id",
            "rule_citation",
            "jurisdictions",
            "transform_requirement_id",
            "transform_id",
            "confidence",
        )
        data = _require_exact_mapping(payload, keys)
        return cls(
            dataset_artifact_id=_require_artifact_id(data["dataset_artifact_id"]),
            header_id=_require_header_id(data["header_id"]),
            support_artifact_id=_require_artifact_id(data["support_artifact_id"]),
            support_sha256=_require_sha256(data["support_sha256"]),
            signal_type=_parse_enum(data["signal_type"], SupportSignalType),
            action=_parse_action(data["action"]),
            matched_rule_id=_require_str(data["matched_rule_id"]),
            rule_citation=_require_str(data["rule_citation"]),
            jurisdictions=_require_string_tuple(data["jurisdictions"]),
            transform_requirement_id=_nullable_value_free_id(
                data["transform_requirement_id"], _REQUIREMENT_ID_RE
            ),
            transform_id=_nullable_value_free_id(
                data["transform_id"], _TRANSFORM_ID_RE
            ),
            confidence=_require_confidence(data["confidence"]),
        )


@dataclass(frozen=True)
class ExtractedRuleCandidate:
    rule_id: str
    action: Action
    literal_aliases: tuple[str, ...]
    citation: str
    jurisdiction: str

    def to_json(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "action": self.action.value,
            "literal_aliases": list(self.literal_aliases),
            "citation": self.citation,
            "jurisdiction": self.jurisdiction,
        }

    @classmethod
    def from_json(cls, payload: object) -> ExtractedRuleCandidate:
        keys = ("rule_id", "action", "literal_aliases", "citation", "jurisdiction")
        data = _require_exact_mapping(payload, keys)
        return cls(
            rule_id=_require_str(data["rule_id"]),
            action=_parse_action(data["action"]),
            literal_aliases=_require_string_tuple(data["literal_aliases"]),
            citation=_require_str(data["citation"]),
            jurisdiction=_require_str(data["jurisdiction"]),
        )


@dataclass(frozen=True)
class OfficialRuleExtraction:
    registry_source_id: str
    jurisdiction: str
    source_sha256: str
    candidates: tuple[ExtractedRuleCandidate, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "registry_source_id": self.registry_source_id,
            "jurisdiction": self.jurisdiction,
            "source_sha256": self.source_sha256,
            "candidates": [candidate.to_json() for candidate in self.candidates],
        }

    @classmethod
    def from_json(cls, payload: object) -> OfficialRuleExtraction:
        keys = ("registry_source_id", "jurisdiction", "source_sha256", "candidates")
        data = _require_exact_mapping(payload, keys)
        raw_candidates = data["candidates"]
        if not isinstance(raw_candidates, list):
            raise _invalid()
        return cls(
            registry_source_id=_require_str(data["registry_source_id"]),
            jurisdiction=_require_str(data["jurisdiction"]),
            source_sha256=_require_sha256(data["source_sha256"]),
            candidates=tuple(
                ExtractedRuleCandidate.from_json(candidate)
                for candidate in raw_candidates
            ),
        )


def _nullable_str(value: object) -> str | None:
    return None if value is None else _require_str(value)


def _nullable_value_free_id(value: object, pattern: re.Pattern[str]) -> str | None:
    if value is None:
        return None
    text = _require_str(value)
    if not pattern.fullmatch(text):
        raise _invalid()
    return text


def _reject_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def _strict_json_loads(raw: str) -> Any:
    try:
        return json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")),
        )
    except RecursionError:
        raise ModelResponseError(ModelFailureCode.RESPONSE_TOO_DEEP) from None
    except (ValueError, TypeError, json.JSONDecodeError):
        raise ModelResponseError(ModelFailureCode.INVALID_JSON) from None


def _parse_model_json(raw: str) -> Any:
    if not isinstance(raw, str):
        raise ModelResponseError(ModelFailureCode.INVALID_JSON)
    try:
        encoded_size = len(raw.encode("utf-8"))
    except UnicodeEncodeError:
        raise ModelResponseError(ModelFailureCode.INVALID_JSON) from None
    if encoded_size > _MAX_RESPONSE_BYTES:
        raise ModelResponseError(ModelFailureCode.RESPONSE_TOO_LARGE)
    fence = _FENCE_RE.fullmatch(raw)
    candidate = fence.group(1) if fence else raw
    value = _strict_json_loads(candidate)
    _validate_model_json_shape(value)
    return value


def _validate_model_json_shape(value: Any) -> None:
    item_count = 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if isinstance(current, str):
            if len(current) > _MAX_STRING_CODEPOINTS:
                raise ModelResponseError(ModelFailureCode.STRING_TOO_LONG)
            continue
        if current is None or isinstance(current, (bool, int)):
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise ModelResponseError(ModelFailureCode.INVALID_JSON)
            continue
        if isinstance(current, list):
            if depth > _MAX_RESPONSE_DEPTH:
                raise ModelResponseError(ModelFailureCode.RESPONSE_TOO_DEEP)
            item_count += len(current)
            if item_count > _MAX_RESPONSE_ITEMS:
                raise ModelResponseError(ModelFailureCode.RESPONSE_TOO_MANY_ITEMS)
            stack.extend((item, depth + 1) for item in current)
            continue
        if isinstance(current, dict):
            if depth > _MAX_RESPONSE_DEPTH:
                raise ModelResponseError(ModelFailureCode.RESPONSE_TOO_DEEP)
            item_count += len(current)
            if item_count > _MAX_RESPONSE_ITEMS:
                raise ModelResponseError(ModelFailureCode.RESPONSE_TOO_MANY_ITEMS)
            for key, item in current.items():
                if not isinstance(key, str):
                    raise ModelResponseError(ModelFailureCode.INVALID_SCHEMA)
                if len(key) > _MAX_STRING_CODEPOINTS:
                    raise ModelResponseError(ModelFailureCode.STRING_TOO_LONG)
                stack.append((item, depth + 1))
            continue
        raise ModelResponseError(ModelFailureCode.INVALID_SCHEMA)


def _parse_ollama_transport_json(raw: str, *, response_envelope: bool) -> Any:
    value = _strict_json_loads(raw)
    if (
        response_envelope
        and isinstance(value, dict)
        and isinstance(value.get("response"), str)
    ):
        bounded_envelope = dict(value)
        bounded_envelope["response"] = ""
        _validate_model_json_shape(bounded_envelope)
    else:
        _validate_model_json_shape(value)
    return value


class OfflineLocalLLMClient:
    """Attested loopback-only Ollama transport with exact digest selection.

    ``offline_approved`` is an operator attestation, not proof of isolation.
    Deployments must independently enforce and audit an OS/container outbound-deny
    boundary around the model process.
    """

    def __init__(self, local_config: config.LocalLLMConfig):
        self._config = local_config
        self.max_attempts = 2

    def complete(self, prompt: str) -> str:
        host, port = self._validated_endpoint()
        if not isinstance(prompt, str) or len(prompt.encode("utf-8")) > _MAX_TASK_BYTES:
            raise LocalModelUnavailableError(ModelFailureCode.INPUT_TOO_LARGE)
        tags = self._request_json(host, port, "GET", "/api/tags", None)
        model = self._select_model(tags)
        body = json.dumps(
            {"model": model, "prompt": prompt, "stream": False},
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        if len(body) > _MAX_TASK_BYTES:
            raise LocalModelUnavailableError(ModelFailureCode.INPUT_TOO_LARGE)
        response = self._request_json(host, port, "POST", "/api/generate", body)
        if not isinstance(response, Mapping) or not isinstance(response.get("response"), str):
            raise LocalModelUnavailableError(ModelFailureCode.INVALID_SCHEMA)
        return response["response"]

    def _validated_endpoint(self) -> tuple[str, int]:
        cfg = self._config
        if cfg.provider == "none":
            raise LocalModelUnavailableError(ModelFailureCode.DISABLED)
        if not cfg.offline_approved:
            raise LocalModelUnavailableError(
                ModelFailureCode.OFFLINE_ATTESTATION_MISSING
            )
        if cfg.provider != "ollama":
            raise LocalModelUnavailableError(ModelFailureCode.PROVIDER_UNSUPPORTED)
        if not cfg.models:
            raise LocalModelUnavailableError(ModelFailureCode.MODEL_ALLOWLIST_EMPTY)
        try:
            parsed = urlsplit(cfg.base_url)
            port = parsed.port
        except ValueError:
            raise LocalModelUnavailableError(ModelFailureCode.BASE_URL_INVALID) from None
        if (
            parsed.scheme != "http"
            or not parsed.hostname
            or port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or cfg.base_url.endswith("/")
        ):
            raise LocalModelUnavailableError(ModelFailureCode.BASE_URL_INVALID)
        host = parsed.hostname
        if host not in {"127.0.0.1", "::1"}:
            raise LocalModelUnavailableError(ModelFailureCode.BASE_URL_NOT_ALLOWED)
        canonical = (
            f"http://[{host}]:{port}" if host == "::1" else f"http://{host}:{port}"
        )
        if cfg.base_url != canonical or canonical not in cfg.allowed_base_urls:
            raise LocalModelUnavailableError(ModelFailureCode.BASE_URL_NOT_ALLOWED)
        for model_spec in cfg.models:
            if not isinstance(model_spec, str) or not _MODEL_SPEC_RE.fullmatch(model_spec):
                raise LocalModelUnavailableError(ModelFailureCode.MODEL_DIGEST_MISMATCH)
        return host, port

    def _request_json(
        self,
        host: str,
        port: int,
        method: str,
        path: str,
        body: bytes | None,
    ) -> Any:
        attempts = self._config.max_retries + 1
        for attempt in range(attempts):
            connection = None
            response = None
            try:
                connection = http.client.HTTPConnection(
                    host, port, timeout=self._config.timeout_s
                )
                headers = {"Content-Type": "application/json"} if body is not None else {}
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                if 300 <= response.status < 400:
                    raise LocalModelUnavailableError(
                        ModelFailureCode.REDIRECT_REJECTED
                    )
                if response.status < 200 or response.status >= 300:
                    raise LocalModelUnavailableError(ModelFailureCode.HTTP_ERROR)
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(raw) > _MAX_RESPONSE_BYTES:
                    raise LocalModelUnavailableError(
                        ModelFailureCode.RESPONSE_TOO_LARGE
                    )
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    raise LocalModelUnavailableError(
                        ModelFailureCode.INVALID_JSON
                    ) from None
                try:
                    return _parse_ollama_transport_json(
                        text, response_envelope=path == "/api/generate"
                    )
                except ModelResponseError as exc:
                    raise LocalModelUnavailableError(exc.code) from None
            except LocalModelUnavailableError:
                raise
            except (socket.timeout, TimeoutError):
                if attempt + 1 == attempts:
                    raise LocalModelUnavailableError(ModelFailureCode.TIMEOUT) from None
            except (OSError, http.client.HTTPException):
                if attempt + 1 == attempts:
                    raise LocalModelUnavailableError(
                        ModelFailureCode.CONNECTION_FAILED
                    ) from None
            except Exception:
                raise LocalModelUnavailableError(ModelFailureCode.CONNECTION_FAILED) from None
            finally:
                if response is not None:
                    response.close()
                if connection is not None:
                    connection.close()
        raise LocalModelUnavailableError(ModelFailureCode.CONNECTION_FAILED)

    def _select_model(self, tags: object) -> str:
        if not isinstance(tags, Mapping) or not isinstance(tags.get("models"), list):
            raise LocalModelUnavailableError(ModelFailureCode.INVALID_SCHEMA)
        installed: dict[str, set[str]] = {}
        for entry in tags["models"]:
            if not isinstance(entry, Mapping):
                raise LocalModelUnavailableError(ModelFailureCode.INVALID_SCHEMA)
            name = entry.get("name")
            digest = entry.get("digest")
            if not isinstance(name, str) or not isinstance(digest, str):
                raise LocalModelUnavailableError(ModelFailureCode.INVALID_SCHEMA)
            installed.setdefault(name, set()).add(digest)
        installed_name_seen = False
        for spec in self._config.models:
            match = _MODEL_SPEC_RE.fullmatch(spec)
            if match is None:
                continue
            name, digest = match.groups()
            if name in installed:
                installed_name_seen = True
                if digest in installed[name]:
                    return name
        code = (
            ModelFailureCode.MODEL_DIGEST_MISMATCH
            if installed_name_seen
            else ModelFailureCode.MODEL_NOT_INSTALLED
        )
        raise LocalModelUnavailableError(code)


_T = TypeVar("_T")


@dataclass(frozen=True)
class _VerifiedSupportBinding:
    task: SupportSignalTask = field(repr=False)
    dataset: OrganizedDataset = field(repr=False)
    support: ParsedSupportArtifact = field(repr=False)
    recommendation: DependencyRecommendation = field(repr=False)
    decision: DependencyDecision = field(repr=False)
    current_basis: DependencyDecisionBasis = field(repr=False)


def _new_local_client() -> OfflineLocalLLMClient:
    return OfflineLocalLLMClient(config.get_local_llm_config())


def _new_ordinary_client() -> config.LLMClient:
    client = config.get_llm_client()
    if type(client) is not config.LLMClient:
        raise RuntimeError("ordinary client factory returned unexpected type")
    return client


def _local_completion_transport(
    client: OfflineLocalLLMClient, prompt: str
) -> str:
    return client.complete(prompt)


def _ordinary_completion_transport(client: config.LLMClient, prompt: str) -> str:
    return client.complete(prompt)


def _verified_public_completion_transport(
    client: config.LLMClient,
    fixed_prefix: str,
    public_document: str,
    fixed_suffix: str,
) -> str:
    return client._complete_verified_public(
        fixed_prefix, public_document, fixed_suffix
    )


class ModelTaskRouter:
    def __init__(self) -> None:
        local_client = _new_local_client()
        if type(local_client) is not OfflineLocalLLMClient:
            raise RuntimeError("local client factory returned unexpected type")
        local_client._config = replace(local_client._config, max_retries=0)
        self._local_client = local_client
        self._ordinary_client: config.LLMClient | None = None
        self._verified_support_bindings: dict[int, _VerifiedSupportBinding] = {}

    def build_support_signal_task(
        self,
        *,
        dataset: OrganizedDataset,
        support: ParsedSupportArtifact,
        recommendation: DependencyRecommendation,
        decision: DependencyDecision,
        current_basis: DependencyDecisionBasis,
        candidate_rules: tuple[CandidateRuleView, ...],
    ) -> SupportSignalTask:
        """Build and seal a task from current phase-3 records and normalized bytes."""
        self._verify_current_phase3_records(
            dataset, support, recommendation, decision, current_basis
        )
        rows = self._verified_exact_header_rows(dataset, support, recommendation)
        task = SupportSignalTask(
            dataset_artifact_id=recommendation.dataset_artifact_id,
            dataset_sha256=recommendation.dataset_sha256,
            header_ids=recommendation.header_ids,
            support_artifact_id=support.artifact_id,
            support_sha256=support.source_sha256,
            normalized_support_sha256=_require_sha256(
                recommendation.normalized_support_sha256
            ),
            sensitivity=decision.sensitivity,
            dependency_decision_id=decision.decision_id,
            matched_rows=rows,
            candidate_rules=candidate_rules,
        )
        self._verified_support_bindings[id(task)] = _VerifiedSupportBinding(
            task=task,
            dataset=dataset,
            support=support,
            recommendation=recommendation,
            decision=decision,
            current_basis=current_basis,
        )
        return task

    def resolve_confidential_header(
        self, task: ConfidentialHeaderTask
    ) -> HeaderResolution:
        if not isinstance(task, ConfidentialHeaderTask):
            raise _invalid()
        prompt = (
            "Resolve one confidential dataset header. Return exactly one JSON object "
            "matching the HeaderResolution contract. Use only supplied candidate rules.\n"
            + _bounded_canonical_json(_header_task_payload(task))
        )
        result = self._complete_json(
            self._local_client, prompt, HeaderResolution.from_json
        )
        self._verify_header_resolution(task, result)
        return result

    def extract_support_signals(
        self, task: SupportSignalTask
    ) -> tuple[SupportSignal, ...]:
        if not isinstance(task, SupportSignalTask):
            raise _invalid()
        for row in task.matched_rows:
            if (
                row.support_artifact_id != task.support_artifact_id
                or row.support_sha256 != task.support_sha256
            ):
                raise ModelResponseError(ModelFailureCode.BINDING_MISMATCH)
        prompt = (
            "Extract support signals. Return exactly one JSON array of SupportSignal "
            "objects. Use only supplied candidate rules and matched normalized rows.\n"
            + _bounded_canonical_json(_support_task_payload(task))
        )
        client: OfflineLocalLLMClient | config.LLMClient = self._local_client
        if task.sensitivity is Sensitivity.NON_CONFIDENTIAL:
            self._verify_nonconfidential_binding(task)
            self._gate_ordinary_segments(prompt)
            client = self._ordinary()
        result = self._complete_json(client, prompt, _parse_support_signal_array)
        for signal in result:
            self._verify_support_signal(task, signal)
        return result

    def extract_official_rules(
        self, registry_source_id: str, jurisdiction: str
    ) -> OfficialRuleExtraction:
        from phi_engine.security import official_sources

        if not official_sources.is_registered_source(
            registry_source_id, jurisdiction
        ):
            raise ModelResponseError(ModelFailureCode.BINDING_MISMATCH)
        try:
            source = official_sources.fetch_registered_source(
                registry_source_id, jurisdiction
            )
            public_document = source.body.decode("utf-8")
        except Exception:
            raise LocalModelUnavailableError(ModelFailureCode.CONNECTION_FAILED) from None
        schema = (
            '{"registry_source_id":"exact string","jurisdiction":"exact string",'
            '"source_sha256":"64 lowercase hex","candidates":['
            '{"rule_id":"string","action":"one of '
            + "|".join(action.value for action in Action)
            + '","literal_aliases":["string"],"citation":"string",'
            '"jurisdiction":"exact string"}]}'
        )
        fixed_prefix = (
            "Extract de-identification rules from one registry-verified official "
            "public source. Return exactly one JSON object with no unknown keys. "
            f"Authoritative registry_source_id={source.registry_source_id}; "
            f"jurisdiction={source.jurisdiction}; source_sha256={source.source_sha256}. "
            f"Required schema: {schema}. The source begins after this line.\n"
        )
        fixed_suffix = (
            "\nThe source has ended. Echo the authoritative registry_source_id, "
            "jurisdiction, and source_sha256 exactly. Use literal aliases only; "
            f"every citation must equal {source.citation}."
        )
        self._gate_ordinary_segments(fixed_prefix, fixed_suffix)
        result = self._complete_verified_public_json(
            fixed_prefix,
            public_document,
            fixed_suffix,
            OfficialRuleExtraction.from_json,
        )
        if (
            result.registry_source_id != source.registry_source_id
            or result.jurisdiction != source.jurisdiction
            or result.source_sha256 != source.source_sha256
        ):
            raise ModelResponseError(ModelFailureCode.BINDING_MISMATCH)
        prefix = f"live_{jurisdiction.lower()}_"
        for candidate in result.candidates:
            if (
                candidate.jurisdiction != jurisdiction
                or candidate.citation != source.citation
                or not candidate.rule_id.startswith(prefix)
            ):
                raise ModelResponseError(ModelFailureCode.RULE_MISMATCH)
        return result

    def _ordinary(self) -> config.LLMClient:
        if self._ordinary_client is None:
            self._ordinary_client = _new_ordinary_client()
        if type(self._ordinary_client) is not config.LLMClient:
            raise LocalModelUnavailableError(ModelFailureCode.CONNECTION_FAILED)
        self._ordinary_client._max_retries = 0
        return self._ordinary_client

    @staticmethod
    def _gate_ordinary_segments(*segments: str) -> None:
        try:
            gate = phi_gate_check(segments)
        except Exception:
            raise ModelResponseError(ModelFailureCode.PROMPT_GATE_BLOCKED) from None
        if gate.blocked:
            raise ModelResponseError(ModelFailureCode.PROMPT_GATE_BLOCKED)

    def _complete_json(
        self,
        client: OfflineLocalLLMClient | config.LLMClient,
        prompt: str,
        parser: Callable[[Any], _T],
    ) -> _T:
        if type(client) is OfflineLocalLLMClient:
            transport = lambda: _local_completion_transport(client, prompt)
        elif type(client) is config.LLMClient:
            transport = lambda: _ordinary_completion_transport(client, prompt)
        else:
            raise LocalModelUnavailableError(ModelFailureCode.CONNECTION_FAILED)
        return self._complete_json_with_retry(transport, parser)

    def _complete_verified_public_json(
        self,
        fixed_prefix: str,
        public_document: str,
        fixed_suffix: str,
        parser: Callable[[Any], _T],
    ) -> _T:
        client = self._ordinary()
        return self._complete_json_with_retry(
            lambda: _verified_public_completion_transport(
                client, fixed_prefix, public_document, fixed_suffix
            ),
            parser,
        )

    @staticmethod
    def _complete_json_with_retry(
        transport: Callable[[], str],
        parser: Callable[[Any], _T],
    ) -> _T:
        retryable_codes = {
            ModelFailureCode.INVALID_JSON,
            ModelFailureCode.INVALID_SCHEMA,
            ModelFailureCode.RESPONSE_TOO_LARGE,
            ModelFailureCode.RESPONSE_TOO_DEEP,
            ModelFailureCode.RESPONSE_TOO_MANY_ITEMS,
            ModelFailureCode.STRING_TOO_LONG,
        }
        last_error: ModelResponseError | None = None
        for _ in range(2):
            try:
                raw = transport()
            except LocalModelUnavailableError:
                raise
            except PHIEgressBlockedError:
                raise ModelResponseError(
                    ModelFailureCode.PROMPT_GATE_BLOCKED
                ) from None
            except Exception:
                raise LocalModelUnavailableError(
                    ModelFailureCode.CONNECTION_FAILED
                ) from None
            try:
                return parser(_parse_model_json(raw))
            except ModelResponseError as exc:
                if exc.code not in retryable_codes:
                    raise
                last_error = exc
        assert last_error is not None
        raise last_error

    def _verify_nonconfidential_binding(self, task: SupportSignalTask) -> None:
        binding = self._verified_support_bindings.get(id(task))
        if binding is None or binding.task is not task:
            raise ModelResponseError(ModelFailureCode.BINDING_MISMATCH)
        self._verify_current_phase3_records(
            binding.dataset,
            binding.support,
            binding.recommendation,
            binding.decision,
            binding.current_basis,
        )
        if (
            binding.decision.sensitivity is not Sensitivity.NON_CONFIDENTIAL
            or binding.decision.decision_id != task.dependency_decision_id
            or self._verified_exact_header_rows(
                binding.dataset, binding.support, binding.recommendation
            )
            != task.matched_rows
        ):
            raise ModelResponseError(ModelFailureCode.BINDING_MISMATCH)

    @staticmethod
    def _verify_current_phase3_records(
        dataset: OrganizedDataset,
        support: ParsedSupportArtifact,
        recommendation: DependencyRecommendation,
        decision: DependencyDecision,
        current_basis: DependencyDecisionBasis,
    ) -> None:
        if (
            type(dataset) is not OrganizedDataset
            or type(support) is not ParsedSupportArtifact
            or type(recommendation) is not DependencyRecommendation
            or type(decision) is not DependencyDecision
            or type(current_basis) is not DependencyDecisionBasis
            or support.parse_status is not SupportParseStatus.PARSED
            or support.normalized_rows_path is None
            or support.normalized_rows_sha256 is None
            or recommendation.reason_code
            is not DependencyReasonCode.EXACT_HEADER_MATCH
            or not recommendation.header_ids
            or recommendation.recommendation_id
            != recommendation_identity(
                dataset_artifact_id=recommendation.dataset_artifact_id,
                support_artifact_id=recommendation.support_artifact_id,
                kind=recommendation.kind,
                reason_code=recommendation.reason_code,
                header_ids=recommendation.header_ids,
                transform_requirement_ids=recommendation.transform_requirement_ids,
            )
            or decision.recommendation_id != recommendation.recommendation_id
            or decision.dataset_artifact_id != recommendation.dataset_artifact_id
            or decision.dataset_sha256 != recommendation.dataset_sha256
            or decision.support_artifact_id != recommendation.support_artifact_id
            or decision.support_sha256 != recommendation.support_sha256
            or decision.normalized_support_sha256
            != recommendation.normalized_support_sha256
            or decision.kind is not recommendation.kind
            or decision.level is DependencyLevel.IGNORED
            or decision.sensitivity is not recommendation.default_sensitivity
            or decision.reason_code is not recommendation.reason_code
            or decision.basis != recommendation.basis
            or recommendation.basis != current_basis
            or decision.basis != current_basis
            or dataset.artifact_id != recommendation.dataset_artifact_id
            or dataset.source_sha256 != recommendation.dataset_sha256
            or support.artifact_id != recommendation.support_artifact_id
            or support.source_sha256 != recommendation.support_sha256
            or support.normalized_rows_sha256
            != recommendation.normalized_support_sha256
            or support.kind is not recommendation.kind
        ):
            raise ModelResponseError(ModelFailureCode.BINDING_MISMATCH)
        header_ids = tuple(header.header_id for header in dataset.headers)
        column_indices = tuple(header.column_index for header in dataset.headers)
        if (
            len(set(header_ids)) != len(header_ids)
            or len(set(column_indices)) != len(column_indices)
            or not set(recommendation.header_ids).issubset(header_ids)
        ):
            raise ModelResponseError(ModelFailureCode.BINDING_MISMATCH)

    @staticmethod
    def _verified_exact_header_rows(
        dataset: OrganizedDataset,
        support: ParsedSupportArtifact,
        recommendation: DependencyRecommendation,
    ) -> tuple[MatchedSupportRow, ...]:
        path = support.normalized_rows_path
        if path is None or support.normalized_rows_sha256 is None:
            raise ModelResponseError(ModelFailureCode.BINDING_MISMATCH)
        target_headers = {
            header.header_id: normalize_header(
                header.normalized_name or header.raw_name
            )
            for header in dataset.headers
            if header.header_id in recommendation.header_ids
        }
        if (
            set(target_headers) != set(recommendation.header_ids)
            or any(not name for name in target_headers.values())
        ):
            raise ModelResponseError(ModelFailureCode.BINDING_MISMATCH)

        digest = hashlib.sha256()
        target_names = set(target_headers.values())
        matched_rows: list[MatchedSupportRow] = []
        matched_cell_count = 0
        matched_payload_bytes = 2
        previous_coordinates: tuple[int, int, int] | None = None
        total_bytes = 0
        saw_line = False
        try:
            with Path(path).open("rb") as stream:
                while raw_line := stream.readline(_MAX_NORMALIZED_LINE_BYTES + 1):
                    saw_line = True
                    total_bytes += len(raw_line)
                    if total_bytes > _MAX_NORMALIZED_SUPPORT_BYTES:
                        raise ModelResponseError(ModelFailureCode.INPUT_TOO_LARGE)
                    if len(raw_line) > _MAX_NORMALIZED_LINE_BYTES:
                        raise ModelResponseError(ModelFailureCode.INPUT_TOO_LARGE)
                    digest.update(raw_line)
                    line = raw_line.decode("utf-8").removesuffix("\n")
                    if not line:
                        raise ValueError
                    row = _strict_json_loads(line)
                    if not isinstance(row, Mapping) or set(row) != {
                        "support_artifact_id",
                        "source_sha256",
                        "sheet_index",
                        "table_index",
                        "row_index",
                        "cells",
                    }:
                        raise ValueError
                    if (
                        row["support_artifact_id"] != support.artifact_id
                        or row["source_sha256"] != support.source_sha256
                    ):
                        raise ValueError
                    indices = (
                        row["sheet_index"],
                        row["table_index"],
                        row["row_index"],
                    )
                    if any(
                        not isinstance(index, int)
                        or isinstance(index, bool)
                        or index < 0
                        for index in indices
                    ):
                        raise ValueError
                    if (
                        previous_coordinates is not None
                        and indices <= previous_coordinates
                    ):
                        raise ValueError
                    previous_coordinates = indices

                    raw_cells = row["cells"]
                    if not isinstance(raw_cells, list):
                        raise ValueError
                    matched_columns: list[int] = []
                    included_cell_too_long = False
                    for expected_column, raw_cell in enumerate(raw_cells):
                        if (
                            not isinstance(raw_cell, Mapping)
                            or set(raw_cell) != {"column_index", "value"}
                        ):
                            raise ValueError
                        column_index = raw_cell["column_index"]
                        value = raw_cell["value"]
                        if (
                            not isinstance(column_index, int)
                            or isinstance(column_index, bool)
                            or column_index != expected_column
                            or not isinstance(value, str)
                        ):
                            raise ValueError
                        if len(value) > _MAX_SUPPORT_CELL_CODEPOINTS:
                            included_cell_too_long = True
                        if normalize_header(value) in target_names:
                            matched_columns.append(column_index)

                    if not matched_columns:
                        continue
                    if included_cell_too_long:
                        raise ModelResponseError(ModelFailureCode.INPUT_TOO_LARGE)
                    if len(matched_rows) >= _MAX_SUPPORT_ROWS:
                        raise ModelResponseError(ModelFailureCode.INPUT_TOO_LARGE)
                    if (
                        matched_cell_count + len(raw_cells)
                        > _MAX_SUPPORT_CELLS
                    ):
                        raise ModelResponseError(ModelFailureCode.INPUT_TOO_LARGE)
                    row_payload = {
                        "support_artifact_id": support.artifact_id,
                        "support_sha256": support.source_sha256,
                        "sheet_index": indices[0],
                        "table_index": indices[1],
                        "row_index": indices[2],
                        "matched_column_indices": matched_columns,
                        "cells": raw_cells,
                    }
                    encoded_row = json.dumps(
                        row_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    ).encode("utf-8")
                    projected_bytes = (
                        matched_payload_bytes
                        + len(encoded_row)
                        + (1 if matched_rows else 0)
                    )
                    if projected_bytes > _MAX_TASK_BYTES:
                        raise ModelResponseError(ModelFailureCode.INPUT_TOO_LARGE)
                    matched_cell_count += len(raw_cells)
                    matched_payload_bytes = projected_bytes
                    matched_rows.append(
                        MatchedSupportRow(
                            support_artifact_id=support.artifact_id,
                            support_sha256=support.source_sha256,
                            sheet_index=indices[0],
                            table_index=indices[1],
                            row_index=indices[2],
                            matched_column_indices=tuple(matched_columns),
                            cells=tuple(
                                MatchedSupportCell(
                                    raw_cell["column_index"], raw_cell["value"]
                                )
                                for raw_cell in raw_cells
                            ),
                        )
                    )
        except ModelResponseError as exc:
            if exc.code is ModelFailureCode.INPUT_TOO_LARGE:
                raise
            raise ModelResponseError(ModelFailureCode.BINDING_MISMATCH) from None
        except (OSError, TypeError, UnicodeError, ValueError):
            raise ModelResponseError(ModelFailureCode.BINDING_MISMATCH) from None
        if (
            not saw_line
            or digest.hexdigest() != support.normalized_rows_sha256
            or not matched_rows
        ):
            raise ModelResponseError(ModelFailureCode.BINDING_MISMATCH)
        return tuple(matched_rows)

    @staticmethod
    def _verify_header_resolution(
        task: ConfidentialHeaderTask, result: HeaderResolution
    ) -> None:
        if (
            result.dataset_artifact_id != task.dataset_artifact_id
            or result.header_id != task.header_id
        ):
            raise ModelResponseError(ModelFailureCode.BINDING_MISMATCH)
        rule = _rule_by_id(task.candidate_rules, result.matched_rule_id)
        if rule is None or (
            result.action is not rule.action
            or result.rule_citation != rule.citation
            or result.jurisdictions != rule.jurisdictions
        ):
            raise ModelResponseError(ModelFailureCode.RULE_MISMATCH)
        if result.confidence < _MIN_CONFIDENCE:
            raise ModelResponseError(ModelFailureCode.CONFIDENCE_LOW)

    def _verify_support_signal(
        self, task: SupportSignalTask, result: SupportSignal
    ) -> None:
        if (
            result.dataset_artifact_id != task.dataset_artifact_id
            or result.header_id not in task.header_ids
            or result.support_artifact_id != task.support_artifact_id
            or result.support_sha256 != task.support_sha256
            or result.transform_requirement_id is not None
            or result.transform_id is not None
        ):
            raise ModelResponseError(ModelFailureCode.BINDING_MISMATCH)
        rule = _rule_by_id(task.candidate_rules, result.matched_rule_id)
        if rule is None or (
            result.action is not rule.action
            or result.rule_citation != rule.citation
            or result.jurisdictions != rule.jurisdictions
        ):
            raise ModelResponseError(ModelFailureCode.RULE_MISMATCH)
        if result.confidence < _MIN_CONFIDENCE:
            raise ModelResponseError(ModelFailureCode.CONFIDENCE_LOW)


def _parse_support_signal_array(payload: object) -> tuple[SupportSignal, ...]:
    if not isinstance(payload, list):
        raise _invalid()
    return tuple(SupportSignal.from_json(item) for item in payload)


def _rule_by_id(
    candidates: tuple[CandidateRuleView, ...], rule_id: str
) -> CandidateRuleView | None:
    return next((rule for rule in candidates if rule.rule_id == rule_id), None)
