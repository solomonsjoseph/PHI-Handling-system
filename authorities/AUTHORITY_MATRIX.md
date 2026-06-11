# Consolidated Authority Matrix

**Document version:** 2.0
**Build date:** 2026-06-11
**Maintainer:** See LICENSE and CONTRIBUTING.md

This matrix is the single source of truth for IRB review. Every identifier category, every generator, every edge case in this repository traces to a primary legal or research source. IRB reviewers reading this document should be able to verify corpus coverage at a glance and audit every claim against its citation.

## Table A — Identifier categories mapped to primary authorities

### Direct identifiers (must remove under US Safe Harbor + DPDPA Second Schedule)

| # | Identifier category | HIPAA 164.514(b)(2)(i) | GDPR | DPDPA Rule 14 | ICMR 2017 | SPDI Rule 3 | DICOM PS3.15 | FHIR R4 Patient | Presidio | AWS Comprehend |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Patient name | (A) Names | Art. 4(1) personal data | implicit | 2.3.5 | - | (0010,0010), (0010,1001), (0010,1005) | Patient.name | PERSON | NAME |
| 2 | Provider/physician name | (A) Names (scope: "workforce") | Art. 4(1) personal data | Rule 14 licence | 2.3.1 | - | (0008,0090), (0008,1048), (0008,1050), (0008,1060), (0008,1070) | Practitioner.name | PERSON | NAME |
| 3 | Household/relative name | (A) Names (explicit household scope) | Art. 4(1) personal data | implicit | 2.3.1 | - | - | Patient.contact.name, RelatedPerson.name | PERSON | NAME |
| 4 | Street address (full) | (B) Geographic subdivisions smaller than State | Art. 4(1) personal data | implicit | 2.3.1 | - | (0010,1040) | Patient.address.line | LOCATION | ADDRESS |
| 5 | City | (B) ... except ZIP3 permitted | Art. 4(1) personal data | implicit | 2.3.1 | - | (0010,1040) | Patient.address.city | LOCATION | ADDRESS |
| 6 | State (US) / State (IN) | Permitted under (B) | Art. 4(1) personal data | Rule 14 | - | - | - | Patient.address.state | LOCATION | ADDRESS |
| 7 | ZIP code (full 5/9 digit) | (B) except ZIP3 | CONFLICT: ZIP | - | - | - | - | Patient.address.postalCode | LOCATION | ADDRESS |
| 8 | ZIP3 (restricted 17) | (B) — must be "000" if ≤20,000 pop | CONFLICT: ZIP | - | - | - | - | - | - | - |
| 9 | PIN code (India) | - | CONFLICT: ZIP | Rule 14 (via address) | - | - | - | Patient.address.postalCode | LOCATION | ADDRESS |
| 10 | Birth date | (C) All elements of dates except year | CONFLICT: dates | Rule 14 (identifier if linking) | 2.3.1 | - | (0010,0030) | Patient.birthDate | DATE_TIME | DATE |
| 11 | Admission date | (C) | CONFLICT: dates | - | - | - | (0008,0020) | Encounter.period.start | DATE_TIME | DATE |
| 12 | Discharge date | (C) | CONFLICT: dates | - | - | - | (0008,0022) | Encounter.period.end | DATE_TIME | DATE |
| 13 | Date of death | (C) | CONFLICT: dates | - | - | - | (0040,A023) | Patient.deceasedDateTime | DATE_TIME | DATE |
| 14 | Age over 89 | (C) aggregate to "90+" | Art. 4(1) personal data | - | - | - | (0010,1010) | Patient.birthDate (computed) | - | AGE |
| 15 | Telephone number | (D) | Art. 4(1) personal data | Rule 14 (mobile number) | - | - | (0008,0094), (0010,2154) | Patient.telecom (phone) | PHONE_NUMBER | PHONE_OR_FAX |
| 16 | Fax number | (E) | Art. 4(1) personal data | - | - | - | (0008,0095) | Patient.telecom (fax) | (not distinct) | PHONE_OR_FAX |
| 17 | Pager number | (R) "any other" | Art. 4(1) personal data | - | - | - | - | Patient.telecom (pager) | - | PHONE_OR_FAX |
| 18 | Email address | (F) | Art. 4(1) personal data | Rule 14 (email address) | - | - | - | Patient.telecom (email) | EMAIL_ADDRESS | EMAIL |
| 19 | Social Security Number (US) | (G) | Art. 4(1) personal data | - | - | - | (0010,0020) context | Patient.identifier [SSN] | US_SSN | ID |
| 20 | Medical record number (MRN) | (H) | Art. 4(1) personal data | - | 2.3.5 | 5 (medical record) | (0010,0020) | Patient.identifier [MR] | (not covered) | ID |
| 21 | Health plan beneficiary number | (I) | Art. 4(1) personal data | - | - | - | - | Patient.identifier [NIIP] | US_MBI | ID |
| 22 | Account number | (J) | Art. 4(1) personal data | Rule 14 (customer ID) | - | 2 (financial) | (0010,2200) | Patient.identifier | US_BANK_NUMBER | ID |
| 23 | Credit/debit card | (R) | Art. 4(1) personal data | - | - | 2 (financial) | - | - | CREDIT_CARD | - |
| 24 | Bank account number | (R) | Art. 4(1) personal data | - | - | 2 (financial) | - | Patient.identifier | US_BANK_NUMBER, IBAN_CODE | - |
| 25 | Certificate/license number | (K) | Art. 4(1) personal data | Rule 14 (licence number) | - | - | - | Patient.identifier [DL] | US_DRIVER_LICENSE | ID |
| 26 | Medical license (provider) | (K) | Art. 4(1) personal data | Rule 14 | 4.5 | - | - | Practitioner.qualification | MEDICAL_LICENSE | ID |
| 27 | Driver's license | (K) | Art. 4(1) personal data | Rule 14 | - | - | - | Patient.identifier [DL] | US_DRIVER_LICENSE | ID |
| 28 | Passport number (US) | (K) | Art. 4(1) personal data | - | - | - | - | Patient.identifier [PPN] | US_PASSPORT | ID |
| 29 | Passport number (IN) | (K) | Art. 4(1) personal data | - | - | - | - | Patient.identifier [PPN] | IN_PASSPORT | ID |
| 30 | Vehicle identifier + plate | (L) | Art. 4(1) personal data | - | - | - | - | - | IN_VEHICLE_REGISTRATION, UK_VEHICLE_REGISTRATION, NG_VEHICLE_REGISTRATION | ID |
| 31 | VIN (17 char) | (L) | Art. 4(1) personal data | - | - | - | - | - | (not covered) | ID |
| 32 | Device identifier | (M) | Art. 4(1) personal data | - | - | - | (0018,1000), (0018,1002), (0018,1004) | Device.identifier | (not covered) | ID |
| 33 | Web URL | (N) | Art. 4(1) personal data | - | - | - | - | meta.source | URL | URL |
| 34 | IP address | (O) | Art. 4(1) personal data | - | - | - | - | - | IP_ADDRESS | (not covered) |
| 35 | MAC address | (R) | Art. 4(1) personal data | - | - | - | - | - | MAC_ADDRESS | - |
| 36 | Biometric identifier (fingerprint) | (P) | Art. 4(14) biometric data | - | - | 6 (biometric) | (0018,1148) | - | (not covered) | ID |
| 37 | Biometric identifier (voice print) | (P) | Art. 4(14) biometric data | - | - | 6 (biometric) | - | - | (not covered) | ID |
| 38 | Biometric identifier (retinal/iris) | (P) | Art. 4(14) biometric data | - | - | 6 (biometric) | - | - | (not covered) | ID |
| 39 | Biometric identifier (DNA) | (P) | Art. 4(13) genetic data | - | - | 6 (biometric) | - | - | (not covered) | ID |
| 40 | Full-face photograph | (Q) | Art. 9(1) special category | - | 2.3.3 | - | Burned-in pixel + (0008,1140) | Patient.photo, DocumentReference.content | (image-redactor) | (not covered) |
| 41 | Comparable full-body image | (Q) | Art. 9(1) special category | - | 2.3.3 | - | Burned-in pixel | Patient.photo | (image-redactor) | - |
| 42 | Any unique identifying code | (R) | Art. 9 conflict: pseudonymous | - | - | - | - | any id | (not covered) | ID |

