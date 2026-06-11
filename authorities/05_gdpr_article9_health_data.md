# GDPR Health Data Authority Reference

**Document number:** 05
**Jurisdiction:** European Union
**Primary instrument:** Regulation (EU) 2016/679 of the European Parliament and of the Council (General Data Protection Regulation)
**Official Journal citation:** OJ L 119, 4.5.2016, pp. 1-88
**Secondary instrument:** Regulation (EU) 2024/3175 (European Health Data Space Regulation)
**Official Journal citation:** OJ L 2024/3175, 23.12.2024
**Document version:** 1.0
**Build date:** 2026-06-11
**Purpose:** Primary legal authority reference for corpus design, annotation schema, and IRB documentation. All synthetic data generation decisions referencing EU data protection law trace to this document.

---

## Table of Contents

1. Article 4(1) -- Definition of Personal Data
2. Article 4(15) -- Data Concerning Health
3. Article 4(13) -- Genetic Data
4. Article 4(14) -- Biometric Data
5. Article 9(1) -- Prohibition on Processing Special Categories
6. Article 9(2) -- Exceptions to the Prohibition
7. Article 89 -- Safeguards for Scientific Research Processing
8. Recital 26 -- Anonymous Data and the "Not Reasonably Likely" Standard
9. Recital 35 -- Health Data Definition
10. EU EHDS Regulation 2024/3175 -- Health Data Space Provisions
11. GDPR vs HIPAA Safe Harbor Identifier Comparison Table
12. Key Findings for Corpus Design
    - Conflict Cases

---

## 1. Article 4(1) -- Definition of Personal Data

**Source:** GDPR Article 4(1)

**Full text (verbatim):**

> "personal data" means any information relating to an identified or identifiable natural person ("data subject"); an identifiable natural person is one who can be identified, directly or indirectly, in particular by reference to an identifier such as a name, an identification number, location data, an online identifier or to one or more factors specific to the physical, physiological, genetic, mental, economic, cultural or social identity of that natural person;

**Critical structural point:** The GDPR definition is open-ended and non-enumerative. The phrase "in particular by reference to" does not limit the definition to the listed examples. Any information that relates to a natural person who is or can be identified falls within scope. There is no statutory closed list analogous to HIPAA's 18 Safe Harbor identifiers.

**Contrast with HIPAA:** 45 CFR 164.514(b)(2)(i) enumerates exactly 18 categories of identifiers (paragraphs A through R) whose removal creates a Safe Harbor. GDPR Article 4(1) provides no equivalent closed list. This is the primary source of jurisdiction conflict in multi-framework corpus annotation.

---

## 2. Article 4(15) -- Data Concerning Health

**Source:** GDPR Article 4(15)

**Full text (verbatim):**

> "data concerning health" means personal data related to the physical or mental health of a natural person, including the provision of health care services, which reveal information about his or her health status;

**Scope note:** The phrase "which reveal information about his or her health status" is broad. Data that merely implies or permits inference of health status is covered, not only data that explicitly states a diagnosis or treatment. This broader scope is confirmed by Recital 35 (see Section 9 below).

---

## 3. Article 4(13) -- Genetic Data

**Source:** GDPR Article 4(13)

**Full text (verbatim):**

> "genetic data" means personal data relating to the inherited or acquired genetic characteristics of a natural person which give unique information about the physiology or the health of that natural person and which result, in particular, from an analysis of a biological sample from the natural person in question;

**Structural note:** Genetic data is defined as a separate category from "data concerning health" in Article 4(15). The two definitions overlap but are not coextensive. A genetic variant may be both genetic data and health data simultaneously; however, the GDPR treats genetic data as a distinct special category under Article 9(1) requiring separate regulatory treatment. Corpus generators must annotate genetic identifiers under both Article 4(13) and Article 9(1) labels where applicable.

---

## 4. Article 4(14) -- Biometric Data

**Source:** GDPR Article 4(14)

**Full text (verbatim):**

> "biometric data" means personal data resulting from specific technical processing relating to the physical, physiological or behavioural characteristics of a natural person, which allow or confirm the unique identification of that natural person, such as facial images or dactyloscopic data;

**Scope condition:** Biometric data is only a special category under Article 9(1) "when processed for the purpose of uniquely identifying a natural person." A photograph that is not processed through facial recognition technology is not biometric data under Article 4(14), though it may remain personal data under Article 4(1). Corpus annotation must record the processing purpose field alongside the data type field for biometric records.

