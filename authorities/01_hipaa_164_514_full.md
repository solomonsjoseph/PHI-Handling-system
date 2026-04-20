# 45 CFR 164.514 — Full Text Extraction and Key Findings

**Source:** https://www.law.cornell.edu/cfr/text/45/164.514
**Retrieved:** 2026-04-20
**Citation authority:** 65 FR 82802 (Dec 28, 2000); 78 FR 5700 (Jan 25, 2013); 78 FR 34266 (Jun 7, 2013)

## Text layout

- **(a)** Standard: De-identification
- **(b)** Implementation specifications: Requirements for de-identification
  - **(b)(1)** Expert Determination method
  - **(b)(2)(i)** Safe Harbor method — 18 identifier categories A through R
  - **(b)(2)(ii)** "No actual knowledge" requirement
- **(c)** Implementation specifications: Re-identification
- **(d)** Minimum necessary requirements
- **(e)** **Limited Data Set** (LDS) — THIRD de-identification tier I had missed
- **(f)** Fundraising communications
- **(g)** Uses and disclosures for underwriting
- **(h)** Verification requirements

## Safe Harbor — the full 18 (paragraphs A through R)

Full enumeration verified against the CFR primary text:

| Para | Category | Exactly as written |
|---|---|---|
| A | Names | "Names" |
| B | Geographic subdivisions smaller than a State | "including street address, city, county, precinct, zip code, and their equivalent geocodes, except for the initial three digits of a zip code if..." |
| C | All elements of dates except year, plus ages over 89 | Full text: "All elements of dates (except year) for dates directly related to an individual, including birth date, admission date, discharge date, date of death; and all ages over 89 and all elements of dates (including year) indicative of such age, except that such ages and elements may be aggregated into a single category of age 90 or older" |
| D | Telephone numbers | |
| E | Fax numbers | |
| F | Electronic mail addresses | |
| G | Social security numbers | |
| H | Medical record numbers | |
| I | Health plan beneficiary numbers | |
| J | Account numbers | |
| K | Certificate/license numbers | |
| L | Vehicle identifiers and serial numbers, including license plate numbers | |
| M | Device identifiers and serial numbers | |
| N | Web Universal Resource Locators (URLs) | |
| O | Internet Protocol (IP) address numbers | |
| P | Biometric identifiers, including finger and voice prints | |
| Q | Full face photographic images and any comparable images | |
| R | Any other unique identifying number, characteristic, or code (except permitted re-id codes under (c)) | |

Scope applies to identifiers "of the individual or of relatives, employers, or household members of the individual."

## The ZIP-3 rule (paragraph B, precise reading)

Text says: initial three digits of ZIP OK if current Census data shows (1) geographic unit formed by combining all ZIPs with same three initial digits contains MORE than 20,000 people; and (2) the initial three digits for ZIP3 units with 20,000 OR FEWER must be changed to "000".

Per HHS/OCR 2012 guidance, the set of restricted ZIP3 codes (those changed to "000") currently is:
**036, 059, 063, 102, 203, 556, 692, 790, 821, 823, 830, 831, 878, 879, 884, 890, 893**

This set is Census-dependent and may change with new decennial data. Our corpus pins to this list with a note.

## The "no actual knowledge" safety net — paragraph (b)(2)(ii)

> "The covered entity does not have actual knowledge that the information could be used alone or in combination with other information to identify an individual who is a subject of the information."

CRITICAL: Safe Harbor is NOT just removing the 18. It is removing the 18 PLUS satisfying the actual-knowledge test. If the CE knows combination-based re-identification is possible (rare disease + small geography + date range), Safe Harbor fails even with all 18 removed.

This is the legal hook for the corpus's DPDPA-strict and quasi-identifier layers — similar issues arise under HIPAA via (b)(2)(ii).

## Re-identification codes — paragraph (c)

Permitted IF:
- (c)(1) Derivation: the code is NOT derived from or related to information about the individual, AND not otherwise capable of being translated to identify
- (c)(2) Security: CE does not use/disclose the code for any other purpose AND does not disclose the re-identification mechanism

