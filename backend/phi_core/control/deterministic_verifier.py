"""DeterministicVerifier: the section-54 post-execution verification layer
that replaces the retired ``agents/operator.py::Operator`` (docs
/MASTER_ARCHITECTURE_V2.md's agent-mapping table: "Operator -> migrate
useful deterministic verification into DeterministicVerifier then
remove").

Everything Operator used to check -- per-decision shape/coverage/
reverse-completeness -- is preserved here unchanged (``_verify_record``
below is Operator's own function, moved verbatim). Phase 10 adds the
remaining section-54 checklist items Operator never covered on its own
(checksums, file/column counts, schema-readability), and moves the raw
row-level work (reading Executor's written export plus, for a comparable
transform, the original source file) *inside* the sandbox boundary via
``control.sandbox.run_isolated`` when one is available, mirroring the
``*_maybe_sandboxed`` pattern ``agents/reasoning.py::Executor`` already
uses for its own raw-data work. Only safe summaries -- file/column
identifiers, counts, check names, sha256 digests -- ever cross back out
of the sandbox; no raw cell value is ever placed in a verdict, a log
line, or this module's return value.

``DeterministicVerifier`` is deliberately not an ``Agent`` subclass: it
never calls an LLM, submits no ``WorkItem`` of its own, and needs no
``AgentContext`` beyond the one raw resource it borrows for isolation
(``sandbox: SandboxRecord | None``, taken directly, the same way
``agents/reasoning.py::verify_keep_decisions_maybe_sandboxed`` takes
``ctx.sandbox`` as a plain parameter rather than a whole context).
"""
from __future__ import annotations

import asyncio
import json as _json
import re as _re
from pathlib import Path
from typing import Any

from .artifacts import _hash_file
from .records import SandboxRecord
from .sandbox import run_isolated


def _cap_age_90_ok(value: str) -> bool:
    if value == "90+":
        return True
    if value.isdigit():
        return 0 <= int(value) <= 89
    return False


_YEAR_ONLY_RE = _re.compile(r"^\d{4}$")
_ZIP3_RE = _re.compile(r"^\d{3}$")
_HASH_RE = _re.compile(r"^[0-9a-f]{16}$")
_PSEUDONYM_RE = _re.compile(r"^P[0-9a-f]{8}$")

# One shape predicate per transform action, checked over every non-empty
# written cell (moved verbatim from the retired `agents/operator.py`).
_SHAPE_CHECKS = {
    "cap_age_90": _cap_age_90_ok,
    "year_only": lambda v: bool(_YEAR_ONLY_RE.match(v)),
    "zip3_truncate": lambda v: bool(_ZIP3_RE.match(v)),
    "hash": lambda v: bool(_HASH_RE.match(v)),
    "pseudonymize": lambda v: bool(_PSEUDONYM_RE.match(v)),
}

# Actions whose shape check alone cannot catch a wrong-but-well-shaped
# value; when the source column was read, the expected transform is
# recomputed from each source cell and compared against what Executor
# actually wrote, in addition to the shape check.
_SOURCE_COMPARABLE_ACTIONS = frozenset({"cap_age_90", "year_only", "zip3_truncate", "pseudonymize"})
_SOURCE_REQUIRED_ACTIONS = _SOURCE_COMPARABLE_ACTIONS | {"scrub_text"}


def _pseudonymize_consistent(relevant: list[tuple[str, str]]) -> bool:
    """True iff the source-to-written mapping over this column is a
    consistent function (equal source values map to the same written
    pseudonym) and injective (distinct source values never collide on
    the same written pseudonym). Never inspects the pseudonym algorithm
    itself -- the verifier has no registry salt -- only the mapping's
    shape."""
    forward: dict[str, str] = {}
    reverse: dict[str, str] = {}
    for source_value, written_value in relevant:
        if written_value == "":
            return False
        prior = forward.get(source_value)
        if prior is not None:
            if prior != written_value:
                return False
            continue
        if written_value in reverse:
            return False
        forward[source_value] = written_value
        reverse[written_value] = source_value
    return True


def _source_value_mismatch_problem(action: str, column: str, cells: list[str],
                                    source_cells: list[str]) -> str | None:
    """Row-aligned comparison of every non-empty source cell's expected
    transform against Executor's written cell. Returns ``None`` when
    consistent, else a problem string that never contains a raw value."""
    from .transform_primitives import _apply_action

    relevant = [(s, w) for s, w in zip(source_cells, cells, strict=True) if s != ""]
    if action == "pseudonymize":
        ok = _pseudonymize_consistent(relevant)
        problem = "equal source values did not map to equal pseudonyms, or distinct source values collided"
    else:
        ok = all(_apply_action(source_value, action, column) == written_value
                  for source_value, written_value in relevant)
        problem = "exported value does not match the transform of the source value"
    return None if ok else f"{action}: {problem}"


