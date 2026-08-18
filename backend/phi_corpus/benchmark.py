"""Per-dataset benchmark report.

Builds a single-run report -- per column: the method chosen, why, how it
was applied, the confidence behind it, the gold verdict -- plus headline
totals (leak rate, precision/recall/F1, method-exact rate, autonomy rate)
and an evidence-backed comparison against existing tools. Serves both the
live agentic pipeline (``mode="agentic"``) and the offline deterministic
replay path (``mode="deterministic_replay"``), which is the reproducibility
path: anyone can regenerate the numbers from the repo alone, no Mongo, no
LLM, no network.

Callers whose decisions/schema_columns are keyed by the pipeline's internal
``file_id`` (the live agentic path) must remap that key back to the
ground-truth ``file_name`` before calling :func:`build_report`, the same
normalisation :func:`phi_corpus.verify.verify` requires of its own callers.
The offline replay path needs no remapping: ``replay.replay`` already keys
its decisions by the original dataset file name.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from phi_core.coverage_matrix import COVERAGE, TOOLS, coverage_counts as _coverage_counts
from .verify import _PHI_ACTIONS, mask as _mask


# ---------------------------------------------------------------------
# 3.1 Method vocabulary table -- source of truth for the report's "how".
# ---------------------------------------------------------------------

ACTION_SPECS: dict[str, dict[str, str]] = {
    "keep": {
        "label": "Preserved verbatim",
        "transform": "Value written unchanged.",
        "authority": "45 CFR 164.514(b)(2)(i) - not a listed identifier",
    },
    "drop": {
        "label": "Removed",
        "transform": "Cell replaced with the empty string; the column and its header remain so downstream schemas do not break.",
        "authority": "45 CFR 164.514(b)(2)(i)",
    },
    "cap_age_90": {
        "label": "Age aggregated at 90",
        "transform": "Non-digits stripped, parsed as an integer; values above 89 become the literal '90+', values 89 and below are written unchanged.",
        "authority": "45 CFR 164.514(b)(2)(i)(C)",
    },
    "year_only": {
        "label": "Date truncated to year",
        "transform": "First four-digit run extracted and written alone; a value with no four-digit run becomes the empty string.",
        "authority": "45 CFR 164.514(b)(2)(i)(C)",
    },
    "zip3_truncate": {
        "label": "ZIP truncated to three digits",
        "transform": "Non-digits stripped, first three digits kept, right-padded to three; the seventeen restricted prefixes are remapped to '000'.",
        "authority": "45 CFR 164.514(b)(2)(i)(B)",
    },
    "hash": {
        "label": "Keyed digest",
        "transform": "HMAC-SHA256 over 'column:value' under a server-held key, truncated to sixteen hex characters. The key is not included in any export.",
        "authority": "45 CFR 164.514(c)",
    },
    "pseudonymize": {
        "label": "Study-scoped pseudonym",
        "transform": "Stable 'P' + eight hex characters per distinct value, derived from a per-session HMAC salt so the same real value maps to the same token across every file in the study and to nothing in any other study.",
        "authority": "45 CFR 164.514(c)",
    },
    "scrub_text": {
        "label": "Free-text cell scrub",
        "transform": "Presidio plus regex detectors run over each cell; each detected span is replaced with its bracketed HIPAA category token and the surrounding clinical prose is preserved. No cell content reaches a language model.",
        "authority": "45 CFR 164.514(b)(1)",
    },
    "human_review": {
        "label": "Deferred to a human",
        "transform": "No transform applied; the run halts before the Executor and the column waits for a reviewer decision.",
        "authority": "45 CFR 164.514(b)(2)(ii)",
    },
}

_UNMAPPED_SPEC = {"label": "Unmapped (no decision)", "transform": "", "authority": ""}

_CALIBRATION_BUCKETS: list[tuple[str, float, float, bool]] = [
    ("[0.0,0.6)", 0.0, 0.6, False),
    ("[0.6,0.8)", 0.6, 0.8, False),
    ("[0.8,0.95)", 0.8, 0.95, False),
    ("[0.95,1.0]", 0.95, 1.0, True),
]

_COLUMN_FIELDS = [
    "file", "column", "gold_category", "gold_expected_action",
    "predicted_category", "action", "action_label", "transform", "authority",
    "reason", "citation", "confidence", "schema_confidence", "schema_category",
    "praxis_technique", "praxis_utility_preserving", "decided_by",
    "verdict", "method_exact", "cells_total", "cells_changed", "leak_hits",
    "expectation_kind",
]


def _bucket_for(confidence: float) -> str | None:
    for label, lo, hi, inclusive in _CALIBRATION_BUCKETS:
        if inclusive:
            if lo <= confidence <= hi:
                return label
        elif lo <= confidence < hi:
            return label
    return None


def _context_hygiene(
    agent_log: list[dict[str, Any]],
    ground_truth: dict[str, Any],
    prompt_scrub_counts: dict[str, int],
) -> dict[str, Any]:
    literals: set[str] = set()
    for cell in ground_truth.get("planted", []) or []:
        for lit in cell.get("leak_literals") or []:
            if lit and len(lit) >= 4:
                literals.add(lit.lower())

    prompts_audited = 0
    prompt_chars_audited = 0
    literals_found = 0
    violations: list[dict[str, str]] = []
    for msg in agent_log or []:
        payload = msg.get("payload") or {}
        text = payload.get("prompt_text")
        if not text:
            continue
        prompts_audited += 1
        prompt_chars_audited += len(text)
        lower = text.lower()
        for lit in literals:
            if lit in lower:
                literals_found += 1
                violations.append({
                    "agent": msg.get("agent", ""),
                    "phase": msg.get("phase", ""),
                    "literal": _mask(lit),
                })

    return {
        "identifiers_removed_before_prompt": sum((prompt_scrub_counts or {}).values()),
        "prompts_audited": prompts_audited,
        "prompt_chars_audited": prompt_chars_audited,
        "planted_literals_checked": len(literals),
        "literals_found_in_prompts": literals_found,
        "clean": literals_found == 0,
        "violations": violations,
    }


def _differentiation() -> dict[str, Any]:
    return {
        "tools": TOOLS,
        "coverage_rows": COVERAGE,
        "coverage_counts": _coverage_counts(),
        "prior_art": [
            {
                "name": "Multi-agent LLM de-identification pipelines",
                "citation": "ACL 2026",
                "url": "https://aclanthology.org/2026.acl-long.503.pdf",
            },
            {
                "name": "Metadata-only column classifier",
                "citation": "Ethyca engineering blog",
                "url": "https://www.ethyca.com/insights/engineering-llm-data-classifier-metadata-only",
            },
        ],
        "distinctives": [
            "A machine-checked no-raw-identifier-to-model invariant, measured per run as context_hygiene rather than asserted.",
            "A deterministic executor behind a fail-closed Publish Guard that cannot certify an empty scan as clean.",
            "A shipped adversarial corpus with per-plant, per-column grading rather than a held-out eval set nobody can rerun.",
            "A per-run autonomy rate: the fraction of columns decided without deferring to a human reviewer.",
        ],
    }


# ---------------------------------------------------------------------
# 3.2 Report builder
# ---------------------------------------------------------------------

def build_report(
    *,
    ground_truth: dict[str, Any],
    decisions: list[dict[str, Any]],
    verify_report: dict[str, Any],
    mode: str,
    praxis_methods: dict[str, Any] | None = None,
    schema_columns: list[dict[str, Any]] | None = None,
    sentinel_overrides: list[dict[str, Any]] | None = None,
    keep_demotions: list[dict[str, Any]] | None = None,
    guard_report: dict[str, Any] | None = None,
    prompt_scrub_counts: dict[str, int] | None = None,
    agent_log: list[dict[str, Any]] | None = None,
    phase_timings: Any = None,
    run_elapsed_s: float | None = None,
    model_output_rejections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the per-dataset benchmark report.

    Every agentic-only input is optional. When one is absent (``None``),
    the corresponding section is left at its zero-value and
    ``report["unavailable"]`` gains an entry naming it, rather than
    silently reporting a zero as though it had been measured.
    """
    unavailable: list[dict[str, str]] = []

    def _absent(name: str, value: Any) -> None:
        if value is None:
            unavailable.append({"section": name, "reason": "not produced in deterministic_replay mode"})

    for name, value in (
        ("agent_praxis", praxis_methods),
        ("schema_columns", schema_columns),
        ("sentinel_overrides", sentinel_overrides),
        ("keep_demotions", keep_demotions),
        ("guard_report", guard_report),
        ("prompt_scrub_counts", prompt_scrub_counts),
        ("context_hygiene", agent_log),
        ("phase_timings", phase_timings),
        ("run_elapsed_s", run_elapsed_s),
        ("model_output_rejections", model_output_rejections),
    ):
        _absent(name, value)

    praxis_methods = praxis_methods or {}
    schema_columns = schema_columns or []
    sentinel_overrides = sentinel_overrides or []
    keep_demotions = keep_demotions or []
    prompt_scrub_counts = prompt_scrub_counts or {}
    agent_log_list = agent_log or []
    model_output_rejections = model_output_rejections or []

    # ---- indexes ------------------------------------------------------
    decision_index: dict[tuple[str, str], dict[str, Any]] = {}
    for d in decisions:
        decision_index[(d.get("file_id", ""), d.get("column", ""))] = d

    schema_index: dict[tuple[str, str], dict[str, Any]] = {}
    for c in schema_columns:
        schema_index[(c.get("_file_id", ""), c.get("name", ""))] = c

    keep_demote_keys = {(d.get("file_id", ""), d.get("column", "")) for d in keep_demotions}
    sentinel_override_keys = {(o.get("file_id", ""), o.get("column", "")) for o in sentinel_overrides}

    leak_hits = ((verify_report.get("leak") or {}).get("hits")) or []
    leak_count_by_key: dict[tuple[str, str], int] = {}
    for h in leak_hits:
        key = (h.get("file", ""), h.get("column", ""))
        leak_count_by_key[key] = leak_count_by_key.get(key, 0) + 1
    leaked_plant_ids = {h.get("plant_id") for h in leak_hits if h.get("plant_id")}

    planted_by_col: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for cell in ground_truth.get("planted", []) or []:
        planted_by_col.setdefault((cell.get("file_name", ""), cell.get("column", "")), []).append(cell)

    # ---- per-column walk ------------------------------------------------
    columns_out: list[dict[str, Any]] = []
    for col_meta in ground_truth.get("columns", []) or []:
        file_name = col_meta.get("file_name", "")
        column = col_meta.get("column", "")
        gold_category = col_meta.get("hipaa_category", "")
        gold_expected_action = col_meta.get("expected_action", "")
        key = (file_name, column)

        dec = decision_index.get(key)
        action = dec.get("action") if dec else None
        spec = ACTION_SPECS.get(action, _UNMAPPED_SPEC)

        if dec and (dec.get("reviewer") or "").strip():
            decided_by = "human"
        elif key in keep_demote_keys:
            decided_by = "keep_verification"
        elif key in sentinel_override_keys:
            decided_by = "sentinel_hard_rule"
        elif dec is None:
            decided_by = "unmapped_default"
        else:
            decided_by = "judge_llm"

        if action == "human_review":
            verdict = "deferred"
        else:
            expected_is_phi = gold_expected_action in _PHI_ACTIONS
            actual_is_phi = action in _PHI_ACTIONS
            if expected_is_phi and not actual_is_phi:
                verdict = "under_block"
            elif not expected_is_phi and actual_is_phi:
                verdict = "over_block"
            else:
                verdict = "correct"

        praxis_entry = praxis_methods.get(gold_category) if gold_category else None
        schema_entry = schema_index.get(key)
        first_praxis_method = ((praxis_entry or {}).get("methods") or [{}])[0]


        cells = planted_by_col.get(key, [])
        cells_total = len(cells)
        cells_changed = 0
        expectation_kind = None
        for cell in cells:
            expectation = cell.get("expectation")
            if expectation is None:
                cells_changed += 1
                continue
            if not expectation.get("survives_verbatim"):
                cells_changed += 1
            if expectation_kind is None:
                expectation_kind = expectation.get("kind")

        columns_out.append({
            "file": file_name,
            "column": column,
            "gold_category": gold_category,
            "gold_expected_action": gold_expected_action,
            "predicted_category": dec.get("phi_category") if dec else None,
            "action": action,
            "action_label": spec["label"],
            "transform": spec["transform"],
            "authority": spec["authority"],
            "reason": (dec.get("reason") if dec else "") or "",
            "citation": (dec.get("citation") if dec else "") or "",
            "confidence": dec.get("confidence") if dec else None,
            "schema_confidence": schema_entry.get("confidence") if schema_entry else None,
            "schema_category": schema_entry.get("candidate_phi_category") if schema_entry else None,
            "praxis_technique": first_praxis_method.get("name"),
            "praxis_utility_preserving": first_praxis_method.get("utility_preserving"),
            "decided_by": decided_by,
            "verdict": verdict,
            "method_exact": action == gold_expected_action,
            "cells_total": cells_total,
            "cells_changed": cells_changed,
            "leak_hits": leak_count_by_key.get(key, 0),
            "expectation_kind": expectation_kind,
        })

    # ---- totals ---------------------------------------------------------
    columns_total = len(columns_out)
    phi_columns = sum(1 for c in columns_out if c["gold_expected_action"] in _PHI_ACTIONS)
    clinical_columns = columns_total - phi_columns
    dropped = sum(1 for c in columns_out if c["action"] == "drop")
    transformed = sum(1 for c in columns_out if c["action"] in (_PHI_ACTIONS - {"drop"}))
    kept = sum(1 for c in columns_out if c["action"] == "keep")
    deferred = sum(1 for c in columns_out if c["action"] == "human_review")

    phi_planted_ids = {
        cell.get("plant_id")
        for cell in ground_truth.get("planted", []) or []
        if cell.get("expected_action") in _PHI_ACTIONS and cell.get("plant_id")
    }
    phi_cells_planted = len(phi_planted_ids)
    phi_cells_leaked = len(phi_planted_ids & leaked_plant_ids)
    phi_cells_neutralised = phi_cells_planted - phi_cells_leaked
    leak_rate = round(phi_cells_leaked / phi_cells_planted, 4) if phi_cells_planted else 0.0

    correctness = verify_report.get("correctness", {})
    deferral = verify_report.get("deferral", {})
    method_exact_rate = (
        round(sum(1 for c in columns_out if c["method_exact"]) / columns_total, 4)
        if columns_total else 1.0
    )
    deferral_rate = deferral.get("rate", 0.0)
    utility = verify_report.get("utility") or {}
    transform = verify_report.get("transform") or {}
    guard_status = guard_report.get("status") if guard_report is not None else verify_report.get("guard_status")

    totals = {
        "columns_total": columns_total,
        "phi_columns": phi_columns,
        "clinical_columns": clinical_columns,
        "dropped": dropped,
        "transformed": transformed,
        "kept": kept,
        "deferred": deferred,
        "phi_cells_planted": phi_cells_planted,
        "phi_cells_neutralised": phi_cells_neutralised,
        "phi_cells_leaked": phi_cells_leaked,
        "leak_rate": leak_rate,
        "precision": correctness.get("overall_precision", 1.0),
        "recall": correctness.get("overall_recall", 1.0),
        "f1": correctness.get("overall_f1", 1.0),
        "accuracy": correctness.get("overall_accuracy", 1.0),
        "method_exact_rate": method_exact_rate,
        "autonomy_rate": round(1.0 - deferral_rate, 4),
        "deferral_rate": deferral_rate,
        "utility_rate": utility.get("rate", 1.0),
        "transform_conformance": transform.get("rate", 1.0),
        "guard_status": guard_status,
        "model_output_rejections": len(model_output_rejections),
    }

    # ---- calibration ------------------------------------------------------
    calibration = {label: {"count": 0, "correct": 0, "accuracy": 1.0} for label, *_ in _CALIBRATION_BUCKETS}
    for c in columns_out:
        conf = c["confidence"]
        if conf is None:
            continue
        label = _bucket_for(float(conf))
        if label is None:
            continue
        calibration[label]["count"] += 1
        if c["verdict"] == "correct":
            calibration[label]["correct"] += 1
    for label, bucket in calibration.items():
        bucket["accuracy"] = round(bucket["correct"] / bucket["count"], 4) if bucket["count"] else 1.0

    context_hygiene = _context_hygiene(agent_log_list, ground_truth, prompt_scrub_counts) if agent_log is not None else None

    meta = {
        "scenario_id": ground_truth.get("scenario_id"),
        "jurisdiction": ground_truth.get("jurisdiction"),
        "tier": ground_truth.get("tier"),
        "profile": ground_truth.get("profile"),
        "corpus_version": ground_truth.get("corpus_version"),
        "seed": ground_truth.get("seed"),
        "row_count": ground_truth.get("row_count"),
        "edge_case_tags": ground_truth.get("edge_case_tags", []),
        "mode": mode,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_elapsed_s": run_elapsed_s,
    }

    return {
        "meta": meta,
        "columns": columns_out,
        "totals": totals,
        "regulation": verify_report.get("regulation", {}),
        "calibration": calibration,
        "context_hygiene": context_hygiene,
        "differentiation": _differentiation(),
        "unavailable": unavailable,
    }


