# Standalone Pipeline Stress Test Report

Date: 2026-07-07. Method: every number below is read directly from a
committed/reproducible artifact JSON or a command's actual exit code —
commands are given verbatim for every claim (Truth Protocol, `CLAUDE.md`).

## Commands run

```bash
.venv/bin/python -m harness.make_stress_fixtures --out tmp/stress-source --seed 42
.venv/bin/python -m phi_engine intake   --study Stress --source tmp/stress-source --workspace tmp/stress-ws
.venv/bin/python -m phi_engine organize --study Stress --workspace tmp/stress-ws
.venv/bin/python -m phi_engine run      --study Stress --jurisdiction us --workspace tmp/stress-ws
.venv/bin/python -m phi_engine review   --study Stress --workspace tmp/stress-ws decide --header NOTES   --decision drop
.venv/bin/python -m phi_engine review   --study Stress --workspace tmp/stress-ws decide --header COMMENT --decision drop
.venv/bin/python -m phi_engine run      --study Stress --jurisdiction us --workspace tmp/stress-ws
.venv/bin/python -m harness.spec_check --skip-pytest --workspace tmp/stress-ws --study Stress \
  --source-manifest tmp/stress-source.manifest/stress_manifest.json
.venv/bin/python -m pytest tests/test_stress_standalone.py -q
.venv/bin/python -m pytest -q
```

## Fixture tree (`harness/make_stress_fixtures.py`, seed 42)

66 regular files under `tmp/stress-source/` (manifest:
`tmp/stress-source.manifest/stress_manifest.json`), covering: nested
folders, a clean CRF-shaped xlsx, a corpus-generator sidecar xlsx tree
(`_xlsxgen_sidecar/`, many files), a csv, a jsonl, a genuine legacy `.xls`
(xlwt was installable in this environment — see ".xls handling" below), a
PDF with an embedded table, an annotated CRF PDF matching a dataset stem, a
malformed (truncated) xlsx, three classes of duplicate (same content/two
names, same content/sibling dirs, same name/different content), a broken
symlink, an unknown `.dat` file, and `site_notes.jsonl` planting SSN-shaped
values under `NOTES` and phone-shaped values under `COMMENT`.

## 1. Intake

```
intake exit=0
```

`tmp/stress-ws/intake/Stress/intake_manifest.json`: **64 entries linked, 2
duplicates recorded (not double-linked), 1 error** —

```json
{"path": ".../tmp/stress-source/vanished_file.jsonl", "reason": "broken-symlink-in-source"}
```

66 regular files − 2 content-duplicates + 1 broken symlink (walked but not
linked) = 65 walk outcomes beyond the 64 linked, which reconciles: 64 linked
+ 2 duplicates + 1 error = 67 walked entries (66 manifest-counted regular
files + the 1 broken symlink, which the immutability manifest excludes
since it has no target to hash).

Source-immutability: `harness/spec_check.py`'s `source_immutability` check
(re-hashes all 66 manifest files against the intake-time sha256) —
**PASS**, zero drift, confirmed both after the raw pipeline run and again
via `tests/test_stress_standalone.py::test_intake_links_everything_and_preserves_source_bytes`.

## 2. Organize

```
organize exit=0
```

`organize_manifest.json`: **61 datasets produced, 2 in the review bucket**:

```json
[
  {"file": "corrupted_workbook.xlsx", "link_name": "5697306a__corrupted_workbook.xlsx", "reason": "excel-open-error", "detail": "BadZipFile"},
  {"file": "mystery_export.dat", "link_name": "74874e19__mystery_export.dat", "reason": "unrecognized-format", "suffix": ".dat"}
]
```

`pdf_roles`:

```json
{
  "1a5518b6__lab_results_table.pdf": {"role": "table_extracted", "tables_extracted": 1},
  "73f446a1__1A_Screening.pdf": {"role": "annotated_pdf_companion", "matched_dataset_stem": "1a_screening", "target": ".../data/raw/Stress/annotated_pdfs/1A_Screening.pdf"}
}
```

