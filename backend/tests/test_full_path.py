"""Offline full-path safety proof for a planted dataset corpus."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

from phi_core.agents.base import Agent
from phi_core.agents.operator import Operator
from phi_core.agents.reasoning import (
    Executor,
    annotate_pending_review,
    apply_age_dob_rule,
    apply_blocking_floor,
    apply_confidence_floor,
    apply_sentinel_escalations,
    apply_sentinel_hard_rules,
    apply_site_cardinality_rule,
    validate_decisions,
    verify_keep_decisions,
)
from phi_core.agents.reviewer import Reviewer
from phi_core.bundle import BundleOptions, build_bundle
from phi_core.control.testing import FakeGateway, make_ctx
from phi_core.file_readers import column_value_stats, read_csv_columns
from phi_core.intake import build_manifest
from phi_core.publish_guard import scan_all_exports
from phi_corpus.planters import plant
from phi_corpus.verify import scan_exports_for_leaks


class _ScriptedJudge(Agent):
    NAME = "Judge"
    PROMPT = "Return JSON decisions from the supplied dataset headers."

    async def run(self, header_only_input: dict[str, list[str]]) -> dict[str, Any]:
        return await self.call_json(
            json.dumps({"datasets": header_only_input}, sort_keys=True),
            "full_path.handle",
            default={"decisions": []},
            expect_key="decisions",
        )


def _run_deterministic_gates(
    proposed: list[dict[str, Any]],
    dataset_paths: dict[str, Path],
    stats: dict[tuple[str, str], dict[str, int]],
) -> list[dict[str, Any]]:
    """Apply the current fixed gate sequence without a server or database."""
    decisions, _rejections = validate_decisions(proposed)
    decisions, _hard_rule_overrides = apply_sentinel_hard_rules(decisions)
    decisions, _age_dob_overrides = apply_age_dob_rule(decisions)
    decisions, _site_overrides = apply_site_cardinality_rule(decisions, stats)
    decisions, _sentinel_overrides = apply_sentinel_escalations(decisions, [])
    decisions, _confidence_overrides = apply_confidence_floor(decisions)
    decisions, _blocking_overrides = apply_blocking_floor(decisions, {})
    decisions, _keep_demotions = verify_keep_decisions(decisions, dataset_paths)
    return annotate_pending_review(decisions)


def test_planted_corpus_full_path_uses_real_safety_components(tmp_path, monkeypatch):
    """Intake through bundle stays local while exercising the deterministic spine."""
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")

    planted = plant("l1_sdtm_oncology_v1", row_count=4, seed=7)
    intake_zip = tmp_path / "planted-study.zip"
    intake_zip.write_bytes(planted.zip_bytes)

    manifest = build_manifest("full-path", intake_zip, tmp_path / "intake")
    assert manifest.status == "ready"

    dataset_entries = [entry for entry in manifest.entries if entry.component == "datasets"]
    assert dataset_entries

    files: list[dict[str, Any]] = []
    dataset_paths: dict[str, Path] = {}
    header_only_input: dict[str, list[str]] = {}
    stats: dict[tuple[str, str], dict[str, int]] = {}
    file_ids_by_name: dict[str, str] = {}
    for index, entry in enumerate(dataset_entries):
        source = Path(entry.stored_path)
        columns, _row_count = read_csv_columns(source)
        file_id = f"dataset-{index}"
        file_ids_by_name[Path(entry.relpath).name] = file_id
        files.append(
            {
                "file_id": file_id,
                "kind": "dataset",
                "subtype": "csv",
                "original_name": Path(entry.relpath).name,
                "stored_path": str(source),
                "columns": columns,
            }
        )
        dataset_paths[file_id] = source
        header_only_input[file_id] = columns
        for column, column_stats in column_value_stats(source, "csv", columns).items():
            stats[(file_id, column.lower())] = column_stats

    proposed: list[dict[str, Any]] = []
    review_target: tuple[str, str] | None = None
    for column in planted.ground_truth["columns"]:
        file_id = file_ids_by_name[column["file_name"]]
        action = column["expected_action"]
        if review_target is None and action == "keep" and column["column"].lower() == "domain":
            review_target = (file_id, column["column"])
        proposed.append(
            {
                "file_id": file_id,
                "column": column["column"],
                "action": action,
                "phi_category": column["hipaa_category"],
                "subject": "participant",
                "confidence": 0.95,
                "reason": "scripted header-only classification",
                "citation": "45 CFR 164.514(b)(2)(i)",
            }
        )

    assert review_target is not None
    for decision in proposed:
        if (decision["file_id"], decision["column"]) == review_target:
            decision["confidence"] = 0.10
            break

    gateway = FakeGateway()
    gateway.replies.append(json.dumps({"decisions": proposed}))
    judge = _ScriptedJudge(make_ctx("Judge", gateway=gateway))
    handled = asyncio.run(judge.run(header_only_input))

    planted_literals = {
        literal
        for planted_cell in planted.ground_truth["planted"]
        if planted_cell["hipaa_category"] != "NONE"
        for literal in planted_cell["leak_literals"]
        if literal
    }
    assert len(gateway.requests) == 1
    assert all(literal not in gateway.requests[0].user_prompt for literal in planted_literals)

    gated = _run_deterministic_gates(handled["decisions"], dataset_paths, stats)
    pending = [decision for decision in gated if decision["action"] == "human_review"]
    assert pending

    # Human review confirms every pending row, then the same deterministic gates run again.
    resumed_proposals: list[dict[str, Any]] = []
    for decision in gated:
        resolved = dict(decision)
        if resolved["action"] == "human_review":
            resolved.update(
                action=resolved.get("suggested_action") or "drop",
                confidence=1.0,
                provenance="human_explicit_action",
                reviewer="offline-reviewer",
                reviewer_comment="Reviewed against the delivered source file.",
                actual_knowledge_ack=True,
            )
        resumed_proposals.append(resolved)
    resumed = _run_deterministic_gates(resumed_proposals, dataset_paths, stats)
    assert not [decision for decision in resumed if decision["action"] == "human_review"]

    from phi_core.agents import reasoning

    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    monkeypatch.setattr(reasoning, "EXPORT_DIR", export_dir)

    async def complete_local_workflow() -> tuple[dict[str, str], dict[str, Any], dict[str, Any]]:
        exports = (await Executor(make_ctx("Executor")).run(files, resumed))["exports"]
        operator = await Operator(make_ctx("Operator")).run(files, resumed, exports)
        reviewer = await Reviewer(make_ctx("Reviewer")).run(resumed, operator, exports)
        return reviewer["exports"], operator, reviewer

    exports, operator_result, reviewer_result = asyncio.run(complete_local_workflow())
    assert operator_result["status"] == "clean"
    assert reviewer_result["status"] == "clean"
    assert set(exports) == {file["file_id"] for file in files}

    leak_scan = scan_exports_for_leaks(planted.ground_truth, exports)
    assert leak_scan["status"] == "clean", leak_scan["hits"]

    guard = scan_all_exports(exports, resumed, jurisdiction="us")
    assert guard.status == "clean"

    session = {
        "id": "full-path",
        "jurisdiction": "us",
        "status": "complete",
        "files": files,
        "agent_decisions": resumed,
        "export_paths": exports,
        "guard_report": guard.to_dict(),
        "session_review": [
            {
                "reviewer": "offline-reviewer",
                "comment": "Reviewed against the delivered source file.",
                "reviewed_at": "2026-01-01T00:00:00+00:00",
                "actual_knowledge_ack": True,
            }
        ],
    }
    bundle_bytes, _bundle_name = build_bundle(session, BundleOptions())

    with zipfile.ZipFile(io.BytesIO(bundle_bytes)) as bundle:
        attestation = json.loads(bundle.read("safe_to_share/attestation.json"))
        for file_id, export_path in exports.items():
            archive_name = f"safe_to_share/datasets/{file_id}{Path(export_path).suffix}"
            served_bytes = Path(export_path).read_bytes()
            assert bundle.read(archive_name) == served_bytes
            assert attestation["files"][archive_name] == f"sha256:{hashlib.sha256(served_bytes).hexdigest()}"
