"""Corpus generator — synthetic PHI/PII torture-test harness.

Purpose (Sir's clarification, iteration 12):

    The corpus generator PLANTS PHI/PII into realistic study data so the
    PHI Console can prove it removes every plant. It is not training
    data; it is a red-team rig. Every planted cell carries a
    ground-truth label ``{hipaa_category, expected_action}`` and the
    verifier compares that label against the pipeline's actual decision.

Layout::

    phi_corpus/
      scenarios.py       Real-life study archetypes (oncology, diabetes,
                         pediatric behavioral, rare-disease registry, ...)
      edge_cases.py      Deliberate torture tests (age 89 vs 90, HR 95 vs
                         age 95, restricted ZIP3, name-in-notes, ...)
      planters.py        The primitive: given a scenario + jurisdiction +
                         edge-case bag, plant PHI into cells and emit a
                         ground-truth sidecar the verifier will consume.
      generate.py        Orchestrator + CLI. Emits a manifest ZIP the
                         intake endpoint accepts and a matching ground-
                         truth dict held in memory (Q1(iii) — never on
                         disk).
      verify.py          Compares actual pipeline decisions against the
                         ground truth. Dual scoring per Q2(iii):
                         (a) correctness (was the planted PHI removed?),
                         (b) deferral rate (did Judge defer decidable
                         cases to human_review unnecessarily?).
"""
