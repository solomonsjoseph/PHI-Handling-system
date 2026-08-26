"""Parallel campaign runner -- plants, runs, and grades the whole ladder
in one reproducible pass, either offline (deterministic replay, no key, no
Mongo) or online (the real 12-agent pipeline over HTTP).

The generator itself is microseconds; the cost is entirely the pipeline
(190s wall clock per run warm, 219s cold, measured). The
parallelism that matters is therefore at the campaign level: warm the
shared cache once instead of per run, fan out runs with a bounded
in-flight window, and pool the offline replay across processes because
each worker pays a one-off spaCy/Presidio import (~13s here).
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .tiers import REQUIRED_VIOLATIONS, LadderEntry, corpus_version


@dataclass
class CampaignResult:
    corpus_version: str
    mode: str                        # "deterministic_replay" | "full_pipeline"
    started_at: str
    elapsed_s: float
    jobs: int
    entries: list[dict[str, Any]] = field(default_factory=list)
    rollup: dict[str, Any] = field(default_factory=dict)
    provider_info: dict[str, Any] = field(default_factory=dict)


def _run_one(args: tuple[LadderEntry, str, str]) -> dict[str, Any]:
    """Module-level (picklable) worker body for the offline pool: plant,
    replay into a per-entry temp directory, and verify. A worker that
    raises records the error and lets the campaign continue -- one broken
    scenario must not lose the other thirteen results."""
    entry, workdir_str, unmatched = args
    t0 = time.time()
    try:
        from .planters import plant as _plant
        from .replay import replay as _replay
        from .verify import verify as _verify

        art = _plant(entry.scenario_id, edge_case_tags=list(entry.edge_case_tags),
                      row_count=entry.row_count, seed=entry.seed, tier=entry.tier)
        entry_workdir = Path(workdir_str) / f"{entry.tier}_{entry.scenario_id}_{entry.seed}"
        rr = _replay(art, entry_workdir, unmatched=unmatched)
        report = _verify(art.ground_truth, rr.decisions, file_name_map=rr.file_name_map,
                          guard_report=rr.guard_report, export_paths=rr.export_paths)
        report["mode"] = "deterministic_replay"
        report["llm_dependent_columns"] = rr.llm_dependent_columns
        return {
            "tier": entry.tier, "scenario_id": entry.scenario_id, "seed": entry.seed,
            "elapsed_s": round(time.time() - t0, 3),
            "report": report,
        }
    except Exception as e:
        return {
            "tier": entry.tier, "scenario_id": entry.scenario_id, "seed": entry.seed,
            "elapsed_s": round(time.time() - t0, 3),
            "error": f"{type(e).__name__}: {e}",
        }


def run_offline(entries: Sequence[LadderEntry], *, jobs: int = 4,
                workdir: Path | None = None, unmatched: str = "human_review") -> CampaignResult:
    jobs = max(1, jobs)
    owns_workdir = workdir is None
    work = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="phi_corpus_campaign_"))
    work.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.time()
    args = [(e, str(work), unmatched) for e in entries]

    try:
        if jobs == 1:
            results = [_run_one(a) for a in args]
        else:
            with ProcessPoolExecutor(max_workers=jobs) as ex:
                results = list(ex.map(_run_one, args))

        return CampaignResult(
            corpus_version=corpus_version(), mode="deterministic_replay",
            started_at=started_at, elapsed_s=round(time.time() - t0, 3), jobs=jobs,
            entries=results, rollup=_rollup(results),
        )
    finally:
        # Scratch replay trees are worthless once the report is built. Python 3.9
        # has no TemporaryDirectory(ignore_cleanup_errors=...), so tear down
        # explicitly and never block a campaign on a stale handle.
        if owns_workdir:
            shutil.rmtree(work, ignore_errors=True)


def _column_hipaa_categories(entry: LadderEntry) -> dict[str, str]:
    from .planters import plant as _plant
    art = _plant(entry.scenario_id, edge_case_tags=list(entry.edge_case_tags),
                 row_count=1, seed=entry.seed, tier=entry.tier)
    return {c["column"]: c["hipaa_category"] for c in art.ground_truth.get("columns", [])}


async def run_online(entries: Sequence[LadderEntry], *, base_url: str, token: str = "",
                      jobs: int = 3, warmup: bool = True, iteration_cap: int = 2,
                      poll_s: float = 3.0, timeout_s: float = 1200.0) -> CampaignResult:
    """Drive the existing HTTP surface. Bounded concurrency is enforced by
    holding a semaphore for the WHOLE submit-poll-verify cycle of each
    entry, not only for submission -- ``corpus_study_run`` returns as soon
    as it spawns a detached background pipeline task, so throttling
    submissions alone would not throttle pipelines.
    """
    import httpx

    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.time()
    headers = {"X-API-Token": token} if token else {}

    provider_info: dict[str, Any] = {}
    if warmup:
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                await client.post(f"{base_url}/api/settings/warmup", headers=headers)
        except Exception:
            pass  # best-effort; the campaign still runs, just cold on the first entries
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            cfg_resp = await client.get(f"{base_url}/api/settings/llm", headers=headers)
            cfg_resp.raise_for_status()
            cfg = cfg_resp.json()
            provider = cfg.get("provider", "")
            cat_resp = await client.get(f"{base_url}/api/settings/llm/catalog", headers=headers)
            cat_resp.raise_for_status()
            rows = cat_resp.json()
            rows = rows.get("models", rows) if isinstance(rows, dict) else rows
            web_search = False
            for row in rows or []:
                if row.get("provider_family") == provider or row.get("id", "").startswith(f"{provider}/"):
                    web_search = bool(row.get("web_search_available"))
                    break
            provider_info = {"provider": provider, "model": cfg.get("model", ""),
                              "web_search_available": web_search}
    except Exception:
        provider_info = {}

    sem = asyncio.Semaphore(max(1, jobs))

    async def run_entry(entry: LadderEntry) -> dict[str, Any]:
        async with sem:
            t_entry = time.time()
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.post(
                        f"{base_url}/api/corpus/study/run",
                        json={
                            "scenario_id": entry.scenario_id, "jurisdiction": "us",
                            "edge_case_tags": list(entry.edge_case_tags),
                            "row_count": entry.row_count, "seed": entry.seed,
                            "iteration_cap": iteration_cap,
                        },
                        headers=headers,
                    )
                    resp.raise_for_status()
                    session_id = resp.json()["session_id"]

                    status = await _poll_until(client, base_url, session_id, headers,
                                                {"awaiting_human_review", "complete", "failed"},
                                                poll_s, timeout_s)

                    resolution = ""
                    if status == "awaiting_human_review":
                        # Snapshot pre-resolution decisions BEFORE resolving, so the
                        # deferral metric is measured before any human input.
                        rresp = await client.get(f"{base_url}/api/sessions/{session_id}/results",
                                                  headers=headers)
                        rresp.raise_for_status()
                        pre = rresp.json()
                        gt_cats = _column_hipaa_categories(entry)
                        decisions = pre.get("decisions") if isinstance(pre, dict) else None
                        resolutions = []
                        for d in decisions or []:
                            col = d.get("column", "")
                            action = "keep" if gt_cats.get(col, "") == "NONE" else "drop"
                            resolutions.append({"file_id": d.get("file_id", ""),
                                                 "column": col, "action": action})
                        await client.post(
                            f"{base_url}/api/sessions/{session_id}/human-review",
                            json={"resolutions": resolutions,
                                  "reviewer": "corpus-campaign@lab",
                                  "comment": "conservative auto-resolution",
                                  "actual_knowledge_ack": True},
                            headers=headers,
                        )
                        resolution = "conservative_auto"
                        status = await _poll_until(client, base_url, session_id, headers,
                                                    {"complete", "failed"}, poll_s, timeout_s)

                    vresp = await client.get(f"{base_url}/api/corpus/study/verify/{session_id}",
                                              headers=headers)
                    vresp.raise_for_status()
                    report = vresp.json()
                    report["mode"] = "full_pipeline"
                    out = {"tier": entry.tier, "scenario_id": entry.scenario_id, "seed": entry.seed,
                           "elapsed_s": round(time.time() - t_entry, 3), "report": report}
                    if resolution:
                        out["human_review_resolution"] = resolution
                    return out
            except Exception as e:
                return {"tier": entry.tier, "scenario_id": entry.scenario_id, "seed": entry.seed,
                        "elapsed_s": round(time.time() - t_entry, 3),
                        "error": f"{type(e).__name__}: {e}"}

    results = list(await asyncio.gather(*(run_entry(e) for e in entries)))
    return CampaignResult(
        corpus_version=corpus_version(), mode="full_pipeline",
        started_at=started_at, elapsed_s=round(time.time() - t0, 3), jobs=jobs,
        entries=results, rollup=_rollup(results), provider_info=provider_info,
    )


async def _poll_until(client, base_url: str, session_id: str, headers: dict[str, str],
                       terminal: set[str], poll_s: float, timeout_s: float) -> str | None:
    deadline = time.time() + timeout_s
    status = None
    while time.time() < deadline:
        await asyncio.sleep(poll_s)
        sresp = await client.get(f"{base_url}/api/sessions/{session_id}", headers=headers)
        sresp.raise_for_status()
        status = sresp.json().get("status")
        if status in terminal:
            break
    return status


def _rollup(entries: list[dict[str, Any]]) -> dict[str, Any]:
    per_tier: dict[str, dict[str, Any]] = {}
    regulation_planted: dict[str, int] = {}
    regulation_leaked: dict[str, int] = {}
    regulation_neutralised: dict[str, int] = {}
    errors = 0

    for e in entries:
        if e.get("error"):
            errors += 1
            continue
        report = e.get("report") or {}
        tier = e.get("tier", "")
        bucket = per_tier.setdefault(tier, {
            "scenarios": 0, "leak_hits": 0, "phi_plants": 0,
            "transform_nonconformant": 0, "transform_total": 0,
            "utility_destroyed": 0, "utility_total": 0,
            "precision_sum": 0.0, "recall_sum": 0.0, "f1_sum": 0.0,
            "deferral_count": 0, "deferral_denominator": 0,
        })
        bucket["scenarios"] += 1
        leak = report.get("leak") or {}
        transform = report.get("transform") or {}
        utility = report.get("utility") or {}
        correctness = report.get("correctness") or {}
        deferral = report.get("deferral") or {}
        bucket["leak_hits"] += leak.get("hit_count", 0)
        bucket["phi_plants"] += leak.get("phi_plants", 0)
        bucket["transform_nonconformant"] += transform.get("nonconformant", 0)
        bucket["transform_total"] += transform.get("conformant", 0) + transform.get("nonconformant", 0)
        bucket["utility_destroyed"] += utility.get("destroyed", 0)
        bucket["utility_total"] += utility.get("preserved", 0) + utility.get("destroyed", 0)
        bucket["precision_sum"] += correctness.get("overall_precision", 0.0)
        bucket["recall_sum"] += correctness.get("overall_recall", 0.0)
        bucket["f1_sum"] += correctness.get("overall_f1", 0.0)
        bucket["deferral_count"] += deferral.get("count", 0)
        bucket["deferral_denominator"] += (report.get("summary") or {}).get("planted_columns", 0)

        regulation = report.get("regulation") or {}
        for k, v in (regulation.get("planted") or {}).items():
            regulation_planted[k] = regulation_planted.get(k, 0) + v
        for k, v in (regulation.get("leaked") or {}).items():
            regulation_leaked[k] = regulation_leaked.get(k, 0) + v
        for k, v in (regulation.get("neutralised") or {}).items():
            regulation_neutralised[k] = regulation_neutralised.get(k, 0) + v

    per_tier_out: dict[str, Any] = {}
    for tier, b in per_tier.items():
        n = b["scenarios"] or 1
        per_tier_out[tier] = {
            "scenarios": b["scenarios"],
            "leak_rate": round(b["leak_hits"] / b["phi_plants"], 4) if b["phi_plants"] else 0.0,
            "transform_rate": (round((b["transform_total"] - b["transform_nonconformant"]) / b["transform_total"], 4)
                                if b["transform_total"] else 1.0),
            "utility_rate": (round((b["utility_total"] - b["utility_destroyed"]) / b["utility_total"], 4)
                              if b["utility_total"] else 1.0),
            "avg_precision": round(b["precision_sum"] / n, 4),
            "avg_recall": round(b["recall_sum"] / n, 4),
            "avg_f1": round(b["f1_sum"] / n, 4),
            "deferral_rate": (round(b["deferral_count"] / b["deferral_denominator"], 4)
                               if b["deferral_denominator"] else 0.0),
        }

    unplanted = sorted(k for k in REQUIRED_VIOLATIONS if regulation_planted.get(k, 0) == 0)

    return {
        "errors": errors,
        "per_tier": per_tier_out,
        "regulation": {
            "planted": regulation_planted,
            "leaked": regulation_leaked,
            "neutralised": regulation_neutralised,
            "unplanted": unplanted,
        },
    }
