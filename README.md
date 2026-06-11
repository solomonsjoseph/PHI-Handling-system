# PHI Handling -- IRB-Approval-Ready Corpus and Benchmark

**Repository status:** v2.0.0-dev (2026-06-11)
**License:** MIT (see LICENSE)
**Maintainer:** See CONTRIBUTING.md for contact information

This repository contains a multi-jurisdiction PHI detection test corpus, validation harness, and benchmark framework designed to produce results suitable for IRB review. It covers 10 jurisdictions: USA (HIPAA), EU/EEA (GDPR), India (DPDPA 2023 / Rules 2025), Canada (PIPEDA + provincial), UK (UK GDPR), Australia (Privacy Act 2024), Singapore (PDPA 2021), Japan (APPI 2022), Brazil (LGPD), and China (PIPL -- structurally separate layer, not directly comparable to other jurisdictions).

## For IRB reviewers -- read this first

1. Start with `authorities/AUTHORITY_MATRIX.md` -- every identifier category, every generator, every edge case traces to a primary legal or research source.
2. Continue to `docs/VALIDATION_PROTOCOL.md` -- the clinician review protocol, counsel review checklist, and reproducibility attestation.
3. The corpus itself is in `corpus/` with structural validation in `validators/`.
4. Benchmark comparisons against Presidio, Amazon Comprehend Medical, Modified Deidentify (EMNLP 2025), and other published tools are in `benchmarks/`.

This document is structured so that a reviewer can verify corpus completeness by checking the matrix against the authorities cited, without needing to trust the maintainers' claims.

## Scope and what this repository provides

### What it does

- Generates synthetic PHI test records against a common layer of 7 universal identifiers present across all 10 jurisdictions, plus jurisdiction-specific layers for each jurisdiction's unique identifiers. Jurisdiction-specific layers are never mixed: a US record never contains India-specific identifiers and vice versa, except in explicitly-tagged cross-border conflict cases.
- Provides conflict cases layer for identifiers where HIPAA and GDPR produce different legal outcomes: ZIP codes, dates, and re-identification codes. Conflict cases are tagged with the jurisdiction pair whose rules diverge.
- Assigns a `detection_regime` field on every record with values `rule_applicable` (identifier is enumerated in the relevant statute) or `contextual_ner_required` (identifier is contextually sensitive, not enumerated), following the i2b2 taxonomy.
- Covers three de-identification tiers: Safe Harbor de-identified, HIPAA Limited Data Set (LDS), and identifiable PHI.
- Covers 13+ file formats: JSONL, JSON, CSV, XLSX, DOCX, PDF, EML, DICOM headers, HL7 FHIR R4, HL7 CDA, Parquet, EXIF-tagged images.
- Runs baseline benchmarks against Microsoft Presidio, Amazon Comprehend Medical (optional, requires AWS credentials), Modified Deidentify (EMNLP 2025, pending license confirmation), and other tools.
- Provides membership inference attack (MIA) shadow-model framework for privacy testing.

### What it does NOT do

- It does not contain any real PHI.
- It does not contain any actual patient images (synthetic only).
- It is NOT itself a de-identification tool -- it is a test corpus and evaluation harness for such tools.
- It does NOT certify HIPAA, GDPR, or DPDPA compliance for any specific system. Passing this benchmark is necessary but not sufficient for regulatory compliance.
- It does NOT claim to enumerate every possible PHI instance. Safe Harbor (b)(2)(ii) "no actual knowledge" remains a human judgment.
- It does NOT substitute for counsel review. Sections marked `[COUNSEL REVIEW REQUIRED]` require legal sign-off before production use. See `docs/COUNSEL_REVIEW_CHECKLIST.md`.
- It does NOT substitute for clinician review. Three independent board-certified clinicians are required for clinical plausibility review. Status: PENDING. See `docs/CLINICIAN_REVIEW_PROTOCOL.md`.
- It does NOT claim synthetic data is IRB-exempt by definition. Legal basis for synthetic data use in research must be evaluated per-jurisdiction. See `docs/SYNTHETIC_DATA_LEGAL_BASIS.md`.
- China PIPL layer results are NOT directly comparable to HIPAA/GDPR results. The PIPL layer uses a structurally separate taxonomy reflecting China's data processor-centric model. Comparisons across this boundary require explicit acknowledgment in any publication.

## Jurisdictional scope

This corpus covers 10 jurisdictions organized under three regulatory philosophies.

### Rule-based jurisdictions (enumerated identifier lists)