**Contrast with HIPAA:** 45 CFR 164.514(b)(2)(i)(P) lists "biometric identifiers, including finger and voice prints" with no processing-purpose condition. The GDPR purpose condition creates a corpus annotation conflict for records where processing context is unspecified.

---

## 5. Article 9(1) -- Prohibition on Processing Special Categories

**Source:** GDPR Article 9(1)

**Full text (verbatim):**

> Processing of personal data revealing racial or ethnic origin, political opinions, religious or philosophical beliefs, or trade union membership, and the processing of genetic data, biometric data for the purpose of uniquely identifying a natural person, data concerning health or data concerning a natural person's sex life or sexual orientation shall be prohibited.

**The nine special categories, listed explicitly:**

1. Racial or ethnic origin
2. Political opinions
3. Religious or philosophical beliefs
4. Trade union membership
5. Genetic data
6. Biometric data (when processed for the purpose of uniquely identifying a natural person)
7. Data concerning health
8. Data concerning a natural person's sex life
9. Data concerning a natural person's sexual orientation

**Structural note:** Categories 8 and 9 (sex life and sexual orientation) are listed as distinct. Categories 5, 6, and 7 (genetic data, biometric data, health data) are the three categories directly relevant to clinical and biomedical corpus design. These three are defined in Articles 4(13), 4(14), and 4(15) respectively and must be treated as separate annotation labels even when the underlying record would qualify under more than one.

---

## 6. Article 9(2) -- Exceptions to the Prohibition

**Source:** GDPR Article 9(2)

The prohibition in Article 9(1) shall not apply if one of the following applies:

### 6.1 Article 9(2)(a) -- Explicit Consent

**Full text (verbatim):**

> the data subject has given explicit consent to the processing of those personal data for one or more specified purposes, except where Union or Member State law provide that the prohibition referred to in paragraph 1 may not be lifted by the data subject;

**Note for corpus design:** Consent under Article 9(2)(a) must be "explicit," a higher standard than the general consent requirement in Article 6(1)(a). IRB documentation must distinguish between explicit consent for special category processing and standard consent for ordinary personal data processing.

### 6.2 Article 9(2)(b) -- Vital Interests

**Full text (verbatim):**

> processing is necessary to protect the vital interests of the data subject or of another natural person where the data subject is physically or incapable of giving consent;

**Note:** This exception is narrow. "Vital interests" is interpreted as life-threatening situations. Compare ICMR 2017 Section 1.1.5 (right to life supersedes right to privacy for suicidal ideation, homicidal tendency, HIV status, court order), which covers overlapping but not identical ground.

### 6.3 Article 9(2)(g) -- Substantial Public Interest

**Full text (verbatim):**

> processing is necessary for reasons of substantial public interest, on the basis of Union or Member State law, which shall be proportionate to the aim pursued, respect the essence of the right to data protection and provide for suitable and specific measures to safeguard the fundamental rights and the interests of the data subject;

**Note:** This exception requires a Union or Member State legal basis. It is not self-executing. For corpus purposes, generators producing public-interest research scenarios must identify the specific legal basis being simulated.

### 6.4 Article 9(2)(h) -- Medical Purposes

**Full text (verbatim):**

> processing is necessary for the purposes of preventive or occupational medicine, for the assessment of the working capacity of the employee, medical diagnosis, the provision of health or social care or treatment or the management of health or social care systems and services on the basis of Union or Member State law or pursuant to contract with a health professional and subject to the conditions and safeguards referred to in paragraph 3;

**Note:** The phrase "subject to the conditions and safeguards referred to in paragraph 3" means this exception requires that processing is carried out by, or under the responsibility of, a professional subject to an obligation of secrecy under Union or Member State law or rules established by national competent bodies.

### 6.5 Article 9(2)(i) -- Public Health

**Full text (verbatim):**

> processing is necessary for reasons of public health, such as protecting against serious cross-border threats to health or ensuring high standards of quality and safety of health care and of medicinal products or medical devices, on the basis of Union or Member State law which provides for suitable and specific measures to safeguard the rights and freedoms of the data subject, in particular professional secrecy;

### 6.6 Article 9(2)(j) -- Scientific Research

**Full text (verbatim):**

> processing is necessary for archiving purposes in the public interest, scientific or historical research purposes or statistical purposes in accordance with Article 89(1) based on Union or Member State law which shall be proportionate to the aim pursued, respect the essence of the right to data protection and provide for suitable and specific measures to safeguard the fundamental rights and the interests of the data subject;

