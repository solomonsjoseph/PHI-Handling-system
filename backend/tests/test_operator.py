"""Tests for the shared bounded worker-pool batching helper (Task 26) and
for the Operator agent built on top of it (Task 27).

Operator and Reviewer both build on `run_batched`; the tests through
`test_on_batch_fires_while_a_later_batch_is_still_blocked` cover the
helper itself. The Operator-specific tests follow.
"""
from __future__ import annotations

import asyncio
import csv
import threading
from pathlib import Path

import pytest
from phi_core.agents.batching import run_batched
from phi_core.agents.operator import Operator
from phi_core.agents.reasoning import (
    PseudonymRegistry,
    _scrub_text_cell,
    apply_column_actions_to_dataset,
)


def _fixed_check(batch: list[int]) -> list[dict]:
    """Deterministic per-item check with no cross-record comparison."""
    return [{"value": v, "verdict": "pass"} for v in batch]


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_size", [1, 5, 10])
@pytest.mark.parametrize("pool_size", [1, 6])
async def test_output_invariant_across_batch_and_pool_sizes(batch_size, pool_size):
    """The flattened, ordered result never depends on how work is chunked
    or how many workers run it concurrently."""
    items = list(range(23))

    result = await run_batched(items, _fixed_check, batch_size=batch_size, pool_size=pool_size)

    assert [r["value"] for r in result] == items
    assert all(r["verdict"] == "pass" for r in result)


@pytest.mark.asyncio
async def test_each_item_checked_exactly_once():
    """No item is dropped, duplicated, or handed to more than one batch."""
    items = list(range(17))
    lock = threading.Lock()
    seen: list[int] = []

    def check(batch: list[int]) -> list[dict]:
        with lock:
            seen.extend(batch)
        return [{"value": v} for v in batch]

    await run_batched(items, check, batch_size=4, pool_size=3)

    assert sorted(seen) == items


@pytest.mark.asyncio
async def test_on_batch_called_once_per_actual_batch_with_that_batchs_index():
    """23 items at batch_size=8 makes 3 batches (8, 8, 7); each fires
    on_batch exactly once, carrying its own position in `items`, and
    verdicts surface as soon as that batch finishes rather than being
    held until the whole run completes."""
    items = list(range(23))
    calls: dict[int, list[dict]] = {}

    async def on_batch(index: int, results: list[dict]) -> None:
        assert index not in calls, "on_batch fired twice for the same batch"
        calls[index] = results

    result = await run_batched(items, _fixed_check, batch_size=8, on_batch=on_batch)

    assert set(calls) == {0, 1, 2}
    assert [len(calls[i]) for i in (0, 1, 2)] == [8, 8, 7]
    assert sum(len(r) for r in calls.values()) == len(items)
    # on_batch's per-batch payloads reassemble the same ordered result.
    assert calls[0] + calls[1] + calls[2] == result


@pytest.mark.asyncio
async def test_empty_input_produces_no_checks_or_callbacks_and_empty_list():
    calls = []

    async def on_batch(index: int, results: list[dict]) -> None:
        calls.append((index, results))

    def check(batch: list[int]) -> list[dict]:
        raise AssertionError("check must never run on empty input")

    result = await run_batched([], check, on_batch=on_batch)

    assert result == []
    assert calls == []


@pytest.mark.asyncio
async def test_pool_size_bounds_and_reaches_concurrent_batches():
    """`pool_size` is a hard concurrency ceiling, not just a chunking knob:
    with 9 single-item batches and pool_size=3, exactly 3 checks run at
    once per wave. Every running check must rendezvous with `pool_size`
    peers at a barrier; a bound violation (a 4th thread also reaching the
    barrier) or an implementation that never overlaps batches (fewer than
    3 threads ever reaching it) both surface as a barrier timeout, so this
    is deterministic rather than a timing guess.
    """
    items = list(range(9))
    pool_size = 3
    barrier = threading.Barrier(pool_size, timeout=5)
    lock = threading.Lock()
    active = 0
    max_active = 0

    def check(batch: list[int]) -> list[dict]:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            barrier.wait()
        finally:
            with lock:
                active -= 1
        return [{"value": batch[0]}]

    result = await run_batched(items, check, batch_size=1, pool_size=pool_size)

    assert max_active == pool_size
    assert [r["value"] for r in result] == items


@pytest.mark.asyncio
async def test_no_shared_mutable_state_leaks_between_batches():
    """Each batch's `check` call only ever sees its own slice of items,
    never another batch's records, regardless of pool size."""
    items = list(range(20))

    def check(batch: list[int]) -> list[dict]:
        # A leaking implementation would hand every worker the same
        # underlying list; mutating it here must never affect other
        # batches' views.
        batch.append(-1)
        return [{"value": v, "batch_len": len(batch)} for v in batch if v != -1]

    result = await run_batched(items, check, batch_size=4, pool_size=4)

    assert [r["value"] for r in result] == items
    assert all(r["batch_len"] == 5 for r in result)


def test_run_batched_is_a_plain_coroutine_function():
    assert asyncio.iscoroutinefunction(run_batched)


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"batch_size": 0},
    {"batch_size": -1},
    {"pool_size": 0},
    {"pool_size": -3},
])
async def test_invalid_batch_or_pool_size_raises_before_any_work(kwargs):
    """A bad `batch_size`/`pool_size` is rejected up front, even against
    an empty item list, rather than producing an empty range or a
    semaphore that can never be acquired."""
    calls = []

    def check(batch: list[int]) -> list[dict]:
        calls.append(batch)
        return [{"value": v} for v in batch]

    with pytest.raises(ValueError):
        await run_batched([1, 2, 3], check, **kwargs)
    with pytest.raises(ValueError):
        await run_batched([], check, **kwargs)

    assert calls == []


