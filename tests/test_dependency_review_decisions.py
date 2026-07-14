from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from phi_engine.pipeline.dependencies import (
    DependencyDecisionBasis,
    DependencyKind,
    DependencyLevel,
    DependencyReasonCode,
    DependencyRecommendation,
    PrivateDependencyRecommendation,
    RoleSource,
    Sensitivity,
    load_dependency_decisions,
    support_role_sha256,
    write_dependency_recommendations,
)


RULEBOOK_SHA = "1" * 64
SCRUB_SHA = "2" * 64
HEADER_ID = "h_" + "3" * 24
STUDY = "DecisionStudy"


@dataclass(frozen=True)
class _Case:
    dataset_id: str
    recommendation_id: str
    dataset_path: str
    support_id: str | None
    support_path: str | None
    support_state: str
    role_source: RoleSource
    reason_code: DependencyReasonCode
    organizer_role_version: int = 1


def _artifact_id(token: str) -> str:
    return "a_" + token * 32


def _recommendation_id(token: str) -> str:
    return "dr_" + token * 32


def _case(
    token: str,
    *,
    support_state: str = "parsed",
    role_source: RoleSource = RoleSource.INFERRED,
    reason_code: DependencyReasonCode = DependencyReasonCode.ONLY_INTERPRETATION,
    organizer_role_version: int = 1,
) -> _Case:
    support_id = None if support_state.startswith("missing") else _artifact_id(chr(ord(token) + 1))
    support_path = None
    if support_state == "missing_manifest":
        support_path = f"data_dictionary/expected-{token}.csv"
    elif support_id is not None:
        support_path = f"data_dictionary/support-{token}.csv"
    return _Case(
        dataset_id=_artifact_id(token),
        recommendation_id=_recommendation_id(token),
        dataset_path=f"datasets/dataset-{token}.csv",
        support_id=support_id,
        support_path=support_path,
        support_state=support_state,
        role_source=role_source,
        reason_code=reason_code,
        organizer_role_version=organizer_role_version,
    )


