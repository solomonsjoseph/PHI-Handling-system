# PHI Handling System — Gap Analysis (Pre-refactor vs. Standalone, 2026-07-07)

> **Current scope:** USA-only. India/EU/AU/BR/UG generators, non-USA corpus slices, and `docs/JURISDICTION_EVIDENCE_REPORT_IN.md` are removed. Rows below that cite PaperDemoIN or the India evidence report are historical and marked removed.

Method: every number below is read directly from an artifact in this working
tree (`benchmarks/results/phi-system-*/phi_system_result.json`, pytest output,
`docs/STRESS_TEST_REPORT.md`, `docs/AUDIT_REPORT_2026-07-07_STANDALONE.md`),
not estimated. "Pre-refactor" figures cite the specific doc/line they came
from; where no pre-refactor equivalent exists (the standalone package didn't
exist before this session), the row says so explicitly rather than inventing
a number.

## 1. Architecture

| Dimension | Pre-refactor | Post-refactor (standalone) |
|---|---|---|
| Deployability | PHI pipeline logic lived inside `harness/run_phi_system.py`, entangled with the corpus-generation benchmark harness (`generators/`, `harness/generate_corpus.py`) and Indo-VAP-specific hardcoded paths/form names. | `phi_engine/` is a self-contained package (68 `.py` files) with its own CLI (`python -m phi_engine {intake,organize,run,review}`), config (`PHI_WORKSPACE`, `STUDY_NAME` env vars — no hardcoded paths), and zero imports from `generators/` (verified: `grep "from generators\|import generators" phi_engine` → no matches, confirmed independently by the Phase 6 fresh-context audit). |
| Ingestion | None — datasets were placed directly into the pipeline's expected directory structure by hand or by the corpus generator. | `phi_engine intake` — symlink-only (`spec_check`'s `intake_symlink_invariant` enforces every non-manifest intake entry is a symlink; verified `0` violations against the 66-file stress fixture), never copies/moves/modifies source bytes (`source_immutability` check: 66/66 files unchanged). |
| Organization | None — the pipeline assumed pre-organized `.jsonl` datasets under a fixed layout. | `phi_engine organize` — routes raw variables/datasets, `.pdf` (table-extracted or CRF-annotation-companion), `.xlsx`/`.xls`/`.csv`/`.json`/`.jsonl`, duplicates (content-hash dedup), unrecognized formats, and corrupt files into either an organized dataset tree or a `{file, reason}`-only (no row values) review bucket. Verified end-to-end against the Phase 6 stress fixture: 61 datasets organized, 2 correctly routed to review (`excel-open-error`, `unrecognized-format`). |
| Driver | `harness/run_phi_system.py` (494 lines) did dataset extraction, classification, scrub-config synthesis, scrubbing, and gold-ledger measurement all inline, coupled to the benchmark corpus's ~~`PaperDemoIN`/~~`PaperDemoUS` gold-column-legend format. | `phi_engine/pipeline/run.py` (`run_pipeline(study, jurisdiction)`) is the standalone driver — classification, scrub-config synthesis, scrubbing, and publish, with NO gold-ledger/benchmark coupling. `harness/run_phi_system.py` is demoted to a thin measurement wrapper: it now calls `phi_engine.pipeline.run.run_pipeline` internally (see `harness/run_phi_system.py:1-6` module docstring) and adds ONLY the gold-ledger comparison layer needed for paper evidence — the benchmark harness no longer duplicates pipeline logic. |
| SoT (Source-of-Truth) producer | Referenced by the original plan as living in `archive/plugin-setup-and-dictionary/sot-lean-generator/scripts/`, dependent on `scripts.ai_assistant.sot_joined_view` — a module that does not exist anywhere in the repository (verified: not live, not archived). | Ported to `phi_engine/sot/` with `phi_engine/sot/sot_joined_view.py` reimplemented from the two actual callers' shapes + the consumer contract (`phi_review.load_sot_variable_signals`). Wired into `run_pipeline` as fail-soft enrichment (never aborts the PHI pipeline; `PipelineResult.sot_generation_error` records failures). See `docs/AUDIT_REPORT_2026-07-07_STANDALONE.md` Phase-3 addendum for exact scope/limitations (curated PDF-annotation tables are Indo-VAP-specific and must be replaced per new deployment — plumbing is portable, the curated data is not). |

