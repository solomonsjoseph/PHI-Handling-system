# Reproducibility

**Version:** 2026-07-06.task7
**Scope:** Commands and artifacts needed to reproduce the current evidence-first corpus, validation, benchmark, MIA smoke, and release-evidence workflow.

These commands assume repository dependencies are installed and are run from the repository root.

## 1. Generate the current release corpus

```bash
python -m harness.generate_corpus --seed 42 --jurisdiction all --out-dir corpus
```

Expected primary artifact: `corpus/MANIFEST.json`.

## 2. Validate the corpus and manifest

```bash
python -m harness.run_all_validations --corpus-dir corpus --manifest corpus/MANIFEST.json --output validation_report.json
```

Expected primary artifact: `validation_report.json`.

## 3. Run the stock Presidio strict benchmark

```bash
python -m benchmarks.presidio_adapter --corpus-dir corpus/us --output-dir benchmarks/results/presidio-stock --profile stock --scoring-profile strict_all_span --verbose
```

Expected primary artifacts under `benchmarks/results/`, including strict benchmark summary and raw prediction files. Raw prediction artifacts must not include raw record text.

## 4. Run deterministic MIA smoke evidence

```bash
python -m harness.mia_framework --corpus-dir corpus --output mia_report.json
```

Expected primary artifact: `mia_report.json`. This is a deterministic MIA smoke test only; it does not prove synthetic records are non-member-inferable against stronger attacks.

## 5. Build release evidence

```bash
python -m harness.release_evidence --corpus-dir corpus --manifest corpus/MANIFEST.json --validation-report validation_report.json --mia-report mia_report.json --output release_evidence.json
```

Expected primary artifact: `release_evidence.json`.

## Artifact list

Release reviewers should expect these durable artifacts for a reproducible evidence packet:

- `corpus/MANIFEST.json`
- `validation_report.json`
- `benchmarks/results/*`
- `release_evidence.json`

Optional or environment-dependent artifacts may include `mia_report.json` and external-provider benchmark outputs when credentials/licenses and approvals are available. External review, clinician review, and counsel review remain `PENDING` unless separate signed artifacts are provided.