# ---------------------------------------------------------------------
# 3.3 Renderers
# ---------------------------------------------------------------------

def to_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, default=str)


def _reproduction_command(report: dict[str, Any]) -> str:
    meta = report["meta"]
    return (
        f"python -m phi_corpus.benchmark --scenario {meta.get('scenario_id')} "
        f"--rows {meta.get('row_count')} --seed {meta.get('seed')} --out <dir>"
        f"   # corpus_version={meta.get('corpus_version')}"
    )


def to_markdown(report: dict[str, Any]) -> str:
    meta = report["meta"]
    totals = report["totals"]
    lines: list[str] = []

    lines.append(f"# Benchmark report: {meta.get('scenario_id')} ({meta.get('mode')})")
    lines.append("")
    lines.append("## Headline totals")
    lines.append("")
    for key in (
        "columns_total", "phi_columns", "clinical_columns", "dropped", "transformed",
        "kept", "deferred", "phi_cells_planted", "phi_cells_neutralised", "phi_cells_leaked",
        "leak_rate", "precision", "recall", "f1", "accuracy", "method_exact_rate",
        "autonomy_rate", "deferral_rate", "utility_rate", "transform_conformance",
        "guard_status", "model_output_rejections",
    ):
        lines.append(f"- {key}: {totals.get(key)}")
    lines.append("")
    lines.append("## Reproduction")
    lines.append("")
    lines.append("```")
    lines.append(_reproduction_command(report))
    lines.append("```")
    lines.append("")

    lines.append("## Per-column decisions")
    lines.append("")
    lines.append("| file | column | gold category | action | method | why | confidence | decided by | verdict |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for c in report["columns"]:
        conf = "" if c["confidence"] is None else f"{c['confidence']:.2f}"
        why = (c["reason"] or "").replace("|", "\\|").replace("\n", " ")[:160]
        lines.append(
            f"| {c['file']} | {c['column']} | {c['gold_category']} | {c['action']} | "
            f"{c['action_label']} | {why} | {conf} | {c['decided_by']} | {c['verdict']} |"
        )
    lines.append("")

    lines.append("## How each method works")
    lines.append("")
    seen_actions = sorted({c["action"] for c in report["columns"] if c["action"]})
    for action in seen_actions:
        spec = ACTION_SPECS.get(action, _UNMAPPED_SPEC)
        lines.append(f"- **{action}** ({spec['label']}, {spec['authority']}): {spec['transform']}")
    lines.append("")

    lines.append("## Regulation coverage")
    lines.append("")
    regulation = report.get("regulation") or {}
    lines.append("| HIPAA key | planted | neutralised | leaked |")
    lines.append("|---|---|---|---|")
    planted_map = regulation.get("planted", {}) if isinstance(regulation, dict) else {}
    neutralised_map = regulation.get("neutralised", {}) if isinstance(regulation, dict) else {}
    leaked_map = regulation.get("leaked", {}) if isinstance(regulation, dict) else {}
    for k in sorted(planted_map.keys()):
        lines.append(f"| {k} | {planted_map.get(k, 0)} | {neutralised_map.get(k, 0)} | {leaked_map.get(k, 0)} |")
    lines.append("")

    lines.append("## Confidence calibration")
    lines.append("")
    lines.append("| bucket | count | correct | accuracy |")
    lines.append("|---|---|---|---|")
    for label, bucket in report["calibration"].items():
        lines.append(f"| {label} | {bucket['count']} | {bucket['correct']} | {bucket['accuracy']} |")
    lines.append("")

    lines.append("## Context hygiene")
    lines.append("")
    ch = report.get("context_hygiene")
    if ch is None:
        lines.append("Not measured in this mode.")
    else:
        for key in (
            "identifiers_removed_before_prompt", "prompts_audited", "prompt_chars_audited",
            "planted_literals_checked", "literals_found_in_prompts", "clean",
        ):
            lines.append(f"- {key}: {ch.get(key)}")
        if ch.get("violations"):
            lines.append("")
            lines.append("Violations:")
            for v in ch["violations"]:
                lines.append(f"- {v.get('agent')} / {v.get('phase')}: {v.get('literal')}")
    lines.append("")

    lines.append("## Differentiation")
    lines.append("")
    diff = report["differentiation"]
    lines.append("Prior art:")
    for p in diff["prior_art"]:
        lines.append(f"- {p['name']} ({p['citation']}): {p['url']}")
    lines.append("")
    lines.append("Distinctives measured by this report:")
    for d in diff["distinctives"]:
        lines.append(f"- {d}")
    lines.append("")

    lines.append("## Defects")
    lines.append("")
    defects = [c for c in report["columns"] if c["verdict"] != "correct"]
    if not defects:
        lines.append("None. Every column scored correct.")
    else:
        lines.append("| file | column | verdict | gold expected | actual |")
        lines.append("|---|---|---|---|---|")
        for c in defects:
            lines.append(f"| {c['file']} | {c['column']} | {c['verdict']} | {c['gold_expected_action']} | {c['action']} |")
    lines.append("")

    if report.get("unavailable"):
        lines.append("## Unavailable in this mode")
        lines.append("")
        for u in report["unavailable"]:
            lines.append(f"- {u['section']}: {u['reason']}")
        lines.append("")

    return "\n".join(lines)


def per_column_csv(report: dict[str, Any]) -> bytes:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(_COLUMN_FIELDS)
    for c in report["columns"]:
        w.writerow([c.get(f) for f in _COLUMN_FIELDS])
    return out.getvalue().encode("utf-8")


# ---------------------------------------------------------------------
# 3.4 Figures
# ---------------------------------------------------------------------

_VERDICT_COLORS = {
    "correct": "#8C2135",
    "over_block": "#C98A00",
    "under_block": "#12141A",
    "deferred": "#6B7280",
}


def render_figures(report: dict[str, Any]) -> dict[str, bytes]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figures: dict[str, bytes] = {}
    columns = report["columns"]
    mode = report["meta"].get("mode")

    # fig1: per-column confidence (agentic) or verdict counts (replay).
    fig, ax = plt.subplots(figsize=(10, max(3, 0.35 * max(len(columns), 1))))
    if mode == "agentic" and any(c["confidence"] is not None for c in columns):
        labels = [f"{c['file']}:{c['column']}" for c in columns]
        values = [c["confidence"] or 0.0 for c in columns]
        colors = [_VERDICT_COLORS.get(c["verdict"], "#6B7280") for c in columns]
        ax.barh(labels, values, color=colors)
        for i, c in enumerate(columns):
            ax.text(min(values[i] + 0.02, 0.98), i, c["action_label"], va="center", fontsize=7, color="#12141A")
        ax.set_xlim(0, 1.05)
        ax.set_xlabel("Judge confidence")
        ax.set_title("Per-column confidence and verdict", loc="left", color="#12141A", weight="bold")
    else:
        report.setdefault("unavailable", []).append({
            "section": "fig1_per_column_confidence",
            "reason": "no confidence in deterministic_replay mode; substituted verdict counts",
        })
        by_verdict: dict[str, int] = {}
        for c in columns:
            by_verdict[c["verdict"]] = by_verdict.get(c["verdict"], 0) + 1
        labels = list(by_verdict.keys())
        values = [by_verdict[k] for k in labels]
        colors = [_VERDICT_COLORS.get(k, "#6B7280") for k in labels]
        ax.barh(labels, values, color=colors)
        ax.set_xlabel("Column count")
        ax.set_title("Columns per verdict (no confidence in this mode)", loc="left", color="#12141A", weight="bold")
    plt.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor="#F7F5F0")
    plt.close(fig)
    figures["fig1_per_column_confidence.png"] = buf.getvalue()

    # fig2: regulation coverage, neutralised vs leaked.
    regulation = report.get("regulation") or {}
    neutralised_map = regulation.get("neutralised", {}) if isinstance(regulation, dict) else {}
    leaked_map = regulation.get("leaked", {}) if isinstance(regulation, dict) else {}
    keys = sorted(set(neutralised_map.keys()) | set(leaked_map.keys()))
    fig, ax = plt.subplots(figsize=(9, max(3, 0.3 * max(len(keys), 1))))
    neutralised_vals = [neutralised_map.get(k, 0) for k in keys]
    leaked_vals = [leaked_map.get(k, 0) for k in keys]
    ax.barh(keys, neutralised_vals, color="#8C2135", label="neutralised")
    ax.barh(keys, leaked_vals, left=neutralised_vals, color="#C1121F", label="leaked")
    ax.set_xlabel("Planted identifiers")
    ax.set_title("Regulation coverage: neutralised vs leaked", loc="left", color="#12141A", weight="bold")
    ax.legend(loc="lower right", fontsize=8)
    plt.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor="#F7F5F0")
    plt.close(fig)
    figures["fig2_regulation_coverage.png"] = buf.getvalue()

    # fig3: autonomy -- column counts by decided_by.
    by_decided: dict[str, int] = {}
    for c in columns:
        by_decided[c["decided_by"]] = by_decided.get(c["decided_by"], 0) + 1
    labels = list(by_decided.keys())
    values = [by_decided[k] for k in labels]
    colors = ["#12141A" if k == "human" else "#8C2135" for k in labels]
    fig, ax = plt.subplots(figsize=(8, max(3, 0.4 * max(len(labels), 1))))
    ax.barh(labels, values, color=colors)
    ax.set_xlabel("Column count")
    ax.set_title("Autonomy: who decided each column", loc="left", color="#12141A", weight="bold")
    plt.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor="#F7F5F0")
    plt.close(fig)
    figures["fig3_autonomy.png"] = buf.getvalue()

    return figures