def _install_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cases: tuple[_Case, ...],
) -> tuple[Path, dict[str, DependencyRecommendation]]:
    import phi_engine.pipeline.dependencies as dependency_contracts
    import phi_engine.pipeline.review as review

    workspace = tmp_path / "workspace"
    output_root = workspace / "output" / STUDY
    run_dir = output_root / "runs" / "20260714T120000Z"
    organized_root = workspace / "organized" / STUDY
    config_dir = workspace / "config" / STUDY
    verified_root = organized_root / ".verified_sources"
    headers_root = organized_root / ".protected" / "headers"
    support_root = organized_root / ".protected" / "support"
    for directory in (verified_root, headers_root, support_root, config_dir):
        directory.mkdir(parents=True, exist_ok=True)

    recommendations: list[DependencyRecommendation] = []
    private_records: list[PrivateDependencyRecommendation] = []
    declared_missing: dict[str, list[dict[str, object]]] = {}
    by_id: dict[str, DependencyRecommendation] = {}

    for case in cases:
        dataset_bytes = f"subject_id,value\n{case.dataset_id},1\n".encode()
        dataset_sha = hashlib.sha256(dataset_bytes).hexdigest()
        support_sha: str | None = None
        normalized_sha: str | None = None

        (verified_root / case.dataset_id).write_bytes(dataset_bytes)
        dataset_metadata = headers_root / f"{case.dataset_id}.json"
        dataset_metadata.write_text(
            json.dumps(
                {
                    "artifact_id": case.dataset_id,
                    "source_sha256": dataset_sha,
                    "headers": [],
                    "source_relative_path": case.dataset_path,
                }
            ),
            encoding="utf-8",
        )
        dataset_metadata.chmod(0o600)

        if case.support_id is not None:
            support_bytes = f"variable,label\n{case.dataset_id},identifier\n".encode()
            support_sha = hashlib.sha256(support_bytes).hexdigest()
            (verified_root / case.support_id).write_bytes(support_bytes)
            if case.support_state == "parsed":
                normalized_bytes = b'{"normalized_cells":["subject id","identifier"]}\n'
                normalized_sha = hashlib.sha256(normalized_bytes).hexdigest()
                normalized_path = organized_root / "support" / "dictionary" / f"support__{case.support_id}.jsonl"
                normalized_path.parent.mkdir(parents=True, exist_ok=True)
                normalized_path.write_bytes(normalized_bytes)
                normalized_path.chmod(0o600)
                parse_status = "parsed"
                failure_code = None
                normalized_rows_path: str | None = str(normalized_path)
            else:
                parse_status = "failed"
                failure_code = "parse_error"
                normalized_rows_path = None
            support_metadata = support_root / f"{case.support_id}.json"
            support_metadata.write_text(
                json.dumps(
                    {
                        "artifact_id": case.support_id,
                        "source_sha256": support_sha,
                        "kind": "dictionary",
                        "format": "csv",
                        "parse_status": parse_status,
                        "normalized_rows_sha256": normalized_sha,
                        "failure_code": failure_code,
                        "normalized_rows_path": normalized_rows_path,
                        "source_relative_path": case.support_path,
                        "normalized_source_stem": f"support-{case.dataset_id[-1]}",
                    }
                ),
                encoding="utf-8",
            )
            support_metadata.chmod(0o600)

        role_hash = support_role_sha256(
            recommendation_id=case.recommendation_id,
            dataset_artifact_id=case.dataset_id,
            support_artifact_id=case.support_id,
            kind=DependencyKind.DICTIONARY,
            role_source=case.role_source,
            organizer_role_version=case.organizer_role_version,
        )
        basis = DependencyDecisionBasis(
            rulebook_sha256=RULEBOOK_SHA,
            scrub_config_sha256=SCRUB_SHA,
            support_role_sha256=role_hash,
        )
        recommendation = DependencyRecommendation(
            schema_version="dependency-recommendation/v1",
            recommendation_id=case.recommendation_id,
            dataset_artifact_id=case.dataset_id,
            dataset_sha256=dataset_sha,
            support_artifact_id=case.support_id,
            support_sha256=support_sha,
            normalized_support_sha256=normalized_sha,
            kind=DependencyKind.DICTIONARY,
            suggested_level=DependencyLevel.REQUIRED,
            default_sensitivity=Sensitivity.CONFIDENTIAL,
            reason_code=case.reason_code,
            header_ids=(HEADER_ID,),
            matched_rule_ids=("rule-1",),
            transform_requirement_ids=(),
            basis=basis,
        )
        private = PrivateDependencyRecommendation(
            schema_version="dependency-recommendation-private/v1",
            recommendation_id=case.recommendation_id,
            dataset_artifact_id=case.dataset_id,
            dataset_path=case.dataset_path,
            support_artifact_id=case.support_id,
            support_path=case.support_path,
            raw_header_names=("Subject ID",),
            role_source=case.role_source,
            organizer_role_version=case.organizer_role_version,
            basis=basis,
        )
        recommendations.append(recommendation)
        private_records.append(private)
        by_id[case.recommendation_id] = recommendation

        if case.support_state == "missing_manifest":
            declared_missing[case.dataset_path] = [
                {
                    "dataset_artifact_id": case.dataset_id,
                    "dataset_source_sha256": dataset_sha,
                    "support": case.support_path,
                    "support_artifact_id": None,
                    "support_source_sha256": None,
                    "kind": "dictionary",
                    "level": "required",
                    "sensitivity": "confidential",
                    "reason_code": "manifest_declared",
                    "recommendation_id": case.recommendation_id,
                    "basis": basis.to_json(),
                    "confirmed_by": "prior-reviewer",
                    "confirmed_at": "2026-07-14T10:00:00Z",
                }
            ]

    write_dependency_recommendations(
        run_dir=run_dir,
        recommendations=tuple(recommendations),
        private_records=tuple(private_records),
    )
    manifest_path = config_dir / "_forms_manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(
            {
                "required": ["dataset.csv"],
                "optional": [],
                "reject": [],
                "date_locales": {},
                "dataset_dependencies_schema": "dataset-dependencies/v1",
                "dataset_dependencies_code_table_version": 1,
                "dataset_dependencies": declared_missing,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)

    monkeypatch.setattr(review.config, "STUDY_OUTPUT_DIR", output_root)
    monkeypatch.setattr(review.config, "ORGANIZED_DIR", workspace / "organized")
    monkeypatch.setattr(review.config, "TMP_DIR", workspace / "tmp")
    monkeypatch.setattr(
        review.config,
        "study_config_dir",
        lambda selected=None: workspace / "config" / (selected or STUDY),
    )
    monkeypatch.setattr(dependency_contracts, "_effective_scrub_config_sha256", lambda: SCRUB_SHA)
    monkeypatch.setattr(review, "_current_rulebook_sha256", lambda _study: RULEBOOK_SHA, raising=False)
    return manifest_path, by_id


