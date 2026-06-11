# Synthetic Data Legal Basis Statement

**Document version:** 1.0
**Date:** 2026-06-11
**Repository:** PHI-Handling-IRB-approval-ready
**Maintainer:** See SECURITY.md
**Status:** [COUNSEL REVIEW REQUIRED] before any IRB submission

This document replaces the prior implicit assumption that "synthetic data is IRB-exempt by definition." That assumption is legally unsound. This document establishes the actual legal basis for corpus generation and defines the conditions under which that basis holds.

---

## 1. The Prior Assumption and Why It Fails

### 1.1 The assumption

Prior documentation for this repository treated synthetic output as automatically outside the scope of data protection law. The reasoning was: "We generated the data, therefore no real person's information is involved, therefore no legal basis is required."

### 1.2 Why the assumption is legally unsound

**"Synthetic data" is not a legally defined term in any of the following instruments:**

- HIPAA Privacy Rule (45 CFR Parts 160 and 164): The statute uses "individually identifiable health information" (45 CFR 160.103) and "protected health information." Neither term is modified by how the data was created. The statute addresses what the data describes, not its origin.
- GDPR (EU 2016/679): Articles 4(1) and 5 define personal data by whether a natural person "can be identified, directly or indirectly." The mechanism of creation is irrelevant to this test.
- DPDPA 2023 (Act 22 of 2023): Section 2(t) defines "personal data" as "any data about an individual who is identifiable by or in relation to such data." No carve-out for synthetically generated data exists in the Act or in the DPDP Rules 2025 (G.S.R. 846(E)).
- PIPEDA (Canada, SC 2000, c. 5): Schedule 1, Principle 4.3 covers "information about an identifiable individual." No definitional exemption for synthetic generation.

**The GDPR two-step problem**

GDPR Recital 26 states that anonymous information is outside the scope of the Regulation. However, Recital 26 also states: "To determine whether a natural person is identifiable, account should be taken of all the means reasonably likely to be used, such as singling out." This is a functional test, not a generative-process test.

More critically: GDPR Article 5(1)(b) (purpose limitation) and Article 9(2) (conditions for processing special categories) apply to the processing activity, not just to the output. If a synthetic data generation pipeline processes real patient records as training input or generation input, that processing step is itself subject to GDPR. The fact that the output is synthetic does not retroactively exempt the input-processing step.

This creates the "two-step problem": a system that generates synthetic medical data from real EHR records requires a lawful basis under Article 6 and a condition under Article 9(2) for the generation step, even if the output records are not personal data.

**UK ICO position**

The UK Information Commissioner's Office published "Guidance on AI and data protection" (2023) and accompanying synthetic data guidance. The ICO's position is explicit: synthetic data generated from real personal data is subject to data protection law during the generation and training phase. The generation process constitutes processing of the original personal data. The legal basis requirement attaches to the process, not only to the output. Source: ICO, "Explaining decisions made with AI" (2020, updated 2023); ICO synthetic data guidance (2023).

**Singapore PDPC position**

The Personal Data Protection Commission of Singapore published "A Practical Guide to De-identification" (2022). The PDPC states that where synthetic data is derived from real personal data, the generation process constitutes personal data processing under the Personal Data Protection Act 2012 (No. 26 of 2012). A legal purpose is required for the generation step. Source: PDPC Singapore, "A Practical Guide to De-identification," Advisory Guidelines on the PDPA for Selected Topics, 2022.

**South Korea PIPA / ISMS-P**

The Personal Information Protection Act (PIPA, Act No. 10465, 2011, as amended) and the ISMS-P certification framework require a Privacy Impact Assessment (PIA) before processing personal information for purposes including model training, de-identification, and synthetic data generation. The PIA requirement is not waived by the synthetic nature of the output. Source: Korea PIPC, PIPA Article 33; ISMS-P Certification Criteria 2.3.4 (personal information impact assessment).

### 1.3 Why these authorities matter for this repository

This repository does not use real patient records as generation input. However, the prior assumption created a documentation gap. An IRB reviewer or data protection authority encountering the prior documentation could not verify, from that documentation alone, whether the generation process was clean. This document closes that gap by:

1. Stating explicitly that no real personal data was processed at any stage.
2. Citing the legal provisions that govern synthetic corpus generation and explaining why each provision either does not apply or is satisfied.
3. Specifying the technical conditions that must remain true for this legal basis to hold.

---

## 2. This Repository's Position

All records in this corpus are fully synthetic. They were generated by seeded random processes (Python `random.Random(seed)`) operating on:

