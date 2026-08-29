"""Corpus verifier -- tabular-only scoring, extended with export-byte proof.

Given a completed pipeline session and the ground-truth dict produced by
``phi_corpus.planters.plant()``, this module compares every planted cell's
expected action against the pipeline's Judge decision (``correctness`` /
``deferral``, unchanged contract) AND, when ``export_paths`` is supplied,
against the actual bytes the pipeline exported (``leak`` / ``transform`` /
``utility`` / ``regulation``, new in this revision). A perfect
``correctness`` score alone does not prove PHI left the export bytes; the
new blocks do.

Scoring path: every plant is a tabular column decision. One Judge decision
governs an entire column, so ``correctness``/``deferral`` credit or debit
each (file, column) pair once. The new blocks score every planted CELL
against its own ``ExportExpectation``, because two cells in the same
column can differ in value hostility even when the column-level decision
is identical.
"""
from __future__ import annotations

import csv
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from .tiers import REQUIRED_VIOLATIONS

if TYPE_CHECKING:
    from phi_core.control.store import ControlStore


@dataclass
class Miss:
    """One row of the false-positive / false-negative table."""
    file: str
    column: str
    expected_action: str
    actual_action: str
    hipaa_category: str
    edge_case_tag: str = ""
    reason: str = ""


@dataclass
class CategoryScore:
    """Per-HIPAA-category precision/recall/F1."""
    category: str
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    @property
    def precision(self) -> float:
        d = self.tp + self.fp
        return round(self.tp / d, 4) if d else 1.0

    @property
    def recall(self) -> float:
        d = self.tp + self.fn
        return round(self.tp / d, 4) if d else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return round(2 * p * r / (p + r), 4) if (p + r) else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "tp": self.tp, "fp": self.fp, "fn": self.fn, "tn": self.tn,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
        }


# Actions considered "PHI-touching" for the correctness score. If a cell
# with ground-truth expected_action in this set is actually decided as
# ``keep``, that is a FALSE-NEGATIVE (PHI leak). If a cell with expected
# action ``keep`` is actually decided as anything in this set, that is a
# FALSE-POSITIVE (over-blocking clinical data).
_PHI_ACTIONS: frozenset[str] = frozenset({
    "drop", "year_only", "zip3_truncate", "cap_age_90",
    "pseudonymize", "hash", "scrub_text",
})

_HIPAA_LETTERS = frozenset("ABCDEFGHIJKLMNOPQR")


def mask(s: str) -> str:
    """Reproduce ``phi_core.publish_guard._sanitise_sample``: all asterisks
    when ``len(s) <= 4``, otherwise ``s[:2] + "*" * (len(s) - 4) + s[-2:]``.
    No raw planted value ever enters a report."""
    if not s:
        return s
    if len(s) <= 4:
        return "*" * len(s)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9@.'\-]+")


def _read_export_rows(path: str) -> list[list[str]]:
    """Return rows (list of cell strings, row 0 is the header) for a
    csv/tsv/xlsx export. Falls back to one single-cell row per line for
    ``.txt`` and anything else. Never raises on a missing/empty file."""
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return []
    ext = p.suffix.lower().lstrip(".")
    if ext in ("csv", "tsv"):
        delim = "\t" if ext == "tsv" else ","
        with p.open("r", encoding="utf-8", errors="replace", newline="") as f:
            return list(csv.reader(f, delimiter=delim))
    if ext in ("xlsx", "xls"):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(p, data_only=True)
            return [
                ["" if c is None else str(c) for c in r]
                for ws in wb.worksheets
                for r in ws.iter_rows(values_only=True)
            ]
        except Exception:
            return []
    with p.open("r", encoding="utf-8", errors="replace") as f:
        return [[line.rstrip("\n")] for line in f]


