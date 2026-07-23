"""Human-review feedback loop: decision memory + review-item listing.

Persistent per-study decision store
(``<study config dir>/<study>/review_decisions.yaml``)::

    {HEADER_UPPER: {decision: keep|drop|override, action: <Action name>,
                     decided_by: str, decided_at: ISO8601, source: cli|file}}

Consumed by :func:`phi_engine.pipeline.run.run_pipeline`'s classification
step:

- ``keep``     -> ``confirmed_keep_headers`` (the existing
  ``review_form_headers`` parameter designed exactly for this: un-holds a
  PHI-risky-NAMED header the SoT/heuristic path would otherwise force-drop).
- ``drop``     -> merged into the form's ``force_drop_headers`` (removed by
  the scrubber regardless of what the name-rules alone would have decided).
- ``override`` -> the header's classification ``action`` is replaced
  (``dataclasses.replace``) BEFORE the approval JSON is written and BEFORE
  ``synthesize_study_config`` runs, so the scrubber applies the OVERRIDDEN
  method on the NEXT run.

Also fixes prior-audit M3: ``llm_detector._write_review_queue``'s
``review_queue_path`` previously had no resolved default and could be
handed a path relative to the caller's cwd; :data:`DEFAULT_LLM_QUEUE_PATH`
here resolves it through ``config.STUDY_AUDIT_DIR`` (the zone-guarded
chokepoint), and callers should pass that instead of an ad hoc path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from dataclasses import replace as _dc_replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

import phi_engine.config.config as config
from phi_engine.audit import review_paths
from phi_engine.pipeline.intake import IntakeManifestError, load_intake_manifest
from phi_engine.security.phi_review import Action, HeaderClassification
from phi_engine.security.phi_review import (
    load_study_privacy_config,
)
from phi_engine.security.phi_rulebook import resolve_rulebook
from phi_engine.pipeline import dependencies as dependency_contracts
from phi_engine.pipeline.dependencies import (
    CODE_TABLE_VERSION,
    DEPENDENCY_DECISIONS_FILENAME,
    DEPENDENCY_RECOMMENDATIONS_FILENAME,
    PRIVATE_DEPENDENCY_RECOMMENDATIONS_RELATIVE_PATH,
    DependencyDecision,
    DependencyKind,
    DependencyLevel,
    DependencyReasonCode,
    DependencyRecommendation,
    PrivateDependencyRecommendation,
    RoleSource,
    Sensitivity,
    SupportFailureCode,
    append_dependency_decision,
    is_artifact_id,
    is_recommendation_id,
    load_dependency_decisions,
    load_dependency_recommendations,
    load_private_dependency_recommendations,
    support_role_sha256,
    utc_now_z,
)
from phi_engine.utils.pipeline_lock import PipelineBusyError, pipeline_lock

__all__ = [
    "DECISIONS_FILENAME",
    "DEFAULT_LLM_QUEUE_PATH",
    "decide_dependency",
    "load_latest_dependency_recommendations",
    "load_study_dependency_decisions",
    "apply_decisions_to_classifications",
    "confirmed_keep_headers",
    "decide",
    "extra_force_drop_headers",
    "list_review_items",
    "load_review_decisions",
]

DECISIONS_FILENAME = "review_decisions.yaml"
DECISIONS_TRAIL_FILENAME = "decisions.jsonl"
_VALID_DECISIONS = frozenset({"keep", "drop", "override"})
DEPENDENCY_DECISION_DETAILS_FILENAME = "dependency_decision_details.jsonl"
PENDING_DEPENDENCY_RECOMMENDATIONS_FILENAME = (
    "pending_dependency_recommendations.jsonl"
)
_DATASET_DEPENDENCIES_SCHEMA = "dataset-dependencies/v1"
_DECIDED_BY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$")
_ORGANIZER_ROLE_VERSION = 1


def DEFAULT_LLM_QUEUE_PATH() -> Path:  # noqa: N802 -- reads as a named constant at call sites
    """Zone-guarded default for the LLM header-classifier's uncertain-queue
    path (fixes prior-audit M3: no longer relative to the caller's cwd)."""
    return review_paths.human_review_root(Path(config.STUDY_AUDIT_DIR)) / "llm_uncertain.jsonl"


def _decisions_dir(study: str) -> Path:
    return Path(config.study_config_dir(study))


def _decisions_path(study: str) -> Path:
    return _decisions_dir(study) / DECISIONS_FILENAME


def load_latest_dependency_recommendations(study: str) -> tuple[DependencyRecommendation, ...]:
    recommendations, _private_records, _run_dir = _load_latest_dependency_records(study)
    return recommendations


def load_study_dependency_decisions(study: str) -> tuple[DependencyDecision, ...]:
    return load_dependency_decisions(_decisions_dir(study) / DEPENDENCY_DECISIONS_FILENAME)


def decide_dependency(
    study: str,
    *,
    dataset: str,
    recommendation: str,
    support: str | None,
    level: str | DependencyLevel,
    sensitivity: str | Sensitivity,
    reason_code: str | DependencyReasonCode,
    detail_file: Path | None,
    decided_by: str,
) -> DependencyDecision:
    if not is_artifact_id(dataset):
        raise ValueError("invalid dataset identity")
    if not is_recommendation_id(recommendation):
        raise ValueError("invalid recommendation identity")
    if support is not None and not is_artifact_id(support):
        raise ValueError("invalid support identity")
    if not isinstance(decided_by, str) or not _DECIDED_BY_RE.fullmatch(decided_by):
        raise ValueError("invalid decided_by identifier")
    selected_level = _dependency_enum(DependencyLevel, level, "level")
    selected_sensitivity = _dependency_enum(Sensitivity, sensitivity, "sensitivity")
    selected_reason = _dependency_enum(DependencyReasonCode, reason_code, "reason_code")
    detail = _read_private_detail(detail_file)

    try:
        with pipeline_lock(study):
            return _decide_dependency_locked(
                study,
                dataset=dataset,
                recommendation=recommendation,
                support=support,
                level=selected_level,
                sensitivity=selected_sensitivity,
                reason_code=selected_reason,
                detail=detail,
                decided_by=decided_by,
            )
    except PipelineBusyError:
        raise ValueError("study pipeline lock is busy") from None
    except OSError:
        raise ValueError("study pipeline lock infrastructure failure") from None
    except RuntimeError:
        raise ValueError("study pipeline lock infrastructure failure") from None


def _decide_dependency_locked(
    study: str,
    *,
    dataset: str,
    recommendation: str,
    support: str | None,
    level: DependencyLevel,
    sensitivity: Sensitivity,
    reason_code: DependencyReasonCode,
    detail: str | None,
    decided_by: str,
) -> DependencyDecision:
    recommendations, private_records, _run_dir = _load_latest_dependency_records(study)
    recommendations_by_id = _records_by_recommendation_id(
        recommendations, "recommendations"
    )
    private_by_id = _records_by_recommendation_id(
        private_records, "private recommendations"
    )
    current = recommendations_by_id.get(recommendation)
    private = private_by_id.get(recommendation)
    if current is None or private is None or current.dataset_artifact_id != dataset:
        raise ValueError("dataset/recommendation identity mismatch")
    if (
        private.dataset_artifact_id != current.dataset_artifact_id
        or private.support_artifact_id != current.support_artifact_id
        or private.basis != current.basis
    ):
        raise ValueError("stale protected recommendation identity")
    if support != current.support_artifact_id:
        raise ValueError("support identity mismatch")
    if reason_code is not current.reason_code:
        raise ValueError("reason code mismatch")
    if private.organizer_role_version != _ORGANIZER_ROLE_VERSION:
        raise ValueError("stale recommendation role")

    manifest_path = _decisions_dir(study) / "_forms_manifest.yaml"
    manifest = _load_forms_manifest_for_update(manifest_path)
    if current.support_artifact_id is None:
        if level is DependencyLevel.IGNORED:
            raise ValueError("missing support cannot be ignored")
        if sensitivity is Sensitivity.NON_CONFIDENTIAL:
            raise ValueError(
                "non_confidential sensitivity requires parsed support"
            )
        if not _is_manifest_declared_missing_support(
            manifest, current, private
        ):
            raise ValueError(
                "missing support requires a manifest-declared expected support path"
            )
    elif (
        sensitivity is Sensitivity.NON_CONFIDENTIAL
        and current.normalized_support_sha256 is None
    ):
        raise ValueError("non_confidential sensitivity requires parsed support")

    organized_root = Path(config.ORGANIZED_DIR) / study
    support_is_parsed = _verify_current_dependency_identity(
        organized_root, current, private
    )
    if sensitivity is Sensitivity.NON_CONFIDENTIAL and not support_is_parsed:
        raise ValueError("non_confidential sensitivity requires parsed support")

    role_hash = support_role_sha256(
        recommendation_id=current.recommendation_id,
        dataset_artifact_id=current.dataset_artifact_id,
        support_artifact_id=current.support_artifact_id,
        kind=current.kind,
        role_source=private.role_source,
        organizer_role_version=private.organizer_role_version,
    )
    current_rulebook_sha256 = _current_rulebook_sha256(study)
    current_scrub_sha256 = _current_scrub_config_sha256()
    if (
        role_hash != current.basis.support_role_sha256
        or current_scrub_sha256 != current.basis.scrub_config_sha256
        or current_rulebook_sha256 != current.basis.rulebook_sha256
    ):
        raise ValueError("stale recommendation basis")

    decision = DependencyDecision(
        schema_version="dependency-decision/v1",
        decision_id="dd_" + secrets.token_hex(16),
        recommendation_id=current.recommendation_id,
        dataset_artifact_id=current.dataset_artifact_id,
        dataset_sha256=current.dataset_sha256,
        support_artifact_id=current.support_artifact_id,
        support_sha256=current.support_sha256,
        normalized_support_sha256=current.normalized_support_sha256,
        kind=current.kind,
        level=level,
        sensitivity=sensitivity,
        reason_code=reason_code,
        basis=current.basis,
        decided_by=decided_by,
        decided_at=utc_now_z(),
    )
    dependencies = manifest.get("dataset_dependencies")
    if dependencies is None:
        dependencies = {}
    if not isinstance(dependencies, dict):
        raise ValueError("dataset_dependencies must be a mapping")
    existing = dependencies.get(private.dataset_path, [])
    if not isinstance(existing, list):
        raise ValueError("dataset dependency state must be a list")
    updated = [
        item
        for item in existing
        if not isinstance(item, dict)
        or (
            item.get("recommendation_id") != current.recommendation_id
            and not (
                item.get("support") == private.support_path
                and item.get("kind") == current.kind.value
            )
        )
    ]
    updated.append(_manifest_dependency_record(current, private, decision))
    dependencies[private.dataset_path] = updated
    manifest["dataset_dependencies_schema"] = _DATASET_DEPENDENCIES_SCHEMA
    manifest["dataset_dependencies_code_table_version"] = CODE_TABLE_VERSION
    manifest["dataset_dependencies"] = dependencies

    private_decision_record = {
        "schema_version": "dependency-decision-private/v1",
        "decision_id": decision.decision_id,
        "recommendation_id": decision.recommendation_id,
        "dataset_path": private.dataset_path,
        "support_path": private.support_path,
        "raw_header_names": list(private.raw_header_names),
        "detail": detail,
    }
    _serialize_json_record(private_decision_record)
    _persist_dependency_decision(
        manifest_path=manifest_path,
        manifest=manifest,
        decision_path=_decisions_dir(study) / DEPENDENCY_DECISIONS_FILENAME,
        decision=decision,
        private_path=(
            _decisions_dir(study) / DEPENDENCY_DECISION_DETAILS_FILENAME
        ),
        private_record=private_decision_record,
    )
    return decision


def _load_latest_dependency_records(
    study: str,
) -> tuple[
    tuple[DependencyRecommendation, ...],
    tuple[PrivateDependencyRecommendation, ...],
    Path,
]:
    runs_dir = Path(config.STUDY_OUTPUT_DIR) / "runs"
    if not runs_dir.is_dir():
        raise ValueError("no verified dependency recommendation run is available")
    run_dirs = sorted(
        path for path in runs_dir.iterdir()
        if path.is_dir() and not path.is_symlink()
    )
    if not run_dirs:
        raise ValueError("no verified dependency recommendation run is available")
    run_dir = run_dirs[-1]
    recommendations = load_dependency_recommendations(
        run_dir / DEPENDENCY_RECOMMENDATIONS_FILENAME
    )
    private_records = load_private_dependency_recommendations(
        run_dir / PRIVATE_DEPENDENCY_RECOMMENDATIONS_RELATIVE_PATH
    )
    ordinary_ids = _records_by_recommendation_id(recommendations, "recommendations")
    private_ids = _records_by_recommendation_id(private_records, "private recommendations")
    if set(ordinary_ids) != set(private_ids):
        raise ValueError("ordinary/private recommendation identities mismatch")
    return recommendations, private_records, run_dir


def _records_by_recommendation_id(records: tuple[Any, ...], label: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for record in records:
        recommendation_id = record.recommendation_id
        if recommendation_id in result:
            raise ValueError(f"duplicate identity in {label}")
        result[recommendation_id] = record
    return result


def _dependency_enum(enum_type: type[Any], value: object, field: str) -> Any:
    try:
        return value if isinstance(value, enum_type) else enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}") from exc


def _current_rulebook_sha256(study: str) -> str:
    try:
        privacy = load_study_privacy_config(Path(config.RAW_DATA_DIR) / study)
        value = resolve_rulebook(
            privacy, allow_network=privacy.rule_refresh == "online_preferred"
        ).bundle.rules_sha256
    except Exception as exc:
        raise ValueError("current rulebook is unavailable") from exc
    if not dependency_contracts.is_sha256(value):
        raise ValueError("current rulebook is unavailable")
    return value


def _current_scrub_config_sha256() -> str:
    try:
        value = dependency_contracts._effective_scrub_config_sha256()
    except Exception as exc:
        raise ValueError("current scrub config is unavailable") from exc
    if not dependency_contracts.is_sha256(value):
        raise ValueError("current scrub config is unavailable")
    return value


def _is_manifest_declared_missing_support(
    manifest: dict[str, Any],
    recommendation: DependencyRecommendation,
    private: PrivateDependencyRecommendation,
) -> bool:
    if (
        recommendation.support_artifact_id is not None
        or recommendation.support_sha256 is not None
        or recommendation.normalized_support_sha256 is not None
        or recommendation.reason_code is not DependencyReasonCode.MANIFEST_DECLARED
        or private.role_source is not RoleSource.MANIFEST
        or private.support_path is None
    ):
        return False
    dependencies = manifest.get("dataset_dependencies")
    if not isinstance(dependencies, dict):
        return False
    records = dependencies.get(private.dataset_path)
    if not isinstance(records, list):
        return False
    return any(
        isinstance(record, dict)
        and record.get("dataset_artifact_id")
        == recommendation.dataset_artifact_id
        and record.get("dataset_source_sha256") == recommendation.dataset_sha256
        and record.get("support") == private.support_path
        and record.get("kind") == recommendation.kind.value
        and record.get("reason_code")
        == DependencyReasonCode.MANIFEST_DECLARED.value
        and record.get("recommendation_id")
        == recommendation.recommendation_id
        for record in records
    )


def _read_private_detail(path: Path | None) -> str | None:
    if path is None:
        return None
    path = Path(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("detail file is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("detail file must be a regular mode-0600 file")
        content = os.read(descriptor, 1_048_577)
        if len(content) > 1_048_576:
            raise ValueError("detail file exceeds the private detail size limit")
        try:
            return content.decode("utf-8")
        except UnicodeError as exc:
            raise ValueError("detail file must be UTF-8") from exc
    finally:
        os.close(descriptor)


def _verify_current_dependency_identity(
    organized_root: Path,
    recommendation: DependencyRecommendation,
    private: PrivateDependencyRecommendation,
) -> bool:
    dataset_metadata = _load_private_json(
        organized_root
        / ".protected"
        / "headers"
        / f"{recommendation.dataset_artifact_id}.json",
        {
            "artifact_id",
            "source_sha256",
            "headers",
            "source_relative_path",
        },
        "dataset metadata",
    )
    if (
        dataset_metadata["artifact_id"] != recommendation.dataset_artifact_id
        or dataset_metadata["source_sha256"] != recommendation.dataset_sha256
        or dataset_metadata["source_relative_path"] != private.dataset_path
        or _sha256_file(
            organized_root
            / ".verified_sources"
            / recommendation.dataset_artifact_id,
            "dataset source",
        )
        != recommendation.dataset_sha256
    ):
        raise ValueError("stale dataset identity")

    if recommendation.support_artifact_id is None:
        return False
    support_metadata = _load_private_json(
        organized_root
        / ".protected"
        / "support"
        / f"{recommendation.support_artifact_id}.json",
        {
            "artifact_id",
            "source_sha256",
            "kind",
            "format",
            "parse_status",
            "normalized_rows_sha256",
            "failure_code",
            "normalized_rows_path",
            "source_relative_path",
            "normalized_source_stem",
        },
        "support metadata",
    )
    if (
        support_metadata["artifact_id"] != recommendation.support_artifact_id
        or support_metadata["source_sha256"] != recommendation.support_sha256
        or support_metadata["kind"] != recommendation.kind.value
        or support_metadata["source_relative_path"] != private.support_path
        or _sha256_file(
            organized_root
            / ".verified_sources"
            / recommendation.support_artifact_id,
            "support source",
        )
        != recommendation.support_sha256
    ):
        raise ValueError("stale support identity")

    parse_status = support_metadata["parse_status"]
    if parse_status == "failed":
        failure_code = support_metadata["failure_code"]
        if (
            recommendation.normalized_support_sha256 is not None
            or support_metadata["normalized_rows_sha256"] is not None
            or support_metadata["normalized_rows_path"] is not None
            or not isinstance(failure_code, str)
        ):
            raise ValueError("stale support identity")
        try:
            SupportFailureCode(failure_code)
        except ValueError as exc:
            raise ValueError("stale support identity") from exc
        return False

    if parse_status != "parsed":
        raise ValueError("stale support identity")
    normalized_path_value = support_metadata["normalized_rows_path"]
    if (
        recommendation.normalized_support_sha256 is None
        or support_metadata["normalized_rows_sha256"]
        != recommendation.normalized_support_sha256
        or support_metadata["failure_code"] is not None
        or not isinstance(normalized_path_value, str)
    ):
        raise ValueError("stale support identity")
    normalized_path = Path(normalized_path_value)
    try:
        normalized_path.resolve(strict=True).relative_to(
            organized_root.resolve(strict=True)
        )
    except (OSError, ValueError) as exc:
        raise ValueError("stale support identity") from exc
    if (
        _sha256_file(normalized_path, "normalized support")
        != recommendation.normalized_support_sha256
    ):
        raise ValueError("stale support identity")
    return True


def _load_private_json(path: Path, keys: set[str], label: str) -> dict[str, Any]:
    path = Path(path)
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError(f"{label} must be a regular mode-0600 file")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{label} has duplicate keys")
            result[key] = value
        return result

    try:
        payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != keys:
        raise ValueError(f"{label} schema mismatch")
    return payload


def _sha256_file(path: Path, label: str) -> str:
    path = Path(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    digest = hashlib.sha256()
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{label} is unavailable")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _manifest_dependency_record(
    recommendation: DependencyRecommendation,
    private: PrivateDependencyRecommendation,
    decision: DependencyDecision,
) -> dict[str, Any]:
    return {
        "dataset_artifact_id": recommendation.dataset_artifact_id,
        "dataset_source_sha256": recommendation.dataset_sha256,
        "support": private.support_path,
        "support_artifact_id": recommendation.support_artifact_id,
        "support_source_sha256": recommendation.support_sha256,
        "kind": recommendation.kind.value,
        "level": decision.level.value,
        "sensitivity": decision.sensitivity.value,
        "reason_code": recommendation.reason_code.value,
        "recommendation_id": recommendation.recommendation_id,
        "basis": recommendation.basis.to_json(),
        "confirmed_by": decision.decided_by,
        "confirmed_at": decision.decided_at,
    }


class _UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError("forms manifest has duplicate keys")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_forms_manifest_for_update(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        payload = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeySafeLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError("forms manifest is invalid") from exc
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("forms manifest must be a mapping")
    return payload


def _atomic_write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    serialized = yaml.safe_dump(payload, sort_keys=False)
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
            os.fchmod(handle.fileno(), 0o600)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _serialize_json_record(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"


def _append_private_jsonl(path: Path, record: dict[str, Any]) -> None:
    serialized = _serialize_json_record(record)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _persist_dependency_decision(
    *,
    manifest_path: Path,
    manifest: dict[str, Any],
    decision_path: Path,
    decision: DependencyDecision,
    private_path: Path,
    private_record: dict[str, Any],
) -> None:
    manifest_snapshot = _snapshot_complete_file(manifest_path, "forms manifest")
    decision_snapshot = _snapshot_append_file(
        decision_path, "dependency decision trail"
    )
    private_snapshot = _snapshot_append_file(
        private_path, "private dependency decision trail"
    )
    manifest_mutated = False
    decision_attempted = False
    private_attempted = False
    try:
        _atomic_write_yaml(manifest_path, manifest)
        manifest_mutated = True
        decision_attempted = True
        append_dependency_decision(decision_path, decision)
        private_attempted = True
        _append_private_jsonl(private_path, private_record)
    except Exception as exc:
        recovery_failed = False
        if private_attempted:
            try:
                _restore_append_file(private_path, private_snapshot)
            except Exception:
                recovery_failed = True
        if decision_attempted:
            try:
                _restore_append_file(decision_path, decision_snapshot)
            except Exception:
                recovery_failed = True
        if manifest_mutated:
            try:
                _restore_complete_file(manifest_path, manifest_snapshot)
            except Exception:
                recovery_failed = True
        if recovery_failed:
            raise ValueError("dependency decision recovery failed") from exc
        raise ValueError("dependency decision persistence failed") from exc


def _snapshot_complete_file(
    path: Path,
    label: str,
) -> tuple[bool, bytes, int]:
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False, b"", 0o600
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label} is unavailable")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    return True, content, stat.S_IMODE(info.st_mode)


def _snapshot_append_file(
    path: Path,
    label: str,
) -> tuple[bool, int, int]:
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False, 0, 0o600
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label} is unavailable")
    return True, info.st_size, stat.S_IMODE(info.st_mode)


def _restore_complete_file(
    path: Path,
    snapshot: tuple[bool, bytes, int],
) -> None:
    existed, content, mode = snapshot
    if existed:
        _atomic_write_bytes(path, content, mode)
    else:
        Path(path).unlink(missing_ok=True)


def _restore_append_file(
    path: Path,
    snapshot: tuple[bool, int, int],
) -> None:
    existed, size, mode = snapshot
    path = Path(path)
    if not existed:
        path.unlink(missing_ok=True)
        return
    flags = os.O_WRONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("decision trail is not a regular file")
        os.ftruncate(descriptor, size)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, content: bytes, mode: int) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            os.fchmod(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def load_review_decisions(study: str) -> dict[str, dict[str, Any]]:
    """Return ``{HEADER_UPPER: {decision, action, decided_by, decided_at, source}}``."""
    path = _decisions_path(study)
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k).upper(): dict(v) for k, v in data.items() if isinstance(v, dict)}


def decide(
    study: str,
    *,
    header: str,
    decision: str,
    action: str | None = None,
    decided_by: str = "cli",
    source: str = "cli",
) -> Path:
    """Record one review decision (non-interactive, scriptable).

    Idempotent overwrite semantics: a repeat call for the same header
    replaces the prior decision -- the store holds CURRENT state, applied on
    the NEXT pipeline run. Every call also appends to the append-only
    ``decisions.jsonl`` audit trail.
    """
    if decision not in _VALID_DECISIONS:
        raise ValueError(f"decision must be one of {sorted(_VALID_DECISIONS)}, got {decision!r}")
    if decision == "override" and not action:
        raise ValueError("decision='override' requires an explicit --action")
    valid_actions = {a.value for a in Action}
    if action is not None and action not in valid_actions:
        raise ValueError(f"unknown action {action!r}; must be one of {sorted(valid_actions)}")

    decisions = load_review_decisions(study)
    record = {
        "decision": decision,
        "action": action,
        "decided_by": decided_by,
        "decided_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
    }
    decisions[header.upper()] = record

    out_dir = _decisions_dir(study)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / DECISIONS_FILENAME
    path.write_text(yaml.safe_dump(decisions, sort_keys=True), encoding="utf-8")

    trail_path = out_dir / DECISIONS_TRAIL_FILENAME
    with trail_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"header": header.upper(), **record}, sort_keys=True) + "\n")
    return path


