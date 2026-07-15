"""Turn eligible data-dictionary / mapping support into scrub parameters.

The dependency layer parses dictionary/mapping artifacts, recommends links, and
records human decisions, but nothing ever turned a confirmed (or auto-helpful)
mapping table into the ``generalization_maps`` a GENERALIZE header needs — so a
GENERALIZE column stayed fail-closed (quarantined) forever. This module closes
that gap deterministically and locally (no LLM, no network):

* :func:`build_transform_maps_from_support` reads the normalized rows of an
  ELIGIBLE support artifact, detects a two-column ``code -> label`` table, and
  emits the ``_synth_generalize_<header>`` map for the matching GENERALIZE
  header. Support may only FILL an already-GENERALIZE header's empty map; it can
  never promote KEEP/DROP or otherwise weaken the deterministic floor.

Eligibility (deterministic path, sensitivity-agnostic because no row text ever
leaves the process):

* support parsed AND its hashes match the recommendation's support hashes, AND
* a CURRENT decision links it at level REQUIRED or HELPFUL, OR
* no decision yet but the recommendation is an auto-inferred HELPFUL
  EXACT_HEADER_MATCH link (the "auto-helpful" case).
* a REQUIRED recommendation with no current decision is NEVER applied — the
  dataset is already held by ``_evaluate_dependency_state`` until a human decides.

:func:`apply_support_signal_actions` applies model-produced support signals in a
STRENGTHEN-ONLY manner (a signal can raise a header's protection rank, never
lower it). The signal PRODUCER path is opt-in and requires a configured local
model; without one the deterministic path above is the whole behavior.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping

from phi_engine.pipeline.dependencies import (
    DependencyDecision,
    DependencyKind,
    DependencyLevel,
    DependencyReasonCode,
    DependencyRecommendation,
    OrganizedDataset,
    ParsedSupportArtifact,
    Sensitivity,
    SupportParseStatus,
    canonical_sha256,
    dependency_decision_is_current,
    support_evidence_sha256,
)
from phi_engine.security.phi_review import (
    _ACTION_RANK,
    Action,
    HeaderClassification,
    normalize_header,
)
from phi_engine.security.model_routing import CandidateRuleView as _CandidateRuleView
from phi_engine.utils.logging_system import get_logger

__all__ = [
    "SupportMapProvenance",
    "SupportPolicyResult",
    "apply_support_signal_actions",
    "build_transform_maps_from_support",
    "extract_support_signals",
]

_logger = get_logger(__name__)

@dataclass(frozen=True)
class SupportMapProvenance:
    """Audit-only record of one applied support -> generalize-map binding."""

    dataset_artifact_id: str
    header_id: str
    map_name: str
    support_artifact_id: str
    support_sha256: str
    recommendation_id: str
    entry_count: int
    support_evidence_sha256: str


@dataclass(frozen=True)
class SupportPolicyResult:
    """Per-header generalize maps built from eligible support, plus provenance."""

    generalization_maps: dict[str, dict[str, str]]
    applied_header_ids: frozenset[str]
    provenance: tuple[SupportMapProvenance, ...]


def _read_support_rows(support: ParsedSupportArtifact) -> tuple[Mapping[str, Any], ...]:
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


def _row_cells(row: Mapping[str, Any]) -> dict[int, str]:
    cells: dict[int, str] = {}
    for cell in row.get("cells", ()):
        if isinstance(cell, Mapping) and "column_index" in cell:
            try:
                index = int(cell["column_index"])
            except (TypeError, ValueError):
                continue
            cells[index] = "" if cell.get("value") is None else str(cell["value"])
    return cells


def _linked_header_names(header: Any) -> frozenset[str]:
    """Normalized name(s) that identify a support artifact's linking column."""
    names = {
        normalize_header(name)
        for name in (header.raw_name, header.normalized_name)
        if name
    }
    return frozenset(name for name in names if name)


