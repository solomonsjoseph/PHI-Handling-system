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
- **(e)** **Limited Data Set** (LDS) — a restricted PHI data set, distinct from the de-identification methods in (b)
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

This set is Census-dependent and may change with new decennial data. `authorities/AUTHORITY_MATRIX.md` pins to this list with a note.

## The "no actual knowledge" safety net — paragraph (b)(2)(ii)

> "The covered entity does not have actual knowledge that the information could be used alone or in combination with other information to identify an individual who is a subject of the information."

CRITICAL: Safe Harbor is NOT just removing the 18. It is removing the 18 PLUS satisfying the actual-knowledge test. If the CE knows combination-based re-identification is possible (rare disease + small geography + date range), Safe Harbor fails even with all 18 removed.

This is the legal hook for combination-based re-identification risk generally — the pipeline's classification layer must treat quasi-identifier combinations (rare disease + small geography + date range) as a residual risk even after all 18 Safe Harbor categories are removed.

## Re-identification codes — paragraph (c)

Permitted IF:
- (c)(1) Derivation: the code is NOT derived from or related to information about the individual, AND not otherwise capable of being translated to identify
- (c)(2) Security: CE does not use/disclose the code for any other purpose AND does not disclose the re-identification mechanism

Implications for the pipeline's pseudonymization layer:
- A code that is truly independent of the individual (sequential record
  numbers, randomly assigned IDs unrelated to the patient) is permitted
  under (c).
- A cryptographic hash of the individual's OWN identifier value, even with
  a secret salt/key, is NOT automatically a permitted (c) re-identification
  code: NIST IR 8053 states HIPAA specifically allows properly salted
  one-way hashes for pseudonymization only as part of the Expert
  Determination method (b)(1), requiring expert certification -- not
  merely by virtue of using a secret key. A keyed hash of the raw
  identifier remains "derived from ... information about the individual"
  in the ordinary sense of (c)(1).
- Mapping tables/keys must be secured and separately held; mechanism
  disclosure is forbidden under (c)(2) regardless of which method is used.

## Minimum necessary — paragraph (d)

Applies to requests and disclosures. Entity must:
- (d)(2) identify roles/classes and their access needs
- (d)(3) limit routine disclosures via policy; review non-routine disclosures individually
- (d)(4) limit requests from other covered entities
- (d)(5) never disclose/request entire medical record unless specifically justified

Implication: the envelope must enforce "entire medical record" blocking and document the minimum-necessary justification at runtime.

## Limited Data Set (LDS) — paragraph (e)

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

This is a restricted PHI category, not a de-identification tier and not
itself a de-identification method. Implication for the pipeline: an
LDS-scoped output MAY retain dates and general geography (town/city,
state, ZIP) while still removing the 16 excluded direct identifiers above;
it must remain tagged as PHI requiring a Data Use Agreement, never as
de-identified output.

## Fundraising — paragraph (f)

PHI permitted without authorization for fundraising:
- Demographic info (name, address, contact, age, gender, DOB)
- Dates of healthcare provided
- Department of service
- Treating physician
- Outcome information
- Health insurance status

This is a SEPARATE permitted use — not de-identification. But it means names+address+DOB can legally be used for fundraising even though they are Safe Harbor identifiers. Implication: the pipeline's classification layer must distinguish a fundraising-scoped context from a research/treatment context before applying the fundraising exception, rather than force-dropping these fields unconditionally.

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

## PHI-handling implications

1. **Limited Data Set (LDS) handling** (paragraph (e)) — an LDS action/method in
   `phi_engine/security/phi_scrub.py` MAY retain dates and town/city/state/ZIP
   while removing the 16 LDS-excluded identifiers, and must never be classified as
   fully de-identified output.

2. **Re-identification-code handling** (paragraph (c)) — `phi_scrub`'s
   HMAC-pseudonymize action computes `HMAC-SHA256(key, label:raw_id)` -- a
   keyed, ONE-WAY hash of the individual's OWN raw identifier value. Key
   possession does not decrypt a pseudonym back to its raw value, but it
   does permit deterministic recomputation of the pseudonym for any
   candidate raw value, enabling enumeration/linkage against a bounded
   candidate space. This is linkable pseudonymization, not an independent
   (c) re-identification code, and it does not by itself satisfy Safe
   Harbor or (c) absent a documented Expert Determination (b)(1). The
   pipeline must never disclose the pseudonymization mechanism or key in
   published output.

3. **Fundraising-context handling** (paragraph (f)) — classification must
   distinguish a fundraising-scoped context from research/treatment before
   applying the fundraising permitted-use exception; outside that context, the
   listed fields remain Safe Harbor identifiers subject to normal scrub actions.

4. **Verification-disclosure handling** (paragraph (h)) — any disclosure path the
   pipeline exposes must log a verification artifact (badge/letterhead/statement
   of authority) per (h), captured value-free in `output/<study>/audit/`.
