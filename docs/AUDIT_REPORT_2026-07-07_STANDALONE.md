# Standalone PHI Handling System — Audit Report (2026-07-07)

Method: every claim below is read directly from the working tree this
session (commit `b454b9a` plus uncommitted working-tree changes). Baseline:
`.venv/bin/python -m pytest -q` → **331 passed, 0 failed** (27.71s), matching
the `PAPER_EVIDENCE_PACK.md:71-73` baseline. Smoke commands
(`harness.run_phi_system --study PaperDemoIN/US --jurisdiction in/us --seed 42
--n-subjects 60`) both exit 0: `redaction_recall` 0.9917 (IN) / 0.9958 (US),
`residual ok=True`, `human_review_rate=0.0`. These two recall figures are
NOT a defect — `docs/JURISDICTION_EVIDENCE_REPORT_IN.md`/`_US.md` §5.2
explain the residual as date cells whose per-subject SANT jitter offset
computed to exactly zero this seed (a disclosed ~1/61 probability event,
confirmed identical at the held-out seed 1337 run), not an unredacted leak.

## Implemented (live, working)

| Area | Evidence |
|---|---|
| Classification engine | `phi_engine/security/phi_review.py` — pinned per-jurisdiction rules with authority citations (`_PINNED_RULE_SPECS`, 327–554), strictest-wins matching (`_ACTION_RANK`, 79–87), action→method mapping (`_ACTION_METHOD`, 94–102), `review_form_headers` (1261–1459) full SoT cross-verification + force-drop logic. |
| De-identification engine | `phi_engine/security/phi_scrub.py` — force-drop/keep/drop/HMAC-pseudonymize (`pseudo_id`, 1345)/SANT-date-jitter (`shift_date`, 1425)/cap (`cap_numeric`, 1495)/generalize (`generalize_value`, 1515)/band (`band_categorical`/`band_numeric`, 1542/1558)/small-cell-suppress (`suppress_small_cell`, 1581)/quarantine, orchestrated by `run_scrub` (2547–3108). |
| Residual guard | `phi_engine/security/phi_guard_gate.py::run_phi_guard_gate` — OR-combines Presidio (`presidio_gate.py`) + legacy pattern scan (`llm_source_gate.py`), fails if either finds PHI. |
| Header LLM classifier | `phi_engine/security/llm_detector.py::classify_headers` — headers-only prompt construction verified at lines 49–74 (`_build_prompt` receives only `headers`+`jurisdiction`, never row values); regex/Presidio fallback at `_fallback_classify` (87–121) when no LLM key. |
| Extraction utilities | `phi_engine/utils/_extraction_io/{file_discovery,sheet_split,jsonl_reader,file_io,clinical_dates}.py` — live, imported by `sheet_split.py` (via `phi_engine.utils.logging_system`), tested. |
| SoT consumer | `phi_review.py:798 load_sot_variable_signals(sot_root, form_name)` reads `{sot_root}/{stem}/joined/{stem}_joined_query_view.yaml` (fallback `audit/SoT_construction/{stem}/pdf/{stem}_policy.yaml`); `phi_scrub.py:2716` loads `sot_force_drop_by_stem` via `_load_approval_classifications`; `phi_scrub.py:3054`-area records PDF-policy provenance. Fail-soft to `{}` (`phi_review.py:807-808`) when SoT absent — name-only review proceeds. |
| Review queue (partial) | `llm_detector.classify_headers(..., review_queue_path)` writes `llm_uncertain.jsonl` via `_write_review_queue` (124–128); `run_phi_system.py` measures `human_review_rate` from it (290–293). |
| Scrub hold/approve (partial) | `phi_scrub.run_scrub(partial_on_review=True)` + `_load_approval_classifications(runs_dir, run_id)` (2343–2384) reads `phi_handling_approval.json`; `FormReviewApproval.to_json()` (250–270) defines the write-side shape. |

## Missing (specified, not yet built — this is the task this plan executes)

