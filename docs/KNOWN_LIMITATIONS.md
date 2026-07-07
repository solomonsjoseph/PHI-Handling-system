# KNOWN_LIMITATIONS.md

**Version:** 2.0.0
**Last updated:** 2026-07-06
**Applies to:** Evidence-first PHI corpus, benchmark, and runtime safety harness

This document records limitations, gaps, and unresolved questions in the corpus and benchmark framework as of the version above. IRB reviewers should read this document before drawing conclusions from any benchmark results produced using this corpus. The authors have stress-tested this work adversarially; what follows is an honest account of what remains incomplete, unverified, or out of scope.

---

## Part 1 -- Regulatory Scope Limitations

### 1. DPDPA enforcement timeline

Rules 3 through 16 of the DPDP Rules 2025 (the substantive data principal rights provisions, including security safeguards, breach notification, data fiduciary obligations, and the research exemption under the Second Schedule) do not commence until 2027-05-13 per the official notification G.S.R. 846(E). Rules 17 through 21 and Rules 1, 2 are in force immediately. Rule 4 commences 2026-11-13.

The corpus India-specific layer tests against the full eventual rule set, not the currently-enforced subset. Generators are designed to be enforcement-ready, not currently-enforced.

Any IRB submission filed prior to 2027-05-13 must acknowledge this gap explicitly: the Indian PHI layer is validated against a legal framework that is partially not yet in force. This is not a corpus deficiency in itself, but an IRB reviewer who treats DPDPA compliance claims as currently operative is reading the corpus incorrectly.

### 2. GDPR intra-EU fragmentation not covered

GDPR Article 9(4) permits EU member states to impose additional conditions for processing health data. The corpus covers GDPR as a baseline framework only. It does not cover:

- Germany's Landesdatenschutzgesetze (BDSG and state-level laws)
- France's CNIL health data rules and Health Data Hub requirements
- Netherlands UAVG health-specific provisions
- Any other member-state derogation under Article 9(4)

European IRB reviewers from member states with stricter requirements than baseline GDPR will find gaps in the GDPR layer. The corpus claims GDPR baseline coverage, not member-state completeness.

### 3. China PIPL structural incompatibility

The China PIPL layer results are not directly comparable to HIPAA, GDPR, or DPDPA results. China's Personal Information Protection Law uses a three-tier classification: important data, personal information, and sensitive personal information. This classification has no one-to-one mapping to the Safe Harbor 18-identifier scheme, GDPR special categories, or DPDPA Rule 14 identifiers.

Benchmark tool F1 scores on the China PIPL layer cannot be interpreted using the same thresholds as other layers. A tool with 90% F1 on HIPAA identifiers may perform substantially differently on PIPL-specific identifiers that are not part of HIPAA's enumerated categories. Cross-layer comparisons that include PIPL results require separate interpretation.

### 4. No PIPEDA provincial health coverage

Canada's federal PIPEDA framework is covered in the corpus. Canada's provincial health privacy statutes are not covered:

- PHIPA (Personal Health Information Protection Act, Ontario)
- HIA (Health Information Act, Alberta)
- PIPA (Personal Information Protection Act, British Columbia)

All three provincial statutes are more restrictive than PIPEDA in health data contexts and apply to most Canadian clinical research. Any Canadian health IRB reviewer should treat the Canada layer as PIPEDA-only baseline coverage and not as provincial compliance validation.

---

## Part 2 -- Corpus Completeness Limitations

### 5. No real-world distribution matching

Corpus records are generated using seeded random selection from name pools, geographic tables, and identifier format specifications. The corpus does not attempt to match the statistical distribution of PHI in real clinical notes, including:

- Frequency of SSN versus address versus name mentions per document
- Geographic concentration effects (rural versus urban patient populations)
- Disease prevalence affecting how often specific diagnoses co-occur with identifiers
- Document type effects (inpatient notes versus discharge summaries versus lab results have different PHI density profiles)

Benchmark precision and recall figures derived from this corpus may not predict tool performance on real clinical document distributions, particularly on skewed-distribution edge cases such as documents with unusually high or low PHI density.

### 6. Clinical plausibility review not yet completed

As of v2.0.0, no clinician plausibility review has been conducted on the corpus. The ASQ-PHI methodology requires three independent board-certified clinician reviewers evaluating a random sample of n=300 clinical narrative records, with a target plausibility rating of 96% (95% CI: 93-98%).