**Note for corpus design:** This is the primary basis on which a PHI test corpus and benchmark framework operates. The conditions of Article 89(1) (see Section 7) must be met. IRB documentation must cite this exception and specify the Article 89(1) safeguards in place.

---

## 7. Article 89 -- Safeguards for Scientific Research Processing

**Source:** GDPR Article 89

**Article 89(1) full text (verbatim):**

> Processing for archiving purposes in the public interest, scientific or historical research purposes or statistical purposes, shall be subject to appropriate safeguards, in accordance with this Regulation, for the rights and freedoms of the data subject. Those safeguards shall ensure that technical and organisational measures are in place in particular in order to ensure respect for the data minimisation principle. Those measures may include pseudonymisation provided that those purposes can be fulfilled in that manner. Where those purposes can be fulfilled by further processing which does not permit or no longer permits the identification of data subjects, those purposes shall be fulfilled in that manner.

**Article 89(2) full text (verbatim):**

> Where personal data are processed for scientific or historical research purposes or statistical purposes, Union or Member State law may provide for derogations from the rights referred to in Articles 15, 16, 18 and 21 subject to the conditions and safeguards referred to in paragraph 1 of this Article in so far as such rights are likely to render impossible or seriously impair the achievement of the specific purposes, and such derogations are necessary for the fulfilment of those purposes.

**Conditions that must be met for Article 89(1) compliance:**

1. A lawful basis under Article 9(2)(j) or equivalent must exist.
2. Technical and organisational measures must be in place.
3. The data minimisation principle (Article 5(1)(c)) must be respected.
4. Pseudonymisation must be used where the research purpose permits it.
5. Where purposes can be fulfilled without identification of data subjects, that non-identifying processing must be used.
6. Any derogations from data subject rights (Articles 15, 16, 18, 21) must be grounded in Union or Member State law and must be necessary for the research purpose.

**Implication for this corpus:** All synthetic test corpus records derive from seeded generators that do not process real patient records. The corpus does not constitute processing of personal data from identified natural persons. However, where generators produce records that could be used to test systems trained on real data, Article 5(1)(b) purpose limitation applies to the downstream system, not the corpus itself. This distinction must be stated in IRB documentation.

---

## 8. Recital 26 -- Anonymous Data and the "Not Reasonably Likely" Standard

**Source:** GDPR Recital 26

**Full text (verbatim):**

> The principles of data protection should therefore not apply to anonymous information, namely information which does not relate to an identified or identifiable natural person or to personal data rendered anonymous in such a manner that the data subject is not or no longer identifiable. This Regulation does not therefore apply to the processing of such anonymous information, including for statistical and research purposes. The principles of data protection should apply to any information concerning an identified or identifiable natural person. Personal data which have been pseudonymised, which could be attributed to a natural person by the use of additional information should be considered to be information on an identifiable natural person. To determine whether a natural person is identifiable, account should be taken of all the means reasonably likely to be used, such as singling out, either by the controller or by another person to identify the natural person directly or indirectly. To ascertain whether means are reasonably likely to be used to identify the natural person, account should be taken of all objective factors, such as the costs of and the amount of time required for identification, having regard to the available technology at the time of the processing and technological developments.

**Why Recital 26 is the most important recital for synthetic PHI corpus design:**

Recital 26 establishes that GDPR does not apply to truly anonymous data, but that the standard for anonymity is "not reasonably likely" re-identification, assessed against "available technology at the time of processing and technological developments." This is a contextual, time-sensitive standard, not a static one.

Four implications follow directly:

1. **Synthetic data generated by large language models (LLMs) or statistical generators may not be anonymous** if the generator was trained on or conditioned by real patient records, because the generation process may encode re-identifiable patterns. The corpus must document that its generators are seeded stochastic processes not derived from real patient data.

2. **Pseudonymous data is explicitly excluded from the anonymous data carve-out.** Recital 26 states that pseudonymised data "could be attributed to a natural person by the use of additional information" and should be considered personal data. HIPAA's re-identification code provisions at 45 CFR 164.514(c) permit codes that are not derived from the individual and whose mechanism is not disclosed. This creates a direct jurisdiction conflict: a dataset that is de-identified under HIPAA may still be personal data under GDPR.

3. **The "reasonably likely" standard evolves with technology.** A de-identification technique adequate in 2020 may not satisfy Recital 26 in 2026 if advances in LLM-assisted re-identification have reduced the cost and time required for re-identification. Corpus versions must be dated and the technological context at time of release must be stated.

