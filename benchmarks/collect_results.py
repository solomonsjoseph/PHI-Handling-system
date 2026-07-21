"""
Benchmark result collector -- walks benchmarks/results/*/*_benchmark_result.json
and emits one comparison table per jurisdiction (rows=tools, cols=strict P/R/F1,
legacy P/R/F1, macro-F1, gap rate, gap span count), plus a JSON twin.

Tools that were not actually run (package unavailable, env-gated, credential-
gated, or license-gated) get an explicit ``not_run`` row with a precise reason
-- never a faked/omitted result. The NOT_RUN registry below reflects THIS
session's evidence run (verified 2026-07-07); update it if the run environment
changes.

CLI:
    python -m benchmarks.collect_results --results-dir benchmarks/results \\
        --output benchmarks/results/comparison_table.md
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Known jurisdiction codes this repo's canonical corpus covers (harness/generate_corpus.py).
JURISDICTIONS = ("us",)

# Result-directory-name prefix -> (display name, result-file basename prefix).
# Directory names are "<prefix>-<jurisdiction>" (e.g. "presidio-stock-us").
RUN_TOOL_PREFIXES: Dict[str, str] = {
    "presidio-stock": "presidio_stock",
    "presidio-tuned": "presidio_tuned",
    "spacy": "spacy",
    "phi-engine": "phi_engine",
}

# Tools that were NOT run in this evidence pass, with the precise, evidence-
# backed reason (evidence plan Phase 4.2/4.3 contingencies; philter/pydeid
# reasons are from direct investigation this session -- see
# docs/JURISDICTION_EVIDENCE_REPORT_US.md "Benchmark matrix" for the full
# writeup). Applies identically across every jurisdiction (availability /
# credential gates are not jurisdiction-specific).
NOT_RUN_TOOLS: List[Dict[str, str]] = [
    {
        "tool": "philter",
        "reason": (
            "philter-ucsf 1.0.3 installs from PyPI but is a CLI-only tool "
            "(python3 main.py -i ... -o ... -f <config>.json), not a Python "
            "library with an importable detect_phi()-style API; its internal "
            "module also requires undeclared transitive dependencies (nltk "
            "corpora, chardet) beyond a pip install. Confirmed via source "
            "inspection (github.com/BCHSI/philter-ucsf) and PyPI docs "
            "2026-07-07; deeper CLI/subprocess integration is out of scope "
            "for this evidence pass."
        ),
    },
    {
        "tool": "pydeid",
        "reason": (
            "The PyPI package literally named 'pydeid' (0.0.1) is an empty "
            "placeholder with no importable submodules -- not the real "
            "academic tool. The real pyDeid (GEMINI-Medicine/pyDeid on "
            "GitHub, a refactor of the PhysioNet Perl de-identifier) has no "
            "PyPI release and its git-installable build did not produce an "
            "importable 'pydeid' module in this environment (confirmed by "
            "direct install attempt 2026-07-07). Excluded rather than "
            "reporting a misleading 0%-recall row for a tool that was never "
            "actually exercised."
        ),
    },
    {
        "tool": "clinideid",
        "reason": "requires CLINIDEID_JAR env var (license-gated Java tool); not set in this environment.",
    },
    {
        "tool": "physionet_deid",
        "reason": "requires PHYSIONET_DEID_DIR env var; not set in this environment.",
    },
    {
        "tool": "modified_deidentify",
        "reason": "requires a local model checkpoint path (license-gated); none supplied.",
    },
    {
        "tool": "aws_comprehend_medical",
        "reason": "requires AWS credentials; pending credentials per registry (planned).",
    },
    {
        "tool": "azure_health",
        "reason": "requires Azure credentials; pending credentials per registry (planned).",
    },
]


def _round(x: Optional[float]) -> Optional[float]:
    return round(x, 4) if isinstance(x, (int, float)) else x


def _parse_result_dir(dir_name: str) -> Optional[tuple[str, str]]:
    """Return (tool_prefix, jurisdiction) for a results/<dir_name>/ directory, or None."""
    for jur in JURISDICTIONS:
        suffix = f"-{jur}"
        if dir_name.endswith(suffix):
            prefix = dir_name[: -len(suffix)]
            if prefix in RUN_TOOL_PREFIXES:
                return prefix, jur
    return None


def collect(
    results_dir: Path,
    *,
    suite: str = "us",
    candidates_path: Path | None = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Return {bucket: [row, ...]} across every tool (run + not_run).

    suite="us" (default, unchanged behavior): walks
    results_dir/<prefix>-<jurisdiction>/ directories for the fixed
    RUN_TOOL_PREFIXES set; one bucket per entry in JURISDICTIONS.

    suite="privacy_gateway": discovers every *_benchmark_result.json file
    recursively under results_dir (privacy-gateway adapter output is not
    constrained to the "<prefix>-<jurisdiction>" directory convention),
    derives tool/version from each artifact's own "tool" field rather than
    a directory-name prefix, and -- when *candidates_path* is given -- adds
    a not_run row for every research/privacy_gateway/candidate_registry.jsonl
    entry whose benchmark_status is not_run/not_attempted/pending_poc and
    has no matching result artifact. Single bucket "privacy_gateway".
    """
    if suite == "us":
        return _collect_us(results_dir)
    if suite == "privacy_gateway":
        return _collect_privacy_gateway(results_dir, candidates_path)
    raise ValueError(f"unknown suite {suite!r}; expected 'us' or 'privacy_gateway'")


