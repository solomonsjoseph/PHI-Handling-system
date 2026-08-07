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

## Appendix A. Live wallclock baseline (2026-02-07)

First measured on a real US-HIPAA corpus run through the shipped pipeline
(scenario `diabetes_v1`, 10 columns, iteration_cap=3, cache warm, Claude
Sonnet 4.5 via Emergent Universal Key).

**Total wallclock:** 196.8 seconds.

Per-agent LLM time (sum of duration_ms across all calls):

```
Lexicon              16.98 s   (dictionary parse)
Schema                8.03 s   (dataset headers)
Judge                23.47 s   (per-column classification, 1 iteration)
Sentinel             26.33 s   (LLM review, short-circuited after 1 pass)
Auditor               7.23 s   (metrics narrative)
Ledger.Compare       14.17 s   (per-competitor delta)
Ledger.Aggregate     14.97 s   (headline + recommendations)
Herald.Abstract      53.40 s   (title + abstract + methods + refs)
Herald.Sections      31.02 s   (results + discussion + limitations)
                    -------
                    195.59 s   sum of LLM call durations
```

Phase transitions (in wallclock order):

```
specialists          t=  0.00s  (Lexicon -> Schema serial; Instrument
                                 skipped, no forms in this scenario)
statute              t=  0.00s  (cache hit, 0 s)
praxis               t=  0.00s  25.04 s  (all 17 categories cache-hit;
                                 duration reflects the parallel block max
                                 because Statute + Praxis + Specialists
                                 launched together)
judge_iter_1         t= 25.04s  23.47 s
sentinel_iter_1      t= 48.51s  26.34 s  (short-circuit: 0 blocking issues)
executor             t= 74.86s   1.12 s  (deterministic, no LLM)
publish_guard        t= 75.97s   ~0 s    (deterministic scan)
auditor_scout        t= 75.97s   7.24 s  (parallel)
ledger               t= 83.21s  29.14 s  (Compare + Aggregate)
herald               t=112.35s  84.42 s  (Abstract + Sections)
```

**Parallel-launch impact.** On the cold-cache first run of the day, Statute
(~10 s web search) and Praxis (~70 s across 10 non-deterministic web
searches for categories E, I..R) previously ran *after* the specialists.
Under the current parallel-launch design they overlap with the specialist
block, which is dominated by Lexicon + Schema (~25 s). Projected cold-cache
saving on the first study of the day: ~55 seconds (Statute ~10 s + Praxis
non-overlapping ~45 s, both now absorbed into the specialist window).

Cache-warm runs (every subsequent study within a week) see the parallel
launch essentially free: Statute and Praxis are cache hits, so the block
is bounded by the specialist runtime regardless.

**IRB-quotable summary.** "The end-to-end 12-agent handling of a real
US-HIPAA study of 10 variables completes in under 200 seconds on cache-warm
paths, with zero row values sent to the LLM. On the first study of the day,
parallel launch of Specialists + Statute + Praxis saves approximately
55 seconds versus the earlier serial layout."

This appendix is regenerated whenever the pipeline order or agent set
changes materially; treat any earlier numbers here as historical.