def _decide(case: _Case, **overrides: object):
    import phi_engine.pipeline.review as review

    arguments: dict[str, object] = {
        "dataset": case.dataset_id,
        "recommendation": case.recommendation_id,
        "support": case.support_id,
        "level": "required",
        "sensitivity": "confidential",
        "reason_code": case.reason_code.value,
        "detail_file": None,
        "decided_by": "reviewer-id",
    }
    arguments.update(overrides)
    return review.decide_dependency(STUDY, **arguments)


def test_missing_support_decision_requires_current_manifest_declared_expected_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    declared = _case(
        "1",
        support_state="missing_manifest",
        role_source=RoleSource.MANIFEST,
        reason_code=DependencyReasonCode.MANIFEST_DECLARED,
    )
    inferred = _case(
        "4",
        support_state="missing_inferred",
        role_source=RoleSource.INFERRED,
        reason_code=DependencyReasonCode.TRANSFORM_PARAMETERS_MISSING,
    )
    manifest_path, _ = _install_state(tmp_path, monkeypatch, (declared, inferred))

    decision = _decide(declared)
    assert decision.support_artifact_id is None
    assert decision.support_sha256 is None
    assert decision.normalized_support_sha256 is None
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    stored = manifest["dataset_dependencies"][declared.dataset_path][0]
    assert stored["support"] == declared.support_path
    assert stored["support_artifact_id"] is None
    assert stored["support_source_sha256"] is None

    with pytest.raises(ValueError, match="manifest-declared expected support"):
        _decide(inferred)
    decisions = load_dependency_decisions(manifest_path.parent / "dependency_decisions.jsonl")
    assert decisions == (decision,)


def test_missing_or_failed_support_cannot_be_marked_non_confidential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = _case(
        "1",
        support_state="missing_manifest",
        role_source=RoleSource.MANIFEST,
        reason_code=DependencyReasonCode.MANIFEST_DECLARED,
    )
    failed = _case("4", support_state="failed")
    manifest_path, _ = _install_state(tmp_path, monkeypatch, (missing, failed))

    for case in (missing, failed):
        with pytest.raises(ValueError, match="non_confidential sensitivity requires parsed support"):
            _decide(case, sensitivity="non_confidential")
    assert not (manifest_path.parent / "dependency_decisions.jsonl").exists()


def test_failed_support_verifies_exact_source_identity_without_normalized_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import phi_engine.pipeline.review as review
    failed = _case("1", support_state="failed")
    manifest_path, recommendations = _install_state(tmp_path, monkeypatch, (failed,))

    decision = _decide(failed)
    assert decision.support_sha256 == recommendations[failed.recommendation_id].support_sha256
    assert decision.normalized_support_sha256 is None

    organized_root = Path(review.config.ORGANIZED_DIR) / STUDY
    (organized_root / ".verified_sources" / failed.support_id).write_bytes(b"changed source bytes")
    with pytest.raises(ValueError, match="stale support identity"):
        _decide(failed)
    assert len(load_dependency_decisions(manifest_path.parent / "dependency_decisions.jsonl")) == 1


