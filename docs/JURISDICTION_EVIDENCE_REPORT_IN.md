# India Jurisdiction Evidence Report

Date: 2026-07-07
Commit: b454b9a (branch feat/v2-multi-jurisdiction, plus uncommitted working-tree changes made this session -- see "Changes made this session" below)
Seed: 42 (primary); 1337 (held-out replication, Step 5.6)
Method: Truth Protocol (CLAUDE.md). Every number below is read directly from a committed artifact JSON; none is hand-typed. Reproduction commands are given for every claim.

## 1. Metadata

| Field | Value |
|---|---|
| Study name | `PaperDemoIN` |
| Jurisdiction | INDIA |
| Rulebook | `phi_engine/config/_defaults/phi_rulebook/rulebook_v1_INDIA.json`, sha256 `2b236506fbdf2da24dd5757362d9cebba2f6d6eb632ebe759c40b51b7653091a` |
| Effective scrub config sha256 | `4823dbc8b84716f4a9a6ae2fc05690f7b0c85eda573cecd4329d718c0ba28dd1` (packaged defaults + the one India-specific addition, see 2.2) |
| Run id (seed 42) | `20260707T145337Z` |
| Run id (seed 1337 held-out) | `20260707T153435Z` |
| n_subjects | 60 |
| Python | 3.12.13 (`.venv`, created with `uv venv --python 3.12`) -- see "Environment" below |

## 2. Corpus provenance (canonical span-annotated corpus, Phase 1)

| Field | Value |
|---|---|
| `corpus/MANIFEST.json` sha256 | `5b413b9df27985c961a9b4b688efd9706e194607f2d1bdfeb97eb7054c34e2be` |
| `validation_report.json` sha256 | `769089c19633953a91249f8dac37da1e67ceff29d0f69155d0440f58ccc29fff` (`validation_status: PASS`, all 7 validators) |
| `release_evidence.json` sha256 | `2ee1895c85a69e0bada277b270ba25410882ad9f25ea1ae3b6ceea7aca240120` |
| `mia_report.json` sha256 | `1033f0a8fc742636b8be9163b35ed8a6366815bddaa18d0d578225b3cdb6d923` (`attack_auc: 0.4162`, threshold 0.6, deterministic smoke test -- not external validation) |
| India (`in/`) totals | 56 records, 112 gold spans (`india_dpdpa` 16/32, `india_identifiers` 40/80) |
| Total corpus (all 7 jurisdiction groups) | 886 records, 2506 gold spans |

