# Paper Evidence Pack

Date: 2026-07-07. **Current scope: USA-only.** Non-USA generators (`generators/in|eu|br|au|ug`), non-USA rulebooks, and `docs/JURISDICTION_EVIDENCE_REPORT_IN.md` are removed from the active tree. Every claim in `docs/paper/manuscript.md`, `docs/JURISDICTION_EVIDENCE_REPORT_US.md`, and `docs/SOTA_COMPARISON.md` is keyed below to an artifact file, and where applicable its sha256 hash, plus the exact command that produced it. Environment note (applies to every command below): Python 3.9.21 (system default) is incompatible with this codebase (`requires-python = ">=3.10"` in `pyproject.toml`; `phi_engine/security/phi_scrub.py` uses PEP 604 `isinstance(x, int | float)` syntax). All commands were run under `.venv` (`uv venv --python 3.12 .venv`; `uv pip install -r requirements.txt` with `pycanon` relaxed to `>=1.0.1` from the pin `==1.0.1`, which conflicts with `reportlab>=4.2.5` also in requirements).

## 1. Claim -> Artifact map

| Claim | Artifact file | sha256 | Reproduction command |
|---|---|---|---|
| Canonical corpus (historical multi-jurisdiction manifest hash retained; **active claim surface is USA/HIPAA**) | `corpus/MANIFEST.json` | `5b413b9df27985c961a9b4b688efd9706e194607f2d1bdfeb97eb7054c34e2be` | `python -m harness.generate_corpus --seed 42 --jurisdiction us --out-dir corpus` (non-USA jurisdictions removed from active scope) |
| All 7 structural validators PASS | `validation_report.json` | `769089c19633953a91249f8dac37da1e67ceff29d0f69155d0440f58ccc29fff` | `python -m harness.run_all_validations --corpus-dir corpus --manifest corpus/MANIFEST.json --output validation_report.json` |
| MIA smoke-test AUC 0.4162 (deterministic, not external validation) | `mia_report.json` | `1033f0a8fc742636b8be9163b35ed8a6366815bddaa18d0d578225b3cdb6d923` | `python -m harness.mia_framework --corpus-dir corpus --output mia_report.json` |
| Release-evidence chain (manifest/validation/MIA hashes cross-referenced) | `release_evidence.json` | `2ee1895c85a69e0bada277b270ba25410882ad9f25ea1ae3b6ceea7aca240120` | `python -m harness.release_evidence --corpus-dir corpus --manifest corpus/MANIFEST.json --validation-report validation_report.json --mia-report mia_report.json --output release_evidence.json` |
| ~~India rulebook pinned-seed provenance~~ | ~~`phi_engine/config/_defaults/phi_rulebook/rulebook_v1_INDIA.json`~~ | ~~removed~~ | **REMOVED** with India evidence report / non-USA rulebooks |
| USA rulebook pinned-seed provenance | `phi_engine/config/_defaults/phi_rulebook/rulebook_v1_USA.json` | `5960e1156847781294c951062599d0b302aafb92ca5c087dab36f06ea21db8eb` | (read directly) |
| ~~India effective scrub config~~ | ~~`phi_engine/config/PaperDemoIN/phi_scrub.yaml`~~ | ~~removed~~ | **REMOVED** (`PaperDemoIN` / India path out of scope) |
| USA effective scrub config (packaged defaults verbatim, no addition needed) | `phi_engine/config/PaperDemoUS/phi_scrub.yaml` | `b60de310e6ec3b18e147887a77afd207598edd34551b7a685dc23c1dfa792b5c` (byte-identical to `phi_engine/config/_defaults/phi_scrub.yaml`) | written by `harness/run_phi_system.py::_ensure_study_config` on first run for study `PaperDemoUS` |
| ~~India system run~~ | ~~`benchmarks/results/phi-system-in/phi_system_result.json`~~ | ~~removed~~ | **REMOVED** — `docs/JURISDICTION_EVIDENCE_REPORT_IN.md` deleted; do not cite as live |
| USA system run: redaction recall 99.58% (716/719), zero residual, all fail-closed checks matched | `benchmarks/results/phi-system-us/phi_system_result.json` | (`scrub_config_hash` = `32e0faeb02c4ea579af7311111fde1575e5306c135a36e240c2b55345d5ecc4f`) | `python -m harness.run_phi_system --study PaperDemoUS --jurisdiction us --seed 42 --n-subjects 60 --out-dir benchmarks/results/phi-system-us` |
| ~~Held-out replication (India seed 1337)~~ | ~~`benchmarks/results/phi-system-in-heldout/phi_system_result.json`~~ | ~~removed~~ | **REMOVED** with India evidence path |
| ~~Gold ledger for India run~~ | ~~`benchmarks/results/phi-system-in/gold_ledger.jsonl`~~ | ~~removed~~ | **REMOVED** |
| ~~Pre-scrub copy of staged forms (India)~~ | ~~`benchmarks/results/phi-system-in/pre_scrub/*.jsonl`~~ | ~~removed~~ | **REMOVED** |
| Detection benchmark: phi_engine vs. Presidio stock/tuned vs. spaCy (**USA** dual scoring profile; non-USA rows historical) | `benchmarks/results/comparison_table.md` + `.json` | (generated file, not individually hashed) | `python -m benchmarks.collect_results --results-dir benchmarks/results --output benchmarks/results/comparison_table.md` |
| phi_engine detection surface (**USA**) | `benchmarks/results/phi-engine-us/phi_engine_benchmark_result.json` | (per-file) | `python -m benchmarks.phi_engine_adapter --corpus-dir corpus/us --output-dir benchmarks/results/phi-engine-us -v` |
| Presidio stock (**USA**) | `benchmarks/results/presidio-stock-us/presidio_stock_benchmark_result.json` | (per-file) | `python -m benchmarks.presidio_adapter --corpus-dir corpus/us --output-dir benchmarks/results/presidio-stock-us --profile stock -v` |
| Presidio tuned (**USA**) | `benchmarks/results/presidio-tuned-us/presidio_tuned_benchmark_result.json` | (per-file) | `python -m benchmarks.presidio_adapter --corpus-dir corpus/us --output-dir benchmarks/results/presidio-tuned-us --profile tuned -v` |
| spaCy baseline (**USA**) | `benchmarks/results/spacy-us/spacy_benchmark_result.json` | (per-file) | `python -m benchmarks.spacy_adapter --corpus-dir corpus/us --output-dir benchmarks/results/spacy-us --verbose` |
| Camera-ready tables (dataset stats, system results, benchmark comparison), auto-generated from the artifacts above | `docs/paper_assets/table_{corpus,system,benchmark}.{md,tex}` | (generated; not individually hashed) | `python -m harness.make_paper_assets --results-dir benchmarks/results --out-dir docs/paper_assets` |
| SOTA numbers (Section 1 of `docs/SOTA_COMPARISON.md`) | external sources, each verified via web search 2026-07-07 against its primary source (arXiv abstracts/PDFs, PMC articles, intuitionlabs.ai review) | N/A (external) | see `docs/SOTA_COMPARISON.md` Section 1 citations column |
| `.venv/bin/python -m pytest -q` -> 331 passed, zero regressions across every full-suite run this session | (test run, not a static artifact) | N/A | `.venv/bin/python -m pytest -q` |

