# Privacy Gateway — Stress Test Report

Every number below cites a command actually run in this repository. Vendor-only numbers are never reported here.

## 1. Fail-closed review-routing regression against the v3 fixture packages

Commands (in order, `$SRC`/`$WS` are scratch directories):
```
python -m harness.make_stress_fixtures --out $SRC --seed 42
python -m phi_engine intake --study PrivacyGatewayStress --source $SRC --workspace $WS
python -m phi_engine organize --study PrivacyGatewayStress --workspace $WS
python -m phi_engine run --study PrivacyGatewayStress --jurisdiction us --workspace $WS
python -m harness.spec_check --skip-pytest --workspace $WS --study PrivacyGatewayStress --source-manifest $SRC.manifest/stress_manifest.json
```

| Stage | Measured outcome |
|---|---|
| intake | 10 accepted-format files symlink-linked, 0 review, 0 errors, `status=ready` (the mandatory `datasets/` + `forms/` + `data_dictionary/` + `mappings/` v3 package) |
| organize | 6 datasets produced, 1 routed to the review bucket (`screening_form.pdf`: `pdf-no-extractable-table`, filename/reason recorded, never row values) |
| run | `exit_code=8` ("partial run -- held forms or a non-empty review queue"), `guard_ok=true`, `guard_failed=false`, `published_count=6`, `review_queue_size=17` (1 organizer review item + 16 dependency recommendations) |
| spec_check | `ALL PASS` -- `intake_symlink_invariant`, `llm_boundary_canary`, `source_immutability` |

**Planted-identifier check:** `grep -rlE '[0-9]{3}-[0-9]{2}-[0-9]{4}' $WS/output/PrivacyGatewayStress/llm_source/` → exit code 1 (zero matching files). No SSN-shaped planted identifier from the stress fixture reached the published tree.

**Conclusion:** the mandatory-component ready package publishes only its six recognized datasets; the one non-extractable form and the planted free-text SSN/phone columns are held or suppressed, never silently published; partial exit 8 is distinct from and never conflated with clean exit 0 in the result JSON; the normal combined residual-guard path (Presidio AND legacy regex, both clean) passed. The companion review-required package (`build_review_required_fixtures`, seed 43) drives every fixed intake review reason -- `unsupported-format` (including the JSON/JSONL cases v3 demotes from accepted datasets), `xlsx-workbook-invalid`, `dataset-xlsx-multiple-sheets`, and `source-symlink-not-allowed` -- so malformed inputs fail closed at intake and never reach organize. This exercises the malformed-input review-routing invariant, NOT the guard's detector-outage fail-closed behavior (see §5).

## 2. Adversarial-channel exercise (structural gaps, code-level)

Fixture: `harness/make_privacy_gateway_fixtures.py --seed 42`, 31 records / 44 gold spans, `attack_tags` covering homoglyph evasion, zero-width-character injection, prompt injection, base64 encoding, split secrets across streamed chunks, mislabeled columns, contextual false positives, quasi-identifier combinations, org-dictionary/fingerprint markers, nested/overlapping entities, OCR-error simulation, EXIF/GPS, network test PANs, reserved-range IPs, and long-input boundary placement.

**Structural gaps confirmed by direct code reading, not a comparative benchmark:**
- Base64-encoded PHI (`text-base64-008`): `phi_engine/security/phi_patterns.py`'s catalog and `phi_gate.py` scan the literal text only. There is no decode-then-rescan stage, so base64-encoded PHI is not detected as a plain span.
- Split secrets across streamed chunks (`secret-split-013a`/`013b`): every scan path (`phi_gate_check`, `phi_guard_gate.run_phi_guard_gate`) operates per-file/per-payload, not per-session; there is no session/stream-level reassembly before scanning. This is a genuine channel gap for any pre-egress control that scans per-chunk rather than per-session.

## 3. Linkage / re-identification attacks (NIST SP 800-188 §4.3.12 framework)