These jurisdictions specify identifiers by explicit statutory list. A record is de-identified when it no longer contains items from the enumerated list.

| Jurisdiction | Primary Law | Identifier Scope |
|---|---|---|
| USA | HIPAA Privacy Rule, 45 CFR 164.514 | 18 Safe Harbor categories; LDS tier at 164.514(e) |
| India | DPDPA 2023, DPDP Rules 2025, SPDI Rules 2011 | Rule 14 identifiers; SPDI 8-category sensitive data |

### Principle-based jurisdictions (contextual determination)

These jurisdictions define personal data by function ("any information relating to an identified or identifiable natural person") rather than by enumerated list. De-identification sufficiency is determined contextually.

| Jurisdiction | Primary Law | Notes |
|---|---|---|
| EU/EEA | GDPR Art. 4(1), Art. 9 (special categories) | Recital 26 re-identification standard |
| UK | UK GDPR, Data Protection Act 2018 | Post-Brexit UK GDPR is functionally similar to EU GDPR; ICO guidance applies |
| Canada | PIPEDA, provincial health privacy acts (PHIPA, HIA) | Quebec Law 25 adds additional obligations from 2023 |
| Australia | Privacy Act 1988 (as amended 2024) | 2024 amendments strengthen de-identification obligations |
| Singapore | PDPA 2021 | Includes new mandatory data breach notification obligations |
| Japan | APPI 2022 (revised) | Anonymously processed information (API) and pseudonymously processed information (PPI) are distinct categories |
| Brazil | LGPD (Lei 13.709/2018) | Sensitive data at Art. 11; anonymization standard at Art. 12 |

### Structurally separate layer

This jurisdiction uses a regulatory model that is not directly comparable to HIPAA/GDPR. Results from this layer must not be aggregated with other jurisdictions without explicit methodology disclosure.

| Jurisdiction | Primary Law | Notes |
|---|---|---|
| China | PIPL (Personal Information Protection Law, 2021) | Data processor-centric model; national security carve-outs; separate taxonomy |

### Architecture rule

The common layer covers the 7 universal identifiers that appear in all 10 jurisdictions: full name, date of birth, email address, telephone number, postal address, government-issued national identifier (or functional equivalent), and medical record number (or clinical encounter identifier). These are placed in `corpus/universal/`. No jurisdiction-specific identifier ever appears in the universal layer.

## Structure

