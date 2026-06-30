---
name: study-setup
description: Prepare a study for a publish run OUTSIDE the 10-phase DAG — pre-create the run directory tree and report readiness of the required inputs (forms manifest, study privacy config, raw datasets dir, PHI HMAC key); optionally bootstrap a fresh 0600 PHI key when none exists (never overwriting). Setup only, not a publish phase.
---

# Study Setup

> **Global Rule (GR-1):** No LLM — including Claude — may read dataset row values at any time, under any circumstance. Column headers (row 1) are the only permitted LLM dataset input. Failure reports carry pattern + column + count only, never a value.

## Core Rule

Setup is **not** a publish phase (Gap 4) — it never reads dataset rows and never
mutates published output. The PHI key is **never overwritten**: bootstrapping a
new key only happens when none exists, because overwriting would silently
invalidate every prior pseudonym and force full re-ingestion.

## What This Skill Does

Prepares a study so the orchestrator can run cleanly:

- pre-creates `output/<study>/` + per-run directories (`ensure_directories` +
  `ensure_run_directories`),
- reports readiness of each required input:
  `config/<study>/_forms_manifest.yaml`, `config/<study>/_study_privacy.yaml`,
  `data/raw/<study>/datasets/`, and the PHI HMAC key,
- with `--bootstrap-key`, creates a fresh 0600 HMAC key **only if absent**.

## Guided config authoring (Note 11)

The authoritative config (`_study_privacy.yaml` + `_forms_manifest.yaml`) and its
fail-closed phase-0 validation already exist; this skill adds the optional
front-end that **writes** them so a maintainer need not hand-edit YAML:

- `--interactive` — a Q&A wizard: offers jurisdictions (from the rulebook),
  compliance posture (from the scrub engine), `data_as_of`, and per-file
  **Required / Optional / Reject** with smart duplicate/superseded suggestions;
  validates each answer live; writes both YAMLs.
- `--write-config` — the same, non-interactive, from flags
  (`--jurisdictions`, `--compliance-posture`, `--data-as-of`, repeatable
  `--required`/`--optional`/`--reject`), for CI/scripted setup.

Both refuse to overwrite an existing config without `--force`. The wizard is a
guardrail (catch typos up front), not a gatekeeper — the pipeline still
re-validates at phase 0. It reads file NAMES + config only, never a row value.
The decision logic is pure/unit-tested in `scripts/wizard.py`; a separate
Streamlit UI wizard lives at `scripts/ai_assistant/ui/wizard.py`.

## CLI

```bash
# Readiness scaffold (default) + optional key bootstrap.
python plugins/report-ai-study-pipeline/skills/study-setup/scripts/run.py \
  --study <STUDY> [--run-id <RUN_ID>] [--bootstrap-key]

# Guided config authoring (Note 11).
python plugins/report-ai-study-pipeline/skills/study-setup/scripts/run.py \
  --study <STUDY> --interactive
python plugins/report-ai-study-pipeline/skills/study-setup/scripts/run.py \
  --study <STUDY> --write-config --jurisdictions USA,INDIA \
  --compliance-posture safe_harbor --data-as-of 2024-12-31 \
  --required 1_Enrollment.xlsx --reject "Paste Errors.xlsx"
```

Exit `0` when all required inputs are present (or config written); `1` when any
are missing (named in `readiness`); `2` on a config-authoring validation error.

## Result Contract

Emits one `RPLN_SKILL_RESULT:` marker line: a per-input present/absent readiness
map and a `key_created` flag — no secrets, no row values.

## Portability

Pure host-side Python; no LLM call, no network.

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | Ready — all required inputs present (readiness scaffold), or config written (`--interactive` / `--write-config`). |
| `1` | One or more required inputs missing (named in the result's `readiness` map). |
| `2` | Config-authoring validation error (invalid jurisdictions/posture/`data_as_of` or manifest inputs, or a refusal to overwrite without `--force`); also argparse usage error. |

## What This Skill Does NOT Do

- **Does not read dataset row values** — touches file NAMES, config YAML, and directory presence only (GR-1).
- **Does not run the pipeline** — only authors config and pre-creates the run-directory tree; it is not an orchestrator phase (Gap 4).
- **Does not overwrite the PHI HMAC key** — `--bootstrap-key` creates one only when none exists, because overwriting would invalidate every prior pseudonym.
- **Does not gatekeep config** — the wizard is a guardrail; the pipeline still re-validates the written config at phase 0.
