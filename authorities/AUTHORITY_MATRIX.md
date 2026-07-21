# Consolidated Authority Matrix

> **Scope: USA/HIPAA ONLY.**

**Document version:** 3.0
**Maintainer:** See LICENSE

This matrix is the USA/HIPAA authority source of truth for `phi_engine`.

## Table A — Identifier categories mapped to primary authorities

### Direct identifiers (must remove under US Safe Harbor)

| # | Identifier category | HIPAA 164.514(b)(2)(i) | DICOM PS3.15 | FHIR R4 Patient | Presidio | AWS Comprehend |
|---|---|---|---|---|---|---|
| 1 | Patient name | (A) Names | (0010,0010), (0010,1001), (0010,1005) | Patient.name | PERSON | NAME |
| 2 | Provider/physician name | Outside strict Safe Harbor (A) scope -- (b)(2)(i)'s scope note (line below Table A) limits (A) to "the individual or of relatives, employers, or household members of the individual"; provider names are a separate re-identification-risk concern, not a cited Safe Harbor category | (0008,0090), (0008,1048), (0008,1050), (0008,1060), (0008,1070) | Practitioner.name | PERSON | NAME |
| 3 | Household/relative name | (A) Names (explicit household scope) | - | Patient.contact.name, RelatedPerson.name | PERSON | NAME |
| 4 | Street address (full) | (B) Geographic subdivisions smaller than State | (0010,1040) | Patient.address.line | LOCATION | ADDRESS |
| 5 | City | (B) ... except ZIP3 permitted | (0010,1040) | Patient.address.city | LOCATION | ADDRESS |
| 6 | State (US) | Permitted under (B) | - | Patient.address.state | LOCATION | ADDRESS |
| 7 | ZIP code (full 5/9 digit) | (B) except ZIP3 | - | Patient.address.postalCode | LOCATION | ADDRESS |
| 8 | ZIP3 (restricted 17) | (B) — must be "000" if ≤20,000 pop | - | - | - | - |
| 10 | Birth date | (C) All elements of dates except year | (0010,0030) | Patient.birthDate | DATE_TIME | DATE |
| 11 | Admission date | (C) | (0008,0020) | Encounter.period.start | DATE_TIME | DATE |
| 12 | Discharge date | (C) | (0008,0022) | Encounter.period.end | DATE_TIME | DATE |
| 13 | Date of death | (C) | (0040,A023) | Patient.deceasedDateTime | DATE_TIME | DATE |
| 14 | Age over 89 | (C) aggregate to "90+" | (0010,1010) | Patient.birthDate (computed) | - | AGE |
| 15 | Telephone number | (D) | (0008,0094), (0010,2154) | Patient.telecom (phone) | PHONE_NUMBER | PHONE_OR_FAX |
| 16 | Fax number | (E) | (0008,0095) | Patient.telecom (fax) | (not distinct) | PHONE_OR_FAX |
| 17 | Pager number | (R) "any other" | - | Patient.telecom (pager) | - | PHONE_OR_FAX |
| 18 | Email address | (F) | - | Patient.telecom (email) | EMAIL_ADDRESS | EMAIL |
| 19 | Social Security Number (US) | (G) | (0010,0020) context | Patient.identifier [SSN] | US_SSN | ID |
| 20 | Medical record number (MRN) | (H) | (0010,0020) | Patient.identifier [MR] | (not covered) | ID |
| 21 | Health plan beneficiary number | (I) | - | Patient.identifier [NIIP] | US_MBI | ID |
| 22 | Account number | (J) | (0010,2200) | Patient.identifier | US_BANK_NUMBER | ID |
| 23 | Credit/debit card | (R) | - | - | CREDIT_CARD | - |
| 24 | Bank account number | (R) | - | Patient.identifier | US_BANK_NUMBER, IBAN_CODE | - |
| 25 | Certificate/license number | (K) | - | Patient.identifier [DL] | US_DRIVER_LICENSE | ID |
| 26 | Medical license (provider) | (K) | - | Practitioner.qualification | MEDICAL_LICENSE | ID |
| 27 | Driver's license | (K) | - | Patient.identifier [DL] | US_DRIVER_LICENSE | ID |
| 28 | Passport number (US) | (K) | - | Patient.identifier [PPN] | US_PASSPORT | ID |
| 31 | VIN (17 char) | (L) | - | - | (not covered) | ID |
| 32 | Device identifier | (M) | (0018,1000), (0018,1002), (0018,1004) | Device.identifier | (not covered) | ID |
| 33 | Web URL | (N) | - | meta.source | URL | URL |
| 34 | IP address | (O) | - | - | IP_ADDRESS | (not covered) |
| 35 | MAC address | (R) | - | - | MAC_ADDRESS | - |
| 36 | Biometric identifier (fingerprint) | (P) | (0018,1148) | - | (not covered) | ID |
| 37 | Biometric identifier (voice print) | (P) | - | - | (not covered) | ID |
| 38 | Biometric identifier (retinal/iris) | (P) | - | - | (not covered) | ID |
| 39 | Biometric identifier (DNA) | (P) | - | - | (not covered) | ID |
| 40 | Full-face photograph | (Q) | Burned-in pixel + (0008,1140) | Patient.photo, DocumentReference.content | (image-redactor) | (not covered) |
| 41 | Comparable full-body image | (Q) | Burned-in pixel | Patient.photo | (image-redactor) | - |
| 42 | Any unique identifying code | (R) | - | any id | (not covered) | ID |

