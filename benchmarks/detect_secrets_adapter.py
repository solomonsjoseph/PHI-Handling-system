"""
Yelp detect-secrets benchmark adapter.

detect-secrets (https://github.com/Yelp/detect-secrets) is a plugin-based
secret-scanning engine: regex/entropy/keyword detectors per secret family
(AWS keys, Stripe keys, GitHub tokens, private keys, JWTs, ...).

Install: pip install detect-secrets (not in this repo's pinned requirements.txt
-- add it there before relying on this adapter in the pinned .venv).

IMPORTANT, measured this session (see
`research/privacy_gateway/evidence_ledger.jsonl` claim ids `sectok-m001`..
`sectok-m003`): detect-secrets' plugins correctly pattern-match
structurally-valid fake secrets at the regex/entropy layer (raw
`plugin.analyze_line()` calls), but several plugins (AWSKeyDetector,
StripeDetector, ...) additionally perform a LIVE NETWORK CALL to verify the
credential against the real provider API. When that call succeeds and
determines the fake key is not a real, live credential, detect-secrets'
default CLI `scan` command SILENTLY DROPS the finding from the report under
its default verification policy (`detect_secrets.filters.common.
is_ignored_due_to_verification_policies`, `min_level=2`) -- a fake/rotated/
revoked secret that a regex-only scan would still flag is absent from the
final `detect-secrets scan` JSON output. This adapter calls each plugin's
`analyze_line()` directly (bypassing the CLI's verification-filter
pipeline) so measured recall reflects pattern-detection capability, not an
artifact of live-verification network behavior; this distinction is
recorded in the result's `note` field.

When detect-secrets is not not importable in the invoking interpreter (it is
installed under a separate system Python in this session, not this repo's
pinned .venv -- see reason string below), this adapter reports `not_run`
rather than guessing at results.

Authority: https://github.com/Yelp/detect-secrets
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from benchmarks.metrics import (
    BenchmarkResult,
    GoldSpan,
    PredictedSpan,
    aggregate_record_scores,
    print_report,
    score_record,
)

_NOT_RUN_REASON = (
    "detect-secrets is not listed in requirements.txt and is not importable "
    "in this repository's pinned .venv (python 3.12+) interpreter; it was "
    "manually installed and exercised under a separate system python3.9 in "
    "this session (see sectok-m001..sectok-m003 in "
    "research/privacy_gateway/evidence_ledger.jsonl) but this adapter -- "
    "invoked through the pinned .venv, matching how every other adapter in "
    "this file is run -- correctly reports not_run rather than a guessed "
    "or environment-inconsistent result. Add detect-secrets to "
    "requirements.txt to make this adapter runnable in the pinned environment."
)

# detect-secrets does not map to our HIPAA/PHI corpus taxonomy; every plugin
# hit is reported as a generic SECRET/API_KEY-family entity type using the
# plugin's own class name, since secrets have no HIPAA category.
DETECT_SECRETS_GAP_ENTITY_TYPES: frozenset = frozenset()

# detect-secrets plugin class name -> our corpus taxonomy entity type(s).
# Every plugin here detects some flavor of credential/token, which this
# repo's fixtures label uniformly as API_KEY (see
# harness/make_privacy_gateway_fixtures.py); PrivateKeyDetector additionally
# maps to PRIVATE_KEY for any future fixture using that gold label.
DETECT_SECRETS_TO_CORPUS: dict[str, frozenset] = {
    "AWSKeyDetector": frozenset({"API_KEY"}),
    "StripeDetector": frozenset({"API_KEY"}),
    "GitHubTokenDetector": frozenset({"API_KEY"}),
    "KeywordDetector": frozenset({"API_KEY"}),
    "Base64HighEntropyString": frozenset({"API_KEY"}),
    "HexHighEntropyString": frozenset({"API_KEY"}),
    "PrivateKeyDetector": frozenset({"API_KEY", "PRIVATE_KEY"}),
}


def _map_detect_secrets_type(plugin_name: str) -> frozenset:
    return DETECT_SECRETS_TO_CORPUS.get(plugin_name, frozenset({plugin_name}))


class DetectSecretsAdapter:
    """Benchmark adapter for Yelp detect-secrets, called at the plugin API
    level (bypasses the CLI's live-verification filtering pipeline -- see
    module docstring)."""

    def __init__(self) -> None:
        self._version = "unknown"
        try:
            import detect_secrets
            from detect_secrets.plugins.aws import AWSKeyDetector
            from detect_secrets.plugins.high_entropy_strings import (
                Base64HighEntropyString,
                HexHighEntropyString,
            )
            from detect_secrets.plugins.keyword import KeywordDetector
            from detect_secrets.plugins.stripe import StripeDetector
            from detect_secrets.plugins.github_token import GitHubTokenDetector
            from detect_secrets.plugins.private_key import PrivateKeyDetector
            self._plugins = [
                AWSKeyDetector(), Base64HighEntropyString(), HexHighEntropyString(),
                KeywordDetector(), StripeDetector(), GitHubTokenDetector(),
                PrivateKeyDetector(),
            ]
            self._available = True
            self._version = getattr(detect_secrets, "__version__", "unknown")
        except ImportError:
            self._available = False
            self._plugins = []
        self._not_run_reason = "" if self._available else _NOT_RUN_REASON

    def analyze_text(self, text: str) -> List[PredictedSpan]:
        if not self._available:
            return []
        spans: List[PredictedSpan] = []
        lines = text.splitlines(keepends=True)
        offset = 0
        for lineno, raw_line in enumerate(lines, start=1):
            line = raw_line.rstrip("\n")
            for plugin in self._plugins:
                try:
                    results = plugin.analyze_line(filename="<text>", line=line, line_number=lineno)
                except Exception:
                    results = None
                for r in (results or []):
                    idx = line.find(r.secret_value) if r.secret_value else -1
                    if idx < 0:
                        continue
                    start = offset + idx
                    end = start + len(r.secret_value)
                    plugin_name = plugin.__class__.__name__
                    mapped_types = _map_detect_secrets_type(plugin_name)
                    spans.append(PredictedSpan(
                        start=start, end=end,
                        entity_type=plugin_name,
                        mapped_type=next(iter(mapped_types)),
                        mapped_types=mapped_types,
                        score=1.0,
                    ))
            offset += len(raw_line)
        return spans

    def _run_file_impl(self, jsonl_path, strategy, overlap_threshold, entity_type_agnostic):
        record_scores = []
        with open(jsonl_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                gold = [
                    GoldSpan(
                        start=s["start"], end=s["end"],
                        entity_type=s["entity_type"],
                        hipaa_category=s.get("hipaa_category"),
                        detection_regime=s.get("detection_regime", "contextual_ner_required"),
                        jurisdiction=s.get("jurisdiction", "us"),
                    )
                    for s in obj.get("gold_spans", [])
                ]
                predicted = self.analyze_text(obj["text"])
                rs = score_record(
                    predicted=predicted, gold=gold,
                    gap_entity_types=DETECT_SECRETS_GAP_ENTITY_TYPES,
                    strategy=strategy, overlap_threshold=overlap_threshold,
                    entity_type_agnostic=entity_type_agnostic,
                )
                rs["record_id"] = obj["record_id"]
                rs["predicted_count"] = len(predicted)
                rs["gold_count"] = len(gold)
                record_scores.append(rs)
        return record_scores

    def run_file(self, jsonl_path, strategy="overlap", overlap_threshold=0.5,
                 entity_type_agnostic=True):
        return self._run_file_impl(jsonl_path, strategy, overlap_threshold, entity_type_agnostic)

    def run_all(
        self,
        corpus_dir: Path,
        pattern: str = "*.jsonl",
        strategy: str = "overlap",
        overlap_threshold: float = 0.5,
        entity_type_agnostic: bool = True,
        scoring_profile: str = "legacy_overlap_coverable",
        verbose: bool = False,
    ) -> BenchmarkResult:
        corpus_dir = Path(corpus_dir)
        tool_name = f"detect-secrets-{self._version}" if self._available else "detect-secrets-not_run"

        if not self._available:
            result = BenchmarkResult(tool_name=tool_name)
            result.corpus_files = []
            return result

        all_scores: List[dict] = []
        total_predicted = 0
        files_processed = []

        for jsonl_path in sorted(corpus_dir.glob(pattern)):
            if verbose:
                print(f"  Processing {jsonl_path.name} ...", end=" ", flush=True)
            file_scores = self._run_file_impl(
                jsonl_path, strategy, overlap_threshold, entity_type_agnostic)
            all_scores.extend(file_scores)
            total_predicted += sum(rs["predicted_count"] for rs in file_scores)
            files_processed.append(str(jsonl_path.name))
            if verbose:
                tp = sum(rs["tp"] for rs in file_scores)
                print(f"{len(file_scores):3d} records  TP={tp}")

        result = aggregate_record_scores(
            all_scores, tool_name=tool_name,
            gap_entity_types=DETECT_SECRETS_GAP_ENTITY_TYPES,
            scoring_profile=scoring_profile,
        )
        result.total_predicted_spans = total_predicted
        result.corpus_files = files_processed
        return result

    def write_results(self, result: BenchmarkResult, output_dir: Path) -> None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / "detect_secrets_benchmark_result.json"
        if not self._available:
            out_path.write_text(json.dumps(
                {"tool": result.tool_name, "status": "not_run", "reason": self._not_run_reason},
                indent=2, sort_keys=True,
            ))
        else:
            summary = result.summary_dict()
            summary["note"] = (
                "Scored via direct plugin.analyze_line() calls, bypassing the "
                "detect-secrets CLI's live-verification filter pipeline; see "
                "module docstring for why the CLI's own `scan` command would "
                "under-report recall on synthetic/inert secrets."
            )
            out_path.write_text(json.dumps(summary, indent=2, sort_keys=True))
        print(f"Results written to {out_path}")


# ---------------------------------------------------------------------------
# CLI -- matches the standard privacy-gateway adapter contract:
# --corpus-dir PATH --output-dir PATH --scoring-profile {strict_all_span,legacy_overlap_coverable}
# --strategy {exact,overlap} --entity-type-aware --verbose
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="detect-secrets benchmark adapter")
    parser.add_argument("--corpus-dir", type=Path, default=_PROJECT_ROOT / "corpus" / "us")
    parser.add_argument("--output-dir", type=Path,
                         default=_PROJECT_ROOT / "benchmarks" / "results" / "detect-secrets")
    parser.add_argument("--scoring-profile", choices=["legacy_overlap_coverable", "strict_all_span"],
                         default="legacy_overlap_coverable")
    parser.add_argument("--strategy", choices=["exact", "overlap"], default="overlap")
    parser.add_argument("--overlap-threshold", type=float, default=0.5)
    parser.add_argument("--entity-type-aware", action="store_true", default=False)
    parser.add_argument("--verbose", "-v", action="store_true", default=False)
    args = parser.parse_args(argv)

    adapter = DetectSecretsAdapter()
    result = adapter.run_all(
        corpus_dir=args.corpus_dir,
        strategy=args.strategy,
        overlap_threshold=args.overlap_threshold,
        entity_type_agnostic=not args.entity_type_aware,
        scoring_profile=args.scoring_profile,
        verbose=args.verbose,
    )
    if adapter._available:
        print_report(result)
    else:
        print(f"detect-secrets: not_run -- {adapter._not_run_reason}")
    adapter.write_results(result, args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