Implications for the pipeline's pseudonymization layer:
- Hashing with a secret salt is permitted
- Sequential record numbers are permitted (unrelated to patient)
- Hashing of DOB+SSN is NOT permitted (derived from individual)
- Mapping tables must be secured and separately held
- Mechanism disclosure is forbidden

## Minimum necessary — paragraph (d)

Applies to requests and disclosures. Entity must:
- (d)(2) identify roles/classes and their access needs
- (d)(3) limit routine disclosures via policy; review non-routine disclosures individually
- (d)(4) limit requests from other covered entities
- (d)(5) never disclose/request entire medical record unless specifically justified

Implication: the envelope must enforce "entire medical record" blocking and document the minimum-necessary justification at runtime.

## Limited Data Set (LDS) — paragraph (e) — MISSED IN PRIOR CORPUS

An LDS is PHI that EXCLUDES the following 16 direct identifiers:
1. Names
2. Postal address information OTHER THAN town/city, State, ZIP
3. Telephone numbers
4. Fax numbers
5. Electronic mail addresses
6. Social security numbers
7. Medical record numbers
8. Health plan beneficiary numbers
9. Account numbers
10. Certificate/license numbers
11. Vehicle identifiers and serial numbers
12. Device identifiers and serial numbers
13. Web URLs
14. IP address numbers
15. Biometric identifiers
16. Full-face photographic images

NOT excluded from LDS (therefore permitted):
- **All dates** (full birth, admission, discharge, service dates)
- **General geography** (town/city, state, ZIP)
- **Age** (including over 89)

**LDS is STILL PHI.** Requires a Data Use Agreement (DUA) per (e)(4). Can only be used for research, public health, or health care operations per (e)(3).

This is a third de-identification tier. The corpus needs:
- LDS-valid examples (town+state+ZIP+date OK, names removed)
- LDS-invalid examples (names retained despite otherwise-LDS format)
- LDS-with-quasi-id cases (LDS valid but Sweeney-vulnerable — the (b)(2)(ii) actual-knowledge trap)

## Fundraising — paragraph (f)

PHI permitted without authorization for fundraising:
- Demographic info (name, address, contact, age, gender, DOB)
- Dates of healthcare provided
- Department of service
- Treating physician
- Outcome information
- Health insurance status

This is a SEPARATE permitted use — not de-identification. But it means names+address+DOB can legally be used for fundraising even though they are Safe Harbor identifiers. The corpus should include fundraising-scoped cases to verify the pipeline distinguishes contexts.

## Verification — paragraph (h)

Before disclosure:
- (h)(1)(i) Verify identity and authority
- (h)(1)(ii) Obtain documentation/statements when required

For public officials:
- Agency ID badge (in-person) or government letterhead (written) or proof of agency
- Oral or written statement of legal authority

Implication: the envelope's disclosure boundary must log verification artifacts per (h).

## Citations captured

- 65 FR 82802 (2000-12-28) — original promulgation
- 67 FR 53270 (2002-08-14) — first amendment
- 78 FR 5700 (2013-01-25) — HITECH Final Rule
- 78 FR 34266 (2013-06-07) — genetic information underwriting amendment

## Actions for corpus

1. **Add Limited Data Set layer** (NEW — Layer 11 or integrated into existing layers)
   - LDS-valid records (names/MRN/SSN/etc removed, dates+city+ZIP retained)
   - LDS-invalid records (claim to be LDS but contain LDS-excluded identifier)
   - LDS + quasi-identifier-attack records (LDS format, Sweeney-vulnerable)

2. **Add re-identification-code test cases** (paragraph (c))
   - Hash-from-PII case (violates (c)(1)) — e.g., MRN = SHA256(SSN)
   - Hash-with-salt case (permitted)
   - Sequential re-ID (permitted)
   - Published mechanism disclosure (violates (c)(2))

3. **Add fundraising-context cases** (paragraph (f))
   - Records tagged as fundraising-scope with permitted fields
   - Verify detector distinguishes fundraising from research/treatment contexts

4. **Add verification-disclosure log cases** (paragraph (h))
   - Disclosure request with valid government letterhead
   - Disclosure request without authority verification
   - The envelope's audit log must capture the verification artifact

5. **Update the taxonomy doc** with all four additions above and correct citation.
