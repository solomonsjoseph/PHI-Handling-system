"""Coverage for Lexicon's deterministic dictionary index (Task 9) and its
grounded per-column ``answer()`` (Task 10).

Follows the dependency-free convention of test_manager.py: plain
``def test_...()`` driving coroutines with ``asyncio.run(...)``, no live LLM
key, no Mongo, agents built directly with ``llm=None`` and a fake db.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import phi_core.agents.specialists as specialists

# ---- shared fakes, following test_manager.py:20-39 -------------------------


class FakeAgentLog:
    def __init__(self):
        self.inserted: list[dict] = []

    async def insert_one(self, doc, *_args, **_kwargs):
        self.inserted.append(doc)


class FakeDb:
    def __init__(self):
        self.agent_log = FakeAgentLog()


def _write_csv_dictionary(tmp_path: Path, n: int) -> Path:
    path = tmp_path / "dictionary.csv"
    lines = ["column_name,description"]
    for i in range(n):
        lines.append(f"col_{i:02d},Describes field number {i}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---- Task 9: deterministic row index ---------------------------------------


class _DropCountStubLexicon(specialists.Lexicon):
    """Stub that answers every chunk it is asked about, except the last
    ``drop_count`` rows it is ever shown -- simulating a short LLM reply
    without touching the deterministic row-parsing path at all."""

    def __init__(self, *a, drop_count: int = 0, **kw):
        super().__init__(*a, **kw)
        self._drop_budget = drop_count
        self.call_count = 0

    async def call_json(self, prompt, phase, default=None, *, expect_key=None,
                        min_items=0, status_text="", **_kw):
        self.call_count += 1
        marker = "Dictionary rows in this batch:\n"
        start = prompt.index(marker) + len(marker)
        end = prompt.index("\nRespond with JSON only", start)
        batch = ast.literal_eval(prompt[start:end])
        gists = []
        for item in batch:
            if self._drop_budget > 0:
                self._drop_budget -= 1
                continue
            gists.append({
                "name": item["name"],
                "gist": f"gist for {item['name']}",
                "phi_flag_hint": False,
                "clinical_utility": "medium",
            })
        return {"gists": gists}


def test_lexicon_indexes_every_row_even_when_llm_returns_fewer_gists(tmp_path):
    """Direct regression for the dropped-column bug: row extraction is
    structural, so a short LLM reply can only leave gists blank, never lose
    a documented column."""
    path = _write_csv_dictionary(tmp_path, 30)
    db = FakeDb()
    lex = _DropCountStubLexicon(session_id="s", llm=None, db=db, drop_count=4)
    dict_files = [{"file_id": "f1", "original_name": "dictionary.csv",
                  "stored_path": str(path)}]

    result = asyncio.run(lex.run(dict_files=dict_files))

    assert len(result["columns"]) == 30
    assert len(lex._notes) == 30
    assert {c["name"] for c in result["columns"]} == {f"col_{i:02d}" for i in range(30)}

    missing = [c for c in result["columns"] if c["description"] == ""]
    assert len(missing) == 4

    missing_logs = [d for d in db.agent_log.inserted if d["phase"] == "lexicon.gist_missing"]
    assert len(missing_logs) == 4
    assert {d["payload"]["column"] for d in missing_logs} == {c["name"] for c in missing}

    # chunking actually happened (30 rows, _GIST_CHUNK_SIZE=20 -> 2 calls)
    assert lex.call_count == 2


def test_notes_holds_only_scrubbed_text(tmp_path):
    path = tmp_path / "dictionary.csv"
    path.write_text(
        "column_name,description\n"
        'ssn,"Social Security number, e.g. 123-45-6789 on the paper form."\n',
        encoding="utf-8",
    )
    db = FakeDb()
    lex = _DropCountStubLexicon(session_id="s", llm=None, db=db)
    dict_files = [{"file_id": "f1", "original_name": "dictionary.csv",
                  "stored_path": str(path)}]

    asyncio.run(lex.run(dict_files=dict_files))

    note = lex._notes["ssn"]
    assert set(note.keys()) == {"name", "raw_row", "gist", "phi_flag_hint", "clinical_utility"}
    assert "123-45-6789" not in note["raw_row"]
    assert "REDACTED" in note["raw_row"]


def test_broadcast_shape_matches_what_judge_consumes(tmp_path):
    path = _write_csv_dictionary(tmp_path, 3)
    db = FakeDb()
    lex = _DropCountStubLexicon(session_id="s", llm=None, db=db)
    dict_files = [{"file_id": "f1", "original_name": "dictionary.csv",
                  "stored_path": str(path)}]

    result = asyncio.run(lex.run(dict_files=dict_files))

    assert set(result.keys()) == {"columns", "notes"}
    assert isinstance(result["notes"], str)
    for c in result["columns"]:
        assert set(c.keys()) == {"name", "description", "phi_flag_hint",
                                 "clinical_utility", "notes"}


def test_missing_headers_logged_and_skipped(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    db = FakeDb()
    lex = _DropCountStubLexicon(session_id="s", llm=None, db=db)
    dict_files = [{"file_id": "f1", "original_name": "empty.csv", "stored_path": str(path)}]

    result = asyncio.run(lex.run(dict_files=dict_files))

    assert result == {"columns": [], "notes": ""}
    empty_logs = [d for d in db.agent_log.inserted if d["phase"] == "lexicon.empty:f1"]
    assert len(empty_logs) == 1
    assert lex.call_count == 0  # never asked the LLM about zero rows


def test_blank_name_rows_are_logged_as_one_aggregate_event(tmp_path):
    """Rows with a blank/absent name cell are skipped, but never silently:
    every one of them is named in a single aggregate lexicon.blank_name
    event per file (not one event per row -- a dictionary can carry up to
    Task 5's 5000-row cap), and lexicon.parsed's raw-vs-indexed counts
    account for the gap."""
    path = tmp_path / "dictionary.csv"
    path.write_text(
        "column_name,description\n"
        "study_id,Study identifier.\n"
        ",Orphaned description with no column name.\n"
        "last_name,Patient's legal last name.\n"
        ",Another orphaned row.\n"
        "first_name,Patient's legal first name.\n",
        encoding="utf-8",
    )
    db = FakeDb()
    lex = _DropCountStubLexicon(session_id="s", llm=None, db=db)
    dict_files = [{"file_id": "f1", "original_name": "dictionary.csv",
                  "stored_path": str(path)}]

    result = asyncio.run(lex.run(dict_files=dict_files))

    assert {c["name"] for c in result["columns"]} == {"study_id", "last_name", "first_name"}
    assert len(lex._notes) == 3

    blank_logs = [d for d in db.agent_log.inserted if d["phase"] == "lexicon.blank_name"]
    assert len(blank_logs) == 1  # one aggregate event, not one per blank row
    assert blank_logs[0]["payload"] == {
        "file_id": "f1", "reason": "blank_name", "count": 2, "row_indices": [1, 3],
    }

    parsed_logs = [d for d in db.agent_log.inserted if d["phase"] == "lexicon.parsed:f1"]
    assert len(parsed_logs) == 1
    assert parsed_logs[0]["payload"]["raw_row_count"] == 5
    assert parsed_logs[0]["payload"]["indexed_row_count"] == 3


def test_no_blank_name_event_when_every_row_is_named(tmp_path):
    """A dictionary with no blank-name rows emits zero lexicon.blank_name
    events -- the aggregate event only fires when there is something to
    report."""
    path = _write_csv_dictionary(tmp_path, 3)
    db = FakeDb()
    lex = _DropCountStubLexicon(session_id="s", llm=None, db=db)
    dict_files = [{"file_id": "f1", "original_name": "dictionary.csv",
                  "stored_path": str(path)}]

    asyncio.run(lex.run(dict_files=dict_files))

    blank_logs = [d for d in db.agent_log.inserted if d["phase"] == "lexicon.blank_name"]
    assert blank_logs == []


# ---- Task 10: answer() ------------------------------------------------------


def test_answer_absent_column_returns_not_in_dictionary_without_llm_call(tmp_path):
    path = _write_csv_dictionary(tmp_path, 3)
    db = FakeDb()
    lex = _DropCountStubLexicon(session_id="s", llm=None, db=db)
    dict_files = [{"file_id": "f1", "original_name": "dictionary.csv",
                  "stored_path": str(path)}]
    asyncio.run(lex.run(dict_files=dict_files))
    calls_before = lex.call_count

    reply = asyncio.run(lex.answer("not_a_real_column", "it is a free-text note", "no basis"))

    assert reply["verdict"] == "not_in_dictionary"
    assert "not_a_real_column" in reply["explanation"]
    assert reply["citation"] == ""
    assert lex.call_count == calls_before  # no LLM call for an absent column


def test_answer_grounds_correction_only_in_that_columns_row(tmp_path):
    path = tmp_path / "dictionary.csv"
    path.write_text(
        "column_name,description\n"
        "zip_code,Residential 5-digit ZIP code.\n"
        "diagnosis_code,ICD-10-CM tuberculosis diagnosis code.\n",
        encoding="utf-8",
    )
    db = FakeDb()

    captured: dict = {}

    class _AnswerStubLexicon(specialists.Lexicon):
        async def call_json(self, prompt, phase, default=None, *, expect_key=None,
                            min_items=0, status_text="", **_kw):
            captured["prompt"] = prompt
            captured["phase"] = phase
            return {"verdict": "corrected",
                    "explanation": "the dictionary describes this as a ZIP code, not a name",
                    "citation": "45 CFR 164.514(b)(2)(i)(F)"}

    lex = _AnswerStubLexicon(session_id="s", llm=None, db=db)
    dict_files = [{"file_id": "f1", "original_name": "dictionary.csv",
                  "stored_path": str(path)}]
    asyncio.run(lex.run(dict_files=dict_files))

    reply = asyncio.run(lex.answer("zip_code", "this column holds a patient's full name",
                                   "the header looked name-like"))

    assert reply["verdict"] == "corrected"
    assert "ZIP" in reply["explanation"]
    assert reply["citation"] == "45 CFR 164.514(b)(2)(i)(F)"
    # grounded only in zip_code's own row -- diagnosis_code's row never appears
    assert "ZIP code" in captured["prompt"]
    assert "diagnosis" not in captured["prompt"].lower()
