# Privacy Gateway Recommendation — The Decisive Redirect

Date: 2026-07-20/21. This document names one selected component and one fallback for every required capability, states exactly what current code is retained/replaced/wrapped/integrated/built, and leaves no vendor, interface, or policy choice to a future implementer. Every disposition below is validated against `research/privacy_gateway/{candidate_registry,evidence_ledger}.jsonl` by `harness/validate_privacy_research.validate_dispositions` (0 errors as of the last edit; re-run `python -m harness.validate_privacy_research` to confirm). Evidence for `docs/PRIVACY_GATEWAY_RESEARCH.md`'s findings underlies every decision here; this document does not re-derive them.

## What this does not claim

This document does **not** claim HIPAA de-identification, PCI compliance, general anonymization, or production readiness for any capability until the matching legal/statistical and external-review evidence exists. Clinician and counsel review remain `planned`. No managed/commercial candidate below has a signed contract, executed POC, or independently verified retention/training/subprocessor posture — every one so named is `pending_poc` and is a **fallback/future-reevaluation** target only, never the immediately selected production component: a managed candidate with inaccessible POC may remain in the landscape as `pending_poc` but cannot be the selected production component.

## The required pre-egress sequence

```mermaid
flowchart LR
    P0[0. Destination/data-use policy resolution] --> P1[1. Local metadata + deterministic scans]
    P1 --> P2[2. Selected contextual engine(s)]
    P2 --> P3[3. Strictest-wins transform plan]
    P3 --> P4["4. Keyed tokenization/vault (only if linkage required)"]
    P4 --> P5[5. Residual ensemble scan]
    P5 --> P6{6. Release/hold decision}
    P6 -->|release| P7[7. Outbound adapter]
    P6 -->|hold| PH[Value-free held note; nothing egresses]
    P7 --> P8[8. Response/tool/trace/log scans]
    P8 --> P9[9. Value-free audit]
```

| Stage | Selected component | Fallback | Notes |
|---|---|---|---|
| 0. Policy resolution | Repository (`resolve_rulebook`, `run.py:1080-1106`) | n/a — this is the repo's own gate, no external substitute | Already fail-closed: unavailable/weakened rulebook → exit 8, never a silent downgrade |
| 1. Local metadata + deterministic scans | Repository (`phi_review.review_form_headers` + regex/checksum catalog) | Presidio stock (`oss-0001`) | See `phi_pii_detection` disposition |
| 2. Selected contextual engine(s) | Repository detection catalog + Presidio residual-guard integration (`oss-0001`) | n/a (already the fallback for stage 1) | OR-combined with the legacy regex scanner in `phi_guard_gate` — a repository control, not a vendor claim |
| 3. Strictest-wins transform plan | Repository (`phi_scrub._scrub_row`, first-match-wins) | ARX (`oss-0007`) for future formal utility-loss quantification | Fail-closed on unmapped generalize/band values |
| 4. Keyed tokenization/vault | Repository HMAC-SHA256 `pseudo_id` (48-bit tag, domain-separated) | Skyflow Data Privacy Vault (`sectok-0008`) | Disclosed scale limit: adequate only for cohorts < 100k (`phi_scrub.py:106-107`) |
| 5. Residual ensemble scan | Repository (`phi_guard_gate`: Presidio AND legacy regex, OR-combined) | **Must be wrapped before production**: the exception path (`run.py:1562-1566`) currently drops to legacy-regex-alone and still publishes | See §"Weak points wrapped or replaced" |
| 6. Release/hold decision | Repository (`guard_ok` gate, `run.py:1569-1575`) | n/a | Nothing moves unless `guard_ok`; zero planted identifiers reached the published tree in the fixture stress test (`docs/PRIVACY_GATEWAY_STRESS_TEST.md` §1) |
| 7. Outbound adapter | To be built (narrow gap) | n/a | See `tool_mcp_gate` disposition — `validate_llm_read_path` exists but has no caller |
| 8. Response/tool/trace/log scans | Repository `guard_llm_output` (live only on `llm_detector.py`/`regulation_fetcher.py` provider responses) **+ new wrap for general tool returns and the local-model path** | Bedrock Guardrails (`gw-0001`), Nightfall (`gw-0004`) | See `model_output_gate`/`tool_mcp_gate` dispositions |
| 9. Partially value-free audit | Repository (`output/<study>/audit/`) **+ new build for retention/encrypted backup** | Securiti (`gw-0021`) | PHI-blocking-path logs are category-tags-only; the organizer review-bucket record and `phi_scrub_report.json` retain filenames/header-names/reasons/counts. See `audit_governance` disposition |