4. **The corpus must not contain information that is personal data under GDPR even if de-identified under HIPAA.** Annotation must flag records where the GDPR anonymous-data determination is uncertain, specifically: quasi-identifier combinations (Sweeney 2002), rare-disease + small-geography combinations, and any record where a reasonable adversary with publicly available datasets could achieve k-anonymity violation with k less than or equal to 5.

---

## 9. Recital 35 -- Health Data Definition

**Source:** GDPR Recital 35

**Full text (verbatim):**

> Personal data concerning health should include all data pertaining to the health status of a data subject which reveal information about the past, current or future physical or mental health status of the data subject. This includes information about the natural person collected in the course of the registration for, or the provision of, health care services as referred to in Directive 2011/24/EU to that natural person; a number assigned to a natural person uniquely for health purposes; information derived from the testing or examination of a body part or bodily substance, including from genetic data and biological samples; and any information on, for example, a disease, disability, disease risk, medical history, clinical treatment or the physiological or anatomical state independent of its source, for example from a physician or another health professional, a hospital, a medical device or an in vitro diagnostic test.

**Scope broader than HIPAA in three specific ways:**

1. **Future health status is included.** A predictive model output indicating elevated cancer risk for a named individual is health data under Recital 35, even though no diagnosis exists. HIPAA covers only identifiable health information about past or present conditions.

2. **Data derived from health-related services is included.** A number assigned to a person "uniquely for health purposes" (such as a patient portal login ID) is health data, even if it does not reveal any clinical information on its face.

3. **Data from any source is included.** The phrase "independent of its source" means health data retains its special category status whether it originates from a physician, a hospital, a consumer device, or an in vitro diagnostic test. Wearable device data, consumer health app outputs, and fitness tracker records are health data under Recital 35 if they relate to the health status of an identifiable person.

**Implication for corpus design:** Generators producing device-sourced records (FHIR Observation resources for wearable sensors, DICOM-linked device outputs, structured EHR exports) must annotate those records under GDPR health-data status in addition to HIPAA PHI status.

---

## 10. EU EHDS Regulation 2024/3175 -- European Health Data Space Provisions

**Source:** Regulation (EU) 2024/3175 of the European Parliament and of the Council on the European Health Data Space
**Official Journal citation:** OJ L 2024/3175, 23.12.2024
**Entry into force:** 12 January 2025
**Application schedule:** Primary use provisions apply 24 months after entry into force; secondary use provisions apply 48 months after entry into force for most obligations.

### 10.1 Article 3 -- Definitions of Electronic Health Data

**"Electronic health data" (Article 3(1)):**

> personal data concerning health processed in electronic format;

**"Primary use of electronic health data" (Article 3(5)):**

> the processing of electronic health data by health and care providers for the purpose of providing health and care services to natural persons, including for the purpose of diagnosis, treatment, dispensing of medicines, rehabilitation, disease management, or health promotion, and for related administrative, billing, reimbursement and organisational processes;

**"Secondary use of electronic health data" (Article 3(6)):**

> the processing of electronic health data for purposes other than primary use, including processing for scientific research, innovation, policy-making and regulatory activities, patient safety, personalised medicine, official statistics or activities carried out in the public interest;

**"Health data access body" (Article 3(14)):**

> a public body established or designated by a Member State to grant access to electronic health data for secondary use;

### 10.2 Secondary Use Provisions Relevant to Health AI Systems

The EHDS Regulation establishes a framework for secondary use of electronic health data that layers on top of GDPR Article 9(2)(j) and Article 89. Key provisions:

**Chapter IV (Articles 33-57)** governs secondary use. The key requirements are:

1. Secondary use requires a data permit issued by a health data access body (Article 45) or a data request procedure for specific data types (Article 47). Self-authorization by a researcher is not sufficient.

2. Data access environments must be secure (Article 50). Data must be accessed in a "secure processing environment" -- output of analysis may be shared, but underlying patient-level data may not be exported from that environment without specific approval.

3. Synthetic data and anonymised data generated from real electronic health data are covered during the generation process (Recital 47). The controller processing real data to produce synthetic data must have a valid data permit for that secondary use purpose.

4. Article 56 establishes a cross-border secondary use mechanism. Where data from multiple Member States is required, the EHDS Board coordinates access through designated health data access bodies. This mechanism is the primary legal basis for multinational clinical AI benchmark datasets.

