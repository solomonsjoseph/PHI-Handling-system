# Standalone Pipeline Stress Test Report

Method: every number below is read directly from an actual CLI run's output
or exit code. Commands are given verbatim for every claim.

## Commands run

```bash
SMOKE_ROOT="$(mktemp -d)"
python -m harness.make_stress_fixtures --out "$SMOKE_ROOT/source" --seed 42
python -m phi_engine intake   --study RemovalSmoke --source "$SMOKE_ROOT/source" --workspace "$SMOKE_ROOT/workspace"
python -m phi_engine organize --study RemovalSmoke --workspace "$SMOKE_ROOT/workspace"
python -m phi_engine run      --study RemovalSmoke --jurisdiction us --workspace "$SMOKE_ROOT/workspace"
python -m phi_engine review   --study RemovalSmoke --workspace "$SMOKE_ROOT/workspace" list
python -m phi_engine status   --study RemovalSmoke --workspace "$SMOKE_ROOT/workspace"
python -m harness.spec_check --workspace "$SMOKE_ROOT/workspace" --study RemovalSmoke --skip-pytest \
  --source-manifest "$SMOKE_ROOT/source.manifest/stress_manifest.json"
```

## Fixture tree (`harness/make_stress_fixtures.py`, seed 42)

15 regular files under `$SMOKE_ROOT/source/` (manifest:
`$SMOKE_ROOT/source.manifest/stress_manifest.json`), covering: nested
folders, a clean CRF-shaped xlsx, a csv, a jsonl, a genuine legacy `.xls`
(`legacy_site.xls`), a PDF with an embedded table, an annotated CRF PDF
matching a dataset stem, a malformed (truncated) xlsx, three roster `.jsonl`
duplicates (same content, different paths) plus one same-name/different-content
conflict, a broken symlink (`vanished_file.jsonl`), an unknown `.dat` file,
and `site_notes.jsonl` planting SSN-shaped values under `NOTES` and
phone-shaped values under `COMMENT`.

## 1. Intake

```
intake exit=0
```

`intake_manifest.json`: **15 entries linked, 0 duplicates recorded, 1
error** —

```json
{"path": ".../source/vanished_file.jsonl", "reason": "broken-symlink-in-source"}
```

Source-immutability: `harness/spec_check.py`'s `source_immutability` check
(re-hashes all manifest files against the intake-time sha256) — **PASS**,
zero drift.

## 2. Organize

```
organize exit=0
```

`organize_manifest.json`: **6 datasets produced, 9 in the review bucket**.
`phi_engine/pipeline/organize.py::_role_for` routes ANY file whose
relative path has more than one path segment (i.e. lives in a nested
subdirectory of the source tree) straight to `review` with reason
`unrecognized-format`, regardless of suffix, UNLESS its top-level
directory is one of `datasets/`, `data_dictionary/`, `mappings/`,
`annotated_pdfs/`, or `forms/`. This run's nested fixtures
(`batch_2026/site_04/1A_Screening.xlsx`, `.../1A_Screening.pdf`,
`.../2_Demographics.jsonl`, and the three `dup_*/roster*.jsonl` copies)
all land in the review bucket for exactly this reason -- NOT because
`.xlsx`/`.pdf`/`.jsonl` are unsupported suffixes; each of those suffixes
IS routed to a dataset/pdf role when the file sits at the source root
(single path segment). `corrupted_workbook.xlsx` (top-level, single
segment) reaches the dataset route and fails there instead
(`excel-open-error`, `BadZipFile`). `mystery_export.dat` (top-level) is a
genuinely unrecognized suffix.

`pdf_roles`: `lab_results_table.pdf` -> `table_extracted` (1 table
extracted into `lab_results_table__pdftable0.jsonl`).

xls/csv/json/jsonl (top-level files) routed to datasets:
`legacy_site__Legacy.jsonl` (from `.xls`), `3_Labs.jsonl` (from `.csv`),
`extra_group.jsonl` (from `.json`), `roster_copy.jsonl` and
`site_notes.jsonl` (from `.jsonl`). The clean `.xlsx` fixture was present
but was NOT exercised through the dataset route in this run -- it sits in
a nested subdirectory, so `_role_for` sent it to the review bucket instead
(see §2 above); XLSX sheet-split/header-promote routing is implemented
(`docs/STANDALONE_SPEC.md` §3) but this specific run did not exercise it.

## 3. Run — partial exit, zero leak

```
run exit=8
```

`pipeline_result.json`: `exit_code: 8`, `forms_held: []`, `forms_processed`:
the 6 datasets listed above, `organizer_review_count: 9`,
`review_queue_size: 9`, `profile_escalations: 0`, `profile_auto_clears: 0`,
`guard_ok: true`, `guard_failed: false`, `published_count: 6`,
`scrub_raised: null`, `sot_generation_error: null`.

`site_notes.jsonl`'s published output contains neither a `NOTES` nor a
`COMMENT` key (both are `usa_free_text_suppression`-classified,
`action: suppress`, force-dropped by `phi_scrub.run_scrub`'s SUPPRESS
dual-path). `exit_code == 8` is exactly the "held/review present by
construction" outcome the messy fixture guarantees (9 organizer
review-bucket entries).

## 4. Review and status

```
review list exit=0
status exit=0
```

`review list` returns the 9-entry `organizer_review_bucket`, empty
`held_forms`, and empty `llm_uncertain_queue`. `status` reports the single
completed run and echoes `pipeline_result.json` as `latest_result`.

## 5. LLM boundary

`tests/test_stress_standalone.py::test_llm_boundary_zero_prompts_in_default_run_and_egress_gate_blocks_contamination`
covers this invariant: a monkeypatched call-recording spy over a full stress
intake->organize->run pass records **zero calls** — the default
deterministic classification path (`phi_review.review_form_headers` with no
`aligner=` injected) never constructs an LLM prompt at all. Separately, a
real `LLMClient(provider="ollama", ...).complete(...)` call contaminated
with a planted SSN value raises `PHIEgressBlockedError` without echoing the
planted value in the exception message.

## 6. spec_check against the full stress run

```
[PASS] intake_symlink_invariant
[PASS] llm_boundary_canary
[PASS] source_immutability
ALL PASS
```

(`llm_boundary_canary`: `llm.provider` default `none`, zero
`get_llm_client()` calls outside the `llm_detector`/`phi_alignment`
exemption under `phi_engine/pipeline/`. `source_immutability`: every
stress-source file sha256-matches its intake-time hash after the full
intake+organize+run sequence.)

## Test coverage

The suites that exercise this same standalone pipeline path
(`tests/test_stress_standalone.py`, `tests/test_phi_engine_integration.py`,
`tests/test_phase3_run_pipeline_integration.py`,
`tests/test_phase3_run_review_integration.py`,
`tests/test_pipeline_lock.py`, `tests/test_phi_llm_safety.py`,
`tests/test_llm_egress_gate.py`) are the focused regression list in
`README.md`'s Verification section; run that command directly for current
pass/fail counts rather than trusting a stale number in this report.

## Known limitations carried into this report

- `.xls` fail-closed routing (mislabeled/unreadable -> review bucket) is
  implemented (`phi_engine/pipeline/organize.py`'s `ImportError` branch) but
  not exercised by this run, because `xlwt` is installed in this
  environment and the fixture's `.xls` is a genuine legacy workbook. The
  code path is present; this run did not need the fallback.
