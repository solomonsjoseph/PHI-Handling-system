"""Planter -- turn a Scenario + edge-case bag into (corpus_zip, ground_truth).

Ground truth is a plain dict held in memory (Sir's Q1(iii) -- never on
disk); the pipeline never sees it. Structure::

    {
      "scenario_id": "oncology_v1",
      "jurisdiction": "us",
      "row_count": 8,
      "corpus_version": "<12 hex chars>",
      "tier": "L0",
      "profile": "clean",
      "planted": [
        {
          "file_name": "enrollment.csv",
          "row": 2,                       # 1-indexed, matching CSV line numbers
          "column": "name",
          "value": "James Smith",
          "hipaa_category": "A",
          "expected_action": "drop",
          "edge_case_tag": "",
          "plant_id": "p0001",
          "tier": "L0",
          "expectation": {...} | None,
          "leak_literals": [...],
          "link_group": "",
          "difficulty_note": "",
          "sensitivity_class": "",
        },
        ...
      ],
      "columns": [...],
      "dictionary_drift": {"undocumented_columns": [...], "phantom_columns": [...]},
    }

Every planted cell -- whether it is a base PHI value from the column
generator or an edge-case variant -- appears exactly once in ``planted``.
Clinical / non-PHI cells appear too with ``hipaa_category="NONE"`` and
``expected_action="keep"`` so the verifier can score both PHI removal and
clinical-data preservation.

The seven fields beyond the original six (``plant_id``, ``tier``,
``expectation``, ``leak_literals``, ``link_group``, ``difficulty_note``,
``sensitivity_class``) are additive; every existing key and its meaning is
unchanged, so ``backend/tests/test_corpus.py`` needs no edit.
"""
from __future__ import annotations

import csv
import dataclasses
import io
import random
import zipfile
from dataclasses import dataclass
from typing import Any

from .scenarios import SCENARIOS, Scenario, DatasetSpec, ColumnSpec
from .scenarios import REDCAP_DICTIONARY_HEADERS, REDCAP_DICTIONARIES
from .edge_cases import EDGE_CASES, EdgeCase
from . import realism as _realism
from .tiers import corpus_version as _corpus_version

from phi_core.jurisdictions import get_pack as _get_pack

_DENY_ZIP3: frozenset = _get_pack("us").restricted_zip3_prefixes


class CorpusCollisionError(Exception):
    """Raised when a planted PHI literal collides with a verbatim-surviving
    cell or dictionary text after 50 redraw attempts. A collision here
    would make a residual-literal hit in an export ambiguous -- it could be
    the genuine leak the scan exists to catch, or an innocent coincidence
    -- so ``plant()`` refuses to ship a corpus where that ambiguity exists.
    """
    def __init__(self, plant_id: str, literal: str):
        super().__init__(f"canary collision: plant {plant_id!r} literal {literal!r} "
                          f"reused by a verbatim-surviving cell or the dictionary")
        self.plant_id = plant_id
        self.literal = literal


@dataclass(frozen=True)
class ExportExpectation:
    """What a planted cell must look like in the pipeline's actual export
    bytes, derived from the semantic facts recorded at generation time --
    never by re-parsing the rendered string, so this cannot drift into
    agreeing with ``phi_core.agents.reasoning._apply_action`` by
    construction.
    """
    kind: str                                  # "literal" | "regex" | "text_scrub"
    literal: str = ""
    pattern: str = ""
    must_contain: tuple[str, ...] = ()
    must_not_contain: tuple[str, ...] = ()
    link_group: str = ""
    survives_verbatim: bool = False