| Gap | Evidence |
|---|---|
| No portable workspace root | `config.py:171` `BASE_DIR = Path(__file__).resolve().parents[2]` — always the repo the package lives in; no env override. Every workspace-relative constant (`INTAKE_DIR`, `ORGANIZED_DIR`, `DATA_DIR`, `OUTPUT_DIR`, `STUDY_CONFIG_DIR`) cascades from it. |
| No symlink intake | `grep -rn "os.symlink" phi_engine/` returns nothing outside doc comments; no `intake.py` module exists. |
| No organizer | No module routes `.xlsx/.xls/.csv/.pdf/.json` raw inputs into dataset JSONL; `sheet_split.py`/`file_discovery.py` exist but nothing calls them from a standalone entry point. |
| Classification doesn't reach the scrubber for novel headers | Per-study `phi_scrub.yaml` is copied verbatim from `_defaults/phi_scrub.yaml` (`run_phi_system.py:_ensure_study_config`, 114-179) — a header classified `jitter_date`/`pseudonymize`/`cap` by `phi_review` that has NO matching pattern in the RePORTaLIN-specific defaults is scrubbed as a no-op KEEP (published raw) unless it also trips `force_drop_headers`. Classification and scrub-config are two separate systems today; nothing threads one into the other for a study whose headers are not Indo-VAP-shaped. |
| No CLI / no standalone driver | No `python -m phi_engine ...` entry point; the only runnable path (`harness/run_phi_system.py`) is corpus-coupled (see below). |
| No decision-feedback application | Nothing reads `decisions.jsonl` back into a later run. `phi_engine/cli/phi_review.py` (interactive) writes decisions but no loader threads them into `review_form_headers`'s `confirmed_keep_headers` or force-drop set on a subsequent run. |
| No deterministic value profiler | Nothing inspects column VALUES (locally, in-process) to auto-clear closed categoricals or escalate a name-clean header whose values are PHI-shaped. |
| No prompt egress gate | `LLMClient.complete` (`config.py:625-636`) dispatches straight to the provider; no `phi_gate_check` on the outbound prompt (prior-audit C2 open). |

## Broken (present but non-functional — verified this session)

