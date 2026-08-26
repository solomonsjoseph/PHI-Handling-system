"""Tests for the per-dataset benchmark report (backend/phi_corpus/benchmark.py)
and its server-side assembly helper (server._build_corpus_benchmark_report)."""
from __future__ import annotations

import json

import pytest
from phi_corpus.benchmark import (
    ACTION_SPECS,
    _context_hygiene,
    build_report,
    per_column_csv,
    render_figures,
    to_json,
    to_markdown,
)
from phi_corpus.planters import plant


def _stub_verify_report(leak_hits=None, precision=1.0, recall=1.0, f1=1.0, accuracy=1.0,
                         deferral_rate=0.0):
    return {
        "correctness": {
            "overall_precision": precision, "overall_recall": recall,
            "overall_f1": f1, "overall_accuracy": accuracy,
        },
        "deferral": {"rate": deferral_rate},
        "leak": {"hits": leak_hits or []},
        "transform": {"rate": 1.0},
        "utility": {"rate": 1.0},
        "regulation": {"planted": {"A": 1}, "neutralised": {"A": 1}, "leaked": {"A": 0}},
        "guard_status": "clean",
    }


def test_build_report_columns_match_ground_truth_and_mark_correct():
    """When every decision matches the gold expected action, every column
    entry must verdict 'correct' and carry a non-empty how/why via
    ACTION_SPECS, exactly the report's headline claim."""
    art = plant(scenario_id="oncology_v1", row_count=12, seed=7)
    gt = art.ground_truth
    columns = gt["columns"]

    decisions = [
        {
            "file_id": c["file_name"],  # already remapped to ground-truth file_name
            "column": c["column"],
            "action": c["expected_action"],
            "phi_category": c["hipaa_category"] if c["hipaa_category"] != "NONE" else None,
            "subject": "participant",
            "confidence": 0.91,
            "reason": "matches gold expectation",
            "citation": "45 CFR 164.514",
        }
        for c in columns
    ]

    report = build_report(
        ground_truth=gt, decisions=decisions, verify_report=_stub_verify_report(),
        mode="agentic",
    )

    assert report["totals"]["columns_total"] == len(columns)
    assert report["meta"]["mode"] == "agentic"
    for c in report["columns"]:
        assert c["verdict"] == "correct", c
        assert c["method_exact"] is True
        assert c["decided_by"] == "judge_llm"
        spec = ACTION_SPECS[c["action"]]
        assert c["transform"] == spec["transform"]
        assert c["authority"] == spec["authority"]
        assert c["reason"]
    # Agentic-only inputs were never supplied -> each is listed unavailable.
    unavailable_sections = {u["section"] for u in report["unavailable"]}
    assert "agent_praxis" in unavailable_sections
    assert "context_hygiene" in unavailable_sections


def test_build_report_unmapped_column_and_leak_are_flagged():
    """A ground-truth column with no matching decision must verdict
    'under_block' (or 'over_block') via the MISSING-action path, be tagged
    decided_by='unmapped_default', and a leak hit on a column must surface
    in that column's leak_hits count."""
    art = plant(scenario_id="oncology_v1", row_count=12, seed=3)
    gt = art.ground_truth
    columns = gt["columns"]
    phi_col = next(c for c in columns if c["hipaa_category"] != "NONE")

    leak_hit = {"file": phi_col["file_name"], "column": phi_col["column"],
                "plant_id": "p9999", "hipaa_category": phi_col["hipaa_category"]}

    report = build_report(
        ground_truth=gt, decisions=[],  # nothing decided at all
        verify_report=_stub_verify_report(leak_hits=[leak_hit]),
        mode="agentic",
    )
    col_entry = next(c for c in report["columns"] if c["column"] == phi_col["column"])
    assert col_entry["decided_by"] == "unmapped_default"
    assert col_entry["action"] is None
    assert col_entry["action_label"] == "Unmapped (no decision)"
    assert col_entry["leak_hits"] == 1
    assert col_entry["verdict"] == "under_block"


