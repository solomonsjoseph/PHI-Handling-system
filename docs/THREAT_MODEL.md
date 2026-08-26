# PHI Threat Model

This threat model covers the standalone PHI intake/classify/scrub/review/
publish runtime (`phi_engine`). It is a security and trust artifact, not a
regulatory certification.

## System boundary

In scope, all under `phi_engine/`:

- Source-tree-to-intake symlink boundary (`pipeline/intake.py`).
- Intake-to-organized derivation (`pipeline/organize.py`).
- Header classification and row scrubbing (`security/phi_review.py`,
  `security/phi_scrub.py`, `pipeline/synthesize_config.py`).
- Optional LLM egress for header classification and support-signal
  extraction (`security/llm_detector.py`, `config/config.py::get_llm_client`).
- Human review decisions and their application on the next run
  (`pipeline/review.py`).
- Residual guard-to-publish boundary (`security/phi_guard_gate.py`).
- HMAC key custody and per-study configuration
  (`security/phi_keystore.py`, `security/key_rotation.py`, `config/config.py`).
- Audit stores (`output/<study>/audit/`).
- Published output (`output/<study>/llm_source/`).

Out of scope:

- Real PHI ingestion or storage. No real PHI may be committed to this
  repository.
- Claims that the pipeline's output satisfies any institution's regulatory,
  IRB, legal, or clinical review obligations.
- External clinician review, counsel review, or independent third-party
  validation unless separately documented as complete.
- Production encryption/RBAC guarantees beyond the explicit configuration
  gates and tests present in the repository.

## Assets

- Source data reached only through intake symlinks; source bytes themselves.
- Organized/scrubbed dataset JSONL under `organized/`, `data/raw/<study>/`,
  and `output/<study>/`.
- HMAC pseudonymization key and per-study scrub/review configuration.
- Audit/review stores (`output/<study>/audit/`): `phi_gate`/log-hygiene
  blocking-path logs are value-free by design (category tags only); the
  organizer review-bucket record and `phi_scrub_report.json` (both
  access-controlled 0600) additionally retain filenames, link names,
  field/header names, reasons, counts, and diagnostic metadata (exception
  type names, sheet names) -- sensitive metadata, not row values.
  `llm_uncertain.jsonl` retains the same class of metadata (plus the raw
  header name) but is written via plain append with NO explicit chmod --
  not 0600-guaranteed -- see Data flows §7.
- PHI/LLM safety configuration, provider approval state, and guard behavior.

Security properties to preserve:

- No real PHI in source, tests, fixtures, or documentation.
- Source bytes under `intake/<study>/` are never modified, moved, or
  deleted by the pipeline.
- No unapproved external egress for PHI-task LLM calls.
- PHI/security blocking-path error messages and phi_gate/log-hygiene audit
  entries never echo raw values (category tags only, e.g. `SSN`/`EMAIL`).
  This guarantee does NOT extend to the organizer review-bucket record,
  `phi_scrub_report.json`, or `llm_uncertain.jsonl`, which retain
  filenames, link names, field/header names, reasons, counts, and
  diagnostic metadata -- sensitive but not row-value data. The organizer
  review-bucket record (`organize.py`, explicit `chmod(0o600)`) and
  `phi_scrub_report.json` are access-controlled; `llm_uncertain.jsonl`
  (`llm_detector.py::_write_review_queue`, plain `Path.open("a")`, no
  chmod) is NOT -- an empirical tempfile check produced mode 0644 /
  parent 0755.
- A residual PHI guard runs before any artifact is published.

## Trust boundaries

| Boundary | Trusted side | Untrusted or lower-trust side | Controls |
|---|---|---|---|
| Source tree to intake | Repository code | External source directory passed to `intake_add` | `phi_engine/pipeline/intake.py` (symlink-only, streamed-hash-only reads) |
| Intake to organized | Symlinked intake tree | Any path outside `<workspace>/intake/` | `phi_engine/pipeline/organize.py` (normalized dataset content reads only through intake symlinks; a separate direct metadata-only read of an optional forms manifest from the external source root also exists) |
| Source tree to LLM-visible tool input | Repository code and approved generated artifacts | Any path requested by an LLM tool | `phi_engine.security.llm_tool_guard.validate_llm_read_path` -- defined and exported, but has NO production caller anywhere in `phi_engine`; not yet an enforced chokepoint |
| Tool result to LLM output | PHI-screened result payload (on covered paths) | Any returned string, mapping, sequence, dataclass, or primitive | `phi_engine.security.llm_tool_guard.guard_llm_output` -- live only on `llm_detector.py`/`regulation_fetcher.py` provider responses; the generic `llm_safe_tool` decorator has zero production uses, so arbitrary tool returns are not routed through it |
| Row data to LLM prompt | Header/metadata-only prompt construction | Row values | `phi_engine.security.phi_gate.phi_gate_check`, headers-only `llm_detector.py` |
| Scrubbed candidate output to promoted output | Generated artifacts under review | `output/<study>/llm_source/` published tree | `phi_engine.security.phi_guard_gate.run_phi_guard_gate` |
| Local PHI workflow to external provider | Disabled or approved LLM provider configuration | External LLM APIs and network egress | `phi_engine.config.config.get_llm_client` |
| Audit/snapshot stores to readers | Guarded internal evidence and operational stores | General file reads and LLM-mediated reads | `phi_engine.audit.zone_guards` (`deny_if_audit_zone`/`deny_if_snapshot_root`) -- only reachable via `validate_llm_read_path`, which has no production caller; not an enforced chokepoint for general reads |

