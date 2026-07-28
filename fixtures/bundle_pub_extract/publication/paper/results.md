# Results

## Publish Guard verdict

Status: **clean**. 4 file(s)
scanned; 0 blocked.

## Category coverage vs. established tools

Figure 1 (`fig1_category_coverage.png`) and Table 1
(`table_1_category_coverage.csv`) present a side-by-side comparison of
which HIPAA identifier categories each tool targets. PHI Console covers
every A-R identifier plus five categories that no existing off-the-shelf
tool addresses today:

* Structured dataset column classification with LLM restricted to
  headers.
* Data-dictionary and codebook cell scrubbing.
* Cross-file exact-match pseudonymisation with per-study salting.
* Fail-closed Publish Guard at the download boundary.
* Machine-checkable reviewer invariant.

Figure 2 (`fig2_category_totals.png`) reports the total number of
categories covered per tool.

## Per-category precision / recall / F1

The benchmark harness in `benchmark/` computes precision, recall and F1
per HIPAA category once a gold-annotated corpus is provided. Reference
numbers for Amazon Comprehend PHId, CliniDeID and NLM Scrubber on the
2014 and 2016 i2b2 corpora are drawn from Heider et al. 2020 for context.