## 2. Review reduction (the stated goal)

| Mechanism | Status | Evidence |
|---|---|---|
| Deterministic name-based classification (jurisdiction rulebooks) | Pre-existing, unchanged. | `phi_review.review_form_headers` against the pinned **USA** rulebook. ~~INDIA rulebook removed from active scope.~~ |
| SoT-confirmed benign override | Pre-existing, unchanged (fixture-verified this session — see SoT addendum). | `tests/test_sot_producer.py`. |
| Human review feedback loop (`review decide --header H --decision {drop,override}`) | New in this refactor. | `tests/test_review_feedback_loop.py` (5 tests) + `tests/test_stress_standalone.py::test_feedback_loop_drop_decision_clears_the_flagged_columns` — a recorded `drop` decision removes the column from ALL subsequent runs' published output, and does not regress `organizer_review_count`. |
| Deterministic value profiler (LOCAL, in-process, never leaves the process) — ESCALATION (name-blind PHI-shaped values force-dropped) + AUTO-CLEAR (proven-safe closed-categorical columns un-held) | New in this refactor. | `phi_engine/pipeline/profile.py` (`tests/test_value_profiler.py`, 8 tests). Stress-verified: a genuinely name-blind column (`PROCESS_TAG`, zero overlap with any packaged keep/date/id/drop/cap/generalize/band/suppress pattern) carrying SSN-shaped values is force-dropped before publish with zero human review. |
| Prompt egress gate (`phi_gate_check`) | Pre-existing; hardened with an explicit unit test this session. | `tests/test_llm_egress_gate.py` — a prompt contaminated with a planted SSN/email/phone raises `PHIEgressBlockedError` before dispatch; a clean headers-only classification prompt passes. |
| Net human-review rate across evidence studies + the adversarial stress fixture | `0.0` on clean synthetic USA study — matches pre-refactor. ~~PaperDemoIN / PaperDemoINHeldout figures removed (India artifacts deleted).~~ The stress fixture (deliberately malformed inputs + planted mis-named PHI) still correctly routes 2 organizer-level entries + upholds the profiler backstop, i.e. review is reserved for genuinely ambiguous/malformed input, not pushed onto the operator for cases the system can resolve deterministically. | `benchmarks/results/phi-system-us/phi_system_result.json` (`ai_layer.human_review_rate: 0.0`), `docs/STRESS_TEST_REPORT.md`. |

## 3. Accuracy (redaction recall)

USA evidence study, re-run through the standalone `phi_engine` path
(`harness/run_phi_system.py` → `phi_engine.pipeline.run.run_pipeline`),
reproduces the documented pre-refactor USA baseline EXACTLY:

| Study | Seed | `redaction_recall` | Pre-refactor documented baseline | Match |
|---|---:|---:|---:|---|
| PaperDemoUS | 42 | `0.9958275382475661` | `0.9958` (`docs/JURISDICTION_EVIDENCE_REPORT_US.md`) | Exact |
| ~~PaperDemoIN~~ | ~~42~~ | ~~removed~~ | ~~`docs/JURISDICTION_EVIDENCE_REPORT_IN.md` deleted~~ | ~~out of scope~~ |
| ~~PaperDemoINHeldout~~ | ~~1337~~ | ~~removed~~ | ~~India held-out report deleted~~ | ~~out of scope~~ |

The residual gap from `1.0` on PaperDemoUS is the disclosed SANT date-offset
zero event (~1/61 probability per subject per seed, not an unredacted leak) —
verified in `redaction.leaks`: every residual cell is a date column
(`VISITDAT`/`COLLDAT`/`TBTXDT`), never an identifier, address, or free-text
field. `human_review_rate: 0.0` and `residual.ok: true`.

### Historical note (India profiler over-escalation; artifact removed)

~~A post-refactor PaperDemoIN measurement briefly reported `redaction_recall: 0.9944` because the value profiler's ESCALATION rule force-dropped `TBTXDT` under INDIA rules. That India evidence path and `docs/JURISDICTION_EVIDENCE_REPORT_IN.md` are deleted.~~ The USA-path fix (compute `published_raw_headers` from the effective scrub config) remains in `phi_engine/pipeline/run.py`; see `docs/AUDIT_REPORT_2026-07-07_STANDALONE.md` Phase-7 addendum for the historical write-up.

