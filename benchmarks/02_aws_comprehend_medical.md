# Amazon Comprehend Medical DetectPHI — Benchmark Reference

**Source:** https://docs.aws.amazon.com/comprehend-medical/latest/dev/textanalysis-phi.html
**Authority:** Amazon Web Services managed PHI detection service
**Official note:** "Under the HIPAA act, PHI that is based on a list of 18 identifiers must be treated with special care. Amazon Comprehend Medical detects entities associated with these identifiers but these entities don't map 1:1 to the list specified by the Safe Harbor method."

## DetectPHI Entity Types (9 total)

| Entity | Description | HIPAA Safe Harbor category |
|---|---|---|
| AGE | All components of age, spans, any age mentioned (patient/family/others). Default years. | 3. Dates related to an individual (C) |
| DATE | Any date related to patient or patient care | 3. Dates (C) |
| NAME | All names (patient, family, provider) | 1. Name (A) |
| PHONE_OR_FAX | Any phone, fax, pager; EXCLUDES named numbers like 1-800-QUIT-NOW and 911 | 4. Phone (D) + 5. FAX (E) |
| EMAIL | Any email address | 6. Email (F) |
| ID | SSN, MRN, facility ID, clinical trial number, certificate/license, vehicle/device number, biometric number, place-of-care ID, provider ID | 7. SSN (G) + 8. MRN (H) + 9. Health Plan (I) + 10. Account (J) + 11. Cert/License (K) + 12. Vehicle (L) + 13. Device (M) + 16. Biometric (P) + 18. Any other identifying characteristic (R) |
| URL | Any web URL | 14. URLs (N) |
| ADDRESS | All geographic subdivisions; named medical facilities; wards within a facility | 2. Geographic (B) |
| PROFESSION | Profession or employer of patient or family | 18. Any other identifying characteristic (R) |

## Critical observations

1. **Total: 9 entity types** vs. HIPAA's 18 categories — Amazon maps multiple HIPAA categories to single entities
2. **"ID" is a mega-category** covering HIPAA categories G, H, I, J, K, L, M, P, R — 9 out of 18. A detector cannot distinguish MRN from SSN from license. This is a **granularity gap**.
3. **IP address (HIPAA category O) is NOT in the DetectPHI list** — neither is biometric identifier as a distinct type (only via the ID category)
4. **Full-face photographs (category Q)** — NOT COVERED. Amazon Comprehend Medical is text-only.
5. **English only** — Amazon Comprehend Medical limitation
6. **PROFESSION tags employment** — goes beyond the 18 identifiers into quasi-identifier territory (useful)
7. **Multiple ADDRESS granularities** — returns street, city, state individually
8. **Document size limit: 20,000 bytes** for synchronous DetectPHI

## Known Amazon Comprehend Medical limitations

- No Indian identifiers (no PAN, Aadhaar, ABHA, MRN format awareness for Indian institutions)
- No geographical reasoning about ZIP3 (cannot tell if a ZIP is one of the 17 restricted ZIP3 codes)
- No date shifting or age-binning (only detection)
- No distinguishing of household members vs. patient
- Cannot detect quasi-identifier combinations (rare disease + ZIP + DOB)
- The ID mega-category means gold-standard evaluation is tricky: our corpus should map both our specific types and Amazon's flatter types
- PROFESSION detection can create false positives on general employment terms ("teacher", "doctor") without verifying it is the patient's/family's profession

## Additional Comprehend Medical non-PHI categories (available via DetectEntitiesV2)

For context, Comprehend Medical has 6 non-PHI categories:
- ANATOMY
- BEHAVIORAL_ENVIRONMENTAL_SOCIAL (tobacco, alcohol, drugs, allergies, gender, race/ethnicity)
- MEDICAL_CONDITION
- MEDICATION
- PROTECTED_HEALTH_INFORMATION
- TEST_TREATMENT_PROCEDURE
- TIME_EXPRESSION

**Ontology linking:** ICD-10-CM, RxNorm, SNOMED CT via InferICD10CM, InferRxNorm, InferSNOMEDCT

## Implications for corpus benchmarking

1. **Our taxonomy is 2-3x more granular than Comprehend Medical's** — we can test if our detector can distinguish within their ID mega-category
2. **The "PROFESSION" entity** is not in Presidio but IS in Comprehend Medical — our corpus should include profession-only test cases to benchmark both
3. **The DATE entity in Comprehend Medical** combines all date types; our corpus should measure their performance on age-over-89 edge cases (HIPAA C) vs. normal dates
4. **Document size** — corpus records should include at least some <20,000 byte narratives for direct Comprehend Medical compatibility
5. **Comprehend Medical is US-biased** — our corpus should explicitly measure its degraded performance on Indian text (PAN, Aadhaar, ABHA-style IDs should NOT be detected correctly)

## Recommended benchmark metrics vs. Comprehend Medical

- Macro-F1 per Comprehend Medical entity type mapped to our taxonomy
- **Mega-category resolution rate:** can we distinguish SSN vs MRN vs license within their ID category?
- **Jurisdiction sensitivity:** performance on US records vs. IN records (expected steep dropoff)
- **HIPAA category coverage:** Comprehend Medical covers 12-14 of 18; our system must demonstrate coverage of all 18

## Integration harness strategy

```python
# Conceptual benchmark adapter
import boto3

client = boto3.client('comprehendmedical', region_name='us-east-1')

def detect_phi_comprehend_medical(text: str) -> list:
    """Return list of (begin, end, type, score) tuples."""
    if len(text.encode('utf-8')) > 20000:
        raise ValueError("Text exceeds 20000 byte limit")
    response = client.detect_phi(Text=text)
    return [
        (e['BeginOffset'], e['EndOffset'], e['Type'], e['Score'])
        for e in response['Entities']
    ]
```

Required: an ENTITY_MAPPING dictionary between our corpus taxonomy and Comprehend Medical's 9 types for score reconciliation.