def test_parsed_and_failed_support_metadata_states_are_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed = _case("1", support_state="parsed")
    failed = _case("4", support_state="failed")
    manifest_path, _ = _install_state(tmp_path, monkeypatch, (parsed, failed))
    import phi_engine.pipeline.review as review

    organized_root = Path(review.config.ORGANIZED_DIR) / STUDY
    parsed_source_path = organized_root / ".verified_sources" / parsed.support_id
    parsed_source_bytes = parsed_source_path.read_bytes()
    parsed_source_path.write_bytes(b"changed parsed support source")
    with pytest.raises(ValueError, match="stale support identity"):
        _decide(parsed)
    parsed_source_path.write_bytes(parsed_source_bytes)
    parsed_metadata_path = organized_root / ".protected" / "support" / f"{parsed.support_id}.json"
    parsed_metadata = json.loads(parsed_metadata_path.read_text(encoding="utf-8"))
    parsed_normalized_path = Path(parsed_metadata["normalized_rows_path"])
    parsed_normalized_bytes = parsed_normalized_path.read_bytes()
    parsed_normalized_path.write_bytes(b"changed normalized support")
    with pytest.raises(ValueError, match="stale support identity"):
        _decide(parsed)
    parsed_normalized_path.write_bytes(parsed_normalized_bytes)
    parsed_metadata["failure_code"] = "parse_error"
    parsed_metadata_path.write_text(json.dumps(parsed_metadata), encoding="utf-8")
    parsed_metadata_path.chmod(0o600)
    with pytest.raises(ValueError, match="stale support identity"):
        _decide(parsed)

    failed_metadata_path = organized_root / ".protected" / "support" / f"{failed.support_id}.json"
    failed_metadata = json.loads(failed_metadata_path.read_text(encoding="utf-8"))
    failed_metadata["normalized_rows_path"] = str(organized_root / "unexpected.jsonl")
    failed_metadata_path.write_text(json.dumps(failed_metadata), encoding="utf-8")
    failed_metadata_path.chmod(0o600)
    with pytest.raises(ValueError, match="stale support identity"):
        _decide(failed)
    assert not (manifest_path.parent / "dependency_decisions.jsonl").exists()


def test_decision_revalidates_current_rulebook_scrub_and_role_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import phi_engine.pipeline.dependencies as dependency_contracts
    import phi_engine.pipeline.review as review

    current = _case("1")
    stale_role = _case("4", organizer_role_version=2)
    manifest_path, _ = _install_state(tmp_path, monkeypatch, (current, stale_role))

    monkeypatch.setattr(review, "_current_rulebook_sha256", lambda _study: "9" * 64)
    with pytest.raises(ValueError, match="stale recommendation basis"):
        _decide(current)
    monkeypatch.setattr(review, "_current_rulebook_sha256", lambda _study: RULEBOOK_SHA)
    monkeypatch.setattr(dependency_contracts, "_effective_scrub_config_sha256", lambda: "8" * 64)
    with pytest.raises(ValueError, match="stale recommendation basis"):
        _decide(current)
    def fail_scrub_hash() -> str:
        raise yaml.YAMLError("private parser detail")

    monkeypatch.setattr(
        dependency_contracts,
        "_effective_scrub_config_sha256",
        fail_scrub_hash,
    )
    with pytest.raises(ValueError, match="current scrub config is unavailable"):
        _decide(current)
    monkeypatch.setattr(dependency_contracts, "_effective_scrub_config_sha256", lambda: SCRUB_SHA)
    with pytest.raises(ValueError, match="stale recommendation role"):
        _decide(stale_role)
    private_path = (
        Path(review.config.STUDY_OUTPUT_DIR)
        / "runs"
        / "20260714T120000Z"
        / ".protected"
        / "dependency_recommendations.jsonl"
    )
    private_payloads = [
        json.loads(line)
        for line in private_path.read_text(encoding="utf-8").splitlines()
    ]
    private_payloads[0]["role_source"] = "directory"
    private_path.write_text(
        "".join(json.dumps(payload) + "\n" for payload in private_payloads),
        encoding="utf-8",
    )
    private_path.chmod(0o600)
    with pytest.raises(ValueError, match="stale recommendation basis"):
        _decide(current)
    assert not (manifest_path.parent / "dependency_decisions.jsonl").exists()


def test_append_failures_roll_back_manifest_and_both_decision_trails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import phi_engine.pipeline.review as review

    current = _case("1")
    manifest_path, _ = _install_state(tmp_path, monkeypatch, (current,))
    original_manifest = manifest_path.read_bytes()

    def fail_decision_append(_path: Path, _decision: object) -> Path:
        raise OSError("simulated ordinary append failure")

    monkeypatch.setattr(review, "append_dependency_decision", fail_decision_append)
    with pytest.raises(ValueError, match="dependency decision persistence failed"):
        _decide(current)
    assert manifest_path.read_bytes() == original_manifest
    assert not (manifest_path.parent / "dependency_decisions.jsonl").exists()
    assert not (manifest_path.parent / "dependency_decision_details.jsonl").exists()


