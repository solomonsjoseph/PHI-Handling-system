# HL7 FHIR R4 Patient Resource — PHI Fields Analysis

**Source:** https://hl7.org/fhir/R4/patient.html
**Authority:** HL7 FHIR R4 Specification v4.0.1 (Oct 30, 2019)
**Successor:** FHIR R5 v5.0.0 (same Patient fields, minor additions)

## Patient resource structure (FHIR R4)

| Element | Cardinality | Type | PHI risk | HIPAA Safe Harbor mapping |
|---|---|---|---|---|
| identifier | 0..* | Identifier | HIGH - direct | H (MRN), I (Health Plan beneficiary), J (Account), K (License), R (Other unique) |
| active | 0..1 | boolean | NONE | - |
| name | 0..* | HumanName | HIGH - direct | A (Name) |
| telecom | 0..* | ContactPoint | HIGH - direct | D (Phone), E (Fax), F (Email) |
| gender | 0..1 | code | LOW (quasi) | quasi-identifier |
| birthDate | 0..1 | date | HIGH - direct | C (Date related to individual) |
| deceased[x] | 0..1 | boolean/dateTime | HIGH if dateTime | C (Date of death) |
| address | 0..* | Address | HIGH - direct | B (Geographic subdivisions) |
| maritalStatus | 0..1 | CodeableConcept | LOW (quasi) | quasi |
| multipleBirth[x] | 0..1 | boolean/integer | LOW | quasi |
| photo | 0..* | Attachment | HIGH - direct | Q (Full-face photograph) |
| contact | 0..* | BackboneElement | HIGH | A+D+E+F (Household/guardian) |
| contact.relationship | 0..* | CodeableConcept | - | - |
| contact.name | 0..1 | HumanName | HIGH | A (Household member) |
| contact.telecom | 0..* | ContactPoint | HIGH | D+E+F |
| contact.address | 0..1 | Address | HIGH | B |
| contact.gender | 0..1 | code | LOW | - |
| contact.organization | 0..1 | Reference | MEDIUM | may contain PHI via employer |
| contact.period | 0..1 | Period | LOW | dates |
| communication | 0..* | BackboneElement | LOW | language preferences |
| communication.language | 1..1 | CodeableConcept | LOW | - |
| generalPractitioner | 0..* | Reference | MEDIUM | K (provider license) |
| managingOrganization | 0..1 | Reference | LOW | institution |
| link | 0..* | BackboneElement | HIGH | links to other Patient resources |
| link.other | 1..1 | Reference(Patient) | HIGH | - |
| link.type | 1..1 | code | - | replaced-by, replaces, refer, seealso |

## Common Identifier.type codings (PHI risk)

| Code | System | Description | PHI risk |
|---|---|---|---|
| MR | HL7 v2 0203 | Medical record number | HIGH - HIPAA H |
| SSN | HL7 v2 0203 | Social Security Number | HIGH - HIPAA G |
| NIIP | HL7 v2 0203 | National insurance payor ID | HIGH - HIPAA I |
| PRC | HL7 v2 0203 | Permanent Resident Card Number | HIGH |
| DL | HL7 v2 0203 | Driver's License | HIGH - HIPAA K |
| PPN | HL7 v2 0203 | Passport Number | HIGH |
| NI | HL7 v2 0203 | National Identifier | HIGH |

## Other FHIR R4 resources with Patient-linked PHI

Beyond Patient resource, PHI surfaces in:
- **Practitioner** (0..* identifier, name, telecom, address, qualification) — provider license numbers (K)
- **PractitionerRole** — who treats whom (context can identify)
- **RelatedPerson** — household/relatives (HIPAA scope includes these)
- **Person** — base person not yet Patient
- **Group** — cohort identity
- **Encounter** — subject (Patient), period (dates), hospitalization.admitSource, location
- **Condition** — subject, code (diagnosis - can be re-identifying), onsetDateTime
- **Observation** — subject, effectiveDateTime (dates), performer
- **DiagnosticReport** — subject, issued, effective, media (photos)
- **Immunization** — patient, occurrenceDateTime, lotNumber
- **MedicationRequest** — subject, authoredOn, dispenseRequest.validityPeriod
- **CarePlan** — subject, period, goal.dueDate
- **Appointment** — start, end, participant.actor
- **Composition** — subject, author, date
- **DocumentReference** — subject, author, date, content.attachment (may be full document with PHI)

## Narrative/extension leakage surfaces

Every FHIR resource has:
- **text.div** — XHTML narrative that may contain PHI (physician-authored summaries)
- **extension** — arbitrary extensions may carry PHI
- **contained** — embedded resources
- **meta.tag** — may leak institutional identity
- **meta.source** — source URL (HIPAA N - web URL)
- **id** — logical ID (if derived from patient data, violates 164.514(c))

## FHIR bundle dangers

- **Bundle.entry.fullUrl** — may be the canonical URL of the Patient resource
- **Bundle.entry.request** — may contain linking information
- **Bundle.signature** — may reveal signer

## Actions for corpus

1. **Add FHIR R4 Patient JSON fixtures** — valid JSON with every PHI-carrying field populated
2. **Add FHIR bundle fixtures** — Patient + Practitioner + Encounter + Observation bundle
3. **Add FHIR Patient.link test cases** — verify sanitizer handles Patient-Patient links
4. **Add FHIR text.div test cases** — PHI in narrative XHTML
5. **Add FHIR extension test cases** — PHI in arbitrary extensions
6. **Add FHIR meta test cases** — PHI in meta.tag, meta.source
7. **Add FHIR photo Attachment test cases** — base64 PHI in photo element
8. **Add FHIR Practitioner fixtures** — provider PHI (K - license) distinct from Patient
9. **Add FHIR RelatedPerson fixtures** — household/guardian PHI (HIPAA scope)
10. **Verify sanitizer handles both XML and JSON representations** equivalently

## Integration strategy

- Use `fhir.resources` Python library for validation
- Use `fhirclient` for resource construction
- Include FHIR CapabilityStatement for test server metadata
- Include reference implementation of a FHIR sanitization operation (`$de-identify` per FHIR operations framework)
