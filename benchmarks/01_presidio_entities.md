# Microsoft Presidio Supported Entities — Benchmark Reference

**Source:** https://microsoft.github.io/presidio/supported_entities/
**Retrieved:** 2026-04-19
**Authority:** Microsoft open-source de-identification framework, de facto industry baseline
**License:** MIT

## Global entities (13)

| Entity | Description | Detection |
|---|---|---|
| CREDIT_CARD | 12-19 digits | Pattern+checksum |
| CRYPTO | Bitcoin wallet only | Pattern+context+checksum |
| DATE_TIME | Absolute/relative dates/periods/times <1 day | Pattern+context |
| EMAIL_ADDRESS | RFC-822 | Pattern+context+RFC validation |
| IBAN_CODE | International Bank Account Number | Pattern+context+checksum |
| IP_ADDRESS | IPv4 or IPv6 | Pattern+context+checksum |
| MAC_ADDRESS | Network interface MAC | Pattern+context |
| NRP | Nationality, religious or political group | Custom logic+context |
| LOCATION | Cities, provinces, countries, bodies of water, mountains | Custom+context |
| PERSON | Full name (first/middle/last) | Custom+context |
| PHONE_NUMBER | Phone number | Custom+pattern+context |
| MEDICAL_LICENSE | Common medical license numbers | Pattern+context+checksum |
| URL | Uniform Resource Locator | Pattern+context+TLD validation |

## USA entities (7)

US_BANK_NUMBER (8-17 digits), US_DRIVER_LICENSE, US_ITIN (9 digits starting with 9, 4th digit 7 or 8), US_MBI (Medicare Beneficiary Identifier, 11 alphanumeric), US_NPI (National Provider Identifier, 10 digits), US_PASSPORT (9 digits), US_SSN (9 digits)

## UK entities (5)

UK_NHS (10 digits+checksum), UK_NINO (National Insurance), UK_PASSPORT (2 letters+7 digits, 2015+), UK_POSTCODE (5-8 alphanumeric), UK_VEHICLE_REGISTRATION (current/prefix/suffix formats)

## Spain (2): ES_NIF, ES_NIE

## Italy (5): IT_FISCAL_CODE, IT_DRIVER_LICENSE, IT_VAT_CODE, IT_PASSPORT, IT_IDENTITY_CARD

## Poland (1): PL_PESEL

## Singapore (2): SG_NRIC_FIN, SG_UEN

## Australia (4): AU_ABN, AU_ACN, AU_TFN, AU_MEDICARE

## India entities (6) — relevant to RePORTaLiN

| Entity | Description |
|---|---|
| IN_PAN | Indian Permanent Account Number (12 char alphanumeric) |
| IN_AADHAAR | Indian Unique Identity Number (12 digits+checksum) |
| IN_VEHICLE_REGISTRATION | Transport vehicle registration |
| IN_VOTER | Election Commission 10 digit alphanumeric voter ID |
| IN_PASSPORT | Indian Passport Number |
| IN_GSTIN | Goods and Services Tax ID (15 char with state code 01-37) |

## Finland (1): FI_PERSONAL_IDENTITY_CODE

## Korea (5): KR_DRIVER_LICENSE, KR_FRN, KR_PASSPORT, KR_BRN, KR_RRN

## Nigeria (2): NG_NIN, NG_VEHICLE_REGISTRATION

## Thai (1): TH_TNIN

## Medical/Clinical (8 — requires transformers extra, uses blaze999/Medical-NER)

MEDICAL_DISEASE_DISORDER, MEDICAL_MEDICATION, MEDICAL_THERAPEUTIC_PROCEDURE, MEDICAL_CLINICAL_EVENT, MEDICAL_BIOLOGICAL_ATTRIBUTE, MEDICAL_BIOLOGICAL_STRUCTURE, MEDICAL_FAMILY_HISTORY, MEDICAL_HISTORY

---

## Total Presidio entity count: 62 predefined entities across 13 jurisdictions + 8 clinical entities

## Gap analysis — Presidio vs. HIPAA Safe Harbor 18 categories