def _build_map_for_header(
    rows: tuple[Mapping[str, Any], ...], header_names: frozenset[str]
) -> dict[str, str]:
    """Build a ``code -> label`` map for one linked header from support rows.

    The support parser strips the source table's header row, so column identity
    is inferred structurally rather than from column labels. A data dictionary
    that LINKS to a dataset column carries that column's NAME as a cell value in
    a "variable" column (that value-name match is exactly what produced the
    EXACT_HEADER_MATCH recommendation). This finds that linking column, restricts
    to its rows for the linked header, and reads the first two OTHER columns (by
    index) as ``code`` then ``label`` — the standard value-label dictionary order.

    Deterministic and fail-closed: an unresolvable shape (no linking column,
    fewer than two other columns) or an inconsistent ``code -> label`` mapping
    returns ``{}`` (the header stays quarantined) rather than a guess. A reversed
    code/label guess simply yields keys the dataset values never match, so the
    scrub quarantines those values — never a wrong publish.
    """
    if not rows:
        return {}
    column_indices: set[int] = set()
    for row in rows:
        column_indices.update(_row_cells(row).keys())
    linking: int | None = None
    for index in sorted(column_indices):
        if any(
            normalize_header(_row_cells(row).get(index, "")) in header_names
            for row in rows
        ):
            linking = index
            break
    if linking is None:
        return {}
    others = sorted(index for index in column_indices if index != linking)
    if len(others) < 2:
        return {}
    source_column, target_column = others[0], others[1]
    mapping: dict[str, str] = {}
    for row in rows:
        cells = _row_cells(row)
        if normalize_header(cells.get(linking, "")) not in header_names:
            continue  # a row describing a different variable
        code = cells.get(source_column, "").strip()
        label = cells.get(target_column, "").strip()
        if not code or not label:
            continue
        existing = mapping.get(code)
        if existing is not None and existing != label:
            return {}  # inconsistent taxonomy -> fail-closed
        mapping[code] = label
    return mapping


def _link_eligible(
    recommendation: DependencyRecommendation,
    decision: DependencyDecision | None,
    support: ParsedSupportArtifact,
) -> bool:
    if support.parse_status is not SupportParseStatus.PARSED:
        return False
    if (
        support.source_sha256 != recommendation.support_sha256
        or support.normalized_rows_sha256 != recommendation.normalized_support_sha256
    ):
        return False
    if decision is not None and dependency_decision_is_current(decision, recommendation):
        return decision.level in (DependencyLevel.REQUIRED, DependencyLevel.HELPFUL)
    # No current decision: a REQUIRED link must wait for a human (dataset held).
    if recommendation.suggested_level is DependencyLevel.REQUIRED:
        return False
    # Auto-helpful: an inferred exact-header dictionary/mapping link may supply
    # parameters without waiting (deterministic, local, strengthen-only).
    return (
        recommendation.suggested_level is DependencyLevel.HELPFUL
        and recommendation.reason_code is DependencyReasonCode.EXACT_HEADER_MATCH
    )