```
PHI-Handling-IRB-approval-ready/
|-- LICENSE                          # MIT license
|-- CODE_OF_CONDUCT.md               # Contributor Covenant 2.1
|-- CONTRIBUTING.md                  # How to contribute
|-- SECURITY.md                      # Security disclosure policy
|-- README.md                        # This file
|-- CHANGELOG.md                     # Versioned changes
|-- MANIFEST.json                    # Corpus hash and provenance
|
|-- authorities/                     # Primary legal/research sources
|   |-- AUTHORITY_MATRIX.md          # The single source of truth for IRB reviewers
|   |-- 01_hipaa_164_514_full.md     # HIPAA 45 CFR 164.514 analysis
|   |-- 02_dpdp_rules_2025.md        # DPDP Rules 2025 analysis
|   |-- 03_icmr_2017.md              # ICMR 2017 analysis
|   |-- 04_spdi_rules_2011.md        # SPDI Rules 2011 analysis
|   |-- 05_gdpr_article9_health_data.md  # GDPR Art. 9 special category analysis
|   |-- 06_regulatory_philosophy_comparison.md  # Cross-jurisdiction philosophy taxonomy
|   `-- citations.bib                # BibTeX of all research citations
|
|-- corpus/                          # Generated test corpus
|   |-- universal/                   # Common layer: 7 identifiers, all 10 jurisdictions
|   |-- us/                          # US HIPAA-specific tests
|   |-- eu/                          # EU GDPR-specific tests
|   |-- in/                          # India DPDPA-specific tests
|   |-- ca/                          # Canada PIPEDA/provincial tests
|   |-- uk/                          # UK GDPR-specific tests
|   |-- au/                          # Australia Privacy Act tests
|   |-- sg/                          # Singapore PDPA tests
|   |-- jp/                          # Japan APPI tests
|   |-- br/                          # Brazil LGPD tests
|   |-- cn/                          # China PIPL tests (separate, not comparable)
|   |-- conflict_cases/              # Identifiers with divergent HIPAA/GDPR outcomes
|   |-- file_formats/                # File-format fixtures
|   |-- limited_data_set/            # HIPAA LDS tier
|   |-- fundraising/                 # 164.514(f) context tests
|   |-- reidentification_codes/      # 164.514(c) tests
|   |-- quasi_identifiers/           # k-anonymity tests
|   |-- injection/                   # OWASP LLM01 prompt injection
|   |-- membership_inference/        # Nature Sci Rep 2024 MIA
|   `-- statistical/                 # Statistical utility fixtures
|
|-- generators/                      # Python code to generate corpus
|   |-- README.md                    # How generators work
|   |-- common.py                    # Shared utilities
|   |-- common_layer/                # Planned: universal 7-identifier generators
|   |-- hipaa_specific/              # Planned: HIPAA 18-category generators
|   |-- india_specific/              # Planned: DPDPA/SPDI/ICMR generators
|   |-- gdpr_specific/               # Planned: GDPR Art. 9 and national law generators
|   |-- conflict_cases/              # Planned: cross-jurisdiction divergence cases
|   |-- hipaa_safe_harbor.py         # 18-category generators
|   |-- hipaa_lds.py                 # Limited Data Set generator
|   |-- dpdpa_second_schedule.py     # DPDPA research exemption
|   |-- spdi_sensitive.py            # 8-category SPDI generator
|   |-- indian_identifiers.py        # Aadhaar, PAN, ABHA, CTRI, etc.
|   |-- file_formats/                # Per-format generators
|   |   |-- xlsx_gen.py
|   |   |-- csv_gen.py
|   |   |-- pdf_gen.py
|   |   |-- docx_gen.py
|   |   |-- dicom_header_gen.py
|   |   |-- fhir_gen.py
|   |   |-- eml_gen.py
|   |   `-- exif_gen.py
|   `-- injection.py                 # Prompt injection fixtures
|
|-- validators/                      # Corpus structural validation
|   |-- offset_validator.py          # Verify every gold span
|   |-- hash_validator.py            # Corpus integrity
|   `-- taxonomy_validator.py        # Taxonomy closure check
|
|-- benchmarks/                      # Comparison with baseline tools
|   |-- README.md                    # How to run benchmarks
|   |-- presidio_adapter.py          # Presidio wrapper
|   |-- comprehend_medical_adapter.py  # AWS Comprehend Medical wrapper
|   |-- azure_health_adapter.py      # Azure Health De-ID wrapper
|   |-- modified_deidentify_adapter.py  # Modified Deidentify (EMNLP 2025) wrapper
|   |-- metrics.py                   # Precision/recall/F1
|   `-- results/                     # Benchmark output
|
|-- harness/                         # Integration test harness
|   |-- run_all_validations.py       # Single-command validation
|   |-- generate_corpus.py           # Rebuild corpus from generators
|   `-- mia_framework.py             # Membership inference attacks
|
|-- tests/                           # Unit tests for generators/validators
|   `-- ...
|
|-- docs/                            # Process documentation
|   |-- VALIDATION_PROTOCOL.md       # Clinician + counsel review process
|   |-- ATTESTATION_TEMPLATE.md      # Reproducibility template
|   |-- COUNSEL_REVIEW_CHECKLIST.md  # Per-item legal sign-off
|   |-- CLINICIAN_REVIEW_PROTOCOL.md # Medical plausibility review
|   |-- THREAT_MODEL.md              # OWASP LLM Top 10 + MITRE ATLAS
|   |-- REPRODUCIBILITY.md           # How to rebuild the corpus
|   |-- KNOWN_LIMITATIONS.md         # Everything this corpus does not cover
|   `-- SYNTHETIC_DATA_LEGAL_BASIS.md  # Legal basis for synthetic data per jurisdiction
|
|-- scripts/                         # Utility scripts
|   `-- ...
|
`-- .github/
    |-- workflows/
    |   `-- ci.yml                   # GitHub Actions CI
    `-- ISSUE_TEMPLATE/
        |-- bug_report.md
        |-- authority_citation.md
        `-- new_identifier.md
```

## Quick start

```bash
git clone https://github.com/brucebanner010198-commits/PHI-Handling-IRB-approval-ready.git
cd PHI-Handling-IRB-approval-ready
python -m pip install -r requirements.txt

# Regenerate the corpus from source (deterministic, seeded)
python harness/generate_corpus.py --seed 20260420

# Validate corpus integrity
python harness/run_all_validations.py

# Run Presidio benchmark
python benchmarks/presidio_adapter.py --corpus corpus/ --output benchmarks/results/presidio_v2.0.json

# Run Comprehend Medical benchmark (requires AWS credentials)
python benchmarks/comprehend_medical_adapter.py --corpus corpus/ --aws-region us-east-1
```

