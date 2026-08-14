# Statute regulation coverage — design spec

Date: 2026-08-14
Status: approved, not yet implemented

## Problem

Statute (the regulation expert agent) today researches and reports only
HIPAA Safe Harbor (45 CFR 164.514(b)(2)(i)) for the `us` jurisdiction.
Verification (live run + independent blind sub-agent research, both
2026-08-14) confirmed Statute's HIPAA coverage is accurate and complete:
all 18 identifier categories, correct citations, correct age-90
aggregation threshold, and the `(b)(2)(ii)` "actual knowledge" residual-risk
condition.

But HIPAA Safe Harbor is not the only US regulation that can govern PHI
or PII in a clinical study data-sharing context. Statute has no mechanism
to research or surface:

- 45 CFR 46 (the Common Rule) — human-subjects research protections
- 42 CFR Part 2 — substance-use-disorder treatment record confidentiality
- FERPA — student education records
- The federal Privacy Act (5 U.S.C. § 552a) — PII held by federal agencies
- State law (e.g. California CMIA/CCPA) — can be stricter than the
  federal floor

Direction confirmed with Sir: fill this gap. Scope stays US-only (no
change to the EU/UK/IN/CA/BR stub jurisdictions or the "US-HIPAA only
until green" gate in CLAUDE.md — this is a coverage clarification within
`us`, not a jurisdiction expansion).

## Goals

- Statute researches and reports all five additional regimes above,
  alongside its existing HIPAA output, for the `us` jurisdiction.
- Advisory only: Judge, Sentinel, Publish Guard, and the bundle are
  untouched by this change. Judge/Sentinel have not yet been built to
  consume regulation data beyond HIPAA — that is separate, later work.
  This spec fills in Statute's own knowledge; wiring it into decisions
  is out of scope here.
- HIPAA behavior (existing prompt, schema, cache key, deterministic
  fallback) is unchanged. Nothing here touches
  `Statute.rules_for()`'s existing HIPAA path.
- CLAUDE.md is updated to describe what "US-HIPAA only" actually
  covers today.

## Non-goals

- No new deterministic identifier-detection patterns. These four
  regimes plus the state-law note are advisory citations, not new
  PHI/PII shapes for Publish Guard or the Sentinel hard-rules pass.
  `jurisdictions.py` is untouched.
- No Judge/Sentinel schema changes, no bundle.py changes. The new data
  rides the same path HIPAA data already takes (`session.results.agent_statute`,
  visible via `/results` and the agent-trace UI) and stops there for now.
- No per-state research. `session.jurisdiction` is country-level
  (`"us"`), not state-level, so state law cannot be researched
  per-state today. State law becomes one generic, explicitly
  non-exhaustive advisory note, not five entries.
- No expansion of country-level jurisdiction scope (EU/UK/IN/CA/BR
  stubs stay disabled, unrelated to this change).

## Architecture

`Statute.rules_for(jurisdiction)` keeps its existing HIPAA web-search
call exactly as it is today. A second, parallel web-search call
researches the four named regimes plus the state-law advisory note, and
the two results are merged before being cached and returned.

Running two calls in parallel (rather than one call covering six
regimes) mirrors the precedent already set by Ledger (Compare +
Aggregate) and Herald (Abstract + Sections): keep each individual LLM
call small enough to stay well under the 90s plain-call / 180s
web-search timeout, since a single call trying to research and cite six
distinct regimes risks running long or truncating output.

```
Statute.rules_for("us")
  ├── _hipaa_rules_for("us")        [existing, unchanged]
  └── _adjacent_regimes_for("us")   [new]
        │
        ▼  asyncio.gather
  merge → cache_put(both) → return combined dict
```

### New method: `_adjacent_regimes_for(jurisdiction)`

Cache-first, same pattern as the existing HIPAA path:

1. Check Mongo cache, topic `"adjacent_regulations"`, key `jurisdiction`
   (new cache topic, does not collide with existing `"regulation_rules"`).
2. On miss: `call_json_with_web_search`, prompting for the four named
   regimes with citations and a one-paragraph, explicitly-flagged
   non-exhaustive state-law advisory note (not per-state research).