@pytest.mark.asyncio
async def test_check_returning_wrong_result_count_raises_clear_error():
    """check() must return exactly one result per item; a silent
    truncation or duplication is a programming error, not data to smuggle
    through as misaligned output."""
    def check(batch: list[int]) -> list[dict]:
        return [{"value": batch[0]}]  # always one result, regardless of batch size

    with pytest.raises(ValueError, match="exactly one result per item"):
        await run_batched([1, 2, 3, 4], check, batch_size=2, pool_size=2)


@pytest.mark.asyncio
async def test_a_failing_check_cancels_unstarted_siblings_and_stops_cleanly():
    """A serial pool (pool_size=1) makes batch order deterministic: 0 and
    1 complete and are delivered before 2 fails; 3 and 4 are still parked
    on the pool and are cancelled without ever running `check`. Nothing
    keeps executing in the background after `run_batched` has raised."""
    items = [0, 1, 2, 3, 4]
    checked: list[int] = []
    batches_seen: list[int] = []

    def check(batch: list[int]) -> list[dict]:
        v = batch[0]
        checked.append(v)
        if v == 2:
            raise RuntimeError("boom")
        return [{"value": v}]

    async def on_batch(index: int, results: list[dict]) -> None:
        batches_seen.append(index)

    with pytest.raises(RuntimeError, match="boom"):
        await run_batched(items, check, batch_size=1, pool_size=1, on_batch=on_batch)

    assert checked == [0, 1, 2]
    assert batches_seen == [0, 1]

    await asyncio.sleep(0.05)
    assert checked == [0, 1, 2], "a cancelled sibling ran check() after run_batched raised"


@pytest.mark.asyncio
async def test_on_batch_fires_while_a_later_batch_is_still_blocked():
    """True incremental delivery, proven causally rather than by timing:
    batch 1's check cannot return until on_batch(0, ...) unblocks it, so
    if on_batch(0, ...) has run, batch 1 is provably still mid-check."""
    items = [0, 1]
    slow_started = threading.Event()
    allow_slow_finish = threading.Event()
    fast_on_batch_seen = threading.Event()

    def check(batch: list[int]) -> list[dict]:
        v = batch[0]
        if v == 1:
            slow_started.set()
            assert allow_slow_finish.wait(timeout=5), \
                "fast batch's on_batch never unblocked the slow batch"
        return [{"value": v}]

    async def on_batch(index: int, results: list[dict]) -> None:
        if index == 0:
            assert slow_started.wait(timeout=2), "sibling batch never started concurrently"
            allow_slow_finish.set()
            fast_on_batch_seen.set()

    result = await run_batched(items, check, batch_size=1, pool_size=2, on_batch=on_batch)

    assert fast_on_batch_seen.is_set()
    assert [r["value"] for r in result] == [0, 1]


# ---- Operator agent (Task 27) ----------------------------------------------


class FakeAgentLog:
    def __init__(self):
        self.inserted: list[dict] = []

    async def insert_one(self, doc, *_args, **_kwargs):
        self.inserted.append(doc)


class FakeDb:
    def __init__(self):
        self.agent_log = FakeAgentLog()


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)


def _dataset_file(file_id: str, stored_path: str | None = None) -> dict:
    d = {"file_id": file_id, "kind": "dataset", "subtype": "csv"}
    if stored_path is not None:
        d["stored_path"] = stored_path
    return d


def test_all_action_types_pass_against_a_real_executor_export(tmp_path):
    """One passing case per action type, checked against an export written
    by the real Executor transform (not hand-built), so the shape checks
    are proven against Executor's actual output encoding."""
    header = ["id", "ssn", "age", "dob", "zip", "mrn", "name", "notes"]
    rows = [
        ["1", "123-45-6789", "45", "1980-05-01", "90210",
         "abc123", "Jane Doe", "Contact patient at john.smith@example.com for follow up."],
        ["2", "987-65-4321", "96", "1975-02-14", "10001-1234",
         "def456", "Jane Doe", "Patient reports feeling better this week."],
    ]
    src = tmp_path / "dataset.csv"
    _write_csv(src, header, rows)
    dst = tmp_path / "dataset_out.csv"

    decisions = [
        {"file_id": "f1", "column": "id", "action": "keep", "phi_category": "NONE", "citation": ""},
        {"file_id": "f1", "column": "ssn", "action": "drop", "phi_category": "G",
         "citation": "45 CFR 164.514(b)(2)(i)(G)"},
        {"file_id": "f1", "column": "age", "action": "cap_age_90", "phi_category": "C",
         "citation": "45 CFR 164.514(b)(2)(i)(C)"},
        {"file_id": "f1", "column": "dob", "action": "year_only", "phi_category": "C",
         "citation": "45 CFR 164.514(b)(2)(i)(C)"},
        {"file_id": "f1", "column": "zip", "action": "zip3_truncate", "phi_category": "B",
         "citation": "45 CFR 164.514(b)(2)(i)(B)"},
        {"file_id": "f1", "column": "mrn", "action": "hash", "phi_category": "H",
         "citation": "45 CFR 164.514(b)(2)(i)(H)"},
        {"file_id": "f1", "column": "name", "action": "pseudonymize", "phi_category": "A",
         "citation": "45 CFR 164.514(b)(2)(i)(A)"},
        {"file_id": "f1", "column": "notes", "action": "scrub_text", "phi_category": "A",
         "citation": "45 CFR 164.514(b)(2)(i)(A)"},
    ]
    apply_column_actions_to_dataset(src, dst, "csv", decisions, PseudonymRegistry(salt="test-salt"))

    files = [_dataset_file("f1", str(src))]
    exports = {"f1": str(dst)}

    op = Operator(session_id="s", llm=None, db=FakeDb())
    result = asyncio.run(op.run(files, decisions, exports))

    assert result["failed_file_ids"] == []
    by_col = {v["column"]: v for v in result["verdicts"]}
    for col in header:
        assert by_col[col]["verdict"] == "pass", (col, by_col[col])
    assert result["status"] == "clean"


