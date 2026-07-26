"""Standalone PHI pipeline driver: organize -> classify -> scrub -> publish.

``python -m phi_engine run --study S --jurisdiction us [--workspace W]``

Steps (mirrors the evidence plan's Phase 2 step 10 a-i):
    a. Re-organize if the intake manifest changed since the last organize.
    b. Stage approved forms' organized JSONL into ``tmp/<study>/datasets/``
       (the shape ``phi_scrub.run_scrub`` requires), clearing the stale
       sentinel + quarantine exactly as the demoted harness driver used to.
    c. Classify every form's headers (metadata only -- header NAMES read via
       ``rows[0].keys()``, never a value) through the pinned-rule engine
       (``phi_review.review_form_headers``).
    d. Write ``phi_handling_approval.json`` in the exact shape
       ``phi_scrub._load_approval_classifications`` parses.
    e. Held forms are excluded from scrub staging; a value-free review note
       is written per held form.
    f. Scrub approved forms (``phi_scrub.run_scrub(partial_on_review=True)``).
    g. Residual PHI guard gate over the staging tree.
    h. Publish: move scrubbed JSONL to ``output/<study>/llm_source/datasets/``
       only when the guard passes.
    i. Write ``pipeline_result.json``.  Exit codes: 0 clean, 8 partial (held
       forms or a non-empty review queue), 5 guard failure, 1 scrub raised,
       2 config/input error.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
from dataclasses import asdict, dataclass, field
from dataclasses import replace as _dc_replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import phi_engine.config.config as config
from phi_engine.audit import review_paths
from phi_engine.pipeline.intake import IntakeManifestError, load_intake_manifest
from phi_engine.pipeline.organize import _organize_locked
from phi_engine.pipeline.profile import profile_column
from phi_engine.pipeline.dependencies import (
    DependencyDecision,
    ORGANIZER_ROLE_VERSION,
    DependencyDecisionBasis,
    DependencyKind,
    DependencyLevel,
    DependencyReasonCode,
    DependencyRecommendation,
    OrganizedDataset,
    OrganizedHeader,
    ParsedSupportArtifact,
    PrivateDependencyRecommendation,
    Sensitivity,
    RoleSource,
    StructuredTransformKind,
    SupportFailureCode,
    SupportParseStatus,
    TransformRequirement,
    TransformRequirementOrigin,
    canonical_sha256,
    dependency_decision_is_current,
    recommendation_identity,
    recommend_dependencies,
    support_role_sha256,
    write_dependency_recommendations,
)
from phi_engine.pipeline.review import (
    apply_decisions_to_classifications,
    confirmed_keep_headers,
    extra_force_drop_headers,
    load_review_decisions,
    load_study_dependency_decisions,
)
from phi_engine.pipeline.synthesize_config import bootstrap_study_privacy, synthesize_study_config
from phi_engine.pipeline.support_policy import (
    apply_support_signal_actions,
    build_transform_maps_from_support,
    extract_support_signals,
)
from phi_engine.security import phi_scrub
from phi_engine.security.phi_guard_gate import run_phi_guard_gate
from phi_engine.security.phi_review import (
    Action,
    FormReviewApproval,
    HeaderClassification,
    load_sot_variable_signals,
    load_study_privacy_config,
    review_form_headers,
)
from phi_engine.security.phi_rulebook import (
    RulebookUnavailableError,
    resolve_rulebook,
)
from scripts.extraction.forms_manifest import (
    DependencyRelation,
    DependencyRelationState,
)
from phi_engine.utils.pipeline_lock import (
    PipelineBusyError,
    acquire_pipeline_lock,
    lock_path_for,
    release_pipeline_lock,
)

__all__ = ["PipelineResult", "run_pipeline"]

_JURISDICTION_LABELS = {"us": "USA"}
_PENDING_DEPENDENCY_RECOMMENDATIONS_FILENAME = (
    "pending_dependency_recommendations.jsonl"
)


@dataclass
class PipelineResult:
    study: str
    jurisdiction: str
    run_id: str | None
    exit_code: int
    message: str
    forms_processed: list[str] = field(default_factory=list)
    forms_held: list[str] = field(default_factory=list)
    review_queue_size: int = 0
    organizer_review_count: int = 0
    guard_ok: bool | None = None
    guard_failed: bool = False
    scrub_raised: str | None = None
    scrub_config_hash: str | None = None
    rulebook_sha256: str | None = None
    rulebook_cache_status: str | None = None
    rulebook_source_mode: str | None = None
    published_count: int = 0
    profile_escalations: int = 0
    profile_auto_clears: int = 0
    dependency_review_count: int = 0
    dependency_held_dataset_ids: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _read_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _write_jsonl_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def _raw_rows_from_organized(organized_root: Path, entry: dict[str, Any], rows: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    artifact_id = entry.get("artifact_id")
    protected_path = organized_root / ".protected" / "headers" / f"{artifact_id}.json"
    if not protected_path.is_file():
        headers = list(rows[0].keys()) if rows else []
        return headers, rows
    protected = _load_mode_0600_json(
        protected_path,
        {"artifact_id", "source_sha256", "headers", "source_relative_path"},
        "protected dataset metadata",
    )
    if protected["artifact_id"] != artifact_id:
        raise ValueError("protected dataset identity mismatch")
    ordered = sorted(
        _parse_protected_headers(protected["headers"]),
        key=lambda item: item.column_index,
    )
    headers = [item.raw_name for item in ordered]
    id_to_raw = {
        item.header_id: item.raw_name
        for item in ordered
    }
    raw_rows = [
        {id_to_raw.get(key, key): value for key, value in row.items()}
        for row in rows
    ]
    return headers, raw_rows


@dataclass(frozen=True)
class _HydratedDependencyInputs:
    datasets: tuple[OrganizedDataset, ...]
    support_artifacts: tuple[ParsedSupportArtifact, ...]
    dataset_paths_by_id: Mapping[str, str]
    support_paths_by_id: Mapping[str, str]
    dataset_outputs_by_id: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class _DependencyDisposition:
    held_dataset_ids: frozenset[str]
    review_recommendation_ids: frozenset[str]


def _write_pending_dependency_recommendations(
    run_dir: Path,
    recommendations: tuple[DependencyRecommendation, ...],
    pending_ids: frozenset[str],
) -> Path:
    recommendations_by_id = {
        recommendation.recommendation_id: recommendation
        for recommendation in recommendations
    }
    if len(recommendations_by_id) != len(recommendations):
        raise ValueError("duplicate dependency recommendation identity")
    unknown = pending_ids - recommendations_by_id.keys()
    if unknown:
        raise ValueError("pending dependency recommendation is unavailable")
    path = (
        Path(run_dir)
        / _PENDING_DEPENDENCY_RECOMMENDATIONS_FILENAME
    )
    _write_jsonl_rows(
        path,
        [
            recommendations_by_id[recommendation_id].to_json()
            for recommendation_id in sorted(pending_ids)
        ],
    )
    path.chmod(0o600)
    return path


def _sha256_regular_file(path: Path, label: str) -> str:
    path = Path(path)
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label} must be a regular file")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_mode_0600_json(path: Path, expected_keys: set[str], label: str) -> dict[str, Any]:
    path = Path(path)
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise ValueError(f"{label} must be a regular mode-0600 file")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{label} has duplicate keys")
            result[key] = value
        return result

    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicates,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise ValueError(f"{label} schema mismatch")
    return payload


def _parse_protected_headers(raw_headers: object) -> tuple[OrganizedHeader, ...]:
    if not isinstance(raw_headers, list):
        raise ValueError("protected dataset headers must be a list")
    protected_header_keys = {
        "header_id",
        "column_index",
        "raw_name",
        "normalized_name",
    }
    headers: list[OrganizedHeader] = []
    for item in raw_headers:
        if not isinstance(item, Mapping) or set(item) != protected_header_keys:
            raise ValueError("protected dataset header schema mismatch")
        if (
            not isinstance(item["header_id"], str)
            or not isinstance(item["column_index"], int)
            or isinstance(item["column_index"], bool)
            or not isinstance(item["raw_name"], str)
            or not isinstance(item["normalized_name"], str)
        ):
            raise ValueError("protected dataset header types mismatch")
        headers.append(
            OrganizedHeader(
                header_id=item["header_id"],
                column_index=item["column_index"],
                raw_name=item["raw_name"],
                normalized_name=item["normalized_name"],
            )
        )
    return tuple(headers)


def _hydrate_dependency_inputs(
    organized_root: Path,
    organize_manifest: Mapping[str, Any],
) -> _HydratedDependencyInputs:
    organized_root = Path(organized_root)
    datasets: list[OrganizedDataset] = []
    dataset_paths: dict[str, str] = {}
    dataset_outputs: dict[str, list[str]] = {}
    for entry in organize_manifest.get("datasets", ()):
        if not isinstance(entry, Mapping):
            raise ValueError("organized dataset metadata must be a mapping")
        artifact_id = str(entry.get("artifact_id", ""))
        protected = _load_mode_0600_json(
            organized_root / ".protected" / "headers" / f"{artifact_id}.json",
            {"artifact_id", "source_sha256", "headers", "source_relative_path"},
            "protected dataset metadata",
        )
        if protected["artifact_id"] != artifact_id:
            raise ValueError("protected dataset identity mismatch")
        headers = _parse_protected_headers(protected["headers"])
        public_headers = entry.get("headers")
        if not isinstance(public_headers, list) or [
            {
                "header_id": header.header_id,
                "column_index": header.column_index,
                "normalized_name": header.normalized_name,
            }
            for header in headers
        ] != public_headers:
            raise ValueError("public/protected dataset header mismatch")
        source_sha = str(entry.get("source_sha256", ""))
        if protected["source_sha256"] != source_sha:
            raise ValueError("protected dataset source mismatch")
        if (
            _sha256_regular_file(
                organized_root / ".verified_sources" / artifact_id,
                "verified dataset source",
            )
            != source_sha
        ):
            raise ValueError("verified dataset source hash mismatch")
        output = str(entry.get("output", ""))
        normalized_path = organized_root / "datasets" / output
        normalized_sha = str(entry.get("normalized_rows_sha256", ""))
        if _sha256_regular_file(normalized_path, "organized dataset rows") != normalized_sha:
            raise ValueError("organized dataset rows hash mismatch")
        dataset = OrganizedDataset(
            artifact_id=artifact_id,
            source_sha256=source_sha,
            normalized_rows_path=normalized_path,
            normalized_rows_sha256=normalized_sha,
            headers=headers,
        )
        prior_path = dataset_paths.setdefault(
            artifact_id,
            str(protected["source_relative_path"]),
        )
        if prior_path != protected["source_relative_path"]:
            raise ValueError("dataset artifact has conflicting protected paths")
        dataset_outputs.setdefault(artifact_id, []).append(output)
        datasets.append(dataset)

    support_artifacts: list[ParsedSupportArtifact] = []
    support_paths: dict[str, str] = {}
    for public in organize_manifest.get("support_artifacts", ()):
        if not isinstance(public, Mapping):
            raise ValueError("organized support metadata must be a mapping")
        artifact_id = str(public.get("artifact_id", ""))
        protected = _load_mode_0600_json(
            organized_root / ".protected" / "support" / f"{artifact_id}.json",
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
            "protected support metadata",
        )
        if {
            key: protected[key]
            for key in (
                "artifact_id",
                "source_sha256",
                "kind",
                "format",
                "parse_status",
                "normalized_rows_sha256",
                "failure_code",
            )
        } != dict(public):
            raise ValueError("public/protected support metadata mismatch")
        source_sha = str(protected["source_sha256"])
        if (
            _sha256_regular_file(
                organized_root / ".verified_sources" / artifact_id,
                "verified support source",
            )
            != source_sha
        ):
            raise ValueError("verified support source hash mismatch")
        normalized_value = protected["normalized_rows_path"]
        normalized_path = Path(normalized_value) if isinstance(normalized_value, str) else None
        normalized_sha_value = protected["normalized_rows_sha256"]
        if normalized_path is not None:
            try:
                normalized_path.resolve(strict=True).relative_to(
                    organized_root.resolve(strict=True)
                )
            except (OSError, ValueError) as exc:
                raise ValueError("normalized support path escapes organized root") from exc
            if (
                not isinstance(normalized_sha_value, str)
                or _sha256_regular_file(normalized_path, "normalized support rows")
                != normalized_sha_value
            ):
                raise ValueError("normalized support rows hash mismatch")
        elif normalized_sha_value is not None:
            raise ValueError("normalized support path/hash mismatch")
        failure_value = protected["failure_code"]
        support = ParsedSupportArtifact(
            artifact_id=artifact_id,
            source_sha256=source_sha,
            kind=DependencyKind(protected["kind"]),
            format=str(protected["format"]),
            parse_status=SupportParseStatus(protected["parse_status"]),
            normalized_rows_path=normalized_path,
            normalized_rows_sha256=normalized_sha_value,
            failure_code=SupportFailureCode(failure_value)
            if failure_value is not None
            else None,
        )
        support_artifacts.append(support)
        support_paths[artifact_id] = str(protected["source_relative_path"])

    return _HydratedDependencyInputs(
        datasets=tuple(datasets),
        support_artifacts=tuple(support_artifacts),
        dataset_paths_by_id=dataset_paths,
        support_paths_by_id=support_paths,
        dataset_outputs_by_id={
            artifact_id: tuple(outputs)
            for artifact_id, outputs in dataset_outputs.items()
        },
    )


def _header_protected_by_effective_config(
    effective_config: Any,
    header: str,
) -> bool:
    if effective_config is None:
        return False
    # Keep is scrub priority 1 and publishes the source value unchanged.
    if effective_config.field_is_keep(header):
        return False
    return bool(
        effective_config.field_is_date(header)
        or effective_config.field_is_birthdate(header)
        or effective_config.field_is_id(header)
        or effective_config.field_is_drop(header)
        or effective_config.cap_rule_for(header) is not None
        or effective_config.generalize_rule_for(header) is not None
        or effective_config.band_rule_for(header) is not None
        or effective_config.field_is_suppress_small_cell(header)
    )


def _transform_requirement_id(
    *,
    dataset: OrganizedDataset,
    header: OrganizedHeader,
    action: Action,
    kind: StructuredTransformKind,
    origin: TransformRequirementOrigin,
    origin_rule_id: str | None,
    required_support_kind: DependencyKind | None,
) -> str:
    return "tr_" + canonical_sha256(
        {
            "dataset_artifact_id": dataset.artifact_id,
            "dataset_sha256": dataset.source_sha256,
            "header_id": header.header_id,
            "classification_action": action.value,
            "kind": kind.value,
            "origin": origin.value,
            "origin_rule_id": origin_rule_id,
            "required_support_kind": (
                required_support_kind.value
                if required_support_kind is not None
                else None
            ),
        }
    )[:32]


def _build_preliminary_dependency_inputs(
    datasets: tuple[OrganizedDataset, ...],
    classifications_by_dataset: Mapping[str, tuple[HeaderClassification, ...]],
    effective_config: Any,
) -> tuple[
    dict[str, frozenset[str]],
    dict[str, tuple[TransformRequirement, ...]],
]:
    published_raw: dict[str, frozenset[str]] = {}
    requirements_by_dataset: dict[str, tuple[TransformRequirement, ...]] = {}
    for dataset in datasets:
        published_raw[dataset.artifact_id] = frozenset(
            header.header_id
            for header in dataset.headers
            if not _header_protected_by_effective_config(
                effective_config,
                header.raw_name,
            )
        )
        classifications = {
            classification.header: classification
            for classification in classifications_by_dataset.get(
                dataset.artifact_id,
                (),
            )
        }
        requirements: list[TransformRequirement] = []
        for header in dataset.headers:
            classification = classifications.get(header.raw_name)
            configured = False
            action: Action | None = None
            kind: StructuredTransformKind | None = None
            required_support_kind: DependencyKind | None = None
            if effective_config is not None:
                if (
                    effective_config.field_is_birthdate(header.raw_name)
                    or effective_config.field_is_drop(header.raw_name)
                ):
                    continue
                if not effective_config.field_is_keep(header.raw_name):
                    cap_rule = effective_config.cap_rule_for(header.raw_name)
                    generalize_rule = (
                        effective_config.generalize_rule_for(header.raw_name)
                    )
                    if cap_rule is not None:
                        configured = True
                        action = Action.CAP
                        kind = StructuredTransformKind.CAP
                    elif generalize_rule is not None:
                        action = Action.GENERALIZE
                        kind = StructuredTransformKind.GENERALIZE
                        configured = True
                        if not getattr(generalize_rule, "mapping", None):
                            required_support_kind = DependencyKind.DICTIONARY_MAPPING
                    elif effective_config.band_rule_for(header.raw_name) is not None:
                        # Banding has no rulebook action or
                        # TransformRequirement kind. Its explicit config
                        # protects the header; never synthesize banding.
                        continue
                    elif effective_config.field_is_suppress_small_cell(
                        header.raw_name
                    ):
                        configured = True
                        action = Action.SUPPRESS
                        kind = StructuredTransformKind.SUPPRESS_SMALL_CELL
            if action is None and classification is not None:
                action = classification.action
                if action is Action.CAP:
                    kind = StructuredTransformKind.CAP
                    required_support_kind = DependencyKind.DICTIONARY_MAPPING
                elif action is Action.GENERALIZE:
                    kind = StructuredTransformKind.GENERALIZE
                    required_support_kind = DependencyKind.DICTIONARY_MAPPING
                elif (
                    action is Action.SUPPRESS
                    and effective_config is not None
                    and effective_config.field_is_suppress_small_cell(
                        header.raw_name
                    )
                ):
                    kind = StructuredTransformKind.SUPPRESS_SMALL_CELL
            if action is None or kind is None:
                continue
            origin = (
                TransformRequirementOrigin.EFFECTIVE_CONFIG
                if configured
                else TransformRequirementOrigin.RULE_CLASSIFICATION
            )
            origin_rule_id = (
                None
                if configured
                or classification is None
                or not classification.matched_rules
                else sorted(classification.matched_rules)[0]
            )
            requirement = TransformRequirement(
                requirement_id=_transform_requirement_id(
                    dataset=dataset,
                    header=header,
                    action=action,
                    kind=kind,
                    origin=origin,
                    origin_rule_id=origin_rule_id,
                    required_support_kind=required_support_kind,
                ),
                dataset_artifact_id=dataset.artifact_id,
                dataset_sha256=dataset.source_sha256,
                header_id=header.header_id,
                classification_action=action,
                kind=kind,
                origin=origin,
                origin_rule_id=origin_rule_id,
                required_support_kind=required_support_kind,
            )
            requirements.append(requirement)
        requirements_by_dataset[dataset.artifact_id] = tuple(
            sorted(requirements, key=lambda requirement: requirement.requirement_id)
        )
    return published_raw, requirements_by_dataset


def _evaluate_dependency_state(
    recommendations: tuple[DependencyRecommendation, ...],
    decisions: tuple[DependencyDecision, ...],
    support_artifacts: tuple[ParsedSupportArtifact, ...],
    *,
    support_filled_header_ids: frozenset[str] = frozenset(),
) -> _DependencyDisposition:
    decisions_by_id = {
        decision.recommendation_id: decision
        for decision in decisions
    }
    support_by_id = {
        support.artifact_id: support
        for support in support_artifacts
    }
    held: set[str] = set()
    review: set[str] = set()
    for recommendation in recommendations:
        # A GENERALIZE header's "missing mapping" requirement is SATISFIED once
        # eligible support has filled that header's value taxonomy — the dataset
        # must not be held (nor queued for review) for a mapping it now has.
        if (
            recommendation.reason_code
            is DependencyReasonCode.TRANSFORM_PARAMETERS_MISSING
            and recommendation.header_ids
            and set(recommendation.header_ids) <= support_filled_header_ids
        ):
            continue
        decision = decisions_by_id.get(recommendation.recommendation_id)
        current = (
            decision is not None
            and dependency_decision_is_current(decision, recommendation)
        )
        support = (
            support_by_id.get(recommendation.support_artifact_id)
            if recommendation.support_artifact_id is not None
            else None
        )
        evidence_available = (
            support is not None
            and support.parse_status is SupportParseStatus.PARSED
            and support.source_sha256 == recommendation.support_sha256
            and support.normalized_rows_sha256
            == recommendation.normalized_support_sha256
        )
        if current and decision is not None:
            if decision.level is DependencyLevel.IGNORED and evidence_available:
                continue
            if decision.level is DependencyLevel.IGNORED:
                review.add(recommendation.recommendation_id)
                if recommendation.suggested_level is DependencyLevel.REQUIRED:
                    held.add(recommendation.dataset_artifact_id)
                continue
            if evidence_available:
                continue
            review.add(recommendation.recommendation_id)
            if decision.level is DependencyLevel.REQUIRED:
                held.add(recommendation.dataset_artifact_id)
            continue

        review.add(recommendation.recommendation_id)
        required = recommendation.suggested_level is DependencyLevel.REQUIRED
        if decision is not None and decision.level is DependencyLevel.REQUIRED:
            required = True
        if required:
            held.add(recommendation.dataset_artifact_id)

    return _DependencyDisposition(
        held_dataset_ids=frozenset(held),
        review_recommendation_ids=frozenset(review),
    )


def _support_by_path(hydrated: _HydratedDependencyInputs) -> dict[str, ParsedSupportArtifact]:
    support_by_id = {
        support.artifact_id: support
        for support in hydrated.support_artifacts
    }
    support_by_path: dict[str, ParsedSupportArtifact] = {}
    for artifact_id, support_path in hydrated.support_paths_by_id.items():
        support = support_by_id.get(artifact_id)
        if support is None:
            raise ValueError("protected support path lacks hydrated artifact")
        prior = support_by_path.setdefault(support_path, support)
        if prior.artifact_id != artifact_id:
            raise ValueError("protected support path identity conflict")
    return support_by_path


def _manifest_dependency_recommendation_id(
    dependency: DatasetDependency, support_by_path: Mapping[str, ParsedSupportArtifact]
) -> str:
    """Recompute the deterministic recommendation id a manifest-declared
    dependency would produce -- DatasetDependency carries no stored
    recommendation_id (it is a raw site declaration, not a pipeline output),
    so both builders below must derive it identically from the dependency's
    identity plus whichever support artifact is CURRENTLY hydrated at its
    declared path (None when that support is missing/never organized)."""
    support = support_by_path.get(dependency.support)
    return recommendation_identity(
        dataset_artifact_id=dependency.dataset_source_artifact_id,
        support_artifact_id=support.artifact_id if support is not None else None,
        kind=dependency.kind,
        reason_code=DependencyReasonCode.MANIFEST_DECLARED,
        header_ids=(),
        transform_requirement_ids=(),
    )


def _build_unavailable_manifest_recommendations(
    hydrated: _HydratedDependencyInputs,
    dependency_relations: Mapping[str, tuple[DependencyRelation, ...]],
    *,
    rulebook_sha256: str,
    scrub_config_sha256: str,
) -> tuple[DependencyRecommendation, ...]:
    datasets_by_id = {
        dataset.artifact_id: dataset
        for dataset in hydrated.datasets
    }
    support_by_path = _support_by_path(hydrated)
    support_by_id = {
        support.artifact_id: support
        for support in hydrated.support_artifacts
    }
    support_ids = set(support_by_id)
    recommendations: dict[str, DependencyRecommendation] = {}
    for relations in dependency_relations.values():
        for relation in relations:
            if (
                relation.dataset_state is DependencyRelationState.MISSING
                or relation.support_state is DependencyRelationState.CURRENT
            ):
                continue
            dependency = relation.dependency
            if (
                dependency.support_artifact_id is not None
                and dependency.support_artifact_id in support_ids
            ):
                # Stale bytes that still organized produce an ordinary current
                # recommendation through recommend_dependencies.
                continue
            dataset = datasets_by_id.get(dependency.dataset_source_artifact_id)
            if dataset is None:
                continue
            recommendation_id = _manifest_dependency_recommendation_id(dependency, support_by_path)
            support = support_by_path.get(dependency.support)
            support_artifact_id = (
                support.artifact_id if support is not None else None
            )
            basis = DependencyDecisionBasis(
                rulebook_sha256=rulebook_sha256,
                scrub_config_sha256=scrub_config_sha256,
                support_role_sha256=support_role_sha256(
                    recommendation_id=recommendation_id,
                    dataset_artifact_id=dataset.artifact_id,
                    support_artifact_id=support_artifact_id,
                    kind=dependency.kind,
                    role_source=RoleSource.MANIFEST,
                    organizer_role_version=ORGANIZER_ROLE_VERSION,
                ),
            )
            recommendation = DependencyRecommendation(
                schema_version="dependency-recommendation/v2",
                recommendation_id=recommendation_id,
                dataset_artifact_id=dataset.artifact_id,
                dataset_sha256=dataset.source_sha256,
                support_artifact_id=support_artifact_id,
                support_sha256=(
                    support.source_sha256 if support is not None else None
                ),
                normalized_support_sha256=(
                    support.normalized_rows_sha256
                    if support is not None
                    else None
                ),
                kind=dependency.kind,
                suggested_level=(
                    DependencyLevel.REQUIRED
                    if dependency.level is DependencyLevel.REQUIRED
                    else DependencyLevel.HELPFUL
                ),
                default_sensitivity=Sensitivity.CONFIDENTIAL,
                reason_code=DependencyReasonCode.MANIFEST_DECLARED,
                header_ids=(),
                matched_rule_ids=(),
                transform_requirement_ids=(),
                basis=basis,
            )
            prior = recommendations.setdefault(
                recommendation.recommendation_id,
                recommendation,
            )
            if prior != recommendation:
                raise ValueError(
                    "manifest dependency recommendation identity conflict"
                )
    return tuple(
        recommendations[recommendation_id]
        for recommendation_id in sorted(recommendations)
    )


def _recommendation_role_source(
    recommendation: DependencyRecommendation,
) -> RoleSource:
    matches = [
        role_source
        for role_source in RoleSource
        if support_role_sha256(
            recommendation_id=recommendation.recommendation_id,
            dataset_artifact_id=recommendation.dataset_artifact_id,
            support_artifact_id=recommendation.support_artifact_id,
            kind=recommendation.kind,
            role_source=role_source,
            organizer_role_version=ORGANIZER_ROLE_VERSION,
        )
        == recommendation.basis.support_role_sha256
    ]
    if len(matches) != 1:
        raise ValueError("recommendation role basis is not canonical")
    return matches[0]


def _build_private_dependency_recommendations(
    recommendations: tuple[DependencyRecommendation, ...],
    hydrated: _HydratedDependencyInputs,
    dependency_relations: Mapping[
        str,
        tuple[DependencyRelation, ...],
    ] | None = None,
) -> tuple[PrivateDependencyRecommendation, ...]:
    datasets_by_id = {
        dataset.artifact_id: dataset
        for dataset in hydrated.datasets
    }
    expected_missing_support_paths: dict[str, str] = {}
    support_by_path = _support_by_path(hydrated)
    for relations in (dependency_relations or {}).values():
        for relation in relations:
            if relation.support_state is DependencyRelationState.CURRENT:
                continue
            recommendation_id = _manifest_dependency_recommendation_id(relation.dependency, support_by_path)
            support_path = relation.dependency.support
            prior = expected_missing_support_paths.setdefault(
                recommendation_id,
                support_path,
            )
            if prior != support_path:
                raise ValueError(
                    "manifest recommendation has conflicting support paths"
                )
    private_records: list[PrivateDependencyRecommendation] = []
    for recommendation in recommendations:
        dataset = datasets_by_id.get(recommendation.dataset_artifact_id)
        dataset_path = hydrated.dataset_paths_by_id.get(
            recommendation.dataset_artifact_id
        )
        if dataset is None or dataset_path is None:
            raise ValueError("recommendation dataset lacks protected context")
        raw_by_id = {
            header.header_id: header.raw_name
            for header in dataset.headers
        }
        try:
            raw_names = tuple(raw_by_id[header_id] for header_id in recommendation.header_ids)
        except KeyError as exc:
            raise ValueError("recommendation header lacks protected context") from exc
        role_source = _recommendation_role_source(recommendation)
        support_path = (
            hydrated.support_paths_by_id.get(
                recommendation.support_artifact_id
            )
            if recommendation.support_artifact_id is not None
            else expected_missing_support_paths.get(
                recommendation.recommendation_id
            )
            if role_source is RoleSource.MANIFEST
            else None
        )
        if recommendation.support_artifact_id is not None and support_path is None:
            raise ValueError("recommendation support lacks protected path context")
        private_records.append(
            PrivateDependencyRecommendation(
                schema_version="dependency-recommendation-private/v2",
                recommendation_id=recommendation.recommendation_id,
                dataset_artifact_id=recommendation.dataset_artifact_id,
                dataset_path=dataset_path,
                support_artifact_id=recommendation.support_artifact_id,
                support_path=support_path,
                raw_header_names=raw_names,
                role_source=role_source,
                organizer_role_version=ORGANIZER_ROLE_VERSION,
                basis=recommendation.basis,
            )
        )
    return tuple(private_records)


def _clear_stale_staging(staging_dir: Path, study_staging_dir: Path) -> None:
    """Clear staging before copying the CURRENT run's approved forms into it.

    Bug found during the Phase-7 final audit: this previously cleared ONLY the
    stale-sentinel + quarantine JSONLs, never the staged dataset JSONLs
    themselves. A prior run that scrubbed successfully but then failed the
    residual guard gate (``guard_ok=False`` -- "nothing published") leaves its
    SCRUBBED files sitting in ``staging_dir``; those never get published NOR
    cleaned in that failure path. A LATER run for the same study -- even one
    approving a completely different set of forms -- would then publish that
    leftover data alongside the current run's freshly-copied forms, bypassing
    the current run's classification/approval entirely (confirmed via a
    synthetic repro: a hand-seeded stale JSONL not present in the current
    approval JSON was still published). Clearing every staged ``*.jsonl``
    here guarantees ``staging_dir`` only ever contains files copied by THIS
    run's ``approved_forms`` loop below.
    """
    staging_dir.mkdir(parents=True, exist_ok=True)
    for f in staging_dir.glob("*.jsonl"):
        f.unlink()
    sentinel = study_staging_dir / ".phi_scrub_complete"
    if sentinel.is_file():
        sentinel.unlink()
    quarantine_dir = study_staging_dir / "quarantine"
    if quarantine_dir.is_dir():
        for f in quarantine_dir.glob("*.jsonl"):
            f.unlink()


def _write_held_note(form_name: str, approval: FormReviewApproval) -> None:
    note_path = review_paths.classification_review_path(
        Path(config.STUDY_AUDIT_DIR), Path(form_name).stem
    )
    note_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Classification hold: {form_name}", "", "Reasons:"]
    lines.extend(f"- {reason}" for reason in approval.reasons)
    if approval.held_reason is not None:
        lines.append("")
        lines.append("Held-reason detail (value-free):")
        lines.append("```json")
        lines.append(json.dumps(approval.held_reason.to_json(), indent=2, sort_keys=True))
        lines.append("```")
    note_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_support_transform_provenance(run_dir: Path, result: Any) -> None:
    """Persist applied support->generalize-map provenance to the protected run zone.

    Audit-only (never a dataset value): dataset/header ids, the synth map name,
    the support artifact + evidence hashes, and the entry count. Written mode-0600
    beside the private dependency records.
    """
    if not result.provenance:
        return
    path = Path(run_dir) / ".protected" / "support_transform_maps.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(asdict(record), sort_keys=True) for record in result.provenance]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def run_pipeline(study: str, jurisdiction: str) -> PipelineResult:
    """Run the operational pipeline while owning its canonical per-study lock."""
    if jurisdiction not in _JURISDICTION_LABELS:
        return PipelineResult(
            study=study,
            jurisdiction=jurisdiction,
            run_id=None,
            exit_code=2,
            message=f"unsupported jurisdiction {jurisdiction!r}; choose 'us'",
        )
    try:
        # Validate before creating the lock parent or touching mutable study
        # sources.  The lock path function performs no filesystem writes.
        lock_path_for(study)
    except ValueError as exc:
        return PipelineResult(
            study=study,
            jurisdiction=jurisdiction,
            run_id=None,
            exit_code=2,
            message=f"invalid study: {exc}",
        )

    try:
        acquire_pipeline_lock(study)
    except PipelineBusyError:
        return PipelineResult(
            study=study,
            jurisdiction=jurisdiction,
            run_id=None,
            exit_code=1,
            message="study pipeline lock is busy",
        )
    except (OSError, RuntimeError):
        return PipelineResult(
            study=study,
            jurisdiction=jurisdiction,
            run_id=None,
            exit_code=1,
            message="study pipeline lock infrastructure failure",
        )

    body_result: PipelineResult | None = None
    body_error: BaseException | None = None
    body_traceback: Any = None
    try:
        body_result = _run_pipeline_locked(study, jurisdiction)
    except BaseException as exc:
        body_error = exc
        body_traceback = exc.__traceback__

    try:
        release_pipeline_lock(study)
    except Exception:
        if body_result is not None:
            return _dc_replace(
                body_result,
                exit_code=1,
                message="study pipeline lock release infrastructure failure",
            )
        return PipelineResult(
            study=study,
            jurisdiction=jurisdiction,
            run_id=None,
            exit_code=1,
            message="study pipeline lock release infrastructure failure",
        )

    if body_error is not None:
        raise body_error.with_traceback(body_traceback)
    assert body_result is not None
    return body_result


def _run_pipeline_locked(study: str, jurisdiction: str) -> PipelineResult:
    """Execute the current pipeline state machine under its caller-owned lock."""
    jurisdiction_label = _JURISDICTION_LABELS[jurisdiction]
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    try:
        intake_manifest = load_intake_manifest(study)
    except IntakeManifestError:
        return PipelineResult(
            study=study,
            jurisdiction=jurisdiction,
            run_id=run_id,
            exit_code=2,
            message="intake_failed",
        )
    if intake_manifest.get("status") == "review_required":
        return PipelineResult(
            study=study,
            jurisdiction=jurisdiction,
            run_id=run_id,
            exit_code=8,
            message="intake_review_required",
        )
    if intake_manifest.get("status") != "ready":
        return PipelineResult(
            study=study,
            jurisdiction=jurisdiction,
            run_id=run_id,
            exit_code=2,
            message="intake_failed",
        )

    # Resolve the current rulebook seam before intake or organizer code can
    # open mutable study sources. Richer live-resolution behavior is defined
    # by later phases; this preserves the existing real pinned-rule contract.
    bootstrap_study_privacy(study, jurisdiction_label)
    try:
        privacy = load_study_privacy_config(Path(config.RAW_DATA_DIR) / study)
    except (OSError, ValueError) as exc:
        return PipelineResult(
            study=study,
            jurisdiction=jurisdiction,
            run_id=run_id,
            exit_code=2,
            message=f"privacy config load failed: {exc}",
        )
    # Resolve the active rulebook honoring the study's rule_refresh posture:
    # pinned_only (default) never touches the network; online_preferred opts
    # into the live AI-extract path (still gated by REPORTAL_RULEBOOK_AI_EXTRACT).
    allow_network = privacy.rule_refresh == "online_preferred"
    try:
        resolution = resolve_rulebook(privacy, allow_network=allow_network)
    except RulebookUnavailableError:
        return PipelineResult(
            study=study,
            jurisdiction=jurisdiction,
            run_id=run_id,
            exit_code=8,
            message="rulebook_unavailable",
        )
    if resolution.protection_weakened:
        return PipelineResult(
            study=study,
            jurisdiction=jurisdiction,
            run_id=run_id,
            exit_code=8,
            message="rulebook_protection_weakened",
            rulebook_sha256=resolution.bundle.rules_sha256,
        )
    bundle = resolution.bundle  # offline (pinned) default; live merge when enabled

    # -- a. organize every locked run so descriptor verification and dependency roles
    # can never be skipped by a stale organizer cache.  Keep the intake manifest
    # hash inside organize_manifest as provenance only. Call the private
    # lock-required body directly, never the public organize() wrapper --
    # this call already runs under the per-study lock run_pipeline() holds
    # for the whole _run_pipeline_locked body, and that lock is
    # non-reentrant: reacquiring it here would raise PipelineBusyError.
    #
    # dependency_relations is pulled from organize_manifest below instead
    # of a second, independent source-root read here: _organize_locked
    # already derived it from its own pinned/verified inventory, and
    # reopening source_root/datasets by pathname a second time (even via
    # check_forms_manifest, let alone Path.resolve()) would reintroduce
    # exactly the unsafe path this module must never touch.
    organized_root = Path(config.ORGANIZED_DIR) / study
    organize_manifest = _organize_locked(study)
    dependency_relations = organize_manifest.get("dependency_relations") or {}
    hydrated_dependencies = _hydrate_dependency_inputs(
        organized_root,
        organize_manifest,
    )

    datasets = organize_manifest.get("datasets", [])
    organizer_review_count = len(organize_manifest.get("review_bucket", []))
    runs_dir = Path(config.STUDY_OUTPUT_DIR) / "runs"
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if not datasets:
        write_dependency_recommendations(
            run_dir=run_dir,
            recommendations=(),
            private_records=(),
        )
        _write_pending_dependency_recommendations(
            run_dir,
            (),
            frozenset(),
        )
        result = PipelineResult(
            study=study,
            jurisdiction=jurisdiction,
            run_id=run_id,
            exit_code=2,
            message="no datasets found for study after organize -- nothing staged",
            organizer_review_count=organizer_review_count,
        )
        (run_dir / "pipeline_result.json").write_text(
            json.dumps(result.to_json(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return result

    organized_datasets_dir = organized_root / "datasets"
    sot_root = Path(config.LLM_SOURCE_SOT_DIR)

    # -- c. classify every form's headers (metadata only) -------------------
    # Load the CURRENT effective scrub config (packaged defaults + whatever
    # per-study overrides already exist from a prior run) so a header's
    # "published raw" status is judged against what the scrub engine will
    # ACTUALLY do, not just its phi_review classification action. Bug found
    # during Phase 7 evidence re-runs: TBTXDT has no jurisdiction-specific pinned
    # rule (classifies KEEP), but IS already protected by the packaged
    # defaults' date_fields catch-all pattern -- treating every KEEP header
    # as "published raw" (the old default) force-dropped it as a false
    # value-profile-conflict (its ISO-date values legitimately match the
    # DATE_ISO blocking pattern), discarding real clinical data that would
    # otherwise have been correctly SANT-jittered. No PHI ever leaked (a
    # force-dropped column can't leak), but it was an unnecessary utility
    # loss the effective-config check below eliminates.
    _effective_cfg = phi_scrub.load_scrub_config(study=study)


    approvals: dict[str, FormReviewApproval] = {}
    all_classifications: list[HeaderClassification] = []
    held_forms: list[str] = []
    approved_forms: list[str] = []
    preliminary_classifications: dict[str, list[HeaderClassification]] = {}
    final_classifications_by_dataset: dict[str, list[HeaderClassification]] = {}

    decisions = load_review_decisions(study)
    keep_headers = confirmed_keep_headers(decisions)
    drop_headers = extra_force_drop_headers(decisions)

    profile_escalations = 0
    profile_auto_clears = 0
    raw_rows_by_form: dict[str, list[dict[str, Any]]] = {}

    for entry in sorted(datasets, key=lambda d: d["output"]):
        form_name = entry["output"]
        normalized_rows = _read_jsonl_rows(organized_datasets_dir / form_name)
        headers, rows = _raw_rows_from_organized(organized_root, entry, normalized_rows)
        raw_rows_by_form[form_name] = rows
        sot_signals = load_sot_variable_signals(sot_root, Path(form_name).stem)

        published_raw_headers = frozenset(
            h
            for h in headers
            if not _header_protected_by_effective_config(_effective_cfg, h)
        )
        approval = review_form_headers(
            form_name=form_name,
            headers=headers,
            privacy_config=privacy,
            rule_bundle=bundle,
            sot_signals=sot_signals,
            confirmed_keep_headers=keep_headers,
            published_raw_headers=published_raw_headers,
        )
        preliminary_classifications.setdefault(
            str(entry["artifact_id"]),
            [],
        ).extend(approval.classifications)
        # Apply the feedback loop: 'override' mutates the classified action
        # (threaded into both the approval JSON and the synthesized scrub
        # config below); 'drop' merges into this form's force_drop_headers
        # regardless of what the name-rules alone decided. Original header
        # CASING is preserved (functional matching downstream is already
        # case-insensitive via _normalize_header_for_lookup).
        updated_classifications = apply_decisions_to_classifications(approval.classifications, decisions)
        merged_force_drop = list(approval.force_drop_headers)
        merged_force_drop_upper = {h.upper() for h in merged_force_drop}
        for h in headers:
            if h.upper() in drop_headers and h.upper() not in merged_force_drop_upper:
                merged_force_drop.append(h)
                merged_force_drop_upper.add(h.upper())

        # Deterministic value profiler (LOCAL, in-process, never leaves the
        # process -- see phi_engine/pipeline/profile.py). ESCALATION: a
        # keep-classified header whose values are mostly PHI-shaped is
        # force-dropped even though its NAME gave no indication (the "PHI in
        # an unexpected column" backstop). AUTO-CLEAR: a header already
        # force-dropped pending human confirmation is un-held when its value
        # shape structurally PROVES it cannot be an identifier/date series
        # (closed categorical: <= AUTO_CLEAR_MAX_DISTINCT distinct values,
        # zero blocking/warn/date signal).
        action_by_header = {item.header: item.action for item in updated_classifications}
        for h in headers:
            col_profile = profile_column(row.get(h) for row in rows)
            is_forced = h.upper() in merged_force_drop_upper
            # Auto-clear eligibility EXCLUDES a header the operator explicitly
            # decided to drop -- an automated heuristic must never override
            # deliberate human intent, only the DEFAULT risk-heuristic hold.
            is_auto_clear_eligible = is_forced and h.upper() not in drop_headers
            if (
                not is_forced
                and h in published_raw_headers
                and action_by_header.get(h) == Action.KEEP
                and col_profile.is_value_profile_conflict
            ):
                merged_force_drop.append(h)
                merged_force_drop_upper.add(h.upper())
                profile_escalations += 1
            elif is_auto_clear_eligible and col_profile.is_closed_categorical:
                merged_force_drop = [x for x in merged_force_drop if x.upper() != h.upper()]
                merged_force_drop_upper.discard(h.upper())
                profile_auto_clears += 1

        if (
            updated_classifications != approval.classifications
            or sorted(merged_force_drop) != sorted(approval.force_drop_headers)
        ):
            approval = _dc_replace(
                approval,
                classifications=updated_classifications,
                actions={item.header: item.action.value for item in updated_classifications},
                force_drop_headers=tuple(sorted(merged_force_drop)),
            )
        approvals[form_name] = approval
        all_classifications.extend(approval.classifications)
        final_classifications_by_dataset.setdefault(
            str(entry["artifact_id"]), []
        ).extend(approval.classifications)

        if approval.status == "held":
            held_forms.append(form_name)
            _write_held_note(form_name, approval)
        else:
            approved_forms.append(form_name)

    # -- classification -> baseline scrub-config synthesis (threads EVERY ---
    # -- action, not just force-drop/suppress, into the row scrubber). This is
    # -- the AUTHORITATIVE config the dependency recommendations are hashed
    # -- against; support maps are overlaid onto it further below for the scrubber.
    synthesize_study_config(study, jurisdiction_label, all_classifications)
    scrub_config_hash = phi_scrub.effective_scrub_config_hash(study=study)
    effective_dependency_config = phi_scrub.load_scrub_config(study=study)
    published_raw_header_ids, transform_requirements = (
        _build_preliminary_dependency_inputs(
            hydrated_dependencies.datasets,
            {
                artifact_id: tuple(classifications)
                for artifact_id, classifications
                in preliminary_classifications.items()
            },
            effective_dependency_config,
        )
    )
    dependency_decisions = load_study_dependency_decisions(study)
    generated_dependency_recommendations = recommend_dependencies(
        datasets=hydrated_dependencies.datasets,
        support_artifacts=hydrated_dependencies.support_artifacts,
        published_raw_headers_by_dataset=published_raw_header_ids,
        transform_requirements_by_dataset=transform_requirements,
        confirmed_links=dependency_decisions,
        rule_bundle=bundle,
    )
    if scrub_config_hash is None:
        raise ValueError("dependency scrub config hash is unavailable")
    recommendations_by_id = {
        recommendation.recommendation_id: recommendation
        for recommendation in generated_dependency_recommendations
    }
    for recommendation in _build_unavailable_manifest_recommendations(
        hydrated_dependencies,
        dependency_relations,
        rulebook_sha256=bundle.rules_sha256,
        scrub_config_sha256=scrub_config_hash,
    ):
        obsolete_inferred_ids = [
            recommendation_id
            for recommendation_id, generated in recommendations_by_id.items()
            if recommendation_id != recommendation.recommendation_id
            and generated.dataset_artifact_id
            == recommendation.dataset_artifact_id
            and generated.support_artifact_id
            == recommendation.support_artifact_id
            and generated.kind is recommendation.kind
            and generated.reason_code
            in {
                DependencyReasonCode.SAME_STEM_COMPANION,
                DependencyReasonCode.EXACT_HEADER_MATCH,
            }
        ]
        for recommendation_id in obsolete_inferred_ids:
            del recommendations_by_id[recommendation_id]
        prior = recommendations_by_id.setdefault(
            recommendation.recommendation_id,
            recommendation,
        )
        if prior != recommendation:
            raise ValueError(
                "dependency recommendation identity conflict"
            )
    dependency_recommendations = tuple(
        recommendations_by_id[recommendation_id]
        for recommendation_id in sorted(recommendations_by_id)
    )
    if any(
        recommendation.basis.scrub_config_sha256 != scrub_config_hash
        for recommendation in dependency_recommendations
    ):
        raise ValueError("dependency recommendations used a stale scrub config")
    private_dependency_recommendations = (
        _build_private_dependency_recommendations(
            dependency_recommendations,
            hydrated_dependencies,
            dependency_relations,
        )
    )
    write_dependency_recommendations(
        run_dir=run_dir,
        recommendations=dependency_recommendations,
        private_records=private_dependency_recommendations,
    )

    # -- support -> scrub-parameter continuity + optional strengthen-only ----
    # -- signals. The recommendations written above are the AUTHORITATIVE
    # -- snapshot at the baseline config hash, so a human dependency decision
    # -- stays current run-to-run. Here support fills each ELIGIBLE GENERALIZE
    # -- header's value taxonomy and the config is re-synthesized so the SCRUBBER
    # -- sees the maps; a support-filled GENERALIZE header then no longer holds
    # -- the dataset for a "missing" mapping it now has. Model signals (opt-in,
    # -- fail-soft) strengthen only.
    support_signals = extract_support_signals(
        datasets=hydrated_dependencies.datasets,
        support_artifacts=hydrated_dependencies.support_artifacts,
        recommendations=dependency_recommendations,
        decisions=dependency_decisions,
        rule_bundle=bundle,
    )
    signals_applied = False
    if support_signals:
        header_name_by_id = {
            header.header_id: header.raw_name
            for dataset in hydrated_dependencies.datasets
            for header in dataset.headers
        }
        candidate_rule_ids = frozenset(rule.id for rule in getattr(bundle, "rules", ()))
        artifact_by_form = {
            str(entry["output"]): str(entry["artifact_id"]) for entry in datasets
        }
        for form_name, approval in list(approvals.items()):
            updated = tuple(
                apply_support_signal_actions(
                    approval.classifications,
                    support_signals,
                    header_name_by_id=header_name_by_id,
                    candidate_rule_ids=candidate_rule_ids,
                )
            )
            if updated != tuple(approval.classifications):
                approvals[form_name] = _dc_replace(
                    approval,
                    classifications=updated,
                    actions={item.header: item.action.value for item in updated},
                )
                signals_applied = True
        if signals_applied:
            all_classifications = [
                classification
                for form_name in approvals
                for classification in approvals[form_name].classifications
            ]
            final_classifications_by_dataset = {}
            for form_name, approval in approvals.items():
                artifact_id = artifact_by_form.get(form_name)
                if artifact_id is not None:
                    final_classifications_by_dataset.setdefault(
                        artifact_id, []
                    ).extend(approval.classifications)

    support_policy = build_transform_maps_from_support(
        datasets=hydrated_dependencies.datasets,
        support_artifacts=hydrated_dependencies.support_artifacts,
        recommendations=dependency_recommendations,
        decisions=dependency_decisions,
        classifications_by_dataset={
            artifact_id: tuple(classifications)
            for artifact_id, classifications in final_classifications_by_dataset.items()
        },
    )
    if support_policy.generalization_maps or signals_applied:
        synthesize_study_config(
            study,
            jurisdiction_label,
            all_classifications,
            generalization_map_overlay=support_policy.generalization_maps or None,
        )
        # The scrubber loads this re-synthesized config directly; scrub_config_hash
        # is updated for run-result truthfulness. The persisted recommendations keep
        # their baseline-hash basis (written above) so decisions stay current.
        scrub_config_hash = phi_scrub.effective_scrub_config_hash(study=study)
        if support_policy.generalization_maps:
            _write_support_transform_provenance(run_dir, support_policy)

    dependency_disposition = _evaluate_dependency_state(
        dependency_recommendations,
        dependency_decisions,
        hydrated_dependencies.support_artifacts,
        support_filled_header_ids=support_policy.applied_header_ids,
    )
    _write_pending_dependency_recommendations(
        run_dir,
        dependency_recommendations,
        dependency_disposition.review_recommendation_ids,
    )
    dependency_held_outputs = {
        output
        for artifact_id in dependency_disposition.held_dataset_ids
        for output in hydrated_dependencies.dataset_outputs_by_id.get(
            artifact_id,
            (),
        )
    }
    approved_forms = [
        form_name
        for form_name in approved_forms
        if form_name not in dependency_held_outputs
    ]

    # -- d. write phi_handling_approval.json ---------------------------------
    approval_payload = {
        "rule_bundle": bundle.to_json(),
        "forms": [approvals[name].to_json() for name in sorted(approvals)],
    }
    (run_dir / "phi_handling_approval.json").write_text(
        json.dumps(approval_payload, indent=2, sort_keys=True), encoding="utf-8"
    )

    dependency_review_count = len(
        dependency_disposition.review_recommendation_ids
    )
    review_queue_size = (
        organizer_review_count
        + len(held_forms)
        + dependency_review_count
    )
    if not approved_forms:
        result = PipelineResult(
            study=study, jurisdiction=jurisdiction, run_id=run_id, exit_code=8,
            message="every eligible dataset held -- nothing to scrub this run",
            forms_processed=[], forms_held=held_forms,
            review_queue_size=review_queue_size, organizer_review_count=organizer_review_count,
            scrub_config_hash=scrub_config_hash, rulebook_sha256=bundle.rules_sha256,
            rulebook_cache_status=resolution.cache_status,
            rulebook_source_mode=bundle.source_mode,
            dependency_review_count=dependency_review_count,
            dependency_held_dataset_ids=sorted(
                dependency_disposition.held_dataset_ids
            ),
        )
        (run_dir / "pipeline_result.json").write_text(
            json.dumps(result.to_json(), indent=2, sort_keys=True), encoding="utf-8"
        )
        return result

    # -- b. stage approved forms into tmp/<study>/datasets/ ------------------
    staging_dir = Path(config.STAGING_DATASETS_DIR)
    _clear_stale_staging(staging_dir, Path(config.STUDY_STAGING_DIR))
    for form_name in approved_forms:
        _write_jsonl_rows(staging_dir / form_name, raw_rows_by_form[form_name])

    # -- f. scrub -------------------------------------------------------------
    scrub_raised: str | None = None
    try:
        phi_scrub.run_scrub(study, run_id=run_id, runs_dir=runs_dir, partial_on_review=True)
    except Exception:  # noqa: BLE001 -- controlled code only in result JSON
        scrub_raised = "scrub_exception"

    if scrub_raised is not None:
        result = PipelineResult(
            study=study, jurisdiction=jurisdiction, run_id=run_id, exit_code=1,
            message="phi_scrub.run_scrub raised",
            forms_processed=approved_forms, forms_held=held_forms,
            review_queue_size=review_queue_size, organizer_review_count=organizer_review_count,
            scrub_raised=scrub_raised, scrub_config_hash=scrub_config_hash,
            rulebook_sha256=bundle.rules_sha256,
            rulebook_cache_status=resolution.cache_status,
            rulebook_source_mode=bundle.source_mode,
            dependency_review_count=dependency_review_count,
            dependency_held_dataset_ids=sorted(
                dependency_disposition.held_dataset_ids
            ),
        )
        (run_dir / "pipeline_result.json").write_text(
            json.dumps(result.to_json(), indent=2, sort_keys=True), encoding="utf-8"
        )
        return result

    # -- g. residual guard gate ------------------------------------------------
    try:
        guard = run_phi_guard_gate(staging_dir)
        guard_ok = guard.ok
    except Exception:  # noqa: BLE001 -- Presidio unavailable fallback
        from phi_engine.security.llm_source_gate import scan_tree_for_phi

        legacy = scan_tree_for_phi(staging_dir)
        guard_ok = legacy.ok

    # -- h. publish -------------------------------------------------------------
    published_count = 0
    if guard_ok:
        publish_dir = Path(config.STUDY_LLM_SOURCE_DIR) / "datasets"
        publish_dir.mkdir(parents=True, exist_ok=True)
        for jsonl_file in sorted(staging_dir.glob("*.jsonl")):
            shutil.move(str(jsonl_file), str(publish_dir / jsonl_file.name))
            published_count += 1

    exit_code = 0
    message = "clean run"
    if not guard_ok:
        exit_code = 5
        message = "residual PHI guard gate failed -- nothing published"
    elif held_forms or review_queue_size:
        exit_code = 8
        message = "partial run -- held forms or a non-empty review queue"

    result = PipelineResult(
        study=study, jurisdiction=jurisdiction, run_id=run_id, exit_code=exit_code,
        message=message, forms_processed=approved_forms, forms_held=held_forms,
        review_queue_size=review_queue_size, organizer_review_count=organizer_review_count,
        guard_ok=guard_ok, guard_failed=not guard_ok,
        scrub_config_hash=scrub_config_hash, rulebook_sha256=bundle.rules_sha256,
        published_count=published_count,
        rulebook_cache_status=resolution.cache_status,
        rulebook_source_mode=bundle.source_mode,
        profile_escalations=profile_escalations, profile_auto_clears=profile_auto_clears,
        dependency_review_count=dependency_review_count,
        dependency_held_dataset_ids=sorted(
            dependency_disposition.held_dataset_ids
        ),
    )
    (run_dir / "pipeline_result.json").write_text(
        json.dumps(result.to_json(), indent=2, sort_keys=True), encoding="utf-8"
    )
    return result