def test_renderers_produce_nonempty_artifacts():
    art = plant(scenario_id="oncology_v1", row_count=12, seed=11)
    gt = art.ground_truth
    decisions = [
        {"file_id": c["file_name"], "column": c["column"], "action": c["expected_action"],
         "confidence": 0.8, "reason": "r", "citation": "c"}
        for c in gt["columns"]
    ]
    report = build_report(
        ground_truth=gt, decisions=decisions, verify_report=_stub_verify_report(),
        mode="agentic",
    )

    js = to_json(report)
    parsed = json.loads(js)
    assert parsed["meta"]["scenario_id"] == "oncology_v1"

    md = to_markdown(report)
    assert "# Benchmark report" in md
    assert "## Per-column decisions" in md
    assert "## Regulation coverage" in md
    assert "## Differentiation" in md

    csv_bytes = per_column_csv(report)
    assert csv_bytes.count(b"\n") == len(report["columns"]) + 1  # header + one row per column

    figures = render_figures(report)
    assert set(figures.keys()) == {
        "fig1_per_column_confidence.png", "fig2_regulation_coverage.png", "fig3_autonomy.png",
    }
    for png in figures.values():
        assert png[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.asyncio
async def test_server_benchmark_route_remaps_pipeline_file_id(monkeypatch):
    """The live-session route keys decisions/schema by the pipeline's
    internal file_id (a UUID-shaped string distinct from the original file
    name); the route must remap that back to ground-truth file_name before
    calling build_report, or every column would wrongly report
    unmapped_default."""
    import server as srv

    art = plant(scenario_id="oncology_v1", row_count=12, seed=5)
    gt = art.ground_truth
    columns = gt["columns"]
    file_name = columns[0]["file_name"]
    pipeline_file_id = "f_9c8b7a6d"

    decisions = [
        {
            "file_id": pipeline_file_id,
            "column": c["column"],
            "action": c["expected_action"],
            "phi_category": c["hipaa_category"] if c["hipaa_category"] != "NONE" else None,
            "confidence": 0.85,
            "reason": "matches gold",
            "citation": "45 CFR 164.514",
        }
        for c in columns
    ]
    schema_columns = [
        {"_file_id": pipeline_file_id, "name": c["column"]}
        for c in columns
    ]

    doc = {
        "id": "sid",
        "corpus_ground_truth": gt,
        "files": [{"original_name": file_name, "file_id": pipeline_file_id}],
        "agent_decisions": decisions,
        "agent_specialists": {"schema": {"columns": schema_columns}},
        "export_paths": {},
        "guard_report": {"status": "clean", "results": []},
    }

    report = srv._build_corpus_benchmark_report(doc, [])

    assert report["totals"]["columns_total"] == len(columns)
    unavailable_sections = {u["section"] for u in report["unavailable"]}
    assert "schema_columns" not in unavailable_sections
    for c in report["columns"]:
        assert c["decided_by"] != "unmapped_default", c


def test_context_hygiene_reads_renamed_prompt_text_field():
    """The agent-trace redesign renamed `prompt_preview`/`prompt_full` to a
    single always-full `prompt_text`. The context-hygiene metric MUST read
    that field, not the old ones -- silently reading nothing here would
    report a false-clean 0-audited result instead of catching a real leak."""
    ground_truth = {"planted": [{"leak_literals": ["James Smith", "415-555-1234"]}]}
    agent_log = [
        {"agent": "Judge", "phase": "judge.decide",
         "payload": {"prompt_text": "Column headers: name, phone. No row values."}},
        {"agent": "Judge", "phase": "judge.decide",
         "payload": {"prompt_text": "Leaked cell value: James Smith called from 415-555-1234."}},
        {"agent": "Sentinel", "phase": "sentinel.review", "payload": {"error": "timeout"}},
    ]
    hygiene = _context_hygiene(agent_log, ground_truth, prompt_scrub_counts={"lexicon": 3})
    assert hygiene["prompts_audited"] == 2, "must count every message carrying prompt_text"
    assert hygiene["literals_found_in_prompts"] == 2, "must still detect literals via prompt_text"
    assert hygiene["clean"] is False
    assert hygiene["identifiers_removed_before_prompt"] == 3

    # The old field names must no longer be read at all -- a message that
    # only carries the legacy shape is silently skipped, not miscounted.
    legacy_only_log = [{"agent": "Judge", "phase": "judge.decide",
                        "payload": {"prompt_preview": "James Smith", "prompt_full": "James Smith"}}]
    legacy_hygiene = _context_hygiene(legacy_only_log, ground_truth, prompt_scrub_counts={})
    assert legacy_hygiene["prompts_audited"] == 0