| Break | Evidence |
|---|---|
| Orchestrator `skills_root()` points outside the repo | `phi_engine/utils/skill_protocol.py:99-101` returns `config.BASE_DIR / "plugins" / "report-ai-study-pipeline" / "skills"`; `find . -maxdepth 1 -name plugins` → no match. Every `invoke_skill()` call raises `SkillInvocationError` before a subprocess is even spawned. |
| Every skill `run.py`'s `_REPO_ROOT` resolves one directory ABOVE the repo | `phi_engine/skills/{report-ai-study-pipeline,phi-scrubbing,phi-classification,audit-verification,header-extraction}/scripts/run.py` each open with `_REPO_ROOT = Path(__file__).resolve().parents[5]`. At the actual depth (`phi_engine/skills/<skill>/scripts/run.py`, 4 parents to repo root), `parents[5]` is the directory containing the repo, not the repo itself — this affects ALL FIVE skill entrypoints, not only the orchestrator's own `run.py`, and is independent of the `skills_root()` break above (this one fires even on a direct `python .../run.py` invocation, bypassing `invoke_skill` entirely). |
| Orchestrator P1b import is missing | `phi_engine/skills/report-ai-study-pipeline/scripts/run.py:789` `from scripts.source_truth.generate_lean_outputs import main` — `scripts/` in this repo contains only `extraction/forms_manifest.py`; `scripts.source_truth` does not exist → `ImportError`. |
| `phi-classification` and `audit-verification` skills import a module that does not exist in this repo | `phi_engine/skills/phi-classification/scripts/run.py:58` and `phi_engine/skills/audit-verification/scripts/run.py:35-36` both `from scripts.skills.extract_to_llm_source import ...` — that module lives only under `archive/plugin-setup-and-dictionary/` in this repo and was never ported. Both entrypoints raise `ImportError` before doing any work — this is a SEPARATE break from the orchestrator/`_REPO_ROOT` issues above (three independent reasons the plugin tree cannot run, not one). |
| Child skills the orchestrator invokes are archived | `dictionary-to-llm-source`, `dataset-deduplication`, `dataset-to-llm-source` (publish supervisor) exist only under `archive/plugin-setup-and-dictionary/` — deliberately archived by the previous plan (`docs/IMPLEMENTATION_PLAN.md` Part 3.1). |
| Duplicated engine copies exist and can silently drift | `phi_engine/skills/phi-classification/scripts/phi_review.py` is byte-identical to `phi_engine/security/phi_review.py` TODAY, but nothing imports it (`phi-classification/scripts/run.py` imports `scripts.skills.extract_to_llm_source`, not the local copy) — verified via `grep -n "^import phi_review\|^import phi_scrub\|^import phi_gate\|from phi_review import\|from phi_scrub import\|from phi_gate import" phi_engine tests harness` → no matches anywhere. `phi_engine/skills/phi-scrubbing/scripts/phi_scrub.py` has ALREADY drifted from `phi_engine/security/phi_scrub.py` (one-line `datetime` import diff at line 129-130) despite also being dead code (`phi-scrubbing/scripts/run.py:39` imports the real `phi_engine.security.phi_scrub`, not the local file) — proof the duplication is a live drift hazard even while unused. `phi_engine/skills/phi-scrubbing/scripts/phi_gate.py` is a third unreferenced copy. |
| `phi_alignment.py` imports an absent module | `phi_engine/security/phi_alignment.py:276-305` references `scripts.ai_assistant.llm_adapter`, which does not exist in this repo — the opt-in AI header-alignment path (`review_form_headers(..., aligner=...)`) cannot construct a working aligner without it. Not exercised by any live call site (aligner defaults to `None`), so it is dormant rather than crashing, but it is unusable as shipped. |
| `run_phi_system.py` is corpus-coupled | Line 196: `from generators.study_tabular import IndiaStudyTabularGenerator, USStudyTabularGenerator` inside the PHI system's own execution path. Its measurement section is hardcoded against that generator's exact form names (`1A_Screening.jsonl`, `2_Demographics.jsonl`, `3_Labs.jsonl` at lines 455–463) and column set (`id_columns_by_form`/`date_columns_by_form`, 456-463). Real (non-synthetic) study data cannot be substituted without editing this file. Jurisdiction is hardcoded to `choices=["in", "us"]` (line 185) even though rulebooks/generators for `eu/br/au/ug` exist on the corpus side. |
| `header-extraction` skill is standalone-safe internally but unreachable | Its `run.py` does not depend on `scripts.skills.*` (self-contained row-1 header reader), but the same `_REPO_ROOT = parents[5]` break above still makes it unrunnable through the normal invocation path, and `invoke_skill()` can never find it (`skills_root()` break). |

## Carried prior-audit issues (`docs/AUDIT_REPORT_2026-07-06.md`)

