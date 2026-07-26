# Standalone Pipeline Stress Test Report

Method: every number below is read directly from an actual CLI run's output
or exit code against the intake-manifest/v3 fixtures. Commands are given
verbatim for every claim.

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

`build_stress_fixtures` emits the mandatory intake-manifest/v3 component
package under `$SMOKE_ROOT/source/`, all in accepted formats (manifest:
`$SMOKE_ROOT/source.manifest/stress_manifest.json`, a complete atime-excluding
entry snapshot):

- `datasets/`: `labs.csv`; `batch_2026/site_04/labs_dup.csv` (identical bytes
  to `labs.csv`, in a nested subdirectory -- duplicate content and nested
  directories must BOTH survive intake as distinct entries); `screening.xlsx`
  (single-sheet); `legacy_site.xls` (a genuine legacy `.xls` via `xlwt` when
  installable, else a mislabeled-`.xls` fallback fixture); and `site_notes.csv`
  planting SSN-shaped values under a `NOTES` header and phone-shaped values
  under `COMMENT`.
- `forms/`: `consent_table.pdf` (an embedded extractable table) and
  `screening_form.pdf` (no extractable table). `forms/` holds PDFs only and
  never distinguishes annotated from non-annotated documents.
- `dictionary_mapping/`: `dict.csv`, `labs_dup.csv` (header-similar to
  `datasets/labs.csv` but not byte-identical, so it stays a normal kept
  entry rather than tripping the cross-component-dataset-copy quarantine),
  and `site_map.csv`.

Ten regular files total. A separate, deliberately-invalid package
(`build_review_required_fixtures`, seed 43) exercises every fixed intake
review reason and is covered in section 1a.

## 1. Intake (ready package)

```
intake exit=0
```

Redacted receipt:

```json
{"study": "RemovalSmoke", "status": "ready", "linked": 10, "review": 0, "errors": 0, "manifest": ".../workspace/intake/RemovalSmoke/intake_manifest.json"}
```

All ten accepted-format files link cleanly: `status == "ready"`, zero review
items, zero errors. The `intake-manifest/v3` `schema`/`study`/
`study_name_source`/`status`/`source_root`/`entries`/`review_items`/`errors`/
`removals` document records each entry's `artifact_id`, `intake_path`,
`component`, `relative_path`, `original_path`, `sha256`, `size`, `mtime_ns`,
`device`, `inode`, and `mode`.

Source-immutability: `harness/spec_check.py`'s `source_immutability` check
compares the complete entry set (type, sha256, size, mode, `mtime_ns`, uid,
gid, and symlink target, excluding atime) against the fixture-build-time
manifest -- **PASS**, zero drift.

## 1a. Intake (review-required package, seed 43)

```
intake exit=8
```

Redacted receipt: `{"study": "RRPkg", "status": "review_required", "linked": 8, "review": 6, "errors": 0, ...}`. The six blocking review items map one-to-one
to the fixed v3 preflight reasons:

| Path | Reason |
|---|---|
| `datasets/mystery_export.dat` | `unsupported-format` |
| `datasets/corrupted_workbook.xlsx` | `xlsx-workbook-invalid` |
| `datasets/multi_sheet.xlsx` | `dataset-xlsx-multiple-sheets` |
| `datasets/broken_link.csv` (a source symlink) | `source-symlink-not-allowed` |
| `datasets/extra_group.json` | `unsupported-format` |
| `datasets/demographics.jsonl` | `unsupported-format` |

`.json` and `.jsonl` are NOT accepted dataset formats under v3: they are
demoted to `_unclassified` review items, not normalized. A source symlink
anywhere in the tree is rejected outright rather than followed. Any single
blocking item holds the whole study, so this package is intentionally never
organized or run.

## 2. Organize (ready package)

```
organize exit=0
```

`organize_manifest.json`: **6 datasets produced, 1 in the review bucket**.
Organization is component-authoritative -- the organizer routes each entry
purely by the `component` the intake manifest already assigned
(`_COMPONENT_ROLES`: `datasets -> dataset`, `dictionary_mapping ->
dictionary_mapping`, `forms -> pdf`; `_unclassified` is never parsed). It
does not re-guess a role from the file path or suffix.

Datasets produced (nested and duplicate entries both survive as distinct
outputs): `labs.jsonl` and `labs_dup.jsonl` (the nested duplicate),
`legacy_site__Legacy.jsonl` (from the `.xls`), `screening__Screening.jsonl`
(from the single-sheet `.xlsx`), `site_notes.jsonl`, and
`consent_table__pdftable0.jsonl` (one table extracted from the forms PDF).
Support artifacts: two dictionary CSVs and one mapping CSV.

