# PHI Handling — IRB-Approval-Ready Corpus and Benchmark

**Repository status:** v1.0.0 (initial release, 2026-04-20)
**License:** MIT (see LICENSE)
**Maintainer:** See CONTRIBUTING.md for contact information

This repository contains a dual-jurisdiction (HIPAA + DPDPA) PHI detection test corpus, validation harness, and benchmark framework designed to produce results suitable for IRB review.

## For IRB reviewers — read this first

1. Start with `authorities/AUTHORITY_MATRIX.md` — every identifier category, every generator, every edge case traces to a primary legal or research source.
2. Continue to `docs/VALIDATION_PROTOCOL.md` — the clinician review protocol, counsel review checklist, and reproducibility attestation.
3. The corpus itself is in `corpus/` with structural validation in `validators/`.
4. Benchmark comparisons against Presidio, Amazon Comprehend Medical, and other published tools are in `benchmarks/`.

This document is structured so that a reviewer can verify corpus completeness by checking the matrix against the authorities cited, without needing to trust the maintainers' claims.

## Scope and what this repository provides

### What it does
- Generates synthetic PHI test records covering HIPAA 18 Safe Harbor identifiers, DPDPA Rule 14 identifiers, SPDI Rules 2011 sensitive categories, and ICMR 2017 vulnerability classifications.
- Provides three de-identification tiers: Safe Harbor de-identified, HIPAA Limited Data Set (LDS), and identifiable PHI.
- Covers 13+ file formats: JSONL, JSON, CSV, XLSX, DOCX, PDF, EML, DICOM headers, HL7 FHIR R4, HL7 CDA, Parquet, EXIF-tagged images.
- Runs baseline benchmarks against Microsoft Presidio, Amazon Comprehend Medical (optional, requires AWS credentials), and other tools.
- Provides membership inference attack (MIA) shadow-model framework for privacy testing.

### What it does NOT do
- It does not contain any real PHI.
- It does not contain any actual patient images (synthetic only).
- It is NOT itself a de-identification tool — it is a test corpus and evaluation harness for such tools.
- It does NOT claim to enumerate every possible PHI instance. Safe Harbor (b)(2)(ii) "no actual knowledge" remains a human judgment.
- It does NOT substitute for counsel review. Sections marked `[COUNSEL REVIEW REQUIRED]` require legal sign-off before production use.
- It does NOT substitute for clinician review. Sections marked `[CLINICIAN REVIEW REQUIRED]` require medical sign-off for clinical plausibility.

## Jurisdictional scope

This corpus covers two primary jurisdictions:

1. **United States** — HIPAA Privacy Rule, HITECH, 2013 Omnibus Final Rule, Common Rule (45 CFR 46), FTC Act as applicable
2. **India** — DPDPA 2023, DPDP Rules 2025, IT Act SPDI Rules 2011, ICMR 2017 National Ethical Guidelines, CDSCO New Drugs and Clinical Trials Rules 2019

A universal/common layer covers identifiers that apply across jurisdictions (dates, email, phone in any format, person names) and is kept separate from country-specific layers. Country-specific layers are never mixed — an IN record never contains US-specific identifiers and vice versa, except in explicitly-tagged cross-border test cases.

## Structure