def test_private_append_failure_rolls_back_prior_ordinary_append_and_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import phi_engine.pipeline.review as review

    current = _case("1")
    manifest_path, _ = _install_state(tmp_path, monkeypatch, (current,))
    original_manifest = manifest_path.read_bytes()

    def fail_private_append(_path: Path, _record: dict[str, object]) -> None:
        raise OSError("simulated private append failure")

    monkeypatch.setattr(review, "_append_private_jsonl", fail_private_append)
    with pytest.raises(ValueError, match="dependency decision persistence failed"):
        _decide(current)
    assert manifest_path.read_bytes() == original_manifest
    assert not (manifest_path.parent / "dependency_decisions.jsonl").exists()
    assert not (manifest_path.parent / "dependency_decision_details.jsonl").exists()


def test_concurrent_decisions_use_study_lock_and_retry_without_lost_manifest_updates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import phi_engine.pipeline.review as review

    first = _case("1")
    second = _case("4")
    manifest_path, _ = _install_state(tmp_path, monkeypatch, (first, second))
    loaded_once = threading.Event()
    release_first = threading.Event()
    original_load = review._load_forms_manifest_for_update
    calls_lock = threading.Lock()
    load_calls = 0

    def coordinated_load(path: Path) -> dict[str, object]:
        nonlocal load_calls
        payload = original_load(path)
        with calls_lock:
            load_calls += 1
            call_number = load_calls
        if call_number == 1:
            loaded_once.set()
            release_first.wait(timeout=0.25)
        elif call_number == 2:
            release_first.set()
        return payload

    monkeypatch.setattr(review, "_load_forms_manifest_for_update", coordinated_load)
    barrier = threading.Barrier(2)

    def decide_with_retry(case: _Case):
        barrier.wait(timeout=2)
        for _ in range(100):
            try:
                return _decide(case)
            except ValueError as exc:
                if str(exc) != "study pipeline lock is busy":
                    raise
                time.sleep(0.005)
        raise AssertionError("decision never acquired the study lock")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(decide_with_retry, case) for case in (first, second)]
        decisions = [future.result(timeout=5) for future in futures]

    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert set(manifest["dataset_dependencies"]) == {first.dataset_path, second.dataset_path}
    persisted = load_dependency_decisions(manifest_path.parent / "dependency_decisions.jsonl")
    assert {decision.decision_id for decision in persisted} == {
        decision.decision_id for decision in decisions
    }
    assert loaded_once.is_set()


def test_review_list_exposes_only_latest_pending_dependency_recommendations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import phi_engine.pipeline.review as review

    first = _case("1")
    second = _case("4")
    _, recommendations = _install_state(tmp_path, monkeypatch, (first, second))
    monkeypatch.setattr(review.config, "STUDY_AUDIT_DIR", tmp_path / "audit")
    pending_path = (
        Path(review.config.STUDY_OUTPUT_DIR)
        / "runs"
        / "20260714T120000Z"
        / "pending_dependency_recommendations.jsonl"
    )
    pending_path.write_text(
        json.dumps(recommendations[first.recommendation_id].to_json()) + "\n",
        encoding="utf-8",
    )
    pending_path.chmod(0o600)

    listed = review.list_review_items(STUDY)
    assert listed["dependency_recommendations"] == [
        recommendations[first.recommendation_id].to_json()
    ]


def test_review_list_rejects_pending_record_that_differs_from_latest_ordinary_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import phi_engine.pipeline.review as review

    current = _case("1")
    _, recommendations = _install_state(tmp_path, monkeypatch, (current,))
    monkeypatch.setattr(review.config, "STUDY_AUDIT_DIR", tmp_path / "audit")
    pending_payload = recommendations[current.recommendation_id].to_json()
    pending_payload["suggested_level"] = "helpful"
    pending_path = (
        Path(review.config.STUDY_OUTPUT_DIR)
        / "runs"
        / "20260714T120000Z"
        / "pending_dependency_recommendations.jsonl"
    )
    pending_path.write_text(json.dumps(pending_payload) + "\n", encoding="utf-8")
    pending_path.chmod(0o600)

    with pytest.raises(
        ValueError, match="pending dependency recommendation mismatch"
    ):
        review.list_review_items(STUDY)