3. On tool error/timeout: deterministic fallback from a
   `_ADJACENT_REGIMES_FALLBACK` constant in `experts.py` (same style as
   Praxis's `_DETERMINISTIC_METHODS` — a hand-authored, citation-accurate
   fallback so the pipeline never blocks on an external service).
4. `cache_put(db, "adjacent_regulations", jurisdiction, ...)`.

### Schema addition

Additive only — no existing key changes shape or meaning:

```json
{
  "adjacent_regimes": [
    {
      "name": "45 CFR 46 (Common Rule)",
      "citation": "45 CFR Part 46",
      "applicability": "Governs human-subjects research conduct (IRB review, informed consent) independent of HIPAA. Applies to the study process, not the de-identification transformation itself.",
      "advisory": "This system does not perform consent or IRB-review compliance checks. The study team remains responsible for Common Rule compliance outside this tool.",
      "sources": [{"url": "...", "title": "..."}]
    },
    { "name": "42 CFR Part 2", "...": "SUD treatment record confidentiality, stricter than HIPAA where applicable" },
    { "name": "FERPA", "...": "student education records" },
    { "name": "Privacy Act (5 U.S.C. § 552a)", "...": "PII held by federal agencies" },
    { "name": "State law (non-exhaustive)", "...": "e.g. CA CMIA/CCPA; can impose stricter requirements than the federal floor; site-specific counsel review recommended, not researched per-state by this system" }
  ]
}
```

## Manager / caching lifecycle (noted for later, not built here)

Sir flagged that the Manager (not Statute directly) should own *when*
this research runs and how its cache is refreshed:

- The Manager triggers Statute's regulation research (as it already
  mediates other specialist/expert calls).
- Once fetched, the result is used for the lifetime of the running
  local host process — i.e. it is not re-fetched mid-session, matching
  the existing weekly-refresh cache model.
- When the cache is invalidated/cleared (existing weekly TTL, or a
  future manual trigger), garbage collection of the stale cache entry
  should happen immediately, not lazily — clear then reclaim, not
  clear-and-leave.

This is explicitly deferred: it depends on the not-yet-built
`Manager.attach_statute()` / broker pattern (same shape as the planned
`attach_instrument`/`ask_instrument` work already on record for
Instrument). This spec only notes the requirement so it isn't lost;
implementing the Manager broker for Statute is a separate task.

## CLAUDE.md update

Replace the current line:

> Jurisdiction scope is **US-HIPAA only** until end-to-end runs are
> consistently green.

with:

> Jurisdiction scope is **US-only** until end-to-end runs are
> consistently green. Within `us`, Statute researches HIPAA Safe Harbor
> (45 CFR 164.514) plus adjacent PHI/PII regimes: the Common Rule (45
> CFR 46), 42 CFR Part 2 (SUD records), FERPA, and the federal Privacy
> Act (5 U.S.C. § 552a), with a non-exhaustive state-law advisory note.
> EU / UK / IN / CA / BR stubs live in `phi_core/jurisdictions.py` and
> stay disabled at the wizard level until Sir clears expansion.

## Testing

- New unit test: clear the `adjacent_regulations` cache entry, run
  `Statute.rules_for("us")` live, assert all four named regimes appear
  by name with a non-empty `citation`, and assert the state-law entry
  is present and its `advisory` text flags non-exhaustiveness.
- Existing HIPAA-path tests rerun unchanged to confirm no regression
  (`pytest -k "statute"`).
- Deterministic-fallback path tested by forcing the web-search call to
  raise, asserting `_ADJACENT_REGIMES_FALLBACK` content is returned
  instead of blocking.

## Invariants preserved

- Zero-row-read: this change is jurisdiction-level research, never
  touches dataset rows.
- Cache-first / web-search-second / deterministic-fallback-third: same
  three-tier policy as every existing Statute/Praxis call.
- No blocking on external service: fallback path guarantees a reply.
- HIPAA path byte-for-byte unchanged: existing consumers of
  `agent_statute.identifier_categories` / `.handling_rules` /
  `.age_aggregation_threshold` see no schema change.

## Known follow-up (explicitly out of scope here)

- `Manager.attach_statute()` / `ask_statute()` broker + cache lifecycle
  (clear-then-GC on refresh) — deferred, see above.
- Wiring `adjacent_regimes` into Judge/Sentinel decisions or the
  published bundle — deferred until Judge is built/redesigned.