def confirmed_keep_headers(decisions: dict[str, dict[str, Any]]) -> frozenset[str]:
    """Headers with a ``keep`` decision -- feeds
    ``review_form_headers(confirmed_keep_headers=...)``."""
    return frozenset(h for h, d in decisions.items() if d.get("decision") == "keep")


def extra_force_drop_headers(decisions: dict[str, dict[str, Any]]) -> frozenset[str]:
    """Headers with a ``drop`` decision -- merged into a form's
    ``force_drop_headers`` regardless of the header's own rule-based action."""
    return frozenset(h for h, d in decisions.items() if d.get("decision") == "drop")


def apply_decisions_to_classifications(
    classifications: tuple[HeaderClassification, ...],
    decisions: dict[str, dict[str, Any]],
) -> tuple[HeaderClassification, ...]:
    """Apply ``override`` decisions to a form's classification tuple.

    ``keep``/``drop`` decisions are threaded through
    ``review_form_headers``'s ``confirmed_keep_headers`` param / a
    ``force_drop_headers`` merge instead (see :func:`confirmed_keep_headers`
    / :func:`extra_force_drop_headers`) -- only ``override`` needs a
    classification MUTATION, because it is the only decision kind that names
    a DIFFERENT applied method (read by both the approval JSON and
    ``synthesize_study_config``).
    """
    updated: list[HeaderClassification] = []
    for item in classifications:
        entry = decisions.get(item.header.upper())
        if entry and entry.get("decision") == "override" and entry.get("action"):
            new_action = Action(entry["action"])
            if new_action != item.action:
                updated.append(
                    _dc_replace(
                        item,
                        action=new_action,
                        matched_rules=(*item.matched_rules, "review_decision_override"),
                        reasons=(f"review-decision override -> {new_action.value}",),
                    )
                )
                continue
        updated.append(item)
    return tuple(updated)