| Attack | Target | Finding | Bounded outcome |
|---|---|---|---|
| Known-plaintext linkage | HMAC pseudonymization (`phi_scrub.pseudo_id`) | Deterministic per `(label, raw_id, key)` — the same raw identifier under the same label always yields the same `RID_<LABEL>_<alpha12>` token (code-traced, `phi_scrub.py`). An attacker holding one known `(raw_id, pseudonym)` pair and query access can confirm linkage for other records without the key. | Inherent to deterministic keyed pseudonymization, not a defect; it is exactly why this repository does not and cannot claim HIPAA Safe Harbor for the `pseudonymize` action. `pseudo_id` is a one-way HMAC truncated to 12 characters, not a reversible cipher: key possession does not decrypt a pseudonym back to its raw value, but it does permit deterministic recomputation of the pseudonym for any CANDIDATE raw value, enabling enumeration/linkage attacks against a bounded candidate space. |
| Interval-preservation frequency attack | SANT date-shift | The per-subject offset is constant across all of that subject's dates by construction (code-traced, `phi_scrub.py`'s date-jitter implementation keys the offset by subject, not by row). Knowing the true date of any one event for a subject recovers every other shifted date for that subject. | Same disclosed vector as any interval-preserving date-shift method; the statutory reason Safe Harbor requires year-only generalization instead. |
| Quasi-identifier combination (Sweeney-style) | Free-text narrative (DOB+ZIP+rare-diagnosis) | `harness/make_privacy_gateway_fixtures.py` record `text-quasi-identifier-006` plants exactly this combination in free text. Neither `phi_scrub`'s header-driven config nor the regex/checksum detection catalog performs cross-field quasi-identifier risk scoring on narrative text; `kanon_gate.py`/`pycanon_gate.py` operate on structured/tabular data only. | **Confirmed structural gap**: free-text quasi-identifier combinations are not covered by any current control. Structured/tabular quasi-identifier risk is available as an explicit, query-time analysis utility (`kanon_gate.py`/`pycanon_gate.py`) but is NOT wired into the publish path -- `pycanon_gate.py`'s own docstring states publish-gate status is DEFERRED and it is not invoked at promotion. |

## 4. Encryption, key custody, RBAC, and audit enforcement

Code-traced (`config.yaml`, `phi_scrub.py`, `phi_keystore.py`):

| Control | Status | Evidence |
|---|---|---|
| Encryption at rest | **Disabled by default** | `config.yaml: security.encryption.enabled: false`. Pseudonymization is not encryption — data at rest is plaintext with re-linkable-with-key pseudonyms. |
| RBAC | **Disabled by default** | `config.yaml: security.rbac.enabled: false`. Only enforced role gate in code is the `llm-agent` key-access denial (`phi_scrub.py`, `phi_keystore.py`), not a general RBAC system. |
| Log encryption | **Not implemented** | `config.yaml` (explicit reserved-block comment). Logs/telemetry record category tags only (never raw values, `phi_gate.py`) but those tags are stored unencrypted. |
| Key custody | Code-enforced | HMAC key: 0600-mode sidecar file; `PHIKeyStore` zeroizable-bytearray singleton; `llm-agent` role denied at two layers. Deleting the key forfeits re-derivation of prior pseudonyms — the documented irreversibility lever. |
| Audit | Enabled, partially value-free | `output/<study>/audit/` (`phi_scrub_report.json`, `human_review/`, `telemetry/`). `phi_gate`/log-hygiene blocking-path logging is category-tags-only; `phi_scrub_report.json` and the organizer review-bucket record additionally retain filenames, field/header names, reasons, and counts (sensitive metadata, access-controlled 0600, not row values). No automated retention policy or encrypted-backup mechanism found in config — a real, disclosed gap. |

## 5. Failure-mode / detector-outage behavior (code-traced)

`run.py`: if `run_phi_guard_gate` raises (e.g. a Presidio import failure), the pipeline falls back to `scan_tree_for_phi` — the **legacy regex scanner alone** — and still allows publish on its `ok`. The two-scanner OR-guarantee (`phi_guard_gate.py`, `ok = presidio.ok and legacy.ok`) is silently reduced to one scanner on that exception path. This is a **known, code-confirmed weak-fallback bypass**: "fallback to a weaker detector and publish" is exactly the failed-case pattern the gateway design must not repeat (see `docs/PRIVACY_GATEWAY_RECOMMENDATION.md` §"Weak points wrapped or replaced").

## 6. Summary

- Malformed-input review routing: **confirmed intact** (v3 fixture packages, §1) -- the ready package's normal combined-guard path (both scanners clean) published six scrubbed datasets with zero planted SSN-shaped matches while holding the one non-extractable form; the review-required package failed closed at intake on all six malformed inputs, none reaching organize or publish.
- Detector-outage fail-closed behavior is **NOT intact and NOT exercised by this stress pass**: the guard-exception legacy-only fallback (§5) remains open, so a Presidio failure during a real run would still publish on the legacy scanner alone.
- Zero planted identifiers reached the published tree.
- Two structural gaps confirmed by code reading: base64-encoded payloads and split-across-chunk secrets, neither covered by any current control.
- Encryption-at-rest, RBAC, and log-encryption are confirmed disabled/unimplemented by default — pseudonymization must never be described as encryption in any downstream document.
- The residual-guard weak-fallback path (regex-only on Presidio failure) remains open and must be closed before any capability that "wraps" the residual guard can be marked production-ready.
