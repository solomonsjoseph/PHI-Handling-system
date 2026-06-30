---
name: audit-verification
description: Run the 17-assertion post-publish verifier for a completed run — manifest reconciliation, ledger hashes + no-LLM sentinel, quarantine-empty, PHI absence scan, decided-vs-applied protection lattice, SoT joined-view presence, and ledger coverage + entry-field completeness — with the canonical exit-code routing. Exit codes only, never row values.
---

# Audit Verification

> **Global Rule (GR-1):** No LLM — including Claude — may read dataset row values at any time, under any circumstance. Column headers (row 1) are the only permitted LLM dataset input. Failure reports carry pattern + column + count only, never a value.

## Core Rule

The verifier is **fail-closed**: any failed assertion holds the run (writes
`verifier_passed: false`) and returns a non-zero exit code that the orchestrator
treats as a hard stop. A clean pass is the precondition for committing an
immutable snapshot.

## What This Skill Does

Phase 9 of the publish pipeline. Delegates to the trusted
`extract_to_llm_source verify` path, which owns the assertion suite and its
exit-code routing. The 17 assertions (executed 1→12, 14, 15, 16, 17, 13) cover:

| Area | Assertions | Exit |
|---|---|---|
| Manifest existence / reconciliation / JSONL presence | 1, 2, 10 | 2 |
| Ledger hash + no-LLM sentinel | 5, 6 | 3 |
| Quarantine empty | 7 | 4 |
| PHI absence scan / forbidden runtime keys | 8, 9 | 5 |
| Pipeline lock absent | 11 | 6 |
| Staging absent / attestation valid | 3, 4 | 7 |
| Decided-vs-applied (protection lattice) | 12 | 9 |
| Cap application complete (no un-capped age > threshold in output) | 17 | 5 |
| SoT joined-view present (sole LLM-facing SoT file) | 15 | — |
| Ledger coverage + entry-field completeness | 14, 16 | 10 |
| status.json update (terminal) | 13 | — |

## CLI

```bash
python plugins/report-ai-study-pipeline/skills/audit-verification/scripts/run.py \
  --study <STUDY> --run-id <RUN_ID>
```

Exit `0` only when every assertion passes; otherwise the routed code above.

## Result Contract

Emits one `RPLN_SKILL_RESULT:` marker line carrying the verifier exit code —
no row values.

## Portability

Pure host-side Python; no LLM call, no network.

## Exit Codes

Forwarded verbatim from the `extract_to_llm_source verify` path:

| Code | Meaning |
|---|---|
| `0` | All assertions passed (`verifier_passed: true`). |
| `2` | Manifest existence / reconciliation / required-JSONL failure (assertions 1, 2, 10); also argparse usage error. |
| `3` | Ledger hash null or no-LLM sentinel missing (assertions 5, 6). |
| `4` | Quarantine directory non-empty (assertion 7). |
| `5` | PHI absence / runtime-key / SoT joined-view / status-write failure (assertions 8, 9, 15, 13). |
| `6` | Run could not be resolved, or the pipeline lock is present (assertion 11). |
| `7` | Staging not destroyed / destruction attestation invalid (assertions 3, 4). |
| `9` | Decided-vs-applied protection-lattice mismatch (assertion 12). |
| `10` | Ledger coverage or entry-field completeness failure (assertions 14, 16). |

## What This Skill Does NOT Do

- **Does not modify the tree** — read-only assertions; it only writes the value-free `verifier_report.json` and a count-only human-review note on failure.
- **Does not read dataset row values** — the PHI-absence scan reports pattern + path + line number + count only, never a matched value (GR-1).
- **Does not own the assertions** — it delegates to the trusted `extract_to_llm_source verify` path, which holds the assertion suite and exit-code routing.
- **Does not re-publish, snapshot, or repair** — a failed assertion holds the run; remediation is a separate step.