5. Article 53 prohibits re-identification and any attempt to re-identify data that has been provided in anonymised or pseudonymised form. This prohibition applies to both the data recipient and any downstream processors.

### 10.3 What EHDS Adds Beyond GDPR for Health AI Systems

The EHDS Regulation adds four specific provisions that GDPR alone does not provide for health AI systems:

1. **Explicit secondary use permit regime.** GDPR Article 9(2)(j) permits scientific research processing but does not specify the institutional mechanism. EHDS Articles 45-47 establish a mandatory permit system for accessing real electronic health data for AI training or benchmarking.

2. **Mandatory secure processing environments.** GDPR Article 89(1) requires "appropriate safeguards" without specifying the technical form. EHDS Article 50 specifies that real patient-level data must be accessed in a certified secure processing environment. This is a binding technical requirement, not merely a best practice.

3. **Synthetic data governance during generation.** EHDS Recital 47 explicitly addresses synthetic data derived from real electronic health data and requires a data permit for the generation process itself. This closes the gap that would otherwise allow a controller to generate synthetic data from real records under an ambiguous GDPR research basis.

4. **Data quality and interoperability requirements.** EHDS Chapter II (primary use) and Chapter III (MyHealth@EU) require that electronic health data conform to the European Electronic Health Record Exchange Format (EEHRF) based on HL7 FHIR R4. This makes HL7 FHIR R4 the legally mandated format for electronic health data exchange in the EU, not merely a technical standard. Corpus generators producing FHIR records (see Phase 3, `fhir_gen.py`) are therefore generating data in a legally mandated format.

---

## 11. GDPR vs HIPAA Safe Harbor Identifier Comparison Table

The following table maps each of the 18 HIPAA Safe Harbor identifier categories (45 CFR 164.514(b)(2)(i), paragraphs A through R) to their GDPR status. GDPR status categories used:

- **Personal data (Article 4(1)):** Falls within the general definition but is not a special category.
- **Special category (Article 9(1)):** Meets the threshold of a special category requiring Article 9(2) basis.
- **Not enumerated:** GDPR does not list this as a distinct identifier; may still be personal data under the general definition depending on context.
- **Conflict case:** The GDPR and HIPAA treatments diverge in a way that requires corpus-level annotation to flag the jurisdiction.

| Para | HIPAA Safe Harbor Category | GDPR Status | Notes |
|------|---------------------------|-------------|-------|
| A | Names | Personal data (Article 4(1)) | Direct identifier under both frameworks. No conflict. |
| B | Geographic subdivisions smaller than a State (including street address, city, county, precinct, ZIP code) | CONFLICT CASE -- see below | ZIP codes and sub-state geography are conflict cases. Full analysis in Section 12. |
| C | All elements of dates except year; ages over 89 | CONFLICT CASE -- see below | Dates are conflict cases. Full analysis in Section 12. |
| D | Telephone numbers | Personal data (Article 4(1)) | Direct identifier under both frameworks. No conflict. |
| E | Fax numbers | Personal data (Article 4(1)) | GDPR treats fax numbers as contact data equivalent to telephone numbers. No conflict. |
| F | Electronic mail addresses | Personal data (Article 4(1)) | Listed as example identifier in GDPR Article 4(1) by implication; confirmed by Recital 30 (online identifiers). No conflict. |
| G | Social security numbers | Personal data (Article 4(1)) | Equivalent national identification numbers are personal data under GDPR. No conflict in classification, though GDPR Article 87 permits Member States to add specific conditions for national identification numbers. |
| H | Medical record numbers | Personal data (Article 4(1)); may also be health data (Article 4(15)) | A number assigned "uniquely for health purposes" is health data per Recital 35. This may elevate MRN to special category status under Article 9(1). |
| I | Health plan beneficiary numbers | Personal data (Article 4(1)); health data (Recital 35) | Number assigned uniquely for health purposes per Recital 35. Elevation to special category likely. |
| J | Account numbers | Personal data (Article 4(1)) | Financial account numbers are personal data; not a special category under GDPR. No conflict. |
| K | Certificate and license numbers | Personal data (Article 4(1)) | Not enumerated as a distinct GDPR category. Personal data by general definition. No conflict in direction, but GDPR scope is broader. |
| L | Vehicle identifiers and serial numbers, including license plate numbers | Personal data (Article 4(1)) | Confirmed personal data by European Court of Justice case law (C-582/14, Breyer) for IP addresses; same logic applies to license plates. No conflict in direction. |
| M | Device identifiers and serial numbers | Personal data (Article 4(1)) | Medical device identifiers linked to a patient are personal data. May also qualify as health data under Recital 35 (data from a medical device). |
| N | Web Universal Resource Locators (URLs) | Personal data (Article 4(1)) | Online identifiers confirmed as potential personal data by Recital 30. No conflict. |
| O | Internet Protocol (IP) address numbers | Personal data (Article 4(1)) | Confirmed as personal data by CJEU C-582/14 (Breyer, 2016). No conflict. |
| P | Biometric identifiers, including finger and voice prints | Special category (Article 9(1)) when processed for unique identification | CONFLICT CASE: GDPR adds a processing-purpose condition. HIPAA paragraph (P) has no such condition. A fingerprint in a medical record that is not being used for identification may be PHI under HIPAA but may not be biometric data under GDPR Article 4(14). Corpus annotation must record processing purpose. |
| Q | Full face photographic images and any comparable images | Personal data (Article 4(1)); special category (Article 9(1)) when processed for facial recognition | Same processing-purpose issue as paragraph (P). Photograph not processed for facial recognition is personal data but not necessarily special category biometric data under GDPR. |
| R | Any other unique identifying number, characteristic, or code | Personal data (Article 4(1)) -- GDPR's open-ended definition is broader | No conflict in direction. GDPR's general definition covers this class more broadly than HIPAA's catch-all. |

