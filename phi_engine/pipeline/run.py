"""Standalone PHI pipeline driver: organize -> classify -> scrub -> publish.

``python -m phi_engine run --study S --jurisdiction in|us [--workspace W]``

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

import json
import shutil
from dataclasses import asdict, dataclass, field
from dataclasses import replace as _dc_replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import phi_engine.config.config as config
from phi_engine.audit import review_paths
from phi_engine.pipeline.intake import load_intake_manifest
from phi_engine.pipeline.organize import intake_manifest_sha, organize
from phi_engine.pipeline.profile import profile_column
from phi_engine.pipeline.review import (
    apply_decisions_to_classifications,
    confirmed_keep_headers,
    extra_force_drop_headers,
    load_review_decisions,
)
from phi_engine.pipeline.synthesize_config import bootstrap_study_privacy, synthesize_study_config
from phi_engine.security import phi_scrub
from phi_engine.security.phi_guard_gate import run_phi_guard_gate
from phi_engine.security.phi_review import (
    Action,
    FormReviewApproval,
    HeaderClassification,
    load_sot_variable_signals,
    load_study_privacy_config,
    refresh_jurisdiction_rules,
    review_form_headers,
)

__all__ = ["PipelineResult", "run_pipeline"]

_JURISDICTION_LABELS = {"in": "INDIA", "us": "USA"}


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
    published_count: int = 0
    profile_escalations: int = 0
    profile_auto_clears: int = 0
    sot_generation_error: str | None = None

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


def run_pipeline(study: str, jurisdiction: str) -> PipelineResult:
    """Run the full organize -> classify -> scrub -> publish pipeline once."""
    if jurisdiction not in _JURISDICTION_LABELS:
        return PipelineResult(
            study=study, jurisdiction=jurisdiction, run_id=None, exit_code=2,
            message=f"unsupported jurisdiction {jurisdiction!r}; choose 'in' or 'us'",
        )
    jurisdiction_label = _JURISDICTION_LABELS[jurisdiction]

    # -- a. organize if the intake manifest changed since the last pass -----
    organized_root = Path(config.ORGANIZED_DIR) / study
    manifest_path = organized_root / "organize_manifest.json"
    intake_manifest = load_intake_manifest(study)

    organize_manifest: dict[str, Any] | None = None
    if manifest_path.is_file():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if existing.get("source_manifest_sha") == intake_manifest_sha(intake_manifest):
                organize_manifest = existing
        except (json.JSONDecodeError, OSError):
            organize_manifest = None
    if organize_manifest is None:
        organize_manifest = organize(study)

    datasets = organize_manifest.get("datasets", [])
    organizer_review_count = len(organize_manifest.get("review_bucket", []))
    if not datasets:
        return PipelineResult(
            study=study, jurisdiction=jurisdiction, run_id=None, exit_code=2,
            message="no datasets found for study after organize -- nothing staged",
            organizer_review_count=organizer_review_count,
        )

    # -- config bootstrap + privacy load -------------------------------------
    bootstrap_study_privacy(study, jurisdiction_label)
    try:
        privacy = load_study_privacy_config(Path(config.RAW_DATA_DIR) / study)
    except (OSError, ValueError) as exc:
        return PipelineResult(
            study=study, jurisdiction=jurisdiction, run_id=None, exit_code=2,
            message=f"privacy config load failed: {exc}",
            organizer_review_count=organizer_review_count,
        )

    bundle = refresh_jurisdiction_rules(privacy)  # offline (pinned) default

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    runs_dir = Path(config.STUDY_OUTPUT_DIR) / "runs"
    organized_datasets_dir = organized_root / "datasets"
    sot_root = Path(config.LLM_SOURCE_SOT_DIR)

    # SoT is enrichment-only. Generate joined views before classification so
    # load_sot_variable_signals can use them, but never abort the PHI pipeline
    # when the PDF-specific producer cannot resolve a form.
    sot_generation_error: str | None = None
    annotated_pdf_dir = Path(config.ANNOTATED_PDFS_DIR)
    if annotated_pdf_dir.is_dir() and any(annotated_pdf_dir.glob("*.pdf")):
        try:
            from phi_engine.sot import generate_sot

            rc = generate_sot(study)
            if rc != 0:
                sot_generation_error = f"generate_sot returned {rc}"
        except Exception as exc:  # noqa: BLE001 -- SoT is fail-soft enrichment
            sot_generation_error = f"{type(exc).__name__}: {exc}"

    # -- c. classify every form's headers (metadata only) -------------------
    # Load the CURRENT effective scrub config (packaged defaults + whatever
    # per-study overrides already exist from a prior run) so a header's
    # "published raw" status is judged against what the scrub engine will
    # ACTUALLY do, not just its phi_review classification action. Bug found
    # during Phase 7 evidence re-runs: TBTXDT has no INDIA-specific pinned
    # rule (classifies KEEP), but IS already protected by the packaged
    # defaults' date_fields catch-all pattern -- treating every KEEP header
    # as "published raw" (the old default) force-dropped it as a false
    # value-profile-conflict (its ISO-date values legitimately match the
    # DATE_ISO blocking pattern), discarding real clinical data that would
    # otherwise have been correctly SANT-jittered. No PHI ever leaked (a
    # force-dropped column can't leak), but it was an unnecessary utility
    # loss the effective-config check below eliminates.
    _effective_cfg = phi_scrub.load_scrub_config(study=study)

    def _protected_by_effective_config(header: str) -> bool:
        if _effective_cfg is None:
            return False
        return bool(
            _effective_cfg.field_is_keep(header)
            or _effective_cfg.field_is_date(header)
            or _effective_cfg.field_is_birthdate(header)
            or _effective_cfg.field_is_id(header)
            or _effective_cfg.field_is_drop(header)
            or _effective_cfg.cap_rule_for(header) is not None
            or _effective_cfg.generalize_rule_for(header) is not None
            or _effective_cfg.band_rule_for(header) is not None
            or _effective_cfg.field_is_suppress_small_cell(header)
        )

    approvals: dict[str, FormReviewApproval] = {}
    all_classifications: list[HeaderClassification] = []
    held_forms: list[str] = []
    approved_forms: list[str] = []

    decisions = load_review_decisions(study)
    keep_headers = confirmed_keep_headers(decisions)
    drop_headers = extra_force_drop_headers(decisions)

    profile_escalations = 0
    profile_auto_clears = 0

    for entry in sorted(datasets, key=lambda d: d["output"]):
        form_name = entry["output"]
        rows = _read_jsonl_rows(organized_datasets_dir / form_name)
        headers = list(rows[0].keys()) if rows else []
        sot_signals = load_sot_variable_signals(sot_root, Path(form_name).stem)

        published_raw_headers = frozenset(
            h for h in headers if not _protected_by_effective_config(h)
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

        if approval.status == "held":
            held_forms.append(form_name)
            _write_held_note(form_name, approval)
        else:
            approved_forms.append(form_name)

    # -- classification -> scrub-config synthesis (threads EVERY action, ---
    # -- not just force-drop/suppress, into the row scrubber) --------------
    synthesize_study_config(study, jurisdiction_label, all_classifications)
    scrub_config_hash = phi_scrub.effective_scrub_config_hash(study=study)

    # -- d. write phi_handling_approval.json ---------------------------------
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    approval_payload = {
        "rule_bundle": bundle.to_json(),
        "forms": [approvals[name].to_json() for name in sorted(approvals)],
    }
    (run_dir / "phi_handling_approval.json").write_text(
        json.dumps(approval_payload, indent=2, sort_keys=True), encoding="utf-8"
    )

    review_queue_size = organizer_review_count + len(held_forms)

    if not approved_forms:
        result = PipelineResult(
            study=study, jurisdiction=jurisdiction, run_id=run_id, exit_code=8,
            message="every form held -- nothing to scrub this run",
            forms_processed=[], forms_held=held_forms,
            review_queue_size=review_queue_size, organizer_review_count=organizer_review_count,
            scrub_config_hash=scrub_config_hash, rulebook_sha256=bundle.rules_sha256,
            sot_generation_error=sot_generation_error,
        )
        (run_dir / "pipeline_result.json").write_text(
            json.dumps(result.to_json(), indent=2, sort_keys=True), encoding="utf-8"
        )
        return result

    # -- b. stage approved forms into tmp/<study>/datasets/ ------------------
    staging_dir = Path(config.STAGING_DATASETS_DIR)
    _clear_stale_staging(staging_dir, Path(config.STUDY_STAGING_DIR))
    for form_name in approved_forms:
        shutil.copy2(organized_datasets_dir / form_name, staging_dir / form_name)

    # -- f. scrub -------------------------------------------------------------
    scrub_raised: str | None = None
    try:
        phi_scrub.run_scrub(study, run_id=run_id, runs_dir=runs_dir, partial_on_review=True)
    except Exception as exc:  # noqa: BLE001 -- captured for the result JSON
        scrub_raised = f"{type(exc).__name__}: {exc}"

    if scrub_raised is not None:
        result = PipelineResult(
            study=study, jurisdiction=jurisdiction, run_id=run_id, exit_code=1,
            message="phi_scrub.run_scrub raised",
            forms_processed=approved_forms, forms_held=held_forms,
            review_queue_size=review_queue_size, organizer_review_count=organizer_review_count,
            scrub_raised=scrub_raised, scrub_config_hash=scrub_config_hash,
            rulebook_sha256=bundle.rules_sha256, sot_generation_error=sot_generation_error,
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
        published_count=published_count, sot_generation_error=sot_generation_error,
        profile_escalations=profile_escalations, profile_auto_clears=profile_auto_clears,
    )
    (run_dir / "pipeline_result.json").write_text(
        json.dumps(result.to_json(), indent=2, sort_keys=True), encoding="utf-8"
    )
    return result
