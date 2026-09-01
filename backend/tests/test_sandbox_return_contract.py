"""Rewrite plan step 4: the enforced ``return_kind`` contract on
``control.sandbox.run_isolated`` -- every sandboxed worker must declare
``"path"``/``"count"``/``"status"``/``"json"`` up front, and the child's
return value is validated against it (once in the child before
``queue.put``, once again in the parent after ``queue.get``) rather than
merely type-checked against ``(str, int, float, bool, None)``.

Required coverage (see ``phi-agent-driven-rewrite-plan.md`` step 4 and its
Verification section, item 11): a worker returning ``df.to_csv()`` is
blocked; a path outside the workspace is rejected; a data-bearing
exception never reaches the parent as free text.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pandas as pd
import pytest
from phi_core.control.sandbox import (
    SandboxError,
    SandboxMissingOutputError,
    SandboxPathViolation,
    SandboxReturnContractViolation,
    SandboxWorkerFailure,
    create_sandbox,
    destroy_sandbox,
    get_sandbox_error_detail,
    run_isolated,
)


@pytest.fixture(autouse=True)
def _allow_unenforced_sandbox_memory(monkeypatch):
    # Darwin/XNU cannot enforce RLIMIT_AS at any finite value (CPython
    # issue 78783); every test here needs a real sandbox, not this
    # module's own concern.
    monkeypatch.setenv("PHI_SANDBOX_ALLOW_UNENFORCED_MEMORY", "1")


def _run_id() -> str:
    return uuid4().hex


# ---------------------------------------------------------------------------
# return_kind="path"
# ---------------------------------------------------------------------------


def _write_and_return_filename(workspace_path: str, name: str, content: str) -> str:
    (Path(workspace_path) / name).write_text(content, encoding="utf-8")
    return name


def test_path_kind_accepts_a_real_workspace_artifact_the_worker_wrote():
    record = create_sandbox(_run_id())
    try:
        name = run_isolated(
            record, _write_and_return_filename, record.workspace_path, "out.txt", "hello",
            return_kind="path",
        )
        assert name == "out.txt"
        assert (Path(record.workspace_path) / name).read_text(encoding="utf-8") == "hello"
    finally:
        destroy_sandbox(record)


def _return_traversal_path() -> str:
    return "../../../etc/passwd"


def test_path_kind_rejects_a_path_that_escapes_the_workspace():
    """Required scenario: a path outside the workspace is rejected."""
    record = create_sandbox(_run_id())
    try:
        with pytest.raises(SandboxPathViolation) as excinfo:
            run_isolated(record, _return_traversal_path, return_kind="path")
    finally:
        destroy_sandbox(record)
    # The rejection message must never embed the candidate value itself --
    # otherwise the rejection becomes a second way for content to leak.
    assert "/etc/passwd" not in str(excinfo.value)


def _claim_a_file_never_written() -> str:
    return "never_actually_written.json"


def test_path_kind_rejects_a_resolvable_path_whose_file_was_never_written():
    record = create_sandbox(_run_id())
    try:
        with pytest.raises(SandboxMissingOutputError):
            run_isolated(record, _claim_a_file_never_written, return_kind="path")
    finally:
        destroy_sandbox(record)


def _return_dataframe_to_csv_dump() -> str:
    """The concrete leak rewrite plan step 4 exists to close: a worker
    handing back an entire dataset's rendered bytes instead of a real
    workspace artifact path."""
    df = pd.DataFrame({
        "patient_name": [f"Patient-{i} Doe-Smith-Johnson-Williams" for i in range(20)],
        "ssn": [f"{100 + i:03d}-45-{6000 + i:04d}" for i in range(20)],
        "diagnosis": ["confidential clinical narrative text " * 3 for _ in range(20)],
    })
    return df.to_csv()


def test_path_kind_rejects_dataframe_to_csv_dump():
    """Required scenario: a worker returning df.to_csv() is blocked."""
    record = create_sandbox(_run_id())
    try:
        with pytest.raises(SandboxReturnContractViolation) as excinfo:
            run_isolated(record, _return_dataframe_to_csv_dump, return_kind="path")
    finally:
        destroy_sandbox(record)
    message = str(excinfo.value)
    assert "Patient-0 Doe-Smith-Johnson-Williams" not in message
    assert "100-45-6000" not in message
    assert "confidential clinical narrative" not in message


def test_path_kind_rejects_dataframe_to_csv_dump_even_declared_as_json():
    """The same leak, declared under a different return_kind, must still
    fail: CSV text (unquoted commas/newlines) is not valid JSON."""
    record = create_sandbox(_run_id())
    try:
        with pytest.raises(SandboxReturnContractViolation) as excinfo:
            run_isolated(record, _return_dataframe_to_csv_dump, return_kind="json")
    finally:
        destroy_sandbox(record)
    assert "Patient-0" not in str(excinfo.value)


def _return_oversized_path_string() -> str:
    return "a" * 600


def test_path_kind_rejects_string_over_the_length_cap():
    record = create_sandbox(_run_id())
    try:
        with pytest.raises(SandboxReturnContractViolation):
            run_isolated(record, _return_oversized_path_string, return_kind="path")
    finally:
        destroy_sandbox(record)


# ---------------------------------------------------------------------------
# return_kind="count"
# ---------------------------------------------------------------------------


def _return_int(n: int) -> int:
    return n


def test_count_kind_accepts_a_plain_int():
    record = create_sandbox(_run_id())
    try:
        assert run_isolated(record, _return_int, 31, return_kind="count") == 31
    finally:
        destroy_sandbox(record)


def _return_bool() -> bool:
    return True


def test_count_kind_rejects_bool_even_though_bool_is_an_int_subclass():
    record = create_sandbox(_run_id())
    try:
        with pytest.raises(SandboxReturnContractViolation):
            run_isolated(record, _return_bool, return_kind="count")
    finally:
        destroy_sandbox(record)


def _return_numeric_string() -> str:
    return "31"


def test_count_kind_rejects_a_numeric_string():
    record = create_sandbox(_run_id())
    try:
        with pytest.raises(SandboxReturnContractViolation):
            run_isolated(record, _return_numeric_string, return_kind="count")
    finally:
        destroy_sandbox(record)


# ---------------------------------------------------------------------------
# return_kind="status"
# ---------------------------------------------------------------------------


def _return_status_token() -> str:
    return "clean"


def test_status_kind_accepts_a_short_lowercase_token():
    record = create_sandbox(_run_id())
    try:
        assert run_isolated(record, _return_status_token, return_kind="status") == "clean"
    finally:
        destroy_sandbox(record)


def _return_free_text_status() -> str:
    return "the patient's visit on 1958-03-14 was flagged"


def test_status_kind_rejects_free_text():
    record = create_sandbox(_run_id())
    try:
        with pytest.raises(SandboxReturnContractViolation) as excinfo:
            run_isolated(record, _return_free_text_status, return_kind="status")
    finally:
        destroy_sandbox(record)
    assert "1958-03-14" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# return_kind="json"
# ---------------------------------------------------------------------------


def _return_json_summary() -> str:
    return json.dumps({"columns": ["age", "sex"], "row_count": 10})


def test_json_kind_accepts_valid_json_and_round_trips():
    record = create_sandbox(_run_id())
    try:
        raw = run_isolated(record, _return_json_summary, return_kind="json")
    finally:
        destroy_sandbox(record)
    assert json.loads(raw) == {"columns": ["age", "sex"], "row_count": 10}


def _return_non_json_text() -> str:
    return "not json at all, just plain text"


def test_json_kind_rejects_non_json_text():
    record = create_sandbox(_run_id())
    try:
        with pytest.raises(SandboxReturnContractViolation):
            run_isolated(record, _return_non_json_text, return_kind="json")
    finally:
        destroy_sandbox(record)


def _return_oversized_json() -> str:
    return json.dumps("x" * 200_000)


def test_json_kind_rejects_payload_over_the_size_cap():
    record = create_sandbox(_run_id())
    try:
        with pytest.raises(SandboxReturnContractViolation):
            run_isolated(record, _return_oversized_json, return_kind="json")
    finally:
        destroy_sandbox(record)


# ---------------------------------------------------------------------------
# Every kind rejects a raw, non-string/int object outright
# ---------------------------------------------------------------------------


def _return_raw_row_dicts():
    return [{"patient_name": "Amelia Cross", "ssn": "555-19-2231"}]


@pytest.mark.parametrize("kind", ["path", "count", "status", "json"])
def test_every_kind_rejects_a_raw_row_shaped_object(kind):
    record = create_sandbox(_run_id())
    try:
        with pytest.raises(SandboxReturnContractViolation) as excinfo:
            run_isolated(record, _return_raw_row_dicts, return_kind=kind)
    finally:
        destroy_sandbox(record)
    message = str(excinfo.value)
    assert "Amelia Cross" not in message
    assert "555-19-2231" not in message


def test_run_isolated_rejects_an_unknown_return_kind_up_front():
    record = create_sandbox(_run_id())
    try:
        with pytest.raises(SandboxError):
            run_isolated(record, _return_int, 1, return_kind="row")  # type: ignore[arg-type]
    finally:
        destroy_sandbox(record)


# ---------------------------------------------------------------------------
# A data-bearing exception never reaches the parent as free text.
# ---------------------------------------------------------------------------


def _raise_with_row_shaped_message():
    raise ValueError(
        "row parse failed for Amelia Cross, SSN 555-19-2231, "
        "DOB 1972-11-03, MRN MR7743211"
    )


def test_worker_exception_never_forwards_free_text_but_detail_ref_recovers_a_scrubbed_copy():
    """Required scenario: a data-bearing exception never reaches the
    parent as free text. The structured SandboxWorkerFailure's own
    str()/diagnostic never embed the message; the scrubbed detail is
    reachable only through the explicit get_sandbox_error_detail escape
    hatch, keyed by an opaque ref -- never automatically."""
    record = create_sandbox(_run_id())
    try:
        with pytest.raises(SandboxWorkerFailure) as excinfo:
            run_isolated(record, _raise_with_row_shaped_message, return_kind="status")
    finally:
        destroy_sandbox(record)
    exc = excinfo.value
    top_level = str(exc)
    assert "Amelia Cross" not in top_level
    assert "555-19-2231" not in top_level
    assert exc.diagnostic["kind"] == "ValueError"
    assert exc.diagnostic["code"] == "runtime_error"
    assert "Amelia Cross" not in json.dumps(exc.diagnostic)

    detail = get_sandbox_error_detail(exc.diagnostic["detail_ref"])
    assert detail is not None
    # scrub_persisted_text redacts the known PHI shapes it recognizes
    # (SSN, date, MRN); the name is not one of its recognized shapes
    # here (two-token Title Case is, but "Amelia Cross" alone should be
    # caught by the two-token name detector) -- assert the detail record
    # exists and the strongest identifiers are gone, without depending
    # on exactly which detector caught which token.
    assert "555-19-2231" not in detail
    assert "1972-11-03" not in detail
    assert "MR7743211" not in detail


def test_unknown_detail_ref_returns_none_not_an_error():
    assert get_sandbox_error_detail("not-a-real-ref") is None


# ---------------------------------------------------------------------------
# Timeout still classifies as a structured worker failure (code="timeout").
# ---------------------------------------------------------------------------


def _spin_forever() -> None:
    while True:
        pass


def test_timeout_is_a_structured_worker_failure_with_the_timeout_code():
    from phi_core.control.sandbox import SandboxTimeout

    record = create_sandbox(_run_id(), max_wall_seconds=2)
    try:
        with pytest.raises(SandboxTimeout) as excinfo:
            run_isolated(record, _spin_forever, return_kind="status")
    finally:
        destroy_sandbox(record)
    assert excinfo.value.diagnostic["code"] == "timeout"


# ---------------------------------------------------------------------------
# Child-controlled PATH / absent HOME (step 4's env-allowlist narrowing).
# ---------------------------------------------------------------------------


def _snapshot_path_and_home() -> str:
    return json.dumps({"PATH": os.environ.get("PATH"), "HOME": os.environ.get("HOME")})


def test_child_gets_a_fixed_controlled_path_and_no_home():
    record = create_sandbox(_run_id())
    try:
        raw = run_isolated(record, _snapshot_path_and_home, return_kind="json")
    finally:
        destroy_sandbox(record)
    snapshot = json.loads(raw)
    # Never the parent process's own PATH (arbitrarily wide, deployment-
    # specific) -- a fixed value this module controls instead.
    assert snapshot["PATH"] == "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    assert snapshot["PATH"] != os.environ.get("PATH")
    assert snapshot["HOME"] is None