def test_cap_age_90_shape_violation_is_caught(tmp_path):
    """A corrupted export (age never capped) is caught, and the raw
    offending value never appears in the reported problem text."""
    src = tmp_path / "out.csv"
    _write_csv(src, ["age"], [["96"]])
    files = [_dataset_file("f1")]
    decisions = [{"file_id": "f1", "column": "age", "action": "cap_age_90",
                  "phi_category": "C", "citation": "45 CFR 164.514(b)(2)(i)(C)"}]
    exports = {"f1": str(src)}

    op = Operator(session_id="s", llm=None, db=FakeDb())
    result = asyncio.run(op.run(files, decisions, exports))

    v = result["verdicts"][0]
    assert v["verdict"] == "fail"
    assert "96" not in v["problem"]
    assert result["status"] == "issues"


def test_decision_for_nonexistent_column_is_flagged(tmp_path):
    """Finding 12: a stale/misspelled column name from Judge/Sentinel is
    surfaced rather than silently ignored. The written file's only real
    column ('id') has no decision either, so reverse completeness also
    flags it as undecided."""
    src = tmp_path / "out.csv"
    _write_csv(src, ["id"], [["1"]])
    files = [_dataset_file("f1")]
    decisions = [{"file_id": "f1", "column": "ssn", "action": "drop",
                  "phi_category": "G", "citation": "45 CFR 164.514(b)(2)(i)(G)"}]
    exports = {"f1": str(src)}

    op = Operator(session_id="s", llm=None, db=FakeDb())
    result = asyncio.run(op.run(files, decisions, exports))

    by_col = {v["column"]: v for v in result["verdicts"]}
    assert by_col["ssn"]["verdict"] == "fail"
    assert "no corresponding column" in by_col["ssn"]["problem"]
    assert by_col["id"]["verdict"] == "fail"
    assert by_col["id"]["method"] == "undecided"
    assert result["status"] == "issues"


def test_drop_column_left_populated_fails(tmp_path):
    src = tmp_path / "out.csv"
    _write_csv(src, ["ssn"], [["123-45-6789"], [""]])
    files = [_dataset_file("f1")]
    decisions = [{"file_id": "f1", "column": "ssn", "action": "drop",
                  "phi_category": "G", "citation": "45 CFR 164.514(b)(2)(i)(G)"}]
    exports = {"f1": str(src)}

    op = Operator(session_id="s", llm=None, db=FakeDb())
    result = asyncio.run(op.run(files, decisions, exports))

    v = result["verdicts"][0]
    assert v["verdict"] == "fail"
    assert v["problem"] == "drop column left populated"


def test_scrub_text_cell_preserves_adjacent_markup():
    """Regression test for reasoning.py finding 4 (already fixed): a
    detected PHI span must not eat into adjacent markup. This proves the
    existing `_scrub_text_cell` behavior Operator relies on rather than
    re-checking, not new Operator logic."""
    text = "<b>John Smith</b> <a href='mailto:x@y.com'>"
    out = _scrub_text_cell(text)
    assert "</b>" in out
    assert "John Smith" not in out


def test_omit_by_file_column_expected_absent_passes(tmp_path):
    src = tmp_path / "out.csv"
    _write_csv(src, ["id"], [["1"]])  # 'notes' entirely absent, as omit_by_file specifies
    files = [_dataset_file("f1")]
    decisions = [
        {"file_id": "f1", "column": "id", "action": "keep", "phi_category": "NONE", "citation": ""},
        {"file_id": "f1", "column": "notes", "action": "scrub_text", "phi_category": "A",
         "citation": "45 CFR 164.514(b)(2)(i)(A)"},
    ]
    exports = {"f1": str(src)}
    omit_by_file = {"f1": {"notes"}}

    op = Operator(session_id="s", llm=None, db=FakeDb())
    result = asyncio.run(op.run(files, decisions, exports, omit_by_file))

    by_col = {v["column"]: v for v in result["verdicts"]}
    assert by_col["notes"]["verdict"] == "pass"
    assert by_col["notes"]["performed"] == "column omitted as expected"
    assert result["status"] == "clean"


def test_omit_by_file_column_present_when_expected_absent_fails(tmp_path):
    src = tmp_path / "out.csv"
    _write_csv(src, ["id", "notes"], [["1", "leaked text"]])
    files = [_dataset_file("f1")]
    decisions = [{"file_id": "f1", "column": "notes", "action": "scrub_text",
                  "phi_category": "A", "citation": "45 CFR 164.514(b)(2)(i)(A)"}]
    exports = {"f1": str(src)}
    omit_by_file = {"f1": {"notes"}}

    op = Operator(session_id="s", llm=None, db=FakeDb())
    result = asyncio.run(op.run(files, decisions, exports, omit_by_file))

    by_col = {v["column"]: v for v in result["verdicts"]}
    assert by_col["notes"]["verdict"] == "fail"
    assert by_col["notes"]["problem"] == "column was supposed to be omitted but is present in output"
    # 'id' has no decision either and is not omitted -- reverse completeness.
    assert by_col["id"]["verdict"] == "fail"
    assert by_col["id"]["method"] == "undecided"


