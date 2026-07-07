---
name: phi-rulebook
description: Resolve and inspect the jurisdiction PHI rulebook (HIPAA Safe Harbor + India DPDPA) with a versioned offline cache, a committed airgapped seed, and drift detection. Use when the user asks about which PHI rules are active, the rulebook version/hash, rule drift, or wants to refresh/inspect jurisdiction classification rules. Metadata only — never reads study data.
---

# PHI Rulebook

> **Global Rule (GR-1):** No LLM — including Claude — may read dataset row values at any time, under any circumstance. Column headers (row 1) are the only permitted LLM dataset input. Failure reports carry pattern + column + count only, never a value.

## Core Rule

This skill operates on **rule metadata only** — jurisdiction rule ids, actions,
reasons, official-source URLs, and SHA-256 hashes. It never reads any study
dataset value, row, or header content.

## What This Skill Does

Resolves the active PHI jurisdiction rulebook for a study and records its
provenance, so an IRB reviewer can confirm exactly which rule set produced a
publish, and an operator is warned when the rules change.

The rulebook engine lives in the host repo at
`scripts/security/phi_rulebook.py`; it wraps the pinned jurisdiction rules in
`scripts/security/phi_review.py` with three guarantees:

- **Versioned offline cache** — each resolved bundle's provenance is persisted
  to `output/{STUDY}/audit/phi_rulebook/rulebook_v{N}_{JURIS}.json` (audit zone,
  no LLM access).
- **Committed airgapped seed** — `config/_defaults/phi_rulebook/` holds a v1 seed
  per supported jurisdiction set so the first run in a network-isolated
  environment still has a known-good baseline. The classification rules
  themselves are pinned in code, so the engine always works offline.
- **Drift detection** — the freshly built `rules_sha256` is compared to the
  cached/seed baseline; a mismatch is surfaced so a rule-set change (code update
  or live-source update) is never silent.

## Live rule extraction (opt-in, N7)

When `REPORTAL_RULEBOOK_AI_EXTRACT=1` **and** `--allow-network` **and** the
study's `rule_refresh: online_preferred`, the engine fetches the **latest**
official regulation text per jurisdiction (allowlisted gov HTTPS hosts only —
HHS/eCFR/India Code/ICMR/UIDAI/MeitY), has the AI **extract structured rules from
that PUBLIC text** (never any study data — GR-1), and **merges them OVER the
pinned floor**. Default off → the deterministic pinned path is unchanged.

Guarantees (AI proposes, determinism disposes):

- **Verified** (`verify_extracted_rules`): each rule's id is namespaced
  `live_<juris>_*` (cannot shadow a pinned rule), its action is in the Action
  enum, its source is official, and its patterns compile, are word-anchored, and
  do not match a benign-clinical probe set (rejects over-broad regexes).
- **Floor preserved** — additive merge + strictest-wins means an extracted rule
  can only *add or strengthen* protection; `detect_protection_weakening` asserts
  the merged bundle never lowers a pinned decision (flagged prominently if it
  somehow did).
- **Reuse-if-unchanged** — the v2 live cache records per-source freshness hashes;
  when every fetched source is unchanged, the cached verified rules are reused
  with **no LLM call** (and no cost). A changed/new source is re-extracted.
- **Offline / unverifiable** → falls back to the pinned floor with an
  `offline_warning`. `REPORTAL_RULEBOOK_REQUIRE_LIVE=1` makes that a fail-closed
  error instead (for environments that must use live rules).
- **Reproducible** — the run records the rule-set `rules_sha256`; the snapshot
  captures it, and a content change surfaces as a `rulebook_update` staleness
  finding.

## CLI

```bash
# Resolve the active rulebook for a study (offline / pinned by default).
uv run --all-groups python -m \
  phi_engine.skills.phi-rulebook.scripts.rulebook_cli \
  resolve --study Indo-VAP

# Inspect a committed seed rulebook for a jurisdiction set.
uv run --all-groups python -m \
  phi_engine.skills.phi-rulebook.scripts.rulebook_cli \
  show --jurisdictions INDIA,USA

# Fetch latest official regs + AI-extract rules (opt-in; needs --allow-network
# and REPORTAL_RULEBOOK_AI_EXTRACT=1).
uv run --all-groups python -m \
  phi_engine.skills.phi-rulebook.scripts.rulebook_cli \
  refresh --study Indo-VAP --allow-network
```

Exit codes: `0` resolved (no drift) · `3` resolved but DRIFT detected (confirm
the rule-set change) · `4` (`refresh`) live extraction flagged a
protection-weakening rule (review) · `2` usage/config error.

## Result Contract

Unlike the DAG-node skills, `rulebook_cli` is a shared-module operator command,
not an orchestrator subprocess — it does **not** emit an `RPLN_SKILL_RESULT:`
marker. It prints a JSON provenance object to stdout: `rules_sha256`, the
resolved jurisdictions, the cache/seed `source`, and `drift_detected` (and, for
`refresh`, the live-extraction status). The object carries rule **metadata** only
— ids, actions, reasons, official-source URLs, and hashes — never a study dataset
value, row, or header content. The exit code (below) is the machine-readable
drift/weakening signal; the printed JSON is the human/IRB evidence.

## Portability

The engine is platform-neutral host-repo code; this skill is the thin operator
command surface. Any LLM host reads this `SKILL.md`; `agents/llm.yaml` carries
the short-form adapter metadata.

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | Rulebook resolved with **no drift** (`resolve`/`refresh`), or a seed was inspected (`show`). |
| `2` | Usage/config error — bad arguments, missing/invalid privacy config, or no committed seed for the requested jurisdiction set. |
| `3` | Rulebook resolved but **DRIFT detected** — the freshly built `rules_sha256` differs from the cached/seed baseline; confirm the rule-set change. |
| `4` | `refresh` live extraction flagged a **protection-weakening** rule — review before use (the deterministic pinned floor is never lowered silently). |

## What This Skill Does NOT Do

- **Never reads study data** — operates on rule metadata only (ids, actions, reasons, official-source URLs, SHA-256 hashes); never a dataset value, row, or header content (GR-1).
- **Is not an orchestrator DAG node** — the underlying primitive it wraps (`phi_review.refresh_jurisdiction_rules`) is what `phi_engine.pipeline.run.run_pipeline`'s classification step calls directly (pinned-only, no network); this skill's own caching/drift-detection layer (`resolve_rulebook`) is an operator CLI for out-of-band rulebook inspection, not something the pipeline invokes. It does not emit a SkillResult marker and is not a publish phase.
- **Does not fetch the network by default** — live extraction is opt-in (`REPORTAL_RULEBOOK_AI_EXTRACT=1` + `--allow-network` + `rule_refresh: online_preferred`); the default path is the deterministic pinned/offline rulebook.
- **Cannot lower protection** — AI-extracted live rules only add or strengthen decisions (additive merge, strictest-wins); a weakening is flagged (exit `4`), never applied silently.
