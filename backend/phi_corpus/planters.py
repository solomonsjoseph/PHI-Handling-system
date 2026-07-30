"""Planter — turn a Scenario + edge-case bag into (corpus_zip, ground_truth).

Ground truth is a plain dict held in memory (Sir's Q1(iii) — never on
disk); the pipeline never sees it. Structure::

    {
      "scenario_id": "oncology_v1",
      "jurisdiction": "us",
      "row_count": 8,
      "planted": [
        {
          "file_name": "enrollment.csv",
          "row": 2,                       # 1-indexed, matching CSV line numbers
          "column": "name",
          "value": "James Smith",
          "hipaa_category": "A",
          "expected_action": "drop",
          "edge_case_tag": "",
        },
        ...
      ],
    }

Every planted cell — whether it is a base PHI value from the column
generator or an edge-case variant — appears exactly once in ``planted``.
Clinical / non-PHI cells appear too with ``hipaa_category="NONE"`` and
``expected_action="keep"`` so the verifier can score false-positives.
"""
from __future__ import annotations

import csv
import io
import random
import zipfile
from dataclasses import dataclass
from typing import Any

from .scenarios import SCENARIOS, Scenario
from .edge_cases import EDGE_CASES, EdgeCase


@dataclass
class PlantedCell:
    file_name: str
    row: int
    column: str
    value: str
    hipaa_category: str
    expected_action: str
    edge_case_tag: str = ""


@dataclass
class CorpusArtifact:
    """Result of ``plant()``.

    ``zip_bytes``     the manifest ZIP the intake endpoint accepts
    ``ground_truth`` the labelled cells the verifier will compare
                     against the pipeline's actual decisions
    ``ground_truth_summary`` counts by category / action for the report
    """
    zip_bytes: bytes
    ground_truth: dict[str, Any]
    ground_truth_summary: dict[str, Any]


def plant(
    scenario_id: str,
    jurisdiction: str = "us",
    edge_case_tags: list[str] | None = None,
    row_count: int = 8,
    seed: int = 42,
) -> CorpusArtifact:
    """Plant PHI/PII per the scenario + edge-cases and emit both the corpus
    ZIP and the ground-truth dict.

    Emits two study components only:
      1. ``datasets/*.csv`` -- tabular data with per-row PHI plants
      2. ``dictionary/columns.csv`` -- data dictionary describing each column
    """
    scn = SCENARIOS[scenario_id]
    rng = random.Random(seed)
    edge_cases = [EDGE_CASES[t] for t in (edge_case_tags or []) if t in EDGE_CASES]

    zbuf = io.BytesIO()
    planted: list[PlantedCell] = []

    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
        # 1) datasets/
        for ds in scn.datasets:
            csv_text, cells = _generate_dataset(scn, ds, edge_cases, row_count, rng)
            z.writestr(f"datasets/{ds.filename}", csv_text)
            planted.extend(cells)

        # 2) dictionary/
        dict_text = _generate_dictionary(scn)
        z.writestr("dictionary/columns.csv", dict_text)

    ground_truth = {
        "scenario_id": scenario_id,
        "jurisdiction": jurisdiction,
        "row_count": row_count,
        "edge_case_tags": [ec.tag for ec in edge_cases],
        "seed": seed,
        "planted": [c.__dict__ for c in planted],
    }
    summary = _summarise(planted)
    return CorpusArtifact(
        zip_bytes=zbuf.getvalue(),
        ground_truth=ground_truth,
        ground_truth_summary=summary,
    )


def _generate_dataset(
    scn: Scenario,
    ds,
    edge_cases: list[EdgeCase],
    row_count: int,
    rng: random.Random,
) -> tuple[str, list[PlantedCell]]:
    """Generate one CSV file + per-cell ground truth for it."""
    # A given edge case is applied to ALL rows of the target column so the
    # torture-test signal is not diluted. Multiple edge cases targeting
    # DIFFERENT columns can coexist; two edge cases on the SAME column
    # is disallowed (last one wins).
    edge_by_column: dict[str, EdgeCase] = {}
    for ec in edge_cases:
        # Only bind the edge case if the column actually exists in this
        # dataset. Edge cases targeting a column absent from this file are
        # silently ignored — the same edge-case tag can apply across
        # multiple scenarios that use different column names.
        for col in ds.columns:
            if col.name == ec.applies_to_column:
                edge_by_column[ec.applies_to_column] = ec
                break

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([c.name for c in ds.columns])

    planted: list[PlantedCell] = []
    for row_idx in range(row_count):
        line_no = row_idx + 2   # CSV line 1 is the header
        cells: list[str] = []
        for col in ds.columns:
            ec = edge_by_column.get(col.name)
            if ec:
                value = ec.mutate(rng)
                expected = ec.override_expected_action or col.expected_action
                tag = ec.tag
            else:
                value = col.generator(rng)
                expected = col.expected_action
                tag = ""
            cells.append(value)
            planted.append(PlantedCell(
                file_name=ds.filename,
                row=line_no,
                column=col.name,
                value=value,
                hipaa_category=col.hipaa_category,
                expected_action=expected,
                edge_case_tag=tag,
            ))
        writer.writerow(cells)

    return buf.getvalue(), planted


def _generate_dictionary(scn: Scenario) -> str:
    """Generate a per-scenario codebook CSV that the Lexicon agent reads."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["column_name", "description", "type"])
    for r in scn.dictionary:
        w.writerow([r.column_name, r.description, r.type])
    return buf.getvalue()


def _summarise(planted: list[PlantedCell]) -> dict[str, Any]:
    """Aggregate counts by category / action so callers can present the
    corpus at a glance without walking every cell."""
    by_cat: dict[str, int] = {}
    by_action: dict[str, int] = {}
    by_edge: dict[str, int] = {}
    for c in planted:
        by_cat[c.hipaa_category] = by_cat.get(c.hipaa_category, 0) + 1
        by_action[c.expected_action] = by_action.get(c.expected_action, 0) + 1
        if c.edge_case_tag:
            by_edge[c.edge_case_tag] = by_edge.get(c.edge_case_tag, 0) + 1
    return {
        "total_cells": len(planted),
        "phi_cells": sum(1 for c in planted if c.hipaa_category not in ("", "NONE")),
        "clinical_cells": sum(1 for c in planted if c.hipaa_category in ("", "NONE")),
        "by_category": by_cat,
        "by_expected_action": by_action,
        "by_edge_case": by_edge,
    }