- **C1** — LLM tool guards (`llm_safe_tool`, `guard_llm_output`, `validate_llm_read_path` in `phi_engine/security/llm_tool_guard.py:49,74,85`) have no non-test call sites; the LLM boundary is enforced structurally (headers-only prompt construction) but the explicit guard functions are not wired into the request path.
- **C2** — egress direction unscreened: `LLMClient.complete` sends whatever prompt string it is given straight to the provider once `PHI_ALLOW_EXTERNAL_LLM=true`; `guard_llm_output` only scans the return value, never the outbound prompt. Addressed by this refactor's Phase 5 (`PHIEgressBlockedError` prompt gate) — still a defense-in-depth layer on top of, not a replacement for, headers-only prompt construction.
- **C3** — blocking regex tier false negatives remain open (US phones, unhyphenated SSNs, bare names WARN-only, street addresses, ages >89, MRN formats, "Jan 5 2013" dates — `phi_engine/security/phi_patterns.py:215-265`). Not addressed by this refactor; documented as a standing limitation in `docs/GAP_ANALYSIS_BEST.md`.
- **H1** — `no_real_phi_static_validator` remains a 5-substring check, effectively a rubber stamp.
- **H4** — weak validators (taxonomy schema-only, citation any-string, format_parse dead else-branch) unchanged.
- **M3** — `_write_review_queue` (`llm_detector.py:124-128`) writes relative to whatever `queue_path` the caller supplies, bypassing zone-guard resolution when the caller passes a bare relative path; untested. Fixed in Phase 4 of this refactor (default queue path resolved via `config.STUDY_AUDIT_DIR`).
- **H2/H3/M-series** — MIA smoke-only, Presidio strict-F1 scoring bug, reproducibility gaps: relevant to benchmark claims, out of scope for the standalone-refactor path, carried forward as open items.

## Scope note

This report is Phase 1's deliverable, written before the refactor (Phase 2+)
begins. All "Missing" and "Broken" rows above are the acceptance criteria
the remaining phases close; "Implemented" rows are the engine this refactor
reuses without modification (aside from the config/CLI/pipeline additions
layered around it).


## Addendum — discovered and fixed during Phase 2 implementation

Not known at Phase 1 write time; found while wiring `phi_engine/pipeline/run.py`
against the live engine, fixed in place (all covered by the 331-baseline
regression suite plus new smoke tests), and recorded here for the audit trail.

