"""Study data element specialist agents.

Lexicon    - dictionary / mapping (xlsx / csv codebooks) specialist
Schema     - dataset (CSV/XLSX headers only, never rows)
Instrument - forms (PDF collection instruments)

Each specialist reads its assigned artifact and produces a normalised
per-column knowledge record consumed by Judge.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

from ..control import limits
from ..control.opaque import OpaqueMap
from ..control.records import StudyKnowledgePackage
from ..control.sandbox import create_sandbox, destroy_sandbox
from ..control.source_projection import classify_header, source_projection
from ..file_readers import read_docx, read_pdf, read_pdf_form_fields, read_table_rows
from .base import Agent
from .codegen import CodeGenerationExhausted, generate_with_retry
from .extract_model import CardKind, ExtractedSchema, card_kind

_SPAN_COUNT_RE = re.compile(r"^(\d+) PHI detector span")


def _phi_span_count(reasons: list[str]) -> int:
    """Approximate the number of PHI spans a ``source_projection`` result
    redacted, parsed from its ``reasons`` text, for the same diagnostic
    ``scrub_count`` metric Lexicon/Instrument already reported before
    Wave R-c routed their extracted text through ``source_projection``
    instead of a bare ``scrub_for_prompt`` call."""
    total = 0
    for reason in reasons:
        match = _SPAN_COUNT_RE.match(reason)
        if match:
            total += int(match.group(1))
    return total


class UncertainHeaderCeilingExceeded(Exception):
    """Raised when a single ``Schema`` run leaves more than
    ``limits.MAX_UNCERTAIN_HEADERS_PER_RUN`` headers with an
    ``uncertain`` disposition (v3 section 7): past this ceiling, an
    unresolved, ambiguous-header population is itself evidence the
    review process is not keeping up for this run, not something one
    more retry fixes."""

    failure_class = "HEADER_SENSITIVE_CONTENT"

    def __init__(self, count: int, limit: int) -> None:
        self.count = count
        self.limit = limit
        super().__init__(f"{count} uncertain headers exceeds the per-run ceiling of {limit}")


def _opaque_file_id(file_record: dict[str, Any]) -> str:
    return str(file_record.get("opaque_file_id") or file_record.get("file_id") or "")

def assemble_study_knowledge_package(
    *,
    run_id: str,
    datasets: list[str],
    schema: dict[str, Any],
    lexicon: dict[str, Any] | None = None,
    instrument: dict[str, Any] | None = None,
) -> StudyKnowledgePackage:
    """Assemble Lexicon/Schema/Instrument outputs into one versioned
    ``StudyKnowledgePackage`` (docs #28) instead of concatenating
    specialist prose into a single prompt.

    ``schema``, ``lexicon``, and ``instrument`` are the raw ``run()`` return
    dicts each specialist already produces: ``{"columns": [...]}`` for
    Schema and Lexicon, ``{"fields": [...]}`` for Instrument. Schema is
    mandatory (docs #24) and is the sole source of the package's
    per-column identity; Lexicon and Instrument contribute their findings
    only when their source material (a dictionary or forms) actually
    exists, mirroring how the orchestrator skips a specialist with no
    matching input files. The flat ``columns`` list is the ordered,
    de-duplicated set of Schema's column names; ``schema_findings`` keeps
    the per-file detail (``name`` + ``_file_id``) intact.

    Versioning follows the record's own convention: every assembly mints a
    fresh ``package_id`` (the record carries no monotonic ``version``
    integer; identity plus the ``superseded_by`` supersede-chain is the
    versioning mechanism, and ``created_at`` orders successive instances).
    A caller holding a prior package marks it ``superseded_by`` the newer
    package's id; this function never mutates an existing package."""
    schema_findings = list(schema.get("columns") or [])
    lexicon_findings = list((lexicon or {}).get("columns") or [])
    instrument_findings = list((instrument or {}).get("fields") or [])

    columns: list[str] = []
    seen: set[str] = set()
    for finding in schema_findings:
        name = finding.get("name") if isinstance(finding, dict) else None
        if isinstance(name, str) and name and name not in seen:
            seen.add(name)
            columns.append(name)

    return StudyKnowledgePackage(
        run_id=run_id,
        datasets=list(datasets),
        columns=columns,
        schema_findings=schema_findings,
        lexicon_findings=lexicon_findings,
        instrument_findings=instrument_findings,
    )


class Lexicon(Agent):
    NAME = "Lexicon"
    # Lexicon never sees a whole dictionary in one call. Row extraction is
    # fully deterministic (see ``_dict_rows`` below) and happens for every
    # file before any LLM call is made; the LLM is only ever handed an
    # already-indexed batch of rows and asked to summarise them, or a single
    # already-indexed row and asked a grounded question about it. An LLM
    # outage or a short reply can leave a row's gist blank -- it can never
    # drop the row itself.
    PROMPT = (
        "You are Lexicon, a specialist in data dictionaries and code maps for clinical study "
        "datasets. You are handed already-extracted, already-scrubbed dictionary rows -- one "
        "row per documented column -- and asked either to summarise a batch of rows or to "
        "answer a grounded question about one specific row. Identifiers arrive pre-redacted "
        "as [REDACTED:<category>:<entity>] tokens; treat such a token as evidence the row "
        "documents an identifier, never as a literal value. Always follow the JSON schema "
        "given in the prompt exactly, and answer only from the row(s) you are given: you "
        "report what the dictionary says, you never decide whether a column is PHI. Never "
        "invent a row you were not given."
    )

    _GIST_CHUNK_SIZE = 20

    async def run(self, dict_files: list[dict[str, Any]]) -> dict[str, Any]:
        self._notes: dict[str, dict[str, Any]] = {}
        self.scrub_count = 0
        # Pass 1: parse and index every row of every file. No LLM call
        # happens until this pass is complete for the whole batch, so an
        # LLM can never silently drop a documented column.
        per_file: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        for f in dict_files:
            path = Path(f["stored_path"])
            try:
                header, rows = _dict_rows(path)
            except Exception as exc:
                # docs #27: a malformed or unreadable dictionary file
                # degrades Lexicon to an empty index for that file rather
                # than crashing the whole run (and, via the orchestrator's
                # structured-only gather, discarding Schema/Instrument's
                # successful output alongside it). Mirrors Schema's
                # log-and-skip of a file with no headers.
                await self._log(f"lexicon.unreadable:{f['file_id']}", "info",
                                {"file": _opaque_file_id(f),
                                 "error": "unreadable_dictionary_file",
                                 "kind": type(exc).__name__})
                per_file.append((f, []))
                continue
            if not header:
                await self._log(f"lexicon.empty:{f['file_id']}", "info",
                                {"file": _opaque_file_id(f)})
                per_file.append((f, []))
                continue
            name_idx = _name_column_index(header)
            file_entries: list[dict[str, Any]] = []
            blank_row_indices: list[int] = []
            for row_index, row in enumerate(rows):
                if name_idx >= len(row) or not (row[name_idx] or "").strip():
                    blank_row_indices.append(row_index)
                    continue
                name = row[name_idx].strip()
                raw_row = ", ".join(
                    f"{h.strip()}: {v}" for h, v in zip(header, row, strict=True) if (h or "").strip()
                )
                # Dictionary rows are short label-like phrases with little
                # surrounding narrative context -- the same case
                # scrub_for_prompt's docstring documents for form text.
                # Presidio's NER false-positives heavily here (flagged
                # "Patient", "US", "years", "sex_at_birth", "Hispanic"/
                # "Latino" as PHI in the TB study dictionary, none of which
                # are); rule-based regex still catches any genuine
                # identifier value typed into a description. Routed
                # through `source_projection` (Wave R-c, v3 section 22)
                # rather than a bare `scrub_for_prompt` call: a
                # credential-shape or residual-PHI row is fully blocked
                # (`raw_row` becomes empty) instead of merely redacted.
                projection = source_projection(
                    content_type="dictionary", raw_text=raw_row, run_id=self.ctx.run_id,
                )
                self.scrub_count += _phi_span_count(projection.reasons)
                if projection.blocked:
                    await self._log(
                        "lexicon.row_blocked", "info",
                        {"file_id": f["file_id"], "name": name, "disposition": projection.disposition},
                    )
                scrubbed_row = projection.projected_text
                entry = {
                    "name": name,
                    "raw_row": scrubbed_row,
                    "gist": "",
                    "phi_flag_hint": None,
                    "clinical_utility": "low",
                }
                file_entries.append(entry)
                self._notes[name.lower()] = entry
            if blank_row_indices:
                # One aggregate event per file, not one per row (a
                # dictionary can carry up to Task 5's 5000-row cap) --
                # still names every skipped row, just in one document.
                await self._log("lexicon.blank_name", "info",
                                {"file_id": f["file_id"], "reason": "blank_name",
                                 "count": len(blank_row_indices),
                                 "row_indices": blank_row_indices})
            # Auditable raw-vs-indexed count: any gap between the two is
            # accounted for entirely by the lexicon.blank_name event
            # above, never a silent drop.
            await self._log(f"lexicon.parsed:{f['file_id']}", "info",
                            {"file": _opaque_file_id(f), "raw_row_count": len(rows),
                             "indexed_row_count": len(file_entries)})
            per_file.append((f, file_entries))

        # Pass 2: fill in a gist per row, chunked, now that every row is
        # already indexed in self._notes.
        entries: list[dict[str, Any]] = []
        for f, file_entries in per_file:
            if file_entries:
                await self._fill_gists(file_entries, f)
            entries.extend(file_entries)

        columns = [
            {
                "name": e["name"],
                "description": e["gist"],
                "phi_flag_hint": e["phi_flag_hint"],
                "clinical_utility": e["clinical_utility"],
                "notes": "",
            }
            for e in entries
        ]
        return {"columns": columns, "notes": ""}

    async def _fill_gists(self, entries: list[dict[str, Any]], f: dict[str, Any]) -> None:
        """One call_json per chunk of already-indexed rows. A short or
        empty reply can only leave a chunk's gists blank."""
        for start in range(0, len(entries), self._GIST_CHUNK_SIZE):
            chunk = entries[start:start + self._GIST_CHUNK_SIZE]
            batch = [{"name": e["name"], "row": e["raw_row"]} for e in chunk]
            reply = await self.call_json(
                f"Filename: {_opaque_file_id(f)}\n"
                f"Dictionary rows in this batch:\n{batch}\n"
                'Respond with JSON only: {"gists": [{"name": str, "gist": str, '
                '"phi_flag_hint": bool|null, "clinical_utility": "low|medium|high"}]}, one '
                "entry per row above, using the same name given for that row.",
                phase=f"lexicon.gist:{f['file_id']}:{start}",
                default={"gists": []},
                expect_key="gists", min_items=len(chunk),
                status_text=f"Reading the dictionary file {_opaque_file_id(f)}",
            )
            by_name = {
                str(g["name"]).strip().lower(): g
                for g in reply.get("gists", [])
                if isinstance(g, dict) and g.get("name")
            }
            for e in chunk:
                g = by_name.get(e["name"].lower())
                if g is None:
                    await self._log("lexicon.gist_missing", "info", {"column": e["name"]})
                    continue
                e["gist"] = g.get("gist") or ""
                e["phi_flag_hint"] = g.get("phi_flag_hint")
                cu = g.get("clinical_utility")
                if cu in ("low", "medium", "high"):
                    e["clinical_utility"] = cu

    async def answer(self, column: str, assumption: str, reasoning: str) -> dict[str, Any]:
        """Grounded per-column question, answered only from that column's
        already-scrubbed dictionary row -- never by reopening the file,
        never by looking at another column's row."""
        note = self._notes.get((column or "").strip().lower())
        if note is None:
            return {
                "verdict": "not_in_dictionary",
                "explanation": (
                    f"'{column}' is not present in the dictionary -- this index is the "
                    "final list, nothing else exists"
                ),
                "citation": "",
            }
        reply = await self.call_json(
            f"Column: {note['name']}\n"
            f"Dictionary row (scrubbed): {note['raw_row']}\n"
            f"Caller's assumption about this column: {assumption}\n"
            f"Caller's reasoning: {reasoning}\n"
            'Respond with JSON only: {"verdict": "confirmed"|"corrected", "explanation": str, '
            '"citation": str}. Ground your answer only in the dictionary row above.',
            phase=f"lexicon.answer:{note['name']}",
            default={"verdict": "corrected", "explanation": "", "citation": ""},
            status_text=f"Checking the assumption about {note['name']} against the dictionary",
        )
        verdict = reply.get("verdict")
        return {
            "verdict": verdict if verdict in ("confirmed", "corrected") else "corrected",
            "explanation": reply.get("explanation", ""),
            "citation": reply.get("citation", ""),
        }


_DICT_COLUMN_NAME_RE = re.compile(
    r"^(variable|variable_name|column|column_name|field|field_name|name)$", re.I)


def _name_column_index(header: list[str]) -> int:
    """Locate the column-name cell in a dictionary header row; falls back
    to the first column when no header cell matches a recognised heading."""
    for i, cell in enumerate(header):
        if _DICT_COLUMN_NAME_RE.match((cell or "").strip()):
            return i
    return 0


def _docx_dictionary_rows(path: Path) -> tuple[list[str], list[list[str]]]:
    """(header, rows) for a .docx dictionary, parsed from the first table
    ``_read_docx_tables`` extracts. Reuses that helper's CSV-shaped text
    rather than re-walking the XML, so a .docx dictionary is indexed the
    same row-first way as a .csv/.xlsx one."""
    text = _read_docx_tables(path)
    if not text:
        return [], []
    table_lines: list[str] = []
    in_first_table = False
    for line in text.splitlines():
        if line.startswith("# table "):
            if in_first_table:
                break  # only the first table documents columns
            in_first_table = True
            continue
        if line.startswith("#"):
            if in_first_table:
                break
            continue
        if in_first_table:
            table_lines.append(line)
    if not table_lines:
        return [], []
    parsed = [row for row in csv.reader(table_lines) if any(cell.strip() for cell in row)]
    if not parsed:
        return [], []
    return parsed[0], parsed[1:]


def _dict_rows(path: Path) -> tuple[list[str], list[list[str]]]:
    """Deterministic (header, rows) for one dictionary file: csv/tsv/xlsx
    via Task 5's ``read_table_rows``, .docx via the CSV-shaped text
    ``_read_docx_tables`` already produces. Every row this returns becomes
    a Lexicon index entry before any LLM call happens -- an LLM can no
    longer drop a documented column, only fail to describe one."""
    ext = path.suffix.lower()
    if ext in {".csv", ".tsv", ".xlsx"}:
        return read_table_rows(path)
    if ext == ".docx":
        return _docx_dictionary_rows(path)
    return [], []


class Schema(Agent):
    NAME = "Schema"
    PROMPT = (
        "You are Schema, a specialist that writes a small, self-contained Python module "
        "to extract a dataset's own structure -- never its content. You never see a real "
        "column name or a real cell value: you write code that discovers them at execution "
        "time, inside a network-denied sandbox.\n\n"
        "Write a module with exactly one top-level function, `def run():`. It must:\n"
        "1. Read the dataset mounted at the fixed path you are given, using pandas with "
        "dtype=str and keep_default_na=False (never a raw-string encoding guess).\n"
        "2. For every column, in the file's own left-to-right order, compute: its name "
        "exactly as it appears in the file, its 0-based position, the count of distinct "
        "non-null values, the count of null/empty values, and your best single-word guess "
        "at its type (string, integer, float, date, boolean, categorical, or unknown).\n"
        "3. Write a JSON object to the fixed output path you are given, shaped exactly as: "
        '{"columns": [{"name": str, "position": int, "distinct_count": int, '
        '"null_count": int, "inferred_type": str}], "row_count": int}\n'
        "4. Return the output filename (just the name, e.g. \"schema_out.json\") from run().\n\n"
        "Report every column exactly once. Never invent, omit, merge, or reorder a column. "
        "Never hardcode a value you have not read from the file at runtime -- every count "
        "must come from the pandas operations you write, computed when the module actually "
        "runs, not typed in by you."
    )

    # Excel/LibreOffice/Sheets-shaped file extensions this agent's generated
    # extraction module may be asked to read; kept in sync with the
    # container's own pandas capability (read_csv/read_excel), never a
    # superset of what `check_generated_code`'s import allowlist permits.
    _TABULAR_EXTENSIONS: frozenset[str] = frozenset({"csv", "tsv", "xlsx", "xls"})

    # Cap on the raw JSON text this agent will attempt to parse from a
    # generated module's own output artifact -- a defensive ceiling
    # against a runaway or malicious result file, independent of
    # ContainerRunner's own workspace-size ceiling (which bounds the
    # whole staging tree, not this one file).
    _MAX_ARTIFACT_CHARS = 2_000_000

    async def run(self, dataset_files: list[dict[str, Any]]) -> dict[str, Any]:
        """Extract every dataset file's headers and cardinality via
        generated code (step 10), then apply the unchanged deterministic
        header safety gate (below) to every name the generated code
        reports -- the gate never trusted, never bypassed, and never
        aware of how the names arrived. Schema itself never reads a
        dataset file directly; ``_extract_via_codegen`` is the only
        seam that touches ``stored_path``, and it always does so
        through the sandboxed/containerized boundary."""
        results: list[dict[str, Any]] = []
        self._headers: dict[str, list[str]] = {}
        self._stats: dict[tuple[str, str], dict[str, Any]] = {}
        # Wave R-c (v3 section 7 HEADER SAFETY GATE): a header carrying a
        # typed-in real value must never reach `results` (agent/LLM-facing)
        # under its literal text -- only the opaque token does. Falls back
        # to a local, unpersisted `OpaqueMap` when no store-backed
        # `ctx.opaque` was wired (e.g. a context built directly via
        # `control.testing.make_ctx`), so the security property (never
        # leak the literal) holds even outside the live pipeline.
        local_opaque = OpaqueMap(self.ctx.run_id, {})
        uncertain_count = 0
        file_notices: list[str] = []
        # A code-writing run needs a real sandbox for generate_with_retry's
        # own two data-touching checks (assert_no_dataset_literals,
        # assert_no_formula_injection_in_outputs). Reuses ctx.sandbox when
        # the pipeline attached one for this run; otherwise opens and
        # tears down its own -- the same fallback shape the pre-existing
        # `_read_dataset_headers_maybe_sandboxed` pattern in reasoning.py
        # already establishes for a make_ctx-built unit-test context that
        # never attaches one.
        owns_sandbox = self.ctx.sandbox is None
        sandbox = self.ctx.sandbox or create_sandbox(run_id=self.ctx.run_id)
        try:
            for f in dataset_files:
                file_id = f["file_id"]
                try:
                    artifact = await self._extract_via_codegen(f, sandbox)
                except CodeGenerationExhausted as exc:
                    await self._log(f"schema.error:{file_id}", "info", {
                        "error": "no headers provided", "file": _opaque_file_id(f),
                        "reason": "code_generation_exhausted",
                        "diagnostics": exc.diagnostics[:5],
                    })
                    continue
                except (ValueError, TypeError) as exc:
                    # The generated module ran and returned a file, but its
                    # content did not satisfy ExtractedSchema's strict
                    # contract (extra field, wrong type, duplicate name,
                    # out-of-order position, ...) or was not valid JSON at
                    # all. Fail closed for this file rather than coercing --
                    # exactly the same outward shape as codegen exhaustion,
                    # since the practical consequence (this file contributes
                    # zero columns this run) is identical either way.
                    await self._log(f"schema.error:{file_id}", "info", {
                        "error": "no headers provided", "file": _opaque_file_id(f),
                        "reason": "invalid_artifact", "detail": type(exc).__name__,
                    })
                    continue
                if artifact is None or not artifact.columns:
                    await self._log(f"schema.error:{file_id}", "info",
                                    {"error": "no headers provided", "file": _opaque_file_id(f)})
                    continue
                headers = [c.name for c in artifact.columns]
                projected_headers: list[str] = []
                tokenised_count = 0
                for header in headers:
                    disposition, reasons = classify_header(header)
                    if disposition == "safe":
                        projected_headers.append(header)
                        continue
                    if disposition == "uncertain":
                        uncertain_count += 1
                        if uncertain_count > limits.MAX_UNCERTAIN_HEADERS_PER_RUN:
                            raise UncertainHeaderCeilingExceeded(
                                uncertain_count, limits.MAX_UNCERTAIN_HEADERS_PER_RUN,
                            )
                    if self.ctx.opaque is not None:
                        token = await self.ctx.opaque.to_opaque("header", header)
                    else:
                        token = local_opaque.to_opaque("header", header)
                    projected_headers.append(token)
                    tokenised_count += 1
                    if disposition == "uncertain":
                        # Non-blocking review item: a recorded trace event,
                        # never a `HumanReviewRequest` (which pauses the run --
                        # the wrong tool for a disposition this system already
                        # treats identically to `sensitive` for the rest of
                        # this run). If no human ever resolves it, the
                        # disposition simply stays sensitive permanently: no
                        # code path anywhere reverses an opaque projection
                        # back to its literal header.
                        await self._log(
                            "schema.header_uncertain_review", "info",
                            {"file_id": file_id, "opaque_token": token, "reasons": reasons},
                        )
                self._headers[file_id] = [h.lower() for h in projected_headers]
                await self._log(f"schema.headers:{file_id}", "info", {"header_count": len(headers)})
                raw_to_projected = {raw.lower(): proj for raw, proj in zip(headers, projected_headers, strict=True)}
                for col in artifact.columns:
                    projected_name = raw_to_projected.get(col.name.lower(), col.name)
                    kind = card_kind(col.distinct_count, artifact.row_count)
                    # Disclosive-statistics suppression: a cardinality of
                    # exactly 1 (constant) or exactly the row count
                    # (unique) is reportable structure -- on a small
                    # dataset either fact can narrow a quasi-identifier
                    # to a single row. Report the categorical flag
                    # instead of the raw integer in exactly those two
                    # cases; the ordinary "some distinct values, some
                    # repeats" case keeps its real number.
                    stat_entry: dict[str, Any] = {"rows": artifact.row_count}
                    if kind is CardKind.normal:
                        stat_entry["distinct"] = col.distinct_count
                    else:
                        stat_entry["cardinality"] = kind.value
                    self._stats[(file_id, projected_name.lower())] = stat_entry
                await self._log(f"schema.cardinality:{file_id}", "info", {"columns": len(artifact.columns)})
                if tokenised_count:
                    file_notices.append(
                        f"{_opaque_file_id(f)}: {tokenised_count} of {len(headers)} header(s) "
                        "tokenised because they carried a real value or an ambiguous digit run."
                    )
                for h in projected_headers:
                    results.append({"name": h, "_file_id": file_id})
        finally:
            if owns_sandbox:
                destroy_sandbox(sandbox)
        return {"columns": results, "header_notice": "; ".join(file_notices)}

    @staticmethod
    def _dataset_ext(f: dict[str, Any]) -> str:
        path = Path(f["stored_path"])
        return (f.get("subtype") or path.suffix.lstrip(".")).lower()

    async def _extract_via_codegen(self, f: dict[str, Any], sandbox: Any) -> "ExtractedSchema | None":
        """Generate and run one extraction module for one dataset file.

        The prompt names only the opaque file id and the fixed container
        mount path -- never a raw header or row value. The generated
        source computes headers/cardinality itself, inside
        ``generate_with_retry``'s sandboxed/containerized boundary
        (``agents/codegen.py``, step 9); this method never opens
        ``stored_path`` itself. Raises :class:`CodeGenerationExhausted`
        on exhaustion (the caller escalates to the same fail-loud path
        as an empty extraction); raises :class:`ValueError` on a
        artifact that ran but failed :class:`ExtractedSchema`'s strict
        contract, or that exceeded ``_MAX_ARTIFACT_CHARS``.
        """
        ext = self._dataset_ext(f)
        mount_name = f"dataset.{ext}" if ext in self._TABULAR_EXTENSIONS else "dataset"
        opaque_id = _opaque_file_id(f)

        def build_prompt(previous_source: str | None, previous_diagnostics: list[str] | None) -> str:
            retry_note = ""
            if previous_diagnostics:
                retry_note = (
                    "\n\nYour previous attempt failed. Fix these exact problems, "
                    f"do not repeat them: {previous_diagnostics}"
                )
            return (
                f"The dataset is mounted read-only at /data/{mount_name}. "
                'Write the output JSON to /workspace/schema_out.json and return '
                'the string "schema_out.json" from run().' + retry_note
            )

        source, result = await generate_with_retry(
            self, build_prompt, phase=f"schema.extract:{opaque_id}",
            dataset_path=f["stored_path"], inputs={mount_name: f["stored_path"]},
            entrypoint="extract.py", declared_outputs=frozenset({"schema_out.json"}),
            sandbox=sandbox,
        )
        try:
            raw = (result.workspace_path / "schema_out.json").read_text(encoding="utf-8")
        finally:
            result.cleanup()
        if len(raw) > self._MAX_ARTIFACT_CHARS:
            raise ValueError(f"extraction artifact exceeds the {self._MAX_ARTIFACT_CHARS}-char cap")
        data = json.loads(raw)
        return ExtractedSchema.model_validate(data)

    def verify(self, column: str, file_id: str | None = None) -> dict[str, Any]:
        """No LLM call: a column is present iff it is literally in the
        dataset headers this run parsed, nothing inferred."""
        key = column.lower()
        candidates = [file_id] if file_id else list(self._headers)
        for fid in candidates:
            if key in self._headers.get(fid, []):
                return {"present": True, "file_id": fid}
        return {
            "present": False,
            "explanation": "not present in the dataset headers -- this is the final list, nothing else exists",
        }


class Instrument(Agent):
    NAME = "Instrument"
    PROMPT = (
        "You are Instrument, a specialist in PDF data-collection forms used in clinical studies. "
        "Given the extracted text of a form, list every field it asks a person to fill in. "
        "Return JSON: "
        '{"fields": [{"label": str, "collected_variable": str|null}]}. '
        "`collected_variable` is the machine-readable variable name ONLY if one is literally "
        "printed on the form next to the label (e.g. in brackets or parentheses, REDCap-style). "
        "Most fields on a form have no such annotation -- for those, `collected_variable` MUST be "
        "null. Never infer, guess, or construct a variable name that is not literally printed on "
        "the form, even if it seems like an obvious snake_case name for the field."
    )

    async def run(self, form_files: list[dict[str, Any]]) -> dict[str, Any]:
        aggregated: list[dict[str, Any]] = []
        self.scrub_count = 0
        self._fields: dict[str, list[dict[str, Any]]] = {}
        self._source_names: dict[str, str] = {}
        for f in form_files:
            file_id = f["file_id"]
            path = Path(f["stored_path"])
            self._source_names[file_id] = _opaque_file_id(f)

            # Tier 1: true fillable (AcroForm) PDF -- real field names read
            # straight off the PDF, zero LLM call, zero fabrication risk.
            acroform_fields = None
            if path.suffix.lower() == ".pdf":
                try:
                    acroform_fields = read_pdf_form_fields(path)
                except Exception:
                    acroform_fields = None
            if acroform_fields:
                await self._log(
                    f"instrument.acroform:{file_id}", "info",
                    {"fields_found": len(acroform_fields)},
                )
                self._fields[file_id] = acroform_fields
                aggregated.extend(acroform_fields)
                continue

            # Tier 2: flat/scanned PDF or .docx -- extraction-only LLM call
            # on text read through the shared readers, never parsed inline
            # here. `read_pdf` inherits the OCR fallback for scanned forms
            # (nothing form-specific is reimplemented); `.docx` forms
            # combine the structured table view with the full narrative
            # paragraph text.
            text = _read_form_text(path)
            projection = source_projection(
                content_type="form", raw_text=text[:6000], run_id=self.ctx.run_id,
            )
            n_removed = _phi_span_count(projection.reasons)
            self.scrub_count += n_removed
            await self._log(f"instrument.scrub:{file_id}", "info",
                            {"identifiers_removed": n_removed, "blocked": projection.blocked})
            if projection.blocked:
                await self._log(f"instrument.blocked:{file_id}", "info",
                                {"disposition": projection.disposition})
                self._fields[file_id] = []
                continue
            scrubbed = projection.projected_text
            reply = await self.call_json(
                f"Form: {_opaque_file_id(f)}\nExtracted text:\n{scrubbed}\n"
                "Respond with JSON only.",
                phase=f"instrument.read:{file_id}",
                default={"fields": []},
                status_text=f"Reading the form {_opaque_file_id(f)}",
            )
            fields = reply.get("fields", [])
            self._fields[file_id] = fields
            aggregated.extend(fields)
        await self._write_reports()
        return {"fields": aggregated}

    def verify(self, field_or_variable: str, file_id: str | None = None) -> dict[str, Any]:
        """No LLM call: a field is present iff this run's index literally
        has it, matched case-insensitively on either the printed label or
        the collected_variable name."""
        needle = field_or_variable.strip().casefold()
        candidates = [file_id] if file_id else list(self._fields)
        for fid in candidates:
            for field in self._fields.get(fid, []):
                label = (field.get("label") or "").strip().casefold()
                variable = (field.get("collected_variable") or "").strip().casefold()
                if needle == label or (variable and needle == variable):
                    return {"present": True, "file_id": fid, "field": field}
        return {
            "present": False,
            "explanation": "not present in any extracted form field",
        }

    async def _write_reports(self) -> list[str]:
        """Write one per-form field report into UPLOAD_DIR/<sid>/, built
        from the in-memory ``self._fields`` index only -- never
        reconstructed from `agent_log`, whose write-time scrub can mangle
        a label the way it did for the Tier-2 LLM's free-text replies."""
        from ..paths import UPLOAD_DIR, safe_join
        from ..security import scrub_persisted_text

        session_dir = UPLOAD_DIR / self.session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        for file_id, fields in self._fields.items():
            payload = {
                "file_id": file_id,
                "source_filename": self._source_names.get(file_id, ""),
                "fields": fields,
            }
            text = scrub_persisted_text(json.dumps(payload, indent=2))
            report_path = safe_join(session_dir, f"instrument_report_{file_id}.json")
            report_path.write_text(text, encoding="utf-8")
            written.append(str(report_path))
        return written


def _read_form_text(path: Path) -> str:
    """Deterministic text extraction for a Tier-2 form, dispatched by
    extension onto the shared, OCR-capable readers -- no inline parsing
    lives in the agent body."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        try:
            return read_pdf(path)
        except Exception:
            return ""
    if ext == ".docx":
        table_text = _read_docx_tables(path)
        try:
            prose_text = read_docx(path)
        except Exception:
            prose_text = ""
        return "\n\n".join(t for t in (table_text, prose_text) if t)
    return ""


# --- deterministic helpers ------------------------------------------------


_DOCX_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _read_docx_tables(path: Path) -> str:
    """Extract every table from a .docx file as CSV-shaped text.

    A .docx is a ZIP that stores the document body at ``word/document.xml``.
    We walk that XML and pull every ``<w:tbl>`` element out row-by-row so
    the LLM sees a flat, header + rows shape that mirrors what it would
    see for a CSV dictionary. Non-table paragraphs (title, intro prose)
    are concatenated after the tables so the LLM still gets any framing
    text the data steward may have written above the table.

    Security: bomb / DTD defence lives in ``phi_core.docx_safe`` so the
    dictionary and narrative readers can never drift again (root cause
    of iter_22 SEC-001).
    """
    from defusedxml import ElementTree as _DET

    from ..docx_safe import safe_read_docx_xml

    raw = safe_read_docx_xml(path)
    if raw is None:
        return ""
    try:
        tree = _DET.fromstring(raw, forbid_dtd=True)
    except (_DET.ParseError, ValueError):
        return ""

    lines: list[str] = []
    prose: list[str] = []
    root = tree
    body = root.find(f"{_DOCX_W_NS}body")
    if body is None:
        return ""

    def _cell_text(cell) -> str:
        parts = [t.text or "" for t in cell.iter(f"{_DOCX_W_NS}t")]
        return " ".join(x for x in parts if x).strip()

    table_index = 0
    for child in body:
        tag = child.tag
        if tag == f"{_DOCX_W_NS}p":
            # paragraph text -- capture short framing prose only
            text = " ".join((t.text or "") for t in child.iter(f"{_DOCX_W_NS}t")).strip()
            if text:
                prose.append(text)
        elif tag == f"{_DOCX_W_NS}tbl":
            table_index += 1
            lines.append(f"# table {table_index}")
            for tr in child.iter(f"{_DOCX_W_NS}tr"):
                cells = [_cell_text(tc) for tc in tr.iter(f"{_DOCX_W_NS}tc")]
                # emit CSV-shaped row; escape any embedded commas
                lines.append(",".join(
                    '"' + c.replace('"', '""') + '"' if ("," in c or '"' in c) else c
                    for c in cells
                ))
    if prose:
        lines.append("# narrative context")
        lines.extend(prose[:40])   # cap at 40 paragraphs to keep prompt bounded
    return "\n".join(lines)

