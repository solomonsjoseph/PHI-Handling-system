#!/usr/bin/env python3
"""A1 PHI decider↔cleaner reconciliation derivation (value-free dry-run gate).

Recomputes, per form, every header's **decided** action (``phi_review.
classify_headers`` — the regulation classifier / decider) versus the **applied**
action (``extract_to_llm_source._configured_scrub_action`` — the scrub config /
cleaner) and reports the contradictions plan A1 must drive to zero:

    contradiction := decided == "keep"  AND  applied != "keep"

A column the decider keeps but the cleaner transforms/drops gets BOTH a
``keep_decision`` and a transform ``event`` in the per-form PHI ledger — the 284
"keep + transform" contradictions A1 reconciles (notes Note 28).

This is the fast inner loop for the A1 edit: it reuses the exact two functions
the live pipeline uses, reads only row-1 column NAMES (GR-1 — never a row value),
and emits header names + counts only. The authoritative gate remains a real
Indo-VAP scrub run (events ∩ keep_decisions == 0 in the ledgers); this tool lets
the coordinated ``phi_review`` + ``phi_scrub.yaml`` edit be iterated without one.

Usage:
    uv run --all-groups python -m scripts.utils.reconcile_phi_decisions \
        --study Indo-VAP [--compare-oracle docs/plans/a1_reconciliation_baseline.json] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import phi_engine.config.config as config

# Mirror of extract_to_llm_source._PROTECTION_RANK (assertion 12 lattice). The
# decided-vs-applied verifier fails ONLY the under-protection direction —
# applied protection < decided protection (scrub did LESS than the decider
# decided → potential leak). Adding phi_review patterns that decide a STRICTER
# action than the config applies would trip this; the harness flags it here so
# such a regression is caught in the dry-run, not at the live verifier.
_PROTECTION_RANK: dict[str, int] = {
    "keep": 0,
    "generalize": 1,
    "band": 1,
    "cap": 1,
    "suppress_small_cell": 1,
    "suppress": 1,
    "jitter_date": 2,
    "pseudonymize": 2,
    "drop": 3,
    "birthdate_drop": 3,
}


def _resolve_forms(study: str) -> dict[str, Path]:
    """Map each form stem → its canonical raw dataset path (row-1 read source).

    Prefers an exact ``<stem>.xlsx``/``.csv`` over dedup variants (``<stem>_1``).
    """
    raw_dir = Path(config.RAW_DATA_DIR) / study / "datasets"
    by_stem: dict[str, Path] = {}
    for path in sorted(raw_dir.glob("*")):
        if path.suffix.lower() not in {".xlsx", ".xls", ".csv"}:
            continue
        by_stem.setdefault(path.stem, path)
    return by_stem


def _oracle_forms(oracle: dict[str, Any] | None) -> list[str]:
    if not oracle:
        return []
    seen: list[str] = []
    for row in oracle.get("contradictions", []):
        form = str(row.get("form", ""))
        if form and form not in seen:
            seen.append(form)
    return seen


def derive(study: str, oracle: dict[str, Any] | None = None) -> dict[str, Any]:
    """Recompute decided vs applied for every header in every form.

    Returns a value-free report: per-form decided/applied maps + the global
    contradiction list + a distribution. When *oracle* is supplied, also diffs
    the contradiction set against the baseline (missing / extra columns).
    """
    from phi_engine.security.phi_review import classify_headers, load_study_privacy_config
    from phi_engine.security.phi_rulebook import resolve_rulebook
    from phi_engine.security.phi_scrub import load_scrub_config
    from scripts.skills.extract_to_llm_source import _configured_scrub_action
    from scripts.source_truth.study_intake import read_headers_only

    raw_dir = Path(config.RAW_DATA_DIR) / study
    privacy = load_study_privacy_config(raw_dir)
    # Pinned (offline) bundle — deterministic; the edit target is _PINNED_RULE_SPECS.
    bundle = resolve_rulebook(privacy, allow_network=False).bundle
    cfg = load_scrub_config(study=study)

    by_stem = _resolve_forms(study)
    # Restrict to the forms the oracle covers when given (the published set), else
    # every raw dataset stem.
    wanted = _oracle_forms(oracle) or sorted(by_stem)

    contradictions: list[dict[str, str]] = []
    under_protections: list[dict[str, str]] = []
    decided_applied: dict[str, dict[str, dict[str, str]]] = {}
    missing_raw: list[str] = []

    for form in wanted:
        path = by_stem.get(form)
        if path is None:
            missing_raw.append(form)
            continue
        headers = read_headers_only(path)
        classified = classify_headers(tuple(headers), privacy, bundle)
        form_map: dict[str, dict[str, str]] = {}
        for header in headers:
            decided = str(classified[header].action)
            applied = _configured_scrub_action(cfg, header)
            form_map[header] = {"decided": decided, "applied": applied}
            # Contradiction: decider keeps, cleaner transforms → keep_decision + event.
            if decided == "keep" and applied != "keep":
                contradictions.append(
                    {"form": form, "column": header, "decided": decided, "applied": applied}
                )
            # Assertion-12 under-protection: cleaner protects LESS than the decider
            # decided (e.g. a phi_review pattern decides DROP but the config keeps).
            elif _PROTECTION_RANK.get(applied, 0) < _PROTECTION_RANK.get(decided, 0):
                under_protections.append(
                    {"form": form, "column": header, "decided": decided, "applied": applied}
                )
        decided_applied[form] = form_map

    dist: dict[str, int] = {}
    for row in contradictions:
        dist[row["applied"]] = dist.get(row["applied"], 0) + 1

    report: dict[str, Any] = {
        "study": study,
        "forms_evaluated": len(decided_applied),
        "forms_missing_raw": missing_raw,
        "contradictions_total": len(contradictions),
        "under_protection_total": len(under_protections),
        "under_protections": under_protections,
        "applied_distribution": dict(sorted(dist.items())),
        "contradictions": contradictions,
        "decided_applied": decided_applied,
    }

    if oracle is not None:
        oracle_keys = {(r["form"], r["column"]) for r in oracle.get("contradictions", [])}
        derived_keys = {(r["form"], r["column"]) for r in contradictions}
        report["oracle_diff"] = {
            "oracle_total": len(oracle_keys),
            "derived_total": len(derived_keys),
            "in_oracle_not_derived": sorted(f"{f}:{c}" for f, c in (oracle_keys - derived_keys)),
            "in_derived_not_oracle": sorted(f"{f}:{c}" for f, c in (derived_keys - oracle_keys)),
        }
    return report


def _norm(h: str) -> str:
    import re

    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(h).strip())
    s = re.sub(r"[^A-Za-z0-9]+", "_", s)
    return s.strip("_").lower()


def check_ledger_invariant(study: str) -> dict[str, Any]:
    """AUTHORITATIVE post-publish gate: no published column may carry BOTH a
    ``keep_decision`` and a transform ``event`` in its PHI ledger.

    Parses every ``output/{study}/audit/datasets/*/phi_handling_ledger.as_written.json``
    and intersects the (normalized) variable_ids of its ``events`` with its
    ``keep_decisions``. Value-free — variable NAMES + counts only. Returns a
    report with per-form contradiction lists; ``contradictions_total == 0`` is
    the A1 reconciliation invariant (Note 28).
    """
    audit_root = Path(config.OUTPUT_DIR) / study / "audit" / "datasets"
    forms: dict[str, list[str]] = {}
    ledgers_found = 0
    for ledger in sorted(audit_root.glob("*/phi_handling_ledger.as_written.json")):
        ledgers_found += 1
        form = ledger.parent.name
        try:
            data = json.loads(ledger.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ev = {
            _norm(e.get("variable_id", "")) for e in data.get("events", []) if isinstance(e, dict)
        }
        kd = {
            _norm(k.get("variable_id", ""))
            for k in data.get("keep_decisions", [])
            if isinstance(k, dict)
        }
        both = sorted(c for c in (ev & kd) if c)
        if both:
            forms[form] = both
    total = sum(len(v) for v in forms.values())
    return {
        "study": study,
        "ledgers_found": ledgers_found,
        "contradictions_total": total,
        "contradiction_forms": forms,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", default=None, help="Study name (default: auto-detect).")
    parser.add_argument(
        "--compare-oracle",
        type=Path,
        default=None,
        help="Path to a1_reconciliation_baseline.json to diff against.",
    )
    parser.add_argument("--json", action="store_true", help="Emit the full report as JSON.")
    parser.add_argument(
        "--check-ledgers",
        action="store_true",
        help="AUTHORITATIVE post-publish gate: parse the real PHI ledgers and report "
        "any column carrying both a keep_decision and a transform event (events ∩ "
        "keep_decisions). Requires a published output tree for the study.",
    )
    parser.add_argument(
        "--dump-map",
        type=Path,
        default=None,
        help="Write the full per-form decided/applied map to this JSON path (no-new-drops baseline).",
    )
    parser.add_argument(
        "--baseline-map",
        type=Path,
        default=None,
        help="A prior --dump-map snapshot to diff against — reports columns whose applied action "
        "changed, flagging any column that became a NEW drop (regression).",
    )
    args = parser.parse_args(argv)

    study = args.study or config.detect_study_name()

    if args.check_ledgers:
        led = check_ledger_invariant(study)
        if args.json:
            print(json.dumps(led, indent=2, sort_keys=True))
        else:
            print(f"study={led['study']} ledgers_found={led['ledgers_found']}")
            print(f"LEDGER INVARIANT keep+transform contradictions: {led['contradictions_total']}")
            for form, cols in sorted(led["contradiction_forms"].items()):
                print(f"  {form}: {', '.join(cols)}")
        return 1 if led["contradictions_total"] else 0

    oracle = None
    if args.compare_oracle is not None:
        oracle = json.loads(args.compare_oracle.read_text(encoding="utf-8"))

    report = derive(study, oracle)

    if args.dump_map is not None:
        args.dump_map.write_text(
            json.dumps(report["decided_applied"], indent=2, sort_keys=True), encoding="utf-8"
        )
        print(f"wrote decided/applied map → {args.dump_map}")

    if args.baseline_map is not None:
        base = json.loads(args.baseline_map.read_text(encoding="utf-8"))
        now = report["decided_applied"]
        changed: list[str] = []
        new_drops: list[str] = []
        for form, cols in now.items():
            for col, pair in cols.items():
                prev = base.get(form, {}).get(col)
                if prev is None:
                    continue
                if prev["applied"] != pair["applied"]:
                    changed.append(f"{form}:{col} applied {prev['applied']}→{pair['applied']}")
                    if pair["applied"] == "drop" and prev["applied"] != "drop":
                        new_drops.append(f"{form}:{col} ({prev['applied']}→drop)")
        report["map_diff"] = {"changed": changed, "new_drops": new_drops}
        print(f"map_diff: {len(changed)} applied-action change(s), {len(new_drops)} NEW drop(s)")
        if new_drops:
            print("  NEW DROPS (regression):", ", ".join(new_drops[:40]))

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"study={report['study']} forms={report['forms_evaluated']}")
        print(f"contradictions_total={report['contradictions_total']}")
        print(f"under_protection_total={report['under_protection_total']}")
        if report["under_protections"]:
            print(
                "  under_protection (assertion-12 risk):",
                ", ".join(
                    f"{r['form']}:{r['column']}({r['decided']}>{r['applied']})"
                    for r in report["under_protections"][:40]
                ),
            )
        print(f"applied_distribution={report['applied_distribution']}")
        if report["forms_missing_raw"]:
            print(f"forms_missing_raw={report['forms_missing_raw']}")
        if "oracle_diff" in report:
            od = report["oracle_diff"]
            print(
                f"oracle: total={od['oracle_total']} derived={od['derived_total']} "
                f"missing={len(od['in_oracle_not_derived'])} extra={len(od['in_derived_not_oracle'])}"
            )
            if od["in_oracle_not_derived"]:
                print("  in_oracle_not_derived:", ", ".join(od["in_oracle_not_derived"][:40]))
            if od["in_derived_not_oracle"]:
                print("  in_derived_not_oracle:", ", ".join(od["in_derived_not_oracle"][:40]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
