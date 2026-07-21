# Security Policy

## Reporting suspected PHI or security leakage

Do not file public GitHub issues for suspected PHI/security leakage.

Use the private maintainer contact configured by the project owner. If no private maintainer contact is configured, stop distribution and notify the repository owner out-of-band before sharing details publicly.

Reports should avoid including raw PHI or secrets. Provide paths, record IDs, hashes, issue categories, and reproduction steps whenever possible.

## PHI handling rules

- No real PHI may be committed.
- No real PHI may be added to tests, fixtures, runtime outputs (`intake/`, `organized/`, `output/`), logs, screenshots, or documentation examples.
- If suspected real PHI is found, stop release/distribution of the affected artifact until it is removed and any affected workspace output is regenerated from synthetic fixtures.

## External LLM providers

External LLM providers are disabled by default for PHI tasks and require explicit approval before use. PHI-task LLM configuration must be reviewed through the project configuration and `docs/THREAT_MODEL.md`'s release-gate process before any external provider egress is enabled.

## Security-relevant tests

Security-relevant tests include:

- `tests/test_phi_llm_safety.py`
- `tests/test_llm_egress_gate.py`
- `tests/test_stress_standalone.py`
- `tests/test_phase3_run_pipeline_integration.py`

Passing tests are required evidence for the covered controls, but they do not replace clinician review, counsel review, external security review, or regulatory certification.
