#!/usr/bin/env python3
"""Minimal compatibility shim for the forms-manifest gate."""

from __future__ import annotations

import fnmatch
import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import NamedTuple

import yaml

from phi_engine.pipeline.dependencies import (
    DatasetDependency,
    DatasetDependencyBasis,
    DependencyKind,
    DependencyLevel,
    DependencyReasonCode,
    Sensitivity,
    is_artifact_id,
    is_recommendation_id,
    is_sha256,
    is_timestamp_z,
)

_gate_log = logging.getLogger("scripts.extraction.dataset_pipeline")
SUPPORTED_EXTENSIONS: tuple[str, ...] = (".xlsx", ".xls", ".csv")
_ALLOWED_TOP_KEYS = {
    "required",
    "optional",
    "reject",
    "date_locales",
    "dataset_dependencies_schema",
    "dataset_dependencies_code_table_version",
    "dataset_dependencies",
}
_ALLOWED_DEP_KEYS = {
    "dataset_artifact_id",
    "dataset_source_sha256",
    "support",
    "support_artifact_id",
    "support_source_sha256",
    "kind",
    "level",
    "sensitivity",
    "reason_code",
    "recommendation_id",
    "basis",
    "confirmed_by",
    "confirmed_at",
}
_ALLOWED_BASIS_KEYS = {"rulebook_sha256", "scrub_config_sha256", "support_role_sha256"}


class ManifestMismatchError(Exception):
    """Raised when the datasets directory does not match the forms manifest."""


class DependencyRelationState(str, Enum):
    """Currency of one manifest-declared artifact identity."""

    CURRENT = "current"
    MISSING = "missing"
    STALE = "stale"


@dataclass(frozen=True)
class DependencyRelation:
    """A structurally valid dependency plus its current artifact disposition."""

    dependency: DatasetDependency
    dataset_state: DependencyRelationState
    support_state: DependencyRelationState


class ManifestCheckResult(NamedTuple):
    required: tuple[str, ...]
    optional: tuple[str, ...]
    reject: tuple[str, ...]
    date_locales: dict[str, str]
    rejected_files: frozenset[str]
    dataset_dependencies: dict[str, tuple[DatasetDependency, ...]]
    dependency_relations: dict[str, tuple[DependencyRelation, ...]]


def _safe_rel(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestMismatchError(f"{field} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ManifestMismatchError(f"unsafe {field}: {value!r}")
    return path.as_posix()

def _string_list(raw: dict, field: str) -> list[str]:
    value = raw.get(field, [])
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item for item in value)
    ):
        raise ManifestMismatchError(f"{field} must be a list of non-empty strings")
    return value


def _load_intake_by_rel(study_name: str) -> dict[str, dict]:
    try:
        from phi_engine.pipeline.intake import load_intake_manifest

        manifest = load_intake_manifest(study_name)
    except Exception:
        return {}
    return {entry.get("relative_path"): entry for entry in (manifest.get("entries") or {}).values() if isinstance(entry, dict)}


def _enum(enum_type, value: object, field: str):
    try:
        return enum_type(value)
    except Exception as exc:
        raise ManifestMismatchError(f"invalid {field}: {value!r}") from exc


def _relation_state(
    entry: dict | None,
    *,
    artifact_id: str | None,
    source_sha256: str | None,
) -> DependencyRelationState:
    if entry is None:
        return DependencyRelationState.MISSING
    if (
        artifact_id is None
        or source_sha256 is None
        or entry.get("artifact_id") != artifact_id
        or entry.get("sha256") != source_sha256
    ):
        return DependencyRelationState.STALE
    return DependencyRelationState.CURRENT