def build_transform_maps_from_support(
    *,
    datasets: tuple[OrganizedDataset, ...],
    support_artifacts: tuple[ParsedSupportArtifact, ...],
    recommendations: tuple[DependencyRecommendation, ...],
    decisions: tuple[DependencyDecision, ...],
    classifications_by_dataset: Mapping[str, tuple[HeaderClassification, ...]],
) -> SupportPolicyResult:
    """Build ``_synth_generalize_<header>`` maps from eligible support artifacts.

    Only headers a dataset already classifies GENERALIZE receive a map, so
    support can strengthen (fill a fail-closed map) but never promote or weaken a
    classification.
    """
    support_by_id = {support.artifact_id: support for support in support_artifacts}
    decisions_by_recommendation = {
        decision.recommendation_id: decision for decision in decisions
    }
    header_by_key: dict[tuple[str, str], Any] = {
        (dataset.artifact_id, header.header_id): header
        for dataset in datasets
        for header in dataset.headers
    }
    generalize_names: dict[str, set[str]] = {
        dataset_id: {
            classification.header
            for classification in classifications
            if classification.action is Action.GENERALIZE
        }
        for dataset_id, classifications in classifications_by_dataset.items()
    }

    # Collect every eligible (map_name -> mapping) candidate first, so a map_name
    # that two artifacts fill with DIFFERENT tables can be dropped (fail-closed:
    # an ambiguous map stays empty and quarantines, never a silent last-wins).
    candidates: dict[str, list[tuple[dict[str, str], SupportMapProvenance]]] = {}

    for recommendation in recommendations:
        if recommendation.kind not in (DependencyKind.DICTIONARY, DependencyKind.MAPPING):
            continue
        if recommendation.support_artifact_id is None or not recommendation.header_ids:
            continue
        support = support_by_id.get(recommendation.support_artifact_id)
        if support is None:
            continue
        decision = decisions_by_recommendation.get(recommendation.recommendation_id)
        if not _link_eligible(recommendation, decision, support):
            continue
        rows = _read_support_rows(support)
        eligible_headers = generalize_names.get(recommendation.dataset_artifact_id, set())
        for header_id in recommendation.header_ids:
            header = header_by_key.get((recommendation.dataset_artifact_id, header_id))
            if header is None or header.raw_name not in eligible_headers:
                continue  # only fill a header that is actually GENERALIZE
            mapping = _build_map_for_header(rows, _linked_header_names(header))
            if not mapping:
                continue
            map_name = f"_synth_generalize_{header.raw_name.lower()}"
            record = SupportMapProvenance(
                dataset_artifact_id=recommendation.dataset_artifact_id,
                header_id=header_id,
                map_name=map_name,
                support_artifact_id=support.artifact_id,
                support_sha256=support.source_sha256,
                recommendation_id=recommendation.recommendation_id,
                entry_count=len(mapping),
                support_evidence_sha256=support_evidence_sha256(
                    (
                        {
                            "dataset_artifact_id": recommendation.dataset_artifact_id,
                            "header_id": header_id,
                            "support_artifact_id": support.artifact_id,
                            "recommendation_id": recommendation.recommendation_id,
                        },
                    ),
                    (canonical_sha256(mapping),),
                ),
            )
            candidates.setdefault(map_name, []).append((dict(mapping), record))

    maps: dict[str, dict[str, str]] = {}
    applied: set[str] = set()
    provenance: list[SupportMapProvenance] = []
    for map_name, entries in candidates.items():
        first_mapping = entries[0][0]
        if any(mapping != first_mapping for mapping, _record in entries):
            # Conflicting tables for one synth map name -> leave it empty so the
            # header stays fail-closed (quarantined) pending human curation.
            continue
        maps[map_name] = dict(first_mapping)
        for _mapping, record in entries:
            applied.add(record.header_id)
            provenance.append(record)

    return SupportPolicyResult(
        generalization_maps=maps,
        applied_header_ids=frozenset(applied),
        provenance=tuple(provenance),
    )


def apply_support_signal_actions(
    classifications: Iterable[HeaderClassification],
    signals: Iterable[Any],
    *,
    header_name_by_id: Mapping[str, str],
    candidate_rule_ids: frozenset[str],
) -> tuple[HeaderClassification, ...]:
    """Apply model support signals STRENGTHEN-ONLY over the classification floor.

    A signal upgrades a header's action ONLY when its action is strictly stricter
    (higher ``_ACTION_RANK``) than the current classification and its
    ``matched_rule_id`` is one of the supplied candidate rules. A weaker or
    equal-rank signal, or one citing an unknown rule, is ignored — support can
    never lower the deterministic floor.
    """
    result = list(classifications)
    by_header = {item.header: index for index, item in enumerate(result)}
    for signal in signals:
        header_name = header_name_by_id.get(getattr(signal, "header_id", None))
        if header_name is None or header_name not in by_header:
            continue
        matched_rule_id = getattr(signal, "matched_rule_id", None)
        if matched_rule_id not in candidate_rule_ids:
            continue
        action = getattr(signal, "action", None)
        if not isinstance(action, Action):
            continue
        index = by_header[header_name]
        current = result[index]
        if _ACTION_RANK[action] <= _ACTION_RANK[current.action]:
            continue
        reasons = tuple(current.reasons) + (f"support_signal:{matched_rule_id}",)
        result[index] = replace(current, action=action, reasons=reasons)
    return tuple(result)