Reproduction:
```
python -m harness.generate_corpus --seed 42 --jurisdiction all --out-dir corpus
python -m harness.run_all_validations --corpus-dir corpus --manifest corpus/MANIFEST.json --output validation_report.json
python -m harness.mia_framework --corpus-dir corpus --output mia_report.json
python -m harness.release_evidence --corpus-dir corpus --manifest corpus/MANIFEST.json --validation-report validation_report.json --mia-report mia_report.json --output release_evidence.json
```
(Run under the repo's `.venv` -- see "Environment".)

## 3. New clinical-study tabular corpus (Phase 2)

`generators/study_tabular.py::IndiaStudyTabularGenerator` -- authority citations: DPDPA 2023 Act 22, DPDP Rules 2025 Rule 14, SPDI Rules 2011 Rule 3, ICMR 2017 Section 2.3.5. Emits three CRF-form JSONL files per subject cohort: `1A_Screening.jsonl`, `2_Demographics.jsonl`, `3_Labs.jsonl`. Registry: `format_study_tabular`, status `tested` (promoted from `implemented` after `tests/test_study_tabular.py` passed -- 17 tests, `.venv/bin/python -m pytest tests/test_study_tabular.py -q`).

### 3.1 Column contract and binding to the scrub config

Verified interactively against `phi_engine.security.phi_scrub.load_scrub_config()` (`PHIScrubConfig` methods `field_is_id`/`field_is_date`/`field_is_birthdate`/`cap_rule_for`/`field_is_keep`/`field_is_drop`), not assumed:

| Column | Rule matched (packaged defaults) | Action |
|---|---|---|
| `SUBJID` | `id_fields` pattern `^SUBJID$`, label `SUBJ` | pseudonymize |
| `IC_SCRNNUM` | `id_fields` pattern `^I[CS]_SCRNNUM$`, label `SCRN` | pseudonymize |
| `VISITDAT`, `COLLDAT`, `TBTXDT` | `date_fields` (specific catalog / generic `_?DAT\d*$` catch-all) | jitter_date |
| `IS_BIRTHDAT` | `birthdate_field`, `compliance_posture: safe_harbor`, form has `AGE` | drop |
| `AGE` | `cap_fields` pattern `^(?:[A-Z]{1,4}[-_])?AGE$`, threshold 89 | cap (conditional, see 3.2) |
| `SEX`, `WEIGHT` | no rule matches | published unchanged |
| `CBC_HGB` | `keep_fields` pattern `^CBC_(?!INIT\|SIGN)` | keep |
| `AADHAAR_NUM`, `PAN_NUM`, `MOBILE_NUM` | `drop_fields` (packaged defaults, no addition needed) | drop |
| `ABHA_NUM` | **not covered by packaged defaults** -- see 3.3 | drop (after per-study addition) |

### 3.2 A real measurement-methodology bug found and fixed this session

The first redaction-recall measurement (see 5.2) treated every `AGE` cell as an expected "cap" transform, but HIPAA Safe Harbor Sec.164.514(b)(2)(i)(C) only requires capping ages **over** 89 -- the scrub engine correctly leaves `AGE <= 89` unchanged (58/60 study subjects), which is not a redaction failure. `generators/study_tabular.py::_plant_ledger_rows` was corrected to track only the one actually-capped subject's `AGE` cell as a gold PHI cell; the other 59 are excluded from redaction-recall accounting, mirroring the `SEX`/`WEIGHT`/`CBC_HGB` "keep" treatment. This raised measured `redaction_recall` from 0.8445 (methodology-flawed) to 0.9917 (see 5.2) on the identical run.

A second, separate bug in the same first pass: the redaction check searched whether a raw value appeared **anywhere in the whole output file** rather than in its own row, which produces false-positive "leaks" for low-entropy values (2-digit ages, dates that coincidentally collide with an unrelated subject's shifted date across a 60-subject/~400-day window). `harness/run_phi_system.py` was corrected to a row-scoped comparison (`_row_index_map` helper), eliminating the false positives. Both fixes are visible in the `redaction.method` field of every `phi_system_result.json` and are documented in git history of this session.

### 3.3 Deliberate fail-closed edge cases (Step 2.3)

Planted at fixed subject indices (present for any `n_subjects >= 5`, so present at both n=60 in this run and n=8 in `tests/test_run_phi_system.py`):

| Index | Edge case | Expected outcome |
|---|---|---|
| 0 | `AGE = "92"` | cap to `"90+"` |
| 1, 2 | `VISITDAT = "INVALID-DATE"` (genuinely unparseable, not a recognized null-token like `"UNK"` -- see below) | blank (`unparseable_date_policy: blank`) |
| 3, 4 | `SUBJID = ""` in `3_Labs.jsonl` only (Screening/Demographics keep the real SUBJID) | orphan-row quarantine |

Note: the evidence plan's Step 2.3 draft used the literal string `"UNK"` as the unparseable-date placeholder; verified interactively that `"UNK"` is one of `phi_scrub._DEFAULT_DATE_NULL_TOKENS` and is therefore **skipped** (left as-is, not blanked) by design -- a different code path than `unparseable_date_policy`. `"INVALID-DATE"` was substituted so the planted edge case actually exercises the blank-policy path it is meant to demonstrate.

### 3.4 Gold ledger

`gold_ledger.jsonl` (written to the run's `--out-dir`, never into the staging tree): 794 lines total for a 60-subject run -- 16 column-legend lines (one per (form, column)) + 778 per-PHI-cell lines. Of the 778 cell lines, 719 carry a non-"keep" `expected_action` and are counted toward `total_gold_phi_cells` (redaction denominator); the remaining lines are `SEX`/`WEIGHT`/`CBC_HGB` cells the redaction-recall accounting correctly excludes.

## 4. Porting gaps discovered and fixed this session

`harness/run_phi_system.py` drives `phi_engine.security.phi_scrub.run_scrub` directly on pre-staged JSONL (per the plan's decision not to invoke the skill `run.py` wrappers, which share a bare `import config` seam that resolves to nothing in this repo layout). Three additional defects were discovered while making that call path actually work, all fixed at the source with the smallest correct change:

1. **`phi_engine/config/config.py::BASE_DIR` pointed one directory too deep.** `config.py` was ported from the repository root into `phi_engine/config/config.py`, but `BASE_DIR = Path(__file__).resolve().parent` still assumed `config.py` lived at the repo root (the code comment said so explicitly). Effect: `CONFIG_DEFAULTS_DIR` resolved to a directory that does not exist, so `load_scrub_config()` could never find the packaged defaults; separately, `phi_scrub.run_scrub`'s staging directory landed under `phi_engine/config/tmp/`, outside `phi_engine/security/secure_env.py`'s real-repo-root `tmp/` zone, so `assert_write_zone` hard-failed with `ZoneViolationError` on every call. Fixed: `BASE_DIR = Path(__file__).resolve().parents[2]` (repo root), with `CONFIG_DIR` (which holds `_defaults/` and per-study overrides) kept pointing at config.py's own directory, since the port physically merged the old repo-root-level `config/` data directory into `phi_engine/config/`. Verified: `phi_engine/config/_defaults/phi_scrub.yaml` and `phi_rulebook/rulebook_v1_INDIA.json` now resolve correctly; `python -m pytest -q` stayed green (308/308, then 331/331 after this session's new tests) before and after the fix.
2. **`phi_engine/security/phi_scrub.py` imported `datetime, timezone` but called `timedelta(...)` in `shift_date`.** A `NameError` on every date-jitter call. Root cause: a prior "py3.9 compat" edit to the import line dropped `timedelta`. Fixed: `from datetime import datetime, timedelta, timezone`.
3. **`run_scrub` unconditionally imports `scripts.extraction.forms_manifest.check_forms_manifest`**, a module belonging to the full extraction/publish orchestrator that is explicitly NOT ported into this repo (only `phi_engine`'s security/audit/config/skills layers are). A minimal compatible shim (`scripts/extraction/forms_manifest.py`, ~170 lines) was written from scratch, implementing only `check_forms_manifest`'s documented contract (same `ManifestCheckResult`/`ManifestMismatchError` API as the archived original at `tmp/reportal-phi-plugin.zip:phi-plugin-export/scripts/extraction/forms_manifest.py`), with two deliberate deviations: it imports `phi_engine.config.config` instead of a bare `import config`, and it degrades gracefully when the raw `data/raw/<study>/datasets/` directory does not exist (this repo's harness stages directly into `tmp/<study>/datasets/`, never populating the raw tree the original gate was written to check).

None of these three fixes touch `phi_engine`'s existing test-covered behavior in any way the test suite can detect a difference for: `python -m pytest -q` was 308 passed before Phase 2/3 work began and is 331 passed now (17 new tests for `study_tabular.py`, 2 for `run_phi_system.py`, 4 for `phi_engine_adapter.py`; zero regressions across every intervening full-suite run this session).

A fourth, unrelated defect (not fixed, out of scope): `phi_engine/skills/phi-scrubbing/scripts/phi_scrub.py` is a byte-identical duplicate of `phi_engine/security/phi_scrub.py` (same `timedelta` bug present, unpatched) but is never on this session's call path (the skill wrappers are deliberately not invoked, per the plan's decision -- see Ground truth Note 51).

## 5. End-to-end system run (Phase 3)

Command:
```
STUDY_NAME=PaperDemoIN  # set internally by the driver from --study
python -m harness.run_phi_system --study PaperDemoIN --jurisdiction in --seed 42 --n-subjects 60 --out-dir benchmarks/results/phi-system-in
```
Exit code: 0. `phi_system_result.json` -> `scrub_raised: null` (the scrub function did not raise).

### 5.1 AI layer (header classification, GR-1 -- no LLM ever reads a row value)

- `headers_total: 14`, `headers_auto_classified: 14`, `headers_review_queue: 0`, `human_review_rate: 0.0`.
- `classifier_path: "fallback"` -- shipped `llm.provider: none` (`phi_engine/config/config.yaml`), so `LLMClient.complete()` raises `RuntimeError("PHI LLM provider is disabled...")` immediately (no network call, no hang) and `classify_headers` falls back to `presidio_gate` + `phi_gate` pattern matching over header **names only** (never a row value).
- `header_classification_agreement: 0.2143` (3/14 headers). This is a genuinely low, honestly-measured number, not a defect: the deterministic fallback classifier can only emit `drop` or `keep` (never `pseudonymize`/`jitter_date`/`cap`), and its underlying regex catalog is built to match **data-value shapes** (an Aadhaar number, a date string), not short **column-name** tokens like `SUBJID` or `AGE`. Definition recorded verbatim in every `phi_system_result.json` (`ai_layer.header_classification_agreement_definition`): binary PHI-vs-non-PHI agreement, not transform-type agreement.
- `llm_uncertain.jsonl` under the run's `--out-dir`: 0 lines (matches `headers_review_queue`).

### 5.2 Redaction recall (measured against the gold ledger, row-scoped, exact + digit-normalized match)

| expected_action | total cells | redacted |
|---|---|---|
| pseudonymize | 238 | 238 |
| jitter_date | 174 | 168 |
| drop | 300 | 300 |
| cap | 1 | 1 |
| blank | 2 | 2 |
| quarantine_row | 4 | 4 |
| **Total** | **719** | **713** |

`redaction_recall = 713 / 719 = 0.9917` (99.17%, measured; this is a corpus-scoped tuple, not a general claim).

**All 6 leaked cells are explained and are not a system defect**: `dates.shifted = 168 / 174`, i.e. 6 date cells kept their exact pre-scrub value. The SANT per-subject jitter offset is `HMAC(key, subject_id) mod 61 - 30` (`phi_scrub.date_offset_days`) -- a uniform integer in `[-30, +30]`; a `1/61` chance per subject that the offset computes to exactly `0`. This measured run has exactly 2 subjects with offset `0` (`per_subject_offset_constant: 60` of `60` subjects -- i.e. every subject's offset IS internally constant across their own dates, and among those 60 constant offsets, 2 subjects' constant happens to be zero), producing `2 subjects x 3 date columns = 6` cells whose published value is bit-identical to the raw value. This is a quantified, disclosed property of the jitter algorithm, not a bug: `phi_scrub.run_scrub` did apply the deterministic per-subject transform to every date cell; for these 2 subjects the transform's output equals its input.

### 5.3 Pseudonymization

- `cells_checked: 238`, `regex_pass_count: 238`, `regex_pass_rate: 1.0` -- every `SUBJID`/`IC_SCRNNUM` value published this run matches `^RID_[A-Z0-9]{1,16}_[a-p]{12}$`.
- `cross_form_linkage_subjects_checked: 60`, `cross_form_linkage_ok: 60` -- for every subject, the same raw `SUBJID` produces the identical `RID_SUBJ_...` token across `1A_Screening.jsonl`, `2_Demographics.jsonl`, and `3_Labs.jsonl` (deterministic HMAC keyed only by label + raw value).
- Concrete spot check (plan verification requirement): pre-scrub copy row 0 of `2_Demographics.jsonl` has raw `SUBJID: "IN-0001"`; post-scrub staging row 0 has `SUBJID` matching `^RID_SUBJ_[a-p]{12}$`, and the identical token appears for that subject in `3_Labs.jsonl` -- confirmed as a specific instance of the 60/60 linkage check above.

### 5.4 Dates

- `per_subject_offset_all_constant: true` (60/60 subjects) -- interval-preserving SANT jitter confirmed.
- `interval_preservation_checked: 56`, `interval_preservation_preserved: 56` (100%) -- `(COLLDAT - VISITDAT)` deltas unchanged pre/post scrub for every subject where both cells are populated post-scrub (excludes the 2 unparseable-VISITDAT subjects and the 2 orphan-Labs subjects, where one side of the interval never publishes).
- `within_jitter_bound: 174 / 174` -- every non-blanked date cell's shift is `<= 30` days, matching `max_jitter_days`.

### 5.5 Fail-closed edge cases

| Check | Planted | Observed | Match |
|---|---|---|---|
| Orphan-row quarantine | 2 | 2 | yes |
| Unparseable-date blank | 2 | 2 | yes |
| Age-cap to `"90+"` | 1 | 1 | yes |

### 5.6 Residual guard gate

`run_phi_guard_gate(config.STAGING_DATASETS_DIR)` (OR-combines `presidio_gate.scan_tree_with_presidio` + `llm_source_gate.scan_tree_for_phi`, both value-free): `ok: true`, `presidio_finding_count: 0`, `legacy_finding_count: 0`, `triggered_by: []`. No fallback substitution was needed (presidio was available).

### 5.7 Held-out replication (seed 1337, anti-tuning check)

Separate study name `PaperDemoINHeldout` (to avoid overwriting the seed-42 evidence artifacts). Identical measured outcome to seed 42: `redaction_recall: 0.9917` (same 6 leaked date cells for the same structural reason), `per_subject_offset_all_constant: true`, `quarantine_matches_planted / blank_matches_planted / age_cap_matches_planted: true`, `residual.ok: true`, `header_classification_agreement: 0.2143`. This is the anti-tuning signal the plan requires: the system's measured behavior is not seed-specific.

## 6. Detection benchmark (Phase 4, India corpus, 56 records / 112 gold spans)

Primary protocol profile: `strict_all_span` (exact span + exact type). Secondary: `legacy_overlap_coverable` (IoU >= 0.5, position-agnostic). Full command list and every tool's result file: `benchmarks/results/comparison_table.md`.

| Tool | Strict P | Strict R | Strict F1 | Legacy F1 | Macro-F1 | Gap rate |
|---|---|---|---|---|---|---|
| phi_engine (this system's own detection surface) | 0.4444 | 0.6071 | **0.5132** | 0.5837 | 0.7011 | 0.2857 |
| Presidio stock 2.2.363 | 0.0854 | 0.1250 | 0.1014 | 0.5645 | 0.3867 | 0.2500 |
| Presidio tuned 2.2.363 | 0.0854 | 0.1250 | 0.1014 | 0.5645 | 0.3867 | 0.2500 |
| spaCy en_core_web_sm 3.8.13 | N/A (adapter does not compute strict) | | | 0.4203 | 0.2790 | 0.0714 |
| philter, pydeid, clinideid, physionet_deid, modified_deidentify, aws_comprehend_medical, azure_health | not_run | | | | | |

**On this benchmark**, phi_engine's own structured detection surface outperforms stock and tuned Presidio on strict F1 (0.513 vs 0.101) and macro-F1 (0.701 vs 0.387) for the India corpus. This is a structural-coverage result, not a general NER-quality claim: `phi_engine`'s regex catalog (`phi_patterns.BLOCKING_PATTERNS`) has dedicated Aadhaar (Verhoeff-validated), PAN, Indian-voter-ID, Indian-driving-license, Indian-passport, and Indian-mobile recognizers; stock Microsoft Presidio's predefined recognizer set has none of these. `not_run` reasons (philter/pydeid) were independently investigated and confirmed via web research this session -- see `docs/SOTA_COMPARISON.md`.

`not_run` reasons for `philter` and `pydeid` deserve one more sentence: `philter-ucsf` (PyPI) installs but is confirmed (github.com/BCHSI/philter-ucsf, PyPI docs) to be a CLI-only tool with no importable `detect_phi()`-style Python API, and its internal module additionally requires undeclared transitive dependencies (`nltk` corpora, `chardet`); `pydeid` on PyPI (version 0.0.1) is an empty placeholder unrelated to the real academic tool (GEMINI-Medicine/pyDeid on GitHub), whose git-installable build did not yield an importable module in this environment. Neither row is reported as a fabricated zero-recall measurement.

## 7. Claim statements (bounded, per repo Claim Discipline)

- "phi_engine redacted 713/719 (99.2%) gold-annotated PHI cells on the seed-42 `PaperDemoIN` synthetic clinical-study corpus (60 subjects, 3 CRF forms); the 6 unredacted cells are dates whose deterministic per-subject jitter offset computed to zero, a disclosed 1/61-probability property of the SANT jitter algorithm, not a detection failure -- replicated at seed 1337."
- "On the seed-42 India synthetic corpus (56 records, 112 gold spans), phi_engine's own pattern-detection surface outperforms both evaluated Presidio configurations (stock, tuned) on strict-protocol F1 (0.513 vs 0.101) -- attributable to dedicated India structured-identifier recognizers Presidio's predefined set lacks."
- "Zero residual PHI findings (presidio + legacy value-free scanners, OR-combined) in the published staging tree for this run."
- "human_review_rate: 0.0% for this run's 14 CRF headers, measured under the deterministic fallback classifier (no cloud LLM; GR-1 held)."

The words "fail-proof" and unscoped "100% accurate" do not appear above or anywhere in this report.

## 8. Limitations

- Synthetic-only: no real India patient/study data was used or is claimed to be represented.
- Single 60-subject cohort per seed; two seeds run (42, 1337) as an anti-tuning check, not a statistical power study.
- Circularity: the phi_engine detection-surface benchmark in Section 6 evaluates the system's own pattern catalog against its own generator's synthetic corpus; the structural-gap comparison against Presidio mitigates but does not eliminate this concern (documented per the plan's stress-test section).
- No clinician or counsel review has been performed on this output (tracked in `.phi-build-status`).
- Free-text NER is explicitly out of scope for this system's positioning; see `docs/SOTA_COMPARISON.md`.
- `header_classification_agreement` (21.4%) reflects a real, disclosed limitation of the deterministic (non-LLM) fallback classifier on this generator's short CRF column names -- not tuned away, reported as measured per the single-measured-run decision.

## Environment

This repo's `pyproject.toml` declares `requires-python = ">=3.10"`; the workstation's system Python is 3.9.21, which is incompatible (`phi_engine/security/phi_scrub.py` uses `isinstance(value, int | float)`, PEP 604 syntax requiring 3.10+, and several transitive dependencies -- `pydicom>=3.0.1`, `numpy>=2.1.3` -- have no 3.9-compatible releases). All commands in this report were run under a `uv`-managed Python 3.12.13 virtualenv (`uv venv --python 3.12 .venv`; `uv pip install -r requirements.txt` with `pycanon` unpinned from `==1.0.1` to `>=1.0.1` to resolve a `reportlab` version conflict in that exact pin). `.venv/bin/python -m pytest -q` -> 331 passed.