def expected_for(action: str, value: str, sem: dict) -> ExportExpectation:
    """Derive the expected export value for one planted cell.

    Raises ``ValueError`` when ``action`` needs a semantic fact ``sem``
    does not carry (``cap_age_90`` needs ``age``, ``year_only`` needs
    ``year``, ``zip3_truncate`` needs ``zip3`` or ``non_us``), so a
    scenario author who forgets to return semantics from a generator gets
    an immediate failure rather than a silently unscoreable cell.
    """
    if action == "keep":
        return ExportExpectation(kind="literal", literal=value, survives_verbatim=True)
    if action == "drop":
        return ExportExpectation(kind="literal", literal="")
    if action == "cap_age_90":
        if "age" not in sem:
            raise ValueError(f"{action} column needs semantic facts")
        if sem.get("missing"):
            return ExportExpectation(kind="literal", literal=value)
        age = sem["age"]
        return ExportExpectation(kind="literal", literal="90+" if age > 89 else str(age))
    if action == "year_only":
        if "year" not in sem:
            raise ValueError(f"{action} column needs semantic facts")
        if sem.get("missing"):
            return ExportExpectation(kind="literal", literal=value)
        return ExportExpectation(kind="literal", literal=str(sem["year"]))
    if action == "zip3_truncate":
        if "zip3" not in sem and not sem.get("non_us"):
            raise ValueError(f"{action} column needs semantic facts")
        if sem.get("non_us"):
            return ExportExpectation(kind="literal", literal="")
        zip3 = sem["zip3"]
        literal = "000" if zip3 in _DENY_ZIP3 else zip3
        return ExportExpectation(kind="literal", literal=literal)
    if action == "pseudonymize":
        return ExportExpectation(kind="regex", pattern=r"^P[0-9a-f]{8}$",
                                  link_group=sem.get("subject", ""))
    if action == "hash":
        return ExportExpectation(kind="regex", pattern=r"^[0-9a-f]{16}$")
    if action == "scrub_text":
        literals = tuple(sem.get("literals") or ())
        if literals:
            fragment = sem.get("clinical_fragment", "")
            must_contain = (fragment,) if fragment else ()
            return ExportExpectation(kind="text_scrub", must_not_contain=literals,
                                      must_contain=must_contain)
        return ExportExpectation(kind="literal", literal=value, survives_verbatim=True)
    if action == "human_review":
        # ``literal`` here is the display placeholder for masking/reporting
        # only. The full 12-agent online pipeline can RESOLVE a deferral
        # (see campaign.run_online's human-review step) before the export
        # is produced, so "pending" is not the only valid outcome -- see
        # ``verify._check_expectation``'s ``human_review`` branch, which
        # also accepts drop (PHI removed) or keep-verbatim for a NONE
        # category column (clinical data correctly preserved). The offline
        # deterministic replay never resolves a deferral, so "pending" is
        # the only outcome it can ever actually observe.
        return ExportExpectation(kind="human_review", literal="[HUMAN_REVIEW_PENDING]")
    raise ValueError(f"unknown action: {action!r}")


@dataclass
class PlantedCell:
    file_name: str
    row: int
    column: str
    value: str
    hipaa_category: str
    expected_action: str
    edge_case_tag: str = ""
    plant_id: str = ""
    tier: str = ""
    expectation: ExportExpectation | None = None
    leak_literals: tuple[str, ...] = ()
    link_group: str = ""
    difficulty_note: str = ""
    sensitivity_class: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        if d["expectation"] is not None:
            d["expectation"] = dataclasses.asdict(d["expectation"])
        d["leak_literals"] = list(d["leak_literals"])
        return d


@dataclass
class CorpusArtifact:
    """Result of ``plant()``.

    ``zip_bytes``            the manifest ZIP the intake endpoint accepts
    ``ground_truth``         the labelled cells the verifier compares
                             against the pipeline's actual decisions
    ``ground_truth_summary`` counts by category / action for the report
    """
    zip_bytes: bytes
    ground_truth: dict[str, Any]
    ground_truth_summary: dict[str, Any]


class _PlantIdSeq:
    """Monotonic ``"p0001"``-style id, reset per ``plant()`` call."""
    def __init__(self):
        self._n = 0

    def next(self) -> str:
        self._n += 1
        return f"p{self._n:04d}"


def _normalize(out: Any) -> tuple[str, dict]:
    """A generator/mutate call may return a plain ``str`` (legacy shape) or
    a ``(value, sem)`` tuple. Normalize to the tuple shape."""
    if isinstance(out, tuple):
        return out[0], (out[1] or {})
    return out, {}