### Quasi-identifiers and combination attacks (HIPAA 164.514(b)(2)(ii) "no actual knowledge")

| # | Quasi-identifier | Authority | Risk |
|---|---|---|---|
| 56 | Rare disease (ICD-10 code) | Sweeney 2002; 164.514(b)(2)(ii) | Re-identification via disease prevalence |
| 57 | Race/ethnicity | (not directly removed by Safe Harbor) | Combination with ZIP+DOB (Sweeney 2002) |
| 58 | Profession | Not a Safe Harbor category; quasi-identifier per Sweeney 2002; AWS Comprehend has an explicit PROFESSION entity type | Sweeney found unique profession+ZIP |
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

### Limited Data Set (HIPAA 164.514(e)) — 16 direct identifiers excluded, still PHI

May be retained in LDS (not excluded): all dates, town/city, state, ZIP, age including >89.
Excluded from LDS (must be removed): name, street address, phone, fax, email, SSN, MRN, health plan #, account #, cert/license, vehicle, device, URL, IP, biometric, full-face photo.
LDS remains PHI subject to the Privacy Rule; it is not de-identified information (reg-0006). Not currently available in this checkout -- see Table C.

### Permitted contexts for re-identification codes (HIPAA 164.514(c))

Code must:
- Not be derived from or related to individual
- Not be translatable back to identity
- CE must not disclose mechanism

Permitted under (c): a code that is truly independent of the individual
(sequential numbering, randomly assigned IDs unrelated to the patient).
Properly salted cryptographic hashes are permitted for pseudonymization
only as part of Expert Determination (b)(1), per NIST IR 8053 -- not
automatically under (c) merely by using a secret key.
Forbidden: hash of the individual's own identifier presented as a (c) code
without Expert Determination (e.g. `phi_engine`'s current HMAC-pseudonymize
action, which hashes `label:raw_id` -- see Table C), published algorithm.

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

## Table C — phi_engine classification/detector/action map

`phi_engine`'s own classification/detection/action surface, per identifier
category from Table A. This table intentionally does NOT compare against
Presidio/AWS/Azure/JSL: this repository has not independently, currently
re-benchmarked those tools' entity coverage against this taxonomy, and an
uncited checkmark table is not evidence. See
`docs/PRIVACY_GATEWAY_RESEARCH.md` for the surviving adversarial-fixture
exercise and source-traced findings this repository retains (structural
gaps confirmed by direct code reading, not a comparative benchmark -- see
`docs/PRIVACY_GATEWAY_STRESS_TEST.md`).