def _collect_us(results_dir: Path) -> Dict[str, List[Dict[str, Any]]]:
    """Return {jurisdiction: [row, ...]} across every tool (run + not_run)."""
    by_jurisdiction: Dict[str, List[Dict[str, Any]]] = {jur: [] for jur in JURISDICTIONS}

    for entry in sorted(results_dir.iterdir()):
        if not entry.is_dir():
            continue
        parsed = _parse_result_dir(entry.name)
        if parsed is None:
            continue
        prefix, jur = parsed
        file_prefix = RUN_TOOL_PREFIXES[prefix]
        result_path = entry / f"{file_prefix}_benchmark_result.json"
        if not result_path.is_file():
            by_jurisdiction[jur].append(
                {
                    "tool": prefix,
                    "status": "not_run",
                    "reason": f"expected result file missing: {result_path}",
                }
            )
            continue
        data = json.loads(result_path.read_text(encoding="utf-8"))
        by_jurisdiction[jur].append(
            {
                "tool": data.get("tool", prefix),
                "status": "ok",
                "scoring_profile": data.get("scoring_profile"),
                "total_records": data.get("total_records"),
                "total_gold_spans": data.get("total_gold_spans"),
                "legacy_precision": _round(data.get("aggregate_precision")),
                "legacy_recall": _round(data.get("aggregate_recall")),
                "legacy_f1": _round(data.get("aggregate_f1")),
                "strict_precision": _round(data.get("strict_all_span_precision")),
                "strict_recall": _round(data.get("strict_all_span_recall")),
                "strict_f1": _round(data.get("strict_all_span_f1")),
                "macro_f1": _round(data.get("macro_f1")),
                "gap_detection_rate": _round(data.get("gap_detection_rate")),
                "gap_span_count": data.get("gap_span_count"),
                "result_file": str(result_path),
                "note": (
                    "strict_all_span fields are structurally 0.0 -- this adapter "
                    "(benchmarks/spacy_adapter.py) does not compute the strict "
                    "scoring profile, not a measured zero-recall result"
                )
                if prefix == "spacy"
                else None,
            }
        )

    for jur in JURISDICTIONS:
        for nr in NOT_RUN_TOOLS:
            by_jurisdiction[jur].append(
                {"tool": nr["tool"], "status": "not_run", "reason": nr["reason"]}
            )
        by_jurisdiction[jur].sort(key=lambda r: r["tool"])

    return by_jurisdiction