**.xls handling**: `xlwt` was installable in this environment, so
`legacy_site.xls` is a GENUINE legacy BIFF workbook — it organized cleanly
into `legacy_site__Legacy.jsonl` (a dataset, not a review-bucket entry).
The fail-closed "mislabeled/unreadable .xls → review bucket with reason
`xls-reader-unavailable`" path is exercised by
`phi_engine/pipeline/organize.py`'s `ImportError` branch but was not hit by
THIS run because the real dependency was present; this is documented per
the plan's own contingency ("the mislabeled-file test stands in and the
limitation is recorded" — here the reverse held: the real path worked, so
no substitution was needed).

xlsx/csv/jsonl/json all routed to datasets. Malformed xlsx and the unknown
`.dat` routed to the review bucket with the reasons above. The PDF with an
embedded table extracted 1 table into a dataset; the annotated CRF PDF
(stem `1A_Screening`, matching `1A_Screening.xlsx`'s dataset stem)
correctly routed to `annotated_pdfs/`, not table extraction.

## 3. Run — partial exit, value-profiler escalation, zero leak

```
run exit=8
```

`pipeline_result.json` (first run): `exit_code: 8`, `forms_held: []`,
`forms_processed`: 61 forms (full list in the run artifact),
`organizer_review_count: 2`, `review_queue_size: 2`, `profile_escalations:
2`, `profile_auto_clears: 0`, `guard_ok: true`, `guard_failed: false`,
`published_count: 61`, `scrub_raised: null`, `sot_generation_error: null`.

**`profile_escalations: 2`** — these 2 catches are from `xlsx_phi_corpus.jsonl`'s
`text`/`gold_spans` columns (the corpus generator's own annotated-PHI sidecar:
KEEP-classified by name, force-dropped by the deterministic value profiler
because their VALUES are PHI-shaped), verified by direct instrumentation of
the escalation call site. `NOTES`/`COMMENT` are separately, correctly
force-dropped through a THIRD mechanism, NOT the profiler: `usa_free_text_suppression`
(a jurisdiction-rulebook NAME rule) classifies both `action: suppress`, and
`phi_scrub.run_scrub`'s documented SUPPRESS dual-path (Note 32) force-drops any
`suppress`-classified header that isn't also a small-cell-clamp candidate --
independent of `force_drop_headers` and of the profiler. (An earlier draft of
this report attributed `NOTES`/`COMMENT` to the ESCALATION rule; corrected
after Phase 7 evidence re-runs traced the actual call path -- both were always
`action: suppress`, never `action: keep`, so `profile_escalations` never
counted them.) Net result unaffected either way: `site_notes.jsonl`'s
published output contains neither a `NOTES` nor a `COMMENT` key, and a full
string-containment scan of every published `llm_source/datasets/*.jsonl` file
against `manifest["planted_unexpected_phi_rows"]` (12 planted SSN/phone
values) found **zero leaked planted values**.

`exit_code == 8` is exactly the "held/review present by construction"
outcome the messy fixture guarantees (2 organizer review-bucket entries).

## 4. Feedback loop — drop decisions clear the flagged columns

```bash
review decide --header NOTES   --decision drop   -> "recorded decision: NOTES -> drop"
review decide --header COMMENT --decision drop   -> "recorded decision: COMMENT -> drop"
run (second pass)                                -> exit_code=8
```

`exit_code` STAYS 8 after the drop decisions — this is CORRECT, not a
regression: the remaining review-queue entries are the organizer's
`corrupted_workbook.xlsx` and `mystery_export.dat` (unrelated files, no
decision resolves an organizer-level parse failure). What DOES change:
`site_notes.jsonl`'s republished output has **zero `NOTES`/`COMMENT` keys
at all** (confirmed by direct inspection — not just that the values are
blank, the columns are entirely absent), and a repeat full-corpus scan for
all 12 planted SSN/phone values again found zero leaks.

## 5. LLM boundary

`tests/test_stress_standalone.py::test_llm_boundary_zero_prompts_in_default_run_and_egress_gate_blocks_contamination`:

- `LLMClient.complete` was monkeypatched with a call-recording spy for the
  full stress intake→organize→run pass. **Zero calls recorded** — the
  default deterministic classification path (`phi_review.review_form_headers`
  with no `aligner=` injected) never constructs an LLM prompt at all. This
  is a stronger guarantee than "prompts were clean": there are no prompts.
- Separately, a real `LLMClient(provider="ollama", ...).complete(...)` call
  contaminated with a planted SSN value raised `PHIEgressBlockedError`
  (verified `type(exc).__name__ == "PHIEgressBlockedError"`, `isinstance(exc,
  PermissionError)` — matches `tests/test_llm_egress_gate.py`'s pattern) and
  did not echo the planted value in the exception message.

## 6. spec_check against the full stress run

```
[PASS] intake_symlink_invariant
[PASS] llm_boundary_canary
[PASS] source_immutability
ALL PASS
```

(`llm_boundary_canary`: `llm.provider` default `none`, zero
`get_llm_client()` calls outside the `llm_detector`/`phi_alignment`
exemption under `phi_engine/pipeline/`. `source_immutability`: all 66
stress-source files sha256-match their intake-time hash after the full
intake+organize+run+decide+re-run sequence.)

## 7. Test suite

```
.venv/bin/python -m pytest tests/test_stress_standalone.py -q  -> 7 passed
.venv/bin/python -m pytest -q                                  -> 358 passed, 0 failed, 88 warnings
```

Baseline before this refactor: 331 passed (`docs/paper/PAPER_EVIDENCE_PACK.md`).
Net new tests added across the refactor (verified via `grep -c "^def test_"`
per file): `tests/test_review_feedback_loop.py` (5),
`tests/test_llm_egress_gate.py` (3), `tests/test_value_profiler.py` (8),
`tests/test_sot_producer.py` (2), `tests/test_stress_standalone.py` (7,
including a stale-staged-file publish-bypass regression test added after the
Phase 7 final audit -- see `docs/AUDIT_REPORT_2026-07-07_STANDALONE.md`'s
Phase 7 addendum), plus regression-only edits to `tests/test_run_phi_system.py`
and scattered hardening elsewhere accounting for the remainder = 331 + 27 =
358. Zero pre-existing tests were removed or weakened.

## Failures

None. Every check above passed on the first fully-wired run (after the
mid-implementation bugs documented in
`docs/AUDIT_REPORT_2026-07-07_STANDALONE.md`'s Phase 2 addendum — the
`_adversarial_header_validation` jurisdiction-blindness bug, the
`synthesize_config.py` SUPPRESS/KEEP over-synthesis bugs, and the
row-alignment rewrite — were found and fixed during manual smoke-testing
BEFORE this stress suite ran; the stress suite itself, run against the
fully-fixed pipeline, found zero additional source bugs).

## Known limitations carried into this report

- `.xls` fail-closed routing (mislabeled/unreadable → review bucket) is
  implemented (`phi_engine/pipeline/organize.py`'s `ImportError` branch) but
  NOT exercised by this specific run, because `xlwt` was installable and the
  fixture's `.xls` ended up genuine. The code path is present; this run did
  not need the fallback.
- The SoT producer's annotated-PDF leg (Phase 3) is fixture-verified only —
  see `docs/AUDIT_REPORT_2026-07-07_STANDALONE.md`'s Phase 3 addendum. The
  `1A_Screening.pdf` fixture here is a plain reportlab-rendered page (no
  engineered pdfplumber-visible form annotations), so it exercises the
  organizer's "route to `annotated_pdfs/`" leg correctly but does not
  exercise `generate_sot`'s Indo-VAP-specific annotation-binding logic
  meaningfully (that logic is documented as not portable out of the box).
