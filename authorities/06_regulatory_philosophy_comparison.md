# Regulatory Philosophy Comparison: Multi-Jurisdiction PHI De-identification

**Document version:** 1.0
**Build date:** 2026-06-11
**Audience:** IRB reviewers, corpus designers, legal counsel
**Supersedes:** None (new document)
**Traces to:** AUTHORITY_MATRIX.md Table A, Table B, Table C

This document defines the three structurally distinct regulatory philosophies for health-data privacy and derives the corpus design implications of each. Every identifier category, layer name, and conflict case defined here traces to a primary legal source. Do not treat this document as legal advice; consult qualified counsel for compliance determinations.

---

## 1. The Three Regulatory Philosophies

### Philosophy A -- Rule-based Enumeration (HIPAA model)

**Core mechanism:** A fixed, exhaustive list of identifier categories is specified in statute or regulation. Compliance is achieved by verifying that none of the enumerated identifiers remain in the output. The list is bounded, stable across versions (absent legislative amendment), and mechanically auditable.

**Authoritative source for the canonical list:** 45 CFR 164.514(b)(2)(i), paragraphs (A) through (R). The list contains exactly 18 categories. The scope clause explicitly extends to "relatives, employers, or household members of the individual," which is a design-critical detail.

**Compliance outcome:** Binary. A document is either Safe Harbor compliant or it is not. Expert Determination under 164.514(b)(1) permits a statistical residual-risk argument as an alternative path, but Safe Harbor itself is binary.

**Auditable structure:** An IRB reviewer can verify corpus coverage by checking each of the 18 enumerated paragraphs against the corpus inventory. No contextual judgment about linkability is required for Safe Harbor compliance.

**Known structural limitations:**
- Paragraph (R) "any other unique identifying number, characteristic, or code" functions as a catch-all but does not enumerate what it covers, introducing interpretive uncertainty at the boundary.
- The binary outcome creates a cliff: a document containing a single enumerated identifier fails entirely, regardless of the realistic re-identification risk that identifier introduces.
- Safe Harbor is silent on quasi-identifiers. The Expert Determination method addresses quasi-identifiers; Safe Harbor does not. The "no actual knowledge" clause at 164.514(b)(2)(ii) is the only quasi-identifier protection in the Safe Harbor path.

**Adopters:** United States (HIPAA Privacy Rule, 45 CFR Parts 160 and 164, effective 2003; 2013 Omnibus Final Rule, 78 FR 5566). Canada's PIPEDA Schedule 1 principles adopt a partially enumerated approach, though the federal statute lacks a fixed PHI identifier list at the regulatory level; provincial health privacy laws (e.g., Ontario PHIPA, Alberta HIA) introduce enumerated categories.

**Corpus implication:** Test cases can be designed to achieve provable exhaustive coverage. For every paragraph (A) through (R), at least one positive test case (identifier present, should be flagged) and one negative test case (identifier absent or removed, should be clean) can be systematically generated. Coverage is finite and measurable. Corpus layer: `hipaa_specific`.

---

### Philosophy B -- Principle-based Proportionality (GDPR model)

**Core mechanism:** "Personal data" is defined as any information relating to an identified or identifiable natural person (GDPR Article 4(1)). There is no fixed identifier list. De-identification is assessed contextually: data is not "personal data" if the person "is not or no longer identifiable" accounting for "all the means reasonably likely to be used" by the controller or any other person (Recital 26). The standard is not absolute anonymization but contextual non-identifiability.

**Authoritative source for the proportionality standard:** GDPR Recital 26 ("not reasonably likely" to be used for re-identification); Article 25 (data protection by design and by default); Article 35 (Data Protection Impact Assessment). Article 9 creates a structurally distinct tier of "special categories" for which processing is prohibited absent explicit basis.

**Special categories (Article 9(1)):** Racial or ethnic origin, political opinions, religious or philosophical beliefs, trade union membership, genetic data, biometric data processed for unique identification, health data, sex life or sexual orientation. Three of these (genetic data, biometric data, health data) are distinct enumerated categories, but the outer boundary of "personal data" itself remains unenumerated and context-dependent.

**DPIA requirement:** Processing of special categories at scale, or processing "likely to result in a high risk" to individuals, requires a documented Data Protection Impact Assessment before processing begins (Article 35). A DPIA must describe the processing, assess necessity and proportionality, and identify measures to address identified risks.

**Cross-border transfer constraint -- design-critical for this corpus:** Article 46 GDPR requires that cross-border transfers to third countries occur only where the recipient country provides "essentially equivalent" protection. This means any test corpus or system that handles data from EU/EEA subjects, or that will be evaluated by EU-regulated entities, must satisfy GDPR standards regardless of the country where the system operates. This is the primary reason GDPR compliance is the minimum bar for this repository (see Section 5).