---

## 12. Key Findings for Corpus Design

### 12.1 GDPR Does Not Use an Enumerated Identifier List

GDPR Article 4(1) is a general, open-ended definition of personal data. Any information relating to an identifiable natural person is personal data, regardless of whether it appears on any enumerated list. This has three direct implications:

- Corpus annotation schemas that use only the HIPAA 18 identifier categories as labels are insufficient for GDPR compliance representation. The annotation schema must include a `gdpr_personal_data` boolean field and a `gdpr_special_category` boolean field for every annotated span.
- New identifier types discovered after the corpus release date (for example, a novel health application data field not currently recognized by any framework) are personal data under GDPR if they relate to an identifiable person. The corpus cannot claim to be exhaustive with respect to GDPR scope.
- Generators must not assume that removing the HIPAA 18 identifiers produces GDPR-anonymous data. Residual quasi-identifier combinations may still render records identifiable under the Recital 26 "reasonably likely" standard.

### 12.2 Three Distinct Special Categories for Biomedical Data

GDPR Article 9(1) lists genetic data (Article 4(13)), biometric data (Article 4(14)), and data concerning health (Article 4(15)) as three separate special categories. These three categories have partially overlapping scope but cannot be collapsed into one label:

- Genetic data: inherited or acquired genetic characteristics from analysis of a biological sample. Always special category. Does not require a processing-purpose condition.
- Biometric data: physical, physiological, or behavioural characteristics processed for unique identification. Special category only when the purpose-of-unique-identification condition is met.
- Health data: data related to physical or mental health, including data from health services, data inferred from health-related processing (Recital 35). Always special category when it reveals health status.

Generator annotation schema must include separate boolean flags for `gdpr_genetic`, `gdpr_biometric`, and `gdpr_health_data`. A single `gdpr_special_category` flag without subcategory is insufficient for accurate IRB documentation.

### 12.3 Pseudonymous Data Remains Personal Data

GDPR Recital 26 explicitly states that pseudonymised data "should be considered to be information on an identifiable natural person." This means:

- HIPAA's re-identification code provisions at 45 CFR 164.514(c) permit a covered entity to retain a code allowing re-identification, provided the code is not derived from the individual and the mechanism is not disclosed. Under GDPR, the controller who holds both the pseudonymised data and the key is processing personal data and cannot claim the anonymous-data exclusion in Recital 26.
- A dataset that satisfies HIPAA de-identification Safe Harbor is not necessarily anonymous under GDPR. Cross-border transfers of HIPAA-compliant de-identified data to EU processors require a GDPR-compliant transfer mechanism under Article 46.
- Corpus records tagged as `hipaa_deidentified: true` must separately carry a `gdpr_anonymous: boolean` flag assessed against the Recital 26 standard. The two flags will not always agree.

### 12.4 Synthetic Data Generated From Real Records

GDPR Article 5(1)(b) (purpose limitation) applies to the processing of real patient records during the generation of synthetic data. Even if the synthetic output is anonymous under Recital 26, the generation process itself is processing of the original personal data and requires a lawful basis. EHDS Recital 47 confirms this: a data permit is required for the generation process, not only for access to the output.