### India-specific identifiers (DPDPA + SPDI + ICMR)

| # | Identifier category | Authority | Format | Coverage |
|---|---|---|---|---|
| 43 | Aadhaar number (12 digits) | UIDAI Act 2016 + DPDPA | 12 digit + Verhoeff checksum | IN_AADHAAR (Presidio), custom |
| 44 | PAN (Permanent Account Number) | Income Tax Act 1961 | 5 letters + 4 digits + 1 letter | IN_PAN (Presidio), custom |
| 45 | ABHA / ABHA Address | ABDM HDMP 2020 | 14 digit + checksum OR user@abdm | custom required |
| 46 | CTRI registration ID | ICMR 3.7 | CTRI/YYYY/MM/NNNNNN | custom required |
| 47 | Driver's license (state-specific) | Motor Vehicles Act 1988 | State code + DD + year + 7 digit | custom required per state |
| 48 | Voter ID (EPIC) | Representation of the People Act 1950 | 3 letters + 7 digits | IN_VOTER (Presidio) |
| 49 | Ration card (state-specific) | National Food Security Act 2013 | 29 state-specific formats | custom required per state |
| 50 | UAN (Universal Account Number, EPF) | EPF Act 1952 | 12 digit | custom required |
| 51 | ESI number (Employees' State Insurance) | ESI Act 1948 | 10 digit | custom required |
| 52 | CGHS beneficiary number | CGHS Rules 2014 | 7 digit | custom required |
| 53 | BPL card number | state-specific | varies | custom required |
| 54 | GSTIN (India Goods and Services Tax) | CGST Act 2017 | 15 char, state 01-37 + PAN + entity | IN_GSTIN (Presidio) |
| 55 | Vehicle registration (IN) | Motor Vehicles Act 1988 | State code + RTO + series + number | IN_VEHICLE_REGISTRATION (Presidio) |

### Quasi-identifiers and combination attacks (HIPAA 164.514(b)(2)(ii) "no actual knowledge")

| # | Quasi-identifier | Authority | Risk |
|---|---|---|---|
| 56 | Rare disease (ICD-10 code) | Sweeney 2002; 164.514(b)(2)(ii) | Re-identification via disease prevalence |
| 57 | Race/ethnicity | (not directly removed by Safe Harbor) | Combination with ZIP+DOB (Sweeney 2002) |
| 58 | Profession | (R) in HIPAA spirit; explicitly in AWS Comprehend PROFESSION | Sweeney found unique profession+ZIP |
| 59 | Marital status | quasi | combination |
| 60 | Institution name | - (not 18) | de facto quasi (DICOM E.3.11 optional retain) |
| 61 | Combination (DOB + gender + ZIP) | Sweeney 2002 | k-anonymity threshold |

### Permitted-context identifiers (HIPAA 164.514(f) fundraising)

| Identifier | Status in fundraising context |
|---|---|
| Demographic (name, address, contact, age, gender, DOB) | Permitted without authorization |
| Dates of healthcare provided | Permitted |
| Department of service | Permitted |
| Treating physician | Permitted |
| Outcome information | Permitted |
| Health insurance status | Permitted |

### Limited Data Set (HIPAA 164.514(e)) — 16 direct identifiers excluded

Retained in LDS: all dates, town/city, state, ZIP, age including >89.
Excluded from LDS: name, street address, phone, fax, email, SSN, MRN, health plan #, account #, cert/license, vehicle, device, URL, IP, biometric, full-face photo.

### Permitted contexts for re-identification codes (HIPAA 164.514(c))

Code must:
- Not be derived from or related to individual
- Not be translatable back to identity
- CE must not disclose mechanism

Permitted: hash-with-secret-salt, sequential numbering, randomly assigned IDs
Forbidden: hash of SSN, hash of DOB+name, published algorithm

### Additional jurisdiction coverage

This table extends the corpus beyond the US + India + EU core to national health and identity identifiers in additional jurisdictions. Each country-specific identifier is structurally separated in the corpus layer it belongs to, never mixed across jurisdictions. The China Resident Identity Card row is marked STRUCTURALLY SEPARATE: it is held in an isolated corpus partition and never co-mingled with other jurisdictions' records, consistent with PIPL cross-border transfer restrictions.

| # | Identifier | HIPAA | GDPR | DPDPA | Canada (PIPEDA/PHIPA) | UK (NHS) | Australia | Singapore | Japan | Brazil | China (PIPL) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | NHS Number (10-digit, UK) | (R) any unique code | Art. 4(1) personal data | - | - | NHS Number (Data Security and Protection Toolkit; modulus-11 checksum) | - | - | - | - | - |
| 2 | Social Insurance Number (SIN, Canada) | (R) | Art. 4(1) personal data | - | PIPEDA personal info; Luhn checksum | - | - | - | - | - | - |
| 3 | Ontario OHIP card number | (R) | Art. 4(1) personal data | - | PHIPA personal health info (10-digit + 2-letter version) | - | - | - | - | - | - |
| 4 | Alberta AHCIP number | (R) | Art. 4(1) personal data | - | PHIPA-equivalent (Alberta HIA); 9-digit | - | - | - | - | - | - |
| 5 | BC CareCard / Personal Health Number | (R) | Art. 4(1) personal data | - | BC E-Health/FIPPA; 10-digit PHN | - | - | - | - | - | - |
| 6 | AU Medicare number | (R) | Art. 4(1) personal data | - | - | - | Privacy Act 1988 + My Health Records Act; 10-digit + IRN | - | - | - | - |
| 7 | AU DVA (Dept Veterans' Affairs) file number | (R) | Art. 4(1) personal data | - | - | - | Privacy Act 1988; alpha-prefix + digits | - | - | - | - |
| 8 | AU My Health Record ID (IHI) | (R) | Art. 4(1) personal data | - | - | - | Healthcare Identifiers Act 2010; 16-digit IHI | - | - | - | - |
| 9 | Singapore NRIC / FIN | (R) | Art. 4(1) personal data | - | - | - | - | PDPA 2012; S/T/F/G/M prefix + 7 digits + checksum letter | - | - | - |
| 10 | Japan My Number (Individual Number) | (R) | Art. 4(1) personal data | - | - | - | - | - | APPI + My Number Act; 12-digit with check digit | - | - |
| 11 | Brazil CPF | (R) | Art. 4(1) personal data | - | - | - | - | - | - | LGPD; 11-digit (NNN.NNN.NNN-NN) with 2 check digits | - |
| 12 | Brazil RG (Registro Geral) | (R) | Art. 4(1) personal data | - | - | - | - | - | - | LGPD; state-issued, format varies | - |
| 13 | Brazil CNH (driver license) | (R) | Art. 4(1) personal data | - | - | - | - | - | - | LGPD; 11-digit registration number | - |
| 14 | China Resident Identity Card (18-digit) | (R) | Art. 4(1) personal data | - | - | - | - | - | - | - | PIPL sensitive personal information; 18-digit (GB 11643-1999) with check digit. STRUCTURALLY SEPARATE |

## Table B — Legal authority citation list

### United States

| Authority | Citation | Scope |
|---|---|---|
| HIPAA Privacy Rule | 45 CFR 164.500 - 164.534 | PHI protection, uses, disclosures |
| HIPAA Safe Harbor | 45 CFR 164.514(b)(2) | 18 identifier categories |
| HIPAA Expert Determination | 45 CFR 164.514(b)(1) | Statistical method |
| HIPAA Limited Data Set | 45 CFR 164.514(e) | 16-identifier LDS |
| HIPAA Re-ID codes | 45 CFR 164.514(c) | Permitted pseudonymization |
| OCR Guidance | HHS OCR 2012-11-26 | Interpretive guidance |
| HITECH Act | 42 USC 17921 et seq. | Breach notification |
| 2013 Omnibus Final Rule | 78 FR 5700 | GINA + HITECH implementation |
| Common Rule | 45 CFR 46 (2018 revision) | Human subjects research |

### India

| Authority | Citation | Scope |
|---|---|---|
| DPDPA 2023 | Act 22 of 2023 | Digital Personal Data Protection |
| DPDP Rules 2025 | G.S.R. 846(E), Gazette 2025-11-13 | Implementation rules |
| DPDP Rules Second Schedule | Rule 16 + Second Schedule | Research/archival/statistical exemption |
| DPDP Rule 6 | Security safeguards | 1-year log retention minimum |
| DPDP Rule 7 | Breach notification | 72-hour reporting |
| DPDP Rule 13(3) | Algorithmic due diligence | SDF AI/ML audits |
| DPDP Fourth Schedule Part A | Pediatric exemption | Clinical/educational |
| IT Act 2000 | Act 21 of 2000 | Foundational IT law |
| SPDI Rules 2011 | G.S.R. 313(E) 2011-04-11 | 8 categories of SPDI |
| Section 43A IT Act | Civil liability | Damages for negligence |
| Section 72A IT Act | Criminal liability | 3 years + ₹5L fine |
| ICMR 2017 | ISBN 978-81-910091-94 | National ethics guidelines |
| ICMR 1.1.5 | Privacy principle | Right to life supersedes privacy |
| ICMR 2.1 Table 2.1 | 4-tier risk categorization | EC review type |
| ICMR 2.3.5 | Coding/anonymization mandate | Research with stored samples |
| ICMR 3.3.2 | Data ownership | Institutions are custodians |
| ICMR 3.8.3 | HMSC approval | International collaboration |
| ICMR 4.8 Table 4.2 | Review types | Exempt/expedited/full |
| Puttaswamy v Union of India | (2017) 10 SCC 1 | Right to privacy as fundamental right |

### European Union (cross-reference)

| Authority | Citation | Relevance |
|---|---|---|
| GDPR | EU 2016/679 | Data protection regulation |
| GDPR Article 4(1) | Definition of personal data | Identifiability test |
| GDPR Recital 26 | Anonymous data | "No longer identifiable" |
| GDPR Recital 30 | Online identifiers | IP addresses as PII |
| GDPR Recital 75 | Risks to rights | Pseudonymization guidance |
| GDPR Article 9 | Special categories | Health data |
| GDPR Article 89 | Research exemption | Scientific/historical research |

### Standards and frameworks

| Authority | Citation | Scope |
|---|---|---|
| NIST SP 800-188 | Garfinkel 2015 (reissued) | De-identifying government data |
| NIST IR 8053 | Garfinkel 2015 | De-identification of PII |
| ISO/IEC 27559:2022 | ISO | Privacy-enhancing de-identification framework |
| ISO/IEC 27001:2022 | ISO | Information security management |
| HITRUST CSF v11 | HITRUST | Healthcare security framework |
| OWASP LLM Top 10 2025 | OWASP | LLM application security |
| MITRE ATLAS | MITRE | Adversarial threat landscape |
| NIST AI RMF 1.0 | NIST | AI risk management |
| NIST AI 600-1 | NIST | Generative AI Profile |
| IHE De-Identification Handbook | IHE | Imaging de-identification |
| DICOM PS3.15 Annex E | NEMA | Basic Confidentiality Profile |
| HL7 FHIR R4 | HL7 | Patient resource specification |

### Peer-reviewed research

| Authority | Citation | Relevance |
|---|---|---|
| Sweeney 2002 | k-anonymity: a model for protecting privacy | Int J Uncertainty, Fuzziness, Knowledge-based Systems 10(5):557-570 |
| El Emam 2015 | Concepts and methods for de-identifying | IOM chapter |
| Vanderbilt TIME study | Atreya et al. 2013 | Temporal shifting |
| Meystre 2010 | Automatic de-identification of textual EHR | BMC Medical Research Methodology |
| i2b2 2014 | Stubbs et al. 2015 | Annotation guidelines |

## Table C — Benchmark tool coverage matrix

| Identifier (from Table A) | Presidio | AWS CM | Azure Health | John Snow Labs | Our corpus |
|---|---|---|---|---|---|
| Names | ✓ | ✓ | ✓ | ✓ | ✓ |
| Addresses | ✓ | ✓ | ✓ | ✓ | ✓ |
| Dates | ✓ | ✓ | ✓ | ✓ | ✓ |
| Age > 89 rule | ✗ | ✗ | partial | partial | ✓ |
| Phone | ✓ | ✓ | ✓ | ✓ | ✓ |
| Fax (distinct) | ✗ | ✗ | ✗ | partial | ✓ |
| Email | ✓ | ✓ | ✓ | ✓ | ✓ |
| SSN | ✓ (US_SSN) | ✓ (ID) | ✓ | ✓ | ✓ |
| MRN | ✗ | ✓ (ID) | ✓ | ✓ | ✓ |
| Account number | partial | ✓ (ID) | ✓ | ✓ | ✓ |
| License | partial | ✓ (ID) | ✓ | ✓ | ✓ |
| Vehicle identifier | partial | ✓ (ID) | partial | partial | ✓ |
| Device identifier | ✗ | ✓ (ID) | partial | partial | ✓ |
| URL | ✓ | ✓ | ✓ | ✓ | ✓ |
| IP address | ✓ | ✗ | ✓ | ✓ | ✓ |
| Biometric | ✗ | ✓ (ID) | ✗ | ✗ | ✓ |
| Full-face photo (image redactor) | partial | ✗ | ✗ | partial | ✓ |
| Profession | ✗ | ✓ | partial | ✓ | ✓ |
| Aadhaar (IN) | ✓ (IN_AADHAAR) | ✗ | ✗ | ✗ | ✓ |
| PAN (IN) | ✓ (IN_PAN) | ✗ | ✗ | ✗ | ✓ |
| ABHA (IN) | ✗ | ✗ | ✗ | ✗ | ✓ |
| CTRI ID (IN) | ✗ | ✗ | ✗ | ✗ | ✓ |
| UAN, ESI, CGHS, BPL (IN) | ✗ | ✗ | ✗ | ✗ | ✓ |
| Ration card state-specific | ✗ | ✗ | ✗ | ✗ | ✓ |
| Household member PHI | ✗ | ✗ | ✗ | ✗ | ✓ |
| Quasi-identifier combos | ✗ | ✗ | ✗ | partial | ✓ |
| Limited Data Set tier | ✗ | ✗ | ✗ | ✗ | ✓ |
| Re-ID code derivation check | ✗ | ✗ | ✗ | ✗ | ✓ |
| DPDPA Second Schedule | ✗ | ✗ | ✗ | ✗ | ✓ |
| Fundraising context | ✗ | ✗ | ✗ | ✗ | ✓ |

## Table D — File format coverage

| Format | Authority | Our coverage | Gap in major tools |
|---|---|---|---|
| JSONL | JSON RFC 8259 | ✓ | — |
| JSON | JSON RFC 8259 | ✓ | — |
| CSV | RFC 4180 | Planned | dirty-CSV edge cases |
| TSV | — | Planned | — |
| Excel XLSX | OOXML ECMA-376 | Planned | authors/sheet metadata often missed |
| DOCX | OOXML ECMA-376 | Planned | track changes, comments, author |
| PDF (text) | ISO 32000-1 | Planned | form fields, metadata, author |
| PDF (form) | ISO 32000-1 | Planned | XFDF leakage |
| Plain text (.txt) | — | ✓ | — |
| HTML | W3C HTML 5 | Planned | hidden elements, meta tags |
| Markdown | CommonMark | Planned | HTML embeds |
| Email (.eml) | RFC 5322 | Planned | headers, From/To, References |
| XML | W3C XML 1.1 | Planned | namespaces, processing instructions |
| HL7 v2 | HL7 v2.x | Planned | PID, NK1, IN1 segments |
| HL7 FHIR R4 JSON | HL7 FHIR | ✓ | — |
| HL7 FHIR R4 XML | HL7 FHIR | Planned | — |
| HL7 CDA | HL7 CDA R2 | Planned | narrative text PHI |
| DICOM (.dcm header) | DICOM PS3.10 | Planned | private tags |
| DICOM (.dcm with pixel) | DICOM PS3.15 E.3.1 | Planned | burned-in pixel PHI |
| Parquet | Apache Parquet | Planned | sensitive column names |
| Arrow (.arrow) | Apache Arrow | Planned | schema leakage |
| Image EXIF (JPEG/TIFF) | Exif 2.32 | Planned | GPS, artist, comment fields |
| Image PNG (tEXt/iTXt) | PNG spec | Planned | text chunks |
| ZIP archive | PKWARE APPNOTE | Planned | file names can leak |
| SQLite database | SQLite file format | Planned | row-level PHI |

## Table E — Attack surface matrix (OWASP LLM Top 10 + MITRE ATLAS)

| Attack | Source | Our corpus layer | Test density |
|---|---|---|---|
| LLM01: Prompt Injection | OWASP LLM Top 10 2025 | injection layer | 100 test cases |
| LLM02: Sensitive Info Disclosure | OWASP | all layers | full corpus |
| LLM03: Supply Chain | OWASP | meta-layer | — |
| LLM04: Data Poisoning | OWASP | injection layer | 100 cases |
| LLM05: Improper Output Handling | OWASP | verification layer | — |
| LLM06: Excessive Agency | OWASP | RBAC layer | — |
| LLM07: System Prompt Leakage | OWASP | envelope layer | — |
| LLM08: Vector/Embedding | OWASP | MIA layer | shadow model |
| LLM09: Misinformation | OWASP | epistemic layer | — |
| LLM10: Unbounded Consumption | OWASP | throughput layer | — |
| Membership Inference | Nature Sci Rep 2024 | MIA layer | 6 shadow scenarios |
| k-anonymity violation | Sweeney 2002 | quasi-id layer | 50 combos |
| Re-identification codes | 164.514(c) | pseudonym layer | 20 cases |

## Table F — Detection regime taxonomy

This table categorizes each identifier by the detection regime required. Corpus records include a detection_regime field with one of two values: rule_applicable (regex/pattern matching sufficient) or contextual_ner_required (requires transformer NER to detect from context). This split follows the i2b2 taxonomy confirmed in arXiv 2412.10918.

| # | Identifier type | Detection regime | Rationale |
|---|---|---|---|
| 1 | ACCOUNT (account numbers) | rule_applicable | Fixed-length numeric patterns |
| 2 | DLN (driver license number) | rule_applicable | State-format patterns |
| 3 | EMAIL | rule_applicable | RFC 822 pattern |
| 4 | FAX | rule_applicable | Phone-format pattern + context label |
| 5 | IP (IPv4, IPv6) | rule_applicable | CIDR pattern |
| 6 | LICENSE (medical/professional) | rule_applicable | Pattern + NPI checksum |
| 7 | PLATE (license plates) | rule_applicable | State-format patterns |
| 8 | SSN | rule_applicable | NNN-NN-NNNN pattern |
| 9 | URL | rule_applicable | RFC 3986 pattern |
| 10 | VIN | rule_applicable | 17-char ISO 3779 pattern |
| 11 | AGE | contextual_ner_required | "age 45" vs other numbers requires context |
| 12 | CITY | contextual_ner_required | City names overlap with common words |
| 13 | COUNTRY | contextual_ner_required | NER required |
| 14 | DATE | contextual_ner_required | Date formats vary; context required to distinguish PHI dates from non-PHI |
| 15 | DEVICE | contextual_ner_required | UDI format varies; clinical context required |
| 16 | DOCTOR | contextual_ner_required | Provider names require NER |
| 17 | HOSPITAL | contextual_ner_required | Organization names require NER |
| 18 | IDNUM | contextual_ner_required | Generic numeric IDs require context to classify |
| 19 | LOCATION-OTHER | contextual_ner_required | NER required |
| 20 | MEDICAL RECORD | contextual_ner_required | MRN formats vary; clinical context required |
| 21 | ORGANIZATION | contextual_ner_required | NER required |
| 22 | PATIENT | contextual_ner_required | Patient names require NER |
| 23 | PHONE | contextual_ner_required | Phone numbers require context (phone vs other numeric) |
| 24 | PROFESSION | contextual_ner_required | Profession descriptions are free text |
| 25 | STATE | contextual_ner_required | State names/abbreviations require context |
| 26 | STREET | contextual_ner_required | Street addresses require NER to distinguish from context |
| 27 | USERNAME | contextual_ner_required | Username context varies |
| 28 | ZIP | contextual_ner_required + CONFLICT | ZIP is PHI under HIPAA; contextual NER required; CONFLICT with GDPR |

Conflict cases: ZIP codes (row 28) are PHI under HIPAA Safe Harbor (B) but are not enumerated as personal data under GDPR in isolation without additional linkage. Corpus records in layer 'conflict_cases' tag these records with detection_regime: conflict_case to distinguish them from pure rule_applicable or contextual_ner_required cases.

## How this matrix is used

1. **IRB reviewers** should start here. Every claim about corpus coverage maps to exactly one authority and one (or more) test cases.
2. **Counsel review** should check Table B against applicable jurisdiction.
3. **Security review** should check Table E against current threat model.
4. **Engineering review** should check Tables A+C+D to verify detector coverage matches the claimed taxonomy.
5. **Reproducibility** is guaranteed if every test case includes the authority citation field and every detector emits the authority citation in its result.

## Update procedure

This matrix is updated when:
- A new authority is ratified (add to Table B)
- A new identifier category is added (add to Table A with citation)
- A new benchmark tool is integrated (add column to Table C)
- A new file format generator is added (add to Table D)
- A new attack vector is documented (add to Table E)

Every update requires a version bump, a CHANGELOG entry, and a MANIFEST hash update.
