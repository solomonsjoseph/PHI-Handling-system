"""End-to-end continuity: a data dictionary fills a GENERALIZE column's map.

Drives the REAL public entry points (``intake_add`` + ``run_pipeline``) on a
hand-built source tree — a dataset with a coded ``CODE`` column plus a
``data_dictionary`` mapping ``code -> label`` — with a review-decision override
that classifies ``CODE`` as GENERALIZE. Proves the full product story the
cleanup restored: the dictionary is parsed as support, an EXACT_HEADER_MATCH
link is inferred, ``build_transform_maps_from_support`` fills the synth map,
``run_pipeline`` re-synthesizes so the scrubber sees it, and the PUBLISHED
output carries the broad LABELS (``Low``/``Mid``/``High``) instead of the raw
codes — with a provenance record written to the protected run zone.

Uses a hermetic env pattern (per-study STUDY_NAME + PHI key, module sweep so
STUDY_NAME-derived config paths resolve fresh, full per-study cleanup) to
avoid stale import-time configuration and class identity leaking across
studies.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_PHI_KEY_HEX = "0" * 64


def _drop_phi_runtime_modules() -> None:
    keep = {"phi_engine", "phi_engine.utils", "phi_engine.utils.pipeline_lock"}
    for name in list(sys.modules):
        if name in keep:
            continue
        if name.startswith("phi_engine."):
            del sys.modules[name]


def _published_rows(published_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(published_dir.glob("*.jsonl")):
        rows.extend(
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    return rows


def test_dictionary_fills_generalize_map_and_publishes_labels(tmp_path):
    study = f"PytestSupportE2E{uuid.uuid4().hex[:8]}"
    original_study = os.environ.get("STUDY_NAME")
    original_key = os.environ.get("PHI_KEY_PATH")

    key_path = tmp_path / "phi_key"
    key_path.write_text(TEST_PHI_KEY_HEX, encoding="utf-8")
    key_path.chmod(0o600)
    os.environ["STUDY_NAME"] = study
    os.environ["PHI_KEY_PATH"] = str(key_path)
    _drop_phi_runtime_modules()

    try:
        import phi_engine.config.config as config
        from phi_engine.pipeline.intake import intake_add
        from phi_engine.pipeline.run import run_pipeline
        from phi_engine.pipeline.synthesize_config import bootstrap_study_privacy

        # Source tree: a coded dataset + its data dictionary.
        source = tmp_path / "source"
        (source / "data_dictionary").mkdir(parents=True)
        dataset_rows = [
            {"SUBJID": "S1", "CODE": "A"},
            {"SUBJID": "S2", "CODE": "B"},
            {"SUBJID": "S3", "CODE": "C"},
        ]
        (source / "labs.jsonl").write_text(
            "\n".join(json.dumps(r) for r in dataset_rows) + "\n", encoding="utf-8"
        )
        # Real data-dictionary shape: a "variable" column naming the dataset
        # column (that value match yields the EXACT_HEADER_MATCH link) plus
        # code + label columns. The parser strips the CSV header row.
        (source / "data_dictionary" / "labs.csv").write_text(
            "variable,code,label\nCODE,A,Low\nCODE,B,Mid\nCODE,C,High\n",
            encoding="utf-8",
        )

        # Seed the study config dir + a review-decision override making CODE a
        # GENERALIZE column (the reviewer's "this coded column needs a taxonomy").
        bootstrap_study_privacy(study, "USA")
        decisions_path = Path(config.study_config_dir(study)) / "review_decisions.yaml"
        decisions_path.write_text(
            "CODE:\n"
            "  decision: override\n"
            "  action: generalize\n"
            "  decided_by: reviewer\n"
            '  decided_at: "2026-07-14T10:00:00Z"\n'
            "  source: file\n",
            encoding="utf-8",
        )

        intake_add(source, study)
        result = run_pipeline(study, "us")

        # The dataset is not held (the dictionary link is auto-helpful), so labs
        # is scrubbed + published.
        published_dir = REPO_ROOT / "output" / study / "llm_source" / "datasets"
        rows = _published_rows(published_dir)
        assert rows, f"nothing published (exit={result.exit_code}, msg={result.message})"

        codes = {row.get("CODE") for row in rows}
        # Raw codes were rewritten to their dictionary labels; no raw A/B/C leaks.
        assert codes <= {"Low", "Mid", "High"}, codes
        assert codes & {"Low", "Mid", "High"}
        assert not (codes & {"A", "B", "C"})

        # The synthesized scrub config carries the filled generalize map.
        scrub_yaml = Path(config.study_config_dir(study)) / "phi_scrub.yaml"
        import yaml

        cfg = yaml.safe_load(scrub_yaml.read_text(encoding="utf-8"))
        gen_maps = cfg.get("generalization_maps") or {}
        assert gen_maps.get("_synth_generalize_code"), gen_maps

        # Provenance was written to the protected run zone.
        run_dir = REPO_ROOT / "output" / study / "runs" / result.run_id
        provenance = run_dir / ".protected" / "support_transform_maps.jsonl"
        assert provenance.is_file()
        records = [
            json.loads(line)
            for line in provenance.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert records and records[0]["map_name"] == "_synth_generalize_code"

        # Idempotent: a second run over the SAME study still publishes the labels
        # (the baseline-hashed recommendations stay stable, so the support-filled
        # GENERALIZE header is never spuriously re-held on rerun).
        rerun = run_pipeline(study, "us")
        rerun_rows = _published_rows(
            REPO_ROOT / "output" / study / "llm_source" / "datasets"
        )
        assert rerun_rows, f"rerun published nothing (exit={rerun.exit_code})"
        assert {row.get("CODE") for row in rerun_rows} <= {"Low", "Mid", "High"}
    finally:
        if original_study is None:
            os.environ.pop("STUDY_NAME", None)
        else:
            os.environ["STUDY_NAME"] = original_study
        if original_key is None:
            os.environ.pop("PHI_KEY_PATH", None)
        else:
            os.environ["PHI_KEY_PATH"] = original_key
        for sub in ("phi_engine/config", "tmp", "output", "intake", "organized", "data/raw"):
            shutil.rmtree(REPO_ROOT / sub / study, ignore_errors=True)
        _drop_phi_runtime_modules()
