# 0005: Evidence claims require tool-backed, independently verified sources

## Status

Accepted

## Context

Before this work, Statute, Praxis, Scout, and `CorpusResearcher` trusted `reply["sources"]` from an LLM response directly: a model-authored URL was treated as evidence with no check that the provider's tool call actually returned it in that response's citations, and no independent verification of authority, freshness, or contradiction. Model confidence was the only signal distinguishing a well-grounded answer from a plausible-sounding fabrication.

## Decision

`backend/phi_core/control/evidence.py` implements the D12 rule: an `EvidenceClaim` reaches `VERIFIED` only when it has at least one `EvidenceSource` that is both tool-backed (`is_tool_backed`: the source URL was actually present in the originating response's tool-call citations, never a model-authored URL taken on faith) and independently passes all five `VerificationDimension` checks (`retrieval_authenticity`, `source_authority`, `claim_support`, `freshness`, `contradiction`). A claim with no such source, or with only a `CONTRADICTED` source, never reaches `VERIFIED` regardless of the model's own reported confidence; confidence is telemetry, not evidence.

Statute's HIPAA-rule and adjacent-regime lookups, Praxis's method research, Scout's citation-bearing findings, and `CorpusResearcher`'s dataset-repository research each now correlate their reported sources to the response's actual tool-call citations and build `EvidenceClaim`/`EvidenceSource` records through this module. When a claim does not reach `VERIFIED`, each agent falls back to its own deterministic, non-LLM default (Statute's baked-in `_pack_fallback`, Praxis's `_fallback(category)`) rather than retrying the same ungrounded call or degrading the result silently.

## Consequences

- A hallucinated citation can no longer silently become an `EvidenceClaim.state == "VERIFIED"` record: every VERIFIED claim in the codebase is traceable to `evaluate_claim`'s return value, never set ad hoc by a caller.
- Deterministic fallback is the fail-closed answer when evidence does not verify, matching the rest of the codebase's fail-closed convention (deterministic default over silent pass-through or blind retry).
- `run_decision_gates` (`0004-artifact-registry.md`'s sibling ADR for the decision gate, recorded when Phase 4's `WorkflowRun`/`decision_version` wiring lands under `0001-workflow-engine.md`) does not itself consume `EvidenceClaim` records yet; Statute/Praxis output still reaches Judge as a plain dict. Binding decision-level evidence citations to a specific `EvidenceClaim` id is Phase 5+ scope.