| HIPAA Safe Harbor Category | Presidio coverage |
|---|---|
| A. Names | PERSON (yes) |
| B. Geographic subdivisions | LOCATION (yes, but no ZIP3 logic) |
| C. Dates (elements except year) and ages >89 | DATE_TIME (partial, no age-over-89 rule) |
| D. Telephone numbers | PHONE_NUMBER (yes) |
| E. Fax numbers | **NOT DISTINCTLY COVERED** |
| F. Email addresses | EMAIL_ADDRESS (yes) |
| G. SSN | US_SSN (yes) |
| H. Medical record numbers | **NOT COVERED** (only MEDICAL_LICENSE) |
| I. Health plan beneficiary numbers | US_MBI (yes, for Medicare) |
| J. Account numbers | US_BANK_NUMBER (partial) |
| K. Certificate/license numbers | US_DRIVER_LICENSE, MEDICAL_LICENSE (partial) |
| L. Vehicle identifiers including license plates | IN_VEHICLE_REGISTRATION, UK_VEHICLE_REGISTRATION, NG_VEHICLE_REGISTRATION (partial, no US, no VIN pattern) |
| M. Device identifiers and serial numbers | **NOT COVERED** |
| N. Web URLs | URL (yes) |
| O. IP addresses | IP_ADDRESS (yes) |
| P. Biometric identifiers | **NOT COVERED** |
| Q. Full-face photographs | **NOT COVERED** (image redactor is separate) |
| R. Any other unique identifying code | (catchall, not implemented) |

## Gap analysis — Presidio vs. DPDPA Rule 14 identifiers

| DPDPA identifier type | Presidio coverage |
|---|---|
| Customer ID file number | **NOT COVERED** |
| Customer acquisition form number | **NOT COVERED** |
| Application reference number | **NOT COVERED** |
| Enrolment ID | **NOT COVERED** |
| Email address | EMAIL_ADDRESS (yes) |
| Mobile number | PHONE_NUMBER (yes) |
| Licence number | partial (driver/medical) |

## Gap analysis — SPDI Rules 2011 (Indian sensitive personal data)

| SPDI category | Presidio coverage |
|---|---|
| Password | **NOT COVERED** |
| Financial information (bank/credit/debit) | CREDIT_CARD, US_BANK_NUMBER, IBAN_CODE (partial) |
| Physical/physiological/mental health condition | partial (MEDICAL_DISEASE_DISORDER) |
| Sexual orientation | **NOT COVERED** |
| Medical records/history | MEDICAL_HISTORY (partial) |
| Biometric information | **NOT COVERED** (image redaction separate) |

## Critical Presidio gaps vs. RePORTaLiN requirements

1. **Indian identifiers missing:** ABHA (14-digit Ayushman Bharat Health Account), Ration Card (29 state-specific formats), UAN (EPF), ESI, CGHS beneficiary, BPL card number, CTRI enrolment ID
2. **US identifiers missing:** VIN pattern (17-char), HICN legacy Medicare, state-specific license plate patterns, UDI-DI (device identifier), Clinical Trial NCT ID as linkable
3. **Biometric signals missing:** fingerprint references, voice print references, DNA sequence references, face image metadata
4. **Medical identifiers missing:** CTRI ID, NCT/ClinicalTrials.gov ID, WHO ICTRP ID, EudraCT ID, ISRCTN ID
5. **Household identifiers missing:** HIPAA Safe Harbor covers identifiers of relatives/employers/household members — Presidio does not have relative-relationship tags
6. **Quasi-identifier detection absent:** Presidio does not have built-in k-anonymity or combination-attack detection

## Implications for corpus benchmarking

For our corpus to be meaningfully benchmark-comparable:

1. **Include explicit Presidio-aligned entity tags** on every test case so Presidio can be run against the corpus using only its predefined entity set
2. **Include gap-fill entity tags** for categories Presidio cannot detect (biometric, device, fax, MRN, household relations, Indian health-scheme, CTRI, ABHA)
3. **Report Presidio baseline score** on our corpus as a point of comparison — expected to be high on global entities, lower on gap categories
4. **Provide a Presidio configuration file** showing which custom recognizers would be needed to bring Presidio to full coverage on our corpus
5. **Document the false-positive profile** — Presidio context-aware detection may flag non-PHI as PHI; our corpus validation must handle this

## Presidio integration strategy for benchmarking

```python
# Benchmark harness conceptual layout
from presidio_analyzer import AnalyzerEngine

analyzer = AnalyzerEngine()
for record in corpus:
    text = record["text"]
    gold_spans = record["gold_spans"]
    presidio_results = analyzer.analyze(text=text, language='en')
    # Compare entity/span overlap with gold spans
    # Report: precision, recall, F1 per entity type
    # Report: false-positive types (Presidio detections with no gold span match)
```

Recommended metrics:
- Macro-F1 across entity types
- Per-entity-type precision and recall
- Gap-detection rate (entities Presidio cannot detect that gold has)
- False-positive rate (Presidio detections not in gold)
- Per-jurisdiction F1 (US-only, IN-only, mixed)