Until this review is complete and results are reported:

- Clinical narrative records in the `narratives/` layer should not be used as the sole basis for evaluating medical-context PHI detection performance.
- Any IRB submission claiming clinical plausibility must substitute a clinician review conducted by the submitting institution.
- This is a blocker for full IRB-audit-ready status. It is documented as an open item, not a resolved one.

### 7. File format PHI coverage is synthetic-structural only

File format generators (DICOM, FHIR, XLSX, DOCX, EXIF, Parquet, email, SQLite) embed PHI in metadata fields and data cells using synthetic values following format specifications. They do not replicate the PHI embedding patterns found in real clinical documents, which include:

- PHI burned into DICOM pixel data (not in header tags)
- PHI in DOCX revision history and tracked-change author fields embedded in XML
- PHI in PDF form field values versus body text versus embedded font streams
- PHI in Excel cell formulae, custom XML parts, and external link targets
- PHI in email thread history embedded in quoted reply blocks

Real clinical document PHI embedding is more varied and adversarially located than this corpus tests. A system that clears all corpus file format cases may still fail on these embedded-PHI patterns.

### 8. Quasi-identifier combinations are not exhaustive

The quasi-identifier layer implements Sweeney 2002 three-variable k-anonymity violation scenarios: DOB plus gender plus ZIP, rare disease plus small geography, and profession plus ZIP. These are well-documented in the re-identification literature and serve as a baseline.

The layer does not cover:

- Multi-variable linkage attacks combining five or more quasi-identifiers simultaneously
- Temporal re-identification (sequence of clinical visit dates plus partial demographics)
- Network-based re-identification exploiting social graph structure
- Auxiliary data attacks using publicly available databases (voter registration, obituaries, social media profiles)

The Sweeney baseline covers the most-cited historical re-identification patterns. It is not a comprehensive re-identification attack surface.

---

## Part 3 -- Benchmark Comparison Limitations

### 9. Vendor F1 claims are not independently verified

Performance claims in the existing literature for commercial PHI de-identification tools could not be verified from primary sources during adversarial review of this corpus:

- John Snow Labs Healthcare NLP: 96% F1 on PHI de-identification (vendor documentation and preprints; not independently replicated)
- AWS Comprehend Medical: 83% F1 (AWS whitepaper figures; not independently replicated)
- GPT-4o on PHI de-identification tasks: 79% F1 (preprint figures; not independently replicated)

These figures appear in vendor documentation and preprints. They are cited as directional context only. The benchmark adapters in this repository produce empirical results on this corpus's specific test cases. Those empirical results are the authoritative comparison for this framework. Vendor claims are not.

IRB reviewers should not use vendor-stated F1 figures as a compliance threshold. They should use the adapter-generated results from Section 4 of the benchmark report.

### 10. Three benchmark adapters require credentials not included in this repository

The following adapters will produce null results without external credentials or licenses:

- `benchmarks/comprehend_medical_adapter.py`: requires AWS credentials with `comprehendmedical:DetectPHI` permission
- `benchmarks/azure_health_adapter.py`: requires Azure Health Data Services subscription
- `benchmarks/jsl_adapter.py`: requires a paid John Snow Labs Healthcare NLP license

Benchmarks produced using these adapters are marked `[CREDENTIALS REQUIRED]` in the results. Community members who independently run these adapters with their own credentials should report results via the repository's issue tracker using the benchmark-results issue template, and must include their adapter version, credential scope, and corpus version used.

### 11. Modified Deidentify (EMNLP 2025) adapter is not fully implemented

The Modified Deidentify model (arXiv 2509.14464v1, accepted at EMNLP 2025) was identified as the current state-of-the-art comparator for clinical PHI de-identification during adversarial review. It reports four times fewer clinically dangerous false positives than Llama-3.3 70B on equivalent test conditions. Its adapter has been stubbed in `benchmarks/modified_deidentify_adapter.py` but is not fully implemented.

The reason is pending license confirmation for the model weights and evaluation data. Until the adapter is complete, the Modified Deidentify comparison is absent from benchmark outputs. This is the highest-priority missing benchmark and represents a gap in the state-of-the-art comparison.

---

## Part 4 -- IRB Process Limitations

### 12. No institutional legal counsel review completed

