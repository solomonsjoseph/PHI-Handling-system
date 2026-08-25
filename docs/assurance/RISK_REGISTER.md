# Assurance risk register

| Phase | Risk | Likelihood | Impact | Depends on | Mitigation | Finding |
| --- | --- | --- | --- | --- | --- | --- |
| 0 | Baseline evidence is stale or dependency resolution is non-reproducible. | Medium | High | None | Pin the resolver-compatible set, record source anchors and CI-parity measurements. | F-DEP-001, F-TEST-001 |
| 1 | The verification gate permits inert tests, flakes, or unexplained skips. | High | High | 0 | Enforce async collection, fix deterministic archives, add full-path and frontend tests, split CI. | F-TEST-001 |
| 2 | A provider call or tool call escapes policy or includes restricted data. | High | Critical | 1 | Gateway-only inference, capability grants, opaque identifiers, static and captured-payload tests. | F-EGRESS-001, F-CAP-001, F-POLICY-004 |
| 3 | Decision, evidence, or artifact transitions lose identity or expose partial bytes. | High | Critical | 2 | Typed records, canonical gates, run-scoped staging, atomic promotion, hash-bound downloads. | F-ART-001, F-EVID-001 |
| 4 | Process death or stale work produces duplicate, missing, or stale consequential effects. | High | Critical | 3 | Leases, fences, single-document CAS, embedded outbox, restart and kill-boundary tests. | F-DUR-001, F-ORCH-001 |
| 5 | Workflow entry paths or agent delegation bypass deterministic and human-review controls. | High | Critical | 4 | Super Orchestrator owns transitions and enqueue; typed bounded child work; static boundaries. | F-ORCH-001, F-CAP-001 |
| 6 | Human review is unauthenticated, non-idempotent, stale, or treats model confidence as authority. | High | Critical | 5 | Typed persisted events, role checks, fences, delivery version, explicit confirmation, UI parity. | F-HITL-001, F-EVID-001, F-POLICY-001 |
| 7 | Audit events are lost, artifacts orphaned, or PHI erasure failures disappear. | Medium | Critical | 3, 4, 6 | Hash-chained events, fan-out, bounded state, reconciliation, holds, retryable tombstones. | F-OBS-001, F-RET-001, F-ART-001, F-POLICY-002, F-POLICY-003 |
| 8 | Runtime content self-activates learning or stale cache crosses policy versions. | Medium | High | 2, 5 | Disabled-by-default learning, reviewer approval, evaluation, canary monitor, rollback, versioned cache. | F-LEARN-001 |
| 9 | Migration or documentation diverges from the final system, leaving an unsafe rollback. | Medium | Critical | 0-8 | Idempotent reversible migration, API export, adversarial review, full regression and acceptance matrix. | F-ART-001, F-DUR-001, F-OBS-001 |

## Rollback mapping

- Phase 0: revert dependency pins and assurance documents as one commit.
- Phase 1: restore prior test configuration only if CI evidence proves the repaired setup invalid; retain failing-test evidence.
- Phase 2: retain the legacy provider path only until the gateway passes its captured-payload tests; do not introduce a bypass.
- Phase 3: adapters isolate old call signatures; staging rollback retains only verified pre-cutover exports.
- Phase 4: recover through the embedded outbox and persisted checkpoint; never resume detached workers.
- Phase 5: route rollback uses the persisted workflow version and command handlers, not direct state writes.
- Phase 6: supersede review requests with a typed event; never delete review history to roll back.
- Phase 7: tombstones and reconciler retry erasure; preserve hash segments before purging events.
- Phase 8: deactivate by recording rollback to the prior activated version; keep learning disabled by default.
- Phase 9: each migration has an idempotent reverse operation documented in `docs/assurance/MIGRATION.md`.