def test_missing_export_file_fails_every_decision(tmp_path):
    files = [_dataset_file("f1")]
    decisions = [
        {"file_id": "f1", "column": "id", "action": "keep", "phi_category": "NONE", "citation": ""},
        {"file_id": "f1", "column": "ssn", "action": "drop", "phi_category": "G",
         "citation": "45 CFR 164.514(b)(2)(i)(G)"},
    ]
    exports: dict[str, str] = {}  # Executor never wrote f1

    op = Operator(session_id="s", llm=None, db=FakeDb())
    result = asyncio.run(op.run(files, decisions, exports))

    assert result["failed_file_ids"] == ["f1"]
    assert len(result["verdicts"]) == 2
    assert all(v["verdict"] == "fail" for v in result["verdicts"])
    assert all(v["checks"] == [] for v in result["verdicts"])
    assert all("missing from exports or could not be read" in v["problem"] for v in result["verdicts"])
    assert result["status"] == "issues"


def test_non_dataset_file_decisions_are_out_of_scope(tmp_path):
    """Metadata/narrative files never carry per-column decisions in this
    pipeline; Operator must not invent a verdict for one."""
    files = [{"file_id": "f1", "kind": "metadata", "subtype": "csv"}]
    decisions = [{"file_id": "f1", "column": "code", "action": "keep",
                  "phi_category": "NONE", "citation": ""}]
    exports = {"f1": str(tmp_path / "does_not_matter.csv")}

    op = Operator(session_id="s", llm=None, db=FakeDb())
    result = asyncio.run(op.run(files, decisions, exports))

    assert result["verdicts"] == []
    assert result["failed_file_ids"] == []
    assert result["status"] == "clean"


def test_agent_log_row_emitted_per_batch(tmp_path):
    """One agent_log row per batch, phase='operator.batch:<n>', carrying
    accurate pass/fail/count verdict tallies."""
    src = tmp_path / "out.csv"
    header = [f"col{i}" for i in range(10)]
    _write_csv(src, header, [["x"] * 10])
    files = [_dataset_file("f1")]
    decisions = [{"file_id": "f1", "column": c, "action": "keep",
                  "phi_category": "NONE", "citation": ""} for c in header]
    exports = {"f1": str(src)}
    db = FakeDb()

    op = Operator(session_id="s", llm=None, db=db)
    result = asyncio.run(op.run(files, decisions, exports))

    batch_rows = [row for row in db.agent_log.inserted if row["phase"].startswith("operator.batch:")]
    assert len(batch_rows) == 2  # 10 decisions at batch_size=8 -> batches of 8 and 2
    by_phase = {row["phase"]: row["payload"] for row in batch_rows}
    assert set(by_phase) == {"operator.batch:0", "operator.batch:1"}
    assert by_phase["operator.batch:0"] == {"pass": 8, "fail": 0, "count": 8}
    assert by_phase["operator.batch:1"] == {"pass": 2, "fail": 0, "count": 2}
    assert len(result["verdicts"]) == 10
    assert result["status"] == "clean"


def test_undecided_written_column_is_flagged(tmp_path):
    """Reverse completeness: a column present in the written output with
    no matching decision and no omit_by_file entry is invisible to
    Judge/Sentinel and must be surfaced, not silently accepted."""
    src = tmp_path / "out.csv"
    _write_csv(src, ["id", "extra"], [["1", "leftover value"]])
    files = [_dataset_file("f1")]
    decisions = [{"file_id": "f1", "column": "id", "action": "keep",
                  "phi_category": "NONE", "citation": ""}]
    exports = {"f1": str(src)}

    op = Operator(session_id="s", llm=None, db=FakeDb())
    result = asyncio.run(op.run(files, decisions, exports))

    by_col = {v["column"]: v for v in result["verdicts"]}
    assert by_col["id"]["verdict"] == "pass"
    assert by_col["extra"]["verdict"] == "fail"
    assert by_col["extra"]["method"] == "undecided"
    assert by_col["extra"]["violation"] == {}
    assert result["status"] == "issues"


@pytest.mark.parametrize("action,bad_value", [
    ("year_only", "198"),
    ("zip3_truncate", "9021"),
    ("hash", "not-a-hash"),
    ("pseudonymize", "JaneDoe"),
])
def test_transform_shape_violations_are_caught(tmp_path, action, bad_value):
    """A shape violation is caught for every transform action, not just
    cap_age_90, and the raw offending value never leaks into the problem
    text."""
    src = tmp_path / "out.csv"
    _write_csv(src, ["col"], [[bad_value]])
    files = [_dataset_file("f1")]
    decisions = [{"file_id": "f1", "column": "col", "action": action,
                  "phi_category": "C", "citation": "45 CFR 164.514(b)(2)(i)(C)"}]
    exports = {"f1": str(src)}

    op = Operator(session_id="s", llm=None, db=FakeDb())
    result = asyncio.run(op.run(files, decisions, exports))

    v = result["verdicts"][0]
    assert v["verdict"] == "fail"
    assert bad_value not in v["problem"]
    assert action in v["problem"]