def _intake_review_items(study: str) -> list[dict[str, Any]]:
    """Redacted intake-manifest/v3 review items for *study*: ``artifact_id``
    (when present), ``reason``, ``blocking``, ``detail`` (when present), and
    a fixed ``source: "intake"`` marker -- never the manifest's protected
    ``path`` or, for ``study-name-conflict``, its ``candidates``. Missing or
    invalid intake is tolerated here (empty list); unlike organize/run,
    which fail closed on the same condition."""
    try:
        manifest = load_intake_manifest(study)
    except IntakeManifestError:
        return []
    redacted: list[dict[str, Any]] = []
    for item in manifest.get("review_items", []):
        record: dict[str, Any] = {}
        if "artifact_id" in item:
            record["artifact_id"] = item["artifact_id"]
        record["reason"] = item["reason"]
        record["blocking"] = item["blocking"]
        if "detail" in item:
            record["detail"] = item["detail"]
        record["source"] = "intake"
        redacted.append(record)
    return redacted


def list_review_items(study: str) -> dict[str, Any]:
    """Everything currently awaiting human review for *study*:

    (a) the organizer review bucket, (b) header holds from the latest run's
    approval JSON, (c) the LLM header-classifier's uncertain-queue entries
    (when that optional path is in use), plus the decisions already on file.
    """
    audit_dir = Path(config.STUDY_AUDIT_DIR)

    organizer_bucket: list[dict[str, Any]] = []
    organizer_path = review_paths.organizer_review_path(audit_dir)
    if organizer_path.is_file():
        for line in organizer_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                organizer_bucket.append(json.loads(line))

    held_forms: list[dict[str, Any]] = []
    runs_dir = Path(config.STUDY_OUTPUT_DIR) / "runs"
    if runs_dir.is_dir():
        run_ids = sorted(p.name for p in runs_dir.iterdir() if p.is_dir())
        if run_ids:
            approval_path = runs_dir / run_ids[-1] / "phi_handling_approval.json"
            if approval_path.is_file():
                approval = json.loads(approval_path.read_text(encoding="utf-8"))
                for form in approval.get("forms", []):
                    if form.get("status") == "held":
                        held_forms.append(
                            {"form_name": form.get("form_name"), "reasons": form.get("reasons", [])}
                        )

    dependency_recommendations: list[dict[str, Any]] = []
    if runs_dir.is_dir():
        run_dirs = sorted(
            path
            for path in runs_dir.iterdir()
            if path.is_dir() and not path.is_symlink()
        )
        if run_dirs:
            run_dir = run_dirs[-1]
            recommendation_path = (
                run_dir / DEPENDENCY_RECOMMENDATIONS_FILENAME
            )
            if recommendation_path.is_file():
                ordinary = load_dependency_recommendations(
                    recommendation_path
                )
                selected = ordinary
                pending_path = (
                    run_dir
                    / PENDING_DEPENDENCY_RECOMMENDATIONS_FILENAME
                )
                if pending_path.is_file():
                    pending = load_dependency_recommendations(pending_path)
                    ordinary_by_id = _records_by_recommendation_id(
                        ordinary, "recommendations"
                    )
                    pending_by_id = _records_by_recommendation_id(
                        pending, "pending recommendations"
                    )
                    if any(
                        ordinary_by_id.get(recommendation_id)
                        != recommendation
                        for recommendation_id, recommendation
                        in pending_by_id.items()
                    ):
                        raise ValueError(
                            "pending dependency recommendation mismatch"
                        )
                    selected = pending
                dependency_recommendations = [
                    recommendation.to_json()
                    for recommendation in sorted(
                        selected,
                        key=lambda item: item.recommendation_id,
                    )
                ]

    llm_uncertain: list[dict[str, Any]] = []
    queue_path = DEFAULT_LLM_QUEUE_PATH()
    if queue_path.is_file():
        for line in queue_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                llm_uncertain.append(json.loads(line))

    return {
        "study": study,
        "organizer_review_bucket": organizer_bucket,
        "held_forms": held_forms,
        "llm_uncertain_queue": llm_uncertain,
        "dependency_recommendations": dependency_recommendations,
        "intake_review_items": _intake_review_items(study),
        "decisions_on_file": load_review_decisions(study),
    }
