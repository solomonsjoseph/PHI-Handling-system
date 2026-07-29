# TODO — road to v1.0 (IRB-ready, publication-defensible)

Owner: E1  ·  Sir's five requirements below are gated on this list closing.

## The five outstanding requirements (Sir's own words)

1. Zero PHI leak — provable, at the download boundary, across **every** HIPAA
   category the pipeline claims to handle.
2. Human review where necessary — column-level *and* row-level spot-check.
3. Accurate classification + right method per variable and per value.
4. Zero loss of research-relevant information; Safe Harbor transforms preserve
   clinical / epidemiological signal.
5. IRB-ready to present *and* to approve.

Plus: OCR for scanned/annotated PDFs; actual-knowledge attestation per
HHS §164.514(b)(2)(ii); measurable proof rather than architectural claims.

---

## Ordered plan — five phases

### PHASE A · Classification & method accuracy — HIGHEST VALUE
Directly answers Sir's new requirement "all PHI variables and values are
accurately classified and the right method applied." Turns the paper claim
from architectural to empirical.

- [ ] A.1 Ship a labelled classification corpus at
      `/app/backend/tests/corpora/hipaa_categories.json` — ~80 column
      specimens, each with `column_name`, `dict_hint`, `expected_hipaa_letter`,
      `expected_action`. Covers every A-R + non-PHI keepers + free-text.
- [ ] A.2 New module `phi_core/validation.py` — `ClassificationValidator`:
      runs Sentinel hard-rules + (optional) Judge over each column; emits
      predicted category + action + match/mismatch; computes per-category
      precision / recall / F1 and per-action correctness.
- [ ] A.3 Endpoint `GET /api/classification-accuracy` — returns overall
      accuracy, F1 per category, method-appropriateness percentage.
- [ ] A.4 Wizard hero shows a compact "System accuracy on shipped corpus"
      strip (3 numbers, live from the endpoint) — becomes the paper's
      Table 3 headline.
- [ ] A.5 Tests: `test_classification_accuracy.py` — asserts overall F1
      ≥ 0.95 and method-appropriateness ≥ 0.98 on the shipped corpus;
      regression fails if either drops.

**Acceptance gate**: F1 ≥ 0.95 across all HIPAA categories, printed at
`/api/classification-accuracy`.

### PHASE B · Publish Guard pattern parity
Closes the residual-PHI gap on HIPAA categories L / M / N / O / P / Q / R
that the current guard doesn't scan for.

- [ ] B.1 Extend `_PATTERNS` in `publish_guard.py`:
      vehicle plate, device serial / IMEI, URL residual, IP v4/v6,
      long alphanumeric MRN-ish, image-file references, biometric hash refs.
- [ ] B.2 Add negative + positive cases per new pattern in
      `test_publish_guard.py`.

**Acceptance gate**: every HIPAA A-R category has at least one guard pattern.

### PHASE C · OCR path for scanned / annotated PDFs
Required so paper CRFs and scanned consent forms don't pass through unread.

- [ ] C.1 Install `pytesseract`, `pdf2image`, `pillow`; verify system
      `tesseract-ocr` binary or install it. Add to `requirements.txt`.
- [ ] C.2 In `phi_core/file_readers.py`, detect image-only PDFs
      (extracted text < 50 chars); run OCR page-by-page.
- [ ] C.3 OCR output flows into the same `_scrub_text_cell` detector.
- [ ] C.4 Unit test with a synthetic image-only PDF fixture at
      `/app/backend/tests/fixtures/scanned_form.pdf`.

**Acceptance gate**: scanned PDF containing "James Smith, 415-555-1234"
comes out redacted; verified by test.

### PHASE D · Row-level review preview
Closes the "reviewer never spot-checks a cell" gap.

- [ ] D.1 New endpoint `GET /api/sessions/{sid}/preview` — returns
      up to 5 (original, redacted) cell pairs per file for reviewer
      spot-check. Original is masked-partial so preview itself is safe.
- [ ] D.2 SessionDetail renders a "spot-check strip" during
      `awaiting_human_review`; three columns: file · column · redacted.
- [ ] D.3 Reviewer must tick `I have reviewed the sample` before Submit
      enables. Persisted to session_review.
- [ ] D.4 Test: `/preview` returns exactly N samples and no raw PHI.

**Acceptance gate**: cannot submit human review without spot-check ack.

### PHASE E · Actual-knowledge attestation per HHS §164.514(b)(2)(ii)
IRB-required procedural step distinct from method compliance.

- [ ] E.1 `HumanReviewSubmit` requires new field
      `actual_knowledge_ack: bool`. Endpoint rejects if false.
- [ ] E.2 Human review UI adds a required checkbox with the exact HHS
      wording: "I have no actual knowledge that the remaining information
      alone or in combination could be used to identify an individual,
      per 45 CFR 164.514(b)(2)(ii)."
- [ ] E.3 Persist to `session_review.actual_knowledge_ack=true` and
      surface in `attestation.json` and `attestation.txt`.
- [ ] E.4 Test: endpoint 400 when false; bundle attestation.json shows
      ack=true after submit.

**Acceptance gate**: attestation.json shows `"actual_knowledge_ack": true`
for every completed session.

### PHASE F · Verification + PR
- [ ] F.1 Full pytest run (target: 100+ tests all green).
- [ ] F.2 Live E2E: upload → configure → run → review with spot-check +
      actual-knowledge tick → complete → download publication bundle →
      verify attestation.json + preview + accuracy strip.
- [ ] F.3 `testing_agent_v3_fork` sweep.
- [ ] F.4 Update `/app/memory/PRD.md` and `/app/memory/CHANGELOG.md`.
- [ ] F.5 Sir gives green light → push to main / PR.

---

## Parallelisation strategy

Independent, safe to run in parallel:
- A.1 + A.2 (corpus + validator)
- B.1 (guard patterns)
- E.1 (HumanReviewSubmit field)

Sequential (depends on earlier work):
- A.3 depends on A.2
- A.5 depends on A.1 + A.2
- D.1 + D.2 depend on export files (already exist)
- F depends on everything

## Definition of "done" (Sir's original bar)

Every one of these must produce a YES on a fresh audit:

1. **Best in the world** — YES (empirical F1 ≥ 0.95 on shipped corpus; matches or exceeds every reviewed baseline)
2. **Works on 2 or 3 elements, annotated or not** — YES (with C shipped)
3. **0 % PHI leak** — YES (with B, guard covers every A-R)
4. **Human review where necessary** — YES (column-level today + row-level with D)
5. **Accurate classification + right method per variable/value** — YES (with A)
6. **0 % loss of research signal** — YES (already verified: clinical terms survive scrub_text)
7. **IRB-ready** — YES (with E procedural ack + bundle attestation)

## Estimated scope

~900 LOC across five phases; ~30 new tests. Every phase is testable in
isolation. No new heavy dependencies except `tesseract-ocr` (system) and
`pytesseract` / `pdf2image` / `pillow` (Python).

## Non-goals for this PR

- Corpus generator with fully-generated synthetic gold (deferred; Phase A
  ships a static hand-curated corpus which is enough for v1.0).
- India DPDPA / other jurisdictions (deferred; v1.0 is US Safe Harbor).
- Herald abstract-first split (deferred; publication bundle already ships
  methods.md + results.md + discussion.md).