- Public census frequency tables for name generation
- Published medical terminology (ICD-10-CM, SNOMED CT, RxNorm, CPT-4) for clinical concepts
- Algorithmically constructed identifier formats derived from published regulatory specifications
- No real patient records, no EMR exports, no clinical notes, and no de-identified datasets derived from real patients

The generation process itself had no contact with real PHI at any stage.

This position must be documented in MANIFEST.json (see Section 6) and attested per ATTESTATION_TEMPLATE.md before each release.

---

## 3. Legal Basis for Corpus Generation by Jurisdiction

### 3.1 United States (HIPAA)

**Applicable statute:** Health Insurance Portability and Accountability Act of 1996 (Pub. L. 104-191); Privacy Rule at 45 CFR Parts 160 and 164.

**Position:** HIPAA does not apply to this corpus.

**Reasoning:**

45 CFR 160.103 defines "protected health information" (PHI) as individually identifiable health information that is (i) created or received by a covered entity or business associate and (ii) relates to the past, present, or future physical or mental health of an individual.

The records in this corpus were not created or received by a covered entity. They were created by software generators with no input from any covered entity's systems or data. No record in this corpus relates to the health condition of any real individual, because no real individual contributed data to the generation process.

The HHS Office for Civil Rights de-identification guidance (HHS OCR, "Guidance Regarding Methods for De-identification of Protected Health Information in Accordance with the Health Insurance Portability and Accountability Act (HIPAA) Privacy Rule," 2012-11-26) states: "If health information is not individually identifiable, it is not PHI." More directly applicable: health information that was never linked to a real individual is not PHI because the definitional requirement of "individually identifiable" is never satisfied.

The de-identification provisions at 45 CFR 164.514(b) (Safe Harbor) and 164.514(b)(1) (Expert Determination) are procedures for transforming PHI into non-PHI. They do not apply here because this corpus does not originate as PHI. There is no transformation step because there is no PHI to transform.

45 CFR 164.514(e) (Limited Data Set) likewise does not apply because it governs disclosures of PHI with certain identifiers removed. This corpus is not a limited data set derived from PHI; it is a fully synthetic corpus.

**The corpus does constitute test data for validating PHI detection systems.** The corpus intentionally contains strings that pattern-match PHI categories (SSNs, MRNs, dates, names) for the purpose of testing detection algorithms. This does not make those strings PHI. A test string that looks like an SSN but was randomly generated and never assigned to any person is not protected by HIPAA.

**Condition for this legal basis to hold:** No real PHI, de-identified data, or limited data set may ever be used as input to any corpus generator. See Section 5.

### 3.2 European Union (GDPR)

**Applicable regulation:** Regulation (EU) 2016/679 (General Data Protection Regulation); Recitals 26, 30, 75; Articles 4(1), 5, 9, 89.

**Position:** GDPR does not apply to the generation process because no personal data of any EU natural person was processed during generation. The generation process produced no output that constitutes personal data under Article 4(1) of the GDPR.

**Reasoning on the generation process:**

Article 4(1) defines personal data as "any information relating to an identified or identifiable natural person." Recital 26 clarifies that "information which does not relate to an identified or identifiable natural person or to personal data rendered anonymous in such a manner that the data subject is not or no longer identifiable" is not personal data. The corpus records do not relate to any identified or identifiable natural person because no natural person contributed data to their creation.

The UK ICO and Singapore PDPC concerns described in Section 1.2 arise specifically when synthetic data is generated from real personal data. That precondition does not hold here. There is no "generation from real data" step; the generation process is purely algorithmic.

**Applicability if the corpus is used in EU-connected research:**

If this corpus is used by an EU-based research institution or processed on EU-located infrastructure, the research organization may be acting as a data controller. In that downstream use context, the following provision is the applicable legal basis:

Article 9(2)(j) permits processing of special categories of data (which would include health data that pattern-matches to an identified individual) "for reasons of public interest in the area of scientific or historical research or statistical purposes in accordance with Article 89(1)." Article 89(1) requires appropriate safeguards including pseudonymization where possible.

For the corpus design and validation purpose documented in this repository, Article 9(2)(j) is the applicable condition if any downstream use requires GDPR coverage. The corpus itself does not trigger Article 9 because no data subject exists.

[COUNSEL REVIEW REQUIRED] EU counsel should confirm that the intended research use in the institution's jurisdiction satisfies the Article 89(1) safeguard requirements before any EU-jurisdiction IRB submission citing this document.

### 3.3 India (DPDPA 2023 and DPDP Rules 2025)