def extract_support_signals(
    *,
    datasets: tuple[OrganizedDataset, ...],
    support_artifacts: tuple[ParsedSupportArtifact, ...],
    recommendations: tuple[DependencyRecommendation, ...],
    decisions: tuple[DependencyDecision, ...],
    rule_bundle: Any,
    router: Any = None,
) -> tuple[Any, ...]:
    """Produce model support signals for confirmed exact-header links (opt-in).

    OPT-IN and FAIL-SOFT: a signal is only ever requested for an EXACT_HEADER_MATCH
    link that already has a CURRENT, non-ignored human decision, and every model /
    binding failure (including the default offline local model) degrades to no
    signals so the deterministic classification floor stands and no dataset is
    held on the model's account. Any produced signal is still applied
    strengthen-only by :func:`apply_support_signal_actions`.
    """
    try:
        candidate_rules = tuple(
            _CandidateRuleView(
                rule_id=rule.id,
                action=rule.action,
                citation=rule.reason,
                jurisdictions=(rule.jurisdiction,),
            )
            for rule in rule_bundle.rules
        )
    except Exception as exc:
        _logger.debug(
            "support signals skipped: candidate rules unavailable (%s)",
            type(exc).__name__,
        )
        return ()

    dataset_by_id = {dataset.artifact_id: dataset for dataset in datasets}
    support_by_id = {support.artifact_id: support for support in support_artifacts}
    decisions_by_recommendation = {
        decision.recommendation_id: decision for decision in decisions
    }

    eligible: list[tuple[Any, DependencyDecision]] = []
    for recommendation in recommendations:
        if (
            recommendation.reason_code is not DependencyReasonCode.EXACT_HEADER_MATCH
            or not recommendation.header_ids
            or recommendation.support_artifact_id is None
        ):
            continue
        decision = decisions_by_recommendation.get(recommendation.recommendation_id)
        if (
            decision is None
            or decision.level is DependencyLevel.IGNORED
            or not dependency_decision_is_current(decision, recommendation)
        ):
            continue
        eligible.append((recommendation, decision))
    if not eligible:
        return ()

    try:
        from phi_engine.security.model_routing import ModelTaskRouter

        task_router = router if router is not None else ModelTaskRouter()
    except Exception as exc:
        _logger.debug(
            "support signals skipped: model router unavailable (%s)",
            type(exc).__name__,
        )
        return ()

    signals: list[Any] = []
    for recommendation, decision in eligible:
        dataset = dataset_by_id.get(recommendation.dataset_artifact_id)
        support = support_by_id.get(recommendation.support_artifact_id or "")
        if dataset is None or support is None:
            continue
        try:
            task = task_router.build_support_signal_task(
                dataset=dataset,
                support=support,
                recommendation=recommendation,
                decision=decision,
                current_basis=recommendation.basis,
                candidate_rules=candidate_rules,
            )
            signals.extend(task_router.extract_support_signals(task))
        except Exception as exc:  # model unavailable / binding / any failure -> keep floor
            _logger.debug(
                "support signal extraction skipped for %s (%s)",
                recommendation.recommendation_id,
                type(exc).__name__,
            )
            continue
    return tuple(signals)
