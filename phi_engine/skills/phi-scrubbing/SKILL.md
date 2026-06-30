---
name: phi-scrubbing
description: Run the fail-closed per-form PHI scrub over the staged dataset JSONL — date jitter, ID pseudonymize, drop/generalize/band, force-drop, small-cell suppression — rewriting rows in place, quarantining un-scrubbable rows, and emitting count-only per-dataset audit ledgers. Defaults to partial-publish-on-review; --strict restores strict-abort. Trusted code path: handles row values internally, emits counts/metadata only.
---

# PHI Scrubbing

> **Global Rule (GR-1):** No LLM — including Claude — may read dataset row values at any time, under any circumstance. Column headers (row 1) are the only permitted LLM dataset input. Failure reports carry pattern + column + count only, never a value.

## Core Rule

This is the **trusted scrub code path** — it does read and rewrite dataset row
values internally, because scrubbing PHI requires touching the data. It never
exposes those values: rows are rewritten in place in staging (never the LLM read
zone), and every artifact it emits (audit ledgers, scrub-outcome sidecar) is
**counts / field-names / reason-codes only**. The scrub is **fail-closed** — an
un-scrubbable row is never promoted.

**Caller, not re-implementer (Note 15, Conflict 1).** The scrub orchestration may
only **call existing `scripts.security.phi_scrub` functions** — it must never
re-implement the HMAC, receive raw key bytes, or access the `PHIKeyStore`
directly. The key is loaded by the single role-gated loader
(`phi_scrub.load_key()` / `get_phi_key()`), used inside the trusted engine, and
zeroized (`clear_phi_key()`) after the scrub; no caller ever holds the raw key.

## What This Skill Does

The fail-closed per-form PHI scrub (Phase 4). It wraps
`scripts.security.phi_scrub.run_scrub` over the staged dataset JSONL, applying
the priority-ordered scrub rules: priority-0 force-drop (direct identifiers
flagged by classification) → keep → birthdate → drop → cap → generalize → band →
suppress_small_cell → date jitter → id pseudonymize. It also:

- jitters dates per-subject (keyed off subject_id so visit intervals survive),
- pseudonymizes the subject ID (`RID_<LABEL>_<alpha12>`, required for linkage),
- quarantines un-scrubbable / orphan (no resolvable subject_id) rows to the
  AMBER no-LLM `staging/quarantine/` zone — never promoted,
- emits the per-dataset PHI audit ledger and, when a `run_id` is supplied, the
  `runs/<run_id>/scrub_outcome.json` sidecar (per-form kept/quarantined counts +
  reason codes + `elevated` flag).

**Modes.** Defaults to **partial-publish-on-review** (one bad form quarantines
only its failing rows and is flagged `elevated`; the rest of the study still
publishes). `--strict` restores strict-abort: the first un-scrubbable row raises
a `PHIScrubError` and aborts the whole study.

## CLI

```bash
uv run --all-groups python \
  plugins/report-ai-study-pipeline/skills/phi-scrubbing/scripts/run.py \
  --study <STUDY> --run-id <RUN_ID> --run-dir <output/<STUDY>/runs/<RUN_ID>>
```

Flags: `--strict` (strict-abort on the first un-scrubbable row; default is
partial-publish-on-review).

Exit `0` when the scrub completes (including partial-publish with quarantined
rows); `1` when it fail-closes (`PHIScrubError`, e.g. an unmappable
band/generalize value, an unshiftable date, a missing scrub config, or a
strict-mode abort).

## Result Contract

Emits one `RPLN_SKILL_RESULT:` marker line (`scripts/utils/skill_protocol.py`):
value-free — study name, mode, run id, and on failure the `PHIScrubError`
subclass NAME only, never a row value.

## Key Rotation & KMS/HSM Upgrade Path

The PHI HMAC key is the most sensitive non-PHI artifact: it deterministically
produces every `RID_<LABEL>_<alpha12>` pseudonym and every per-subject date-jitter
offset, so changing it (rotation) breaks cross-run linkage and invalidates every
prior snapshot. Rotation is therefore a **gated, explicit operation, never
silent**.

- **Pre-scrub hard stop.** Before any row is scrubbed, the publish path compares
  the current key fingerprint (SHA-256 of the 32 raw bytes) against the one
  recorded for the study. On a change it writes a value-free rotation audit entry
  under `output/{STUDY}/audit/key_rotation_events/` (previous fingerprint, new
  fingerprint, UTC date, run id, effect) and **aborts** with
  `KeyRotationRequiresConfirmationError` unless the operator explicitly confirms
  via `--confirm-rotation` or `REPORTAL_CONFIRM_KEY_ROTATION=1`. Confirming forces
  a full re-scrub; all prior snapshots are deprecated.
- **First run / unchanged** are clean no-ops. The recorded fingerprint only
  advances after a successful publish, so an aborted rotation never updates state.
- **Value-free.** Only one-way fingerprints ever appear in audit/lineage — raw
  key bytes are never logged, never a CLI argument, and never exposed to any LLM.

**Production upgrade path (KMS/HSM).** The file-based key at
`$XDG_CONFIG_HOME/report_ai_portal/phi_key` (`0600`) is the correct research
baseline. For production, escalate to a managed service — **AWS KMS, Azure Key
Vault, GCP Cloud KMS, or an HSM**. `phi_scrub.load_key()` (funneled through the
role-gated `PHIKeyStore`) is the single loader to re-point at a KMS-backed fetch;
the fingerprint semantics (`sha256` of the 32-byte key) stay identical, so all
rotation/staleness comparisons and audit evidence remain unchanged.

## Portability

Pure host-side Python; fail-closed, no LLM call, no network. Invoked by the
orchestrator as a file-path subprocess and runnable from any LLM host the same
way.

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | Scrub completed — including partial-publish-on-review (failing rows quarantined, the rest published). |
| `1` | Scrub fail-closed (`PHIScrubError` subclass — e.g. an unmappable band/generalize value, an unshiftable date, a missing scrub config, or a strict-mode abort); the subclass NAME only is reported. |
| `2` | Argparse usage error (e.g. missing `--study`). |

## What This Skill Does NOT Do

- **Does not decide policy** — it consumes `phi_handling_approval.json` and the scrub config; classification decisions are made upstream.
- **Does not promote to `llm_source/`** — it rewrites staged JSONL in place; the publish supervisor promotes the scrubbed tree.
- **Does not re-implement the HMAC or hold the raw key** — it only calls the role-gated `phi_scrub` loader, and the key is zeroized after the scrub (Note 15).
- **Never publishes an un-scrubbable row** — fail-closed: such rows are quarantined to the no-LLM zone, never the read zone.
