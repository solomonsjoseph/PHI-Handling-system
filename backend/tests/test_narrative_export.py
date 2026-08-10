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

from phi_core.agents.llm import LlmConfig
from phi_core.agents.reasoning import Executor
from phi_core.publish_guard import scan_all_exports


class _StubDB:
    """Same shape as test_speed_and_ux.py's _StubDB, with agent_log added:
    Executor._log awaits ``self.db.agent_log.insert_one(...)`` on every step."""

    def __init__(self):
        self.agent_log = self

    async def insert_one(self, *_a, **_kw):
        return None


def test_narrative_redaction_extracts_and_redacts_real_text(tmp_path, monkeypatch):
    monkeypatch.setattr("phi_core.agents.reasoning.EXPORT_DIR", tmp_path)
    src = tmp_path / "consent.txt"
    src.write_text(
        "Consent obtained from James Smith, call 415-555-1234, MRN-12345678.",
        encoding="utf-8",
    )
    executor = Executor(
        session_id="t1", llm=LlmConfig.from_dict({"model": "x"}), db=_StubDB(), emit=None,
    )
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


def test_executor_dataset_output_survives_publish_guard(tmp_path, monkeypatch):
    """Executor's dataset export, run through the widened step-5 pattern
    set, must still come out 'clean' for a properly-decided study export."""
    monkeypatch.setattr("phi_core.agents.reasoning.EXPORT_DIR", tmp_path)
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
    executor = Executor(
        session_id="t2", llm=LlmConfig.from_dict({"model": "x"}), db=_StubDB(), emit=None,
    )
    out = asyncio.run(executor.run(
        files=[{
            "file_id": "f2", "original_name": "enrollment.csv", "stored_path": str(src),
            "kind": "dataset", "subtype": "csv",
        }],
        decisions=decisions,
    ))
    report = scan_all_exports(out["exports"], decisions=decisions)
    assert report.status == "clean", report.to_dict()