## 2. Reproduction command list, verbatim (Phases 1-4)

```bash
# Environment (once)
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt   # relax pycanon==1.0.1 -> >=1.0.1 first, see note above
.venv/bin/python -c "import presidio_analyzer, spacy; spacy.load('en_core_web_sm'); spacy.load('en_core_web_lg')"

# Phase 1 -- canonical corpus refresh (USA-only active scope)
.venv/bin/python -m harness.generate_corpus --seed 42 --jurisdiction us --out-dir corpus
.venv/bin/python -m harness.run_all_validations --corpus-dir corpus --manifest corpus/MANIFEST.json --output validation_report.json
.venv/bin/python -m harness.mia_framework --corpus-dir corpus --output mia_report.json
.venv/bin/python -m harness.release_evidence --corpus-dir corpus --manifest corpus/MANIFEST.json --validation-report validation_report.json --mia-report mia_report.json --output release_evidence.json

# Phase 2 -- clinical-study tabular corpus (USA evidence artifacts)
.venv/bin/python -m generators.study_tabular   # writes benchmarks/results/study_tabular_corpus/us/*.jsonl + gold_ledger.jsonl (deliberately outside corpus/ -- see Section 4)
.venv/bin/python -m pytest tests/test_study_tabular.py -q

# Phase 3 -- end-to-end system run (USA only; India path removed)
.venv/bin/python -m harness.run_phi_system --study PaperDemoUS --jurisdiction us --seed 42 --n-subjects 60 --out-dir benchmarks/results/phi-system-us
.venv/bin/python -m pytest tests/test_run_phi_system.py -q

# Phase 4 -- detection benchmark matrix (USA)
.venv/bin/python -m benchmarks.presidio_adapter --corpus-dir corpus/us --output-dir benchmarks/results/presidio-stock-us --profile stock -v
.venv/bin/python -m benchmarks.presidio_adapter --corpus-dir corpus/us --output-dir benchmarks/results/presidio-tuned-us --profile tuned -v
.venv/bin/python -m benchmarks.spacy_adapter --corpus-dir corpus/us --output-dir benchmarks/results/spacy-us --verbose
.venv/bin/python -m benchmarks.phi_engine_adapter --corpus-dir corpus/us --output-dir benchmarks/results/phi-engine-us -v
.venv/bin/python -m pytest tests/test_phi_engine_adapter.py -q

.venv/bin/python -m benchmarks.collect_results --results-dir benchmarks/results --output benchmarks/results/comparison_table.md
.venv/bin/python -m harness.make_paper_assets --results-dir benchmarks/results --out-dir docs/paper_assets
```

