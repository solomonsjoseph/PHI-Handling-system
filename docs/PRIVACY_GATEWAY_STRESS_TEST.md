# Privacy Gateway — Stress Test Report

Date: 2026-07-20/21. Every number below cites an artifact hash, command, and tool/fixture version. Machine-readable twin: `benchmarks/results/privacy-gateway/comparison.json` (`stress_test` key). Vendor-only numbers are never reported here; every measurement in this document was run in this repository this session.

## 1. Canonical current-path fail-closed behavior

Command:
```
python -m harness.run_phi_system --study PrivacyGatewayUS --jurisdiction us --seed 42 --n-subjects 60 --out-dir benchmarks/results/current-system/privacy-gateway-us
```

Result (`benchmarks/results/current-system/privacy-gateway-us/phi_system_result.json`):

| Field | Value |
|---|---|
| exit_code | 0 (clean) |
| redaction_recall | 0.9958275382475661 |
| residual.ok | true |
| human_review_rate | 0.0 |

Matches the pre-existing documented baseline (`docs/JURISDICTION_EVIDENCE_REPORT_US.md`) exactly. The residual gap is the disclosed SANT zero-jitter-offset property (`docs/GAP_ANALYSIS_BEST.md` §3), not an unredacted leak.

## 2. Fail-closed regression against a deliberately malformed source tree

Commands (in order):
```
python -m harness.make_stress_fixtures --out tmp/privacy-gateway-stress-source --seed 42
python -m phi_engine intake --study PrivacyGatewayStress --source tmp/privacy-gateway-stress-source --workspace tmp/privacy-gateway-stress-ws
python -m phi_engine organize --study PrivacyGatewayStress --workspace tmp/privacy-gateway-stress-ws
python -m phi_engine run --study PrivacyGatewayStress --jurisdiction us --workspace tmp/privacy-gateway-stress-ws
python -m harness.spec_check --skip-pytest --workspace tmp/privacy-gateway-stress-ws --study PrivacyGatewayStress --source-manifest tmp/privacy-gateway-stress-source.manifest/stress_manifest.json
```

| Stage | Measured outcome |
|---|---|
| intake | 66 files symlink-linked, 1 error (`vanished_file.jsonl`: `broken-symlink-in-source`, correctly rejected rather than silently skipped) |
| organize | 6 datasets recognized, 60 routed to the value-free review bucket |
| run | `exit_code=8` ("partial run -- held forms or a non-empty review queue"), `guard_ok=true`, `guard_failed=false`, `published_count=6`, `review_queue_size=60` |
| spec_check | `ALL PASS` — `intake_symlink_invariant`, `llm_boundary_canary`, `source_immutability` |

**Planted-identifier check:** `grep -rlE '[0-9]{3}-[0-9]{2}-[0-9]{4}' tmp/privacy-gateway-stress-ws/output/PrivacyGatewayStress/llm_source/` → exit code 1 (zero matching files). No SSN-shaped planted identifier from the 66-file stress fixture reached the published tree.

**Conclusion:** malformed/ambiguous inputs are held (60/66 files), never silently published; partial exit 8 is distinct from and never conflated with clean exit 0 in the result JSON; the residual guard held.

## 3. Adversarial-channel exercise against benchmarked candidates

Fixture: `tmp/privacy-gateway-fixtures-a` (`harness/make_privacy_gateway_fixtures.py --seed 42`), 31 records / 44 gold spans, `attack_tags` covering homoglyph evasion, zero-width-character injection, prompt injection, base64 encoding, split secrets across streamed chunks, mislabeled columns, contextual false positives, quasi-identifier combinations, org-dictionary/fingerprint markers, nested/overlapping entities, OCR-error simulation, EXIF/GPS, network test PANs, reserved-range IPs, and long-input boundary placement.

Measured (strict_all_span, exact strategy, entity-type-aware; see `benchmarks/results/privacy-gateway/{phi-engine,presidio-stock,detect-secrets}/`):

| Tool | Precision | Recall | F1 | Gold spans | Predicted |
|---|---|---|---|---|---|
| phi_engine (repository control) | 0.6000 | 0.4773 | 0.5316 | 44 | 35 |
| Presidio stock (`oss-0001`) | 0.1094 | 0.1591 | 0.1296 | 44 | 64 |
| detect-secrets (`sectok-0003`) | 1.0000 | 0.0455 | 0.0870 | 44 | 2 |

detect-secrets' low aggregate recall is expected and correct: it is a secrets-only tool scored against a mixed PHI+payment+secrets fixture set; on the `API_KEY` entity type alone it scores precision 1.0 / recall 1.0 (2/2), see §5.

**Structural gaps confirmed by this run, not vendor claims:**
- Base64-encoded PHI (`text-base64-008`): not detected as a plain span by any candidate tested. Requires a dedicated decode-then-rescan stage; none of the three candidates here implement one.
- Split secrets across streamed chunks (`secret-split-013a`/`013b`): correctly not detected in either half individually (by design of the test) — no candidate here does session/stream-level reassembly before scanning. This is a genuine channel gap for any pre-egress gateway that scans per-chunk rather than per-session.

## 4. Linkage / re-identification attacks (NIST SP 800-188 §4.3.12 framework)

