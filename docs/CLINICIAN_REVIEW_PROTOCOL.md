# Clinician Review Protocol

**Status:** PENDING - no clinician review has been completed in this repository.

This protocol defines the minimum clinician review needed before any L4 claim involving clinical plausibility or IRB-audit-ready corpus review. It does not assert that such review has occurred.

## Reviewer panel

- Three independent clinician reviewers are required.
- Reviewers should be independent of corpus generation and benchmark implementation work.
- Reviewer identity, qualifications, conflict-of-interest status, and review date must be recorded in a durable review artifact outside generated corpus data.

## Sampling plan

- Minimum sample size: 300 sampled records once corpus size supports it.
- Sampling must be deterministic or otherwise auditable, with the corpus manifest hash recorded before sampling.
- The sample should cover available jurisdictions, record families, and file/narrative formats proportionally or according to a documented stratification plan.
- If the corpus size or composition does not support 300 meaningful sampled records, the review remains `PENDING` and the limitation must be recorded.

## Required reviewer outputs

Each reviewer must independently record:

1. Plausibility decision for each sampled record.
2. PHI label correctness decision for each sampled record and each reviewed span.
3. Disagreement notes for implausible records, incorrect labels, missing labels, over-labeled spans, or ambiguous cases.
4. Any safety concern indicating that a record appears to contain real PHI or a real-world individual.

## Agreement and adjudication

- Calculate inter-reviewer agreement for PHI label correctness.
- Cohen kappa target: `>= 0.80`.
- Disagreements must be adjudicated and recorded with the adjudication decision, not silently overwritten.
- If kappa is below target, the release must not claim clinician-reviewed L4 status without a documented remediation and re-review.

## Release use

A release packet may cite clinician review only when:

- all three independent reviews are complete;
- the reviewed corpus manifest hash is recorded;
- sample selection is reproducible;
- plausibility and PHI label correctness outputs are archived;
- disagreement notes and adjudication are archived;
- Cohen kappa is reported and meets the target or the exception is explicitly approved.

Until these artifacts exist, clinician review status is `PENDING`.
