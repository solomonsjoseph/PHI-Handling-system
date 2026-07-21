# HIPAA 18-Identifier Tabular Corpus Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic USA-only tabular corpus whose canonical baseline contains exactly one mapped field for every HIPAA Safe Harbor category A–R, plus expected user/audit outputs and five initial mapping edge cases.

**Architecture:** A focused generator module owns the immutable A–R field specification, seeded row generation, semantic validation, and deterministic file rendering. A thin harness CLI writes the package and reports category coverage. Tests exercise the in-memory contract first, then real CSV/XLSX/JSONL output, edge-case validation, and the CLI.

**Tech Stack:** Python 3.10+, standard-library `csv`, `hashlib`, `hmac`, `json`, `zipfile`; existing `openpyxl`; `pytest`.

## Global Constraints

- USA jurisdiction only.
- Authority is 45 CFR 164.514(b)(2)(i)(A) through (R).
- Baseline generation must fail unless category coverage is exactly A–R once each.
- Values are seeded and entirely synthetic.
- PDF forms are out of scope.
- CSV and XLSX renderings are semantically and byte deterministically reproducible.
- Audit output contains no plaintext identifier values.
- Edge cases are written only after the baseline validates.

---

### Task 1: Canonical A–R model and seeded rows

**Files:**
- Create: `generators/hipaa_18_tabular.py`
- Modify: `generators/__init__.py`
- Test: `tests/test_hipaa_18_tabular.py`

**Interfaces:**
- Produces: `IdentifierSpec`, `HIPAA18Corpus`, `HIPAA18ValidationIssue`, `USHIPAA18TabularCorpusGenerator.generate(n_subjects)`.
- Reuses: `HIPAA_CATEGORIES` and existing synthetic value helpers from `generators.hipaa_safe_harbor`.

- [ ] **Step 1: Write failing A–R coverage and determinism tests**

```python
def test_baseline_has_every_hipaa_category_once():
    corpus = USHIPAA18TabularCorpusGenerator(seed=42).generate(n_subjects=18)
    assert [row["hipaa_category"] for row in corpus.dictionary_rows] == list("ABCDEFGHIJKLMNOPQR")
    assert len(corpus.dictionary_rows) == 18
    assert all(list(row) == list(corpus.dataset_rows[0]) for row in corpus.dataset_rows)


def test_in_memory_generation_is_seed_deterministic():
    first = USHIPAA18TabularCorpusGenerator(seed=42).generate(n_subjects=18)
    second = USHIPAA18TabularCorpusGenerator(seed=42).generate(n_subjects=18)
    assert second == first
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `pytest -q tests/test_hipaa_18_tabular.py -k 'baseline or deterministic'`

Expected: collection failure because `generators.hipaa_18_tabular` does not exist.

- [ ] **Step 3: Implement the immutable field specification and corpus dataclasses**

Define an ordered tuple of 18 `IdentifierSpec` instances with fields:

```python
@dataclass(frozen=True)
class IdentifierSpec:
    hipaa_category: str
    source_column: str
    canonical_variable: str
    variable_label: str
    data_type: str
    value_format: str
    hipaa_identifier: str
    entity_type: str
    expected_action: str
    rule_id: str
```

Define `HIPAA18Corpus` with `dataset_rows`, `dictionary_rows`, `expected_user_rows`, `audit_events`, and `gold_entries`. Generate one populated value for every specification for every subject. Category C values must yield ages 18–89 in the baseline.

- [ ] **Step 4: Implement expected user, audit, and gold rows**

For each source cell, emit:

```python
{
    "form": "hipaa18",
    "row_index": row_index,
    "column": spec.source_column,
    "hipaa_category": spec.hipaa_category,
    "original_value": value,
    "expected_action": spec.expected_action,
    "expected_value": expected_value,
}
```

Audit events use a test-only HMAC key and must not contain `original_value`. User rows blank A, B, and D–R and retain only the four-digit year for C.

- [ ] **Step 5: Export the generator and verify GREEN**

Run: `pytest -q tests/test_hipaa_18_tabular.py -k 'baseline or deterministic'`

Expected: all selected tests pass.

- [ ] **Step 6: Commit the slice**

```bash
git add generators/hipaa_18_tabular.py generators/__init__.py tests/test_hipaa_18_tabular.py
git commit -m "feat: add HIPAA A-R tabular corpus model"
```

---

### Task 2: Semantic validator and exact gold-output contracts

**Files:**
- Modify: `generators/hipaa_18_tabular.py`
- Modify: `tests/test_hipaa_18_tabular.py`

**Interfaces:**
- Produces: `validate_corpus(corpus: HIPAA18Corpus) -> list[HIPAA18ValidationIssue]`.
- Consumes: the canonical specification from Task 1.

- [ ] **Step 1: Write failing validation and no-plaintext-audit tests**

```python
def test_baseline_validates_and_cell_evidence_is_complete():
    corpus = USHIPAA18TabularCorpusGenerator(seed=42).generate(n_subjects=18)
    assert validate_corpus(corpus) == []
    assert len(corpus.gold_entries) == 18 * 18
    assert len(corpus.audit_events) == 18 * 18


