# Praxis multi-method report — design spec

Date: 2026-08-14
Status: approved, not yet implemented

## Problem

Praxis (the PHI transformation methods expert) today researches and
returns a single "best" technique per category
(`method_for(category) -> {technique, params, ...}`). Verification
(live run + independent blind sub-agent research, both 2026-08-14)
found two issues:

1. **Category-blind prompt.** `method_for()`'s web-search prompt sends
   only the bare category letter/key, with no description of what
   that category actually means (`f"Category: {category}. Web-search
   the current best-practice PHI transformation for this category..."`).
   Live-tested against category `E`, which this codebase's HIPAA pack
   (`jurisdictions.py:260`) defines as **fax numbers**: the LLM
   returned ZIP-code truncation and the Sweeney k-anonymity citation
   — category B's technique, not E's. It guessed a plausible HIPAA
   identifier from letter position rather than answering the category
   actually asked about.
2. **Single-answer shape doesn't match the real task.** Sir's stated
   intent: Praxis should surface the *set* of current methods for a
   category (e.g. for date-jittering: SANT, simple per-patient random
   offset, HMAC-derived deterministic offset), each with what it is,
   why it preserves clinical/analytical utility, and how to apply it
   — a report for a later selection step (Judge, or a future
   method-choice feature), not a single pre-decided answer.

## Goals

- `method_for(category)` returns a list of candidate methods per
  category, each self-contained (name, how to apply, why it's used —
  including what utility it preserves and how, params, reference,
  sources), for categories where more than one method genuinely
  applies.
- Fix the category-blind prompt: the LLM is told what the category
  actually is before it researches methods for it.
- Categories with no real method alternative (drop-only) stay on the
  existing fast deterministic path, unchanged in shape.
- Cache-first / web-search-second / deterministic-fallback-third
  policy is preserved for every category.

## Non-goals

- No selection logic. Praxis reports candidates; picking one is a
  separate, later concern (Judge is not built yet — this spec does
  not touch Judge, Sentinel, Executor, or the bundle).
- No fixed cap on method count. Praxis reports however many distinct
  methods the search turns up for that category, not a padded or
  truncated top-N.
- No two-call split (unlike the Statute adjacent-regimes design).
  `call_json_with_web_search` already allows `max_uses=3` searches
  inside one call, which is enough headroom to gather several
  candidate methods without a second round-trip.
- No change to Statute or the deterministic identifier-category
  patterns in `jurisdictions.py`.

## Architecture

### Category routing

| Category | Path | Reasoning |
|---|---|---|
| A, D, F, G (name, phone, SSN, and similar drop-only categories) | Deterministic, unchanged | No real alternative to `drop` — no clinical signal to preserve, multi-candidate research is wasted work. |
| B, C, H | **Moved** from "skip search, return deterministic dict" to web-search multi-method | Real method choices exist: geographic-generalization variants for B, date-jittering/shifting/SANT for C, hashing/pseudonymization variants for H. Their current `_DETERMINISTIC_METHODS` entries become the fallback-on-error content instead of the primary path. |
| E, I–R | Already search-based; same call, new schema | No routing change, only prompt + schema change. |

### `method_for(category)` flow (unchanged tiers, new payload shape)

```
1. cache_get(db, f"phi_method:{category}", "generic")
   → hit: return cached list
2. category in {A, D, F, G}:
   → deterministic single-technique dict, wrapped as a one-entry
     methods list for shape consistency, cached, returned
     (no search call — same free/instant path as today)
3. category in {B, C, H} or {E, I..R}:
   → call_json_with_web_search(prompt naming the category's real
     description, asking for a list of current methods)
   → on tool error: deterministic fallback
        - B/C/H: existing _DETERMINISTIC_METHODS[category] entry,
          wrapped as a one-entry methods list
        - E/I..R: existing generic single-entry fallback, wrapped
          the same way
   → cache_put, return
```

### Prompt fix

Before:
```python
prompt = f"Category: {category}. Web-search the current best-practice PHI transformation..."
```

