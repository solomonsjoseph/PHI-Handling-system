---
name: phi-classification
description: Run the header-only PHI handling review before any row value is opened — classify each form's headers by jurisdiction (USA/INDIA), cross-verify direct identifiers against the SoT, and write the authoritative phi_handling_approval.json (approved vs held forms, force-drop columns) consumed downstream by the scrub. Header NAMES + counts only, never row values.
---

# PHI Classification

> **Global Rule (GR-1):** No LLM — including Claude — may read dataset row values at any time, under any circumstance. Column headers (row 1) are the only permitted LLM dataset input. Failure reports carry pattern + column + count only, never a value.

## Core Rule

This skill runs **before any dataset row value is opened**. It classifies on
column NAMES and SoT (printed-PDF) signals only — never row values. Its output,
`phi_handling_approval.json`, is the authoritative decision record the scrub
later applies; the approval report itself is serialization-guarded so it can
never become a PHI side-channel.

## What This Skill Does

The deterministic jurisdiction PHI-classification gate (Phase 3 / 3b,
header-only). It reaches the trusted host gate
`extract_to_llm_source._run_form_approval_gate`, which for the study's
manifest-approved forms:

- classifies each header by jurisdiction (e.g. `USA`, `INDIA`) into `keep` /
  `drop` / `pseudonymize` / `jitter_date` / `generalize` / `cap` / `suppress`
  decisions,
- cross-verifies direct identifiers against the SoT (printed-PDF question
  signal) so a name/SoT-flagged escapee is force-dropped at the column level,
- records each form as **approved** (publishes, possibly with force-drop
  columns) or **held** (PHI-classification uncertainty → human review), a normal
  partial-publish outcome, not an error,
- writes the authoritative `phi_handling_approval.json` into the run dir.

The adversarial classification probe runs **once** against a frozen rule bundle
(deterministic; re-running cannot change the result), so a held form is a
rule-pattern gap to fix, not a transient condition to retry.

## AI header→rule alignment (opt-in, Note 9)

When `REPORTAL_PHI_ALIGNMENT_ENABLED=1` (default **off**), the classifier adds an
AI alignment pass for the **uncovered set** — headers classified `keep` that
matched no deterministic pinned rule. For each, the AI reads the **column NAME
only** (GR-1 — never a row value) plus the value-free rulebook, infers the
variable's nature (e.g. `b_dat`/`birdat` → birth date), and proposes a binding to
one rulebook rule's **action** + a recognizing regex. The rulebook defines the
policy; the AI only decides which rule a fuzzy-named column maps to.

Every proposal is **deterministically verified** (`scripts/security/phi_alignment.py`):
the action must be a real existing one (`drop`/`jitter_date`/`pseudonymize`/`suppress`),
the regex must compile, match its own header, and not be a catch-all, the action
must agree with the cited rulebook rule, and the citation must be an official
source. Up to **3 attempts**, then the header goes to human review. A verified
alignment can only **upgrade** `keep` to a stronger action (it never weakens —
pinned rules remain the floor), and the aligned rules are frozen into
`runs/<RUN_ID>/phi_scrub.generated.yaml` (captured in the snapshot) so the
deterministic `run_scrub` engine applies them and re-runs are reproducible.

## CLI

```bash
uv run --all-groups python \
  plugins/report-ai-study-pipeline/skills/phi-classification/scripts/run.py \
  --study <STUDY> --run-id <RUN_ID> --run-dir <output/<STUDY>/runs/<RUN_ID>>
```

Flags: `--form <FORM>` (repeatable; omit for all manifest forms),
`--max-workers <N>` (cap the header-review thread pool; default auto).

`--run-dir` is required (the approval file is written there); exit `2` if it is
missing. Exit `0` on success — including when some forms are held (partial
publish is a normal outcome, so the result is reported `ok`).

## Result Contract

Emits one `RPLN_SKILL_RESULT:` marker line (`scripts/utils/skill_protocol.py`):
value-free — approved/held form NAMES, counts, and the `partial` flag only,
never a row value.

## Portability

Pure host-side Python; deterministic by default (no LLM call, no network).
**Only** when AI alignment is opted in (`REPORTAL_PHI_ALIGNMENT_ENABLED=1`) does
it call an LLM — header NAMES + the value-free rulebook only, never a row value,
and every proposal is deterministically verified before use. Invoked by the
orchestrator as a file-path subprocess and runnable from any LLM host the same
way.

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | Classification gate completed — including when some forms are held (partial publish is a normal outcome, reported `ok`). |
| `2` | `--run-dir` was not supplied (the approval file is written there); also argparse usage error. |

## What This Skill Does NOT Do

- **Does not read row values or scrub** — it classifies on column NAMES and SoT (printed-PDF) signals only and emits header-only decisions (GR-1).
- **Does not apply the decisions** — it writes the authoritative `phi_handling_approval.json`; the phi-scrubbing skill consumes and applies it.
- **Does not weaken protection** — even with AI alignment opted in, a verified alignment can only upgrade `keep` to a stronger action; pinned rules remain the floor.
- **Does not retry held forms** — the deterministic probe runs once against a frozen rule bundle; a held form is a rule-pattern gap to fix, not a transient condition.
