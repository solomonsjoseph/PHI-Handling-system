# Task 7 report

## Files

- `backend/phi_corpus/generate.py`
- `backend/tests/test_corpus.py`
- `.superpowers/sdd/phi-leak-stress-test-plan/task-7-report.md`

## RED

`DATA_DIR=/tmp/task7-red python -m pytest tests/test_corpus.py::test_campaign_cli_fails_for_leaks_in_non_l0_tiers tests/test_corpus.py::test_campaign_cli_succeeds_when_all_entries_are_clean tests/test_corpus.py::test_campaign_cli_preserves_entry_error_exit_code -q` failed before the gate change: `test_campaign_cli_fails_for_leaks_in_non_l0_tiers` received exit code 0 instead of 2. The other two tests passed, documenting existing clean and entry-error behavior.

## GREEN

- The same focused command passed after the change: `3 passed in 0.58s`.
- `DATA_DIR=/tmp/task7-data python -m phi_corpus.generate --campaign --tier all --offline --jobs 4 --out-dir /tmp/task7-campaign` exited 0 with `errors: 0`.
- `/tmp/task7-campaign/campaign_report.md` records leak rate `0.0` for L0, L1, L2, and L3. Its regulation coverage table reports zero leaked cells for every category.
- Review regression: an L1 entry error and an L3 non-clean leak result exit 1, preserving entry-error precedence. All four focused CLI tests passed: `4 passed in 0.57s`.
- The campaign docstring now describes the ladder without a stale scenario count.

## Commit

`fix: gate campaign leaks across every tier`

## Concerns

The smoke check covers deterministic offline replay only. The environment emitted its existing spaCy model-version compatibility warning during the campaign. No formatters, linters, or broad test suite were run.
