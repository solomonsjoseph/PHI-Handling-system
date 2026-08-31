# Security Policy

## Reporting suspected PHI or security leakage

Do not file public GitHub issues for suspected PHI/security leakage.

Use the private maintainer contact configured by the project owner. If no private maintainer contact is configured, stop distribution and notify the repository owner out-of-band before sharing details publicly.

Reports should avoid including raw PHI or secrets. Provide paths, record IDs, hashes, issue categories, and reproduction steps whenever possible.

## PHI handling rules

- No real PHI may be committed.
- No real PHI may be added to tests, fixtures, runtime outputs (`intake/`, `organized/`, `output/`), logs, screenshots, or documentation examples.
- If suspected real PHI is found, stop release/distribution of the affected artifact until it is removed and any affected workspace output is regenerated from synthetic fixtures.

## PHI Console (backend) release-safety disclosure

This section documents known, disclosed gaps in the live export/release path that a
reader of this policy must not mistake for closed. See `docs/THREAT_MODEL_BACKEND.md`
for the full backend threat model.

**Export is not gated by the mandatory release gate.** The live bundle and export path
is `phi_core/bundle.py::build_bundle`, a self-contained ZIP assembler serving the
`session_bundle`, `session_export`, and `session_reversal_key` endpoints. Those
endpoints are gated on session status (`complete`/`partially_complete`), an
`EXPORT_RETENTION_WINDOW_DAYS` (default 14) 410-Gone check after the window elapses, and
`export_expires_at` surfaced on session reads.

`build_bundle` is NOT gated by `FinalAssuranceGate`
(`control/final_assurance.py::evaluate_final_assurance`), the deterministic
non-bypassable release gate the master architecture mandates (section 57: model
confidence cannot override it). `evaluate_final_assurance` has zero live call sites in
`server.py`, `superorchestrator.py`, or `agents/`. In practical terms, today a session
that reaches `complete`/`partially_complete` can be downloaded without ever passing the
15-condition `FinalAssuranceGate` check. `build_bundle` has its own independent safety
measures, and Publish Guard's residual-PHI scan is real and wired, but the specific
mandatory non-bypassable gate is not in that call path. This gap was disclosed during
the infrastructure rewrite and deliberately left open (wiring it in carries regression
risk across the existing download test suite), not silently fixed or silently ignored.

**Related disclosed gaps** (also in `docs/THREAT_MODEL_BACKEND.md`):

- `SuperOrchestrator` has several built-and-tested methods (`begin_export`,
  `confirm_export`, `authorize_publication`, `authorize_execution`, among others) with
  zero production callers today.
- The `LearningService`/`LearningCaseService` approval-and-promotion layer
  (`control/learning.py`) has no endpoint caller; no human can approve or promote a
  learning candidate through any current API.
- Artifact lineage invalidation (`control/artifacts.py::invalidate_descendants`) is
  built and tested but not triggered by any live event.
- The observability read-side (`user_agent_trace`, `maintainer_trace`) is tested but has
  no live endpoint caller.

**Sandbox boundary disclosures** (macOS/Darwin and same-uid filesystem):

- macOS/Darwin cannot enforce `RLIMIT_AS`; the sandbox fails closed unless
  `PHI_SANDBOX_ALLOW_UNENFORCED_MEMORY` is explicitly set.
- The sandbox's socket monkeypatch stops accidental egress only, not a deliberate
  bypass (`_socket`, `importlib.reload`, `subprocess`, `os.system`, `os.execve`,
  `ctypes`, pre-patch unpickling references).
- The sandbox is not a chroot/container boundary: the same-uid sandboxed worker can
  still read `backend/.env`, `~/.aws/credentials`, `~/.ssh/`, and service-account
  tokens.

**Encryption-key generation:** `crypto.py` auto-generates a dev key when
`APP_ENCRYPTION_KEY` is empty in `.env`, which orphans any ciphertext encrypted under a
previous ephemeral key on every restart. See `docs/RUNBOOK.md`'s "Encryption-key
rotation" section.

**Incident and canary coverage:** `control/security_incident.py` records
`SECURITY_BOUNDARY_VIOLATION` as a `ControlRecord` that deliberately cannot hold a raw
leaked value, wired into `ProviderGateway`'s leak-canary-hit branch. The leak-canary
harness (`control/canary.py`) covers 13 live surfaces (exports plus trace events,
`workflow_runs.opaque_map`, agent logs, HandoffEnvelope payloads, the learning store,
research queries, errors, and ZIP metadata).

## External LLM providers

External LLM providers are disabled by default for PHI tasks and require explicit approval before use. PHI-task LLM configuration must be reviewed through the project configuration and `docs/THREAT_MODEL.md`'s release-gate process before any external provider egress is enabled.

## Security-relevant tests

Security-relevant tests include:

- `tests/test_phi_llm_safety.py`
- `tests/test_llm_egress_gate.py`
- `tests/test_stress_standalone.py`
- `tests/test_phase3_run_pipeline_integration.py`

Passing tests are required evidence for the covered controls, but they do not replace clinician review, counsel review, external security review, or regulatory certification.
