# DPDP Rules 2025 — Full Text Analysis

**Source:** https://www.meity.gov.in/static/uploads/2025/11/53450e6e5dc0bfa85ebd78686cadad39.pdf
**Citation authority:** G.S.R. 846(E), Gazette of India, Extraordinary, Part II Section 3(i), notified 13 November 2025
**Made under:** Section 40(1) and 40(2) of the Digital Personal Data Protection Act, 2023 (22 of 2023)

## Phased commencement (Rule 1)

- Rules 1, 2, 17-21: **immediate** (from publication 2025-11-13)
- Rule 4 (Consent Manager): **2026-11-13** (12 months)
- Rules 3, 5-16, 22-23: **2027-05-13** (18 months)

This pins the corpus validity window.

## Rule 6 — Reasonable Security Safeguards (critical for RePORTaLiN)

The Data Fiduciary must implement, at minimum:

- **(a)** Appropriate data security measures: **encryption, obfuscation, masking, or virtual tokens** mapped to that personal data
- **(b)** Access controls for computer resources used by the Data Fiduciary or Data Processor
- **(c)** Log-based visibility: audit logs, monitoring, review for detection of unauthorized access, investigation, prevention of recurrence
- **(d)** Backup measures for continued processing if integrity/availability is compromised
- **(e)** **Log and personal data retention for minimum 1 year** (unless other law requires longer)
- **(f)** Contractual safeguards with Data Processors
- **(g)** Technical and organizational measures for effective observance

**Impact on phi-handler envelope:** The envelope's encrypted_logging.py module directly satisfies (c) and (e). Nuclear_clean.py satisfies portions of (d) via quarantine. The 1-year retention floor pins the envelope's log retention policy.

## Rule 7 — Breach Notification

### To Data Principal (without delay)
Include:
- (a) description of the breach (nature, extent, time)
- (b) consequences relevant to the Data Principal
- (c) mitigation measures implemented/being implemented
- (d) safety measures the Data Principal may take
- (e) business contact information of a person able to respond

### To Board
- **Without delay:** description (nature, extent, time, place, likely impact)
- **Within 72 hours** (or extended by Board): updated details including:
  - (i) Updated and detailed information about the breach
  - (ii) Broad facts, events, circumstances, causes
  - (iii) Mitigation measures implemented or proposed
  - (iv) Findings regarding the person who caused the breach
  - (v) Remedial measures to prevent recurrence
  - (vi) Report on notifications provided to affected Data Principals

**This is the binding constraint over HIPAA's 60-day window.**

## Rule 10 — Verifiable Parental Consent (child data)

"Child" = under 18. Data Fiduciary must verify parent is adult via:
- (a) Reliable identity/age details available to Data Fiduciary
- (b) Identity/age voluntarily provided:
  - (i) by the person, or
  - (ii) via virtual token from authorized entity (DigiLocker)

"Authorized entity" means entity legally tasked with issuing identity/age details or age-mapped virtual tokens, including entities like DigiLocker service providers notified under the IT Act 2000.

## Rule 11 — Verifiable Guardian Consent (persons with disabilities)

For persons with disabilities who have a lawful guardian (defined under Rights of Persons with Disabilities Act 2016 and National Trust for Welfare of Persons with Autism/Cerebral Palsy/Mental Retardation/Multiple Disabilities Act 1999).

Data Fiduciary must verify guardian appointment is by:
- Court, or
- Designated authority (RPWDA Section 15), or
- Local level committee (NTWPA Section 13)

## Rule 13 — Significant Data Fiduciary additional obligations

- **(1) Annual DPIA and audit** — within 12 months of designation
- **(2) Report to Board** with significant observations
- **(3) Algorithmic software due diligence** — must ensure hosting/display/upload/modification/publishing/transmission/storage/updating/sharing algorithms do not pose risk to Data Principal rights
- **(4) Localization** — personal data (and traffic data of its flow) designated by Central Government shall not be transferred outside India

**Impact on AI/ML systems:** Rule 13(3) is the exact regulatory hook for requiring **algorithmic audits on RAG systems, LLM pipelines, and any ML model operating on personal data.**

## Rule 14 — Data Principal Rights; "identifier" definition

Identifier = any sequence of characters issued by Data Fiduciary to identify the Data Principal, including:
- customer identification file number
- customer acquisition form number
- application reference number
- enrolment ID
- email address
- mobile number
- licence number

**Impact on corpus:** the "UNIQUE_OTHER" / "CERT_LICENSE" categories should be tagged with this DPDPA-specific identifier vocabulary.

## Rule 16 — Research/Archiving/Statistical Exemption (CRITICAL for RePORTaLiN)

> "The provisions of the Act shall not apply to the processing of personal data necessary for research, archival or statistical purposes if it is carried out in accordance with the standards specified in the Second Schedule."

This is the **direct legal authority** for RePORTaLiN-RAG's operation under DPDPA.

## Second Schedule — Research Exemption Standards

Personal data processing for research/archival/statistical purposes must comply with:

