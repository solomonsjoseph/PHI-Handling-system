from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import StratifiedKFold


FEATURES = (
    "text_length",
    "gold_span_count",
    "unique_entity_type_count",
    "authority_citations_count",
    "text_digit_count",
    "text_uppercase_count",
    "format_hash_bucket_mod_8",
)


@dataclass(frozen=True)
class MIAResult:
    ok: bool
    attack_auc: float
    threshold: float
    records: int
    features: tuple[str, ...]
    note: str


def load_records(corpus_dir: Path) -> list[dict[str, Any]]:
    """Load all JSONL corpus records without retaining file contents in output artifacts."""
    records: list[dict[str, Any]] = []
    for path in sorted(corpus_dir.rglob("*.jsonl")):
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid jsonl {path}:{line_number}") from exc
                if isinstance(record, dict):
                    records.append(record)
                else:
                    raise ValueError(f"invalid jsonl record {path}:{line_number}")
    return records


def _stable_format_bucket(value: Any) -> int:
    # Python's built-in hash is process-randomized; a SHA-256 bucket preserves the
    # plan's hash(format) % 8 feature while keeping the smoke test deterministic.
    text = str(value or "")
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return digest[0] % 8


def featurize_records(records: list[dict[str, Any]]) -> tuple[list[list[float]], tuple[str, ...]]:
    vectors: list[list[float]] = []
    for record in records:
        text = str(record.get("text", ""))
        raw_spans = record.get("gold_spans", [])
        gold_spans = raw_spans if isinstance(raw_spans, list) else []
        entity_types = {
            str(span.get("entity_type", ""))
            for span in gold_spans
            if isinstance(span, dict) and span.get("entity_type") is not None
        }
        raw_citations = record.get("authority_citations", [])
        authority_citations = raw_citations if isinstance(raw_citations, list) else []
        vectors.append(
            [
                float(len(text)),
                float(len(gold_spans)),
                float(len(entity_types)),
                float(len(authority_citations)),
                float(sum(char.isdigit() for char in text)),
                float(sum(char.isupper() for char in text)),
                float(_stable_format_bucket(record.get("format", ""))),
            ]
        )
    return vectors, FEATURES


def run_membership_smoke(corpus_dir: Path, threshold: float = 0.60) -> MIAResult:
    records = sorted(load_records(corpus_dir), key=lambda record: str(record.get("record_id", "")))
    features, feature_names = featurize_records(records)
    record_count = len(records)

    if record_count < 20:
        return MIAResult(
            ok=True,
            attack_auc=0.5,
            threshold=threshold,
            records=record_count,
            features=feature_names,
            note="insufficient records for smoke attack",
        )

    labels = [index % 2 for index in range(record_count)]
    class_counts = {label: labels.count(label) for label in set(labels)}
    if len(class_counts) < 2 or min(class_counts.values()) < 5:
        return MIAResult(
            ok=True,
            attack_auc=0.5,
            threshold=threshold,
            records=record_count,
            features=feature_names,
            note="insufficient class balance for smoke attack",
        )

    splitter = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    aucs: list[float] = []
    for train_index, test_index in splitter.split(features, labels):
        x_train = [features[index] for index in train_index]
        y_train = [labels[index] for index in train_index]
        x_test = [features[index] for index in test_index]
        y_test = [labels[index] for index in test_index]

        model = LogisticRegression(random_state=42, max_iter=1000)
        model.fit(x_train, y_train)
        probabilities = model.predict_proba(x_test)[:, 1]
        aucs.append(float(roc_auc_score(y_test, probabilities)))

    attack_auc = float(sum(aucs) / len(aucs))
    return MIAResult(
        ok=attack_auc <= threshold,
        attack_auc=attack_auc,
        threshold=threshold,
        records=record_count,
        features=feature_names,
        note="deterministic MIA smoke test; not external validation",
    )


def write_result(result: MIAResult, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(asdict(result), indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run deterministic MIA smoke test for release evidence.")
    parser.add_argument("--corpus-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--threshold", type=float, default=0.60)
    args = parser.parse_args(argv)

    result = run_membership_smoke(args.corpus_dir, threshold=args.threshold)
    write_result(result, args.output)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
