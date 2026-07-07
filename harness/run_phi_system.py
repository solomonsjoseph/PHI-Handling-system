"""Benchmark/evidence driver for the standalone phi_engine pipeline.

Generates a synthetic clinical study (the CORPUS side -- ``generators/``),
writes it into a throwaway SOURCE directory, then drives the STANDALONE
system exclusively through its public entry points: ``intake_add`` +
``run_pipeline``. The generator import lives ONLY in this harness module --
``phi_engine/`` itself never imports ``generators`` (verified by
``grep -rn "generators" phi_engine/`` returning nothing; the standalone-spec
acceptance check for the corpus/system split).

CLI:
    python -m harness.run_phi_system --study PaperDemoIN --jurisdiction in \\
        --seed 42 --n-subjects 60 --out-dir benchmarks/results/phi-system-in

Steps:
    a. Generate the tabular study (Phase-2 generator), write it as JSONL into
       a throwaway source directory (never touched again after intake copies
       it via symlink), plus a pre-scrub copy + gold ledger under --out-dir.
    b. ``intake_add(source_dir, study)`` -- symlink-only ingestion.
    c. ``run_pipeline(study, jurisdiction)`` -- organize -> classify (pinned
       jurisdiction rules) -> scrub -> residual guard -> publish. This is the
       SAME entry point ``python -m phi_engine run`` uses; the benchmark
       exercises no private/parallel code path.
    d. Measurement against the gold ledger, reading published output from
       ``output/{STUDY}/llm_source/datasets/`` -> ``phi_system_result.json``.
    e. Non-zero exit iff the pipeline itself hard-failed (scrub raised, the
       residual guard failed, or a config/input error) -- a partial run
       (held forms) is not fatal, mirrored from the pipeline's own exit-8
       contract; measurement discrepancies are recorded, not fatal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _digit_normalize(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", value).lower()


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def _align_pre_post(
    pre_rows: list[dict], post_rows: list[dict], quarantined_indices: set[int]
) -> list[tuple[dict, dict, str]]:
    """Align pre-scrub rows to post-scrub rows, filtering out quarantined rows.

    Positional, not identity-based: the subject-ID column is PSEUDONYMIZED by
    the time a row reaches ``post_rows`` (``SUBJID`` -> ``RID_SUBJ_...``), so
    pre/post rows can never be joined by matching that column's raw vs.
    scrubbed value. Instead: drop the rows the gold ledger's
    ``quarantine_row``-tagged entries (by ``row_index``) say never survive,
    then pair what remains positionally -- ``phi_scrub._scrub_file`` appends
    to its ``kept`` list in file-read order, so the two sequences stay in
    lockstep once the known-quarantined indices are removed from the pre
    side. The RAW (pre-scrub) SUBJID is carried through as the row's
    identifier for cross-form linkage checks -- it is never used to look
    anything up post-scrub.
    """
    kept_pre = [row for i, row in enumerate(pre_rows) if i not in quarantined_indices]
    return [(pre, post, pre.get("SUBJID", "")) for pre, post in zip(kept_pre, post_rows)]


def _row_index_map(
    pre_rows: list[dict], post_rows: list[dict], quarantined_indices: set[int]
) -> dict[int, dict | None]:
    """Map each ORIGINAL pre-scrub row index -> its post-scrub row, or ``None``
    when that row was quarantined (never published) -- see :func:`_align_pre_post`
    for why this is positional rather than identity-based."""
    mapping: dict[int, dict | None] = {}
    post_ptr = 0
    for idx in range(len(pre_rows)):
        if idx in quarantined_indices:
            mapping[idx] = None
            continue
        mapping[idx] = post_rows[post_ptr] if post_ptr < len(post_rows) else None
        post_ptr += 1
    return mapping


_RID_RE = re.compile(r"^RID_[A-Z0-9]{1,16}_[a-p]{12}$")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", required=True, help="STUDY_NAME (plain folder name)")
    parser.add_argument("--jurisdiction", required=True, choices=["in", "us"])
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--n-subjects", type=int, default=60)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args(argv)

    # STUDY_NAME must be set BEFORE importing phi_engine.config.config -- it
    # resolves STUDY_NAME (and every path cascading from it) at import time.
    os.environ["STUDY_NAME"] = args.study

    import phi_engine.config.config as config
    from generators.study_tabular import IndiaStudyTabularGenerator, USStudyTabularGenerator
    from phi_engine.pipeline.intake import intake_add
    from phi_engine.pipeline.run import run_pipeline

    jurisdiction_label = "INDIA" if args.jurisdiction == "in" else "USA"
    gen_cls = IndiaStudyTabularGenerator if args.jurisdiction == "in" else USStudyTabularGenerator

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    result: dict[str, Any] = {
        "study": args.study,
        "jurisdiction": jurisdiction_label,
        "seed": args.seed,
        "n_subjects": args.n_subjects,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    # -- a. generate + write to a throwaway SOURCE directory -----------------
    gen = gen_cls(args.seed)
    forms = gen.generate_study(args.n_subjects)
    ledger = gen.gold_ledger()

    source_dir = Path(tempfile.mkdtemp(prefix=f"phi_system_source_{args.study}_"))
    pre_scrub_dir = out_dir / "pre_scrub"
    for filename, rows in forms.items():
        _write_jsonl(source_dir / filename, rows)
        _write_jsonl(pre_scrub_dir / filename, rows)
    _write_jsonl(out_dir / "gold_ledger.jsonl", ledger)

    # -- rulebook provenance ---------------------------------------------------
    rulebook_name = f"rulebook_v1_{jurisdiction_label}.json"
    rulebook_path = Path(config.CONFIG_DEFAULTS_DIR) / "phi_rulebook" / rulebook_name
    result["rulebook_provenance"] = {
        "path": str(rulebook_path),
        "exists": rulebook_path.is_file(),
        "sha256": hashlib.sha256(rulebook_path.read_bytes()).hexdigest()
        if rulebook_path.is_file()
        else None,
    }

    # -- b+c. drive the standalone system exclusively through its public -----
    # -- entry points: intake_add + run_pipeline (the exact path
    # -- `python -m phi_engine intake`/`run` uses) ----------------------------
    try:
        intake_manifest = intake_add(source_dir, args.study)
        pipeline_result = run_pipeline(args.study, args.jurisdiction)
    finally:
        shutil.rmtree(source_dir, ignore_errors=True)

    result["intake"] = {
        "linked": len(intake_manifest["entries"]),
        "duplicates": len(intake_manifest["duplicates"]),
        "errors": len(intake_manifest["errors"]),
    }
    result["pipeline"] = pipeline_result.to_json()
    result["run_id"] = pipeline_result.run_id
    result["scrub_raised"] = pipeline_result.scrub_raised
    result["scrub_config_hash"] = pipeline_result.scrub_config_hash

    hard_failure = pipeline_result.scrub_raised is not None or pipeline_result.exit_code in (2, 5)
    if hard_failure:
        out_path = out_dir / "phi_system_result.json"
        out_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        print(f"phi_system_result.json written to {out_path}")
        print(f"FATAL: pipeline exit_code={pipeline_result.exit_code}: {pipeline_result.message}", file=sys.stderr)
        return 1

    runs_dir = Path(config.STUDY_OUTPUT_DIR) / "runs"
    scrub_outcome_path = runs_dir / pipeline_result.run_id / "scrub_outcome.json"
    result["scrub_outcome"] = (
        json.loads(scrub_outcome_path.read_text(encoding="utf-8"))
        if scrub_outcome_path.is_file()
        else None
    )
    audit_path = Path(config.AUDIT_SCRUB_REPORT_PATH)
    audit_report = (
        json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.is_file() else None
    )

    result["residual"] = {
        "ok": pipeline_result.guard_ok,
        "guard_failed": pipeline_result.guard_failed,
    }

    # -- ai_layer: read the classification actually applied this run ---------
    approval_path = runs_dir / pipeline_result.run_id / "phi_handling_approval.json"
    approval_payload = (
        json.loads(approval_path.read_text(encoding="utf-8")) if approval_path.is_file() else {}
    )
    classifications_by_header: dict[str, str] = {}
    for form in approval_payload.get("forms", []):
        for cls in form.get("classifications", []):
            classifications_by_header.setdefault(cls["header"], cls["action"])

    headers_total = len(classifications_by_header)
    headers_review_queue = pipeline_result.review_queue_size
    human_review_rate = headers_review_queue / len(forms) if forms else 0.0

    legend_by_header: dict[str, str] = {}
    for entry in ledger:
        if "row_index" not in entry:
            legend_by_header.setdefault(entry["column"], entry["expected_action"])

    agreement_hits = 0
    agreement_total = 0
    for header, action in classifications_by_header.items():
        gold_action = legend_by_header.get(header)
        if gold_action is None:
            continue
        agreement_total += 1
        gold_is_phi = gold_action != "keep"
        classifier_is_phi = action != "keep"
        if gold_is_phi == classifier_is_phi:
            agreement_hits += 1
    header_classification_agreement = (
        agreement_hits / agreement_total if agreement_total else None
    )

    result["ai_layer"] = {
        "headers_total": headers_total,
        "headers_review_queue": headers_review_queue,
        "human_review_rate": human_review_rate,
        "classifier_path": f"phi_review:pinned:{jurisdiction_label}",
        "header_classification_agreement": header_classification_agreement,
        "header_classification_agreement_definition": (
            "binary PHI-vs-non-PHI agreement between phi_review's classified "
            "action != 'keep' and the gold column legend's expected_action != "
            "'keep' -- NOT transform-type agreement."
        ),
    }

    # -- d. measurement against the gold ledger -------------------------------
    staging_dir = Path(config.STUDY_LLM_SOURCE_DIR) / "datasets"
    post_forms = {name: _read_jsonl(staging_dir / name) for name in forms}
    pre_forms = {name: _read_jsonl(pre_scrub_dir / name) for name in forms}

    quarantined_indices_by_form: dict[str, set[int]] = {name: set() for name in forms}
    for entry in ledger:
        if entry.get("expected_action") == "quarantine_row" and "row_index" in entry:
            quarantined_indices_by_form.setdefault(entry["form"], set()).add(entry["row_index"])

    row_index_maps = {
        name: _row_index_map(pre_forms[name], post_forms[name], quarantined_indices_by_form[name])
        for name in forms
    }

    redaction_buckets: dict[str, dict[str, Any]] = {}
    leaks: list[dict[str, Any]] = []
    total_cells = 0
    total_redacted = 0
    for entry in ledger:
        if "row_index" not in entry:
            continue
        total_cells += 1
        action = entry["expected_action"]
        bucket = redaction_buckets.setdefault(action, {"total": 0, "redacted": 0})
        bucket["total"] += 1

        post_row = row_index_maps[entry["form"]].get(entry["row_index"])
        if post_row is None:
            # Whole row quarantined (or its form held) -- trivially redacted.
            redacted = True
        else:
            post_value = post_row.get(entry["column"], "")
            orig = entry["original_value"]
            redacted = post_value != orig and _digit_normalize(post_value) != _digit_normalize(orig)

        if redacted:
            bucket["redacted"] += 1
            total_redacted += 1
        else:
            leaks.append({"form": entry["form"], "column": entry["column"]})

    leak_counts: dict[str, int] = {}
    for leak in leaks:
        key = f"{leak['form']}::{leak['column']}"
        leak_counts[key] = leak_counts.get(key, 0) + 1
    leaks_aggregated = [
        {"form": key.split("::")[0], "column": key.split("::")[1], "count": count}
        for key, count in sorted(leak_counts.items())
    ]

    result["redaction"] = {
        "total_gold_phi_cells": total_cells,
        "redacted": total_redacted,
        "redaction_recall": total_redacted / total_cells if total_cells else None,
        "by_expected_action": redaction_buckets,
        "leaks": leaks_aggregated,
        "method": (
            "row-scoped: each ledgered cell's ORIGINAL value is compared only "
            "against its own row's post-scrub value at the same column (exact "
            "match AND digit-normalized match), not searched across the whole "
            "output file -- a whole-file search over-counts false-positive "
            "'leaks' when a short/low-entropy value (a 2-digit age, a jittered "
            "date) coincidentally collides with an unrelated subject's value."
        ),
    }

    # -- pseudonyms + dates: pre/post row alignment per form -------------------
    aligned = {
        name: _align_pre_post(pre_forms[name], post_forms[name], quarantined_indices_by_form[name])
        for name in forms
    }

    id_columns_by_form = {
        "1A_Screening.jsonl": ["SUBJID", "IC_SCRNNUM"],
        "2_Demographics.jsonl": ["SUBJID"],
        "3_Labs.jsonl": ["SUBJID"],
    }
    date_columns_by_form = {
        "1A_Screening.jsonl": ["VISITDAT"],
        "3_Labs.jsonl": ["COLLDAT", "TBTXDT"],
    }

    pseudo_checked = 0
    pseudo_pass = 0
    rid_by_subject: dict[str, dict[str, str]] = {}
    for form, id_cols in id_columns_by_form.items():
        if form not in aligned:
            continue
        for pre_row, post_row, raw_subjid in aligned[form]:
            for col in id_cols:
                post_val = post_row.get(col, "")
                if not post_val:
                    continue
                pseudo_checked += 1
                if _RID_RE.match(post_val):
                    pseudo_pass += 1
                rid_by_subject.setdefault(raw_subjid, {})[f"{form}:{col}"] = post_val

    linkage_ok = 0
    linkage_total = 0
    for raw_subjid, tokens in rid_by_subject.items():
        subj_tokens = {k: v for k, v in tokens.items() if k.endswith(":SUBJID")}
        if len(subj_tokens) < 2:
            continue
        linkage_total += 1
        if len(set(subj_tokens.values())) == 1:
            linkage_ok += 1

    result["pseudonyms"] = {
        "cells_checked": pseudo_checked,
        "regex_pass_count": pseudo_pass,
        "regex_pass_rate": pseudo_pass / pseudo_checked if pseudo_checked else None,
        "cross_form_linkage_subjects_checked": linkage_total,
        "cross_form_linkage_ok": linkage_ok,
    }

    max_jitter_days = 30
    date_checked = 0
    date_shift_ok = 0
    date_within_bound = 0
    offset_by_subject: dict[str, set[int]] = {}
    blanked_count = 0
    for form, date_cols in date_columns_by_form.items():
        if form not in aligned:
            continue
        for pre_row, post_row, raw_subjid in aligned[form]:
            for col in date_cols:
                pre_val = pre_row.get(col, "")
                post_val = post_row.get(col, "")
                if pre_val == "INVALID-DATE":
                    if post_val == "":
                        blanked_count += 1
                    continue
                if not pre_val or not post_val:
                    continue
                date_checked += 1
                try:
                    pre_dt = datetime.strptime(pre_val, "%Y-%m-%d")
                    post_dt = datetime.strptime(post_val, "%Y-%m-%d")
                except ValueError:
                    continue
                offset = (post_dt - pre_dt).days
                if offset != 0:
                    date_shift_ok += 1
                if abs(offset) <= max_jitter_days:
                    date_within_bound += 1
                offset_by_subject.setdefault(raw_subjid, set()).add(offset)

    per_subject_constant = sum(1 for offs in offset_by_subject.values() if len(offs) == 1)
    per_subject_total = len(offset_by_subject)

    # interval preservation: (COLLDAT - VISITDAT) unchanged pre/post, per subject
    # that has both cells populated (excludes the 2 unparseable-VISITDAT subjects
    # and the 2 orphan-Labs subjects, where one side of the interval is missing).
    interval_checked = 0
    interval_preserved = 0
    pre_visit_by_subj = {
        pre["SUBJID"]: pre.get("VISITDAT", "")
        for pre, _post, _s in aligned.get("1A_Screening.jsonl", [])
        if pre.get("VISITDAT", "") != "INVALID-DATE"
    }
    post_visit_by_subj = {
        s: post.get("VISITDAT", "")
        for pre, post, s in aligned.get("1A_Screening.jsonl", [])
        if s in pre_visit_by_subj
    }
    pre_coll_by_subj = {s: pre.get("COLLDAT", "") for pre, _post, s in aligned.get("3_Labs.jsonl", [])}
    post_coll_by_subj = {s: post.get("COLLDAT", "") for _pre, post, s in aligned.get("3_Labs.jsonl", [])}
    for subj, pre_visit in pre_visit_by_subj.items():
        if subj not in pre_coll_by_subj:
            continue
        pre_coll = pre_coll_by_subj[subj]
        post_visit = post_visit_by_subj.get(subj, "")
        post_coll = post_coll_by_subj.get(subj, "")
        if not (pre_visit and pre_coll and post_visit and post_coll):
            continue
        try:
            pre_delta = (
                datetime.strptime(pre_coll, "%Y-%m-%d") - datetime.strptime(pre_visit, "%Y-%m-%d")
            ).days
            post_delta = (
                datetime.strptime(post_coll, "%Y-%m-%d")
                - datetime.strptime(post_visit, "%Y-%m-%d")
            ).days
        except ValueError:
            continue
        interval_checked += 1
        if pre_delta == post_delta:
            interval_preserved += 1

    result["dates"] = {
        "cells_checked": date_checked,
        "shifted": date_shift_ok,
        "within_jitter_bound": date_within_bound,
        "max_jitter_days": max_jitter_days,
        "per_subject_offset_constant": per_subject_constant,
        "per_subject_offset_total": per_subject_total,
        "per_subject_offset_all_constant": per_subject_constant == per_subject_total,
        "interval_preservation_checked": interval_checked,
        "interval_preservation_preserved": interval_preserved,
        "planted_unparseable_blanked_observed": blanked_count,
    }

    # -- fail_closed -------------------------------------------------------------
    quarantine_dir = Path(config.STUDY_STAGING_DIR) / "quarantine"
    quarantined_rows = _read_jsonl(quarantine_dir / "3_Labs.jsonl") if quarantine_dir.is_dir() else []
    demographics_post = post_forms.get("2_Demographics.jsonl", [])
    cap_hits = sum(1 for row in demographics_post if row.get("AGE") == "90+")

    result["fail_closed"] = {
        "planted_orphan_rows": 2,
        "quarantined_rows_observed": len(quarantined_rows),
        "quarantine_matches_planted": len(quarantined_rows) == 2,
        "planted_unparseable_dates": 2,
        "blanked_dates_observed": blanked_count,
        "blank_matches_planted": blanked_count == 2,
        "planted_age_cap_subjects": 1,
        "age_cap_90plus_observed": cap_hits,
        "age_cap_matches_planted": cap_hits == 1,
        "audit_report_present": audit_report is not None,
    }

    out_path = out_dir / "phi_system_result.json"
    out_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")

    print(f"phi_system_result.json written to {out_path}")
    print(f"redaction_recall={result['redaction']['redaction_recall']}")
    print(f"residual ok={result['residual']['ok']}")
    print(f"human_review_rate={result['ai_layer']['human_review_rate']}")
    print(f"pipeline exit_code={pipeline_result.exit_code} ({pipeline_result.message})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