- **(a)** Processing carried out lawfully
- **(b)** Processing is for uses specified in Section 7(b) of the Act OR purposes specified in Section 17(2)(b) [research exemption]
- **(c)** Processing **limited to personal data necessary** for the use/purpose (minimization principle)
- **(d)** Reasonable efforts to ensure **completeness, accuracy, and consistency** of personal data
- **(e)** Retention **only as long as necessary** for the use/purpose, or as required by law
- **(f)** Reasonable security safeguards for preventing breach (per Rule 6 specifications)
- **(g)** [If Section 7(b)] Notice to Data Principal with:
  - (i) business contact info of person who can answer processing questions
  - (ii) special communication link for accessing Data Fiduciary website/app and means to exercise rights under the Act
  - (iii) other standards under Central Government policy or applicable law
- **(h)** **Accountability** of the person determining purpose and means of processing

**The eight-condition Second Schedule is the compliance envelope for RePORTaLiN-RAG.** Every condition maps to a phi-handler module:

| Second Schedule condition | phi-handler module |
|---|---|
| (a) Lawful processing | consent_gate.py + phi_gate.py |
| (c) Minimization | phi_rules.py + phi_sanitizer.py + statistical_gate.py |
| (d) Completeness/accuracy | clinical_dates.py + validation_checks |
| (e) Retention | encrypted_logging.py retention policy |
| (f) Security | full phi-handler stack |
| (g) Notice/transparency | audit_report.py + data_principal_contact |
| (h) Accountability | RBAC + zone_guard + audit trail |

## Third Schedule — Retention periods for high-volume Data Fiduciaries

Applies to e-commerce entities (>=2 crore users), online gaming intermediaries (>=50 lakh users), social media intermediaries (>=2 crore users). Retain **3 years** from last Data Principal contact or rule commencement (whichever later), except for user account access and virtual token access purposes.

## Fourth Schedule — Child Data Processing Exemptions

### Part A — Exempt classes of Data Fiduciary

| Class | Condition |
|---|---|
| Clinical establishment, mental health establishment, healthcare professional | Limited to providing health services to a child entrusted to the establishment/professional, necessary for protection of health |
| Allied healthcare professional | Limited to supporting implementation of healthcare treatment and referral plan recommended by such professional for child |
| Educational institution | Tracking and behavioral monitoring limited to (a) educational activities of institution OR (b) safety of child enrolled |
| Creche or child day care | Tracking/behavioral monitoring limited to safety of child entrusted |
| Transport provider (educational/daycare-appointed) | Location tracking limited to in-transit between institution/center, for safety |

### Part B — Exempt purposes

| Purpose | Condition |
|---|---|
| Exercise of power/performance of function under any law for child's benefit | Limited to necessary extent |
| Provision/issuance of subsidy/benefit under Section 7(b) | Limited to necessary extent |
| Creating user account for email communication | Limited to such accounts with email-only use |
| Real-time location of child | Limited to location tracking for child's safety and protection |
| Ensuring harmful information/services/advertisements do not reach child | Limited to such filtering extent |
| Confirming Data Principal is not a child (Rule 10 due diligence) | Limited to confirmation/due diligence |

**Impact on corpus:** pediatric clinical trial data (common in RePORTaLiN-RAG) falls under Part A item 1 exemption. The corpus should include pediatric test cases that exercise this exemption boundary.

## Rule 15 — Cross-border Transfer

Cross-border transfer permitted, subject to:
- Central Government may specify requirements (general or special order) for transfer to any foreign state, or to a person/entity/agency controlled by such state

**Critical nuance:** This is a **blacklist approach** (permitted by default, can be restricted to specific countries). As of April 2026, no country is blacklisted. Agreement requirements are still fluid.

## Seventh Schedule — Purposes for which Government may demand information

| Purpose | Authorized person |
|---|---|
| Sovereignty/integrity/security | Central/instrumentality officer designated |
| Performance of function under law; disclosure required by law | Person authorized under applicable law |
| Assessment for SDF designation | MeitY officer designated by Secretary MeitY |

## First Schedule Part A — Consent Manager registration conditions

- Incorporated Indian company
- Adequate technical/operational/financial capability
- Net worth >= ₹2 crore
- Directors/KMP/senior management of sound repute
- Must observe operational fiduciary obligations
- Independent certification that the consent platform meets Board data protection standards

## First Schedule Part B — Consent Manager obligations

- Enable Data Principals to give/manage/review/withdraw consent
- Cannot read personal data content being shared (**end-to-end encrypted forwarding**)
- Record consents given/denied/withdrawn, notices, and transfers
- Retain records **≥7 years** (or longer if agreed or required)
- Develop website/app as primary access
- No subcontracting of obligations
- Reasonable security safeguards against breach
- Act in fiduciary capacity toward Data Principal
- Avoid conflicts of interest with Data Fiduciaries
- Publish transparency info: promoters/directors/KMP/senior management, shareholders with >2% stake, corporate shareholders
- Establish effective audit mechanisms

**Implication:** If RePORTaLiN integrates with a Consent Manager, the token-based encrypted consent forwarding pattern is mandatory; the consent content cannot be readable by the Manager itself.

## Actions for corpus

1. **Add Second Schedule compliance fixtures** — Layer 12 — eight-condition compliance audit scenarios
2. **Add pediatric exemption boundary cases** — Fourth Schedule Part A item 1 (clinical data)
3. **Add algorithmic due diligence test cases** — Rule 13(3) scenarios where a detector's algorithm must be auditable
4. **Add breach notification timing fixtures** — 72-hour window test cases
5. **Add Consent Manager token-forwarding test cases** — content unreadable by Manager
6. **Document the identifier list from Rule 14** — customer ID file number, acquisition form, application reference, enrolment ID, licence number