## What makes this IRB-approval-ready

Five properties, each with a defensible mechanism:

1. **Completeness** -- Every HIPAA Safe Harbor identifier, every DPDPA Rule 14 identifier, every SPDI Rules 2011 category, and all identifiers enumerated in the common layer has at least one dedicated generator with test cases. Measured in Table A of `authorities/AUTHORITY_MATRIX.md`. Gap categories are explicitly documented in `docs/KNOWN_LIMITATIONS.md`. The 10-jurisdiction scope is covered at two levels: the common layer (7 identifiers, all jurisdictions) and jurisdiction-specific layers (unique identifiers per jurisdiction, never mixed).

2. **Provenance** -- Every test case has an `authority_citation` field. Every claim in every document in this repository cites the primary source. No claim comes from summaries of summaries.

3. **Reproducibility** -- The corpus is generated from seeded generators. Given the same seed, the output is bitwise identical. `MANIFEST.json` records the hash. `docs/REPRODUCIBILITY.md` documents the exact process.

4. **Benchmark-comparability** -- Baseline results for Presidio (open source, no credentials needed), Amazon Comprehend Medical (optional, needs AWS), and Modified Deidentify (EMNLP 2025, pending license confirmation) are reported. This means an IRB reviewer can compare detector performance to published baselines without trusting the maintainers' numbers. Note: F1 scores for commercial tools have not been independently verified; see Known Limitations.

5. **Reviewer-friendly structure** -- The README points reviewers at the authority matrix first, not at the code. The goal is that a reviewer can say "I believe this corpus is comprehensive" on the basis of evidence, not trust.

## Known limitations

Full detail is in `docs/KNOWN_LIMITATIONS.md`. The five most significant limitations are:

1. **Clinical plausibility review pending.** Three independent board-certified clinicians are required to sign off on clinical plausibility per the ASQ-PHI protocol. This review has not yet been completed. No record in the corpus should be treated as medically authoritative until this review is done.

2. **DPDPA Rules 3-16 not yet in force.** The substantive provisions of the DPDP Rules 2025 (Rules 3 through 16 and Rules 22-23) do not commence until 2027-05-13 per the Official Gazette G.S.R. 846(E). Corpus coverage of these provisions is structurally complete but cannot be validated against live regulatory guidance until commencement.

3. **No independently verified F1 scores for commercial tools.** Benchmark results for Amazon Comprehend Medical, Azure Health Data Services, and John Snow Labs Healthcare NLP are generated using those tools' default configurations. The maintainers have not independently replicated published F1 scores for these tools. Treat benchmark comparisons as indicative, not authoritative.

4. **Modified Deidentify adapter pending license confirmation.** The Modified Deidentify system (EMNLP 2025) benchmark adapter is included in the repository structure, but the adapter cannot be run until license terms for the underlying model weights are confirmed. This item is marked `[COUNSEL REVIEW REQUIRED]`.

5. **China PIPL layer is structurally separate.** The PIPL layer uses a data processor-centric taxonomy that does not map cleanly onto the HIPAA/GDPR identifier model. Aggregate F1 scores that combine PIPL results with other jurisdictions are methodologically invalid. Any publication using this corpus must report PIPL results separately.

Additional limitations covering language coverage, image PHI, state-specific formats, private DICOM tags, longitudinal linkability, and baseline benchmark configurations are documented in `docs/KNOWN_LIMITATIONS.md`.

## Citation

If you use this corpus in research, please cite:

```
[Author]. (2026). PHI-Handling-IRB-approval-ready: A multi-jurisdiction synthetic
PHI test corpus for HIPAA, GDPR, DPDPA, and allied compliance validation [Software].
https://github.com/brucebanner010198-commits/PHI-Handling-IRB-approval-ready
```

## Relationship to RePORTaLiN

This repository is **separate from and not dependent on** the RePORTaLiN-RAG project. It was originally prompted by the need to validate RePORTaLiN-RAG's PHI handling, but the corpus and harness in this repository are general-purpose and can be used with any PHI detection system.

## Security disclosure

See `SECURITY.md`. Do not file security issues as public GitHub issues.

## License

MIT License. See `LICENSE` for full text.

Notable exceptions: The authority matrix references statutory text which is in the public domain. Research citations in `authorities/citations.bib` are by reference only; their contents remain subject to their respective licenses (typically CC-BY for open access journals).
