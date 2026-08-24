# PHI agent architecture

## Scope and how to read this

This document records the code-backed PHI pipeline, supervision, verification, and human-review architecture. The Code anchors table identifies the source symbols for claims developed in this document.

## System context (Level 0)

The frontend, HTTP layer, and pipeline driver are separate code surfaces. Their current entry points are listed in the Code anchors table.

## Agent census and roster table

The roster, supporting agents, and non-Agent drivers are defined in the agent modules listed below.

## Orchestration loop (Level 1)

`run_pipeline` is the pipeline driver. Its execution order and termination paths are documented from that implementation.

## Inside one agent (Level 2) and the per-agent contract

`Agent` provides the shared agent contract. Individual implementations define their own inputs, outputs, and failure behavior.

## Manager supervision (Level 3)

`Manager` supervises agent calls and records run-level decisions.

## Operator and Reviewer coverage audit (Level 4)

`Operator` and `Reviewer` are deterministic audit stages that examine written exports and coverage.

## Human review (Level 5)

The review interface and its supporting HTTP layer are identified in the Code anchors table.

## PHI boundary and what it does and does not prove

The implementation defines the prompt, export, and review boundaries. This document distinguishes named controls from properties that code alone does not establish.

## Failure and escalation matrix

Failure handling and escalation behavior are defined by the pipeline driver, base agent, Manager, and deterministic gates.

## Tunables reference

Configuration and fixed bounds are documented from their current source definitions.

## Code anchors

| Concept | File | Symbol | Current line |
| --- | --- | --- | --- |
| Pipeline driver | `backend/phi_core/agents/orchestrator.py` | `run_pipeline` | 96 |
| Base agent | `backend/phi_core/agents/base.py` | `Agent` | 72 |
| Manager | `backend/phi_core/agents/manager.py` | `Manager` | 26 |
| Judge | `backend/phi_core/agents/reasoning.py` | `Judge` | 874 |
| Sentinel | `backend/phi_core/agents/reasoning.py` | `Sentinel` | 985 |
| Executor | `backend/phi_core/agents/reasoning.py` | `Executor` | 1065 |
| Auditor | `backend/phi_core/agents/reasoning.py` | `Auditor` | 1257 |
| Deterministic gates | `backend/phi_core/agents/reasoning.py` | `validate_decisions`; `apply_sentinel_hard_rules`; `apply_age_dob_rule`; `apply_site_cardinality_rule`; `apply_confidence_floor`; `apply_blocking_floor`; `apply_sentinel_escalations`; `verify_keep_decisions` | 41; 366; 447; 522; 585; 629; 675; 717 |
| Lexicon | `backend/phi_core/agents/specialists.py` | `Lexicon` | 34 |
| Schema | `backend/phi_core/agents/specialists.py` | `Schema` | 261 |
| Instrument | `backend/phi_core/agents/specialists.py` | `Instrument` | 340 |
| Statute | `backend/phi_core/agents/experts.py` | `Statute` | 30 |
| Praxis | `backend/phi_core/agents/experts.py` | `Praxis` | 320 |
| Scout | `backend/phi_core/agents/outward.py` | `Scout` | 18 |
| Ledger and subagents | `backend/phi_core/agents/outward.py` | `LedgerCompare`; `LedgerAggregate`; `Ledger` | 47; 69; 95 |
| Herald and subagents | `backend/phi_core/agents/outward.py` | `HeraldAbstract`; `HeraldSections`; `Herald` | 144; 171; 198 |
| Operator | `backend/phi_core/agents/operator.py` | `Operator` | 236 |
| Reviewer | `backend/phi_core/agents/reviewer.py` | `Reviewer` | 34 |
| Provider calls and JSON parse | `backend/phi_core/agents/llm.py` | `call_llm`; `parse_json` | 210; 221 |
| Batching | `backend/phi_core/agents/batching.py` | `run_batched` | 14 |
| Web cache | `backend/phi_core/agents/cache.py` | `REFRESH_DAYS`; `cache_get`; `cache_put` | 13; 16; 26 |
| Publish guard | `backend/phi_core/publish_guard.py` | `scan_all_exports` | 302 |
| Session statuses | `backend/phi_core/models.py` | `SessionStatus` | 23 |
| HTTP layer | `backend/server.py` | `app` | 126 |
| Review UI | `frontend/src/pages/SessionDetail.jsx` | `SessionDetail` | 623 |
| Launcher UI | `frontend/src/pages/Wizard.jsx` | `Wizard` | 528 |
| API client | `frontend/src/lib/api.js` | `API` | 4 |

## Documented intent versus current code

This section records divergences between architecture descriptions and current source when they are established from code.
