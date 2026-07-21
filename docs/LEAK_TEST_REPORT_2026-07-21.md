# PHI Leak Test Report — 2026-07-21

Synthetic-data leak test executed this session against both egress channels: the file-based publish pipeline (intake -> organize -> run -> publish) and the local-model routing path identified as a gap in `docs/PRIVACY_GATEWAY_RECOMMENDATION.md`. Every command below was run live this session; every number is measured, not carried forward from a prior report.

## Result summary

| Channel | Leak found? | Evidence |
|---|---|---|
| File-based publish pipeline (intake/organize/run/publish) | **No** | 0/66 planted synthetic identifiers reached the published tree, reproduced twice with independently seeded fixtures |
| Local-model routing, CONFIDENTIAL-sensitivity prompts (`resolve_confidential_header`, `extract_support_signals` CONFIDENTIAL branch) | **Yes** | Planted SSN-shaped marker reached the local-model transport with zero PHI-gate invocations, on both call sites, reproducibly |

## 1. File-based publish pipeline — clean

### Method
Fresh, uniquely-named, pre-verified-empty workspace per run (no reused state):

```
WS=/tmp/pg-leakcheck-<ts>-ws ; SRC=/tmp/pg-leakcheck-<ts>-src
ls "$WS"                                                     # confirmed: No such file or directory (genuinely fresh)
python -m harness.make_stress_fixtures --out "$SRC" --seed 999
python -m phi_engine intake    --study PGLeak2 --source "$SRC" --workspace "$WS"
python -m phi_engine organize  --study PGLeak2 --workspace "$WS"
python -m phi_engine run       --study PGLeak2 --jurisdiction us --workspace "$WS"
grep -rlE '[0-9]{3}-[0-9]{2}-[0-9]{4}' "$WS/output/PGLeak2/llm_source"
```

### Result (this run, run_id `20260721T034143Z`)
- Stress fixture tree: 66 files (planted with SSNs, MRNs, phones, addresses, dates, names, adversarial encodings — homoglyph, zero-width, base64, split-secret, quasi-identifier combinations, etc.)
- Intake: `66 linked, 0 duplicates, 1 errors` (the 1 error is the deliberately-planted broken symlink, correctly rejected, not silently skipped)
- Organize: `6 datasets, 60 in review bucket`
- Run: `exit_code=8`, `guard_ok=true`, `guard_failed=false`, `published_count=6`, `review_queue_size=60`
- SSN-shaped grep against the entire published `llm_source` tree: **exit code 1 — zero matches**

Reproduced a second time (seed 42, `run_id 20260721T034028Z`) with identical structural results before the guaranteed-fresh rerun above. Both runs: 0 leaked identifiers.

### Why it's clean
`phi_guard_gate.py:65`: `ok = presidio.ok and legacy.ok` — publish requires **both** the Presidio-backed and legacy-regex scanners to be clean; either one flagging holds the release (`run.py:1570: if guard_ok: publish`). Confirmed structurally correct by direct read, confirmed empirically by two independent zero-leak runs.

## 2. Local-model routing, CONFIDENTIAL path — leaking

### Method
`harness/leak_probe_confidential_local_model.py` (permanent, rerunnable):

```
source .venv/bin/activate && python3 harness/leak_probe_confidential_local_model.py
```

It exercises the **real production code path** (not a stub): it reuses the exact `ConfidentialHeaderTask`/`SupportSignalTask`/payload-builder shapes from `tests/test_model_routing.py` that are already proven to pass the router's own verification checks in the passing test suite. Instrumentation:
1. Wraps the module-level `phi_gate_check` name inside `model_routing` with a call-recording spy.
2. Replaces `_local_completion_transport` with a capture stub that records the exact prompt string handed to the local-model transport and returns a valid contract response, so the call completes successfully (this is a live leak, not an incidental exception path).
3. Plants a unique marker, `LEAK-PROBE-MARKER-SSN-078-05-1120`, in the fields documented as "not headers-only": `ConfidentialHeaderTask.samples` and `MatchedSupportCell.value`.

### Result (reproduced twice, identical both times)

```json
{
  "marker_planted": "LEAK-PROBE-MARKER-SSN-078-05-1120",
  "probes": [
    {
      "probe": "resolve_confidential_header (model_routing.py:968-982)",
      "gate_invocations": 0,
      "prompts_sent_to_local_transport": 1,
      "marker_reached_transport_unscanned": true,
      "transport_call_succeeded": true
    },
    {
      "probe": "extract_support_signals CONFIDENTIAL branch (model_routing.py:984-1008)",
      "gate_invocations": 0,
      "prompts_sent_to_local_transport": 1,
      "marker_reached_transport_unscanned": true,
      "transport_call_succeeded": true
    }
  ],
  "control_same_marker_through_wired_gate": {
    "blocked": true,
    "detail": "ModelResponseError: prompt_gate_blocked"
  }
}
```

### Exact leak location
`phi_engine/security/model_routing.py`:
- `resolve_confidential_header` (lines 968-982): builds `prompt` from `_header_task_payload(task)` (which embeds `task.samples` verbatim, `model_routing.py:360`) and passes it straight to `self._complete_json(self._local_client, prompt, ...)` — **no `phi_gate_check` call anywhere on this path**.
- `extract_support_signals` (lines 984-1008), CONFIDENTIAL branch: same prompt-to-local-client path with no gate call. Gating (`self._gate_ordinary_segments(prompt)`, line 1003) only executes inside the `if task.sensitivity is Sensitivity.NON_CONFIDENTIAL:` branch — the branch that does **not** apply to CONFIDENTIAL data, which is the sensitivity level that most needs it.
- No response-side scan either: grepped `model_routing.py` for `guard_llm_output` — zero matches. The local model's raw response is returned to the caller unscanned in both call sites.

### Proof the detector isn't broken — it's just not called here
Control: the identical marker string, run through `ModelTaskRouter._gate_ordinary_segments` (the function the *ordinary/non-confidential* branch actually calls), raises `ModelResponseError: prompt_gate_blocked` with `phi_gate: BLOCK — findings=['SSN']`. Same detector, same content, same repository — the ordinary-sensitivity path blocks it; the confidential-sensitivity path (the one that should be gated hardest) never asks.

### Severity note
The destination here is a loopback-only local model (`OfflineLocalLLMClient`, `base_url` restricted to an allowlist, default `http://127.0.0.1:11434`), not an external network egress — so this is not the same class of exposure as the fixed outbound-prompt gate for the ordinary client (`config.py:820-833`). But `offline_approved` is documented in source as "an operator attestation, not proof of isolation" (`model_routing.py:714`), and the local model, its logs, and its host process are all still an unaudited destination for raw CONFIDENTIAL-sensitivity content. This is a real defense-in-depth gap, exactly the shape flagged in `PRIVACY_GATEWAY_RECOMMENDATION.md`'s `prompt_input_gate`/`model_output_gate` dispositions — now demonstrated by execution, not inferred from a code read.

## Fix (unchanged from the prior recommendation, now evidence-backed by execution)
1. Add a `phi_gate_check`/`guard_llm_output`-equivalent call on the prompt in `resolve_confidential_header` and on the CONFIDENTIAL branch of `extract_support_signals`, before the prompt reaches `self._local_client`.
2. Add the symmetric response-side scan on both call sites' returned local-model output before it is parsed/used.
3. Add a regression test asserting `gate_invocations >= 1` for a CONFIDENTIAL-sensitivity task carrying a blockable marker (the probe script above is directly convertible into this test).
