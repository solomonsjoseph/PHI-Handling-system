# USA HIPAA 18-Identifier Tabular Corpus Design

**Date:** 2026-07-21

## Goal

Create a deterministic synthetic tabular corpus for the USA jurisdiction in which all 18 HIPAA Safe Harbor identifier categories, 45 CFR 164.514(b)(2)(i)(A) through (R), are mandatory baseline coverage. The first milestone uses dataset and dictionary/mapping files only. PDF form processing is explicitly deferred.

## Regulatory boundary

The corpus is a test oracle, not a compliance determination. It models the identifiers enumerated by HIPAA Safe Harbor and expected conservative transformations. Release claims still require the safeguards in 45 CFR 164.514(b)(2)(ii), including no actual knowledge that remaining information could identify an individual.

Primary sources:

- 45 CFR 164.514: https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-C/part-164/subpart-E/section-164.514
- HHS de-identification guidance: https://www.hhs.gov/hipaa/for-professionals/special-topics/de-identification/index.html

## Scope

### Included

- USA jurisdiction only.
- Seeded synthetic data only; no real person or production record.
- One representative tabular field for every HIPAA category A–R.
- Equivalent CSV and XLSX renderings.
- A flat dictionary/mapping with one canonical row per baseline identifier field.
- Expected user-safe output.
- Expected value-free audit output recording what, why, and how.
- A protected plaintext gold ledger for exact corpus scoring.
- A hash manifest and coverage report.
- Initial structural edge cases generated only after the canonical baseline validates.

### Deferred

- PDF forms and PDF-to-dataset binding.
- Non-USA jurisdictions.
- Expert Determination and SANT profiles.
- Image-content or biometric-content analysis. Category P and Q use realistic synthetic attachment/template references in the tabular field; this milestone tests dictionary-driven handling of those fields, not computer vision.
- Narrative free-text detection, already covered by the existing narrative HIPAA generator.

## Baseline identifier contract

The baseline dataset has exactly these 18 PHI columns. Every generated subject row populates every column.

| HIPAA | Column | Representative value | Entity type | Expected Safe Harbor action |
|---|---|---|---|---|
| A | `FULL_NAME` | Synthetic full name | `NAME_PATIENT` | `drop` |
| B | `STREET_ADDRESS` | Street, city, state, full ZIP | `ADDRESS_FULL` | `drop` |
| C | `DATE_OF_BIRTH` | Full ISO date | `DATE_DOB` | `retain_year` for baseline ages 18–89 |
| D | `PHONE_NUMBER` | US telephone number | `PHONE` | `drop` |
| E | `FAX_NUMBER` | US fax number | `FAX` | `drop` |
| F | `EMAIL_ADDRESS` | Synthetic email address | `EMAIL` | `drop` |
| G | `SOCIAL_SECURITY_NUMBER` | Synthetic SSN-shaped value | `SSN` | `drop` |
| H | `MEDICAL_RECORD_NUMBER` | Synthetic MRN | `MRN` | `drop` |
| I | `HEALTH_PLAN_BENEFICIARY_NUMBER` | Synthetic MBI-shaped value | `HEALTH_PLAN_ID` | `drop` |
| J | `ACCOUNT_NUMBER` | Synthetic account number | `ACCOUNT_NUMBER` | `drop` |
| K | `CERTIFICATE_LICENSE_NUMBER` | Synthetic license number | `LICENSE_NUMBER` | `drop` |
| L | `VEHICLE_IDENTIFIER` | Synthetic VIN | `VIN` | `drop` |
| M | `DEVICE_IDENTIFIER` | Synthetic UDI | `DEVICE_UDI` | `drop` |
| N | `WEB_URL` | Synthetic patient portal URL | `URL` | `drop` |
| O | `IP_ADDRESS` | Synthetic IPv4 address | `IP_V4` | `drop` |
| P | `BIOMETRIC_IDENTIFIER` | Synthetic biometric-template reference | `BIOMETRIC` | `drop` |
| Q | `FULL_FACE_PHOTO` | Synthetic full-face-photo attachment reference | `PHOTO_FULL_FACE` | `drop` |
| R | `OTHER_UNIQUE_IDENTIFIER` | Synthetic internal participant code | `OTHER_UNIQUE_ID` | `drop` |

A generated `ROW_TOKEN` may appear only in expected output and audit artifacts. It is not copied or derived from an input identifier and exists solely to align synthetic expected results.

## Dictionary and mapping contract

The CSV and XLSX dictionaries are semantically equivalent flat tables with these columns:

- `dataset_name`
- `source_column`
- `canonical_variable`
- `variable_label`
- `data_type`
- `format`
- `required`
- `hipaa_category`
- `hipaa_identifier`
- `entity_type`
- `expected_action`
- `rule_id`
- `authority`

Baseline invariants:

1. The dataset header and dictionary `source_column` set are identical.
2. `hipaa_category` is exactly one uppercase letter A–R.
3. Every A–R category appears exactly once.
4. Every source column has exactly one dictionary binding.
5. Every binding has a nonempty entity type, rule ID, action, and category-specific authority citation.
6. Category C alone uses `retain_year`; the other baseline categories use `drop`.
7. CSV and XLSX renderings preserve identical strings and column ordering.

## Generated package

```text
<out-dir>/
├── baseline/
│   ├── input/
│   │   ├── datasets/
│   │   │   ├── hipaa18.csv
│   │   │   └── hipaa18.xlsx
│   │   └── data_dictionary/
│   │       ├── hipaa18_dictionary.csv
│   │       └── hipaa18_dictionary.xlsx
│   ├── expected/
│   │   ├── user_output.csv
│   │   └── audit_output.jsonl
│   ├── gold/
│   │   └── cell_actions.jsonl
│   ├── coverage_report.json
│   └── MANIFEST.json
└── edge_cases/
    ├── missing_dictionary_entry/
    ├── orphan_dictionary_entry/
    ├── duplicate_mapping/
    ├── conflicting_category/
    └── explicit_alias_mapping/
```

## Expected outputs

### User output

The expected CSV has `ROW_TOKEN` plus the same 18 ordered columns. Categories A, B, and D–R are blank. `DATE_OF_BIRTH` contains only the four-digit year. It contains no original direct identifier.

### Audit output

There is one JSONL event for each populated identifier cell. Each event contains:

- `event_id`
- `row_token`
- `dataset_name`
- `row_index`
- `source_column`
- `canonical_variable`
- `hipaa_category`
- `entity_type`
- `what.action`
- `what.outcome`
- `why.rule_id`
- `why.authority`
- `why.reason`
- `how.method`
- `evidence.input_hmac_sha256`
- `evidence.output_hmac_sha256`

The audit output never stores `original_value`. Corpus fixtures use an explicitly test-only HMAC key so expected evidence remains deterministic; that key is never a production default. A production caller must supply its own secret audit key. The protected gold ledger stores the synthetic original value and expected output for exact scoring.

## Determinism

A fixed seed and subject count must produce byte-identical CSV, JSON, JSONL, and XLSX files. XLSX output therefore uses fixed workbook properties and normalized ZIP-member timestamps/order. The manifest excludes its own hash and records SHA-256 for every other package file.

## Baseline validation

Generation fails before writing edge cases unless all of these checks pass:

1. Categories are exactly `A` through `R`.
2. Each category occurs exactly once in the dictionary.
3. Dataset and mapping source-column sets match.
4. All rows use the ordered 18-column schema.
5. No required baseline identifier cell is empty.
6. Gold ledger count equals `subjects × 18`.
7. Audit event count equals `subjects × 18`.
8. Each gold entry resolves to its exact dataset cell.
9. Each audit event resolves to its dictionary row without containing the plaintext input value.
10. Expected output follows the dictionary action.
11. CSV and XLSX dataset values match.
12. CSV and XLSX dictionary rows match.
13. Repeating the run with the same seed produces identical bytes.

## Initial edge cases

Each edge case is a complete small package with `expected_validation.json`. Invalid cases are successful corpus fixtures only when validation returns the declared error code.

| Case | Mutation | Expected result |
|---|---|---|
| `missing_dictionary_entry` | Remove category R dictionary row while retaining its dataset column | `UNMAPPED_DATASET_COLUMN` |
| `orphan_dictionary_entry` | Add a dictionary row for a column absent from the dataset | `ORPHAN_DICTIONARY_VARIABLE` |
| `duplicate_mapping` | Duplicate the category G mapping | `DUPLICATE_MAPPING` |
| `conflicting_category` | Declare the SSN field as category H rather than G | `CONFLICTING_HIPAA_CATEGORY` |
| `explicit_alias_mapping` | Rename the email source header while retaining canonical variable `EMAIL_ADDRESS` | valid; explicit alias resolves |

## Interfaces

A new `USHIPAA18TabularCorpusGenerator` owns the canonical model and renders the package. Public methods:

```python
class USHIPAA18TabularCorpusGenerator:
    def __init__(self, seed: int = 42) -> None: ...
    def generate(self, n_subjects: int = 18) -> HIPAA18Corpus: ...
    def write(self, out_dir: Path, n_subjects: int = 18, include_edge_cases: bool = True) -> dict: ...
```

The CLI is:

```bash
python -m harness.generate_hipaa18_tabular \
  --seed 42 \
  --n-subjects 18 \
  --out-dir <directory>
```

## Acceptance criteria

- The command exits 0 and emits the documented package.
- `coverage_report.json` reports all 18 categories with no missing or duplicate category.
- Baseline CSV/XLSX pairs are semantically equivalent.
- Every baseline subject has all 18 identifier categories.
- Expected user output removes all direct identifiers and retains only DOB year.
- Expected audit output provides what/why/how evidence without raw identifier values.
- The protected ledger provides exact cell-level ground truth.
- All five initial edge cases are emitted and produce their declared validation result.
- The focused test suite and a real CLI smoke run pass.