def _verify_record(record: dict[str, Any], view: dict[str, Any] | None) -> dict[str, Any]:
    """Check one four-part work record against its file's pre-loaded
    written (and, for scrub_text, source) column view. Never opens a
    file: ``view`` was built once per file before any record was
    checked. Moved verbatim from the retired ``agents/operator.py``."""
    from .transform_primitives import _scrub_text_cell

    verdict = dict(record)
    column = record["column"]
    action = record["method"]

    if view is None:
        verdict.update(
            checks=[],
            verdict="fail",
            problem=f"file {record['file_id']!r} is missing from exports or could not be read",
            performed="nothing verifiable: the file never reached exports or could not be read",
        )
        return verdict

    header = view["header"]
    written = view["written"]

    if action == "undecided":
        verdict.update(
            checks=[{"name": "has_decision", "pass": False}],
            verdict="fail",
            problem="written column has no Judge/Sentinel decision and is not marked omitted",
            performed="column present in the written output with no matching decision",
        )
        return verdict

    present = column in header

    if column in view["omit"]:
        ok = not present
        verdict.update(
            checks=[{"name": "omit_expected", "pass": ok}],
            verdict="pass" if ok else "fail",
            problem="" if ok else "column was supposed to be omitted but is present in output",
            performed="column omitted as expected" if ok
            else "column present in output despite being marked omitted",
        )
        return verdict

    if not present:
        verdict.update(
            checks=[{"name": "column_presence", "pass": False}],
            verdict="fail",
            problem="decision has no corresponding column in the written output",
            performed="no matching column found in the written output",
        )
        return verdict

    cells = written.get(column, [])
    non_empty = [v for v in cells if v != ""]

    if action == "drop":
        ok = len(non_empty) == 0
        verdict.update(
            checks=[{"name": "column_presence", "pass": True}, {"name": "drop_empty", "pass": ok}],
            verdict="pass" if ok else "fail",
            problem="" if ok else "drop column left populated",
            performed="column present, all cells empty" if ok
            else f"column present, {len(non_empty)} non-empty cell(s) remain",
        )
        return verdict

    if action == "keep":
        verdict.update(
            checks=[{"name": "column_presence", "pass": True}],
            verdict="pass",
            problem="",
            performed="column present, left unchanged",
        )
        return verdict

    shape_check = _SHAPE_CHECKS.get(action)
    if shape_check is not None:
        ok = all(shape_check(v) for v in non_empty)
        checks = [{"name": "column_presence", "pass": True}, {"name": "shape", "pass": ok}]
        problem = "" if ok else f"{action}: a written cell failed the expected output shape check"
        performed = (f"column transformed via {action}, {len(non_empty)} value(s) checked" if ok
                     else f"column transformed via {action}, one or more values fail the expected shape")
        source = view.get("source")
        if ok and action in _SOURCE_COMPARABLE_ACTIONS and not view.get("source_error") and source is not None:
            value_problem = _source_value_mismatch_problem(action, column, cells, source.get(column, []))
            checks.append({"name": "source_value_match", "pass": value_problem is None})
            if value_problem is not None:
                ok = False
                problem = value_problem
                performed = f"column transformed via {action}, exported value does not match the source transform"
        verdict.update(checks=checks, verdict="pass" if ok else "fail", problem=problem, performed=performed)
        return verdict

    if action == "scrub_text":
        if view.get("source_error"):
            verdict.update(
                checks=[{"name": "column_presence", "pass": True}, {"name": "scrub_ran", "pass": False}],
                verdict="fail",
                problem="cannot verify scrub_text ran",
                performed="source could not be read; scrub_text cannot be verified",
            )
            return verdict
        source = view.get("source") or {}
        source_cells = source.get(column, [])
        relevant = [(s, w) for s, w in zip(source_cells, cells, strict=True) if s != ""]
        changed = any(s != w for s, w in relevant)
        ok = changed or all(_scrub_text_cell(v) == v for v in non_empty)
        verdict.update(
            checks=[{"name": "column_presence", "pass": True}, {"name": "scrub_ran", "pass": ok}],
            verdict="pass" if ok else "fail",
            problem="" if ok else "scrub_text produced no observable change",
            performed="scrub_text ran, at least one cell changed" if changed
            else ("scrub_text ran, output already clean" if ok
                  else "scrub_text produced no observable change from source"),
        )
        return verdict

    verdict.update(
        checks=[{"name": "column_presence", "pass": True}, {"name": "known_action", "pass": False}],
        verdict="fail",
        problem=f"unrecognized action {action!r}; the verifier cannot verify it",
        performed="action not recognized by DeterministicVerifier's verification rules",
    )
    return verdict