After:
```python
description = pack.identifier_categories.get(category, category)
prompt = (
    f"Category: {category} ({description}), under HIPAA Safe Harbor "
    f"(45 CFR 164.514(b)(2)(i)). Web-search and list the current "
    f"methods used to transform this category of PHI so the data "
    f"stays usable for research (e.g. linkable, analyzable, "
    f"comparable) without exposing the real value. For each method "
    f"return how to apply it, why it preserves utility, and its "
    f"params. Stay within Safe Harbor-compatible techniques unless "
    f"a method requires Expert Determination — if so, say so "
    f"explicitly in that method's ``why`` field."
)
```

The explicit Safe Harbor framing responds to a second finding from
the blind sub-agent verification: date shifting is a valid
de-identification technique, but only under HIPAA Expert
Determination (45 CFR 164.514(b)(1)), not Safe Harbor, which requires
plain year-generalization for dates. Without this line, a
multi-method report could list an Expert-Determination-only method
next to Safe-Harbor methods with no distinction, which is exactly the
kind of gap a downstream consumer (Judge, or a human reviewer) needs
flagged rather than discovered later.

### Schema

```json
{
  "category": "C",
  "methods": [
    {
      "name": "SANT (Statistical Age/date Noise Technique)",
      "how_to_apply": "concrete steps",
      "why": "what utility this preserves and how; Safe Harbor / Expert Determination note if applicable",
      "params": {"...": "..."},
      "utility_preserving": true,
      "clinical_impact": "...",
      "reference_paper": "...",
      "sources": [{"url": "...", "title": "..."}]
    },
    {"name": "simple per-patient random offset (±N days)", "...": "..."},
    {"name": "HMAC-derived deterministic offset", "...": "..."}
  ],
  "as_of": "YYYY-MM-DD"
}
```

Replaces the current flat `technique`/`params`/`utility_preserving`/
`clinical_impact`/`reference_paper`/`sources` top-level fields. This
is a clean break, not a migration: nothing currently consumes
Praxis's output (Judge is not built), so there is no caller to keep
compatible.

`agent.run(categories)` (`Praxis.run()`) is unchanged in shape —
still `{"methods": {category: <method_for result>}}` — only the
per-category value's internal shape changes from a single dict to
`{"category", "methods": [...], "as_of"}`.

## Testing

- Unit test: clear the `phi_method:C` cache entry, run
  `method_for("C")` live, assert `methods` is a non-empty list and
  every entry has non-empty `name`, `how_to_apply`, `why`, `params`.
- Unit test: assert categories A/D/F/G still return their existing
  deterministic single-entry shape (wrapped as a one-entry list),
  with no search call made (mock/spy on `call_json_with_web_search`
  to confirm it's not invoked).
- Unit test: force the web-search call to raise for category B,
  assert the existing `_DETERMINISTIC_METHODS["B"]` content is
  returned as a one-entry `methods` list instead of blocking.
- Regression: existing Praxis tests updated for the new `methods`
  list shape (`pytest -k "praxis"`).
- Bug-fix regression: assert the prompt sent to
  `call_json_with_web_search` for a given category includes that
  category's `pack.identifier_categories` description text, not just
  the bare letter/key.

## Invariants preserved

- Zero-row-read: this is category-level method research, never
  touches dataset rows.
- Cache-first / web-search-second / deterministic-fallback-third:
  same three-tier policy, same cache key shape
  (`f"phi_method:{category}"`, `"generic"`).
- No blocking on external service: fallback path guarantees a reply
  for every category.
- A/D/F/G stay free/instant (no search call), unchanged from today.

## Known follow-up (explicitly out of scope here)

- Wiring `methods` (candidate list) into a selection step — Judge is
  not built yet. When it is, Judge (or a future method-choice UI
  feature, per Sir's "maybe later on we could have a feature where we
  choose a method based upon what is needed") picks from this list;
  this spec only produces the list.
- Manager-broker mediation of Praxis calls (same deferred shape noted
  in the Statute adjacent-regimes spec) — not addressed here.
