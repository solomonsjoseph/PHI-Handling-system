# PHI Handling - Evidence-First Corpus, Benchmark, and Runtime Safety Harness

**Repository status:** v2.0.0-dev evidence-alignment in progress  
**Claim level:** Current public claim level: L1 strong, L2 partial, L3 partial, L4/L5 not yet supported  
**License:** MIT (see `LICENSE`)  
**Maintainer:** Private maintainer contact must be configured by the project owner before public security or PHI-leak reports are accepted.

This repository contains a synthetic PHI corpus, benchmark, and runtime safety harness whose public claims are bounded by machine-checkable evidence in `harness/capability_registry.json`, `corpus/MANIFEST.json`, validation reports, tests, benchmark artifacts, MIA smoke reports, and release evidence. If a capability is not listed in the capability registry at the required status, it is not claimed here as release coverage.

This repository is IRB-oriented: it provides IRB-review support artifacts for corpus provenance, validation, benchmark protocol, security controls, and review checklists. It is not a standalone review submission by itself.

This repository does not certify HIPAA, GDPR, DPDPA, or other compliance.

## For reviewers -- current evidence status

1. Start with `harness/capability_registry.json` to see which jurisdictions, formats, benchmarks, controls, and review steps are manifested, tested, implemented, or planned.
2. Use `corpus/MANIFEST.json` for the current canonical corpus release evidence.
3. Use `validation_report.json`, `mia_report.json`, `release_evidence.json`, and `benchmarks/results/*` when those artifacts are generated for a release candidate.
4. Treat planned registry entries as roadmap items, not implemented coverage.

A reviewer should be able to distinguish manifested evidence from tested generators, implemented controls, and planned work without relying on maintainer assertions.

## Claim level

| Level | Current status | Evidence boundary |
|---|---|---|
| L1 | Strong | Registry-backed project scope, no-real-PHI statement, canonical US/HIPAA JSONL corpus entries, validation commands, and security disclosure policy. |
| L2 | Partial | Tested non-US and file-format generators exist, and validator evidence exists, but only registry entries at `manifested` status are described as release coverage. |
| L3 | Partial | Benchmark code, deterministic MIA smoke testing, PHI/LLM boundary guards, and threat-model documentation are implemented, but benchmark result artifacts and external reviews are not claimed as complete release evidence here. |
| L4 | Not yet supported | Clinician review, counsel review, commercial benchmark validation, and strict benchmark artifact review are not complete. |
| L5 | Not yet supported | No external certification, regulatory approval, or independent audit is claimed. |

Supporting implemented controls that are not manifested coverage claims:

| ID | Kind | Status | Claim | Limitations |
|---|---|---|---|---|
| `benchmark_presidio_stock` | benchmark | implemented | Stock Presidio benchmark path exists but requires clean stock/tuned adapter split before manifested comparison | requires clean stock/tuned adapter split before manifested comparison |
| `benchmark_presidio_tuned` | benchmark | implemented | Tuned Presidio benchmark path exists and must be labelled separately from stock Presidio | custom MBI/VIN recognizers must be labelled tuned |
| `mia_framework` | privacy_attack | implemented | Deterministic MIA smoke test for release evidence is implemented |  |
| `no_phi_to_llm_boundary` | security_control | implemented | PHI-to-LLM boundary guards exist but are not yet universally wrapped | existing guards present but not universally wrapped |
| `threat_model` | security_control | implemented | Threat model documentation and release-gate mapping are implemented |  |

## Manifested coverage

Only these registry entries are claimed as manifested release coverage.

| ID | Kind | Status | Jurisdiction | Claim | Output | Limitations |
|---|---|---|---|---|---|---|
| `format_jsonl` | file_format | manifested |  | JSONL is the canonical manifested corpus file format | `corpus/**/*.jsonl` |  |
| `us_hipaa` | jurisdiction | manifested | us | US/HIPAA synthetic corpus with span-level gold annotations | `corpus/us/*.jsonl` |  |

## Tested but not yet release-manifested coverage

