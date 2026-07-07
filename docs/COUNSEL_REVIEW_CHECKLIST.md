# Counsel Review Checklist

**Status:** PENDING - no institutional legal counsel review has been completed in this repository.

This checklist is a review aid for counsel. It is not legal advice and does not certify HIPAA, GDPR, DPDPA, LGPD, Australia Privacy Act, Uganda DPPA, or any other compliance status.

| Item | Jurisdiction | Authority | Repository artifact | Review decision | Reviewer initials/date |
|---|---|---|---|---|---|
| HIPAA de-identification basis | US | 45 CFR 164.514 | `corpus/MANIFEST.json`; authority citations in generated records; `docs/KNOWN_LIMITATIONS.md` | PENDING | PENDING |
| GDPR personal/special-category data and research context | EU | GDPR Articles 4, 9, and 89 | EU generator outputs; authority citations; `docs/KNOWN_LIMITATIONS.md` | PENDING | PENDING |
| DPDPA/DPDP Rules coverage and enforcement timing | India | DPDPA 2023 and DPDP Rules 2025 | India generator outputs; `docs/KNOWN_LIMITATIONS.md` | PENDING | PENDING |
| LGPD identifier and health-data treatment | Brazil | LGPD | Brazil generator outputs; authority citations where present | PENDING | PENDING |
| Australia health/privacy coverage | Australia | Australia Privacy Act | Australia generator outputs; authority citations where present | PENDING | PENDING |
| Uganda health/privacy coverage | Uganda | Uganda DPPA | Uganda generator outputs; authority citations where present | PENDING | PENDING |
| Synthetic data legal basis | Cross-jurisdiction | Applicable de-identification, anonymization, and synthetic-data authorities | Corpus generation process; `corpus/MANIFEST.json`; `validation_report.json`; `release_evidence.json` | PENDING | PENDING |
| External LLM egress approval | Cross-jurisdiction / vendor-specific | Applicable DPA, BAA, institutional data-transfer, and security requirements | `phi_engine.config.config.get_llm_client`; PHI LLM environment/configuration approvals | PENDING | PENDING |

Counsel reviewers should record each decision as approved, approved with conditions, rejected, or not applicable, with initials/date and any separate memo reference. Until a row is reviewed, its decision must remain `PENDING`.