def _draw_cell_value(ds: DatasetSpec, col: ColumnSpec, ec: EdgeCase | None,
                      roster: dict[str, list[str]], subject_idx: int,
                      rng: random.Random, prof: "_realism.RealismProfile") -> tuple[str, dict, str, str]:
    """Returns (value, sem, expected_action, edge_case_tag).

    ``jitter`` is applied only to ``keep``-action values from an unmutated
    generator: the export oracle for ``keep`` is ``literal=value`` no
    matter what ``value`` is, so jittering it first is byte-safe. Roster
    (cross-file linkage) values and edge-case mutations are left alone --
    jittering a roster value would desync the join it exists to guarantee,
    and an edge case already carries its own deliberate mutation. The
    result is rstripped: ``_check_expectation`` already treats trailing
    whitespace on ``actual`` as insignificant (``actual.rstrip()``), so
    jitter's whitespace dial would be silently absorbed on one side of an
    unstripped literal, false-flagging a correctly-preserved value as a
    utility loss. Case noise and the surname-first reorder are unaffected
    and stay meaningfully testable.
    """
    if col.name == ds.link_column and ds.link_column in roster:
        return roster[ds.link_column][subject_idx], {"subject": ds.link_column}, \
            col.expected_action, (col.edge_case_tag or "")
    if ec is not None:
        value, sem = _normalize(ec.mutate(rng))
        return value, sem, (ec.override_expected_action or col.expected_action), ec.tag
    value, sem = _normalize(col.generator(rng))
    if (col.expected_action == "keep" and col.jitterable and value
            and not sem.get("missing") and prof.name != "clean"):
        # Gated on the explicit `jitterable` opt-in (see ColumnSpec), not
        # value shape: a token-count heuristic is not a safe proxy for
        # "free narrative text" -- a two-token controlled term (CDISC
        # ARMCD "ARM A") is just as invalid case-noised ("arm a") as
        # reordered ("A ARM"), so applying jitter by default to anything
        # multi-word would manufacture terms that do not exist in the
        # controlled vocabulary. reorder=False unconditionally: "keep"
        # columns never hold a person name (names are always PHI / drop),
        # so the surname-first swap has no realistic counterpart here.
        value = _realism.jitter(value, prof, rng, reorder=False).rstrip() or value
    return value, sem, col.expected_action, (col.edge_case_tag or "")


def _finalize_cell(ds: DatasetSpec, col: ColumnSpec, value: str, sem: dict, expected: str,
                    tag: str, tier: str, plant_id: str, row: int) -> PlantedCell:
    try:
        expectation = expected_for(expected, value, sem)
    except ValueError:
        # Legacy (pre-oracle) generator supplied no semantic facts for an
        # action that needs them. The cell still participates in leak
        # scanning and correctness scoring; it just is not export-byte
        # scoreable. See module docstring / A2 for the rationale.
        expectation = None

    if sem.get("literals"):
        leak_literals = tuple(sem["literals"])
    elif col.hipaa_category not in ("", "NONE"):
        leak_literals = (value,) if value else ()
    else:
        leak_literals = ()

    sensitivity_class = sem.get("sensitivity_class") or col.sensitivity_class or ""

    return PlantedCell(
        file_name=ds.filename,
        row=row,
        column=col.name,
        value=value,
        hipaa_category=col.hipaa_category,
        expected_action=expected,
        edge_case_tag=tag,
        plant_id=plant_id,
        tier=tier,
        expectation=expectation,
        leak_literals=leak_literals,
        link_group=(expectation.link_group if expectation else ""),
        sensitivity_class=sensitivity_class,
    )


def _build_roster(scn: Scenario, row_count: int, rng: random.Random) -> dict[str, list[str]]:
    """Draw the shared roster for every ``link_column`` declared by any
    dataset in the scenario, once, reused across every dataset declaring
    the SAME link_column name."""
    roster: dict[str, list[str]] = {}
    for ds in scn.datasets:
        if not ds.link_column or ds.link_column in roster:
            continue
        matching = next((c for c in ds.columns if c.name == ds.link_column), None)
        if matching is None:
            raise ValueError(
                f"scenario {scn.id!r} dataset {ds.filename!r} declares "
                f"link_column={ds.link_column!r} but has no such column"
            )
        roster[ds.link_column] = [_normalize(matching.generator(rng))[0] for _ in range(row_count)]
    # A dataset whose link_column was seeded by an EARLIER dataset but does
    # not itself carry that column name would never reach the lookup above.
    for ds in scn.datasets:
        if ds.link_column and not any(c.name == ds.link_column for c in ds.columns):
            raise ValueError(
                f"scenario {scn.id!r} dataset {ds.filename!r} declares "
                f"link_column={ds.link_column!r} but has no such column"
            )
    return roster


