# Validation Protocol

**Version:** 2026-07-06.task7
**Status:** Structural validation protocol for the evidence-first PHI corpus and release artifacts.

Validation is structural evidence that generated corpus files match their manifest, schema, offsets, citations, jurisdiction folders, expected formats, and static no-real-PHI sentinels. It is not regulatory certification, HIPAA/GDPR/DPDPA compliance proof, clinician review, counsel review, or external validation.

## Required command

Run the full validation suite with:

```bash
python -m harness.run_all_validations --corpus-dir corpus --manifest corpus/MANIFEST.json --output validation_report.json
```

The command exits `0` when every validator passes and exits `1` when any validator reports issues. The output JSON contains `validation_status`, `corpus_dir`, `manifest`, and per-validator results.

## Validator list

`harness.run_all_validations` runs these validators in order:

1. `offset_validator` -- checks that each `gold_spans[]` offset resolves to the recorded span value.
2. `hash_validator` -- recomputes SHA-256 for manifest-listed files and detects missing manifest files.
3. `taxonomy_validator` -- checks required record and span schema fields plus detection-regime taxonomy.
4. `citation_validator` -- requires authority citations at record or span level.
5. `jurisdiction_separator` -- checks that records live under matching jurisdiction folders, with the documented `file_formats` exception.
6. `format_parse_validator` -- checks expected structural parse rules for JSON-derived, HL7v2, EML, and text formats.
7. `no_real_phi_static_validator` -- scans for banned real-looking fixture sentinels while allowing documented synthetic-safe values such as `example.com`.

## Issue codes

| Issue code | Emitted by | Meaning |
|---|---|---|
| `OFFSET_MISMATCH` | `offset_validator` | A span object, offset field, or text slice does not match the recorded span value. |
| `HASH_MISMATCH` | `hash_validator` | A manifest-listed file's SHA-256 digest does not match the file bytes. |
| `MISSING_MANIFEST_FILE` | `hash_validator` | The manifest is missing, malformed for file validation, or references a file that is absent. |
| `BAD_SCHEMA` | `taxonomy_validator` | A record or span is missing required fields or has invalid field types. |
| `BAD_DETECTION_REGIME` | `taxonomy_validator` | A span's `detection_regime` is outside `rule_applicable`, `contextual_ner_required`, or `conflict_case`. |
| `MISSING_AUTHORITY` | `citation_validator` | A record or span lacks required authority-citation support. |
| `JURISDICTION_MISMATCH` | `jurisdiction_separator` | A record jurisdiction is missing, incompatible with its folder, or not allowed in `file_formats`. |
| `FORMAT_PARSE_FAIL` | `format_parse_validator` | A record's format-specific text structure failed the expected parse check. |
| `REAL_PHI_SENTINEL` | `no_real_phi_static_validator` | A banned real-PHI sentinel category was detected without echoing the matched raw value. |

## Interpretation

- `PASS` means the configured structural validators found no issues in the supplied corpus/manifest pair.
- `FAIL` means at least one issue must be fixed or accepted as a documented blocker before release evidence is produced.
- Validation reports must be hashed by release evidence before public claim-level statements are made.
- Structural validation does not replace clinician review, counsel review, production security review, or external benchmark validation. Those statuses remain `PENDING` until separate artifacts prove completion.