## Capability-by-capability disposition (all 15, from `research/privacy_gateway/dispositions.json`)

| Capability | Disposition | Selected | Fallback | Score | What changes |
|---|---|---|---|---|---|
| `phi_pii_detection` | **retain** | repository | `oss-0001` (Presidio) | 61.0 | No code change. Repository catalog + residual-guard Presidio integration (`phi_guard_gate`, OR-combined). |
| `secrets_detection` | **integrate** | `sectok-0003` (detect-secrets) | `sectok-0001` (Gitleaks) | 58.5 | **New.** Add `detect-secrets` to `requirements.txt`; wire its plugin-level scan API into the pre-egress prompt/file scan path (§"Weak points" — CLI verification-filtering must be explicitly disabled or bypassed, per `sectok-e011`). |
| `proprietary_data_detection` | **build** | repository (narrow) | `gw-0019` (BigID, future) | 0.0 | **New, narrow.** No current control and no benchmarked open candidate exists. Build a keyword/codename/document-fingerprint layer inside the existing `phi_patterns.py` architecture — do not build a general DSPM classifier. |
| `structured_reidentification_risk` | **wrap** | repository (`kanon_gate`/`pycanon_gate`) | `oss-0007` (ARX) | 45.0 | Both engines are available EXPLICIT-INVOCATION query-time utilities, NOT wired into the publish path (`pycanon_gate.py`: publish-gate status DEFERRED, no `run.py` callsite). Wire one before claiming structured re-identification-risk coverage, and independently benchmark it against a real quasi-identifier attack dataset. |
| `redaction_and_masking` | **retain** | repository (`phi_scrub`) | `oss-0001` | 70.0 | No code change. `phi_scrub._scrub_row` fail-closed on unmapped values. |
| `pseudonymization_and_token_vault` | **retain** | repository (HMAC `pseudo_id`) | `sectok-0008` (Skyflow) | 52.0 | No code change. Disclose the 48-bit/100k-cohort scale limit in any downstream deployment doc; re-evaluate Skyflow if cohort size grows past that bound. |
| `utility_preserving_transforms` | **retain** | repository (SANT date-shift, generalize/band) | `oss-0007` (ARX) | 68.0 | No code change. Interval-preserving by construction (offset keyed by subject, not row); fail-closed on unparseable dates. |
| `multimodal_file_handling` | **integrate** | `oss-0001` (Presidio image-redactor, incl. DICOM) | `mhe-0005` (Google Healthcare API) | 32.0 | **New, PROPOSED dependency.** `requirements.txt` currently pins only `presidio-analyzer`; Presidio's image-redactor is a SEPARATE package with its own imaging transitive dependencies, not already retained. Subject to dependency/license/security review before adding, then wire into `organize()`'s file-type router for image/DICOM inputs — currently unhandled by the runtime scrub path. |
| `prompt_input_gate` | **wrap** | repository (`phi_gate`) | `gw-0001` (Bedrock Guardrails) | 40.0 | **Fix required.** Extend `phi_gate`/`_gate_ordinary_segments` coverage to the currently-ungated loopback local-model prompt path (`model_routing.py:985-1013`, CONFIDENTIAL sensitivity) before that path can be considered production-safe. |
| `model_output_gate` | **wrap** | repository (`guard_llm_output`) | `gw-0001` | 38.0 | Same fix, response side: gate the local-model response path symmetrically. |
| `tool_mcp_gate` | **build** | repository (narrow) | `gw-0004` (Nightfall) | 0.0 | **Fix required, narrow.** `validate_llm_read_path` already exists and is exported — it has zero callers. Wire it into the pipeline's own `_read_jsonl_rows` and any future tool-read chokepoint. This is a scoped one-function-caller fix, not a rebuild. |
| `logs_traces_telemetry` | **build** | repository (narrow) | `gw-0003` (Purview) | 0.0 | **New, narrow.** PHI-blocking-path logging is already value-free (category tags only); add at-rest encryption for the log/telemetry store (`config.yaml`'s `log_encryption` is explicitly `false`/unimplemented). |
| `storage_discovery` | **build** | repository (narrow, scoped) | `gw-0019` (BigID) | 0.0 | **New, deliberately narrow.** Do not build enterprise-wide DSPM. Build a small manifest check that inventories exactly what is already flowing into the gateway via the declared intake source — matches this gateway's pre-egress mission, not a storage-crawling product. |
| `endpoint_browser_sharing` | **integrate** | `gw-0011` (Purview DLP) | `gw-0012` (Netskope) | 20.0 | **Out of repository scope by architecture** — a backend pipeline has no endpoint/browser agent surface. Do not attempt to build this in `phi_engine`; procure and integrate a real endpoint DLP product when this channel is in scope. Selection remains `pending_poc` until a POC/contract exists — do not deploy without independent verification. |
| `audit_governance` | **wrap** | repository (`output/<study>/audit/`) | `gw-0021` (Securiti) | 43.0 | **Fix required.** PHI-blocking-path logging is value-free (category tags only) and code-enforced; the organizer review-bucket record and `phi_scrub_report.json` additionally retain filenames, link names, header/field names, reasons, and counts (sensitive metadata, access-controlled 0600, not row values). `llm_uncertain.jsonl` retains the same class of metadata but is written via plain append with NO chmod -- not 0600-guaranteed. Add an automated retention policy, encrypted-backup mechanism, and a chmod fix for `llm_uncertain.jsonl` — none exists today. |

## Preserved repository controls (never rebuilt)

Symlink/source immutability, fail-closed organize/review, HMAC key role separation (two-layer `llm-agent` denial), deterministic date/linkage transforms (SANT), review feedback loop, and publish-only-after-guard behavior all survive as-is — no measured evidence in this pass supports replacing any of them.

## Weak points wrapped or replaced (from `docs/PRIVACY_GATEWAY_RESEARCH.md` and the stress test)

1. **Residual-gate weak fallback** (`run.py:1562-1566`): on a `run_phi_guard_gate` exception, the pipeline currently falls back to the legacy regex scanner alone and **still publishes**. This must become fail-closed: an exception in the residual guard must **hold**, not publish on a weaker single scanner. This is the single highest-priority fix in this recommendation — a code-confirmed publish-on-weaker-guard bypass.
2. **Unqualified "headers-only" claims**: `llm_detector.classify_headers` is genuinely headers-only (confirmed); `ConfidentialHeaderTask.samples`/`MatchedSupportCell.value` are not — any future documentation must state the qualified version, never the blanket one.
3. **Unwrapped tool/local-model reads**: `validate_llm_read_path` must gain a caller (`tool_mcp_gate` disposition).
4. **Disabled RBAC/encryption**: must be enabled (or the deployment doc must carry an explicit, signed risk acceptance) before any capability here is described as production-ready for real PHI.
5. **Local-model isolation attestation**: `offline_approved` must never be represented as proven OS/container isolation in any downstream document — it is an operator attestation only, verbatim in the source.
6. **Missing Expert Determination/risk review**: `structured_reidentification_risk` and `proprietary_data_detection` both remain below production-grade scoring (45.0 and 0.0) until independently benchmarked and, for any real-PHI use, until Expert Determination / counsel review is obtained.

## Control-plane architecture

The gateway is a **control plane over reusable engines**, not a new monolithic detector:

- **Pluggable adapters (PROPOSED, not yet implemented)**: no adapter layer currently exists in this checkout (the prior benchmark-adapter package was removed). A future integration should give every engine (repository catalog, Presidio, detect-secrets) the same contract: `analyze_text(text) -> List[PredictedSpan]`, a common CLI, and an honest `not_run` report (never a guessed API call) when unavailable -- this is a design target, not an existing interface.
- **Canonical entity/action vocabulary**: the existing entity-type taxonomy (`entity_type`) plus the existing action set (`keep/suppress/cap/generalize/jitter_date/pseudonymize/drop`) is the single vocabulary every adapter maps into — no per-vendor taxonomy leaks into policy.
- **One policy decision object**: destination/data-use resolution (stage 0) produces a single decision object consumed by every downstream stage — never re-derived per channel.
- **Destination-specific rules**: policy varies by destination (LLM prompt vs. tool call vs. external recipient) but the detection/transform/audit machinery does not fork per destination.
- **Content-addressed evidence**: any future benchmark/stress artifact must be sha256-hashed and cited by hash.
- **No raw-value audit logs**: every new build item (secrets detection, org-dictionary detection, tool-read gating) must emit category-tag-only audit records, matching the existing `phi_gate.py:172-177` discipline — never the raw match.
- **Fail-closed semantics** across prompts, files, tools, model responses, traces, logs, and external sharing: an exception anywhere in the scan/gate chain must **hold**, never silently downgrade to a weaker path and publish (directly closing weak point #1 above).

## Phased adoption order (evidence-driven, not vendor-prestige-driven)

**Phase 1 — Close release-boundary and evidence contradictions (no new dependencies).**
1. Fix the residual-gate weak-fallback bypass (`run.py:1562-1566`) to hold, not publish, on guard exception.
2. Wire `validate_llm_read_path` into the pipeline's own read paths (`tool_mcp_gate`).
3. Extend `phi_gate`/`guard_llm_output` coverage to the local-model prompt/response path (`prompt_input_gate`, `model_output_gate`).

**Phase 2 — Integrate the highest-scoring local/open engines behind the common policy.**
4. Add `detect-secrets` to `requirements.txt`; wire its plugin-level scan API into the pre-egress scan path, using the plugin-level API (not the CLI's verification-filtered `scan` command) per `sectok-e011`.
5. Add Presidio's image-redactor package as a new dependency (subject to dependency/license/security review; not already retained by `requirements.txt`'s `presidio-analyzer`), then wire it into `organize()`'s file-type router for image/DICOM inputs (`multimodal_file_handling`).
6. Build the narrow org-dictionary/fingerprint layer for `proprietary_data_detection`.

**Phase 3 — Add channel coverage.**
7. Build the automated retention + encrypted-backup mechanism for `audit_governance`.
8. Build log encryption to close the `logs_traces_telemetry` gap.
9. Enable RBAC and encryption-at-rest, or obtain and document an explicit signed risk acceptance for their absence.

**Phase 4 — Expert/counsel/clinical review where required.**
10. Independently benchmark `structured_reidentification_risk` (`kanon_gate`/`pycanon_gate`) against a real quasi-identifier attack dataset.
11. Obtain Expert Determination / counsel review before any `jitter_date`/`pseudonymize` output is represented as adequate for a use case requiring Safe Harbor.
12. Re-evaluate every `pending_poc` managed/commercial candidate (Bedrock Guardrails, Azure Health de-id, Comprehend Medical, Purview DLP, Netskope, Datavant, etc.) only once a real contract/POC/credential path exists — never select one as a production component from documentation alone.

**Do not claim** HIPAA de-identification, PCI compliance, general anonymization, or production readiness for any capability above Phase 1 completion until the matching legal/statistical and external-review evidence exists.