**Adopters:** European Union and European Economic Area (GDPR, applicable from 2018-05-25); United Kingdom (UK GDPR + Data Protection Act 2018, retained after Brexit with minor amendments); Japan (APPI 2022 amendments, Act No. 57 of 2003 as amended by Act No. 37 of 2022, effective 2022-04-01, recognized as "essentially equivalent" by the European Commission under its 2019 adequacy decision); Brazil (LGPD, Lei No. 13.709/2018, effective 2020-09-18, modeled closely on GDPR structure); Australia (Privacy Act 1988 as amended by the Privacy and Other Legislation Amendment Act 2024, enacted 2024-12-10, introducing a right of erasure and Children's Online Privacy Code modeled on GDPR Article 8 logic); Singapore (PDPA 2012 as amended by the Personal Data Protection (Amendment) Act 2020, effective 2021-02-01, adopting a risk-based obligation structure).

**Corpus implication:** Test cases cannot be exhaustive because identifiability is contextual and unbounded. The corpus must include:
- Contextual linkage attack cases: information that is not individually identifying but becomes identifying when combined (quasi-identifier combinations).
- Aggregation attack cases: multiple low-risk records whose combination crosses the identifiability threshold.
- Cross-jurisdiction transfer cases: data that is de-identified under HIPAA Safe Harbor but re-identifiable under GDPR's "reasonably likely" standard (e.g., three-digit ZIP in a low-population county combined with rare diagnosis code).

Corpus layer: `gdpr_specific` (for GDPR-only identifiers such as genetic data as a standalone category, trade union membership, pseudonymous data). Layers for other Philosophy B jurisdictions: `uk_specific`, `australia_specific`, `singapore_specific`, `japan_specific`, `brazil_specific`.

---

### Philosophy C -- Research-permissive with Consent Exemptions (DPDPA/PIPL model)

**Core mechanism:** Both India's DPDPA and China's PIPL permit processing of personal data for research and statistical purposes without individual consent, subject to safeguards. The consent exemption is not incidental but is structurally integrated into the law as a named schedule or provision. This creates a third regulatory posture distinct from both the enumerated-list approach (Philosophy A) and the risk-proportionality approach (Philosophy B).

**India DPDPA 2023 -- Second Schedule research exemption:**
The Second Schedule to the DPDPA lists eight conditions under which a Data Fiduciary may process personal data without consent for research, archiving, or statistical purposes:
1. The purpose cannot reasonably be fulfilled using anonymized data.
2. Personal data is not used to take any decision specific to a Data Principal.
3. Personal data is not transferred outside India without Central Government approval.
4. Personal data is processed in accordance with standards set by the Central Government.
5. Access to personal data is restricted to persons whose access is necessary for the purpose.
6. Personal data is handled in a manner that prevents identification to the extent possible.
7. Personal data is deleted once the purpose is fulfilled or is no longer necessary.
8. The Data Fiduciary maintains a record of processing activities.

The DPDPA Rules 2025 (G.S.R. 846(E), notified 2025-11-13) add Rule 13(3): Data Fiduciaries using algorithmic systems that process personal data must conduct algorithmic due diligence. This provision has a direct regulatory hook for LLM and RAG systems that process clinical data.

**India DPDPA 2023 -- Fourth Schedule Part A pediatric exemption:**
Allows processing of personal data of children in the context of clinical care and educational activities without parental consent, subject to conditions. This creates an annotation requirement in the corpus: pediatric records processed under this exemption require a separate compliance track from adult records.

**China PIPL 2021 (Personal Information Protection Law):**
Article 13(3): Processors may process personal information without separate consent if necessary for fulfilling statutory responsibilities or legal obligations. Article 13(4): permits processing for responding to public health emergencies or protecting life/property in emergencies. Article 13(6): personal information that has been de-identified may be processed without consent if the processor takes appropriate measures to ensure it cannot be re-identified.

PIPL uses a three-tier classification structure that is structurally incompatible with both HIPAA's 18-item list and GDPR's special categories:
- Tier 1: Important data (defined by sector-specific catalogues; not a universal list)
- Tier 2: Personal information (broad, similar to GDPR "personal data")
- Tier 3: Sensitive personal information (Article 28): biometric recognition, religious belief, specific identity, medical health, financial accounts, location tracking, personal information of minors under 14

The PIPL tier structure produces compliance outcomes that cannot be directly mapped to HIPAA Safe Harbor outcomes or GDPR Article 9 outcomes. A record that is compliant under HIPAA Safe Harbor may still constitute "sensitive personal information" under PIPL Article 28 if it contains medical health information, regardless of whether any of HIPAA's 18 identifiers are present.

**Corpus implication for Philosophy C jurisdictions:**
- India DPDPA test cases require separate annotation tracks for Second Schedule research-exempt processing and Fourth Schedule Part A pediatric-exempt processing.
- PIPL test cases require a structurally separate corpus layer. Results from PIPL test cases are NOT directly comparable to HIPAA Safe Harbor results or GDPR de-identification results. Any benchmark comparison that aggregates PIPL and HIPAA/GDPR results without layer separation is methodologically invalid.
- Corpus layers: `india_specific` (DPDPA/SPDI/ICMR), `china_pipl` (structurally separate, not comparable).

---

## 2. Ten-Jurisdiction Reference Table

| Jurisdiction | Governing Law | Philosophy Type | Date in Force | PHI Identifier Standard or List |
|---|---|---|---|---|
| USA | HIPAA Privacy Rule, 45 CFR Part 164; 2013 Omnibus Final Rule (78 FR 5566) | A | 2003-04-14 (rule); 2013-03-26 (Omnibus) | 45 CFR 164.514(b)(2)(i) paragraphs (A)-(R): 18 enumerated categories |
| EU/EEA | General Data Protection Regulation (EU) 2016/679 | B | 2018-05-25 | No fixed list; Article 4(1) "identifiable natural person"; Article 9(1) special categories (9 types); Recital 26 "not reasonably likely" de-id standard |
| India | Digital Personal Data Protection Act, 2023 (Act 22 of 2023); DPDP Rules 2025 (G.S.R. 846(E)); IT Act SPDI Rules 2011 | C | Act: 2023-08-11; Rules: 2025-11-13 (phased to 2027-05-13) | DPDPA Rule 14 identifier vocabulary; SPDI Rule 3 eight categories; Second Schedule 8-condition research exemption |
| Canada | Personal Information Protection and Electronic Documents Act (PIPEDA), S.C. 2000, c. 5; provincial: Ontario PHIPA (S.O. 2004, c. 3); Alberta HIA (SA 2000, c. H-5) | A/B hybrid | PIPEDA: 2001-01-01; Ontario PHIPA: 2004-11-01; Alberta HIA: 2001-04-01 | Federal PIPEDA: principles-based (Schedule 1), no enumerated list; provincial health laws: enumerated identifier lists similar in structure to HIPAA Safe Harbor |
| UK | UK GDPR (retained EU law); Data Protection Act 2018 (DPA 2018) | B | 2018-05-25 (GDPR retained); DPA 2018: 2018-05-25 | Same structure as EU GDPR; Article 9 special categories retained; UK ICO guidance supplements Recital 26 standard |
| Australia | Privacy Act 1988 (Cth); Australian Privacy Principles (APPs); Privacy and Other Legislation Amendment Act 2024 | B | Privacy Act: 1988; APPs: 2014-03-12; 2024 amendments: 2024-12-10 | APP 3: "sensitive information" includes health information, genetic information, biometric information; no fixed 18-item list; context-dependent identifiability |
| Singapore | Personal Data Protection Act 2012 (PDPA); PDPA Amendment Act 2020 | B | 2012-10-15 (enacted); 2014-07-02 (main obligations); 2021-02-01 (2020 amendments) | No fixed identifier list; PDPC advisory guidelines define "personal data" and "health information" contextually; risk-based obligations |
| Japan | Act on the Protection of Personal Information (APPI), Act No. 57 of 2003; 2022 amendments (Act No. 37 of 2022) | B | 2003-05-30; 2022-04-01 (amendments) | Article 2(3): "sensitive personal information" includes medical history, disability, criminal record, sexual orientation; anonymized information standard in Article 2(9); European Commission adequacy decision 2019 |
| Brazil | Lei Geral de Protecao de Dados (LGPD), Lei No. 13.709/2018 | B | 2020-09-18 | Article 5(II): "sensitive personal data" includes health or sex life data, genetic or biometric data, racial/ethnic origin, religious conviction; no fixed identifier list; Article 12 de-identification standard |
| China | Personal Information Protection Law (PIPL), effective 2021-11-01 | C (structurally separate) | 2021-11-01 | Article 28: three-tier classification (important data / personal information / sensitive personal information); Article 28 sensitive tier includes biometric recognition, medical health, financial accounts, location tracking, minors under 14; NOT mappable to HIPAA 18 or GDPR Article 9 |

---

## 3. Universal Common Layer -- Identifiers Present in Every Jurisdiction

The following seven identifier categories are protected in every jurisdiction listed in Section 2. This is the minimum common intersection across all ten jurisdictions. Every corpus test case should include coverage of these identifiers regardless of the target jurisdiction layer.

| Identifier | HIPAA (45 CFR 164.514) | GDPR (EU 2016/679) | DPDPA/SPDI (India) | PIPEDA/Provincial (Canada) | UK GDPR / DPA 2018 | Privacy Act APPs (Australia) | PDPA (Singapore) | APPI (Japan) | LGPD (Brazil) | PIPL (China) |
|---|---|---|---|---|---|---|---|---|---|---|
| Name | (A) Names; scope includes relatives/employers | Art. 4(1) identifiable person; Recital 26 | DPDPA Rule 14 (implicit); SPDI Rule 3 preamble | PIPEDA Sch. 1 Principle 4; PHIPA s. 4(1) | Art. 4(1) UK GDPR retained | APP 3(3)(a) sensitive information preamble; identifiable individual | PDPA s. 2(1) "personal data" | APPI Art. 2(1)(i) | LGPD Art. 5(I) "dados pessoais" | PIPL Art. 4 |
| Date of birth | (C) "birth date" | Art. 4(1) identifiable; context-dependent | DPDPA Rule 14; SPDI implicit | PIPEDA Sch. 1; PHIPA definition s. 4(1)(a) | Art. 4(1) retained | APP 3(3)(a) preamble | PDPC Personal Data Advisory Guidelines para. 3.2 | APPI Art. 2(1) | LGPD Art. 5(I) | PIPL Art. 4 |
| Health / medical records | (H) Medical record numbers; (R) any unique code; scope covers clinical notes | Art. 9(1) "health data" (special category; processing prohibited absent basis) | SPDI Rule 3(iv) "medical records and history"; DPDPA Rule 14 | PHIPA s. 4(1)(a)-(f) enumeration; PIPEDA "sensitive" health info | Art. 9(1) UK GDPR retained | APP 3(3)(a)(i) "health information" as sensitive | PDPA s. 2(1); PDPC Health Sciences Authority guidelines | APPI Art. 2(3)(i) "medical history" sensitive | LGPD Art. 11(I) "saude ou vida sexual" | PIPL Art. 28(ii) "medical health" |
| Biometric data | (P) "Biometric identifiers, including finger and voice prints" | Art. 9(1) "biometric data processed for the purpose of uniquely identifying a natural person" (distinct special category) | SPDI Rule 3(vi) "biometric information"; DPDPA Rule 14 (implicit under sensitive data) | PIPEDA Sch. 1 (sensitive by context); BC PIPA s. 2 | Art. 9(1) UK GDPR retained | APP 3(3)(a)(iv) "biometric information" that is used for identification purposes | PDPA s. 2(1); PDPC guidelines on biometric data | APPI Art. 2(3)(iii) biometric data enumerated | LGPD Art. 11(I) "dado genetico ou biometrico" | PIPL Art. 28(i) "biometric recognition" |
| Email address | (F) "Electronic mail addresses" | Art. 4(1) identifiable; Recital 30 online identifier | DPDPA Rule 14(v) "electronic mail address" | PIPEDA Sch. 1 | Art. 4(1) UK GDPR retained; ICO guidance | APP 3 general personal information | PDPA s. 2(1) | APPI Art. 2(1) | LGPD Art. 5(I) | PIPL Art. 4 |
| Phone number | (D) "Telephone numbers" | Art. 4(1) identifiable | DPDPA Rule 14(iv) "mobile number" | PIPEDA Sch. 1 | Art. 4(1) UK GDPR retained | APP 3 general personal information | PDPA s. 2(1) | APPI Art. 2(1) | LGPD Art. 5(I) | PIPL Art. 4 |
| Physical address | (B) "street address, city, county, precinct, zip code" (below state level) | Art. 4(1) identifiable | DPDPA Rule 14 (via address components) | PIPEDA Sch. 1; PHIPA s. 4(1)(a) | Art. 4(1) UK GDPR retained | APP 3 general personal information | PDPA s. 2(1) | APPI Art. 2(1) | LGPD Art. 5(I) | PIPL Art. 4 |

**Design note:** These seven identifiers form the `common` corpus layer. Every test case in the `common` layer must be annotated with the specific provision from each jurisdiction's law. Benchmark tools evaluated against the `common` layer must produce recall scores reported per-jurisdiction, not as a single aggregate.

---

## 4. Conflict Cases Table

The following cases produce different compliance outcomes across jurisdictions. These cases require explicit corpus representation in the `conflict_cases` layer. A benchmark tool's performance on the `conflict_cases` layer directly measures its ability to handle multi-jurisdiction compliance, which is the hardest practical requirement for systems like RePORTaLiN-RAG.

| Identifier / Case | HIPAA outcome | GDPR outcome | DPDPA outcome | PIPL outcome | Corpus design implication |
|---|---|---|---|---|---|
| ZIP code / postal code: full 5-digit | Non-compliant (violates (B)) | Non-compliant (identifiable in most contexts per Recital 26) | Non-compliant if combined with other identifiers; DPDPA lacks explicit postal code rule; treated as identifiable by context | Personal information under Art. 4; sensitive if combined | Test case: 5-digit ZIP present. Expected: all four jurisdictions flag. |
| ZIP code / postal code: 3-digit prefix (ZIP3) | Compliant IF population of that ZIP3 unit exceeds 20,000 (45 CFR 164.514(b)(2)(i)(B)); must be changed to "000" if <=20,000 | Not automatically compliant; ZIP3 in low-density rural area may still be identifiable under Recital 26 "reasonably likely" standard | No equivalent threshold rule; contextually identifiable | Personal information; context-dependent | Test case: ZIP3 "005" (population <=20,000 per Census). HIPAA outcome: replace with "000". GDPR outcome: assess linkability. These are distinct outcomes requiring separate annotation. |
| Full dates (month, day, year) | Non-compliant: paragraph (C) removes all elements of dates except year | Non-compliant in most contexts; year alone may still be identifiable with other data | DPDPA: dates are identifiable if linking to individual; no explicit date-element rule equivalent to (C) | Personal information; context-dependent | Test case: "admitted on March 15, 2019." HIPAA: remove month and day, retain year. GDPR: assess full date as identifiable. Different redaction targets. |
| Year of birth alone | Compliant under HIPAA Safe Harbor (only date element explicitly retained) | Potentially non-compliant if individual is rare (e.g., 103 years old) and combined with location or disease | No explicit rule; contextual | Personal information | Test case: "Born in 1923 in rural county, diagnosed with [rare disease]." HIPAA: compliant. GDPR: likely identifiable under Recital 26 linkage analysis. |
| Re-identification codes / pseudonymous identifiers | Permitted under 45 CFR 164.514(c) if: (1) code not derived from individual's information; (2) mechanism for re-identification not disclosed with de-identified data. Hash-of-SSN forbidden. Hash-with-salt permitted if salt not disclosed. | Pseudonymous data is still personal data under GDPR Art. 4(5); pseudonymization is a safeguard, not de-identification; covered by Recital 26 "reasonably likely" via key access | DPDPA: pseudonymization referenced in Second Schedule condition 6 as a safeguard; not equivalent to anonymization | PIPL Art. 73(3): de-identified information (ke shi bie xing yi chu) is not personal information; pseudonymized data remains personal information | Test cases: (a) hash-of-SSN (HIPAA non-compliant; must flag); (b) hash-with-salt where salt is stored separately (HIPAA compliant; GDPR still personal data because key exists); (c) tokenized ID with no derivation from personal data (HIPAA compliant; GDPR pseudonymous, still personal data). All three require distinct expected outcomes by jurisdiction. |
| Synthetic data generated during model training | Not addressed by HIPAA Safe Harbor (no training-data provision); if synthetic data re-identifies an individual from training corpus, it would violate the "no actual knowledge" clause at (b)(2)(ii) | Subject to full GDPR obligations if training corpus contained personal data; Recital 26 "anonymous information" exception requires that re-identification is "not reasonably likely"; Membership Inference Attacks (MIA) directly challenge this claim | DPDPA Rule 13(3) algorithmic due diligence requires Data Fiduciaries using algorithmic systems to assess this risk | PIPL: no explicit training-data provision; general de-identification standard applies | Test cases: MIA scenario where synthetic record enables membership inference about training corpus individual. No jurisdiction produces a "clean" outcome. Corpus layer: `conflict_cases` with sub-tag `mia_training_data`. |
| Genetic data | (P) biometric identifiers or (R) catch-all; NOT a separately enumerated category in HIPAA | Art. 9(1): genetic data is an explicitly named special category, distinct from health data and biometric data; has heightened processing prohibition | DPDPA: not explicitly separated; subsumed under health data or sensitive personal data | PIPL Art. 28: not separately named; health and biometric are named but genetic data is not explicitly enumerated | Test case: genetic test result (e.g., BRCA1 variant). HIPAA: flag under (P) or (R). GDPR: flag as special category Art. 9(1) genetic data. These require different annotation tracks because GDPR requires separate lawful basis for genetic data vs. general health data. |
| Trade union membership | Not in HIPAA 18-identifier list; not PHI | Art. 9(1) explicitly named special category with processing prohibition | Not explicitly named in DPDPA | Not named in PIPL as distinct sensitive category | Test case: "Member of SEIU Local 1199" in clinical note. HIPAA: not flagged under Safe Harbor (not in 18). GDPR: flagged as Art. 9(1) special category. Corpus layer: `gdpr_specific`, not `hipaa_specific`. |
| Location tracking / real-time GPS coordinates | Geographic data below state level violates (B) if combined with other identifiers; real-time coordinates not explicitly named | Personal data under Art. 4(1); location data can be special category health data if it reveals clinic visits | DPDPA: location data identifiable under Rule 14 via address components | PIPL Art. 28: "location tracking" (hangji xinxi) explicitly named as sensitive personal information | Test case: GPS coordinate embedded in EXIF metadata of a photograph. PIPL: sensitive. GDPR: personal data, potentially health data. HIPAA: depends on whether coordinate pinpoints a geographic unit smaller than state. |
| Age over 89 | Non-compliant: paragraph (C) requires aggregation to "age 90 or older" | Potentially identifiable under Recital 26 if rare age combined with other data; no fixed threshold | No equivalent rule | Personal information | Test case: "patient is 94 years old." HIPAA: must be replaced with "age 90 or older." GDPR: assess contextual identifiability. |

---

## 5. Corpus Layer Taxonomy

The following layer names are normative for this corpus. Every test case record must include a `layer` field whose value is one of the names defined below. Benchmark results must be reported separately per layer; cross-layer aggregation is invalid for multi-jurisdiction evaluation.

### Layer definitions

| Layer name | Description | Primary authority | Comparability |
|---|---|---|---|
| `common` | Identifiers present in all ten jurisdictions: name, date of birth, health/medical records, biometric data, email address, phone number, physical address. | HIPAA (A)(C)(D)(F)(H)(P); GDPR Art. 4(1), Art. 9(1); DPDPA Rule 14; SPDI Rule 3; PDPA; APPI; LGPD; PIPL Art. 4, 28 | Results comparable across all jurisdictions |
| `hipaa_specific` | Identifiers enumerated in 45 CFR 164.514(b)(2)(i) that are NOT in other jurisdictions' identifier lists as distinct categories: fax numbers (E), pager numbers (R-scope), account numbers (J), health plan beneficiary numbers (I), certificate/license numbers (K), vehicle identifiers/VIN (L), device identifiers (M), web URLs (N), IP addresses (O), MAC addresses (R-scope), full-face photographs (Q). Also includes HIPAA LDS tier (164.514(e)), re-identification code cases (164.514(c)), fundraising context (164.514(f)), and verification audit cases (164.514(h)). | 45 CFR 164.514 | Comparable to other HIPAA-family implementations; not directly comparable to GDPR or PIPL |
| `india_specific` | DPDPA Rule 14 identifier vocabulary; SPDI Rule 3 eight categories; ICMR 2017 research ethics identifiers; ABHA/ABHA Address; Aadhaar; PAN; CTRI registration ID; state-specific driving license formats (30+ variants); ration card (29 state formats); UAN/ESI/CGHS/BPL numbers; vehicle registration. Also includes: Second Schedule 8-condition compliance cases; Fourth Schedule Part A pediatric exemption cases; Rule 13(3) algorithmic due diligence scenarios; 72-hour breach notification fixtures; ICMR four-tier risk categorization; ICMR vulnerability categories; ICMR emergency disclosure overrides (suicidal ideation, HIV status, court order); HMSC international collaboration metadata. | DPDPA 2023; DPDP Rules 2025 (G.S.R. 846(E)); SPDI Rules 2011; ICMR 2017; UIDAI Act 2016; ABDM HDMP 2020; Income Tax Act 1961 | Comparable within India jurisdiction only; not comparable to HIPAA Safe Harbor outcomes |
| `gdpr_specific` | Identifiers that are distinct special categories under GDPR Art. 9(1) but not separately enumerated in HIPAA: genetic data (distinct from biometric); trade union membership; political opinions; religious/philosophical beliefs; data concerning sex life or sexual orientation. Also includes: pseudonymous data annotated as GDPR personal data; linkage attack cases triggered by Recital 26 contextual analysis; DPIA-required processing scenarios; Article 46 cross-border transfer cases. | GDPR Art. 4(1), Art. 9(1), Recital 26, Art. 35, Art. 46; EDPB guidelines on pseudonymization | Comparable to UK GDPR, LGPD Art. 11, APPI Art. 2(3) for special category identifiers; NOT directly comparable to HIPAA Safe Harbor |
| `conflict_cases` | Cases where two or more jurisdictions produce different compliance outcomes for the same input: ZIP3 population threshold; re-identification code types (hash-of-SSN vs hash-with-salt vs token); year of birth in rare-individual contexts; synthetic training data MIA scenarios; genetic data annotation differences; real-time location data. | See Section 4 above; each case cites specific provision per jurisdiction | Not comparable across jurisdictions by design; intended to expose benchmark tool failures at jurisdiction boundaries |
| `canada_specific` | Identifiers enumerated in provincial health privacy laws that extend beyond PIPEDA: Ontario PHIPA s. 4(1) "personal health information" nine-item definition; Alberta HIA s. 1(1)(q) "health information" definition; British Columbia PIPA health information categories. Also includes: federal PIPEDA Schedule 1 principle-based cases for non-health personal data. | PIPEDA S.C. 2000, c. 5; Ontario PHIPA S.O. 2004, c. 3; Alberta HIA SA 2000, c. H-5; BC PIPA SBC 2003, c. 63 | Comparable to HIPAA Safe Harbor for enumerated provincial-health-law identifiers; comparable to GDPR for principle-based federal identifiers |
| `uk_specific` | UK-specific identifiers and cases under UK GDPR and DPA 2018: National Insurance Number (NINO); NHS Number; UK driving licence format; UK National Health Service identifiers. Also includes post-Brexit divergence cases where UK ICO guidance differs from EDPB guidance on the same provision. | UK GDPR (retained EU law); DPA 2018; ICO Anonymisation Code of Practice 2012; ICO updated guidance 2023 | Comparable to GDPR layer for shared Art. 9(1) special categories; UK-divergence cases not comparable |
| `australia_specific` | Health information as defined under APP 3(3)(a)(i): information or opinion about the health or disability of an individual. Also includes Medicare number, Individual Healthcare Identifier (IHI) under Healthcare Identifiers Act 2010, Pharmaceutical Benefits Scheme (PBS) item numbers, My Health Record data under My Health Records Act 2012. | Privacy Act 1988 (Cth); APPs; Healthcare Identifiers Act 2010; My Health Records Act 2012; 2024 amendments | Comparable to GDPR layer for health data special category; not comparable to HIPAA Safe Harbor (no equivalent 18-item list) |
| `singapore_specific` | Personal data as defined under PDPA s. 2(1); NRIC number and FIN (Foreign Identification Number) per PDPC advisory guidance; SingPass/MyInfo identifiers; health information under PDPC health sciences guidelines. | PDPA 2012 (as amended 2021); PDPC Advisory Guidelines; Health Sciences Authority data sharing frameworks | Comparable to GDPR layer for general personal data cases; not comparable to HIPAA |
| `japan_specific` | Sensitive personal information under APPI Art. 2(3): medical history, disability, criminal record, sexual orientation (2017 amendment addition), social welfare status. Also includes: anonymized information standard under APPI Art. 2(9) and guidance from the Personal Information Protection Commission (PPC). The European Commission adequacy decision of 2019 requires "essentially equivalent" protection, making GDPR the effective floor for transfers. | APPI Act No. 57 of 2003 as amended 2022 (Act No. 37 of 2022); PPC Q&A guidance; European Commission adequacy decision 2019-01-23 | Comparable to GDPR layer given adequacy decision; APPI anonymized information standard aligns closely with Recital 26 |
| `brazil_specific` | Sensitive personal data under LGPD Art. 11(I): data on racial or ethnic origin, religious conviction, political opinion, trade union or religious membership, health or sex life data, genetic or biometric data. Also includes: Art. 12 de-identification standard (data that cannot be associated with individual even with use of own means); ANPD-regulated processing scenarios. | LGPD Lei No. 13.709/2018; ANPD resolutions; Art. 11(I) and 12 specifically | Comparable to GDPR layer given structural alignment; LGPD Art. 12 de-id standard maps to Recital 26 |
| `china_pipl` | STRUCTURALLY SEPARATE. PIPL three-tier classification: important data (sector-defined catalogues), personal information (Art. 4), sensitive personal information (Art. 28: biometric recognition, religious belief, specific identity, medical health, financial accounts, location tracking, minors under 14). Results from this layer are NOT comparable to HIPAA Safe Harbor outcomes or GDPR de-identification outcomes. Benchmark scores from this layer must be reported separately and must not be aggregated with any other layer's scores. | PIPL 2021-11-01; Cyberspace Administration of China (CAC) implementing regulations; GB/T 35273-2020 national standard for personal information security | PIPL layer is self-contained. Cross-layer comparisons to HIPAA or GDPR are methodologically invalid. |

### Quick-reference layer summary

```
common                  -- 7 universal identifiers; comparable across all 10 jurisdictions
hipaa_specific          -- HIPAA 18-identifier list + LDS + re-id codes + fundraising context
india_specific          -- DPDPA/SPDI/ICMR identifiers; consent exemption cases; pediatric cases
gdpr_specific           -- GDPR Art. 9(1) special categories not in HIPAA; pseudonymous data
conflict_cases          -- Cross-jurisdiction outcome disagreements
canada_specific         -- Provincial health law enumerations + PIPEDA principle cases
uk_specific             -- NHS identifiers; NINO; post-Brexit ICO divergence cases
australia_specific      -- APP health information; Medicare/IHI; My Health Record cases
singapore_specific      -- PDPA personal data; NRIC/FIN; health sciences guidelines
japan_specific          -- APPI sensitive information; PPC anonymization standard
brazil_specific         -- LGPD Art. 11(I) sensitive data; ANPD cases
china_pipl              -- STRUCTURALLY SEPARATE. Not comparable to any other layer.
```

---

## 6. IRB Documentation Requirement: Applicable Philosophy and Minimum Compliance Bar

### Which philosophy governs this repository as a whole

This repository is a multi-jurisdiction PHI de-identification test corpus. It contains no real personal data; all records are synthetic and seeded. However, the corpus is designed to be used for evaluating systems that process personal data from individuals in multiple jurisdictions, including EU/EEA jurisdictions. That evaluation use creates a compliance obligation under Philosophy B (GDPR) that cannot be avoided by the synthetic nature of the corpus content.

The applicable determination is: any system that is evaluated using this corpus and that processes data from EU/EEA individuals is subject to GDPR Article 35 DPIA obligations for high-risk processing, and any cross-border transfer of that data is subject to GDPR Article 46 adequacy or appropriate-safeguards requirements.

This repository itself is therefore governed by a hybrid of all three philosophies, because:
- The `hipaa_specific` layer tests Philosophy A compliance.
- The `gdpr_specific`, `conflict_cases`, and majority-Type-B jurisdiction layers test Philosophy B compliance.
- The `india_specific` and `china_pipl` layers test Philosophy C compliance.

### Why GDPR compliance is the minimum bar

GDPR Article 46(1) states: "A controller or processor may transfer personal data to a third country or an international organisation only if the controller or processor has provided appropriate safeguards, and on condition that enforceable data subject rights and effective legal remedies for data subjects are available."

The practical effect is that any organization operating in or serving EU/EEA individuals cannot use a system that meets only HIPAA Safe Harbor without separately documenting GDPR compliance. The converse is not true: a system that meets GDPR's Recital 26 "not reasonably likely" de-identification standard satisfies a more stringent test than HIPAA Safe Harbor in most cases, because:

1. GDPR requires documented risk assessment of "all means reasonably likely to be used" for re-identification (Recital 26), whereas HIPAA Safe Harbor requires only removal of the 18 enumerated categories with no contextual risk assessment.

2. GDPR Art. 9(1) covers genetic data, trade union membership, and political opinions as distinct special categories requiring separate lawful basis; HIPAA Safe Harbor either subsumes these under broader categories (biometric) or does not cover them at all (trade union membership).

3. GDPR Art. 35 requires a DPIA for systematic large-scale processing of special categories; HIPAA has no equivalent proactive pre-processing documentation requirement.

4. GDPR's Article 46 transfer requirement means that a corpus or system achieving only HIPAA compliance cannot be legally used by EU-regulated entities to evaluate their processing of EU subject data.

**IRB documentation consequence:** The IRB submission accompanying this corpus must represent that:
- The corpus was designed against GDPR Recital 26 as the minimum de-identification standard.
- Additional layer-specific coverage was designed against HIPAA 164.514(b)(2)(i) Safe Harbor (for HIPAA compliance claims), DPDPA Second Schedule (for Indian research processing claims), and PIPL Art. 28 (for Chinese data processing claims in a structurally separate track).
- PIPL results are reported separately from all other layers and explicitly labeled as non-comparable.
- No benchmark aggregation combines results from the `china_pipl` layer with results from any other layer.

### Minimum documentation package for IRB submission

The following documents are required for a multi-jurisdiction PHI corpus IRB submission under this framework. Each document is referenced here with its required content; the actual documents are in `docs/`.

| Document | Required content | Relevant philosophy |
|---|---|---|
| `VALIDATION_PROTOCOL.md` | Offset validation methodology; seeded generator verification; hash verification of corpus artifacts | All |
| `COUNSEL_REVIEW_CHECKLIST.md` | Legal sign-off per identifier category per jurisdiction; one row per identifier per jurisdiction in AUTHORITY_MATRIX.md Table A | All |
| `CLINICIAN_REVIEW_PROTOCOL.md` | ASQ-PHI methodology; n>=300 records; minimum 3 independent reviewers; inter-rater reliability (Cohen's kappa >= 0.80) | A, B |
| `THREAT_MODEL.md` | OWASP LLM Top 10 2025; MITRE ATLAS; Membership Inference Attack (MIA) surface; k-anonymity violation scenarios | B (Recital 26 risk assessment) |
| `KNOWN_LIMITATIONS.md` | Explicit statement that PIPL results are not comparable; statement that corpus is synthetic and statistical equivalence to real PHI populations is not claimed; all open questions from CLAUDE.md Section "Open questions" | All |
| `ATTESTATION_TEMPLATE.md` | Per-release attestation: no real PHI, seeded generators, validation passing, MANIFEST.json hash | All |

### Summary statement for IRB cover page

This corpus is designed to test PHI de-identification systems under three structurally distinct regulatory frameworks: rule-based enumeration (HIPAA, 45 CFR 164.514), principle-based proportionality (GDPR and aligned jurisdictions), and research-permissive with consent exemptions (India DPDPA Second Schedule; China PIPL). GDPR Recital 26 serves as the minimum de-identification standard because GDPR Article 46 creates a transfer-equivalent obligation that extends to any system evaluated against data from EU/EEA individuals. PIPL results are structurally separate and are not comparable to HIPAA or GDPR results. All test cases are synthetic, seeded for deterministic reproducibility, and contain no real personal data.

---

**End of document. Every claim above traces to a primary legal source. Consult AUTHORITY_MATRIX.md for the full citation list.**