These entries have tests or validator support, but they are not claimed as manifested release coverage in this README unless and until the registry and release manifest promote them.

| ID | Kind | Status | Jurisdiction | Claim | Output | Limitations |
|---|---|---|---|---|---|---|
| `format_dicom_header` | file_format | tested |  | DICOM header generator is tested but not yet included in the canonical release manifest | `corpus/file_formats/dicom_headers.jsonl` |  |
| `format_eml` | file_format | tested |  | EML generator is tested but not yet included in the canonical release manifest | `corpus/file_formats/eml_messages.jsonl` |  |
| `format_fhir_r4` | file_format | tested |  | FHIR R4 generator is tested but not yet included in the canonical release manifest | `corpus/file_formats/fhir_bundles.jsonl` |  |
| `format_hl7v2` | file_format | tested |  | HL7v2 generator is tested but not yet included in the canonical release manifest | `corpus/file_formats/hl7v2_messages.jsonl` |  |
| `format_xlsx` | file_format | tested |  | XLSX generator is tested but not yet included in the canonical release manifest | `corpus/file_formats/xlsx_phi_corpus.jsonl` |  |
| `australia_privacy` | jurisdiction | tested | au | Australia Privacy Act synthetic identifier generator exists with tests but is not yet in the canonical manifest | `corpus/au/australia_identifiers.jsonl` |  |
| `brazil_lgpd` | jurisdiction | tested | br | Brazil/LGPD synthetic identifier generator exists with tests but is not yet in the canonical manifest | `corpus/br/brazil_identifiers.jsonl` |  |
| `eu_gdpr` | jurisdiction | tested | eu | EU/GDPR synthetic identifier generator exists with tests but is not yet in the canonical manifest | `corpus/eu/eu_identifiers.jsonl` |  |
| `india_dpdpa` | jurisdiction | tested | in | India DPDPA synthetic generator exists with tests but is not yet in the canonical manifest | `corpus/in/india_dpdpa.jsonl` |  |
| `india_identifiers` | jurisdiction | tested | in | India identifier synthetic generator exists with tests but is not yet in the canonical manifest | `corpus/in/india_identifiers.jsonl` |  |
| `uganda_dppa` | jurisdiction | tested | ug | Uganda DPPA synthetic identifier generator exists with tests but is not yet in the canonical manifest | `corpus/ug/uganda_identifiers.jsonl` |  |
| `validator_suite` | validator | tested |  | Standalone validation suite is implemented and covered by corpus validator tests |  |  |

## Planned coverage not claimed as implemented

These entries remain planned. They must not be described as implemented, tested, manifested, externally reviewed, or certified until the registry and release evidence support that status.

| ID | Kind | Status | Jurisdiction | Claim | Output | Limitations |
|---|---|---|---|---|---|---|
| `benchmark_aws_comprehend` | benchmark | planned |  | AWS Comprehend Medical benchmark is planned and credential-gated |  | requires AWS credentials |
| `benchmark_azure_health` | benchmark | planned |  | Azure Health benchmark is planned and credential-gated |  | requires Azure credentials |
| `benchmark_john_snow_labs` | benchmark | planned |  | John Snow Labs Healthcare NLP benchmark is planned and license-gated |  | requires John Snow Labs license |
| `benchmark_modified_deidentify` | benchmark | planned |  | Modified Deidentify benchmark is planned and license-gated |  | requires underlying model license confirmation |
| `format_csv` | file_format | planned |  | CSV corpus export is planned and not generated in the current canonical corpus |  | not generated in current canonical corpus |
| `format_docx` | file_format | planned |  | DOCX corpus export is planned and not generated in the current canonical corpus |  | not generated in current canonical corpus |
| `format_exif` | file_format | planned |  | EXIF corpus export is planned and not generated in the current canonical corpus |  | not generated in current canonical corpus |
| `format_hl7_cda` | file_format | planned |  | HL7 CDA corpus export is planned and not generated in the current canonical corpus |  | not generated in current canonical corpus |
| `format_json` | file_format | planned |  | JSON corpus export is planned and not generated in the current canonical corpus |  | not generated in current canonical corpus |
| `format_parquet` | file_format | planned |  | Parquet corpus export is planned and not generated in the current canonical corpus |  | not generated in current canonical corpus |
| `format_pdf` | file_format | planned |  | PDF corpus export is planned and not generated in the current canonical corpus |  | not generated in current canonical corpus |
| `canada_pipeda` | jurisdiction | planned | ca | Canada/PIPEDA corpus coverage is planned and not implemented in the canonical generator path |  | not implemented in canonical generator path |
| `china_pipl` | jurisdiction | planned | cn | China PIPL corpus coverage is planned and not implemented in the canonical generator path |  | not implemented in canonical generator path |
| `japan_appi` | jurisdiction | planned | jp | Japan APPI corpus coverage is planned and not implemented in the canonical generator path |  | not implemented in canonical generator path |
| `singapore_pdpa` | jurisdiction | planned | sg | Singapore PDPA corpus coverage is planned and not implemented in the canonical generator path |  | not implemented in canonical generator path |
| `uk_gdpr` | jurisdiction | planned | uk | UK GDPR corpus coverage is planned and not implemented in the canonical generator path |  | not implemented in canonical generator path |
| `clinician_review` | review_control | planned |  | Clinician review is planned and not completed |  |  |
| `counsel_review` | review_control | planned |  | Counsel review is planned and not completed |  |  |