def _compute_verification(
    files: list[dict[str, Any]], decisions: list[dict[str, Any]],
    exports: dict[str, str], omit_by_file: dict[str, "set[str] | list[str]"] | None = None,
) -> dict[str, Any]:
    """Re-check every value Executor wrote against the decision Judge/
    Sentinel settled on (docs #54 items 4-8). Pure and synchronous so it
    can run unmodified either in-process or as a ``run_isolated``
    sandbox worker: reads ``agents.reasoning._read_columns`` (the only
    module the raw-reader call-site scan still allowlists) and never
    places a raw cell value anywhere in its return value.

    Returns ``{"verdicts": [...], "failed_file_ids": [...],
    "status": "clean" | "issues"}`` -- the exact shape the retired
    ``Operator.run`` returned, so every existing downstream consumer
    (``control.verification.build_verification_result``,
    ``agents/reviewer.py::Reviewer.run``'s coverage audit, the
    orchestrator's own ``op_failed_ids``/``final_status`` computation)
    keeps working unchanged.
    """
    from ..agents.reasoning import _read_columns

    omit_by_file = {k: set(v) for k, v in (omit_by_file or {}).items()}
    files_by_id = {f.get("file_id"): f for f in files}
    dataset_ids = {fid for fid, f in files_by_id.items() if f.get("kind") == "dataset"}

    by_file: dict[str, list[dict[str, Any]]] = {}
    for d in decisions:
        fid = d.get("file_id", "")
        if fid in dataset_ids or fid not in files_by_id:
            by_file.setdefault(fid, []).append(d)
    for fid in dataset_ids:
        by_file.setdefault(fid, [])

    views: dict[str, dict[str, Any] | None] = {}
    failed_file_ids: list[str] = []
    extra_records: list[dict[str, Any]] = []
    for file_id, group in by_file.items():
        if file_id not in exports:
            views[file_id] = None
            failed_file_ids.append(file_id)
            continue
        f = files_by_id.get(file_id) or {}
        ext = f.get("subtype", "")
        try:
            header, written = _read_columns(exports[file_id], ext)
        except Exception:
            views[file_id] = None
            failed_file_ids.append(file_id)
            continue

        source: dict[str, list[str]] | None = None
        source_error = False
        if any(d.get("action") in _SOURCE_REQUIRED_ACTIONS for d in group):
            src_path = f.get("stored_path")
            if not src_path:
                source_error = True
            else:
                try:
                    _src_header, source = _read_columns(src_path, ext)
                except Exception:
                    source = None
                    source_error = True

        omit_cols = set(omit_by_file.get(file_id, ()) or ())
        views[file_id] = {
            "header": header,
            "written": written,
            "source": source,
            "source_error": source_error,
            "omit": omit_cols,
        }

        decided_columns = {d.get("column", "") for d in group}
        for column in header:
            if column not in decided_columns and column not in omit_cols:
                extra_records.append({
                    "file_id": file_id, "column": column, "violation": {}, "method": "undecided",
                })

    records: list[dict[str, Any]] = [
        {
            "file_id": file_id,
            "column": d.get("column", ""),
            "violation": {"phi_category": d.get("phi_category"), "citation": d.get("citation")},
            "method": d.get("action"),
        }
        for file_id, group in by_file.items()
        for d in group
    ] + extra_records

    verdicts = [_verify_record(r, views.get(r["file_id"])) for r in records]
    status = "issues" if failed_file_ids or any(v["verdict"] == "fail" for v in verdicts) else "clean"
    return {"verdicts": verdicts, "failed_file_ids": failed_file_ids, "status": status}


def _sandboxed_compute_verification(payload_json: str) -> str:
    """Sandboxed dispatch target for ``_compute_verification``: the only
    thing that crosses back across the ``run_isolated`` boundary is this
    function's JSON-encoded return value (verdicts/failed_file_ids/
    status -- no raw cell value)."""
    payload = _json.loads(payload_json)
    result = _compute_verification(
        files=payload["files"], decisions=payload["decisions"],
        exports=payload["exports"], omit_by_file=payload.get("omit_by_file") or {},
    )
    return _json.dumps(result)


async def verify_maybe_sandboxed(
    sandbox: "SandboxRecord | None",
    files: list[dict[str, Any]], decisions: list[dict[str, Any]],
    exports: dict[str, str], omit_by_file: dict[str, "set[str]"] | None = None,
) -> dict[str, Any]:
    """Route ``_compute_verification`` through the sandbox when one is
    supplied (``ActivationFactory.activate(..., needs_sandbox=True)``);
    call it in-process otherwise. The in-process branch is the same
    permanent, documented compatibility path
    ``agents/reasoning.py::Executor``'s own ``*_maybe_sandboxed`` methods
    already use: every pre-existing unit test builds its context via
    ``control.testing.make_ctx``, which never attaches a sandbox."""
    if sandbox is None:
        return _compute_verification(files, decisions, exports, omit_by_file)
    payload = _json.dumps({
        "files": files, "decisions": decisions, "exports": exports,
        "omit_by_file": {k: sorted(v) for k, v in (omit_by_file or {}).items()},
    })
    encoded = await asyncio.to_thread(
        run_isolated, sandbox, _sandboxed_compute_verification, payload, return_kind="json",
    )
    return _json.loads(encoded)


