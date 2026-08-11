# Task 4 report

## Files changed

- `backend/phi_core/agents/reasoning.py`
- `backend/phi_core/intake.py`
- `backend/phi_corpus/replay.py`
- `backend/phi_corpus/verify.py`
- `backend/tests/test_realworld_file_shapes.py`
- `.superpowers/sdd/phi-leak-stress-test-plan/task-4-report.md`

## RED

Command:

```sh
cd backend && DATA_DIR=/tmp/phi-corpus-task4-data python -m pytest tests/test_realworld_file_shapes.py -q
```

Result: 4 failed, 11 passed. The three new regressions failed before implementation because metadata redaction returned `None`, unsupported metadata kept an unscannable extension, and verifier reading omitted the second worksheet. An existing unrelated test also failed because `phi_core.agents.specialists` no longer exports `_DOCX_XML_MAX_BYTES`.

Review RED command:

```sh
cd backend && DATA_DIR=/tmp/phi-corpus-task4-data python -m pytest tests/test_realworld_file_shapes.py -q -k 'intake_accepted_xls_metadata or executor_publishes_withheld_metadata'
```

Result: 1 failed, 1 passed. The legacy `.xls` metadata test raised openpyxl's unsupported-format exception before the file could be withheld. The Executor return-path test already passed.

## GREEN

Command:

```sh
cd backend && DATA_DIR=/tmp/phi-corpus-task4-data python -m pytest tests/test_realworld_file_shapes.py -q -k 'metadata_xlsx_scrubs_every_sheet or withheld_metadata_uses_scannable_txt_destination or read_export_rows_includes_every_xlsx_worksheet'
```

Result: 3 passed, 12 deselected. One existing spaCy model compatibility warning was emitted.

Review GREEN command:

```sh
cd backend && DATA_DIR=/tmp/phi-corpus-task4-data python -m pytest tests/test_realworld_file_shapes.py -q -k 'metadata_xlsx_scrubs_every_sheet or withheld_metadata_uses_scannable_txt_destination or read_export_rows_includes_every_xlsx_worksheet or intake_accepted_xls_metadata or executor_publishes_withheld_metadata'
```

Result: 5 passed, 12 deselected. The same existing spaCy model compatibility warning was emitted.

Regression command:

```sh
cd backend && DATA_DIR=/tmp/phi-corpus-task4-data python -m pytest tests/test_security_exports_and_zip.py -q
```

Result: 6 passed. The same existing spaCy model compatibility warning was emitted.

## Commit

`fix: make withheld metadata scannable`

## Concerns

The unselected full test module still has the pre-existing `_DOCX_XML_MAX_BYTES` import failure. It is outside Task 4 scope. Dictionary exports remain intentionally excluded from replay scoring.
