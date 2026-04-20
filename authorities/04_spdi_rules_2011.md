# IT Act SPDI Rules 2011 — Research Note

**Full title:** Information Technology (Reasonable Security Practices and Procedures and Sensitive Personal Data or Information) Rules, 2011
**Source:** G.S.R. 313(E), published 11 April 2011 by Ministry of Communications and IT, Department of Information Technology
**Authority:** Section 87(2)(ob) read with Section 43A of IT Act 2000
**Sources consulted:**
- https://indiankanoon.org/doc/101774797/ (Rule 3 full text)
- https://indiankanoon.org/doc/114407484/ (full rules context)
- https://cis-india.org/internet-governance/files/it-reasonable-security-practices-and-procedures-and-sensitive-personal-data-or-information-rules-2011.pdf

## Status under DPDPA

SPDI Rules 2011 remain in force alongside DPDPA 2023 until the DPDP Rules 2025 take full effect (2027-05-13 for most rules). They currently apply to "body corporate or any person located within India" per Press Note 2011-08-24. Post-DPDPA full commencement, SPDI Rules are expected to be superseded but not yet formally repealed as of April 2026.

## Rule 3 — Definition of Sensitive Personal Data or Information (SPDI)

Sensitive personal data/information consists of information relating to:

1. **Password**
2. **Financial information** such as bank account, credit card, debit card, or other payment instrument details
3. **Physical, physiological and mental health condition**
4. **Sexual orientation**
5. **Medical records and history**
6. **Biometric information** (defined at Rule 2(b): technologies measuring human body characteristics such as fingerprints, eye retinas and irises, voice patterns, facial patterns, hand measurements, DNA for authentication)
7. **Any detail relating to the above clauses** as provided to body corporate for service
8. **Any information received under above clauses** by body corporate for processing, stored or processed under lawful contract

**Exception:** Information freely available in public domain or furnished under Right to Information Act 2005 is NOT sensitive.

**Key comparison with DPDPA:** DPDPA 2023 does NOT categorize "sensitive" vs. "ordinary" personal data — all personal data is protected at same baseline, with heightened rules for children/SDFs. However, for legacy applications still under SPDI, these 8 categories trigger the compliance regime.

## Rule 4 — Body Corporate Must Publish Privacy Policy

Must include:
- Clear statement of practices and policies
- Type of personal/sensitive information collected
- Purpose of collection and usage
- Disclosure practices (including to third parties)
- Reasonable security practices

## Rule 5 — Collection of Information

- (1) Consent in writing (including electronic) before SPDI collection
- (2) Collection only for lawful purpose connected with body corporate function, and necessary
- (3) Person must know: fact of collection, purpose, intended recipients, collecting/retaining agency
- (4) Retention only as long as required for lawful purpose
- (5) Use only for purpose collected for
- (6) Data subject right to access and review information
- (7) Correction right
- (8) Opt-out mechanism
- (9) Grievance officer designation mandatory

## Rule 6 — Disclosure

- Consent required for third-party disclosure EXCEPT:
  - Government agency request for identity verification, prevention/detection/investigation of offences, cyber incidents, prosecution, punishment
  - Required by law
- Body corporate cannot publish SPDI
- Third-party recipient cannot disclose further

## Rule 7 — Transfer of Information

- Transfer to person in India or any other country IF that country ensures same level of protection
- Transfer permitted only if necessary for lawful contract performance OR if person has consented

## Rule 8 — Reasonable Security Practices

- Body corporate must have comprehensively documented information security program
- Information security policies with managerial/technical/operational/physical measures proportionate to the information assets
- Compliance with international standard IS/ISO/IEC 27001 deemed sufficient
- Audit of policies at least once a year, or when significant upgrade

## Section 43A of IT Act 2000 (the enabling statute)

> "Where a body corporate, possessing, dealing or handling any sensitive personal data or information in a computer resource which it owns, controls or operates, is negligent in implementing and maintaining reasonable security practices and procedures and thereby causes wrongful loss or wrongful gain to any person, such body corporate shall be liable to pay damages by way of compensation to the person so affected."

**No cap on damages** in original statute. This is the statutory liability basis for Indian data breach cases.

## Section 72A of IT Act 2000

Personal information disclosure without consent (or in breach of contract) with intent to cause wrongful loss/gain is a criminal offense (up to 3 years imprisonment + fine up to ₹5 lakh). Intent to cause wrongful loss/gain is sufficient; actual loss need not result.

## Actions for corpus

1. **Add SPDI-category fixtures** — test cases tagged with Rule 3 category (password, financial, health, sexual orientation, medical record, biometric)
2. **Add SPDI exemption edge cases** — information claimed to be "public domain" or "RTI-furnished" (Rule 3 proviso), verify envelope classifies correctly
3. **Add Section 43A breach scenarios** — records demonstrating negligent handling (unencrypted storage, shared credentials, lack of access controls)
4. **Add cross-border transfer test cases** — Rule 7 scenarios where destination country adequacy must be verified
5. **Document that current RePORTaLiN-RAG operates under BOTH SPDI (2011) and DPDPA (2023)** until phased DPDPA commencement completes 2027-05-13