## Evidence commands

```bash
python -m harness.generate_corpus --seed 42 --jurisdiction all --out-dir corpus
python -m harness.run_all_validations --corpus-dir corpus --manifest corpus/MANIFEST.json --output validation_report.json
python -m benchmarks.presidio_adapter --corpus-dir corpus/us --output-dir benchmarks/results/presidio-stock --profile stock --scoring-profile strict_all_span --verbose
python -m harness.mia_framework --corpus-dir corpus --output mia_report.json
python -m harness.release_evidence --corpus-dir corpus --manifest corpus/MANIFEST.json --validation-report validation_report.json --mia-report mia_report.json --output release_evidence.json
```

## Scope and non-claims

### What it does

- Provides a registry-backed synthetic PHI corpus and safety harness.
- Provides current manifested release coverage for US/HIPAA JSONL corpus artifacts only.
- Provides tested, non-manifested generators for India, EU/GDPR, Brazil, Australia, Uganda, DICOM headers, FHIR R4, HL7v2, EML, and XLSX.
- Provides runtime safety controls intended to prevent PHI from crossing LLM tool boundaries without explicit gates.
- Provides IRB-review support artifacts and review checklists for future human review.

### What it does not do

- It does not contain any real PHI.
- It does not contain any actual patient images; synthetic-only image-related coverage is tracked separately.
- It is not itself a de-identification tool; it is a corpus, benchmark, and runtime safety harness for evaluating such tools.
- This repository does not certify HIPAA, GDPR, DPDPA, or other compliance.
- It does not claim to enumerate every possible PHI instance. Safe Harbor (b)(2)(ii) "no actual knowledge" remains a human judgment.
- It does not substitute for counsel review. Counsel review is tracked as planned in the capability registry.
- It does not substitute for clinician review. Three independent clinician reviewers are required for clinical plausibility review. Status: PENDING.
- It does not claim synthetic data is IRB-exempt by definition. Legal basis for synthetic data use in research must be evaluated per jurisdiction.
- It does not claim Canada, UK, Singapore, Japan, China, JSON, CSV, DOCX, PDF, HL7 CDA, Parquet, EXIF, commercial benchmarks, clinician review, counsel review, or external review as implemented coverage.

## Repository structure

Key paths for the current evidence-alignment work:

```text
PHI-Handling-system/
|-- README.md
|-- pyproject.toml
|-- .phi-build-status
|
|-- harness/
|   |-- capability_registry.py       # Registry loader, status checks, Markdown CLI
|   |-- capability_registry.json     # Machine-checkable claim/status source of truth
|   |-- generate_corpus.py           # Seeded corpus generation
|   |-- run_all_validations.py       # Validation runner
|   |-- mia_framework.py             # Deterministic MIA smoke test
|   `-- release_evidence.py          # Release evidence hashing and claim-level summary
|
|-- corpus/
|   |-- MANIFEST.json                # Current canonical manifest when generated
|   `-- us/                          # Manifested US/HIPAA JSONL corpus outputs
|
|-- generators/                      # Implemented and tested generator code
|   |-- hipaa_*.py                   # US/HIPAA generator modules
|   |-- in/ eu/ br/ au/ ug/          # Tested non-US generator modules
|   `-- file_formats/                # Tested DICOM/FHIR/HL7v2/EML/XLSX modules
|
|-- validators/                      # Structural corpus validators
|-- benchmarks/                      # Benchmark code and result artifacts
|-- authorities/                     # Primary legal/research source mapping
|-- docs/                            # Validation, reproducibility, threat model, and review documents
|-- phi_engine/                      # Runtime PHI and LLM safety controls
`-- tests/
```

## Quick start

```bash
python -m pip install -r requirements.txt
python -m pytest tests/test_capability_registry.py -q
python -m harness.capability_registry
```

For release-candidate evidence generation, use the commands in [Evidence commands](#evidence-commands).

## What makes this evidence-first

Five properties, each tied to registry-backed evidence:

1. **Registry-backed claim boundaries** -- `harness/capability_registry.json` records whether each jurisdiction, format, benchmark, validator, security control, review control, and privacy attack capability is planned, implemented, tested, manifested, or externally reviewed.
2. **Provenance** -- Generated records carry authority citation fields, and public claims should map back to primary sources or registry entries rather than summaries of summaries.
3. **Reproducibility** -- The canonical corpus is generated from seeded generators. `corpus/MANIFEST.json` records release hash and span-count evidence when the corpus is generated.
4. **Benchmark-readiness** -- Baseline benchmark code exists for Presidio, while commercial-tool artifacts remain registry-tracked work before they can support release claims.
5. **Reviewer-friendly structure** -- The README points reviewers at machine-checkable registry and manifest evidence first. The goal is that a reviewer can distinguish implemented evidence from planned work without relying on trust.

## Known limitations

Full detail is in `docs/KNOWN_LIMITATIONS.md`. The most important limitations are:

1. **Clinical plausibility review pending.** Three independent clinician reviewers are required before clinical plausibility can be treated as reviewed evidence.
2. **Counsel review pending.** Jurisdiction-specific legal basis, synthetic-data posture, and external LLM egress approval require counsel review before higher claim levels.
3. **Commercial benchmarks pending.** AWS Comprehend Medical, Azure Health, John Snow Labs, and Modified Deidentify remain planned or credential/license-gated.
4. **Release-manifest boundaries matter.** Tested generators are not described as manifested coverage until registry status and manifest evidence support that claim.
5. **No compliance certification.** This repository provides evidence artifacts, not regulatory certification.

Additional limitations covering language coverage, image PHI, state-specific formats, private DICOM tags, longitudinal linkability, and baseline benchmark configurations are documented in `docs/KNOWN_LIMITATIONS.md`.

## Citation

If you use this corpus in research, please cite the repository and release evidence artifact used for your run:

```text
[Author]. (2026). PHI Handling: Evidence-first synthetic PHI corpus,
benchmark, and runtime safety harness [Software].
Release evidence: release_evidence.json for the cited run.
```

## Relationship to RePORTaLiN

This repository is **separate from and not dependent on** the RePORTaLiN-RAG project. It was originally prompted by the need to validate RePORTaLiN-RAG's PHI handling, but the corpus and harness in this repository are general-purpose and can be used with any PHI detection system.

## Security disclosure

Do not file suspected PHI or security leakage as a public GitHub issue. Use a private maintainer contact configured by the project owner; if none is configured, stop distribution and notify the repository owner out of band.

## License

MIT License. See `LICENSE` for full text.

Notable exceptions: The authority matrix references statutory text which is in the public domain. Research citations in `authorities/citations.bib` are by reference only; their contents remain subject to their respective licenses.