def _generate_dataset_matrix(scn: Scenario, ds: DatasetSpec, edge_cases: list[EdgeCase],
                              row_count: int, rng: random.Random, tier: str,
                              roster: dict[str, list[str]],
                              plant_seq: _PlantIdSeq, prof: "_realism.RealismProfile") -> list[list[PlantedCell]]:
    edge_by_column: dict[str, EdgeCase] = {}
    for ec in edge_cases:
        for col in ds.columns:
            if col.name == ec.applies_to_column:
                prior = edge_by_column.get(ec.applies_to_column)
                if prior is not None and prior.tag != ec.tag:
                    raise ValueError(
                        f"scenario {scn.id!r} dataset {ds.filename!r}: edge cases "
                        f"{prior.tag!r} and {ec.tag!r} both target column "
                        f"{ec.applies_to_column!r} -- a column holds one value per "
                        f"row, so only one mutation can apply; drop one from the "
                        f"requested edge_case_tags"
                    )
                edge_by_column[ec.applies_to_column] = ec
                break

    rps = max(ds.rows_per_subject, 1)
    total_rows = row_count * rps
    matrix: list[list[PlantedCell]] = []
    for row_idx in range(total_rows):
        line_no = row_idx + 2  # CSV line 1 is the header
        subject_idx = row_idx // rps
        row_cells: list[PlantedCell] = []
        for col in ds.columns:
            ec = edge_by_column.get(col.name)
            value, sem, expected, tag = _draw_cell_value(ds, col, ec, roster, subject_idx, rng, prof)
            row_cells.append(_finalize_cell(ds, col, value, sem, expected, tag, tier,
                                             plant_seq.next(), line_no))
        matrix.append(row_cells)
    return matrix


def _enforce_canary_uniqueness(scn: Scenario, edge_cases: list[EdgeCase],
                                matrices: dict[str, list[list[PlantedCell]]], dict_text: str,
                                rng: random.Random, tier: str,
                                roster: dict[str, list[str]], prof: "_realism.RealismProfile") -> None:
    """No leak literal of length >= 4 may appear as a substring of any cell
    whose expectation has ``survives_verbatim=True``, nor of any dictionary
    cell. On collision the offending cell is redrawn from its own
    generator/edge-case, up to 50 attempts, then raises
    ``CorpusCollisionError``. Roster-linked (cross-file join) cells are
    never redrawn, since mutating one would desynchronize the linkage the
    roster exists to guarantee; their entropy is high enough that this
    exclusion carries negligible practical risk.
    """
    def blob() -> str:
        parts = [dict_text.lower()]
        for matrix in matrices.values():
            for row in matrix:
                for cell in row:
                    if cell.expectation is not None and cell.expectation.survives_verbatim and cell.value:
                        parts.append(cell.value.lower())
        return "\n".join(parts)

    ds_by_name = {d.filename: d for d in scn.datasets}
    edge_by_dataset: dict[str, dict[str, EdgeCase]] = {}
    for ds in scn.datasets:
        eb: dict[str, EdgeCase] = {}
        for ec in edge_cases:
            for col in ds.columns:
                if col.name == ec.applies_to_column:
                    eb[ec.applies_to_column] = ec
                    break
        edge_by_dataset[ds.filename] = eb

    for filename, matrix in matrices.items():
        ds = ds_by_name[filename]
        col_by_name = {c.name: c for c in ds.columns}
        edge_by_column = edge_by_dataset[filename]
        for row in matrix:
            for col_idx, cell in enumerate(row):
                col = col_by_name[cell.column]
                is_roster = bool(ds.link_column) and col.name == ds.link_column and ds.link_column in roster
                if is_roster:
                    continue
                attempts = 0
                while True:
                    haystack = blob()
                    hit = next((lit for lit in cell.leak_literals
                                if len(lit) >= 4 and lit.lower() in haystack), None)
                    if hit is None:
                        break
                    if attempts >= 50:
                        raise CorpusCollisionError(cell.plant_id, hit)
                    attempts += 1
                    ec = edge_by_column.get(col.name)
                    value, sem, expected, tag = _draw_cell_value(ds, col, ec, roster, 0, rng, prof)
                    new_cell = _finalize_cell(ds, col, value, sem, expected, tag, tier,
                                               cell.plant_id, cell.row)
                    row[col_idx] = new_cell
                    cell = new_cell