# ---------------------------------------------------------------------
# 3.5 Artifact writers
# ---------------------------------------------------------------------

def write(report: dict[str, Any], out_dir: Path) -> dict[str, str]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}

    md_path = out_dir / "benchmark_report.md"
    json_path = out_dir / "benchmark_report.json"
    csv_path = out_dir / "per_column.csv"
    md_path.write_text(to_markdown(report), encoding="utf-8")
    json_path.write_text(to_json(report), encoding="utf-8")
    csv_path.write_bytes(per_column_csv(report))
    paths["markdown"] = str(md_path)
    paths["json"] = str(json_path)
    paths["csv"] = str(csv_path)

    for name, png_bytes in render_figures(report).items():
        p = out_dir / name
        p.write_bytes(png_bytes)
        paths[name] = str(p)

    return paths


def bundle_zip(report: dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("benchmark/benchmark_report.md", to_markdown(report))
        z.writestr("benchmark/benchmark_report.json", to_json(report))
        z.writestr("benchmark/per_column.csv", per_column_csv(report))
        for name, png_bytes in render_figures(report).items():
            z.writestr(f"benchmark/{name}", png_bytes)
    return buf.getvalue()


# ---------------------------------------------------------------------
# Live-session assembly, shared by the two server routes and bundle.py.
# ---------------------------------------------------------------------

def report_from_session(session: dict[str, Any], agent_log: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Assemble the agentic-mode report from a persisted session document.

    Remaps the pipeline's internal file_id back to the ground-truth
    file_name in both decisions and schema columns -- the same
    normalisation :func:`phi_corpus.verify.verify` requires of its own
    callers -- so :func:`build_report`'s (file, column) keys resolve
    without needing a file_name_map parameter of its own.

    Returns ``None`` when the session carries no ``corpus_ground_truth``
    (not a corpus run), so callers can turn that into whatever error shape
    fits their surface (HTTP 400, a one-line bundle note, ...).
    """
    from .verify import verify as _verify

    gt = session.get("corpus_ground_truth")
    if not gt:
        return None

    files = session.get("files") or []
    name_map = {f.get("original_name", ""): f.get("file_id", "") for f in files}
    id_to_name = {v: k for k, v in name_map.items()}

    decisions = session.get("agent_decisions") or []
    verify_report = _verify(
        gt, decisions, file_name_map=name_map,
        export_paths=session.get("export_paths") or {},
        guard_report=session.get("guard_report") or {},
    )

    decisions_for_report = []
    for d in decisions:
        nd = dict(d)
        nd["file_id"] = id_to_name.get(d.get("file_id"), d.get("file_id"))
        decisions_for_report.append(nd)

    schema_cols = ((session.get("agent_specialists") or {}).get("schema") or {}).get("columns")
    schema_for_report = None
    if schema_cols is not None:
        schema_for_report = []
        for c in schema_cols:
            nc = dict(c)
            nc["_file_id"] = id_to_name.get(c.get("_file_id"), c.get("_file_id"))
            schema_for_report.append(nc)

    return build_report(
        ground_truth=gt,
        decisions=decisions_for_report,
        verify_report=verify_report,
        mode="agentic",
        praxis_methods=session.get("agent_praxis"),
        schema_columns=schema_for_report,
        sentinel_overrides=session.get("sentinel_overrides"),
        keep_demotions=session.get("keep_demotions"),
        guard_report=session.get("guard_report"),
        prompt_scrub_counts=session.get("prompt_scrub_counts"),
        agent_log=agent_log,
        phase_timings=session.get("phase_timings"),
        run_elapsed_s=session.get("run_elapsed_s"),
        model_output_rejections=session.get("model_output_rejections"),
    )


# ---------------------------------------------------------------------
# 3.6 Offline CLI -- no Mongo, no LLM, no network.
# ---------------------------------------------------------------------

def _cli(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="phi_corpus.benchmark")
    p.add_argument("--scenario", required=True)
    p.add_argument("--rows", type=int, default=12)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--jurisdiction", default="us")
    p.add_argument("--edge-cases", default="")
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)

    from .planters import plant
    from .replay import replay as _replay
    from . import verify as _verify

    edge_case_tags = [t for t in args.edge_cases.split(",") if t]
    artifact = plant(
        scenario_id=args.scenario, jurisdiction=args.jurisdiction,
        edge_case_tags=edge_case_tags, row_count=args.rows, seed=args.seed,
    )

    import tempfile
    with tempfile.TemporaryDirectory(prefix="phi-bench-") as workdir:
        result = _replay(artifact, Path(workdir), unmatched="human_review")
        verify_report = _verify.verify(
            artifact.ground_truth, result.decisions,
            file_name_map=result.file_name_map,
            guard_report=result.guard_report,
            export_paths=result.export_paths,
        )
        report = build_report(
            ground_truth=artifact.ground_truth,
            decisions=result.decisions,
            verify_report=verify_report,
            mode="deterministic_replay",
            guard_report=result.guard_report,
            model_output_rejections=result.model_output_rejections,
            run_elapsed_s=result.elapsed_s,
        )
        paths = write(report, Path(args.out))

    print(json.dumps(paths, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