For this corpus: all generators are seeded stochastic processes that do not process real patient records. No real patient data is input to any generator. This must be stated in the `REPRODUCIBILITY.md` document and in the IRB application as a foundational design decision.

### 12.5 GDPR as the De Facto Global Standard for Cross-Border Transfers

GDPR Article 46 requires that international transfers to third countries provide an "essentially equivalent" level of protection. The European Commission's adequacy decisions (Article 45) and standard contractual clauses (Article 46(2)(c)) operationalize this. Because the EU represents a large enough data subject population that most global health AI developers require access to EU data subjects, GDPR has become the effective floor for international health data standards.

Implication: Any corpus or benchmark system intended for international use should treat GDPR compliance as the minimum standard and layer HIPAA, DPDPA, and ICMR requirements on top of it, not alongside it as equals. The authority matrix should reflect GDPR as the broadest general framework, with HIPAA and DPDPA as jurisdiction-specific overlays.

---

### 12.6 Conflict Cases

The following identifier categories produce different outcomes under HIPAA and GDPR and require explicit dual-jurisdiction annotation in the corpus. Each conflict case must be represented in the `conflict_jurisdiction` field of affected test records.

#### Conflict Case 1: ZIP Codes and Sub-State Geographic Data

**HIPAA position (45 CFR 164.514(b)(2)(i)(B)):** ZIP codes and sub-state geographic units are covered identifiers that must be removed, with a specific exception for the initial three digits of a ZIP code where the population of the geographic unit formed by all ZIPs with the same three initial digits exceeds 20,000 persons. For ZIP3 units with 20,000 or fewer persons, the ZIP3 must be replaced with "000."

**GDPR position:** ZIP codes are personal data under Article 4(1) because they are geographic location data that can contribute to identification. There is no ZIP-3 exception and no population-threshold rule. However, a ZIP code is not automatically a special category. Whether a ZIP code is "reasonably likely" to identify a person under Recital 26 depends on what other data accompanies it. A ZIP code alone, absent any other linking information, may not render a person identifiable under the Recital 26 standard.

**Conflict:** The HIPAA ZIP-3 rule creates a class of data that is permissible for retention under HIPAA Safe Harbor but is still personal data under GDPR. Records containing ZIP3 codes that satisfy the HIPAA 20,000-population threshold must be annotated `hipaa_safe_harbor: true, gdpr_personal_data: true, conflict_jurisdiction: [HIPAA-B, GDPR-Art4(1)]`.

**Corpus annotation requirement:** The `gdpr_zip_population_threshold_applicable` flag must be set on all records containing US postal codes. Generator `hipaa_lds.py` must emit records with this flag populated.

#### Conflict Case 2: Dates

**HIPAA position (45 CFR 164.514(b)(2)(i)(C)):** All elements of dates except year are covered identifiers for dates "directly related to an individual." Year alone is permitted. Ages over 89 must be aggregated to "90 or older."

**GDPR position:** Dates of birth, admission, and death are personal data under Article 4(1). There is no "year-only" exception. Whether a year of birth alone is identifiable depends on accompanying context. A year of birth combined with diagnosis and location may be identifiable under Recital 26 even if no other HIPAA-covered element is present. Additionally, dates in GDPR scope include any date that contributes to identification, not only dates "directly related to an individual" -- a constraint that HIPAA imposes but GDPR does not.

**Conflict:** The HIPAA year-retention rule creates data that is permissible under HIPAA Safe Harbor but may still be personal data under GDPR when combined with other retained fields. Year of birth in combination with rare disease code and state-level geographic data is a documented quasi-identifier combination (Sweeney 2002) that fails the Recital 26 "reasonably likely" standard.

**Corpus annotation requirement:** Records where only the year element of a date has been retained must carry `hipaa_safe_harbor: true, gdpr_personal_data: uncertain, conflict_jurisdiction: [HIPAA-C, GDPR-Art4(1)-Recital26]`. The `gdpr_personal_data: uncertain` flag triggers mandatory inclusion in the clinician plausibility review protocol.

#### Conflict Case 3: Re-identification Codes

**HIPAA position (45 CFR 164.514(c)):** A code or other means of record identification that allows de-identified information to be re-identified is not a HIPAA identifier, and re-identification is not a violation, provided: (1) the code is not derived from or related to the PHI, and (2) the mechanism for re-identification is not disclosed.

