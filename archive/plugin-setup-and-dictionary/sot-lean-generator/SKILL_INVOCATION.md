# P1b: SoT Lean Outputs — Direct Import Invocation

## Invocation Strategy

Unlike P1c (dictionary-to-llm-source), P2 (deduplication), P2b (header-extraction), P3 (sot-lean-generator subprocess CLI), and the publish supervisor (dataset-to-llm-source), the **P1b SoT lean outputs phase is invoked via direct Python import**, not as a subprocess.

**Location:** `plugins/report-ai-study-pipeline/skills/report-ai-study-pipeline/scripts/run.py`, lines 765–776.

**Invocation:**

```python
from scripts.source_truth.generate_lean_outputs import main as generate_lean_outputs_main

sot_rc = generate_lean_outputs_main(
    ["--study", study, "--repo-root", str(config.BASE_DIR), "--run-dir", str(run_dir)]
)
```

## Rationale

1. **No subprocess overhead**: SoT generation is deterministic (no LLM calls, no network by default); direct import eliminates subprocess marshalling and re-importing the codebase.
2. **Timing dependency on Phase 1b headers**: The header store (built by Phase 1) is read by Phase 1b SoT generation; direct import ensures both are in the same process space, making any transient timing issues visible as crashes rather than silent cache misses.
3. **Shared lock**: The orchestrator holds the per-study lock for the entire run; P1b runs under that lock, so no re-acquisition is needed.

## Contract

- **Entry point:** `scripts.source_truth.generate_lean_outputs.main(argv: list[str]) -> int`
- **Exit code 0:** SoT generation succeeded (all PDF-backed forms have joined query views in `llm_source/SoT/<pair>/joined/`).
- **Exit code non-zero:** SoT generation failed (missing source pairs, verifier failures, or file I/O errors written to audit/human_review).
- **No subprocess result marker:** P1b does not emit `RPLN_SKILL_RESULT:` (it is not invoked via `invoke_skill`); the orchestrator reads the exit code directly.

## Anti-Drift Note

If P1b is refactored to run as a subprocess (e.g., to parallelize or isolate resource limits), the invocation method must be updated here and in `tests/skills/test_invocation_contract.py`. The contract — entry point signature, exit codes, and file outputs — must remain stable.