Sections of the authority matrix marked `[COUNSEL REVIEW REQUIRED]` include citations to 45 CFR 164.514 sub-provisions, DPDPA Rule 13(3) algorithmic due diligence obligations, and ICMR Section 1.1.5 override conditions. As of v2.0.0, no institutional legal counsel has reviewed these authority citations or confirmed that the corpus's legal basis section correctly characterizes the applicable regulations.

Institutions using this corpus in an IRB submission must arrange independent counsel review of the legal basis section before filing. The authority matrix is a research document, not a legal opinion.

### 13. SYNTHETIC_DATA_LEGAL_BASIS.md does not constitute legal advice

The legal basis document provides a structured analysis of why synthetic PHI corpus data does not constitute regulated PHI under HIPAA 164.514(b) and why no data use agreement is required for corpus distribution under GDPR Recital 26 and DPDPA Section 3(c). This analysis is provided for documentation and transparency purposes.

It does not constitute legal advice. It has not been reviewed by a licensed attorney. Institutions that require legal assurance of these positions must obtain independent legal review. The document should be treated as a starting point for counsel review, not as a substitute for it.

### 14. Corpus clearance does not constitute regulatory compliance

This corpus is a test and evaluation instrument. A system that achieves the benchmark thresholds specified in `docs/VALIDATION_PROTOCOL.md` on this corpus has demonstrated that it performs correctly on this corpus's specific test cases with this corpus's specific PHI embedding patterns and this corpus's specific identifier distributions.

This is not equivalent to:

- HIPAA compliance for the system under 45 CFR Parts 160 and 164
- DPDPA compliance for a data fiduciary under DPDP Act 2023
- GDPR compliance for a controller or processor under Regulation (EU) 2016/679
- IRB approval for any specific study protocol

Benchmark clearance is one input to an IRB submission. It is not the submission itself, and it does not substitute for a covered entity's own HIPAA risk assessment, a data fiduciary's own DPDPA audit, or a controller's own DPIA.

---

## Part 5 -- Research Citation Limitations

### 15. Two empirical findings are cited from unreviewed or pre-publication sources

Two empirical claims in the authority matrix rely on sources that had not completed peer-reviewed journal publication as of the corpus release date:

**Claim 1:** Hybrid architecture (retrieval-augmented de-identification combining deterministic rules with neural classification) outperforms either approach alone on clinical PHI detection.
**Source:** arXiv 2412.10918, December 2024. Unreviewed preprint at time of corpus release. Not yet published in a peer-reviewed journal.

**Claim 2:** Modified Deidentify achieves four times fewer clinically dangerous false positives than Llama-3.3 70B on clinical PHI de-identification.
**Source:** arXiv 2509.14464v1, accepted at EMNLP 2025 but not yet in print as of corpus release date.

Both are cited as directional evidence consistent with the broader de-identification literature. They are not cited as definitive proof. IRB reviewers who require peer-reviewed journal citations for these specific quantitative claims should note this gap and either wait for publication or substitute published alternatives.

No other empirical claims in the authority matrix rely solely on preprint sources. All regulatory citations are to primary sources (Federal Register, Official Gazette, official ICMR publications).

---

## Part 6 -- Evidence and Claim-Level Limitations

### 16. Claim ladder governs public interpretation

The current public claim ladder is:

| Level | Allowed claim | Current status |
|---|---|---|
| L0 | Prototype PHI corpus and benchmark harness | Supported |
| L1 | Reproducible US/HIPAA synthetic benchmark with span-level gold annotations | Strong |
| L2 | Multi-jurisdiction synthetic PHI benchmark | Partial |
| L3 | File-format and adversarial PHI benchmark | Partial |
| L4 | IRB-audit-ready benchmark with clinician/counsel review | Not yet supported |
| L5 | Market-leading PHI detector or safest AI-powered PHI system | Not yet supported |

The repository must not be interpreted as L4/L5 unless the capability registry, validation reports, strict benchmark artifacts, clinician/counsel status, threat model, and release evidence support that claim. Clinician review, counsel review, and external validation remain PENDING unless a separate durable artifact records completion.

### 17. Evidence commands are required before release claims

Current evidence artifacts should be generated with:

```bash
python -m harness.generate_corpus --seed 42 --jurisdiction all --out-dir corpus
python -m harness.run_all_validations --corpus-dir corpus --manifest corpus/MANIFEST.json --output validation_report.json
python -m benchmarks.presidio_adapter --corpus-dir corpus/us --output-dir benchmarks/results/presidio-stock --profile stock --scoring-profile strict_all_span --verbose
python -m harness.mia_framework --corpus-dir corpus --output mia_report.json
python -m harness.release_evidence --corpus-dir corpus --manifest corpus/MANIFEST.json --validation-report validation_report.json --mia-report mia_report.json --output release_evidence.json
```

These commands provide reproducible structural evidence and release hashes. They do not certify regulatory compliance, complete clinician review, complete counsel review, or prove performance on real clinical distributions.

---

## Part 7 -- CLAUDE.md Build-Plan Gaps (unbuilt scope, not deferred silently)

The original build plan in `CLAUDE.md` Phase 2/3 specified a larger generator and file-format set than currently exists. This section lists exactly what from that plan is NOT built, so the gap is documented rather than silently absent. None of this is scheduled; it is listed here so a reviewer comparing this repo against `CLAUDE.md` does not have to reverse-engineer the delta themselves.

### 18. DPDPA provision-specific generators not built (CLAUDE.md Phase 2)

Only `generators/in/in_dpdpa.py` exists, covering the four DPDP Rules 2025 Rule 14 identifier categories (customer ID file number, acquisition form number, application reference number, enrolment ID). The following generators named in `CLAUDE.md` Phase 2 do not exist:
- `dpdpa_second_schedule.py` (8-condition research-exemption fixtures)
- `dpdpa_pediatric_exemption.py` (Fourth Schedule Part A)
- `dpdpa_algorithmic_dd.py` (Rule 13(3) algorithmic due diligence)
- `dpdpa_breach_timing.py` (72-hour breach notification)
- `dpdpa_consent_manager.py` (token-forwarding scenarios)

### 19. ICMR-specific generators not built (CLAUDE.md Phase 2)

No `icmr_*` generator files exist. The four ICMR generators named in the plan are not built:
- `icmr_risk_categorization.py` (four-tier risk tags)
- `icmr_vulnerability.py` (legal/clinical/situational vulnerability categories)
- `icmr_emergency_disclosure.py` (suicidal ideation / HIV / court-order override cases)
- `icmr_hmsc_international.py` (HMSC international-collaboration metadata)

### 20. Indian identifier generators partially built

`generators/in/in_identifiers.py` covers Aadhaar, PAN, ABHA Number, ABHA Address, Voter ID, CTRI ID, UAN, driving license (single format), and mobile numbers. Not built: `in_ration_card.py` (29 state-variant formats), state-variant driving license beyond one format, and dedicated ESI/CGHS/BPL identifier generators.

### 21. Quasi-identifier layer is US-only

`generators/hipaa_safe_harbor.py` includes `HIPAAQuasiIdentifierGenerator` (profession, city, rare-disease combinations under Sweeney k-anonymity), but the standalone cross-jurisdiction `quasi_identifier_combinations.py` module the plan specified does not exist, so India-side quasi-identifier coverage is absent.

### 22. File-format generators: 5 of 12 built

Built: `xlsx_gen.py`, `dicom_header_gen.py`, `fhir_gen.py`, `eml_gen.py`, `hl7v2_gen.py` (all in `generators/file_formats/`).

Not built: `csv_gen.py`, `pdf_gen.py`, `docx_gen.py`, `cda_gen.py` (HL7 CDA), `exif_gen.py`, `parquet_gen.py`, `sqlite_gen.py`. Any benchmark claim about PDF-, DOCX-, CSV-, CDA-, EXIF-, Parquet-, or SQLite-embedded PHI detection is out of scope until these exist.

### 23. Legacy v1.0.1 corpus import (CLAUDE.md Phase 1) not done

`corpus/legacy_v1.0.1/` does not exist in this repository. The prior 5,942-record / 1,990-gold-span corpus referenced in `CLAUDE.md` has not been imported or reconciled against the current taxonomy.

---

## Acknowledgment

The limitations above were identified through adversarial self-review. The authors' position is that documenting limitations honestly is a precondition for an IRB submission to be taken seriously, not a reason to withhold the submission. Each limitation above has a clear path to resolution: the resolution steps are tracked in the project issue tracker.

Reviewers with questions about any specific limitation should open an issue using the authority-citation issue template or contact the maintainer directly.
