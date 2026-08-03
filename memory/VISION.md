# VISION — PHI Console

**Owner:** Sir.
**Status:** Standing north-star. Read alongside `GOAL.md` (the operational spec) and `PRD.md` (the delivery plan).
**Style:** no emojis, no em-dashes, cite authorities.

---

## 1. One-sentence vision

A PHI-handling console that lets any research team share their study data with any AI/LLM in confidence, because the console has already redacted, transformed, and attested to it — and can prove, on a public benchmark, that it never had to read a single row of PHI to do so.

## 2. The problem we exist to solve

Every PHI-handling tool on the market today reads the actual data. That is the privacy violation the tool is meant to prevent. Researchers face a false choice:

- **Handle PHI manually** — slow, error-prone, non-reproducible, and impossible to defend at IRB.
- **Use an automated tool** — hand the raw PHI to a vendor's model or on-prem service that reads every cell to decide what to redact. The privacy breach is now upstream of the redaction.

Both paths block the thing researchers actually want: to share study data with modern AI safely, cite a defensible method, and move on.

## 3. Our answer

**The LLM never reads a dataset row.** It reads column headers, the collection form the column came from, and the data dictionary or mapping row that describes it. Nothing more.

Handling decisions are made on that context alone, applied deterministically to the rows by a non-LLM Executor, and re-verified by a deterministic Publish Guard at the download boundary. The result is a study bundle that is:

1. **Handled** per HIPAA Safe Harbor 45 CFR 164.514(b)(2)(i) (or the applicable jurisdiction).
2. **Verifiable** against a shipped adversarial corpus with per-category precision, recall, F1.
3. **Publishable** as an IRB-grade attestation plus a benchmark-ready manuscript draft.

## 4. Non-negotiable principles

1. **Zero-row-read.** The LLM sees headers and context. Ever seeing a raw cell is a bug, not a trade-off.
2. **Signal preservation over blanket redaction.** Age 87 stays. Age 96 becomes "90+". Dates truncate to year. ZIP truncates to ZIP3 (with the 17 restricted ZIP3s further blocked). Diagnoses, procedures, vitals, labs stay.
3. **Deterministic at the boundary.** Every irreversible decision (write to disk, expose in bundle) is executed by non-LLM code with hard rules. LLMs advise; deterministic code decides.
4. **Fail-closed.** Missing jurisdiction rulebook, missing mandatory input, unknown Judge action — none of these silently downgrade. They exit non-zero.
5. **Human-in-the-loop is a feature, not a fallback.** When any agent is uncertain, the column routes to human review with an explicit reason. The human decision is applied on the next iteration and logged.
6. **Provable, not asserted.** Every claim the console makes about accuracy is reproducible from the shipped corpus and the shipped verifier. No black boxes, no marketing math.

## 5. Architecture, in one screen

Twelve agents, each a Claude Sonnet call routed through LiteLLM (Emergent Universal Key by default, BYO-key supported):

- **Specialists (parallel):** Lexicon (dictionary), Schema (headers only), Instrument (PDF forms).
- **Experts (cache-first):** Statute (jurisdiction rulebook), Praxis (PHI transformation techniques).
- **Reasoning (Judge/Sentinel loop, up to 3 iterations, then human review):** Judge (per-column decision), Sentinel (0% leak, 100% accuracy reviewer), Executor (deterministic applier, no LLM), Auditor (verifier + metrics).
- **External / publishing:** Scout (competitor landscape), Ledger (comparative benchmark), Herald (manuscript draft).

Every agent input, output, and duration persists to Mongo `agent_log`. Every session is transient; nothing is retained past the run unless the user pins it.

## 6. Who this is for

- **Clinical researchers** who need to share study data with AI without an IRB fight.
- **IRB reviewers** who need to see the method, not take a vendor's word.
- **Data engineers** at hospitals and CROs who have to defend the pipeline to compliance.
- **Method authors** who want a benchmark they can cite, replicate, and beat.

Not for: general PII scrubbing on unstructured web text, or any workflow where reading the raw data upstream is acceptable.

## 7. What "done" looks like — the trust bar

We consider the vision met when a stranger can:

1. Download the shipped adversarial corpus.
2. Run our pipeline and any competitor's pipeline against it.
3. Read the shipped attestation and see, per HIPAA category, exactly which plants were caught, which were missed, and what transformation was applied.
4. Reproduce the numbers we publish, to the digit.
5. Conclude, without our help, that ours is the only pipeline that never read a row.

## 8. What we are explicitly not building

- A general-purpose PII detector for arbitrary text.
- A PDF form generator (removed by direction; the corpus is datasets + dictionaries only).
- A "cloud service" for retained PHI storage. Sessions are transient. The bundle is the receipt.
- A multi-jurisdiction sprawl before US-HIPAA is airtight end-to-end. Stubs exist for EU-GDPR, UK-GDPR, IN-DPDPA, CA-PIPEDA, BR-LGPD; expansion is deferred until the US pipeline runs green every time.

## 9. How progress is measured

- **Category accuracy** on the shipped corpus (target: 100% on HIPAA A–R direct identifiers).
- **Method appropriateness** (target: 100% — every non-PHI clinical field preserved, every PHI field transformed, not blanket-dropped).
- **Zero-row-read invariant** (verified by static analysis of agent inputs; any agent that reads a row value is a P0 defect).
- **Time-to-attestation** on a real study package (target: minutes, not hours).
- **IRB acceptance rate** of the attestation, once we start collecting it.

## 10. The bet

If the LLM never reads a row, and the deterministic layer at the boundary is provably correct, then the entire class of "model leaked PHI" incidents disappears — not because we caught them, but because we architected them out. Every other property (auditability, reproducibility, IRB-readiness, publishability) follows from that single decision.

That is the vision. Everything in the codebase either serves it or gets removed.