def test_audit_output_never_contains_plaintext_identifiers():
    corpus = USHIPAA18TabularCorpusGenerator(seed=42).generate(n_subjects=2)
    audit_text = json.dumps(corpus.audit_events, sort_keys=True)
    for row in corpus.dataset_rows:
        for value in row.values():
            assert value not in audit_text
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `pytest -q tests/test_hipaa_18_tabular.py -k 'validates or plaintext'`

Expected: failure because `validate_corpus` is not implemented.

- [ ] **Step 3: Implement validation codes**

Validator codes:

```python
UNMAPPED_DATASET_COLUMN
ORPHAN_DICTIONARY_VARIABLE
DUPLICATE_MAPPING
MISSING_HIPAA_CATEGORY
DUPLICATE_HIPAA_CATEGORY
CONFLICTING_HIPAA_CATEGORY
EMPTY_REQUIRED_VALUE
INVALID_EXPECTED_OUTPUT
INVALID_LEDGER_REFERENCE
PLAINTEXT_AUDIT_VALUE
```

Return issues in stable `(code, column, detail)` order. Validate header equality, exact A–R category coverage, nonempty baseline cells, expected actions, cell counts, cell references, and audit plaintext exclusion.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `pytest -q tests/test_hipaa_18_tabular.py -k 'validates or plaintext'`

Expected: all selected tests pass.

- [ ] **Step 5: Commit the slice**

```bash
git add generators/hipaa_18_tabular.py tests/test_hipaa_18_tabular.py
git commit -m "feat: validate HIPAA tabular corpus contracts"
```

---

### Task 3: Deterministic CSV, XLSX, JSONL, coverage, and manifest output

**Files:**
- Modify: `generators/hipaa_18_tabular.py`
- Modify: `tests/test_hipaa_18_tabular.py`

**Interfaces:**
- Produces: `USHIPAA18TabularCorpusGenerator.write(out_dir, n_subjects, include_edge_cases)`.
- Produces package files under `baseline/` exactly as specified in the design.

- [ ] **Step 1: Write failing package-rendering tests**

```python
def test_write_emits_equivalent_csv_and_xlsx_with_valid_manifest(tmp_path):
    report = USHIPAA18TabularCorpusGenerator(seed=42).write(
        tmp_path, n_subjects=18, include_edge_cases=False
    )
    assert report["validation_status"] == "PASS"
    assert read_csv(tmp_path / "baseline/input/datasets/hipaa18.csv") == read_xlsx(
        tmp_path / "baseline/input/datasets/hipaa18.xlsx"
    )
    assert read_csv(tmp_path / "baseline/input/data_dictionary/hipaa18_dictionary.csv") == read_xlsx(
        tmp_path / "baseline/input/data_dictionary/hipaa18_dictionary.xlsx"
    )
    assert_manifest_hashes(tmp_path / "baseline/MANIFEST.json")


def test_write_is_byte_deterministic(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    generator = USHIPAA18TabularCorpusGenerator(seed=42)
    generator.write(first, n_subjects=18, include_edge_cases=False)
    generator.write(second, n_subjects=18, include_edge_cases=False)
    assert tree_hashes(first) == tree_hashes(second)
```

- [ ] **Step 2: Run the rendering tests and verify RED**

Run: `pytest -q tests/test_hipaa_18_tabular.py -k 'write_emits or byte_deterministic'`

Expected: failure because `write` does not exist.

- [ ] **Step 3: Implement deterministic writers**

Use `csv.DictWriter(..., lineterminator="\n")`, sorted-key JSON, and one JSON object per JSONL line. For XLSX, fix workbook created/modified properties, save to memory, then repack ZIP members in sorted order with timestamp `(1980, 1, 1, 0, 0, 0)`.

- [ ] **Step 4: Implement coverage and manifest reports**

`coverage_report.json` contains `jurisdiction`, `authority`, ordered category details, missing categories, duplicate categories, record count, and `validation_status`. `MANIFEST.json` records SHA-256 and byte size for every baseline file except itself.

- [ ] **Step 5: Run rendering tests and verify GREEN**

Run: `pytest -q tests/test_hipaa_18_tabular.py -k 'write_emits or byte_deterministic'`

Expected: all selected tests pass.