**Applicable instruments:** Digital Personal Data Protection Act 2023 (Act 22 of 2023); DPDP Rules 2025 (G.S.R. 846(E), notified 2025-11-13).

**Position:** DPDPA Section 2(t) defines personal data as "any data about an individual who is identifiable by or in relation to such data." The corpus records do not satisfy this definition because no individual is identifiable by them: no real individual contributed to their creation.

**Research exemption (Second Schedule, Rule 16):**

Rule 16 of the DPDP Rules 2025 provides an exemption from certain obligations of the Act for processing of personal data for research, archiving, or statistical purposes. The Second Schedule specifies eight conditions. For completeness, this corpus's compliance with each condition is documented below. Note that this exemption analysis is included for reference; strictly speaking, DPDPA does not apply to corpus generation because no personal data is processed. The Second Schedule analysis governs any downstream use in India.

Second Schedule Condition 1: The purpose of processing is statistical, research, or archival. [Satisfied: The corpus is a benchmark and validation framework for PHI detection systems, constituting scientific research.]

Second Schedule Condition 2: Personal data is not processed for decisions that affect the data principal. [Satisfied: No real data principals exist in this corpus. No decisions affecting any individual are made.]

Second Schedule Condition 3: Personal data is not used to take any action directly against the data principal. [Satisfied: Same reasoning as Condition 2.]

