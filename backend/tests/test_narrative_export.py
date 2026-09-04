"""Regression test for the narrative-export defect fixed in step 1b:
before the fix, ``Executor.run`` read a ``.fulltext.txt`` sidecar that only
the deleted old-flow ``pipeline.ingest_file`` ever wrote, so every
``forms/`` narrative file was exported as an empty ``.redacted.txt``. This
file proves the fix end to end, LLM-free and Mongo-free (no
``pytest-asyncio`` in requirements.txt, so each test drives its own
coroutine with ``asyncio.run``).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from phi_core.agents.reasoning import Executor
from phi_core.control.testing import make_ctx
from phi_core.publish_guard import scan_all_exports


def test_narrative_redaction_extracts_and_redacts_real_text(tmp_path, monkeypatch):
    src = tmp_path / "consent.txt"
    src.write_text(
        "Consent obtained from James Smith, call 415-555-1234, MRN-12345678.",
        encoding="utf-8",
    )
    executor = Executor(make_ctx("Executor"))
    out = asyncio.run(executor.run(
        files=[{
            "file_id": "f1", "original_name": "consent.txt", "stored_path": str(src),
            "kind": "narrative", "subtype": "txt",
        }],
        decisions=[],
    ))
    dst = Path(out["exports"]["f1"])
    text = dst.read_text(encoding="utf-8")
    assert text.strip(), "narrative export must not be empty (the defect this fix closes)"
    assert "[REDACTED:" in text, f"expected a HIPAA-category redaction tag, got: {text!r}"
    assert "415-555-1234" not in text
    assert "MRN-12345678" not in text


def test_executor_dataset_output_survives_publish_guard(tmp_path, monkeypatch, stub_executor_dataset_codegen):
    """Executor's dataset export, run through the widened step-5 pattern
    set, must still come out 'clean' for a properly-decided study export."""
    src = tmp_path / "enrollment.csv"
    src.write_text(
        "patient_name,dob,zip,age,notes\r\n"
        "James Smith,1975-03-15,94103,95,Follow-up scheduled\r\n",
        encoding="utf-8",
    )
    decisions = [
        {"file_id": "f2", "column": "patient_name", "action": "drop", "hipaa_category": "A"},
        {"file_id": "f2", "column": "dob", "action": "year_only", "hipaa_category": "C"},
        {"file_id": "f2", "column": "zip", "action": "zip3_truncate", "hipaa_category": "B"},
        {"file_id": "f2", "column": "age", "action": "cap_age_90", "hipaa_category": "C"},
        {"file_id": "f2", "column": "notes", "action": "scrub_text", "hipaa_category": "R"},
    ]
    executor = Executor(make_ctx("Executor"))
    out = asyncio.run(executor.run(
        files=[{
            "file_id": "f2", "original_name": "enrollment.csv", "stored_path": str(src),
            "kind": "dataset", "subtype": "csv",
        }],
        decisions=decisions,
    ))
    report = scan_all_exports(out["exports"], decisions=decisions)
    assert report.status == "clean", report.to_dict()


def test_executor_dataset_export_survives_a_simulated_cross_device_move(tmp_path, monkeypatch):
    """The transformed dataset output is written straight into the staged
    artifact path (``destination`` under ``STAGING_DIR``, ``DATA_DIR``-
    rooted) with no temp-file hop into shared system ``/tmp`` first, so no
    cross-filesystem rename ever happens -- Docker's ``/tmp`` and a bind-
    mounted ``/app/data`` commonly sit on different mounts, and the retired
    ``mkstemp`` + ``Path.replace`` shape raised EXDEV there.

    The patch below is a guard, not the thing under test: it refuses any
    rename/replace whose source lives under the system temp directory, so
    if anyone reintroduces a temp-dir source file into the export path the
    run fails loudly. ``ArtifactService.finalize`` promotes the already-
    staged file with its own ``os.replace(tmp_path, final_path)`` inside
    ``STAGING_DIR`` itself (same filesystem), so a blanket patch would
    break the legitimate call and misattribute the failure; the stub below
    also writes into ``destination`` directly rather than using the
    ``stub_executor_dataset_codegen`` fixture, whose reference implementation
    performs its own same-directory ``os.replace`` a blanket patch would hit."""
    import errno
    import os as os_module
    import tempfile as _tempfile

    from phi_core.agents.reasoning import Executor as ExecutorClass

    system_tmp = _tempfile.gettempdir()
    real_rename = os_module.rename
    real_replace = os_module.replace

    def _refuse_if_source_in_system_tmp(real_fn):
        def _wrapped(src, dst, *args, **kwargs):
            if str(src).startswith(system_tmp):
                raise OSError(errno.EXDEV, "Invalid cross-device link (simulated)")
            return real_fn(src, dst, *args, **kwargs)
        return _wrapped

    monkeypatch.setattr(os_module, "rename", _refuse_if_source_in_system_tmp(real_rename))
    monkeypatch.setattr(os_module, "replace", _refuse_if_source_in_system_tmp(real_replace))

    async def _trivial_stub(self, f, file_decisions, omit_columns, real_columns, sandbox, local_opaque, salt, pseudonym_state, destination):
        destination.write_bytes(Path(f["stored_path"]).read_bytes())
        return destination, dict(pseudonym_state), []

    monkeypatch.setattr(ExecutorClass, "_dataset_via_codegen", _trivial_stub)

    src = tmp_path / "enrollment.csv"
    src.write_text("patient_name\r\nJames Smith\r\n", encoding="utf-8")
    executor = Executor(make_ctx("Executor"))
    out = asyncio.run(executor.run(
        files=[{
            "file_id": "f2", "original_name": "enrollment.csv", "stored_path": str(src),
            "kind": "dataset", "subtype": "csv",
        }],
        decisions=[{"file_id": "f2", "column": "patient_name", "action": "keep"}],
    ))
    exported = Path(out["exports"]["f2"])
    assert exported.exists() and exported.stat().st_size > 0, "export must land even when rename()/replace() are unavailable"
    assert exported.read_bytes() == src.read_bytes()
