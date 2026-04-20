# DICOM PS3.15 Annex E — Basic Application Level Confidentiality Profile

**Source:** https://dicom.nema.org/medical/dicom/current/output/chtml/part15/chapter_e.html
**Authority:** DICOM Standard 2025e, NEMA
**Scope:** Attribute Confidentiality Profiles (Normative)

## Key concepts

- **Basic Application Level Confidentiality Profile (BACP)**: the single standard DICOM de-identification profile
- **Table E.1-1**: the authoritative enumeration of DICOM attributes to de-identify
- **Action Codes** (Table E.1-1a): D (replace with dummy), Z (replace with zero), X (remove), K (keep), C (clean), U (replace with UID), Z/D, X/Z, X/D, X/Z/D, X/Z/U*

## Basic Application Level Confidentiality Options (Annex E.3)

These options modify the base profile:

| Option | Purpose |
|---|---|
| E.3.1 Clean Pixel Data | Remove burned-in PHI from pixel data |
| E.3.2 Clean Recognizable Visual Features | Remove identifying facial features |
| E.3.3 Clean Graphics | Remove graphical overlays with PHI |
| E.3.4 Clean Structured Content | Remove PHI from Structured Reports |
| E.3.5 Clean Descriptors | Review free-text fields for PHI |
| E.3.6 Retain Longitudinal Temporal Information | Date-shift with preserved intervals vs. full removal |
| E.3.7 Retain Patient Characteristics | Age/sex/weight/height retained for research |
| E.3.8 Retain Device Identity | Device info retained for research |
| E.3.9 Retain UIDs | Study/Series/Instance UIDs retained (with re-hashing option) |
| E.3.10 Retain Safe Private | Retain vendor-private tags known safe |
| E.3.11 Retain Institution Identity | Institution name retained |

## Critical DICOM tag categories with PHI risk

### Patient direct identifiers (must remove for BACP)
- **(0010,0010)** Patient's Name (PN)
- **(0010,0020)** Patient ID (LO)
- **(0010,0021)** Issuer of Patient ID (LO)
- **(0010,0030)** Patient's Birth Date (DA)
- **(0010,0032)** Patient's Birth Time (TM)
- **(0010,0040)** Patient's Sex (CS) — Z action (replace with zero-length)
- **(0010,1000)** Other Patient IDs (LO)
- **(0010,1001)** Other Patient Names (PN)
- **(0010,1005)** Patient's Birth Name (PN)
- **(0010,1010)** Patient's Age (AS)
- **(0010,1040)** Patient's Address (LO)
- **(0010,2154)** Patient's Telephone Numbers (SH)
- **(0010,21C0)** Pregnancy Status (US)
- **(0010,21D0)** Last Menstrual Date (DA)

### Referring/treating personnel (remove)
- **(0008,0080)** Institution Name
- **(0008,0081)** Institution Address
- **(0008,0090)** Referring Physician's Name
- **(0008,0092)** Referring Physician's Address
- **(0008,0094)** Referring Physician's Telephone Numbers
- **(0008,1048)** Physician(s) of Record
- **(0008,1050)** Performing Physician's Name
- **(0008,1060)** Name of Physician(s) Reading Study
- **(0008,1070)** Operators' Name
- **(0040,A123)** Person Name

### Dates (date shift or remove)
- **(0008,0020)** Study Date (DA)
- **(0008,0021)** Series Date (DA)
- **(0008,0022)** Acquisition Date (DA)
- **(0008,0023)** Content Date (DA)
- **(0008,0030)** Study Time (TM)
- **(0008,0031)** Series Time (TM)
- **(0008,0032)** Acquisition Time (TM)
- **(0008,0033)** Content Time (TM)

### Identifiers that may link
- **(0008,0018)** SOP Instance UID (U action - replace with new UID)
- **(0020,000D)** Study Instance UID (U)
- **(0020,000E)** Series Instance UID (U)
- **(0020,0010)** Study ID (SH)
- **(0008,0050)** Accession Number (SH)

### Free text fields (require cleaning)
- **(0008,1030)** Study Description (LO)
- **(0008,103E)** Series Description (LO)
- **(0018,1030)** Protocol Name (LO)
- **(0032,1060)** Requested Procedure Description
- **(0040,0254)** Performed Procedure Description
- **(0010,4000)** Patient Comments (LT)
- **(0040,0275)** Request Attributes Sequence

### De-identification markers (MUST add)
- **(0012,0062)** Patient Identity Removed = "YES" (CS)
- **(0012,0063)** De-identification Method (LO)
- **(0012,0064)** De-identification Method Code Sequence (SQ)

## Critical leakage surfaces NOT always caught

1. **Private tags (odd group numbers)** — vendor-specific, may contain PHI
2. **128-byte File Meta preamble** — per DICOM PS3.10, can contain identifying info
3. **Burned-in pixel PHI** — patient name/MRN overlaid on image pixels (scanner burn-in)
4. **Structured Report content items** — free-text inside SR hierarchy
5. **Overlays (group 60xx)** — deprecated but may still carry PHI
6. **Digital signatures** — may reveal signer identity
7. **Audio recordings** — voice prints are Safe Harbor category P

## Actions for corpus

1. **Add DICOM metadata test fixtures** (binary DICOM headers with synthetic PHI in each of the tags listed above)
2. **Add burned-in pixel PHI cases** (PNG fixtures simulating DICOM images with overlaid PHI text)
3. **Add private-tag test cases** (DICOM headers with vendor-private tags containing PHI)
4. **Verify Patient Identity Removed tag is set to YES** post-sanitization
5. **Test all 11 Annex E.3 options** behavior (retain/remove combinations)
6. **Date shifting fixtures** — preserve intervals between dates while removing absolute dates
7. **UID remapping fixtures** — validate UIDs are remapped consistently across series/study

## Integration with the rest of the corpus

- DICOM binary headers are treated as a FILE FORMAT layer
- Test cases are synthetic DICOM files (no actual patient images)
- Uses `pydicom` library for parsing, `pynetdicom` if network simulation needed
- Reference implementation: `deid` (pydicom's official sister project for de-identification)