Second Schedule Condition 4: Personal data is rendered non-identifiable to the extent possible. [Satisfied: Corpus records are fully synthetic; no real individual's data is present to render identifiable or non-identifiable.]

Second Schedule Condition 5: The processing adheres to such standards as the Board may specify. [Status: The Data Protection Board of India has not yet published research processing standards as of the date of this document. This condition cannot be confirmed as satisfied until such standards are published. [COUNSEL REVIEW REQUIRED] when standards are published.]

Second Schedule Condition 6: The processing does not involve personal data beyond what is necessary. [Satisfied: No real personal data is processed at any stage.]

Second Schedule Condition 7: The personal data is not transferred outside India except in accordance with the Act. [Satisfied: No real personal data is stored or transferred. The corpus files are synthetic and may be transferred as research artifacts subject to any applicable export or collaboration approval.]

Second Schedule Condition 8: Such other conditions as the Board may specify. [Status: No additional conditions have been specified by the Board as of 2026-06-11. [COUNSEL REVIEW REQUIRED] for monitoring.]

**DPDP Rule 13(3) algorithmic due diligence:**

Rule 13(3) requires significant data fiduciaries to conduct algorithmic due diligence for AI and machine learning systems. This corpus is designed to support such due diligence by providing a benchmark against which detection algorithms can be evaluated. The corpus design is consistent with Rule 13(3)'s purpose and supports, rather than requires, compliance with it.

**ICMR 2017 research ethics guidelines:**

ICMR National Ethical Guidelines for Biomedical and Health Research Involving Human Participants (2017, ISBN 978-81-910091-94), Section 4.8, Table 4.2 specifies an "Exempt" review category.

The Exempt category applies when research does not involve identifiable data about individuals, involves no risk to participants, and involves no personally identifiable information. The conditions for Exempt classification that apply to this corpus are:

- No identifiable data is used: Satisfied. All corpus records are synthetic.
- No risk to participants: Satisfied. No participants exist; no real individuals are involved.
- No personally identifiable information in any form: Satisfied with respect to corpus generation. The corpus contains patterns that simulate PII for testing purposes, but no real individual's PII is present.

[COUNSEL REVIEW REQUIRED] Confirmation by the institutional Ethics Committee that the Exempt classification is appropriate for the specific research protocol in which this corpus is used is required. Table 4.2's Exempt category is a classification decision made by the EC, not a self-designation by the researcher.

ICMR Section 3.3.2 designates institutions as custodians of data, not owners. The institution at which this corpus is developed or used bears custodial responsibility for ensuring that the synthetic-only condition is maintained during all phases of corpus use.

---

## 4. What This Document Does Not Claim

This document does not claim any of the following:

**4.1 Output non-identifiability**

This document does not claim that corpus records can never be used in a re-identification attempt. A synthetic record that happens to match a real patient's profile is not PHI under HIPAA (the match is coincidental, not because the record was derived from the patient). However, statistical plausibility attacks are a documented risk. See Section 7.1.

Independent validation of output non-identifiability is required before any claim that the corpus output constitutes "de-identified data" or "anonymous data" under any jurisdiction's law. This document establishes legal basis for the generation process; it does not constitute a formal de-identification certification.

**4.2 IRB exemption for downstream clinical use**

This document does not claim that any downstream use of this corpus in real clinical settings, clinical trials, or research involving human participants is IRB-exempt. IRB review requirements are determined by the nature of the downstream research protocol, not by the synthetic origin of the corpus used as a tool within that protocol.

**4.3 Substitution for counsel review**

This document is not legal advice. It does not substitute for review by qualified legal counsel in each relevant jurisdiction. Every section marked [COUNSEL REVIEW REQUIRED] must be reviewed before any IRB submission, regulatory filing, or public release.

**4.4 Substitution for clinician plausibility review**

The ASQ-PHI methodology requires clinician review of synthetic clinical narratives to confirm that no narrative inadvertently reproduces a real patient's clinical presentation. This document does not substitute for that review. See CLINICIAN_REVIEW_PROTOCOL.md.

---

## 5. Generation Process Requirements

The legal basis documented in Sections 3.1 through 3.3 holds only when all of the following conditions are true. If any condition is violated, the legal basis must be re-evaluated and this document must be revised before further use.

**5.1 Seeded random number generation**

All generators must use Python's `random.Random(seed)` with an explicit integer seed. No generator may call module-level `random.*` functions (which use the global unseeded state). The seed must be recorded in MANIFEST.json. Given the same seed, every generator must produce bitwise-identical output across runs. This requirement is not merely a reproducibility requirement; it is a legal-basis requirement. If output is not reproducible, the attestation that "no real PHI was used" cannot be independently verified.

**5.2 No real data input**

No real patient records, EMR exports, clinical notes, de-identified datasets, limited data sets, or any data derived from real individuals may be used as input to any generator. This prohibition applies to:

- Training data for any language model used to generate narrative text
- Reference datasets used to verify plausibility of synthetic records
- Frequency distributions derived from real patient populations (census frequency tables derived from public government data are permitted; frequency tables derived from clinical databases are not)

**5.3 Name generation source**

Synthetic names must be drawn from publicly available government census frequency tables (e.g., US Census Bureau surname and first name frequency data, India Census surname data). Names must not be drawn from any patient database, hospital directory, or clinical research participant list.

**5.4 Medical terminology source**

Disease names, clinical concepts, drug names, and procedure codes used in narrative generation must be drawn from published, publicly available medical terminologies: ICD-10-CM, ICD-10-PCS, SNOMED CT (SNOMED International public release), RxNorm (NLM), CPT-4 (AMA; used for code format validation only), LOINC (Regenstrief Institute). No clinical note, case report, or patient record may be used as a narrative template or generation prompt.

**5.5 Identifier format derivation**

Synthetic identifier strings (SSN patterns, MRN formats, Aadhaar-format numbers, PAN-format strings, and all other identifier categories in Table A of AUTHORITY_MATRIX.md) must be derived from the published regulatory specifications for those identifiers. They must not be derived from any dataset of real identifiers.

**5.6 Reproducibility**

Same seed must produce bitwise-identical corpus output. This enables any reviewer to independently regenerate the corpus and verify that the resulting records match the SHA-256 hash in MANIFEST.json. A corpus that cannot be reproduced cannot be audited.

---

## 6. MANIFEST.json Requirements for Legal Defensibility

For each corpus release, MANIFEST.json must contain the following fields. Absence of any field constitutes an incomplete release that must not be cited in an IRB submission.

| Field | Requirement | Purpose |
|---|---|---|
| `corpus_sha256` | SHA-256 hash of all corpus records, computed over the canonical serialization | Integrity verification; supports the attestation that the corpus has not been modified post-generation |
| `record_count` | Integer; total number of records in the corpus | Completeness verification |
| `gold_span_count` | Integer; total number of annotated PHI spans across all records | Annotation completeness verification |
| `generator_manifest` | Array; one entry per generator with `name`, `version`, `seed`, `record_count_emitted`, `authority_citation` | Enables per-generator reproducibility audit |
| `generation_date` | ISO 8601 UTC timestamp | Provenance |
| `no_real_phi_attestation` | Boolean `true`; set by the attesting party per ATTESTATION_TEMPLATE.md | Core legal-basis condition |
| `terminology_sources` | Array of strings listing all medical terminology sources used (ICD-10-CM version, SNOMED CT release date, etc.) | Supports Section 5.4 verification |
| `name_source` | String; identifies the census table used for name generation with version or publication date | Supports Section 5.3 verification |
| `python_version` | String; Python version used for generation | Full reproducibility |
| `generator_package_hashes` | Dict mapping package name to installed hash (from `pip hash`) | Full reproducibility |

---

## 7. Residual Risks That Must Be Documented

Recognizing and documenting residual risks is required for IRB defensibility. An IRB submission that does not acknowledge residual risks will be questioned; one that documents and addresses them demonstrates rigor.

### 7.1 Statistical plausibility attack

A synthetic record that was generated without reference to any real patient may nonetheless, by statistical coincidence, match the profile of a real patient: same age range, same diagnosis, same ZIP code prefix, same initials. Under HIPAA, this coincidental match does not make the record PHI because it was never individually linked to a real person (45 CFR 160.103 requires that the information be "created or received by" a covered entity and that it "identify" or provide "a reasonable basis to believe" the information "can be used to identify an individual"). No such basis exists for a purely coincidental match.

However, if an adversary were to claim that a specific synthetic record was derived from a specific real patient, the MANIFEST.json generator seed and reproducibility requirement in Section 5 provide the rebuttal: the record was generated deterministically from a seeded process with no real-data input, and that process can be independently replicated.

Documentation requirement: KNOWN_LIMITATIONS.md must contain a section on coincidental matches and must reference this document for the legal analysis.

### 7.2 Membership inference attack surface

Synthetic corpora that are statistically calibrated to match population distributions can be used to train membership inference attacks (MIA) against real models. The corpus in this repository is designed to test PHI detection systems. If the corpus's statistical distribution is calibrated against a real patient population, that calibration step may introduce a linkage between the corpus and real individuals.

Mitigation: The `harness/mia_framework.py` module implements the shadow-model MIA methodology per the framework described in Shokri et al. (2017) "Membership Inference Attacks against Machine Learning Models" (IEEE S&P 2017) and the approach documented in the Nature Scientific Reports 2024 analysis of synthetic health data MIA vulnerability. The MIA framework must be run against each corpus release and its output included in the release documentation.

Documentation requirement: KNOWN_LIMITATIONS.md must reference the MIA framework output from each release. If any generator's output produces a non-trivial MIA advantage (above random chance by more than the threshold defined in mia_framework.py), that generator's output must be reviewed before release.

### 7.3 Generator input audit trail

If a future maintainer modifies a generator to accept real data as input (intentionally or by error), the legal basis documented here no longer holds for any records generated by that modified generator. The conditions in Section 5 must be enforced by code review, not documentation alone.

Mitigation: The CI workflow (`.github/workflows/ci.yml`) must include a static check that verifies no generator opens a file path outside the `corpus/` directory tree or makes network calls during generation. This is a defense-in-depth measure; it does not replace code review.

---

## 8. Cross-References

| Document | Relationship to this document |
|---|---|
| `authorities/AUTHORITY_MATRIX.md` | Primary citation source for all legal provisions referenced in Section 3 |
| `MANIFEST.json` | Must satisfy Section 6 requirements for this legal basis to be invocable |
| `ATTESTATION_TEMPLATE.md` | Per-release attestation that the no-real-PHI condition is satisfied |
| `KNOWN_LIMITATIONS.md` | Must document the residual risks in Section 7 |
| `harness/mia_framework.py` | Addresses Section 7.2 |
| `harness/clinical_plausibility_review.py` | Addresses Section 4.4 |
| `docs/CLINICIAN_REVIEW_PROTOCOL.md` | ASQ-PHI methodology referenced in Section 4.4 |
| `docs/COUNSEL_REVIEW_CHECKLIST.md` | Must include one row per [COUNSEL REVIEW REQUIRED] item in this document |

---

## 9. Document Maintenance

This document must be reviewed and updated:

1. Before each corpus release.
2. When any generator is modified to change its data sources.
3. When any of the cited legal instruments are amended or when new regulatory guidance is issued in any covered jurisdiction.
4. When [COUNSEL REVIEW REQUIRED] items are resolved; the resolution must be recorded in the checklist at `docs/COUNSEL_REVIEW_CHECKLIST.md` and summarized here.

Open [COUNSEL REVIEW REQUIRED] items as of 2026-06-11:

- Section 3.2: EU counsel confirmation that the intended research use satisfies Article 89(1) safeguard requirements.
- Section 3.3: Monitoring for Data Protection Board of India publication of research processing standards under Second Schedule Condition 5.
- Section 3.3: Monitoring for any additional Board-specified conditions under Second Schedule Condition 8.
- Section 3.3: Institutional Ethics Committee confirmation of Exempt classification for the specific research protocol.

---

**End of document.**