def test_scrub_text_no_change_from_source_fails(tmp_path):
    """Operator itself catches a scrub_text column that never actually
    changed, not merely `_scrub_text_cell` in isolation."""
    src = tmp_path / "in.csv"
    _write_csv(src, ["notes"], [["Patient contacted at john@example.com"]])
    dst = tmp_path / "out.csv"
    _write_csv(dst, ["notes"], [["Patient contacted at john@example.com"]])  # scrub never ran
    files = [_dataset_file("f1", str(src))]
    decisions = [{"file_id": "f1", "column": "notes", "action": "scrub_text",
                  "phi_category": "A", "citation": "45 CFR 164.514(b)(2)(i)(A)"}]
    exports = {"f1": str(dst)}

    op = Operator(session_id="s", llm=None, db=FakeDb())
    result = asyncio.run(op.run(files, decisions, exports))

    by_col = {v["column"]: v for v in result["verdicts"]}
    assert by_col["notes"]["verdict"] == "fail"
    assert by_col["notes"]["problem"] == "scrub_text produced no observable change"


def test_scrub_text_missing_stored_path_fails_closed(tmp_path):
    """No stored_path at all -- Operator cannot compare against a source
    it was never given, so it fails closed rather than passing vacuously."""
    dst = tmp_path / "out.csv"
    _write_csv(dst, ["notes"], [["anything, doesn't matter"]])
    files = [_dataset_file("f1")]  # no stored_path
    decisions = [{"file_id": "f1", "column": "notes", "action": "scrub_text",
                  "phi_category": "A", "citation": "45 CFR 164.514(b)(2)(i)(A)"}]
    exports = {"f1": str(dst)}

    op = Operator(session_id="s", llm=None, db=FakeDb())
    result = asyncio.run(op.run(files, decisions, exports))

    v = result["verdicts"][0]
    assert v["verdict"] == "fail"
    assert v["problem"] == "cannot verify scrub_text ran"


def test_scrub_text_unreadable_source_fails_closed(tmp_path):
    """stored_path points at a file that cannot be read -- same fail-closed
    outcome as a missing path, never a silent pass."""
    dst = tmp_path / "out.csv"
    _write_csv(dst, ["notes"], [["anything, doesn't matter"]])
    files = [_dataset_file("f1", str(tmp_path / "does_not_exist.csv"))]
    decisions = [{"file_id": "f1", "column": "notes", "action": "scrub_text",
                  "phi_category": "A", "citation": "45 CFR 164.514(b)(2)(i)(A)"}]
    exports = {"f1": str(dst)}

    op = Operator(session_id="s", llm=None, db=FakeDb())
    result = asyncio.run(op.run(files, decisions, exports))

    v = result["verdicts"][0]
    assert v["verdict"] == "fail"
    assert v["problem"] == "cannot verify scrub_text ran"


def test_corrupt_written_file_isolated_from_sibling_files(tmp_path):
    """A read failure on one file's written export (unsupported extension
    here, deterministically triggers the read failure) is isolated to that
    file_id -- it lands in failed_file_ids without crashing verification
    of a sibling file that reads cleanly."""
    good_src = tmp_path / "good.csv"
    _write_csv(good_src, ["id"], [["1"]])
    files = [
        _dataset_file("f1"),
        {"file_id": "f2", "kind": "dataset", "subtype": "weird"},
    ]
    decisions = [
        {"file_id": "f1", "column": "id", "action": "keep", "phi_category": "NONE", "citation": ""},
        {"file_id": "f2", "column": "id", "action": "keep", "phi_category": "NONE", "citation": ""},
    ]
    exports = {"f1": str(good_src), "f2": str(tmp_path / "bad.weird")}

    op = Operator(session_id="s", llm=None, db=FakeDb())
    result = asyncio.run(op.run(files, decisions, exports))

    assert result["failed_file_ids"] == ["f2"]
    by_file_col = {(v["file_id"], v["column"]): v for v in result["verdicts"]}
    assert by_file_col[("f1", "id")]["verdict"] == "pass"
    assert by_file_col[("f2", "id")]["verdict"] == "fail"
    assert "missing from exports or could not be read" in by_file_col[("f2", "id")]["problem"]
    assert result["status"] == "issues"


def test_unknown_file_id_decision_is_flagged(tmp_path):
    """A decision naming a file_id Operator has never heard of (not in
    `files` at all) is the file-level analog of finding 12 -- surfaced as
    a failure rather than silently dropped."""
    src = tmp_path / "out.csv"
    _write_csv(src, ["id"], [["1"]])
    files = [_dataset_file("f1")]  # only f1 known
    decisions = [
        {"file_id": "f1", "column": "id", "action": "keep", "phi_category": "NONE", "citation": ""},
        {"file_id": "ghost", "column": "ssn", "action": "drop", "phi_category": "G",
         "citation": "45 CFR 164.514(b)(2)(i)(G)"},
    ]
    exports = {"f1": str(src)}

    op = Operator(session_id="s", llm=None, db=FakeDb())
    result = asyncio.run(op.run(files, decisions, exports))

    assert "ghost" in result["failed_file_ids"]
    by_file_col = {(v["file_id"], v["column"]): v for v in result["verdicts"]}
    assert by_file_col[("ghost", "ssn")]["verdict"] == "fail"
    assert by_file_col[("f1", "id")]["verdict"] == "pass"
    assert result["status"] == "issues"