## 4. Test coverage

| Checkpoint | Passed | Notes |
|---|---:|---|
| Phase 0 baseline (`.venv/bin/python -m pytest -q`) | 331 | Matches `PAPER_EVIDENCE_PACK.md` documented baseline. |
| Final (`.venv/bin/python -m pytest -q`) | 358 | Net new coverage this refactor: SoT producer (`tests/test_sot_producer.py`, 2, fixture-verified), review feedback loop (`tests/test_review_feedback_loop.py`, 5), LLM egress gate (`tests/test_llm_egress_gate.py`, 3), deterministic value profiler (`tests/test_value_profiler.py`, 8), full stress suite (`tests/test_stress_standalone.py`, 7 -- including a stale-staged-file publish-bypass regression test added after the Phase 7 final audit), plus scattered hardening across other files. |
| Stress suite alone (`tests/test_stress_standalone.py`) | 7 | Intake byte-preservation, organizer format-routing (exact counts: 61 datasets, 2 review-bucket entries), partial-run PHI-in-unexpected-column escalation, feedback-loop column-clearing, LLM-boundary zero-prompt proof + egress-gate contamination block, `spec_check` pass, stale-staged-file-never-publishes regression. |
| `spec_check` (portable, project-agnostic invariant checker) | ALL PASS | `intake_symlink_invariant`, `llm_boundary_canary` (static + runtime), `source_immutability` (SHA-256 against the fixture's own manifest) — all 3 checks pass against both the 3-form evidence studies and the 66-file stress fixture. |

## 5. Known limitations carried forward (not closed by this refactor — stated, not hidden)

These match `README.md`'s "Planned coverage not claimed as implemented" table
and `docs/AUDIT_REPORT_2026-07-07_STANDALONE.md`; repeated here because a gap
analysis that omits open gaps is not one:

1. **SoT PDF-annotation-binding quality is fixture-verified only, not proven
   against an arbitrary non-Indo-VAP CRF PDF.** The curated annotation-alias
   tables (`ANNOTATION_ALIASES`, etc.) are Indo-VAP-specific and ship empty
   of relevant entries for a new study — a new deployment must curate its
   own, or the SoT enrichment layer degrades to a no-op (fail-soft, does not
   block the PHI pipeline).
2. **Clinician/counsel review controls remain `planned`, not implemented** —
   unchanged from pre-refactor (`README.md` capability table).
3. **Non-USA jurisdiction coverage (`in`/`eu`/`br`/`au`/`ug`, plus planned `uk_gdpr`/`china_pipl`/`japan_appi`/`singapore_pdpa`) is deferred / out of current scope.** Generators under `generators/in|eu|br|au|ug`, non-USA rulebooks, and the India evidence report have been removed; only the USA rulebook and USA corpus path are active.
4. **The value profiler is a LOCAL, deterministic, in-process backstop for
   PHI-shaped VALUES under unexpectedly-named columns; it is not a general
   PHI-detection classifier** — it only catches values matching the existing
   `BLOCKING_PATTERNS`/`WARN_PATTERNS` regex catalog (SSN, phone, email,
   Aadhaar, PAN, dates, etc.), not free-form narrative PHI (that remains the
   job of the free-text suppression/drop rules).

## 6. Bottom line

Portable standalone package: yes (verified — `phi_engine intake`/`organize`/
`run`/`review` run from an out-of-tree `--workspace`, symlink-only ingest,
zero `generators/` coupling, zero hardcoded study/path names).
Accuracy: USA `redaction_recall` exact-matches the documented pre-refactor
PaperDemoUS baseline. ~~India baselines no longer claimed (artifacts removed).~~
Review reduction: net new mechanisms added (feedback loop + value profiler)
verified end-to-end against
an adversarial stress fixture with zero PHI leaks. AI/API-key boundary: zero
`LLMClient.complete` calls in the default deterministic path (runtime-spied,
not just documented), egress gate blocks contaminated prompts before
dispatch.
