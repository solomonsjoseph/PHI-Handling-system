# USA Jurisdiction Evidence Report

Date: 2026-07-07
Commit: b454b9a (branch feat/v2-multi-jurisdiction, plus uncommitted working-tree changes made this session)
Seed: 42
Method: Truth Protocol (CLAUDE.md). Every number below is read directly from a committed artifact JSON; none is hand-typed. Reproduction commands are given for every claim. Full porting-gap discovery, the redaction-methodology bug fixes, and the environment setup are documented once in `docs/JURISDICTION_EVIDENCE_REPORT_IN.md` Sections 3.2, 4, and "Environment" (India was run first per the user's locked decision; both runs share the same driver, generator base class, and fixes) -- this report does not repeat that narrative and focuses on the USA-specific artifacts and numbers.

## 1. Metadata

| Field | Value |
|---|---|
| Study name | `PaperDemoUS` |
| Jurisdiction | USA |
| Rulebook | `phi_engine/config/_defaults/phi_rulebook/rulebook_v1_USA.json`, sha256 `5960e1156847781294c951062599d0b302aafb92ca5c087dab36f06ea21db8eb` |
| Effective scrub config sha256 | `32e0faeb02c4ea579af7311111fde1575e5306c135a36e240c2b55345d5ecc4f` |
| `phi_engine/config/PaperDemoUS/phi_scrub.yaml` sha256 | `b60de310e6ec3b18e147887a77afd207598edd34551b7a685dc23c1dfa792b5c` -- **byte-identical** to `phi_engine/config/_defaults/phi_scrub.yaml` (`b60de310...`, same hash). Unlike India, no per-study addition was needed: `SSN`, `MRN`, `PHONE_NUM`, `EMAIL` all matched the packaged `drop_fields` defaults directly (verified via `PHIScrubConfig.field_is_drop()` before writing the generator). |
| Run id | `20260707T145528Z` |
| n_subjects | 60 |

## 2. Corpus provenance

Same canonical corpus as the India report (Section 2 there): `corpus/MANIFEST.json` sha256 `5b413b9df27985c961a9b4b688efd9706e194607f2d1bdfeb97eb7054c34e2be`, `validation_status: PASS`. US (`us/`) totals: 550 records, 1314 gold spans across 10 generator groups (`hipaa_safe_harbor` 180/302, `hipaa_quasi_identifiers` 50/150, `hipaa_lds` 40/130, `hipaa_reid_codes` 40/40, `hipaa_fundraising` 40/265, `hipaa_verification` 40/195, `hipaa_biometric` 40/48, `hipaa_device` 40/64, `hipaa_fax` 40/68, `hipaa_vehicle` 40/52).

## 3. New clinical-study tabular corpus

`generators/study_tabular.py::USStudyTabularGenerator` -- authority citation 45 CFR 164.514(b)(2) (HIPAA Safe Harbor). Same three-form CRF shape as India (`1A_Screening.jsonl`, `2_Demographics.jsonl`, `3_Labs.jsonl`), same shared column contract (`SUBJID`, `IC_SCRNNUM`, `VISITDAT`/`COLLDAT`/`TBTXDT`, `IS_BIRTHDAT`, `AGE`, `SEX`, `WEIGHT`, `CBC_HGB`), US-specific identifier columns `SSN`, `MRN`, `PHONE_NUM`, `EMAIL` (all four bind to packaged `drop_fields` defaults with no study-specific addition). Same deliberate fail-closed edge cases at the same fixed subject indices (0: AGE=92; 1,2: VISITDAT="INVALID-DATE"; 3,4: Labs SUBJID=""), same methodology-bug fixes applied (see India report Section 3.2), same gold-ledger schema.

## 4. End-to-end system run

Command:
```
python -m harness.run_phi_system --study PaperDemoUS --jurisdiction us --seed 42 --n-subjects 60 --out-dir benchmarks/results/phi-system-us
```
Exit code: 0. `scrub_raised: null`.

### 4.1 AI layer

- `headers_total: 14`, `headers_auto_classified: 14`, `headers_review_queue: 0`, `human_review_rate: 0.0`.
- `classifier_path: "fallback"` (same reason as India -- `llm.provider: none`, GR-1 held, no cloud LLM).
- `header_classification_agreement: 0.2143` (identical to India -- the classifier's coarse drop/keep vocabulary and value-shape-oriented regex catalog produce the same measured agreement rate regardless of jurisdiction label passed to `classify_headers`, since the underlying `phi_gate`/`presidio_gate` fallback scans header **names**, not jurisdiction-specific value patterns).

### 4.2 Redaction recall

| expected_action | total cells | redacted |
|---|---|---|
| pseudonymize | 238 | 238 |
| jitter_date | 174 | 171 |
| drop | 300 | 300 |
| cap | 1 | 1 |
| blank | 2 | 2 |
| quarantine_row | 4 | 4 |
| **Total** | **719** | **716** |

`redaction_recall = 716 / 719 = 0.9958` (99.58%, measured). `dates.shifted: 171 / 174` -- 3 date cells (1 subject x 3 date columns) whose per-subject SANT offset computed to exactly zero this seed, same disclosed 1/61-probability property documented in the India report Section 5.2. All 3 leaked cells are explained by this mechanism; none is an unexplained finding.

### 4.3 Pseudonymization

- `regex_pass_count: 238 / 238` (100%) match `^RID_[A-Z0-9]{1,16}_[a-p]{12}$`.
- `cross_form_linkage_ok: 60 / 60` subjects.

### 4.4 Dates

- `per_subject_offset_all_constant: true` (60/60).
- `interval_preservation_checked: 56`, `interval_preservation_preserved: 56` (100%).
- `within_jitter_bound: 174 / 174`.

### 4.5 Fail-closed edge cases

| Check | Planted | Observed | Match |
|---|---|---|---|
| Orphan-row quarantine | 2 | 2 | yes |
| Unparseable-date blank | 2 | 2 | yes |
| Age-cap to `"90+"` | 1 | 1 | yes |

### 4.6 Residual guard gate

`ok: true`, `presidio_finding_count: 0`, `legacy_finding_count: 0`, `triggered_by: []`.

## 5. Detection benchmark (Phase 4, US corpus, 550 records / 1314 gold spans)

| Tool | Strict P | Strict R | Strict F1 | Legacy F1 | Macro-F1 | Gap rate |
|---|---|---|---|---|---|---|
| phi_engine (own detection surface) | 0.4478 | 0.3196 | **0.3730** | **0.6029** | 0.6590 | 0.5928 |
| Presidio tuned 2.2.363 | 0.2969 | 0.4094 | 0.3442 | 0.5703 | 0.6856 | 0.2466 |
| Presidio stock 2.2.363 | 0.2774 | 0.3721 | 0.3178 | 0.5456 | 0.6447 | 0.2466 |
| spaCy en_core_web_sm 3.8.13 | N/A (adapter does not compute strict) | | | 0.3884 | 0.4938 | 0.1476 |
| philter, pydeid, clinideid, physionet_deid, modified_deidentify, aws_comprehend_medical, azure_health | not_run | | | | | |

**On this benchmark**, phi_engine's own detection surface has the highest strict-protocol F1 (0.373) and legacy-overlap F1 (0.603) of the three fully-run tools, but also the highest structural gap rate (59.3% vs Presidio's 24.7%) -- phi_engine's regex catalog covers fewer of the US corpus's HIPAA Safe Harbor 18-category taxonomy (no dedicated recognizers for e.g. device identifiers, biometric identifiers, most fax-vs-phone disambiguation, health-plan/account numbers) than Presidio's larger predefined + custom-recognizer set. The honest reading: on the identifier types phi_engine's regex catalog DOES cover (SSN, MRN, email, US phone, ISO/text dates, age-over-89, street address, name-prefix heuristics), it detects them more precisely than Presidio does on the same corpus; it structurally cannot compete on categories it has no recognizer for at all. This matches the plan's stress-test positioning: phi_engine is not claimed to be a free-text/general-PHI NER competitor; see `docs/SOTA_COMPARISON.md`.

Contrast with the India report (`docs/JURISDICTION_EVIDENCE_REPORT_IN.md` Section 6): phi_engine's advantage over Presidio is much larger on the India corpus (strict F1 0.513 vs 0.101, a 5x gap) than on the US corpus (0.373 vs 0.318/0.344, ~1.1x). This is the expected, disclosed asymmetry: Presidio's predefined recognizer set is US/HIPAA-oriented and structurally weak on India-specific identifiers (no Aadhaar/PAN/Indian-voter-ID recognizers), while on its home turf (US identifiers) Presidio is more competitive.

## 6. Claim statements (bounded)

- "phi_engine redacted 716/719 (99.6%) gold-annotated PHI cells on the seed-42 `PaperDemoUS` synthetic clinical-study corpus (60 subjects, 3 CRF forms); the 3 unredacted cells are dates whose deterministic per-subject jitter offset computed to zero for one subject, a disclosed 1/61-probability property of the SANT jitter algorithm, not a detection failure."
- "On the seed-42 US synthetic corpus (550 records, 1314 gold spans), phi_engine's own pattern-detection surface achieves the highest strict-protocol F1 (0.373) among the three fully-evaluated baselines (Presidio stock 0.318, Presidio tuned 0.344), while also having the highest structural-gap rate (59.3% vs 24.7%) -- phi_engine covers fewer HIPAA Safe Harbor categories overall but detects the categories it covers more precisely on this corpus."
- "Zero residual PHI findings (presidio + legacy value-free scanners, OR-combined) in the published staging tree for this run."

The words "fail-proof" and unscoped "100% accurate" do not appear above or anywhere in this report.

## 7. Limitations

Same limitations class as the India report (synthetic-only, single-cohort-per-seed, circularity of the phi_engine-vs-own-corpus benchmark, no clinician/counsel review, free-text NER explicitly out of scope). US-specific addition: the held-out-seed anti-tuning replication (Step 5.6) was run for India only per the plan's scope; US was not independently seed-replicated in this evidence pass.

## Reproduction

```
python -m harness.run_phi_system --study PaperDemoUS --jurisdiction us --seed 42 --n-subjects 60 --out-dir benchmarks/results/phi-system-us
python -m benchmarks.presidio_adapter --corpus-dir corpus/us --output-dir benchmarks/results/presidio-stock-us --profile stock -v
python -m benchmarks.presidio_adapter --corpus-dir corpus/us --output-dir benchmarks/results/presidio-tuned-us --profile tuned -v
python -m benchmarks.spacy_adapter --corpus-dir corpus/us --output-dir benchmarks/results/spacy-us --verbose
python -m benchmarks.phi_engine_adapter --corpus-dir corpus/us --output-dir benchmarks/results/phi-engine-us -v
python -m benchmarks.collect_results --results-dir benchmarks/results --output benchmarks/results/comparison_table.md
```
(Run under the repo's `.venv` -- Python 3.10+ required; see India report "Environment" section for why and how.)