def test_scrub_text_column_with_nothing_to_scrub_passes(tmp_path):
    """A scrub_text column whose source never contained anything
    detectable is correctly unchanged from source -- must not be reported
    as a failure just because nothing differs (the false-positive the
    naive changed-from-source check produced)."""
    rows = [["The subject continues routine treatment as scheduled."],
            ["Nothing sensitive is mentioned in this note."]]
    src = tmp_path / "in.csv"
    _write_csv(src, ["notes"], rows)
    dst = tmp_path / "out.csv"
    _write_csv(dst, ["notes"], rows)  # identical: correct, since there was nothing to scrub
    files = [_dataset_file("f1", str(src))]
    decisions = [{"file_id": "f1", "column": "notes", "action": "scrub_text",
                  "phi_category": "A", "citation": "45 CFR 164.514(b)(2)(i)(A)"}]
    exports = {"f1": str(dst)}

    op = Operator(session_id="s", llm=None, db=FakeDb())
    result = asyncio.run(op.run(files, decisions, exports))

    by_col = {v["column"]: v for v in result["verdicts"]}
    assert by_col["notes"]["verdict"] == "pass"
    assert result["status"] == "clean"


def test_dataset_file_with_zero_decisions_still_gets_reverse_completeness(tmp_path):
    """A dataset file Executor wrote with zero decisions at all (every
    column fell through Executor's SEC-004 fail-closed default) must still
    run the undecided-column pass rather than being skipped entirely for
    lack of any decision to key off of."""
    src = tmp_path / "out.csv"
    _write_csv(src, ["id", "ssn"], [["1", ""]])
    files = [_dataset_file("f1")]
    decisions: list[dict] = []  # Judge/Sentinel produced nothing for this file
    exports = {"f1": str(src)}

    op = Operator(session_id="s", llm=None, db=FakeDb())
    result = asyncio.run(op.run(files, decisions, exports))

    by_col = {v["column"]: v for v in result["verdicts"]}
    assert by_col["id"]["method"] == "undecided"
    assert by_col["id"]["verdict"] == "fail"
    assert by_col["ssn"]["method"] == "undecided"
    assert by_col["ssn"]["verdict"] == "fail"
    assert result["status"] == "issues"


def test_header_only_export_does_not_block_decisions(tmp_path):
    """Zero data rows is a valid, empty dataset -- iter_dataset_rows yields
    nothing to build a header from, so the real on-disk header must still
    be used rather than treating every column as missing."""
    src = tmp_path / "out.csv"
    _write_csv(src, ["id", "ssn"], [])  # header only, zero data rows
    files = [_dataset_file("f1")]
    decisions = [
        {"file_id": "f1", "column": "id", "action": "keep", "phi_category": "NONE", "citation": ""},
        {"file_id": "f1", "column": "ssn", "action": "drop", "phi_category": "G",
         "citation": "45 CFR 164.514(b)(2)(i)(G)"},
    ]
    exports = {"f1": str(src)}

    op = Operator(session_id="s", llm=None, db=FakeDb())
    result = asyncio.run(op.run(files, decisions, exports))

    by_col = {v["column"]: v for v in result["verdicts"]}
    assert by_col["id"]["verdict"] == "pass"
    assert by_col["ssn"]["verdict"] == "pass"
    assert result["status"] == "clean"


def test_zero_decision_file_missing_from_exports_is_not_silently_invisible(tmp_path):
    """A dataset file with zero decisions at all (e.g. Schema couldn't read
    its headers) that ALSO failed to write (Executor's write raised, so it
    never reached exports) must not vanish with no verdict and no audit
    trail -- it has to land in failed_file_ids so the session status
    reflects the loss rather than reporting a false 'complete'."""
    files = [_dataset_file("f1")]
    decisions: list[dict] = []  # no decision ever named this file
    exports: dict[str, str] = {}  # and Executor never wrote it either

    op = Operator(session_id="s", llm=None, db=FakeDb())
    result = asyncio.run(op.run(files, decisions, exports))

    assert result["failed_file_ids"] == ["f1"]
    assert result["status"] == "issues"


# ---- Task 28: Operator wired between Executor and Publish Guard -----------


