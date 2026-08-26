"""Render a ``CampaignResult`` as JSON and markdown.

The markdown is the artifact a reader checks: it must let someone else
reproduce the exact numbers, so ``corpus_version`` and every seed are
printed alongside every table.
"""
from __future__ import annotations

import json
from pathlib import Path

from .campaign import CampaignResult
from .tiers import REQUIRED_VIOLATIONS


def to_json(result: CampaignResult) -> str:
    return json.dumps({
        "corpus_version": result.corpus_version,
        "mode": result.mode,
        "started_at": result.started_at,
        "elapsed_s": result.elapsed_s,
        "jobs": result.jobs,
        "provider_info": result.provider_info,
        "entries": result.entries,
        "rollup": result.rollup,
    }, indent=2)


def _reproduction_command(result: CampaignResult) -> str:
    mode_flag = "--offline" if result.mode == "deterministic_replay" else "--online"
    return (f"python -m phi_corpus.generate --campaign --tier all {mode_flag} "
            f"--jobs {result.jobs} --out-dir <dir>   # corpus_version={result.corpus_version}")


def to_markdown(result: CampaignResult) -> str:
    lines: list[str] = []
    lines.append(f"# Corpus campaign report -- {result.mode}")
    lines.append("")
    provider = result.provider_info or {}
    web_search = "unavailable" if (provider and not provider.get("web_search_available")) else (
        "available" if provider.get("web_search_available") else "n/a (deterministic replay)"
    )
    lines.append(f"- corpus_version: `{result.corpus_version}`")
    lines.append(f"- mode: `{result.mode}`")
    lines.append(f"- jobs: {result.jobs}")
    lines.append(f"- started_at: {result.started_at}")
    lines.append(f"- elapsed_s: {result.elapsed_s}")
    if provider:
        lines.append(f"- active LLM provider: `{provider.get('provider', 'n/a')}` "
                      f"(model `{provider.get('model', 'n/a')}`)")
        lines.append(f"- web_search: {web_search}")
    lines.append(f"- reproduction: `{_reproduction_command(result)}`")
    lines.append("")

    # ---- regulation coverage table -----------------------------------
    lines.append("## Regulation coverage")
    lines.append("")
    lines.append("| key | description | planted | neutralised | leaked |")
    lines.append("| --- | --- | --- | --- | --- |")
    reg = result.rollup.get("regulation", {})
    planted = reg.get("planted", {})
    neutralised = reg.get("neutralised", {})
    leaked = reg.get("leaked", {})
    for key, desc in REQUIRED_VIOLATIONS.items():
        lines.append(f"| {key} | {desc} | {planted.get(key, 0)} | "
                      f"{neutralised.get(key, 0)} | {leaked.get(key, 0)} |")
    unplanted = reg.get("unplanted", [])
    lines.append("")
    lines.append(f"unplanted: {'none' if not unplanted else ', '.join(unplanted)}")
    lines.append("")

    # ---- per-tier section ----------------------------------------------
    lines.append("## Per-tier rollup")
    lines.append("")
    lines.append("| tier | scenarios | leak rate | transform conformance | utility rate | "
                  "precision | recall | f1 | deferral rate |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for tier in ("L0", "L1", "L2", "L3"):
        b = result.rollup.get("per_tier", {}).get(tier)
        if not b:
            continue
        lines.append(f"| {tier} | {b['scenarios']} | {b['leak_rate']} | {b['transform_rate']} | "
                      f"{b['utility_rate']} | {b['avg_precision']} | {b['avg_recall']} | "
                      f"{b['avg_f1']} | {b['deferral_rate']} |")
    lines.append("")

    # ---- per-scenario table --------------------------------------------
    lines.append("## Per-scenario")
    lines.append("")
    lines.append("| tier | scenario | system | llm_dependent_columns | seed | elapsed_s | status |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for e in result.entries:
        if e.get("error"):
            lines.append(f"| {e.get('tier','')} | {e.get('scenario_id','')} | -- | -- | "
                          f"{e.get('seed','')} | {e.get('elapsed_s','')} | ERROR: {e['error']} |")
            continue
        report = e.get("report", {})
        llm_dep = len(report.get("llm_dependent_columns") or [])
        lines.append(f"| {e.get('tier','')} | {e.get('scenario_id','')} | "
                      f"{report.get('scenario_id','')} | {llm_dep} | {e.get('seed','')} | "
                      f"{e.get('elapsed_s','')} | ok |")
    lines.append("")

    # ---- defect table ----------------------------------------------------
    lines.append("## Defects")
    lines.append("")
    lines.append("| tier | scenario | seed | kind | column | sample | reason |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for e in result.entries:
        if e.get("error"):
            continue
        report = e.get("report", {})
        tier = e.get("tier", "")
        scenario = e.get("scenario_id", "")
        seed = e.get("seed", "")
        for h in (report.get("leak") or {}).get("hits", []):
            lines.append(f"| {tier} | {scenario} | {seed} | leak | {h.get('column','')} | "
                          f"{h.get('sample','')} | reached {h.get('export_file','')} |")
        for v in (report.get("transform") or {}).get("violations", []):
            lines.append(f"| {tier} | {scenario} | {seed} | transform | {v.get('column','')} | "
                          f"{v.get('actual_sample','')} | {v.get('reason','')} |")
        for u in (report.get("utility") or {}).get("losses", []):
            lines.append(f"| {tier} | {scenario} | {seed} | utility | {u.get('column','')} | "
                          f"{u.get('actual_sample','')} | {u.get('reason','')} |")
    lines.append("")

    return "\n".join(lines)


def write(result: CampaignResult, out_dir: Path) -> dict[str, str]:
    """Write only the report. Never writes the ground truth, which stays
    in memory. The existing ``generate.py --ground-truth`` flag is a
    separate, deliberate single-scenario operator affordance and keeps
    its current behaviour."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "campaign_report.json"
    md_path = out_dir / "campaign_report.md"
    json_path.write_text(to_json(result), encoding="utf-8")
    md_path.write_text(to_markdown(result), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path)}