def _partition_leak_literals(
    planted: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[tuple[str, dict[str, Any]]], int]:
    """Partition every planted cell's ``leak_literals`` into a single-token
    lowercase lookup and a multi-token substring list. Shared by every leak
    scanner in this module (export bytes, run surfaces) so a run surface is
    checked against literally the same planted-literal set an export scan
    is. Literals shorter than 4 characters are excluded -- matching the
    canary-uniqueness floor in ``planters.py`` -- because shorter strings
    collide with innocuous content too often to be a meaningful signal.
    """
    single_token: dict[str, dict[str, Any]] = {}
    multi_token: list[tuple[str, dict[str, Any]]] = []
    phi_plants = 0
    for cell in planted:
        literals = cell.get("leak_literals") or []
        if literals:
            phi_plants += 1
        for lit in literals:
            if not lit or len(lit) < 4:
                continue
            tokens = [t for t in _TOKEN_SPLIT.split(lit) if t]
            if len(tokens) <= 1:
                single_token.setdefault(lit.lower(), cell)
            else:
                multi_token.append((lit.lower(), cell))
    return single_token, multi_token, phi_plants


def _scan_value_for_hits(
    text: str,
    single_token: dict[str, dict[str, Any]],
    multi_token: list[tuple[str, dict[str, Any]]],
) -> list[tuple[dict[str, Any], str]]:
    """Every ``(owning_cell, matched_literal_lowercased)`` pair found in
    ``text``, using the exact tokenize-then-lookup / substring-contains
    tests this module has always used per export cell."""
    if not text:
        return []
    lower = text.lower()
    found: list[tuple[dict[str, Any], str]] = []
    cell_tokens = set(t for t in _TOKEN_SPLIT.split(lower) if t)
    for tok in cell_tokens:
        owner = single_token.get(tok)
        if owner is not None:
            found.append((owner, tok))
    for lit_lower, owner in multi_token:
        if lit_lower in lower:
            found.append((owner, lit_lower))
    return found


