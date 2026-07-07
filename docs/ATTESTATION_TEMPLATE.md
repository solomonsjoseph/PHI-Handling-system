# Release Attestation Template

**Purpose:** Fill this template for a specific release evidence packet. Do not mark any item complete unless a durable artifact exists.

## Release metadata

- Release identifier:
- Date:
- Attesting maintainer:
- Repository commit:

## Checklist

- [ ] No real PHI attestation: I attest that no real PHI was intentionally committed or included in release artifacts, and static PHI sentinel validation has passed.
- [ ] Seed and manifest hash recorded:
  - Seed:
  - Manifest path:
  - Manifest SHA-256:
- [ ] Validation report hash recorded:
  - Validation report path:
  - Validation report SHA-256:
  - Validation status:
- [ ] Benchmark protocol version recorded:
  - Benchmark protocol version:
  - Primary scoring profile:
- [ ] Strict benchmark summary path recorded:
  - Strict benchmark summary path:
  - Raw prediction artifact path:
- [ ] MIA smoke result path recorded:
  - MIA smoke result path:
  - MIA status:
- [ ] Threat model version recorded:
  - Threat model path:
  - Threat model version/date:
- [ ] Clinician/counsel status recorded:
  - Clinician review status: PENDING / COMPLETE / NOT APPLICABLE
  - Counsel review status: PENDING / COMPLETE / NOT APPLICABLE
  - External review status: PENDING / COMPLETE / NOT APPLICABLE
- [ ] Claim level recorded:
  - Claim level:
  - Supporting `release_evidence.json` path:

## Required statement

This attestation supports only the claim level evidenced by the listed artifacts. It does not certify HIPAA, GDPR, DPDPA, LGPD, Australia Privacy Act, Uganda DPPA, IRB approval, or other regulatory compliance. Clinician, counsel, and external review may be claimed only when their status is marked COMPLETE and the supporting artifacts are attached.