- [ ] **Step 6: Commit the slice**

```bash
git add generators/hipaa_18_tabular.py tests/test_hipaa_18_tabular.py
git commit -m "feat: render deterministic HIPAA corpus package"
```

---

### Task 4: Initial dictionary-mapping edge cases

**Files:**
- Modify: `generators/hipaa_18_tabular.py`
- Modify: `tests/test_hipaa_18_tabular.py`

**Interfaces:**
- Produces: `generate_edge_cases()` and five package directories under `edge_cases/`.
- Consumes: `validate_corpus` from Task 2 and CSV writers from Task 3.

- [ ] **Step 1: Write failing edge-case tests**

```python
@pytest.mark.parametrize(
    ("case_name", "expected_codes"),
    [
        ("missing_dictionary_entry", {"UNMAPPED_DATASET_COLUMN", "MISSING_HIPAA_CATEGORY"}),
        ("orphan_dictionary_entry", {"ORPHAN_DICTIONARY_VARIABLE"}),
        ("duplicate_mapping", {"DUPLICATE_MAPPING", "DUPLICATE_HIPAA_CATEGORY"}),
        ("conflicting_category", {"CONFLICTING_HIPAA_CATEGORY", "MISSING_HIPAA_CATEGORY", "DUPLICATE_HIPAA_CATEGORY"}),
        ("explicit_alias_mapping", set()),
    ],
)
def test_edge_case_declares_observed_validation(case_name, expected_codes):
    case = generate_edge_cases(seed=42)[case_name]
    assert {issue.code for issue in validate_corpus(case)} == expected_codes
```

- [ ] **Step 2: Run the edge tests and verify RED**

Run: `pytest -q tests/test_hipaa_18_tabular.py -k edge_case`

Expected: failure because edge-case generation does not exist.

- [ ] **Step 3: Implement one explicit mutation per case**

Copy the one-subject baseline model, apply only the documented mutation, rebuild evidence when the valid alias changes a source column, and write `expected_validation.json` containing the exact stable issue list.

- [ ] **Step 4: Run edge tests and verify GREEN**

Run: `pytest -q tests/test_hipaa_18_tabular.py -k edge_case`

Expected: all selected tests pass.

- [ ] **Step 5: Commit the slice**

```bash
git add generators/hipaa_18_tabular.py tests/test_hipaa_18_tabular.py
git commit -m "feat: add HIPAA mapping edge-case fixtures"
```

---

### Task 5: CLI and end-to-end smoke verification

**Files:**
- Create: `harness/generate_hipaa18_tabular.py`
- Modify: `tests/test_hipaa_18_tabular.py`

**Interfaces:**
- Produces: `main(argv: list[str] | None = None) -> int`.
- CLI: `python -m harness.generate_hipaa18_tabular --seed 42 --n-subjects 18 --out-dir PATH`.

- [ ] **Step 1: Write a failing CLI test**

```python
def test_cli_writes_passing_baseline_and_edges(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "harness.generate_hipaa18_tabular",
            "--seed", "42",
            "--n-subjects", "18",
            "--out-dir", str(tmp_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "HIPAA categories: 18/18" in result.stdout
    assert json.loads((tmp_path / "baseline/coverage_report.json").read_text())["validation_status"] == "PASS"
```

- [ ] **Step 2: Run the CLI test and verify RED**

Run: `pytest -q tests/test_hipaa_18_tabular.py -k cli`

Expected: nonzero subprocess exit because the harness module does not exist.

- [ ] **Step 3: Implement the thin CLI**

Parse `--seed`, `--n-subjects`, `--out-dir`, and `--no-edge-cases`. Call the generator, print record count, `18/18` category coverage, baseline validation, and edge-case count. Return 1 if baseline validation is not `PASS`.

- [ ] **Step 4: Run the complete focused suite**

Run: `pytest -q tests/test_hipaa_18_tabular.py`

Expected: all tests pass.

- [ ] **Step 5: Run the real smoke scenario**

Run:

```bash
python -m harness.generate_hipaa18_tabular \
  --seed 42 \
  --n-subjects 18 \
  --out-dir tmp/hipaa18-corpus-smoke
```

Expected output includes:

```text
HIPAA categories: 18/18
Subjects: 18
Baseline validation: PASS
Edge cases: 5
```

- [ ] **Step 6: Run regression tests for reused generators**

Run: `pytest -q tests/test_study_tabular.py tests/test_hipaa_safe_harbor.py`

Expected: all tests pass.

- [ ] **Step 7: Commit the slice**

```bash
git add harness/generate_hipaa18_tabular.py tests/test_hipaa_18_tabular.py
git commit -m "feat: add HIPAA 18-category corpus CLI"
```