def _collect_privacy_gateway(
    results_dir: Path, candidates_path: Path | None
) -> Dict[str, List[Dict[str, Any]]]:
    rows: List[Dict[str, Any]] = []
    seen_tools: set[str] = set()

    if results_dir.is_dir():
        for result_path in sorted(results_dir.rglob("*_benchmark_result.json")):
            try:
                data = json.loads(result_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                rows.append({
                    "tool": result_path.stem, "status": "not_run",
                    "reason": f"unreadable/corrupt result artifact: {result_path}",
                })
                continue
            tool = str(data.get("tool", result_path.stem))
            seen_tools.add(tool)
            if data.get("status") == "not_run":
                rows.append({
                    "tool": tool, "status": "not_run",
                    "reason": data.get("reason", "unspecified"),
                    "result_file": str(result_path),
                })
                continue
            rows.append({
                "tool": tool,
                "status": "ok",
                "scoring_profile": data.get("scoring_profile"),
                "total_records": data.get("total_records"),
                "total_gold_spans": data.get("total_gold_spans"),
                "legacy_precision": _round(data.get("aggregate_precision")),
                "legacy_recall": _round(data.get("aggregate_recall")),
                "legacy_f1": _round(data.get("aggregate_f1")),
                "strict_precision": _round(data.get("strict_all_span_precision")),
                "strict_recall": _round(data.get("strict_all_span_recall")),
                "strict_f1": _round(data.get("strict_all_span_f1")),
                "macro_f1": _round(data.get("macro_f1")),
                "gap_detection_rate": _round(data.get("gap_detection_rate")),
                "gap_span_count": data.get("gap_span_count"),
                "result_file": str(result_path),
                "note": None,
            })

    if candidates_path is not None and candidates_path.is_file():
        # Adapter tool names follow the "<prefix>-<version>" convention
        # (e.g. "presidio-2.2.x", "philter-not_run"); candidate_registry
        # rows use registry ids ("oss-0001") that never share that prefix,
        # so dedup against each candidate's own product/vendor name instead
        # of a raw candidate_id-vs-tool-name comparison.
        seen_bases = {t.split("-")[0].lower() for t in seen_tools}
        for line in candidates_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            candidate = json.loads(line)
            candidate_id = str(candidate.get("candidate_id", ""))
            if not candidate_id:
                continue
            product_base = str(candidate.get("product", "")).lower().replace(" ", "")
            vendor_base = str(candidate.get("vendor", "")).lower().replace(" ", "")
            candidate_bases = [b for b in (product_base, vendor_base) if b]
            if any(base and any(base in cb or cb in base for cb in candidate_bases) for base in seen_bases):
                continue
            status = candidate.get("benchmark_status")
            if status in {"not_run", "not_attempted", "pending_poc"}:
                rows.append({
                    "tool": candidate_id,
                    "status": "not_run",
                    "reason": candidate.get("not_run_reason") or f"benchmark_status={status}",
                })

    rows.sort(key=lambda r: r["tool"])
    return {"privacy_gateway": rows}


def _fmt(value: Any) -> str:
    if value is None:
        return "--"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def render_markdown(by_bucket: Dict[str, List[Dict[str, Any]]]) -> str:
    lines: List[str] = ["# Benchmark Comparison Table", ""]
    lines.append(
        "Generated by `benchmarks/collect_results.py`. Primary protocol profile "
        "(`benchmarks/protocol.py`): `strict_all_span`. Secondary: "
        "`legacy_overlap_coverable`. `not_run` rows carry a reason, never a "
        "faked/omitted result -- see `docs/SOTA_COMPARISON.md` for full context."
    )
    lines.append("")

    for bucket in by_bucket:
        rows = by_bucket[bucket]
        heading = f"Jurisdiction: {bucket}" if bucket in JURISDICTIONS else f"Suite: {bucket}"
        lines.append(f"## {heading}")
        lines.append("")
        lines.append(
            "| Tool | Strict P | Strict R | Strict F1 | Legacy P | Legacy R | "
            "Legacy F1 | Macro-F1 | Gap rate | Gap spans | Gold spans | Notes |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for row in rows:
            if row["status"] == "not_run":
                lines.append(
                    f"| {row['tool']} | not_run | | | | | | | | | | {row['reason']} |"
                )
                continue
            note = row.get("note") or ""
            lines.append(
                "| {tool} | {sp} | {sr} | {sf1} | {lp} | {lr} | {lf1} | {mf1} | "
                "{gap} | {gapn} | {gold} | {note} |".format(
                    tool=row["tool"],
                    sp=_fmt(row["strict_precision"]),
                    sr=_fmt(row["strict_recall"]),
                    sf1=_fmt(row["strict_f1"]),
                    lp=_fmt(row["legacy_precision"]),
                    lr=_fmt(row["legacy_recall"]),
                    lf1=_fmt(row["legacy_f1"]),
                    mf1=_fmt(row["macro_f1"]),
                    gap=_fmt(row["gap_detection_rate"]),
                    gapn=_fmt(row["gap_span_count"]),
                    gold=_fmt(row["total_gold_spans"]),
                    note=note,
                )
            )
        lines.append("")

    return "\n".join(lines)


def main(argv: List[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", type=Path, default=_PROJECT_ROOT / "benchmarks" / "results")
    parser.add_argument(
        "--output", type=Path, default=_PROJECT_ROOT / "benchmarks" / "results" / "comparison_table.md"
    )
    parser.add_argument("--suite", choices=["us", "privacy_gateway"], default="us")
    parser.add_argument("--candidates", type=Path, default=None,
                         help="research/privacy_gateway/candidate_registry.jsonl (privacy_gateway suite only)")
    args = parser.parse_args(argv)

    by_bucket = collect(args.results_dir, suite=args.suite, candidates_path=args.candidates)
    markdown = render_markdown(by_bucket)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(markdown, encoding="utf-8")

    json_path = args.output.with_suffix(".json")
    json_path.write_text(json.dumps(by_bucket, indent=2, sort_keys=True), encoding="utf-8")

    print(f"Markdown table written to {args.output}")
    print(f"JSON twin written to {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