def _validate_dependencies(
    raw: dict,
    study_name: str,
) -> tuple[
    dict[str, tuple[DatasetDependency, ...]],
    dict[str, tuple[DependencyRelation, ...]],
]:
    if "dataset_dependencies" not in raw:
        return {}, {}
    if raw.get("dataset_dependencies_schema") != "dataset-dependencies/v1":
        raise ManifestMismatchError("dataset_dependencies_schema must be dataset-dependencies/v1")
    if raw.get("dataset_dependencies_code_table_version") != 1:
        raise ManifestMismatchError("dataset_dependencies_code_table_version must be 1")
    deps_raw = raw.get("dataset_dependencies")
    if not isinstance(deps_raw, dict):
        raise ManifestMismatchError("dataset_dependencies must be a mapping")
    intake_by_rel = _load_intake_by_rel(study_name)
    parsed: dict[str, tuple[DatasetDependency, ...]] = {}
    relations: dict[str, tuple[DependencyRelation, ...]] = {}
    for dataset_path_raw, dep_list in deps_raw.items():
        dataset_path = _safe_rel(dataset_path_raw, field="dataset dependency key")
        if not isinstance(dep_list, list):
            raise ManifestMismatchError(f"dataset_dependencies[{dataset_path}] must be a list")
        dataset_entry = intake_by_rel.get(dataset_path)
        items: list[DatasetDependency] = []
        relation_items: list[DependencyRelation] = []
        for dep in dep_list:
            if not isinstance(dep, dict):
                raise ManifestMismatchError("dataset dependency item must be a mapping")
            unknown = set(dep) - _ALLOWED_DEP_KEYS
            if unknown:
                raise ManifestMismatchError(f"unknown dataset dependency keys: {sorted(unknown)}")
            missing = _ALLOWED_DEP_KEYS - set(dep)
            if missing:
                raise ManifestMismatchError(f"missing dataset dependency keys: {sorted(missing)}")
            support = _safe_rel(dep["support"], field="support")
            if not is_artifact_id(dep.get("dataset_artifact_id")) or not is_sha256(dep.get("dataset_source_sha256")):
                raise ManifestMismatchError("invalid dataset artifact id/hash")
            dataset_state = _relation_state(
                dataset_entry,
                artifact_id=dep["dataset_artifact_id"],
                source_sha256=dep["dataset_source_sha256"],
            )
            support_id = dep.get("support_artifact_id")
            support_sha = dep.get("support_source_sha256")
            level = _enum(DependencyLevel, dep.get("level"), "level")
            if support_id is None or support_sha is None:
                if level is DependencyLevel.IGNORED:
                    raise ManifestMismatchError("ignored dependencies require concrete support artifact id/hash")
                if support_id is not None or support_sha is not None:
                    raise ManifestMismatchError("support artifact id/hash must both be null or both concrete")
            else:
                if not is_artifact_id(support_id) or not is_sha256(support_sha):
                    raise ManifestMismatchError("invalid support artifact id/hash")
            support_state = _relation_state(
                intake_by_rel.get(support),
                artifact_id=support_id,
                source_sha256=support_sha,
            )
            basis_raw = dep.get("basis")
            if not isinstance(basis_raw, dict) or set(basis_raw) != _ALLOWED_BASIS_KEYS:
                raise ManifestMismatchError("invalid dependency basis")
            if not all(is_sha256(basis_raw[k]) for k in _ALLOWED_BASIS_KEYS):
                raise ManifestMismatchError("invalid dependency basis hashes")
            if not is_recommendation_id(dep.get("recommendation_id")):
                raise ManifestMismatchError("invalid recommendation_id")
            if not isinstance(dep.get("confirmed_by"), str) or not dep.get("confirmed_by"):
                raise ManifestMismatchError("confirmed_by is required")
            if not is_timestamp_z(dep.get("confirmed_at")):
                raise ManifestMismatchError("confirmed_at must be UTC second timestamp")
            dependency = DatasetDependency(
                dataset_path=dataset_path,
                dataset_artifact_id=dep["dataset_artifact_id"],
                dataset_source_sha256=dep["dataset_source_sha256"],
                support=support,
                support_artifact_id=support_id,
                support_source_sha256=support_sha,
                kind=_enum(DependencyKind, dep.get("kind"), "kind"),
                level=level,
                sensitivity=_enum(Sensitivity, dep.get("sensitivity"), "sensitivity"),
                reason_code=_enum(DependencyReasonCode, dep.get("reason_code"), "reason_code"),
                recommendation_id=dep["recommendation_id"],
                basis=DatasetDependencyBasis(
                    rulebook_sha256=basis_raw["rulebook_sha256"],
                    scrub_config_sha256=basis_raw["scrub_config_sha256"],
                    support_role_sha256=basis_raw["support_role_sha256"],
                ),
                confirmed_by=dep["confirmed_by"],
                confirmed_at=dep["confirmed_at"],
            )
            items.append(dependency)
            relation_items.append(
                DependencyRelation(
                    dependency=dependency,
                    dataset_state=dataset_state,
                    support_state=support_state,
                )
            )
        parsed[dataset_path] = tuple(items)
        relations[dataset_path] = tuple(relation_items)
    return parsed, relations