def test_run_pipeline_excludes_corrupted_export_and_ends_partially_complete(tmp_path, monkeypatch):
    """Full-pipeline-shaped proof, run directly against
    `orchestrator.run_pipeline` with the same fake-agent-double pattern
    `test_keep_verification_pipeline.py` uses against this same function:
    every agent except the real Operator is faked, Executor is faked to
    hand back one hand-corrupted export (finding 9's `cap_age_90`
    violation) alongside one clean export, and the assertions prove
    Operator's filtering and status change land end to end.

    A corrupted export must: (1) be excluded from the final `exports`
    dict used everywhere downstream (Publish Guard, Auditor, the
    completion `$set`), (2) be named in `operator_failures`, and (3)
    leave the run `partially_complete`, not `complete`.
    """
    from phi_core.agents import orchestrator

    bad_export = tmp_path / "f1_export.csv"
    _write_csv(bad_export, ["age"], [["96"]])  # cap_age_90 shape violation
    good_export = tmp_path / "f2_export.csv"
    _write_csv(good_export, ["age"], [["45"]])  # valid cap_age_90 output

    class FakeSessions:
        def __init__(self):
            self.updates = []

        async def find_one(self, *_args, **_kwargs):
            return None

        async def update_one(self, *_args, **_kwargs):
            self.updates.append(_args[1])

    class FakeAgentLog:
        async def insert_one(self, *_args, **_kwargs):
            return None

    class FakeDb:
        def __init__(self):
            self.sessions = FakeSessions()
            self.agent_log = FakeAgentLog()

    class FakeStatute:
        def __init__(self, **_kwargs):
            pass

        async def run(self, **_kwargs):
            return {}

    class FakePraxis:
        def __init__(self, **_kwargs):
            pass

        async def method_for(self, _category):
            return {}

    class FakeLexicon:
        def __init__(self, **_kwargs):
            pass

        async def run(self, **_kwargs):
            return {"columns": []}

    class FakeInstrument(FakeLexicon):
        async def run(self, **_kwargs):
            return {"fields": []}

    class FakeSchema(FakeLexicon):
        pass

    class FakeJudge:
        def __init__(self, **_kwargs):
            self.call_failures = 0
            self.last_message_id = None

        async def run(self, **_kwargs):
            return {"decisions": [
                {"file_id": "f1", "column": "age", "action": "cap_age_90",
                 "phi_category": "C", "citation": "45 CFR 164.514(b)(2)(i)(C)",
                 "confidence": 0.95, "reason": "Judge decision"},
                {"file_id": "f2", "column": "age", "action": "cap_age_90",
                 "phi_category": "C", "citation": "45 CFR 164.514(b)(2)(i)(C)",
                 "confidence": 0.95, "reason": "Judge decision"},
            ]}

    class FakeSentinel:
        def __init__(self, **_kwargs):
            self.call_failures = 0

        async def run(self, **_kwargs):
            return {"issues": []}

    class FakeExecutor:
        def __init__(self, **_kwargs):
            pass

        async def run(self, **_kwargs):
            return {"exports": {"f1": str(bad_export), "f2": str(good_export)}}

    class FakeAuditor:
        def __init__(self, **_kwargs):
            pass

        async def _log(self, *_args, **_kwargs):
            return None

        async def run(self, **_kwargs):
            return {"verdict": "clean", "issues": [], "metrics": {}, "confidence": 1.0, "summary": "ok"}

    class FakeScout:
        def __init__(self, **_kwargs):
            pass

        async def run(self, **_kwargs):
            return {}

    class FakeLedger:
        def __init__(self, **_kwargs):
            pass

        async def run(self, **_kwargs):
            return {}

    class FakeHerald:
        def __init__(self, **_kwargs):
            pass

        async def run(self, **_kwargs):
            return {}

    monkeypatch.setattr(orchestrator, "Statute", FakeStatute)
    monkeypatch.setattr(orchestrator, "Praxis", FakePraxis)
    monkeypatch.setattr(orchestrator, "Lexicon", FakeLexicon)
    monkeypatch.setattr(orchestrator, "Instrument", FakeInstrument)
    monkeypatch.setattr(orchestrator, "Schema", FakeSchema)
    monkeypatch.setattr(orchestrator, "Judge", FakeJudge)
    monkeypatch.setattr(orchestrator, "Sentinel", FakeSentinel)
    monkeypatch.setattr(orchestrator, "Executor", FakeExecutor)
    monkeypatch.setattr(orchestrator, "Auditor", FakeAuditor)
    monkeypatch.setattr(orchestrator, "Scout", FakeScout)
    monkeypatch.setattr(orchestrator, "Ledger", FakeLedger)
    monkeypatch.setattr(orchestrator, "Herald", FakeHerald)

    phase_events = []

    async def emit(_message):
        return None

    async def on_phase(phase, payload):
        phase_events.append((phase, payload))

    db = FakeDb()
    result = asyncio.run(orchestrator.run_pipeline(
        {
            "id": "session",
            "files": [
                {"kind": "dataset", "file_id": "f1", "subtype": "csv", "stored_path": str(bad_export)},
                {"kind": "dataset", "file_id": "f2", "subtype": "csv", "stored_path": str(good_export)},
            ],
        },
        db,
        object(),
        emit,
        on_phase,
    ))

    assert "f1" not in result["exports"]
    assert result["exports"] == {"f2": str(good_export)}
    assert result["operator_failures"] == ["f1"]
    assert result["status"] == "partially_complete"

    operator_events = [e for e in phase_events if e[0] == "operator"]
    assert len(operator_events) == 1
    assert operator_events[0][1]["decision_count"] == 2

    completion_update = db.sessions.updates[-1]["$set"]
    assert completion_update["status"] == "partially_complete"
    assert completion_update["operator_failures"] == ["f1"]
    assert completion_update["export_paths"] == {"f2": str(good_export)}


# ---- Task 30: Reviewer wired between Operator and Publish Guard -----------


