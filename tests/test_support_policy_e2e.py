"""End-to-end continuity: a data dictionary fills a GENERALIZE column's map.

Drives the REAL public entry points (``intake_add`` + ``run_pipeline``) on a
mandatory-component v3 source package -- a CSV dataset with a coded ``CODE``
column, a ``forms/`` PDF, and a ``data_dictionary`` mapping ``code -> label``
-- with a review-decision override that classifies ``CODE`` as GENERALIZE.
Proves the full product story the cleanup restored: the dictionary is parsed
as support, an EXACT_HEADER_MATCH link is inferred,
``build_transform_maps_from_support`` fills the synth map, ``run_pipeline``
re-synthesizes so the scrubber sees it, and the PUBLISHED output carries the
broad LABELS (``Low``/``Mid``/``High``) instead of the raw codes -- with a
provenance record written to the protected run zone.

Uses the shared hermetic-workspace/module-isolation harness
(``tests/_workspace_harness.py``) with a tmp_path-scoped PHI_WORKSPACE (no
repo-root pollution, no manual cleanup needed) to avoid stale import-time
configuration and class identity leaking across studies/test files.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from tests._workspace_harness import hermetic_phi_workspace, write_csv, write_pdf_table


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

    with hermetic_phi_workspace(tmp_path, study) as workspace:
        import phi_engine.config.config as config
        from phi_engine.pipeline.intake import intake_add
        from phi_engine.pipeline.run import run_pipeline
        from phi_engine.pipeline.synthesize_config import bootstrap_study_privacy

        # Source tree: a coded dataset + its data dictionary + the
        # mandatory forms/ PDF (an extractable table so it never lands in
        # the organizer's own non-blocking review bucket).
        source = tmp_path / "source"
        dataset_rows = [["S1", "A"], ["S2", "B"], ["S3", "C"]]
        write_csv(source / "datasets" / "labs.csv", ["SUBJID", "CODE"], dataset_rows)
        write_pdf_table(source / "forms" / "consent.pdf", ["FIELD", "VALUE"], [["consent", "signed"]])
        # Real data-dictionary shape: a "variable" column naming the dataset
        # column (that value match yields the EXACT_HEADER_MATCH link) plus
        # code + label columns.
        write_csv(
            source / "data_dictionary" / "labs.csv",
            ["variable", "code", "label"],
            [["CODE", "A", "Low"], ["CODE", "B", "Mid"], ["CODE", "C", "High"]],
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

        intake_manifest = intake_add(source, study)
        assert intake_manifest["status"] == "ready", intake_manifest["review_items"]
        result = run_pipeline(study, "us")

        # The dataset is not held (the dictionary link is auto-helpful), so labs
        # is scrubbed + published.
        published_dir = workspace / "output" / study / "llm_source" / "datasets"
        rows = _published_rows(published_dir)
        assert rows, f"nothing published (exit={result.exit_code}, msg={result.message})"
        # Only the labs dataset carries CODE -- restrict the label/raw-code
        # assertion to rows that actually have that column (the forms/
        # PDF's own extracted-table output is published alongside it under
        # v3's mandatory forms/ requirement, and has no CODE column at all).
        code_rows = [row for row in rows if "CODE" in row]
        assert code_rows, rows
        codes = {row["CODE"] for row in code_rows}
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
        run_dir = workspace / "output" / study / "runs" / result.run_id
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
        rerun_rows = _published_rows(published_dir)
        assert rerun_rows, f"rerun published nothing (exit={rerun.exit_code})"
        rerun_code_rows = [row for row in rerun_rows if "CODE" in row]
        assert {row["CODE"] for row in rerun_code_rows} <= {"Low", "Mid", "High"}