- **New bug — `load_study_privacy_config` bare `import config`** (`phi_engine/security/phi_review.py:884`, pre-fix): identical failure class to C1's `secure_env.py` fallback — no top-level `config` module exists in this repo, so this call always raised `ModuleNotFoundError`. Dead/unreachable in every path this session's baseline exercised (no test called it), but directly blocking for `run_pipeline`, which must call it. Fixed: qualified `import phi_engine.config.config as config`. Same fix applied to `phi_engine/security/secure_env.py::_resolve_markers` (Phase 2 step 5) so `assert_write_zone`/`assert_output_zone` (called live from `phi_scrub.run_scrub`) validate against the CURRENT (possibly `PHI_WORKSPACE`-relocated) config instead of a path that only coincidentally matched before any workspace override existed. `phi_engine/utils/cleanup_verifier.py:306,333` and `phi_engine/utils/log_hygiene.py:219` have the same bare-`import config` defect but are NOT on the standalone pipeline's call path (`cleanup_verifier` additionally depends on the archived `scripts.extraction.header_store`, and `log_hygiene.install_phi_redactor_best_effort` has zero live callers) — left as-is, noted here rather than silently fixed out of scope.
- **New bug — jurisdiction-blind adversarial probe** (`phi_engine/security/phi_review.py::_adversarial_header_validation`, pre-fix): probed `synthetic_aadhaar_header` unconditionally expecting `DROP`, but Aadhaar is an India-only identifier — a single-jurisdiction `("USA",)` `RuleBundle` correctly does not recognize it, so the probe failed EVERY TIME for a USA-only study, holding EVERY form on EVERY run regardless of header content. Verified before/after with `_adversarial_header_validation` called directly against `("USA",)`, `("INDIA",)`, `("USA","INDIA")` bundles. This is the exact mechanism `--jurisdiction us` studies need (the plan's CLI contract is single-jurisdiction `in|us`), so this bug would have made the refactored system's classification gate permanently non-functional for every US study. Fixed: the Aadhaar probe now only runs when `"INDIA"` is in `privacy_config.jurisdictions`; the other four probes (participant id / visit date / email / culture result) are verified jurisdiction-agnostic and stay universal.
- **New bug — `synthesize_study_config` publishing free text raw** (`phi_engine/pipeline/synthesize_config.py`, pre-fix, caught by the standalone system's OWN measurement, not a pre-existing defect): the first implementation blanket-added every `SUPPRESS`-classified header (e.g. a free-text `NOTES`/`COMMENT` column) to `suppress_small_cell_fields`. `phi_scrub.run_scrub` has a documented dual path for `SUPPRESS` (Note 32, `phi_scrub.py:2844-2852`): force-drop by DEFAULT, small-cell-clamp only when `field_is_suppress_small_cell` is true — clamping is meant for numeric COUNT columns, and is a no-op pass-through on a string. Adding the pattern flipped every free-text SUPPRESS header onto the clamp (no-op) path, publishing it RAW. Caught via a manual CLI smoke test (`NOTES` published unredacted) before any stress-test phase ran. Fixed by removing the auto-add; the header now correctly force-drops via `run_scrub`'s own action=="suppress" detection with no scrub-config entry needed.
- **New bug — `synthesize_study_config` `KEEP` classification overriding a more specific packaged rule** (`phi_engine/pipeline/synthesize_config.py`, pre-fix): the first implementation added every `KEEP`-classified header to `keep_fields` as a defense-in-depth measure. `keep_fields` is priority-1 (checked FIRST in `_scrub_row`, short-circuits every other rule). `IC_SCRNNUM` is classified `keep` by the pinned INDIA regulation rules (no INDIA rule specifically names screening-number identifiers) but the PACKAGED defaults already pseudonymize it via a specific `id_fields` pattern (`^I[CS]_SCRNNUM$`); forcing it into `keep_fields` overrode that pattern and published the raw screening number unchanged. Caught by `tests/test_run_phi_system.py`'s strict `pseudonyms.regex_pass_count == pseudonyms.cells_checked` assertion after fixing the row-alignment bug below (see next item) surfaced real per-cell values instead of a trivial 100%-pass artifact. Fixed by removing `KEEP` from the synthesis loop entirely: a header with no matching pattern anywhere is *already* published unchanged by default, so no explicit rule was ever needed for `KEEP`.
- **New bug — the demoted harness's own row-alignment helpers were unverifiable** (`harness/run_phi_system.py::_align_pre_post` / `_row_index_map`): the ORIGINAL bodies were elided in every read this session took of the pre-refactor file (only the docstring + the final `return` line were visible, and the file was untracked in git with no history to recover), so the demotion (step 11) could not literally "keep this logic unchanged" — it had to be re-derived. The original clearly matched rows by IDENTITY (its docstring said "Alignment is by... SUBJID"), which is unsound once `SUBJID` is pseudonymized: a first re-derivation matched pre/post rows by raw `SUBJID` string equality against the ALREADY-PSEUDONYMIZED post value, which never matches — this silently makes EVERY row look "quarantined" (`post_row is None`) to the redaction-recall check, which trivially reports 100% recall regardless of real leaks (a dangerous false-confidence measurement bug, not merely a missed metric). Rewritten to align POSITIONALLY: the gold ledger's `expected_action == "quarantine_row"` entries name exactly which pre-scrub row indices never survive; every other pre-scrub row is paired with the next post-scrub row in file order (matching how `phi_scrub._scrub_file` appends kept rows). Verified this reproduces the documented baseline recall exactly (0.9917 IN / 0.9958 US, byte-identical to `docs/JURISDICTION_EVIDENCE_REPORT_{IN,US}.md`) once the two synthesis bugs above were also fixed.

## Cleanup executed this phase (step 12/13)

Per `grep -rn "phi_engine.skills\|skills/report-ai\|skill_protocol" phi_engine tests harness` (re-run after each deletion, zero remaining references): removed `phi_engine/skills/{phi-classification,phi-scrubbing,header-extraction,audit-verification,report-ai-study-pipeline}/` (all five import the now-orphaned `skill_protocol` AND independently fail via either the `_REPO_ROOT = parents[5]` break or a missing `scripts.skills.extract_to_llm_source`/`scripts.source_truth` import — every one superseded by `phi_engine/pipeline/run.py`, or in `audit-verification`'s case simply unrestorable without porting the archived verifier) and `phi_engine/utils/skill_protocol.py` (zero remaining importers after the five skill dirs are gone). `phi_engine/skills/phi-rulebook/` is KEPT — verified independent: `rulebook_cli.py` does not import `skill_protocol`, and its own `SKILL.md` documents it as "a shared-module operator command, not an orchestrator subprocess." `header-extraction`'s row-1 header logic was NOT reused by the organizer (`phi_engine/pipeline/organize.py` uses `sheet_split.py` + `pandas` directly), confirming the plan's fallback instruction to delete it.

## Addendum — Phase 3 (SoT producer restoration)

`archive/plugin-setup-and-dictionary/sot-lean-generator/scripts/{study_intake.py, extract_sources.py, generate_pdf_aware_candidate.py, generate_lean_outputs.py}` were ported to `phi_engine/sot/` with imports rewritten to `phi_engine.*`. Two facts discovered during the port that the original plan step 13 did not anticipate:

- **`scripts.ai_assistant.sot_joined_view` (the module `generate_lean_outputs.py`/`generate_joined_query_view.py` import `build_joined_query_view`/`write_joined_query_view_yaml` from) does not exist anywhere in this repository — not live, not archived.** It was reimplemented from scratch as `phi_engine/sot/sot_joined_view.py`, derived from (a) the two actual callers' shapes (`_write_dataset_schema`/`_policy_phi_actions` in `generate_lean_outputs.py` define the schema JSON `{columns: [{name, source_order, phi_action?}]}`; `build_candidate()` in `generate_pdf_aware_candidate.py` defines the policy YAML `{variables: {NAME: {pdf_question, phi, ...}}}`) and (b) the CONSUMER contract at `phi_engine/security/phi_review.py:798-856` (`load_sot_variable_signals`), which is authoritative for the output shape. This is a reimplementation, not a port — there was nothing to port.
- **`generate_form()`'s archived orchestration shelled out via `subprocess` to `check_lean_policy.py` (a validator, not in the plan's port list) and `scripts/source_truth/diff_against_gold.py` (does not exist anywhere, archived or live).** The ported `generate_form()` calls the source-pack builder and PDF-candidate builder as in-process function calls instead of subprocess/shell-out (removing the hardcoded `plugins/report-ai-study-pipeline/skills/...` path construction entirely), and drops the `check_lean_policy.py` validation step and the `diff_against_gold.py` anchored-gold branch (the latter was already dead for any non-Indo-VAP deployment: it only ever fires when `data/SoT/<study>/<form>_policy.lean.yaml` exists, which no new study will have). The discrepancy-detection safety net that routes an unresolved PDF/dataset mismatch to a human-review report instead of publishing it is retained.
- **`generate_pdf_aware_candidate.py`'s ~1000 lines of curated annotation tables (`ANNOTATION_ALIASES`, `NON_VARIABLE_ANNOTATIONS`, `TRUE_PDF_VARIABLES_WITHOUT_DATASET_HEADER`, the `10_TST`/`6_HIV` form-specific calibration logic, the hardcoded `STUDY_NAME`) were ported UNCHANGED.** These are Indo-VAP-study-specific curated data, not general-purpose PDF-annotation-alignment logic. A new deployment pointing this component at its own annotated CRF PDFs will find these tables empty of relevant entries and must either clear or replace them with its own study's curated aliases — the SoT producer is NOT "portable to any project's annotated PDFs" out of the box, only its plumbing (intake, dataset-schema join, joined-view publish, fail-soft wiring into `run_pipeline`) is.
- Wired into `run_pipeline`: when `config.ANNOTATED_PDFS_DIR` contains at least one `*.pdf`, `phi_engine.sot.generate_sot(study)` runs before header classification. Fail-soft by design — any exception or non-zero return is recorded in `PipelineResult.sot_generation_error` and never aborts the PHI pipeline (`load_sot_variable_signals` already tolerates a missing/malformed SoT tree, per its own docstring).
- End-to-end verification is FIXTURE-VERIFIED ONLY, per the plan's own contingency clause: a fixture-level unit test (`tests/test_sot_producer.py::test_joined_query_view_feeds_phi_review_sot_signals`) hand-builds a policy YAML + schema JSON, joins them, and confirms `load_sot_variable_signals` reads the result correctly. A second smoke test built a `reportlab`-generated PDF (ordinary page text, not engineered pdfplumber-visible form-field annotations) and confirmed `generate_sot` runs to completion (`rc=0`) and publishes a joined view without crashing — this proves the PLUMBING works, it does NOT prove generic annotation-binding quality against an arbitrary non-Indo-VAP PDF layout, which the curated-table limitation above makes structurally unlikely to bind correctly without per-study curation.

## Addendum — Phase 7 (value-profiler over-escalation bugfix)

During the Phase 7 evidence re-run, `PaperDemoIN`'s measured `redaction_recall`
came back as `0.9944` instead of the documented `0.9917` baseline (same seed,
same key, byte-identical pre-scrub data and scrub-config hash across repeat
runs) — a real BEHAVIOR CHANGE, not noise. Root cause: `3_Labs.jsonl`'s
`TBTXDT` column (a genuine specimen-collection date) has no INDIA-jurisdiction
pinned rule matching it by name, so `phi_review` classifies it `action: keep`
with `matched_rules: []`. The Phase 5 value profiler's ESCALATION rule
(`phi_engine/pipeline/run.py`) treated EVERY `keep`-classified header as
"published raw," so `TBTXDT`'s genuine ISO-date values (100% match on the
`DATE_ISO` blocking pattern) tripped the value-profile-conflict check and
force-dropped the column — even though the packaged
`phi_engine/config/_defaults/phi_scrub.yaml` `date_fields` catch-all pattern
(`(?:_?DAT\d*$|_DT$|DATE\d*$|_date$)`) already matches `TBTXDT` and would have
SANT-jittered it correctly. Net effect: no PHI ever leaked (a force-dropped
column cannot leak), but real clinical data was discarded unnecessarily —
a utility regression, not a security one, and it inflated `redaction_recall`
by shrinking the denominator of publishable cells.

Fix: `run_pipeline` now loads the CURRENT effective scrub config
(`phi_scrub.load_scrub_config(study=study)`) once per run and computes
`published_raw_headers` per form — a header only counts as "published raw"
(profiler-escalation-eligible) if NONE of `field_is_keep` / `field_is_date` /
`field_is_birthdate` / `field_is_id` / `field_is_drop` / `cap_rule_for` /
`generalize_rule_for` / `band_rule_for` / `field_is_suppress_small_cell`
already protect it. This is passed into `review_form_headers(...,
published_raw_headers=...)` and used to gate the ESCALATION rule. Verified:
`TBTXDT` is now correctly SANT-jittered (same per-subject offset as
`COLLDAT` in the same row, confirming correct linkage), and
`redaction_recall` for `PaperDemoIN` is back to `0.9916550764951322` —
matching the documented `0.9917` pre-refactor baseline exactly (the
residual is the same disclosed ~1/61 zero-offset date-jitter event, per
`docs/JURISDICTION_EVIDENCE_REPORT_IN.md` §5.2, NOT a new leak).

Regression check: `tests/test_value_profiler.py`'s escalation end-to-end
test used `SITE_CODE` as its "unexpectedly-named column" fixture header —
but `SITE_CODE` itself matches the packaged `id_fields` pattern
`(?:facility|center|site|clinic|hospital)[-_]?(?:id|code|num|no)`, so it was
NEVER a genuine name-blind-spot case; the fix correctly stops escalating it
(already protected via HMAC pseudonymization). The fixture header was
swapped to `PROCESS_TAG` (verified zero overlap with every packaged
keep/date/id/drop/cap/generalize/band/suppress pattern). Full suite:
357 passed, 0 failed (was 356/1 before this fixture correction).
Separately, `docs/STRESS_TEST_REPORT.md`'s `NOTES`/`COMMENT` claim was
corrected: those two headers were always protected via a THIRD, unrelated
mechanism (`usa_free_text_suppression` jurisdiction rule → `action:
suppress` → `phi_scrub.run_scrub`'s documented SUPPRESS dual-path, Note 32)
— never `action: keep`, so they never went through `profile_escalations` at
all; the stress run's actual 2 escalations trace to `xlsx_phi_corpus.jsonl`'s
`text`/`gold_spans` sidecar columns (confirmed by direct instrumentation of
the escalation call site). No leak either way; the report now names the
correct mechanism.

## Addendum — Phase 7 (final independent audit findings, fixed)

A fresh-context subagent with no access to this session's reasoning was
dispatched to adversarially re-verify every claim above by re-running
commands itself. Verdict: PASS-WITH-CONCERNS, with two concrete findings
beyond confirming the TBTXDT fix, test counts, and `generators/` decoupling.
Both are fixed as of this addendum:

1. **Stale-staged-file publish bypass (real correctness bug, now fixed).**
   `phi_engine/pipeline/run.py`'s `_clear_stale_staging` cleared only the
   `.phi_scrub_complete` sentinel and `quarantine/*.jsonl` — never the staged
   dataset JSONLs themselves. A prior run that scrubbed successfully but then
   failed the residual guard gate (`guard_ok=False` → "nothing published")
   leaves its scrubbed files sitting in `tmp/<study>/datasets/`, uncleaned. A
   LATER run for the same study — even one approving a completely different
   set of forms — would publish that leftover data alongside the current
   run's freshly-copied forms (the publish loop moves every `*.jsonl` in
   staging, not just the current run's `approved_forms`), bypassing that
   run's classification/scrub/approval pipeline entirely. Confirmed via a
   synthetic repro (a hand-seeded `stale.jsonl`, absent from the current
   approval JSON, was still published) before the fix, and confirmed absent
   after. Fix: `_clear_stale_staging` now unlinks every `*.jsonl` directly
   under `staging_dir` (`config.STAGING_DATASETS_DIR`, study-scoped — does
   not touch other studies or the separately-cleared `quarantine/`
   subdirectory) before the current run's `approved_forms` are copied in.
   Regression test: `tests/test_stress_standalone.py::test_stale_staged_file_never_publishes_without_current_approval`.
   Full suite after fix: 358 passed (was 357), 0 failed.
2. **Repo-root runtime scaffolding from the documented evidence commands.**
   `harness/run_phi_system.py`'s evidence-command invocation (as documented
   in `README.md`) does not set `PHI_WORKSPACE`, so `phi_engine`'s default
   workspace (repo root) is used — by design, matching this entry point's
   pre-refactor behavior, and unrelated to the new standalone `phi_engine`
   CLI's own workspace handling (which every stress/unit test exercises via
   an explicit `--workspace`/`PHI_WORKSPACE`, and which the Phase 6 audit
   independently confirmed leaves ZERO repo-root residue). This is not a new
   bug, but re-running the documented evidence commands does leave
   `output/`, `intake/`, `organized/`, `data/` directories at the repo root,
   which is confusing working-tree noise. Fixed by adding these four
   directories (anchored to repo root only, confirmed zero tracked files
   under any of them beforehand via `git ls-files`) to `.gitignore`, with a
   comment explaining they are the default-workspace evidence-command
   scaffolding, not a source of truth (that remains `benchmarks/results/`).

Both fixes are covered by the full 358-test suite. No other correctness
concern was raised; the auditor's remaining note (second-granularity run IDs
theoretically colliding if two runs of the same study start in the same
wall-clock second) was flagged as lower-priority and not independently
reproduced — noted here for future awareness, not fixed in this pass (no
evidence it manifests in any tested workflow: the CLI is not invoked
concurrently for the same study anywhere in this codebase or its tests).