# Standalone PHI Handling System — Spec Checklist

This file is the audit target for the fresh-context review subagents spawned
after Phases 2, 4, and 6 (per `local://phi-standalone-refactor-plan.md`
Phase 0 step 2). Each item below is checkable against the working tree with
file:line evidence. A subagent auditing this file has NO access to the
implementing conversation and MUST re-derive violations from the code itself.

## 1. Two independent parts

- [ ] The corpus/benchmark generator (`generators/`, `harness/generate_corpus.py`,
      `harness/run_phi_system.py`'s data-preparation step) and the PHI handling
      system (`phi_engine/`) are independent in ONE direction only: the corpus
      side MAY import `phi_engine` (e.g. `generate_corpus --mode llm`'s lazy
      `get_llm_client` import), but `phi_engine` MUST NEVER import
      `generators`. Check: `grep -rn "from generators\|import generators" phi_engine/`
      returns nothing.

## 2. Symlink-only intake

- [ ] `phi_engine/pipeline/intake.py::intake_add` links files into
      `<workspace>/intake/<study>/` via `os.symlink` only — never
      `shutil.copy*`. Check: `grep -n "shutil.copy" phi_engine/pipeline/intake.py`
      returns nothing.
- [ ] Every entry under `<workspace>/intake/<study>/` is either a symlink or
      the `intake_manifest.json` bookkeeping file. Verified by
      `harness/spec_check.py`'s `intake_symlink_invariant` check.
- [ ] Intake never opens a source file for write and never deletes a source
      file (content hashing is a streamed read only).

## 3. Never move/modify/copy source data

- [ ] The organizer (`phi_engine/pipeline/organize.py::organize`) reads ONLY
      through the intake symlinks (never a path outside `<workspace>/intake/`)
      and writes derived artifacts under `<workspace>/organized/<study>/` —
      never back into the SOURCE tree (the external directory passed to
      `intake_add`). Writes elsewhere WITHIN the workspace are allowed and
      expected: compatibility symlinks under `<workspace>/data/raw/<study>/
      {datasets,annotated_pdfs}/` (so unmodified engine constants like
      `config.DATASETS_DIR` keep working, per the standalone plan step 8) and
      the review bucket under `<workspace>/output/<study>/audit/human_review/`.
      "Never touches source" is the invariant under test — not "writes to a
      single directory."
- [ ] `harness/spec_check.py`'s `source_immutability` check re-hashes every
      stress-fixture source file after a full intake+organize+run pass and
      compares against the fixture-build-time manifest; any drift is FAIL.

## 4. Organizer handles messy inputs

- [ ] Raw variables/datasets in `.jsonl` and `.json` are validated and
      normalized into `organized/<study>/datasets/`.
- [ ] `.csv` is parsed (`dtype=str`, no NA coercion) into JSONL.
- [ ] `.xlsx` is sheet-split and header-promoted (one JSONL per sheet-table).
- [ ] `.xls` is read via `xlrd` when available; unreadable/mislabeled `.xls`
      routes to human review (fail-closed), never silently dropped or
      silently parsed as garbage.
- [ ] `.pdf` is either routed to `annotated_pdfs/` (CRF companion, matched by
      stem) or table-extracted via `pdfplumber`; a PDF with no extractable
      table and no dataset-stem match lands in the review bucket.
- [ ] Any unrecognized suffix, broken intake symlink, or parse failure lands
      in the review bucket with a `{file, reason}` record — no row values.

## 5. Regulation-driven classification, methods applied per classification

- [ ] Classification is driven by `phi_engine.security.phi_review` pinned
      per-jurisdiction rules (`in`/`us`), not by any corpus-side heuristic.
- [ ] Every classified header's `action` reaches the row scrubber: the
      per-study `phi_scrub.yaml` is synthesized
      (`phi_engine/pipeline/synthesize_config.py`) so a NOVEL header's
      classified action actually gets applied — not just force-drop/suppress.
- [ ] `phi_scrub.run_scrub` applies the named method (force-drop / keep /
      drop / HMAC-pseudonymize / SANT-date-jitter / cap / generalize / band /
      small-cell-suppress / quarantine) per the synthesized config.

## 6. AI-safe output

- [ ] Header classification prompts (`llm_detector.py`) are headers-only —
      never a row value.
- [ ] The published `llm_source/datasets/` tree passes the residual PHI guard
      gate (`phi_guard_gate.run_phi_guard_gate`) before publish.
- [ ] Prompt egress gate: `LLMClient.complete` runs `phi_gate_check` on the
      outbound prompt before provider dispatch and raises
      `PHIEgressBlockedError` on a match (closes prior-audit C2 for the
      pipeline path). Known limitation: the blocking tier has false
      negatives (prior-audit C3) — documented, not silently claimed fixed.

## 7. Human review routing + feedback application

- [ ] Held forms, organizer review-bucket entries, and LLM-uncertain queue
      entries are all visible via `python -m phi_engine review --study S list`.
- [ ] A `keep`/`drop`/`override <action>` decision
      (`python -m phi_engine review --study S decide ...`) is persisted to
      `review_decisions.yaml` and is APPLIED on the next `run` — not merely
      logged. `drop` removes the column from published output; `keep`
      un-holds a name-flagged header; `override` changes the applied method.

## 8. API-key AI systems never read rows during pipeline execution

- [ ] Every module that constructs an `LLMClient` and sends a prompt during
      pipeline execution (`llm_detector.py`, `phi_alignment.py`) builds that
      prompt from headers/metadata/classifications ONLY — verified by
      `harness/spec_check.py`'s `llm_boundary_canary` check (static grep) and
      by `tests/test_stress_standalone.py`'s fake-LLM prompt-capture assertion
      (zero planted row values in any captured prompt).
- [ ] Single documented exception: Phase 7 step 24 permits the IMPLEMENTING
      AGENT (not a pipeline-internal AI system reached via an API key) to
      read post-scrub rows of the SYNTHETIC evidence-study outputs for
      verification, with every file/row read listed in the final report.

## 9. Corpus generator stays behind as optional tooling

- [ ] `generators/` + `harness/generate_corpus.py` remain in place, unmoved.
- [ ] `harness/run_phi_system.py` (post-refactor) uses the generator ONLY in
      its data-preparation step, then drives the system exclusively through
      `intake_add` + `run_pipeline` — `grep -rn "generators" phi_engine/`
      returns nothing.

## 10. Portability — plug into any project, zero code changes

- [ ] `PHI_WORKSPACE` env var (read before `phi_engine.config.config` import)
      relocates every workspace-relative path (`intake/`, `organized/`,
      `data/raw/<study>/`, `output/<study>/`, per-study `config/<study>/`).
- [ ] Running `python -m phi_engine ...` from a DIFFERENT cwd with a
      DIFFERENT `PHI_WORKSPACE`, against a foreign source tree, produces the
      same behavior with no repo-root dependence for data paths (checked in
      Verification step 3 of the plan).
