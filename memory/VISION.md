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

## Appendix A. Live wallclock baselines (2026-02-07)

Measured on two real US-HIPAA corpus runs of the same shipped pipeline
(scenario `diabetes_v1`, 10 columns, iteration_cap=3, Claude Sonnet 4.5
via Emergent Universal Key). Both runs completed with Sentinel
short-circuiting on iteration 1 (no blocking issues).

### Cold cache — first study of the day

Praxis cache wiped before this run; the 10 non-deterministic HIPAA
categories (E, I..R) had to web-search from scratch.

**Total wallclock: 181.2 s.**

Per-agent LLM time (sum of duration_ms across all calls):

```
Praxis              289.87 s   (10 categories × ~29 s web search, ALL RUN IN PARALLEL)
Herald.Abstract      62.63 s
Herald.Sections      33.64 s
Ledger.Compare       17.04 s
Ledger.Aggregate     14.76 s
Judge                20.78 s
Sentinel             23.13 s
Lexicon              19.30 s
Schema                8.77 s
Auditor               7.81 s
                    -------
                    497.72 s   sum of LLM call durations
```

The critical wallclock reduction: **Praxis 289.87 s of sequential LLM time
collapsed into a 34.92 s parallel block** because all 10 web searches fire
concurrently under `asyncio.gather`. Specialists (Lexicon + Schema, ~28 s)
overlap the same block; the block is bounded by the slowest single Praxis
search, not the sum.

### Warm cache — subsequent studies within the weekly refresh window

Cache hit on every Praxis category; Statute cache hit; specialists still
parse the incoming ZIP.

**Total wallclock: 159.9 s.**

Per-agent LLM time (sum of duration_ms across all calls):

```
Herald.Abstract      48.81 s
Herald.Sections      30.89 s
Judge                22.83 s
Sentinel             20.08 s
Lexicon              20.39 s
Ledger.Aggregate     19.05 s
Ledger.Compare       15.23 s
Schema                7.24 s
Auditor               6.13 s
                    -------
                    190.65 s   sum of LLM call durations
```

### What parallel launch buys us (measured, not projected)

Two parallelisations landed on 2026-02-07:

1. **Specialists + Statute + Praxis at t=0.** On cold cache the Praxis
   block collapses 289.87 s of sequential LLM work into 34.92 s of
   wallclock. Savings: **~255 seconds versus a fully sequential Praxis
   walk**, absorbed into what would otherwise be the specialist wait
   anyway.

2. **Herald.Abstract || Herald.Sections.** Previously Abstract had to
   finish before Sections started (Sections received the abstract text
   as context). By reworking the Sections prompt to skip restating the
   aim, both LLM calls now fire concurrently.
     - Cold-cache run: Herald wallclock 62.6 s vs 96.3 s serial. **Saved ~33 s.**
     - Warm-cache run: Herald wallclock 48.8 s vs 79.7 s serial. **Saved ~31 s.**

### IRB-quotable summary

"The end-to-end 12-agent handling of a real US-HIPAA study of 10 variables
completes in **160 seconds cache-warm** and **181 seconds cache-cold** on
the shipped Claude Sonnet 4.5 pipeline, with zero row values sent to the
LLM at any point. Two parallel-launch designs — concurrent
Specialists+Statute+Praxis and concurrent Herald.Abstract+Sections — save
approximately 285 seconds versus a fully sequential layout, meaning a
typical study is redacted, attested, and manuscript-drafted in under three
minutes."

This appendix is regenerated whenever the pipeline order or agent set
changes materially; treat any earlier numbers here as historical.

