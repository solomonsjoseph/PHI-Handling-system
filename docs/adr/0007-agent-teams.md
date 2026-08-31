# 0007: Fixed non-authoritative agent teams for aggregate budget reporting

## Status

Superseded. The five non-authoritative team labels this ADR records still exist in `control/policy.py::TEAMS`, but the member sets recorded below are stale: `Statute` and `Praxis` were renamed `RegulationsExpert` and `PHIMethodsExpert` (Phase 5/6), `Sentinel` was retired into `Reviewer` (Phase 8), and `Auditor` was retired (Phase 17-B). The current mapping in `control/policy.py::TEAMS` is authoritative. See `docs/PHASE_STATUS.md`.

## Context

The existing agent roster has distinct manifests, input classes, tool permissions, and capability grants. The Phase 5 requirements name five team labels for aggregate budget reporting and `test_control_bounds.py` assertions. They do not establish a new execution layer, a delegation hierarchy, shared grants, or shared authority.

Adding team objects, team agents, or a routing layer would create a second naming and authority system without changing any real permission boundary. It would also conflict with the control-plane rule that only typed manifests and durable work records can create work or grant capability.

## Decision

`control/policy.py::TEAMS` is an immutable mapping with exactly these five groups:

- `regulatory_evidence`: `Statute`, `Praxis`, `CorpusResearcher`
- `data_and_instrument`: `Lexicon`, `Schema`, `Instrument`
- `proposal_and_challenge`: `Judge`, `Sentinel`
- `verification_and_audit`: `Executor`, `Operator`, `Reviewer`, `Auditor`
- `publication_and_reporting`: `Scout`, `Ledger`, `Ledger.Compare`, `Ledger.Aggregate`, `Herald`, `Herald.Abstract`, `Herald.Sections`

The mapping is used only for aggregate budget reporting and the exact-partition contract in `backend/tests/test_control_bounds.py`. It cannot issue grants, create `WorkItem`s, route provider calls, accept child results, advance workflow state, or replace the individual `MANIFESTS` authority. `Manager` remains supervisory and `Pipeline` remains the top-level durable transport unit, so neither belongs to an agent work team.

No new agents are added. Further delegation is allowed only when a test demonstrates a concrete benefit and the resulting authority change receives its own ADR.

## Consequences

- Per-agent `AgentManifest` remains the sole source of concrete capability and resource limits.
- A team-level report can sum existing usage without widening any member's budget.
- Future code must not interpret team membership as permission to substitute, construct, or supervise a member agent.
- Changes to a team membership are architectural decisions and require an ADR update plus the exact-partition test update.