def _serialize_csv(header: list[str], matrix: list[list[PlantedCell]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in matrix:
        writer.writerow([cell.value for cell in row])
    return buf.getvalue()


def _generate_dictionary(scn: Scenario) -> str:
    """Generate a per-scenario codebook CSV that the Lexicon agent reads.

    Two REDCap scenarios ship the real fixed 18-column REDCap data
    dictionary shape instead of the generic 3-column one, because
    ``Identifier?`` flag coverage IS the scenario under test.
    """
    if scn.id in REDCAP_DICTIONARIES:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(list(REDCAP_DICTIONARY_HEADERS))
        for row in REDCAP_DICTIONARIES[scn.id]:
            w.writerow(list(row))
        return buf.getvalue()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["column_name", "description", "type"])
    for r in scn.dictionary:
        w.writerow([r.column_name, r.description, r.type])
    return buf.getvalue()


def _dictionary_drift(scn: Scenario) -> dict[str, list[str]]:
    """Columns documented in the dictionary but absent from every dataset
    (phantom), and columns present in a dataset but undocumented."""
    dataset_columns: set[str] = set()
    for ds in scn.datasets:
        dataset_columns.update(c.name for c in ds.columns)
    if scn.id in REDCAP_DICTIONARIES:
        documented = {row[0] for row in REDCAP_DICTIONARIES[scn.id]}
    else:
        documented = {r.column_name for r in scn.dictionary}
    return {
        "undocumented_columns": sorted(dataset_columns - documented),
        "phantom_columns": sorted(documented - dataset_columns),
    }


def plant(
    scenario_id: str,
    jurisdiction: str = "us",
    edge_case_tags: list[str] | None = None,
    row_count: int = 12,
    seed: int = 42,
    *,
    profile: str = "",
    tier: str = "",
) -> CorpusArtifact:
    """Plant PHI/PII per the scenario + edge-cases and emit both the corpus
    ZIP and the ground-truth dict.

    Emits two study components only:
      1. ``datasets/*.csv`` -- tabular data with per-row PHI plants
      2. ``dictionary/*.csv`` -- data dictionary describing each column

    ``profile`` overrides the scenario's own declared realism profile when
    non-empty (controls only the ZIP member text encoding here; per-value
    messiness is baked into each scenario's own generators).  ``tier``
    overrides the scenario's own declared tier for ground-truth stamping.
    """
    scn = SCENARIOS[scenario_id]
    rng = random.Random(seed)
    edge_cases = [EDGE_CASES[t] for t in (edge_case_tags or []) if t in EDGE_CASES]
    prof = _realism.PROFILES[profile or scn.profile]
    use_tier = tier or scn.tier

    roster = _build_roster(scn, row_count, rng)
    plant_seq = _PlantIdSeq()

    matrices: dict[str, list[list[PlantedCell]]] = {}
    for ds in scn.datasets:
        matrices[ds.filename] = _generate_dataset_matrix(
            scn, ds, edge_cases, row_count, rng, use_tier, roster, plant_seq, prof
        )

    dict_text = _generate_dictionary(scn)

    _enforce_canary_uniqueness(scn, edge_cases, matrices, dict_text, rng, use_tier, roster, prof)

    zbuf = io.BytesIO()
    planted: list[PlantedCell] = []
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
        for ds in scn.datasets:
            matrix = matrices[ds.filename]
            csv_text = _serialize_csv([c.name for c in ds.columns], matrix)
            z.writestr(f"datasets/{ds.filename}", csv_text.encode(prof.encoding))
            for row in matrix:
                planted.extend(row)
        z.writestr("dictionary/columns.csv", dict_text.encode(prof.encoding))

    columns_meta = [
        {
            "file_name": ds.filename,
            "column": col.name,
            "hipaa_category": col.hipaa_category,
            "expected_action": col.expected_action,
            "sensitivity_class": col.sensitivity_class,
        }
        for ds in scn.datasets
        for col in ds.columns
    ]

    ground_truth = {
        "scenario_id": scenario_id,
        "jurisdiction": jurisdiction,
        "row_count": row_count,
        "edge_case_tags": [ec.tag for ec in edge_cases],
        "seed": seed,
        "corpus_version": _corpus_version(),
        "tier": use_tier,
        "profile": prof.name,
        "planted": [c.to_dict() for c in planted],
        "columns": columns_meta,
        "dictionary_drift": _dictionary_drift(scn),
    }
    summary = _summarise(planted)
    return CorpusArtifact(
        zip_bytes=zbuf.getvalue(),
        ground_truth=ground_truth,
        ground_truth_summary=summary,
    )


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