def test_run_pipeline_reviewer_only_finding_excludes_file_and_ends_partially_complete(tmp_path, monkeypatch):
    """Full-pipeline-shaped proof that Reviewer's own coverage check, not
    Operator's, is what excludes a file from the final export.

    f1 gets two Judge decisions naming the same column ('field', both
    'drop') -- Operator processes decisions one-for-one and verifies each
    independently, so a duplicate decision on an already-empty column
    still verifies clean and Operator reports zero failures for f1. Only
    Reviewer's independent recount (2 decisions vs. 1 real written
    column) catches the coverage_mismatch. f2 has one matching decision
    and is unaffected.

    The real Operator and real Reviewer both run; every other agent is a
    fake double, mirroring the Task 28 proof test above.
    """
    from phi_core.agents import orchestrator

    f1_export = tmp_path / "f1_export.csv"
    _write_csv(f1_export, ["field"], [[""]])  # dropped column: empty cell
    f2_export = tmp_path / "f2_export.csv"
    _write_csv(f2_export, ["field"], [[""]])  # dropped column: empty cell

    class FakeSessions:
        def __init__(self):
            self.updates = []

        async def find_one(self, *_args, **_kwargs):
            return None

        async def update_one(self, *_args, **_kwargs):
            self.updates.append(_args[1])

    class FakeAgentLog:
        async def insert_one(self, *_args, **_kwargs):
            return None

    class FakeDb:
        def __init__(self):
            self.sessions = FakeSessions()
            self.agent_log = FakeAgentLog()

    class FakeStatute:
        def __init__(self, **_kwargs):
            pass

        async def run(self, **_kwargs):
            return {}

    class FakePraxis:
        def __init__(self, **_kwargs):
            pass

        async def method_for(self, _category):
            return {}

    class FakeLexicon:
        def __init__(self, **_kwargs):
            pass

        async def run(self, **_kwargs):
            return {"columns": []}

    class FakeInstrument(FakeLexicon):
        async def run(self, **_kwargs):
            return {"fields": []}

    class FakeSchema(FakeLexicon):
        pass

    class FakeJudge:
        def __init__(self, **_kwargs):
            self.call_failures = 0
            self.last_message_id = None

        async def run(self, **_kwargs):
            return {"decisions": [
                {"file_id": "f1", "column": "field", "action": "drop",
                 "phi_category": "G", "citation": "45 CFR 164.514(b)(2)(i)(G)",
                 "confidence": 0.95, "reason": "Judge decision"},
                {"file_id": "f1", "column": "field", "action": "drop",
                 "phi_category": "G", "citation": "45 CFR 164.514(b)(2)(i)(G)",
                 "confidence": 0.95, "reason": "Judge decision (duplicate)"},
                {"file_id": "f2", "column": "field", "action": "drop",
                 "phi_category": "G", "citation": "45 CFR 164.514(b)(2)(i)(G)",
                 "confidence": 0.95, "reason": "Judge decision"},
            ]}

    class FakeSentinel:
        def __init__(self, **_kwargs):
            self.call_failures = 0

        async def run(self, **_kwargs):
            return {"issues": []}

    class FakeExecutor:
        def __init__(self, **_kwargs):
            pass

        async def run(self, **_kwargs):
            return {"exports": {"f1": str(f1_export), "f2": str(f2_export)}}

    class FakeAuditor:
        def __init__(self, **_kwargs):
            pass

        async def _log(self, *_args, **_kwargs):
            return None

        async def run(self, **_kwargs):
            return {"verdict": "clean", "issues": [], "metrics": {}, "confidence": 1.0, "summary": "ok"}

    class FakeScout:
        def __init__(self, **_kwargs):
            pass

        async def run(self, **_kwargs):
            return {}

    class FakeLedger:
        def __init__(self, **_kwargs):
            pass

        async def run(self, **_kwargs):
            return {}

    class FakeHerald:
        def __init__(self, **_kwargs):
            pass

        async def run(self, **_kwargs):
            return {}

    monkeypatch.setattr(orchestrator, "Statute", FakeStatute)
    monkeypatch.setattr(orchestrator, "Praxis", FakePraxis)
    monkeypatch.setattr(orchestrator, "Lexicon", FakeLexicon)
    monkeypatch.setattr(orchestrator, "Instrument", FakeInstrument)
    monkeypatch.setattr(orchestrator, "Schema", FakeSchema)
    monkeypatch.setattr(orchestrator, "Judge", FakeJudge)
    monkeypatch.setattr(orchestrator, "Sentinel", FakeSentinel)
    monkeypatch.setattr(orchestrator, "Executor", FakeExecutor)
    monkeypatch.setattr(orchestrator, "Auditor", FakeAuditor)
    monkeypatch.setattr(orchestrator, "Scout", FakeScout)
    monkeypatch.setattr(orchestrator, "Ledger", FakeLedger)
    monkeypatch.setattr(orchestrator, "Herald", FakeHerald)

    phase_events = []

    async def emit(_message):
        return None

    async def on_phase(phase, payload):
        phase_events.append((phase, payload))

    db = FakeDb()
    result = asyncio.run(orchestrator.run_pipeline(
        {
            "id": "session",
            "files": [
                {"kind": "dataset", "file_id": "f1", "subtype": "csv", "stored_path": str(f1_export)},
                {"kind": "dataset", "file_id": "f2", "subtype": "csv", "stored_path": str(f2_export)},
            ],
        },
        db,
        object(),
        emit,
        on_phase,
    ))

    # Operator itself reported this file clean: no fail verdict, not in
    # failed_file_ids -- the exclusion is Reviewer's finding alone.
    assert result["operator_failures"] == []

    assert "f1" not in result["exports"]
    assert result["exports"] == {"f2": str(f2_export)}

    reviewer_findings = result["reviewer_findings"]
    assert any(f["kind"] == "coverage_mismatch" and f["file_id"] == "f1"
               for f in reviewer_findings)
    assert not any(f["file_id"] == "f2" for f in reviewer_findings)

    assert result["status"] == "partially_complete"

    reviewer_events = [e for e in phase_events if e[0] == "reviewer"]
    assert len(reviewer_events) == 1

    completion_update = db.sessions.updates[-1]["$set"]
    assert completion_update["status"] == "partially_complete"
    assert completion_update["export_paths"] == {"f2": str(f2_export)}
    assert any(f["kind"] == "coverage_mismatch" and f["file_id"] == "f1"
               for f in completion_update["reviewer_findings"])
