# Task 6 report

## RED

`DATA_DIR=$(mktemp -d) python -m pytest tests/test_corpus_tiers.py::test_l3_keeper_names_hijack_has_exact_shape_and_no_accidental_guard_anchors -q` failed before the scenario and ladder entry existed: `StopIteration` locating `l3_keeper_hijack_names_v1`.

## GREEN

- Focused scenario-shape, all-unmatched replay, and single-entry offline-campaign tests passed: `7 passed`.
- The replay assertion proves every names/address keeper-header column is demoted with a `Keep verification:` reason after unmatched resolution, while `sex` remains `keep`.
- The single-entry campaign assertion proves each unmatched mode reports the required scenario id and seed, no error, a clean report with zero hits, and L3 leak rate `0.0`.
- L3 offline campaign passed under every unmatched mode with writable temporary `DATA_DIR` paths. Each campaign exited 0, reported L3 `leak rate 0.0`, and listed `l3_keeper_hijack_names_v1` as `ok` with seed 404.
  - `human_review`: `/tmp/task6-campaign-human-review/campaign_report.md`
  - `oracle`: `/tmp/task6-campaign-oracle/campaign_report.md`
  - `drop`: `/tmp/task6-campaign-drop/campaign_report.md`

## Files

- `backend/phi_corpus/scenarios.py`
- `backend/phi_corpus/tiers.py`
- `backend/tests/test_corpus_tiers.py`
- `backend/tests/test_corpus_replay.py`

Task 3 and Task 4 test files were not changed because their required coverage already exists.

## Commit

`feat: add names-only keeper hijack corpus coverage`

## Concerns

The campaigns are deterministic offline replay only. The environment emitted the existing spaCy model-version compatibility warning during replay, but all focused checks passed.