def scan_exports_for_leaks(
    ground_truth: dict[str, Any],
    export_paths: dict[str, str],
    file_name_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Scan every export for any planted PHI literal reaching it verbatim.

    Leak literals are partitioned once via ``_partition_leak_literals``:
    single-token literals go into a ``set[str]`` and are matched via
    per-cell tokenization + set lookup (O(total tokens)); the few hundred
    multi-token literals (names, note fragments) are matched with ``in``
    against the whole cell text. Both tests are case-insensitive.
    """
    _ = file_name_map  # leak literals are checked against every export, not just the origin file
    planted = ground_truth.get("planted") or []
    single_token, multi_token, phi_plants = _partition_leak_literals(planted)

    hits: list[dict[str, Any]] = []
    scanned = 0
    for export_file, path in (export_paths or {}).items():
        rows = _read_export_rows(path)
        if not rows:
            continue
        scanned += 1
        for row_idx, row in enumerate(rows, start=1):
            for cell_text in row:
                for owner, sample in _scan_value_for_hits(cell_text, single_token, multi_token):
                    hits.append(_leak_hit(owner, export_file, row_idx, sample))

    hit_count = len(hits)
    return {
        "status": "leaked" if hit_count else "clean",
        "hit_count": hit_count,
        "phi_plants": phi_plants,
        "leak_rate": round(hit_count / phi_plants, 4) if phi_plants else 0.0,
        "scanned_exports": scanned,
        "hits": hits,
    }


def _leak_hit(cell: dict[str, Any], export_file: str, row: int, sample: str) -> dict[str, Any]:
    return {
        "plant_id": cell.get("plant_id", ""),
        "tier": cell.get("tier", ""),
        "file": cell.get("file_name", ""),
        "column": cell.get("column", ""),
        "hipaa_category": cell.get("hipaa_category", ""),
        "edge_case_tag": cell.get("edge_case_tag", ""),
        "sample": mask(sample),
        "export_file": export_file,
        "row": row,
    }


def _leak_hit_for_surface(cell: dict[str, Any], surface: str, location: str, sample: str) -> dict[str, Any]:
    """Same core fields and masking discipline as ``_leak_hit`` (never the
    raw matched value, only ``mask(sample)``), adapted for a non-export
    run surface: ``surface``/``location`` replace ``export_file``/``row``.
    ``location`` must never itself be (or contain) the matched value --
    callers pass an id/collection/field descriptor, never the free-text
    field that produced the hit."""
    return {
        "plant_id": cell.get("plant_id", ""),
        "tier": cell.get("tier", ""),
        "file": cell.get("file_name", ""),
        "column": cell.get("column", ""),
        "hipaa_category": cell.get("hipaa_category", ""),
        "edge_case_tag": cell.get("edge_case_tag", ""),
        "sample": mask(sample),
        "surface": surface,
        "location": location,
    }


def _walk_strings(value: Any) -> Iterator[str]:
    """Recursively yield every string leaf in a possibly-nested
    dict/list/tuple document -- used to scan a whole Mongo document (or
    sub-document) without hardcoding its field names."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _walk_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _walk_strings(v)


def _scan_zip_metadata(
    zip_path: str,
    single_token: dict[str, dict[str, Any]],
    multi_token: list[tuple[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Scan a ZIP archive's own metadata -- entry filenames and the
    archive-level and per-entry comments -- for a planted literal. Distinct
    from ``scan_exports_for_leaks``, which scans entry CONTENTS, never
    metadata. Never raises on a missing/unreadable/corrupt archive."""
    hits: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            archive_comment = (zf.comment or b"").decode("utf-8", errors="replace")
            for owner, sample in _scan_value_for_hits(archive_comment, single_token, multi_token):
                hits.append(_leak_hit_for_surface(owner, "zip_metadata", "archive_comment", sample))
            for info in zf.infolist():
                for owner, sample in _scan_value_for_hits(info.filename, single_token, multi_token):
                    hits.append(_leak_hit_for_surface(owner, "zip_metadata", "zip_entry_filename", sample))
                entry_comment = (info.comment or b"").decode("utf-8", errors="replace") if info.comment else ""
                for owner, sample in _scan_value_for_hits(entry_comment, single_token, multi_token):
                    hits.append(_leak_hit_for_surface(owner, "zip_metadata", "zip_entry_comment", sample))
    except (OSError, zipfile.BadZipFile):
        return []
    return hits


async def scan_run_surfaces_for_leaks(
    store: "ControlStore",
    ground_truth: dict[str, Any],
    *,
    run_id: str,
    session_id: str = "",
    zip_path: str | None = None,
) -> dict[str, Any]:
    """Scan the section-72 forbidden surfaces ``scan_exports_for_leaks``
    does not cover for any planted PHI literal reaching them verbatim.
    Reuses the exact same planted-literal set (``_partition_leak_literals``)
    and matching logic (``_scan_value_for_hits``) as the export scanner,
    and the same masking discipline (``_leak_hit_for_surface``: never the
    raw matched value, only ``mask(sample)``).

    Covers, per spec section 72:

    * ``trace_events`` (``phase_events.py`` collection ``trace_events``),
      including ``status_text`` -- scanned via a full recursive walk of
      every persisted field (``payload``, ``status_text``,
      ``retry_category``, ``sanitized_rationale``, ...), because
      ``trace_sanitizer``'s write-time scrub is signature-based (PHI/PII
      detectors, secrets), not literal-substring-aware, and would not
      necessarily catch an arbitrary planted canary token.
    * ``workflow_runs.opaque_map`` -- values are Fernet-encrypted at rest
      (``control/opaque.py``); decrypted with ``decrypt_opaque_value``
      before scanning, since ciphertext never substring-matches plaintext.
    * the legacy ``agent_log`` collection (pre-Phase-7 write path;
      ``control/migrate.py`` still indexes and migrates it) -- scoped by
      ``session_id`` since legacy rows predate ``run_id``.
    * HandoffEnvelope payloads -- ``control/handoff.py``'s
      ``HandoffGateway.handoff`` explicitly never persists
      ``HandoffEnvelope.payload`` ("the handoff's own payload never enters
      the trace, allowed or not" -- read, not edited, per this wave's
      scope). The only place a leak on this surface could physically
      manifest today is therefore the ``trace_events`` row a handoff
      attempt itself produces; hits on such a row (``phase == "handoff"``)
      are labelled ``surface="handoff_envelope_payload"`` rather than
      folded anonymously into the general ``trace_events`` count, so a
      regression that started copying the envelope's payload into trace
      would be attributed correctly.
    * the learning store (``learning_proposals``/``learning_evaluations``/
      ``learning_activations``) -- process-global (D16: no ``run_id``
      field on any of the three), so every document is scanned.
    * research queries (``web_cache``, keyed by topic+jurisdiction, also
      process-global) -- ``topic`` is the query text itself.
    * errors (``work_items.error_category``) -- scoped by ``run_id``.
    * ZIP metadata (filenames and comments), via ``_scan_zip_metadata``,
      when ``zip_path`` is supplied.
    """
    from phi_core.crypto import KeyRotated, decrypt_opaque_value

    planted = ground_truth.get("planted") or []
    single_token, multi_token, _phi_plants = _partition_leak_literals(planted)
    hits: list[dict[str, Any]] = []
    surfaces_scanned: list[str] = []

    def _record(value: Any, surface: str, location: str) -> None:
        for text in _walk_strings(value):
            for owner, sample in _scan_value_for_hits(text, single_token, multi_token):
                hits.append(_leak_hit_for_surface(owner, surface, location, sample))

    # trace_events (status_text included via the recursive walk), plus
    # HandoffEnvelope payloads folded in by phase -- see docstring.
    trace_docs = await store.find_many("trace_events", {"run_id": run_id})
    surfaces_scanned.append("trace_events")
    for doc in trace_docs:
        surface = "handoff_envelope_payload" if doc.get("phase") == "handoff" else "trace_events"
        _record(doc, surface, str(doc.get("event_id", "")))

    # workflow_runs.opaque_map
    run_doc = await store.get_one("workflow_runs", {"run_id": run_id})
    surfaces_scanned.append("workflow_runs.opaque_map")
    if run_doc:
        for encrypted in (run_doc.get("opaque_map") or {}).values():
            try:
                plaintext = decrypt_opaque_value(encrypted)
            except KeyRotated:
                plaintext = encrypted
            _record(plaintext, "workflow_runs.opaque_map", run_id)

    # agent_log (legacy)
    if session_id:
        agent_log_docs = await store.find_many("agent_log", {"session_id": session_id})
        surfaces_scanned.append("agent_log")
        for doc in agent_log_docs:
            _record(doc, "agent_log", str(doc.get("session_id", "")))

    # learning store
    for collection, id_field in (
        ("learning_proposals", "proposal_id"),
        ("learning_evaluations", "evaluation_id"),
        ("learning_activations", "activation_id"),
    ):
        docs = await store.find_many(collection, {})
        surfaces_scanned.append(collection)
        for doc in docs:
            _record(doc, "learning_store", str(doc.get(id_field, "")))

    # research queries
    cache_docs = await store.find_many("web_cache", {})
    surfaces_scanned.append("web_cache")
    for doc in cache_docs:
        _record(doc, "research_queries", str(doc.get("jurisdiction", "")))

    # errors
    work_items = await store.find_many("work_items", {"run_id": run_id})
    surfaces_scanned.append("work_items.error_category")
    for doc in work_items:
        _record(doc.get("error_category", ""), "errors", str(doc.get("task_id", "")))

    # ZIP metadata
    if zip_path:
        surfaces_scanned.append("zip_metadata")
        hits.extend(_scan_zip_metadata(zip_path, single_token, multi_token))

    hit_count = len(hits)
    return {
        "status": "leaked" if hit_count else "clean",
        "hit_count": hit_count,
        "surfaces_scanned": surfaces_scanned,
        "hits": hits,
    }



def _resolve_export_key(file_name: str, export_paths: dict[str, str],
                         file_name_map: dict[str, str]) -> str | None:
    if file_name in export_paths:
        return file_name
    mapped = file_name_map.get(file_name)
    if mapped and mapped in export_paths:
        return mapped
    return None


def _check_expectation(actual: str, expectation: dict[str, Any], cell: dict[str, Any],
                        link_maps: dict[str, dict[str, set]]) -> bool:
    stripped = actual.rstrip()
    if stripped.startswith("'"):
        # 4.18: the executor prefixes formula-shaped values with a leading
        # apostrophe before writing them to disk (CSV/XLSX formula-injection
        # neutralisation). Compare against the pre-escape value so a planted
        # 'keep' cell whose value happens to start with =/+/-/@ still scores
        # as preserved.
        stripped = stripped[1:]
    kind = expectation.get("kind")
    if kind == "literal":
        return stripped == expectation.get("literal", "")
    if kind == "regex":
        pattern = expectation.get("pattern", "")
        ok = bool(re.fullmatch(pattern, stripped)) if pattern else False
        link_group = expectation.get("link_group", "")
        if ok and link_group:
            lm = link_maps.setdefault(link_group, {})
            lm.setdefault(cell.get("value", ""), set()).add(stripped)
        return ok
    if kind == "human_review":
        # A deferral is either still pending (offline replay; unresolved
        # online session) or was RESOLVED before export. Resolution can
        # only go two ways (see campaign.run_online): drop (PHI removed,
        # actual == "") or keep-verbatim (only correct for a genuinely
        # non-PHI / NONE-category column).
        if stripped == expectation.get("literal", ""):
            return True
        if stripped == "":
            return True
        is_none_cat = cell.get("hipaa_category") in ("", "NONE")
        return is_none_cat and stripped == (cell.get("value", "") or "").rstrip()
    if kind == "text_scrub":
        must_not = expectation.get("must_not_contain") or []
        must = expectation.get("must_contain") or []
        return (all(m not in stripped for m in must_not)
                and all((m in stripped) for m in must if m))
    return False


def _sample_for(expectation: dict[str, Any], cell: dict[str, Any]) -> str:
    return expectation.get("literal") or expectation.get("pattern") or cell.get("value", "")


def score_cells(
    ground_truth: dict[str, Any],
    export_paths: dict[str, str],
    file_name_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Walk the export in row order alongside ``planted`` entries and
    compare each cell against its ``ExportExpectation``.

    A cell whose expectation has ``survives_verbatim=True`` and does not
    match counts in ``utility``, not in ``transform``. Everything else
    counts in ``transform``. A missing export file, or a planted cell with
    no matching export row, counts as ONE ``transform`` violation
    regardless of ``survives_verbatim`` -- a structural export failure is
    never scored as a utility loss, and it must never read as clean.
    """
    export_paths = export_paths or {}
    file_name_map = file_name_map or {}
    planted = ground_truth.get("planted") or []

    file_rows_cache: dict[str, list[list[str]]] = {}

    def get_rows(export_key: str) -> list[list[str]]:
        if export_key not in file_rows_cache:
            file_rows_cache[export_key] = _read_export_rows(export_paths.get(export_key, ""))
        return file_rows_cache[export_key]

    transform_violations: list[dict[str, Any]] = []
    utility_losses: list[dict[str, Any]] = []
    conformant = 0
    preserved = 0
    link_maps: dict[str, dict[str, set]] = {}

    for cell in planted:
        expectation = cell.get("expectation")
        if expectation is None:
            continue
        file_name = cell.get("file_name", "")
        col = cell.get("column", "")
        row_no = cell.get("row", 0)
        export_key = _resolve_export_key(file_name, export_paths, file_name_map)

        actual: str | None = None
        reason: str | None = None
        if export_key is None:
            reason = "export unavailable"
        else:
            rows = get_rows(export_key)
            if not rows:
                reason = "export unavailable"
            else:
                headers = rows[0]
                col_idx = headers.index(col) if col in headers else -1
                idx = row_no - 1
                if col_idx < 0 or not (0 <= idx < len(rows)):
                    reason = "export row missing"
                else:
                    data_row = rows[idx]
                    if col_idx < len(data_row):
                        actual = data_row[col_idx]
                    else:
                        reason = "export row missing"

        if reason is not None:
            transform_violations.append({
                "plant_id": cell.get("plant_id", ""), "tier": cell.get("tier", ""),
                "file": file_name, "column": col,
                "expected_kind": expectation.get("kind", ""),
                "expected_sample": mask(_sample_for(expectation, cell)),
                "actual_sample": "",
                "reason": reason,
            })
            continue

        ok = _check_expectation(actual, expectation, cell, link_maps)
        survives = bool(expectation.get("survives_verbatim"))
        if survives:
            if ok:
                preserved += 1
            else:
                utility_losses.append({
                    "plant_id": cell.get("plant_id", ""), "tier": cell.get("tier", ""),
                    "file": file_name, "column": col,
                    "expected_sample": mask(_sample_for(expectation, cell)),
                    "actual_sample": mask(actual or ""),
                    "reason": "value changed",
                })
        else:
            if ok:
                conformant += 1
            else:
                transform_violations.append({
                    "plant_id": cell.get("plant_id", ""), "tier": cell.get("tier", ""),
                    "file": file_name, "column": col,
                    "expected_kind": expectation.get("kind", ""),
                    "expected_sample": mask(_sample_for(expectation, cell)),
                    "actual_sample": mask(actual or ""),
                    "reason": "value did not match expectation",
                })

    for group, src_map in link_maps.items():
        token_to_srcs: dict[str, set] = {}
        for src, toks in src_map.items():
            if len(toks) > 1:
                transform_violations.append({
                    "plant_id": "", "tier": "", "file": "", "column": group,
                    "expected_kind": "regex", "expected_sample": "", "actual_sample": "",
                    "reason": f"link_group {group!r}: one source value produced multiple tokens",
                })
            for t in toks:
                token_to_srcs.setdefault(t, set()).add(src)
        for _tok, srcs in token_to_srcs.items():
            if len(srcs) > 1:
                transform_violations.append({
                    "plant_id": "", "tier": "", "file": "", "column": group,
                    "expected_kind": "regex", "expected_sample": "", "actual_sample": "",
                    "reason": f"link_group {group!r}: two source values collided on one token",
                })

    nonconformant = len(transform_violations)
    total = conformant + nonconformant
    destroyed = len(utility_losses)
    utility_total = preserved + destroyed

    return {
        "conformant": conformant, "nonconformant": nonconformant,
        "rate": round(conformant / total, 4) if total else 1.0,
        "violations": transform_violations,
        "preserved": preserved, "destroyed": destroyed,
        "utility_rate": round(preserved / utility_total, 4) if utility_total else 1.0,
        "losses": utility_losses,
    }


def _classify_regulation_keys(cell: dict[str, Any]) -> list[str]:
    """Same rules ``tiers.coverage`` uses: the hipaa_category letter for A
    through R; C90 when the cell's semantics put the subject over 89; B000
    when the planted ZIP's three-digit prefix is in the deny set; QI when
    edge_case_tag == 'quasi_identifier'; 42CFR2 when sensitivity_class ==
    '42cfr2'."""
    keys: list[str] = []
    cat = cell.get("hipaa_category", "")
    if cat in _HIPAA_LETTERS:
        keys.append(cat)
    expectation = cell.get("expectation") or {}
    if cell.get("expected_action") == "cap_age_90" and expectation.get("literal") == "90+":
        keys.append("C90")
    if cell.get("expected_action") == "zip3_truncate" and expectation.get("literal") == "000":
        keys.append("B000")
    if cell.get("edge_case_tag") == "quasi_identifier":
        keys.append("QI")
    if cell.get("sensitivity_class") == "42cfr2":
        keys.append("42CFR2")
    return keys


def verify(
    ground_truth: dict[str, Any],
    decisions: list[dict[str, Any]],
    file_name_map: dict[str, str] | None = None,
    guard_report: dict[str, Any] | None = None,
    export_paths: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Compare a corpus ground-truth against the pipeline's actual outputs.

    ``file_name_map`` translates the ORIGINAL corpus file name to the
    pipeline's ``file_id`` so ``(file_id, column)`` lookups resolve.

    ``guard_report`` is surfaced in the report but not used for scoring so
    leaks the guard missed are still caught by the Judge decision.

    ``export_paths``, when supplied, unlocks the ``leak`` / ``transform`` /
    ``utility`` / ``regulation`` blocks, which score the actual export
    bytes rather than only the Judge's per-column decision.
    """
    planted = ground_truth.get("planted") or []

    # ------------------------------------------------------------------
    # Decision index: (file_key, column) -> decision
    # ------------------------------------------------------------------
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for d in decisions:
        fid = d.get("file_id") or d.get("file_name") or ""
        col = d.get("column") or ""
        by_key[(fid, col)] = d

    if file_name_map:
        name_by_id = {v: k for k, v in file_name_map.items()}
        for d in list(decisions):
            fid = d.get("file_id") or ""
            orig = name_by_id.get(fid)
            if orig:
                by_key[(orig, d.get("column", ""))] = d

    per_cat: dict[str, CategoryScore] = {}
    misses_false_neg: list[Miss] = []
    misses_false_pos: list[Miss] = []
    deferred_decidable: list[Miss] = []
    matched = 0
    scored_pairs: set[tuple[str, str]] = set()
    total_planted_cells = 0
    excluded_count = 0

    for cell in planted:
        col_name = cell["column"]
        file_name = cell["file_name"]
        cat = cell["hipaa_category"]
        expected = cell["expected_action"]
        is_qi = cell.get("edge_case_tag") == "quasi_identifier"

        pair_key = (file_name, col_name)
        if pair_key in scored_pairs:
            continue
        scored_pairs.add(pair_key)
        if is_qi:
            excluded_count += 1
        else:
            total_planted_cells += 1

        dec = by_key.get(pair_key)
        if not dec and file_name_map:
            dec = by_key.get((file_name_map.get(file_name, ""), col_name))
        actual = dec.get("action") if dec else "MISSING"

        # Deferral: decidable case punted to human_review. Quasi-identifier
        # columns are excluded -- human_review IS the correct answer there,
        # so they fall through to ordinary correctness scoring instead.
        if actual == "human_review" and not is_qi:
            deferred_decidable.append(Miss(
                file=file_name, column=col_name,
                expected_action=expected, actual_action=actual,
                hipaa_category=cat,
                edge_case_tag=cell.get("edge_case_tag", ""),
                reason="Judge deferred a decidable case",
            ))
            continue

        scr = per_cat.setdefault(cat, CategoryScore(category=cat))
        expected_is_phi = expected in _PHI_ACTIONS
        actual_is_phi = actual in _PHI_ACTIONS

        if expected_is_phi and actual_is_phi:
            scr.tp += 1
            matched += 1
        elif expected_is_phi and not actual_is_phi:
            scr.fn += 1
            misses_false_neg.append(Miss(
                file=file_name, column=col_name,
                expected_action=expected, actual_action=str(actual),
                hipaa_category=cat,
                edge_case_tag=cell.get("edge_case_tag", ""),
                reason="PHI leaked: expected transform, got keep-equivalent",
            ))
        elif not expected_is_phi and actual_is_phi:
            scr.fp += 1
            misses_false_pos.append(Miss(
                file=file_name, column=col_name,
                expected_action=expected, actual_action=str(actual),
                hipaa_category=cat,
                edge_case_tag=cell.get("edge_case_tag", ""),
                reason="Clinical data over-blocked",
            ))
        else:
            scr.tn += 1
            matched += 1

    # ---- aggregate ---------------------------------------------------
    total_tp = sum(s.tp for s in per_cat.values())
    total_fp = sum(s.fp for s in per_cat.values())
    total_fn = sum(s.fn for s in per_cat.values())
    total_tn = sum(s.tn for s in per_cat.values())
    total = total_tp + total_fp + total_fn + total_tn
    overall_p = total_tp / (total_tp + total_fp) if (total_tp + total_fp) else 1.0
    overall_r = total_tp / (total_tp + total_fn) if (total_tp + total_fn) else 1.0
    overall_f1 = 2 * overall_p * overall_r / (overall_p + overall_r) if (overall_p + overall_r) else 0.0
    accuracy = (total_tp + total_tn) / total if total else 1.0

    deferral_rate = (
        len(deferred_decidable) / total_planted_cells
        if total_planted_cells else 0.0
    )

    report: dict[str, Any] = {
        "scenario_id": ground_truth.get("scenario_id"),
        "jurisdiction": ground_truth.get("jurisdiction"),
        "edge_case_tags": ground_truth.get("edge_case_tags", []),
        "tier": ground_truth.get("tier", ""),
        "correctness": {
            "overall_precision": round(overall_p, 4),
            "overall_recall": round(overall_r, 4),
            "overall_f1": round(overall_f1, 4),
            "overall_accuracy": round(accuracy, 4),
            "per_category": [s.to_dict() for s in per_cat.values()],
            "false_positives": [m.__dict__ for m in misses_false_pos],
            "false_negatives": [m.__dict__ for m in misses_false_neg],
        },
        "deferral": {
            "rate": round(deferral_rate, 4),
            "count": len(deferred_decidable),
            "excluded_count": excluded_count,
            "cells": [m.__dict__ for m in deferred_decidable],
        },
        "summary": {
            "planted_columns": total_planted_cells,
            "matched": matched,
            "tp": total_tp, "fp": total_fp, "fn": total_fn, "tn": total_tn,
        },
        "guard_status": (guard_report or {}).get("status"),
    }

    if export_paths:
        leak = scan_exports_for_leaks(ground_truth, export_paths, file_name_map)
        cells_score = score_cells(ground_truth, export_paths, file_name_map)
        transform = {k: cells_score[k] for k in ("conformant", "nonconformant", "rate", "violations")}
        utility = {
            "preserved": cells_score["preserved"], "destroyed": cells_score["destroyed"],
            "rate": cells_score["utility_rate"], "losses": cells_score["losses"],
        }

        leaked_ids = {h["plant_id"] for h in leak["hits"] if h.get("plant_id")}
        violated_ids = {v["plant_id"] for v in transform["violations"] if v.get("plant_id")}

        planted_by_key: dict[str, set[str]] = {k: set() for k in REQUIRED_VIOLATIONS}
        for cell in planted:
            pid = cell.get("plant_id", "")
            if not pid:
                continue
            for key in _classify_regulation_keys(cell):
                planted_by_key[key].add(pid)

        regulation = {
            "planted": {k: len(v) for k, v in planted_by_key.items()},
            "neutralised": {k: len(v - leaked_ids - violated_ids) for k, v in planted_by_key.items()},
            "leaked": {k: len(v & leaked_ids) for k, v in planted_by_key.items()},
            "unplanted": sorted(k for k, v in planted_by_key.items() if not v),
        }

        tier = ground_truth.get("tier", "")
        report.update({
            "leak": leak,
            "transform": transform,
            "utility": utility,
            "regulation": regulation,
            "per_tier": {
                tier: {
                    "leak": leak, "transform": transform, "utility": utility,
                    "correctness": report["correctness"],
                }
            } if tier else {},
        })

    return report