```
PHI-Handling-IRB-approval-ready/
├── LICENSE                          # MIT license
├── CODE_OF_CONDUCT.md               # Contributor Covenant 2.1
├── CONTRIBUTING.md                  # How to contribute
├── SECURITY.md                      # Security disclosure policy
├── README.md                        # This file
├── CHANGELOG.md                     # Versioned changes
├── MANIFEST.json                    # Corpus hash and provenance
│
├── authorities/                     # Primary legal/research sources
│   ├── AUTHORITY_MATRIX.md          # The single source of truth for IRB reviewers
│   ├── 01_hipaa_164_514_full.md     # HIPAA 45 CFR 164.514 analysis
│   ├── 02_dpdp_rules_2025.md        # DPDP Rules 2025 analysis
│   ├── 03_icmr_2017.md              # ICMR 2017 analysis
│   ├── 04_spdi_rules_2011.md        # SPDI Rules 2011 analysis
│   └── citations.bib                # BibTeX of all research citations
│
├── corpus/                          # Generated test corpus
│   ├── universal/                   # Cross-jurisdiction tests
│   ├── us/                          # US HIPAA-specific tests
│   ├── in/                          # India DPDPA-specific tests
│   ├── file_formats/                # File-format fixtures
│   ├── limited_data_set/            # HIPAA LDS tier
│   ├── fundraising/                 # 164.514(f) context tests
│   ├── reidentification_codes/      # 164.514(c) tests
│   ├── quasi_identifiers/           # k-anonymity tests
│   ├── injection/                   # OWASP LLM01 prompt injection
│   ├── membership_inference/        # Nature Sci Rep 2024 MIA
│   └── statistical/                 # Statistical utility fixtures
│
├── generators/                      # Python code to generate corpus
│   ├── README.md                    # How generators work
│   ├── common.py                    # Shared utilities
│   ├── hipaa_safe_harbor.py         # 18-category generators
│   ├── hipaa_lds.py                 # Limited Data Set generator
│   ├── dpdpa_second_schedule.py     # DPDPA research exemption
│   ├── spdi_sensitive.py            # 8-category SPDI generator
│   ├── indian_identifiers.py        # Aadhaar, PAN, ABHA, CTRI, etc.
│   ├── file_formats/                # Per-format generators
│   │   ├── xlsx_gen.py
│   │   ├── csv_gen.py
│   │   ├── pdf_gen.py
│   │   ├── docx_gen.py
│   │   ├── dicom_header_gen.py
│   │   ├── fhir_gen.py
│   │   ├── eml_gen.py
│   │   └── exif_gen.py
│   └── injection.py                 # Prompt injection fixtures
│
├── validators/                      # Corpus structural validation
│   ├── offset_validator.py          # Verify every gold span
│   ├── hash_validator.py            # Corpus integrity
│   └── taxonomy_validator.py        # Taxonomy closure check
│
├── benchmarks/                      # Comparison with baseline tools
│   ├── README.md                    # How to run benchmarks
│   ├── presidio_adapter.py          # Presidio wrapper
│   ├── comprehend_medical_adapter.py # AWS Comprehend Medical wrapper
│   ├── azure_health_adapter.py      # Azure Health De-ID wrapper
│   ├── metrics.py                   # Precision/recall/F1
│   └── results/                     # Benchmark output
│
├── harness/                         # Integration test harness
│   ├── run_all_validations.py       # Single-command validation
│   ├── generate_corpus.py           # Rebuild corpus from generators
│   └── mia_framework.py             # Membership inference attacks
│
├── tests/                           # Unit tests for generators/validators
│   └── ...
│
├── docs/                            # Process documentation
│   ├── VALIDATION_PROTOCOL.md       # Clinician + counsel review process
│   ├── ATTESTATION_TEMPLATE.md      # Reproducibility template
│   ├── COUNSEL_REVIEW_CHECKLIST.md  # Per-item legal sign-off
│   ├── CLINICIAN_REVIEW_PROTOCOL.md # Medical plausibility review
│   ├── THREAT_MODEL.md              # OWASP LLM Top 10 + MITRE ATLAS
│   ├── REPRODUCIBILITY.md           # How to rebuild the corpus
│   └── KNOWN_LIMITATIONS.md         # Everything this corpus does not cover
│
├── scripts/                         # Utility scripts
│   └── ...
│
└── .github/
    ├── workflows/
    │   └── ci.yml                   # GitHub Actions CI
    └── ISSUE_TEMPLATE/
        ├── bug_report.md
        ├── authority_citation.md
        └── new_identifier.md
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
python benchmarks/presidio_adapter.py --corpus corpus/ --output benchmarks/results/presidio_v1.0.json

# Run Comprehend Medical benchmark (requires AWS credentials)
python benchmarks/comprehend_medical_adapter.py --corpus corpus/ --aws-region us-east-1
```