def check_forms_manifest(
    datasets_dir: Path | str,
    *,
    study: str | None = None,
    actual_files: Iterable[str] | None = None,
) -> ManifestCheckResult:
    """Validate *datasets_dir* against its study's forms manifest.

    ``study``, when supplied, names the manifest's owning study directly
    instead of inferring it from ``datasets_dir``'s lexical basename --
    callers that already know the study (organize/run) should always pass
    it explicitly.

    ``actual_files``, when supplied, is used verbatim as the set of
    dataset basenames present in ``datasets_dir`` instead of an ordinary
    (symlink-following) ``Path.is_dir()``/``Path.iterdir()`` scan of that
    directory -- callers holding only a descriptor-verified inventory
    (e.g. an intake manifest's already-verified ``datasets`` component
    entries) must pass this instead of a directory to read, so this
    function never performs a filesystem access on an external/hostile
    source root itself.
    """
    from phi_engine.config import config

    datasets_dir = Path(datasets_dir)
    if study is not None:
        study_name = study
    else:
        study_name = config.STUDY_NAME if Path(config.study_config_path("_forms_manifest.yaml", study=config.STUDY_NAME)).exists() else datasets_dir.parent.name
    manifest_path = config.study_config_path("_forms_manifest.yaml", study=study_name)

    if not manifest_path.exists():
        _gate_log.warning(
            "No forms manifest found at %s; extraction proceeds without form-level gate (add _forms_manifest.yaml to enable it)",
            manifest_path,
        )
        return ManifestCheckResult(required=(), optional=(), reject=(), date_locales={}, rejected_files=frozenset(), dataset_dependencies={}, dependency_relations={})

    with manifest_path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ManifestMismatchError("forms manifest must be a mapping")
    unknown = set(raw) - _ALLOWED_TOP_KEYS
    if unknown:
        raise ManifestMismatchError(f"unknown forms manifest keys: {sorted(unknown)}")

    required = _string_list(raw, "required")
    optional = _string_list(raw, "optional")
    reject = _string_list(raw, "reject")
    date_raw = raw.get("date_locales", {})
    if not isinstance(date_raw, dict):
        raise ManifestMismatchError("date_locales must be a mapping")
    date_locales: dict[str, str] = {}
    for key, value in date_raw.items():
        if not isinstance(key, str) or not key:
            raise ManifestMismatchError("date_locales keys must be non-empty strings")
        upper = key.upper()
        if value not in {"DMY", "MDY"}:
            raise ManifestMismatchError(f"invalid date locale for {upper}: {value!r}")
        date_locales[upper] = value
    dataset_dependencies, dependency_relations = _validate_dependencies(raw, study_name)

    if actual_files is not None:
        actual_files = sorted(set(actual_files))
    else:
        if not datasets_dir.is_dir():
            return ManifestCheckResult(required=tuple(required), optional=tuple(optional), reject=tuple(reject), date_locales=date_locales, rejected_files=frozenset(), dataset_dependencies=dataset_dependencies, dependency_relations=dependency_relations)

        actual_files = sorted(
            p.name
            for p in datasets_dir.iterdir()
            if p.is_file() and not p.name.startswith(".") and not p.name.startswith("~$") and p.suffix.lower() in SUPPORTED_EXTENSIONS
        )

    required_set: frozenset[str] = frozenset(required)
    optional_set: frozenset[str] = frozenset(optional)

    rejected: set[str] = set()
    for fname in actual_files:
        for pattern in reject:
            if fname == pattern or fnmatch.fnmatch(fname, pattern):
                rejected.add(fname)
                _gate_log.info("Reject-listed form auto-skipped: %s (matched pattern %r)", fname, pattern)
                break

    actual_set: frozenset[str] = frozenset(actual_files)
    for required_form in required:
        if required_form not in actual_set:
            raise ManifestMismatchError(f"required form missing: {required_form!r} not found in {datasets_dir}")
        if required_form in rejected:
            raise ManifestMismatchError(f"manifest conflict: {required_form!r} appears in both required: and reject: — fix _forms_manifest.yaml")

    for fname in actual_files:
        if fname in required_set or fname in optional_set or fname in rejected:
            continue
        # Dataset dependencies are source-relative, while this legacy file gate is basename-only.
        if f"datasets/{fname}" in dataset_dependencies:
            continue
        raise ManifestMismatchError(f"unknown form (not in manifest): {fname!r}; add to required/optional/reject in _forms_manifest.yaml")

    for opt_form in optional:
        if opt_form not in actual_set:
            _gate_log.info("Optional form not present (skipped): %s", opt_form)

    return ManifestCheckResult(required=tuple(required), optional=tuple(optional), reject=tuple(reject), date_locales=date_locales, rejected_files=frozenset(rejected), dataset_dependencies=dataset_dependencies, dependency_relations=dependency_relations)


__all__ = [
    "DependencyRelation",
    "DependencyRelationState",
    "ManifestCheckResult",
    "ManifestMismatchError",
    "check_forms_manifest",
]