`pdf_roles`: `consent_table.pdf` -> `table_extracted` (1 table);
`screening_form.pdf` -> `review` with reason `pdf-no-extractable-table`. That
single PDF is the only review-bucket entry, recording filename, link name, and
reason -- never row values.

## 3. Run -- partial exit, zero leak

```
run exit=8
```

`pipeline_result.json`: `exit_code: 8`, `forms_held: []`, `forms_processed`:
the 6 datasets above, `organizer_review_count: 1`, `dependency_review_count:
16`, `review_queue_size: 17`, `profile_escalations: 0`,
`profile_auto_clears: 0`, `guard_ok: true`, `guard_failed: false`,
`published_count: 6`, `scrub_raised: null`. There is no automatic
source-of-truth generation step in `run_pipeline` and therefore no
`sot_generation_error` field: standalone SoT is available only to callers that
explicitly maintain the legacy annotated-PDF layout (see section 3a).

`site_notes.jsonl`'s published output contains neither a `NOTES` nor a
`COMMENT` key (both are `usa_free_text_suppression`-classified,
`action: suppress`, force-dropped by `phi_scrub.run_scrub`'s SUPPRESS
dual-path). `exit_code == 8` is exactly the "held/review present by
construction" outcome the fixtures guarantee (one organizer review-bucket
entry plus sixteen dependency recommendations awaiting decision).

Planted-identifier check:
`grep -rlE '[0-9]{3}-[0-9]{2}-[0-9]{4}' $WS/output/RemovalSmoke/llm_source/`
-> exit 1 (zero matching files). No SSN-shaped planted identifier reached the
published tree.

## 3a. No automatic source-of-truth generation

`run_pipeline` reads SoT variable signals when a caller has explicitly
maintained an SoT tree (`load_sot_variable_signals`), but it never generates
one automatically. This is deliberate: the undifferentiated v3 `forms/`
contract cannot prove any PDF is an annotated CRF, so the pipeline does not
synthesize an SoT from it. Standalone SoT remains available to callers that
explicitly maintain its legacy annotated-PDF layout.

## 4. Review and status

```
review list exit=0
status exit=0
```

`review list` returns `organizer_review_bucket` of size 1,
`dependency_recommendations` of 16, and empty `held_forms`,
`llm_uncertain_queue`, and `intake_review_items`. `status` reports the single
completed run and echoes `pipeline_result.json` (`exit_code: 8`) as
`latest_result`.

## 5. LLM boundary

`tests/test_stress_standalone.py` covers this invariant: a monkeypatched
call-recording spy over a full stress intake->organize->run pass records
**zero calls** -- the default deterministic classification path
(`phi_review.review_form_headers` with no `aligner=` injected) never
constructs an LLM prompt at all. Separately, a real
`LLMClient(provider="ollama", ...).complete(...)` call contaminated with a
planted SSN value raises `PHIEgressBlockedError` without echoing the planted
value in the exception message. Intake's own optional study-name inference is
the only sanctioned local-LLM path and is exercised only when `--study` is
omitted with `--support-confirmed-no-phi`; the deterministic runs above never
reach it.

## 6. spec_check against the full stress run

```
[PASS] intake_symlink_invariant
[PASS] llm_boundary_canary
[PASS] source_immutability
ALL PASS
```

- `intake_symlink_invariant`: every entry under `<workspace>/intake/<study>/`
  is a symlink or the `intake_manifest.json` bookkeeping file, and the intake
  root, study directory, and each component directory
  (`datasets`/`forms`/`dictionary_mapping`/`_unclassified`) are
  `lstat`-checked and rejected if any is itself a symlink.
- `llm_boundary_canary`: `llm.provider` default `none`; zero `get_llm_client()`
  calls outside the `llm_detector`/`phi_alignment` exemption under
  `phi_engine/pipeline/`; and `model_routing.new_offline_local_client()` --
  the sole sanctioned local-LLM factory for intake study naming -- is
  referenced ONLY as the callee of the one sanctioned call inside
  `intake_naming.resolve_intake_study`/`_resolve_intake_study`. Any alias
  import, any other callsite, or any direct `OfflineLocalLLMClient(...)`
  construction under `phi_engine/pipeline/` is a violation.
- `source_immutability`: every stress-source entry matches its
  fixture-build-time snapshot across the full comparison field set after the
  complete intake+organize+run sequence.

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

- `.xls` fail-closed routing (a mislabeled or unreadable workbook lands in the
  review bucket) is implemented but not exercised by the ready package when
  `xlwt` is installed and the fixture's `.xls` is a genuine legacy workbook.
  The mislabeled-`.xls` fallback fixture and the review-required package's
  invalid workbooks cover the fail-closed paths.