| Attack | Target | Finding | Bounded outcome |
|---|---|---|---|
| Known-plaintext linkage | HMAC pseudonymization (`phi_scrub.pseudo_id`) | Deterministic per `(label, raw_id, key)` — the same raw identifier under the same label always yields the same `RID_<LABEL>_<alpha12>` token (code-traced, `phi_scrub.py:1345-1374`). An attacker holding one known `(raw_id, pseudonym)` pair and query access can confirm linkage for other records without the key. | Inherent to deterministic keyed pseudonymization, not a defect; it is exactly why this repo does not and cannot claim HIPAA Safe Harbor for the `pseudonymize` action (current-system trace §2.7) — full reversal still requires key compromise. |
| Interval-preservation frequency attack | SANT date-shift | Per-subject offset is constant across all of that subject's dates (measured 56/56 intervals preserved, `docs/JURISDICTION_EVIDENCE_REPORT_US.md`). Knowing the true date of any one event for a subject recovers every other shifted date for that subject. | Same disclosed vector as any interval-preserving date-shift method; the statutory reason Safe Harbor requires year-only generalization instead. |
| Quasi-identifier combination (Sweeney-style) | Free-text narrative (DOB+ZIP+rare-diagnosis) | `harness/make_privacy_gateway_fixtures.py` record `text-quasi-identifier-006` plants exactly this combination in free text. Neither `phi_scrub`'s header-driven config nor the regex/checksum detection catalog performs cross-field quasi-identifier risk scoring on narrative text; `kanon_gate.py`/`pycanon_gate.py` operate on structured/tabular data only. | **Confirmed structural gap**: free-text quasi-identifier combinations are not covered by any current control. Structured/tabular quasi-identifier risk is covered by the existing k-anon gate (not independently re-benchmarked this session). |
| MIA (retained context) | Corpus-level membership inference | This repo's own deterministic 7-feature MIA smoke test (`mia_report.json`) is retained as a smoke test per `docs/KNOWN_LIMITATIONS.md`, not treated here as external validation. Its own reported population differs across artifacts (698 vs 606 vs manifest 550-us+148-formats) — see current-system trace §6.3; this is a repository-documentation contradiction, not a new finding from this stress pass. | Not usable as re-identification-risk evidence until the population figures are reconciled. |

## 5. Encryption, key custody, RBAC, and audit enforcement

Code-traced this session (`config.yaml:16-28`, `phi_scrub.py`, `phi_keystore.py`):

| Control | Status | Evidence |
|---|---|---|
| Encryption at rest | **Disabled by default** | `config.yaml: security.encryption.enabled: false`. Pseudonymization is not encryption — data at rest is plaintext with re-linkable-with-key pseudonyms. |
| RBAC | **Disabled by default** | `config.yaml: security.rbac.enabled: false`. Only enforced role gate in code is the `llm-agent` key-access denial (`phi_scrub.py:335-343`, `phi_keystore.py:100-124`), not a general RBAC system. |
| Log encryption | **Not implemented** | `config.yaml:17-22` (explicit reserved-block comment). Logs/telemetry record category tags only (never raw values, `phi_gate.py:172-177`) but those tags are stored unencrypted. |
| Key custody | Code-enforced | HMAC key: 0600-mode sidecar file; `PHIKeyStore` zeroizable-bytearray singleton; `llm-agent` role denied at two layers. Deleting the key forfeits re-derivation of prior pseudonyms — the documented irreversibility lever. |
| Audit | Enabled, value-free | `output/<study>/audit/` (`phi_scrub_report.json`, `human_review/`, `telemetry/`). No automated retention policy or encrypted-backup mechanism found in config — a real, disclosed gap. |

## 6. Failure-mode / detector-outage behavior (code-traced, not independently re-triggered this session)

`run.py:1558-1566`: if `run_phi_guard_gate` raises (e.g. a Presidio import failure), the pipeline falls back to `scan_tree_for_phi` — the **legacy regex scanner alone** — and still allows publish on its `ok`. The two-scanner OR-guarantee (`phi_guard_gate.py:65`, `ok = presidio.ok and legacy.ok`) is silently reduced to one scanner on that exception path. This is a **known, code-confirmed weak-fallback bypass**: "fallback to a weaker detector and publish" is exactly the failed-case pattern the gateway design must not repeat (see `docs/PRIVACY_GATEWAY_RECOMMENDATION.md` §"Weak points wrapped or replaced").

## 7. Summary

- Fail-closed publish behavior: **confirmed intact** end to end (canonical path + adversarial 66-file stress tree).
- Zero planted identifiers reached the published tree in either run.
- Adversarial-channel measurement is real, on-corpus, and produces a materially different (worse) F1 for every candidate than the clean `corpus/us` baseline — never conflate the two.
- Two structural gaps are newly confirmed by this pass: base64-encoded payloads and split-across-chunk secrets, neither covered by any evaluated candidate.
- Encryption-at-rest, RBAC, and log-encryption are confirmed disabled/unimplemented by default — pseudonymization must never be described as encryption in any downstream document.
- The residual-guard weak-fallback path (regex-only on Presidio failure) remains open and must be closed before any capability that "wraps" the residual guard can be marked production-ready.