## What makes this IRB-approval-ready

Five properties, each with a defensible mechanism:

1. **Completeness** — Every HIPAA Safe Harbor identifier, every DPDPA Rule 14 identifier, every SPDI Rules 2011 category has at least one dedicated generator with test cases. Measured in Table A of `authorities/AUTHORITY_MATRIX.md`. Gap categories are explicitly documented in `docs/KNOWN_LIMITATIONS.md`.

2. **Provenance** — Every test case has an authority_citation field. Every claim in every document in this repository cites the primary source. No claim comes from summaries of summaries.

3. **Reproducibility** — The corpus is generated from seeded generators. Given the same seed, the output is bitwise identical. `MANIFEST.json` records the hash. `docs/REPRODUCIBILITY.md` documents the exact process.

4. **Benchmark-comparability** — Baseline results for Presidio (open source, no credentials needed) and Amazon Comprehend Medical (optional, needs AWS) are reported. This means an IRB reviewer can compare our detector performance to published baselines without trusting our numbers.

5. **Reviewer-friendly structure** — The README points reviewers at the authority matrix first, not at our code. The goal is that a reviewer can say "I believe this corpus is comprehensive" on the basis of evidence, not trust.

## Known limitations

This section exists because overclaiming is worse than underclaiming.

1. **Language coverage:** Text generators are English and transliterated Hindi/Tamil/Bengali names only. Urdu, Kashmiri, Punjabi, Malayalam, Telugu, Odia, Assamese, Marathi, Gujarati, Kannada names are not covered at production density.

2. **Image PHI:** Full-face photograph detection (HIPAA Q) requires image analysis that is out of scope for text-based corpus validation. See `docs/KNOWN_LIMITATIONS.md` for the rationale.

3. **State-specific formats:** India has 29 states with independent ration card formats. Coverage is best-effort and may miss new formats. See the authority matrix.

4. **Private DICOM tags:** Vendor-specific private DICOM tags cannot be enumerated generically. We cover common vendors (GE, Siemens, Philips, Canon) but claim no universal coverage.

5. **Longitudinal linkability:** Re-identification via longitudinal data (same rare-disease patient visible across years) requires dataset-level analysis and is not fully covered in record-level test cases.

6. **Newly enacted authorities:** DPDP Rules 2025 most substantive provisions do not commence until 2027-05-13. Applicability of our corpus to future rule interpretations cannot be guaranteed.

7. **Benchmarks are baseline:** Presidio and Comprehend Medical can be enhanced with custom recognizers. Our baseline comparisons use each tool's default configuration. Tool performance will differ with customization.

## Citation

If you use this corpus in research, please cite:

```
[Author]. (2026). PHI-Handling-IRB-approval-ready: A dual-jurisdiction synthetic 
PHI test corpus for HIPAA and DPDPA compliance validation [Software]. 
https://github.com/brucebanner010198-commits/PHI-Handling-IRB-approval-ready
```

## Relationship to RePORTaLiN

This repository is **separate from and not dependent on** the RePORTaLiN-RAG project. It was originally prompted by the need to validate RePORTaLiN-RAG's PHI handling, but the corpus and harness in this repository are general-purpose and can be used with any PHI detection system.

## Security disclosure

See `SECURITY.md`. Do not file security issues as public GitHub issues.

## License

MIT License. See `LICENSE` for full text.

Notable exceptions: The authority matrix references statutory text which is in the public domain. Research citations in `authorities/citations.bib` are by reference only; their contents remain subject to their respective licenses (typically CC-BY for open access journals).