## Data flows

1. `intake_add` symlinks source files into `<workspace>/intake/<study>/`,
   recording a content-hash manifest; source bytes are never copied,
   modified, or deleted.
2. `organize` reads normalized dataset content only through intake
   symlinks (plus a direct metadata-only read of an optional forms
   manifest from the external source root), normalizes recognized formats
   into `organized/<study>/datasets/`, and routes anything unrecognized or
   unparseable to a review bucket recording filename, link-name, reason,
   and diagnostic metadata (never row values).
3. `run_pipeline` classifies headers against pinned USA/HIPAA rules
   (`phi_review.review_form_headers`), synthesizes a per-study scrub config
   (`synthesize_config.py`), and applies it via `phi_scrub.run_scrub`
   (fail-closed on any unmapped action).
4. Optional header-classification LLM calls (`llm_detector.classify_headers`)
   send headers/metadata only; `LLMClient.complete` runs `phi_gate_check` on
   every outbound prompt before provider dispatch and raises
   `PHIEgressBlockedError` on a match.
5. Before publish, `run_phi_guard_gate` scans the candidate output with
   both Presidio and a legacy regex scanner (OR-combined: both must be
   clean) when the gate runs cleanly. On a guard exception, the pipeline
   falls back to the legacy regex scanner ALONE and can still publish on
   that scanner's result alone -- a known, disclosed weak-fallback path
   (`phi_engine/pipeline/run.py`'s residual-guard exception handler), not
   an unconditional two-scanner guarantee.
6. Held forms, organizer review-bucket entries, and LLM-uncertain queue
   entries are exposed via `review list`; a `keep`/`drop`/`override`
   decision persists to `review_decisions.yaml` and is applied on the next
   `run`, not merely logged.
7. PHI-blocking-path audit records under `output/<study>/audit/` (phi_gate,
   log-hygiene) are value-free (category tags only, e.g. `SSN`/`EMAIL`,
   never raw values or offsets). The organizer review-bucket record and
   `phi_scrub_report.json` are a narrower, access-controlled (0600)
   guarantee: no row values, but filenames, link names, field/header
   names, reasons, counts, and diagnostic metadata are retained.
   `llm_uncertain.jsonl` retains the same class of metadata but is NOT
   0600-guaranteed (plain append, no chmod).
8. PHI-task LLM client creation uses `phi_engine.config.config.get_llm_client`,
   with external providers disabled by default unless explicitly approved.

## STRIDE controls

| STRIDE category | Threat | Required control evidence | Residual risk |
|---|---|---|---|
| Spoofing | An artifact pretends to be a clean/published run result. | `run_pipeline`'s `pipeline_result.json` records `guard_ok`/`guard_failed`/`exit_code` per run, checkable via `python -m phi_engine status`. | Result JSON proves pipeline state, not institutional approval. |
| Tampering | Source or organized files change after intake. | `harness/spec_check.py`'s `source_immutability` check compares the complete source entry set -- type, sha256, size, mode, `mtime_ns`, uid, gid, and symlink target (excluding atime) -- against the fixture-build-time manifest; any drift, vanished, or newly-appeared entry fails. | Manifest itself must be independently reviewed alongside any published claim. |
| Repudiation | A review decision or scrub outcome cannot be tied to who/what decided it. | `review_decisions.yaml` records `decided_by`/`source`; audit records are written per run. | Human signoff for real-PHI use remains an operational responsibility outside this repository. |
| Information disclosure | Real PHI or a row value reaches an LLM or the published tree. | `guard_llm_output` (live only on two provider-response paths -- see OWASP mapping), `phi_gate_check`, `phi_guard_gate.run_phi_guard_gate` (both scanners clean); on a guard exception, `phi_guard_gate.run_phi_guard_gate` is bypassed and only the legacy scanner runs (see Data flows §5). `validate_llm_read_path` is defined but has no production caller. | Rule-based/model-based scanning reduces risk but is not proof that arbitrary future content is PHI-free; the guard-exception fallback and unwired read-path wrapper are open gaps (see Release gates). |
| Denial of service | Large intake, organize, or LLM operations exhaust resources. | Commands are explicit and reproducible; external LLM providers are disabled by default. | Dedicated resource limits are not a substitute for operational deployment controls. |
| Elevation of privilege | LLM tools read audit/workspace-internal paths or call external providers without approval. | `phi_engine.config.config.get_llm_client`. `llm_tool_guard.validate_llm_read_path`/`llm_safe_tool` (which alone invoke `zone_guards`) exist for this purpose but have no production caller yet. | Production RBAC/encryption are not claimed complete (see Residual risks); the read-path wrapper gap is a real, disclosed elevation-of-privilege exposure until wired. |

## OWASP LLM Top 10 2025 mapping

| LLM risk area | Repository-specific concern | Controls |
|---|---|---|
| Prompt injection | Prompted tool use could request restricted workspace paths or induce disclosure. | `phi_engine.security.llm_tool_guard.validate_llm_read_path` -- defined, no production caller yet |
| Sensitive information disclosure | Tool output, published artifacts, or an outbound prompt could include a real row value. | `guard_llm_output` (live only on `llm_detector.py`/`regulation_fetcher.py` provider responses; the generic `llm_safe_tool` decorator has zero production uses), `phi_gate_check`, `phi_guard_gate.run_phi_guard_gate` |
| Improper output handling | LLM-facing tool returns could be trusted without PHI screening. | `guard_llm_output` blocks unsafe serialized output on the two paths it covers, without echoing raw values; general tool returns are not yet routed through it. |
| Excessive agency | LLM workflows could read from restricted filesystem zones or select external providers. | `config.get_llm_client`; `validate_llm_read_path` exists but is not yet wired |
| System prompt leakage / secret exposure | Audit or configuration paths could be read through tools. | `phi_engine.audit.zone_guards` (only reachable via the unwired `validate_llm_read_path`) |
| Misinformation / overreliance | Documentation could imply compliance or review completion beyond evidence. | This document's explicit non-certification boundary; `SECURITY.md`. |

## Release gates

The pipeline MUST NOT be described as production-ready for real PHI unless
all of the following are true:

- The focused regression suite passes (`tests/test_phi_engine_integration.py`,
  `tests/test_stress_standalone.py`, `tests/test_phase3_run_pipeline_integration.py`,
  `tests/test_phase3_run_review_integration.py`, `tests/test_pipeline_lock.py`,
  `tests/test_phi_llm_safety.py`, `tests/test_llm_egress_gate.py`).
- `harness/spec_check.py` reports `ALL PASS` against a real run:
  `intake_symlink_invariant` (symlink-only intake tree, plus `lstat`
  rejection of a symlinked intake root/study/component directory),
  `llm_boundary_canary` (provider default `none`, no stray `get_llm_client`,
  and `model_routing.new_offline_local_client()` referenced only as the
  callee of the one sanctioned call inside
  `intake_naming.resolve_intake_study`/`_resolve_intake_study` -- any alias,
  other callsite, or direct `OfflineLocalLLMClient(...)` construction under
  `phi_engine/pipeline/` is a violation), and `source_immutability`
  (complete-entry-set comparison, see Tampering above).
- External provider egress is explicitly approved before any PHI-task
  external LLM provider is used.
- Encryption-at-rest, RBAC, and log encryption are enabled, or an explicit,
  documented risk acceptance covers their absence (`config.yaml`'s
  `security.encryption.enabled` and `security.rbac.enabled` default `false`,
  as does log encryption).
- The residual-guard weak-fallback path (legacy-scanner-alone publish on a
  guard exception) is closed, or an explicit, documented risk acceptance
  covers it.
- `validate_llm_read_path`/`llm_safe_tool` gain a production caller before
  any claim that LLM tool reads are chokepoint-enforced.

## Residual risks

- The residual PHI guard is a rule/model-based scanner, not a mathematical
  proof that no real PHI exists in published output.
- Clinician review and counsel review are not performed by this repository;
  any real-PHI deployment must arrange both separately.
- Encryption at rest, RBAC, and log encryption are disabled/unimplemented
  by default; pseudonymization (`phi_scrub`'s HMAC `pseudo_id`) is not
  encryption.
- `LLMClient`'s prompt egress gate (`phi_gate_check`) has documented false
  negatives; it reduces but does not eliminate egress risk.
- The residual-guard exception path falls back to the legacy regex scanner
  alone and can still publish -- a code-confirmed weak-fallback bypass, not
  yet closed.
- `llm_tool_guard.validate_llm_read_path` and `llm_safe_tool` are available
  but have no production caller; LLM-visible tool reads are not currently
  routed through a read-path chokepoint.