class DeterministicVerifier:
    """The section-54 deterministic post-execution verification layer.

    Not an ``Agent`` (see module docstring): a plain, stateless class a
    caller constructs once and calls ``run`` on, passing the raw
    ``SandboxRecord`` (or ``None``) directly rather than a whole
    ``AgentContext``.
    """

    async def run(
        self,
        files: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
        exports: dict[str, str],
        omit_by_file: dict[str, "set[str]"] | None = None,
        *,
        sandbox: "SandboxRecord | None" = None,
    ) -> dict[str, Any]:
        """Run the full section-54 checklist. Items 4-8 (DROP/KEEP/
        transform/no-unexpected-columns/output-readable) come from
        ``verify_maybe_sandboxed`` above, unchanged from the retired
        Operator's own per-record checks. Items 1-3 (all input datasets
        accounted, all expected outputs exist, every manifest column
        accounted) are implicit in that same pass: a dataset file with
        no readable export lands in ``failed_file_ids``, and a decision
        with no matching column becomes a ``fail`` verdict.

        Items 9-12 (schema valid, file counts, column counts, checksums)
        are additive fields this method computes itself, entirely from
        safe metadata (file readability, row/column counts, sha256
        digests of already-written export files) -- never from a raw
        cell value, so they need no sandbox boundary of their own (the
        same reasoning the execute tail's own ``artifact_refs``
        computation already relies on for its post-execution
        ``_hash_file`` pass).
        """
        core = await verify_maybe_sandboxed(sandbox, files, decisions, exports, omit_by_file)
        result = dict(core)
        result["checksums"] = self._checksums(exports)
        result["file_counts"] = self._file_counts(files, exports, core["failed_file_ids"])
        result["column_counts"] = self._column_counts(decisions, core["verdicts"])
        result["schema_valid"] = self._schema_valid(files, core["failed_file_ids"])
        return result

    @staticmethod
    def _checksums(exports: dict[str, str]) -> dict[str, str]:
        """Item 12: a sha256 digest per written export file -- a safe
        summary of the bytes, never the bytes or a raw value themselves."""
        checksums: dict[str, str] = {}
        for file_id, path in exports.items():
            try:
                sha256, _size = _hash_file(Path(path))
            except OSError:
                continue
            checksums[file_id] = sha256
        return checksums

    @staticmethod
    def _file_counts(
        files: list[dict[str, Any]], exports: dict[str, str], failed_file_ids: list[str],
    ) -> dict[str, int]:
        """Items 1 and 10: how many dataset files were expected versus
        how many actually ended up with a readable, verified export."""
        dataset_ids = {f.get("file_id") for f in files if f.get("kind") == "dataset"}
        failed = set(failed_file_ids)
        readable = {fid for fid in dataset_ids if fid in exports and fid not in failed}
        return {"datasets_expected": len(dataset_ids), "datasets_readable": len(readable)}

    @staticmethod
    def _column_counts(decisions: list[dict[str, Any]], verdicts: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
        """Item 11: per file, how many columns had a decision versus how
        many verdicts the pass above actually produced for it (an
        ``undecided`` reverse-completeness record counts as a verdict,
        never as a decision)."""
        decision_counts: dict[str, int] = {}
        verdict_counts: dict[str, int] = {}
        for d in decisions:
            decision_counts[d.get("file_id", "")] = decision_counts.get(d.get("file_id", ""), 0) + 1
        for v in verdicts:
            verdict_counts[v.get("file_id", "")] = verdict_counts.get(v.get("file_id", ""), 0) + 1
        file_ids = set(decision_counts) | set(verdict_counts)
        return {
            fid: {"decisions": decision_counts.get(fid, 0), "verdicts": verdict_counts.get(fid, 0)}
            for fid in file_ids
        }

    @staticmethod
    def _schema_valid(files: list[dict[str, Any]], failed_file_ids: list[str]) -> dict[str, bool]:
        """Item 9: a dataset file's schema/subtype is "valid" exactly
        when its export was actually readable -- an unsupported or
        corrupt extension is what put it in ``failed_file_ids`` in the
        first place, so this is a named restatement of that same fact
        rather than a second, divergent read."""
        failed = set(failed_file_ids)
        return {
            f.get("file_id"): f.get("file_id") not in failed
            for f in files if f.get("kind") == "dataset" and f.get("file_id")
        }