**GDPR position (Recital 26):** The controller who holds both the pseudonymised record and the key (re-identification mechanism) is processing personal data. The controller cannot claim the anonymous-data exclusion in Recital 26, because the data "could be attributed to a natural person by the use of additional information" -- specifically, the key that the controller holds.

**Conflict:** A dataset that uses HIPAA-permitted re-identification codes is personal data under GDPR. The controller is subject to all GDPR obligations, including the requirement for a lawful basis under Article 6 and, if health data is involved, a basis under Article 9(2). The HIPAA Safe Harbor determination does not change this outcome.

**Corpus annotation requirement:** Records generated by `hipaa_reid_codes.py` must carry `hipaa_reidentifiable: true, gdpr_personal_data: true, conflict_jurisdiction: [HIPAA-164.514(c), GDPR-Recital26]`. The generator must emit both the permitted (non-derived code) and forbidden (SSN-derived hash) variants as documented in `authorities/01_hipaa_164_514_full.md`.

#### Conflict Case 4: Biometric Data Processing Purpose

**HIPAA position (45 CFR 164.514(b)(2)(i)(P)):** "Biometric identifiers, including finger and voice prints" are covered identifiers with no processing-purpose condition. Any biometric identifier in a covered entity's records is PHI.

**GDPR position (Article 4(14), Article 9(1)):** Biometric data is a special category only "when processed for the purpose of uniquely identifying a natural person." A fingerprint stored in a medical record for audit trail purposes (confirming that a specific clinician performed a procedure) may not be biometric data under GDPR Article 4(14) if it is not being used to identify a natural person as data subject.

**Conflict:** A biometric identifier that is PHI under HIPAA paragraph (P) may not be biometric data under GDPR Article 4(14), and therefore may not require an Article 9(2) basis. It remains personal data under Article 4(1).

**Corpus annotation requirement:** Biometric records generated by `hipaa_biometric.py` must include a `processing_purpose` field. Records with `processing_purpose: identification` carry `gdpr_special_category: true, gdpr_biometric: true`. Records with `processing_purpose: audit_trail` or `processing_purpose: unspecified` carry `gdpr_special_category: uncertain, gdpr_biometric: uncertain, conflict_jurisdiction: [HIPAA-P, GDPR-Art4(14)]`.

---

## Citation Summary

| Citation | Description |
|----------|-------------|
| GDPR Art. 4(1) | Personal data definition |
| GDPR Art. 4(13) | Genetic data definition |
| GDPR Art. 4(14) | Biometric data definition |
| GDPR Art. 4(15) | Data concerning health definition |
| GDPR Art. 9(1) | Prohibition on processing special categories |
| GDPR Art. 9(2)(a) | Explicit consent exception |
| GDPR Art. 9(2)(b) | Vital interests exception |
| GDPR Art. 9(2)(g) | Substantial public interest exception |
| GDPR Art. 9(2)(h) | Medical purposes exception |
| GDPR Art. 9(2)(i) | Public health exception |
| GDPR Art. 9(2)(j) | Scientific research exception |
| GDPR Art. 46 | Cross-border transfer requirements |
| GDPR Art. 89(1) | Safeguards for scientific research |
| GDPR Art. 89(2) | Derogations for scientific research |
| GDPR Recital 26 | Anonymous data and the "not reasonably likely" standard |
| GDPR Recital 30 | Online identifiers as personal data |
| GDPR Recital 35 | Health data definition including inferred data |
| GDPR Recital 47 | (via EHDS) Synthetic data generation requires data permit |
| EHDS Art. 3(1) | Electronic health data definition |
| EHDS Art. 3(5) | Primary use definition |
| EHDS Art. 3(6) | Secondary use definition |
| EHDS Art. 3(14) | Health data access body definition |
| EHDS Art. 45 | Data permit requirement for secondary use |
| EHDS Art. 47 | Data request procedure |
| EHDS Art. 50 | Secure processing environment requirement |
| EHDS Art. 53 | Re-identification prohibition |
| EHDS Art. 56 | Cross-border secondary use mechanism |
| CJEU C-582/14 | Breyer (2016) -- IP addresses as personal data |
| 45 CFR 164.514(b)(2)(i) | HIPAA Safe Harbor 18 identifier categories |
| 45 CFR 164.514(c) | HIPAA re-identification code provisions |
| Sweeney 2002 | k-anonymity quasi-identifier combinations |

---

**End of document. Cross-reference: `authorities/AUTHORITY_MATRIX.md` Table A rows 1-55 for GDPR column additions needed in the next matrix revision.**
