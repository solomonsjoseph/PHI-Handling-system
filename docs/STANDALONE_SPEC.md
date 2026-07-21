# Standalone PHI Handling System — Spec Checklist

This file is the audit target for the PHI runtime (`phi_engine`). Each item
below is checkable against the working tree with file:line evidence.

## 1. Symlink-only intake

- [ ] `phi_engine/pipeline/intake.py::intake_add` links files into
      `<workspace>/intake/<study>/` via `os.symlink` only — never
      `shutil.copy*`. Check: `grep -n "shutil.copy" phi_engine/pipeline/intake.py`
      returns nothing.
- [ ] Every entry under `<workspace>/intake/<study>/` is either a symlink or
      the `intake_manifest.json` bookkeeping file. Verified by
      `harness/spec_check.py`'s `intake_symlink_invariant` check.
- [ ] Intake never opens a source file for write and never deletes a source
      file (content hashing is a streamed read only).

## 2. Never move/modify/copy source data

- [ ] The organizer (`phi_engine/pipeline/organize.py::organize`) reads
      normalized dataset content ONLY through the intake symlinks (never a
      path outside `<workspace>/intake/`) and writes derived artifacts
      under `<workspace>/organized/<study>/` — never back into the SOURCE
      tree (the external directory passed to `intake_add`). Writes
      elsewhere WITHIN the workspace are allowed and expected: compatibility
      symlinks under `<workspace>/data/raw/<study>/{datasets,annotated_pdfs}/`
      (so unmodified engine constants like `config.DATASETS_DIR` keep
      working) and the review bucket under
      `<workspace>/output/<study>/audit/human_review/`.
      "Never touches source" is the invariant under test — not "writes to a
      single directory." KNOWN EXCEPTION: `organize()` also performs a
      direct, read-only lookup of an optional forms manifest at
      `source_root / "datasets"` (the external source tree itself, not an
      intake symlink) via `scripts.extraction.forms_manifest.check_forms_manifest`
      — a metadata-only read outside `<workspace>/intake/`, not a write.
- [ ] `harness/spec_check.py`'s `source_immutability` check re-hashes every
      stress-fixture source file after a full intake+organize+run pass and
      compares against the fixture-build-time manifest; any drift is FAIL.

## 3. Organizer handles messy inputs

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
      in the review bucket with a `{file, reason}` record — filename/reason
      metadata only, never row values.

## 4. Regulation-driven classification, methods applied per classification

- [ ] Classification is driven by `phi_engine.security.phi_review` pinned
      per-jurisdiction rules (`us`), grounded in `authorities/*.md`.
- [ ] Every classified header's `action` reaches the row scrubber: the
      per-study `phi_scrub.yaml` is synthesized
      (`phi_engine/pipeline/synthesize_config.py`) so a NOVEL header's
      classified action actually gets applied — not just force-drop/suppress.
- [ ] `phi_scrub.run_scrub` applies the named method (force-drop / keep /
      drop / HMAC-pseudonymize / SANT-date-jitter / cap / generalize / band /
      small-cell-suppress / quarantine) per the synthesized config.

## 5. AI-safe output

- [ ] Header classification prompts (`llm_detector.py`) are headers-only —
      never a row value.
- [ ] The published `llm_source/datasets/` tree passes the residual PHI guard
      gate (`phi_guard_gate.run_phi_guard_gate`) before publish WHEN the gate
      runs cleanly. KNOWN LIMITATION: on a guard exception, the pipeline
      currently falls back to the legacy regex scanner alone and can still
      publish on that scanner's result — not yet closed (see
      `docs/PRIVACY_GATEWAY_RECOMMENDATION.md` §"Weak points wrapped or
      replaced").
- [ ] Prompt egress gate: `LLMClient.complete` runs `phi_gate_check` on the
      outbound prompt before provider dispatch and raises
      `PHIEgressBlockedError` on a match. Known limitation: the blocking tier
      has false negatives — documented, not silently claimed fixed.

## 6. Human review routing + feedback application

- [ ] Held forms, organizer review-bucket entries, and LLM-uncertain queue
      entries are all visible via `python -m phi_engine review --study S list`.
- [ ] A `keep`/`drop`/`override <action>` decision
      (`python -m phi_engine review --study S decide ...`) is persisted to
      `review_decisions.yaml` and is APPLIED on the next `run` — not merely
      logged. `drop` removes the column from published output; `keep`
      un-holds a name-flagged header; `override` changes the applied method.

## 7. API-key AI systems and row-data isolation

- [ ] `llm_detector.py` and `phi_alignment.py` construct an `LLMClient` and
      send a prompt built from headers/metadata/classifications ONLY —
      never a patient dataset row value — verified by
      `harness/spec_check.py`'s `llm_boundary_canary` check (static grep,
      scoped to `phi_engine/pipeline/` only) and by
      `tests/test_stress_standalone.py`'s fake-LLM prompt-capture assertion
      (zero planted row values in any captured prompt).
- [ ] KNOWN, DISTINCT ROUTE (not covered by `llm_boundary_canary`, which
      only scans `phi_engine/pipeline/`): `phi_engine/security/model_routing.py`'s
      `ModelTaskRouter.extract_support_signals`, reached via
      `run_pipeline` -> `support_policy.extract_support_signals`, builds a
      prompt from `SupportSignalTask.matched_rows` — actual support-cell
      values from an explicitly human-approved, exact-header-matched
      dependency link, NOT primary patient dataset rows. When
      `decision.sensitivity` is `NON_CONFIDENTIAL`, this task is routed to
      `_new_ordinary_client()` (`config.get_llm_client()`, the same
      off-box, API-key-capable client) after `_gate_ordinary_segments`
      screens the prompt through `phi_gate_check`. `CONFIDENTIAL`
      sensitivity stays on the loopback-only local client and is NOT
      `phi_gate`-checked before the local model sees it (see
      `docs/PRIVACY_GATEWAY_RESEARCH.md` §1.3). This route is opt-in
      (requires a current, non-ignored human decision on an
      EXACT_HEADER_MATCH recommendation) and fail-soft (any router/model
      failure degrades to no signal), but it is a real exception to
      "headers/metadata/classifications ONLY" and is not exercised by
      `llm_boundary_canary`.

## 8. Portability — plug into any project, zero code changes

- [ ] `PHI_WORKSPACE` env var (read before `phi_engine.config.config` import)
      relocates every workspace-relative path (`intake/`, `organized/`,
      `data/raw/<study>/`, `output/<study>/`, per-study `config/<study>/`).
- [ ] Running `python -m phi_engine ...` from a DIFFERENT cwd with a
      DIFFERENT `PHI_WORKSPACE`, against a foreign source tree, produces the
      same behavior with no repo-root dependence for data paths.