Note on scoring-profile flags: `presidio_adapter` and `phi_engine_adapter` must be run **without** `--scoring-profile strict_all_span` to capture both the primary (strict) and secondary (legacy overlap) views in one result JSON. Passing `--scoring-profile strict_all_span` explicitly makes `aggregate_precision/recall/f1` and `strict_all_span_precision/recall/f1` identical in the output (both computed from the strict scores), silently discarding the legacy-overlap secondary view -- discovered and corrected this session; both adapters now default to `legacy_overlap_coverable`, which populates both views.

## 3. Full test suite

```
.venv/bin/python -m pytest -q
```
331 passed (308 baseline at session start + 17 `tests/test_study_tabular.py` + 2 `tests/test_run_phi_system.py` + 4 `tests/test_phi_engine_adapter.py`), zero regressions across every intervening full-suite run this session.

## 4. Porting-gap and bug-fix disclosure (historical; India report removed)

Three defects were found and fixed while making `harness.run_phi_system` call `phi_engine.security.phi_scrub.run_scrub` directly:
1. `phi_engine/config/config.py::BASE_DIR` resolved one directory too deep after the port (fixed: `parents[2]` instead of `parent`, with `CONFIG_DIR` kept at config.py's own directory).
2. `phi_engine/security/phi_scrub.py` was missing `timedelta` in its `datetime` import (used at runtime in `shift_date`, causing an unconditional `NameError` on every date-jitter call).
3. A minimal compatible shim (`scripts/extraction/forms_manifest.py`) was written for `run_scrub`'s unconditional import of a module the full (un-ported) extraction pipeline owns.

A separate measurement-methodology bug (not a `phi_engine` defect) was found and fixed in this session's own new code: the first redaction-recall pass searched for a leaked value anywhere in the whole output file rather than in its own row, producing false-positive "leaks" for low-entropy values; and the gold ledger initially (incorrectly) treated every `AGE` cell as expected to change, when HIPAA Safe Harbor only requires capping ages over 89. ~~Full before/after narrative previously lived in `docs/JURISDICTION_EVIDENCE_REPORT_IN.md` Section 3.2 — that file is deleted; USA numbers remain in `docs/JURISDICTION_EVIDENCE_REPORT_US.md`.~~

A third, separate own-code bug (not a `phi_engine` defect) was found during Phase 6 final verification: `validators.common.corpus_files()` recursively globs every `corpus/**/*.jsonl` with no manifest scoping (`sorted(corpus_dir.rglob("*.jsonl"))`), so Phase-2 evidence artifacts first staged at `corpus/study_tabular/{in,us}/*.jsonl` were swept into the narrative-record-schema validators (`citation_validator`, `taxonomy_validator`, etc.) and failed `python -m harness.run_all_validations` with thousands of `BAD_SCHEMA`/`MISSING_AUTHORITY` issues -- the tabular rows correctly have no `text`/`gold_spans` fields (Step 2.6's own design), but any `.jsonl` under `corpus/` is validated against that schema regardless. Fixed by moving the evidence artifacts to `benchmarks/results/study_tabular_corpus/` outside `corpus/`.