| Identifier (from Table A) | Classification path | Residual detector (`phi_patterns.py`) | Applied action / control | Known limitation |
|---|---|---|---|---|
| Names | header-driven (`phi_review.py` pinned rules) | `PERSON_NAME_PREFIX`/`PERSON_NAME_GENERIC` (warn-tier only, not blocking) | force-drop / suppress per classification | No blocking-tier free-text name detector; warn-tier patterns are audit-only |
| Addresses | header-driven | `ADDRESS` (blocking) | drop / generalize | Free-text address embedded in an unrelated field is not scanned by header classification |
| Dates | header-driven | `DATE_ISO`/`DATE_TEXT` (blocking), `DATE_MDY` (warn) | SANT date-jitter / cap (age>89) | n/a |
| Age > 89 rule | header-driven | `AGE_OVER_89` (blocking) | cap | n/a |
| Phone | header-driven | `US_PHONE` (blocking) | drop / suppress | n/a |
| Fax (distinct from phone) | header-driven only | none (no fax-distinct pattern) | drop / suppress | Free-text fax numbers are indistinguishable from phone in `phi_patterns.py` |
| Email | header-driven | `EMAIL` (blocking) | drop / suppress | n/a |
| SSN | header-driven | `SSN`/`SSN_UNHYPHENATED` (blocking) | drop / suppress | n/a |
| MRN | header-driven | `MRN`/`MRN_LABELED` (blocking) | drop / suppress | n/a |
| Account / license / vehicle / device identifier | header-driven only | none | drop / suppress per classification | No free-text/regex detector; relies entirely on the column header naming the category |
| URL | header-driven | `URL` (blocking) | drop / suppress | n/a |
| IP address | header-driven | `IP` (blocking) | drop / suppress | n/a |
| Biometric identifier | header-driven only | none | drop / suppress per classification | No free-text/regex detector |
| Full-face photograph | not handled | none | none | `organize()` has no image-file route; image/DICOM inputs are unrecognized-format |
| Profession | not handled | none | none | No detector or action; quasi-identifier risk is not scored |
| Quasi-identifier combination (structured/tabular) | `phi_engine/security/kanon_gate.py`/`pycanon_gate.py` | n/a | available EXPLICIT-INVOCATION query-time k-anonymity analysis, NOT wired into the publish path (`pycanon_gate.py`'s own docstring: publish-gate status DEFERRED, no `run.py` callsite) | Structured re-identification risk is an open gap at publish time, not a wired control |
| Quasi-identifier combination (free text) | not handled | none | none | Confirmed structural gap -- see `docs/PRIVACY_GATEWAY_STRESS_TEST.md` §3 |
| Limited Data Set (LDS) posture | not currently available | n/a | `phi_scrub` fails closed (`PHIScrubError` raised at synthesis time) | Requires `authorities/phi_limited_dataset.md`, which does not exist in this checkout; `compliance_posture: limited_dataset` cannot currently be selected |
| Re-identification pseudonym | header-driven | n/a | HMAC-pseudonymize: `HMAC-SHA256(key, label:raw_id)` | Linkable pseudonymization -- one-way (key does not decrypt back to the raw value; it permits deterministic recomputation for a candidate value, enabling enumeration/linkage) -- not an independent §164.514(c) code and not itself Safe-Harbor-compliant absent Expert Determination -- see `authorities/01_hipaa_164_514_full.md` |

## Table D — File formats phi_engine's organizer routes

`phi_engine/pipeline/organize.py::organize()` is the only runtime file-format
router; there is no separate format-generation subsystem.

| Format | Authority | phi_engine `organize()` handling |
|---|---|---|
| JSONL | JSON RFC 8259 | ✓ validated/normalized into `organized/<study>/datasets/` |
| JSON | JSON RFC 8259 | ✓ validated/normalized |
| CSV | RFC 4180 | ✓ parsed (`dtype=str`, no NA coercion) |
| XLSX | OOXML ECMA-376 | ✓ sheet-split, header-promoted |
| XLS (legacy BIFF) | — | ✓ via `xlrd` when available; unreadable/mislabeled routes to review bucket (fail-closed) |
| PDF | ISO 32000-1 | ✓ table-extracted (`pdfplumber`) or matched as an annotated-CRF companion by stem; no extractable table and no stem match routes to review bucket |
| Any other suffix (DOCX, HTML, XML, HL7 v2/CDA, DICOM, image, Parquet, archive, etc.) | — | Not handled — routes to the review bucket with `reason: unrecognized-format` |

## Table E — LLM/attack surface controls

Repository-specific concern mapped to the `phi_engine` control that
addresses it (mirrors `docs/THREAT_MODEL.md`'s OWASP LLM Top 10 table).

| Attack | Source | phi_engine control |
|---|---|---|
| LLM01: Prompt Injection | OWASP LLM Top 10 2025 | `phi_engine.security.llm_tool_guard.validate_llm_read_path` -- defined, no production caller yet |
| LLM02: Sensitive Info Disclosure | OWASP | `guard_llm_output` (live only on `llm_detector.py`/`regulation_fetcher.py` provider responses; the generic `llm_safe_tool` decorator has zero production uses), `phi_gate_check`, `phi_guard_gate.run_phi_guard_gate` |
| LLM05: Improper Output Handling | OWASP | `guard_llm_output` blocks unsafe serialized output without echoing raw values |
| LLM06: Excessive Agency | OWASP | `config.get_llm_client` (external providers disabled by default); `validate_llm_read_path` exists but is not yet wired |
| LLM07: System Prompt Leakage | OWASP | `phi_engine.audit.zone_guards`; `validate_llm_read_path` exists but is not yet wired |
| LLM09: Misinformation | OWASP | `docs/THREAT_MODEL.md`'s explicit non-certification boundary |
| k-anonymity violation (structured) | Sweeney 2002 | `phi_engine/security/kanon_gate.py`/`pycanon_gate.py` -- available query-time utility, NOT wired into the publish path |
| Re-identification codes | 164.514(c) | `phi_scrub`'s HMAC-pseudonymize action -- linkable pseudonymization only, see Table C |

## Table F — Detection regime taxonomy

Each identifier is categorized by the detection regime required: rule-applicable
(regex/pattern matching sufficient, as implemented in `phi_patterns.py`) or
contextual-NER-required (needs classification context beyond a fixed
pattern — currently addressed only by header-driven classification, not a
free-text NER model in this repository).

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
| 28 | ZIP | contextual_ner_required + CONFLICT | ZIP is PHI under HIPAA; contextual NER required; cross-regime conflict (out of scope) |

ZIP codes (row 28) are PHI under HIPAA Safe Harbor (B).

## How this matrix is used

1. **Engineering review** should check Tables A+C+D to verify `phi_engine`'s
   actual detector/router coverage matches the claimed taxonomy.
2. **Security review** should check Table E against `docs/THREAT_MODEL.md`.
3. **Counsel review** should check Table B against applicable authority.

## Update procedure

This matrix is updated when:
- A new authority is ratified (add to Table B)
- A new identifier category is added (add to Table A with citation)
- `phi_engine`'s own detection/action surface changes (update Table C)
- `phi_engine/pipeline/organize.py`'s routed formats change (update Table D)
- A new attack vector or control is documented (add to Table E)
