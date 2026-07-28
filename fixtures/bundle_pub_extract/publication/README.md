# Publication add-on

This folder holds artefacts for writing up a de-identification paper using
this run:

* `paper/tables/table_1_category_coverage.csv` — HIPAA identifier coverage
  vs. Amazon Comprehend PHId, CliniDeID, NLM Scrubber, Microsoft Presidio,
  MITRE MIST, and GPT-4 (zero-shot ICL).
* `paper/figures/fig1_category_coverage.png` — heatmap version of the same
  table with our system column highlighted.
* `paper/figures/fig2_category_totals.png` — bar chart of total categories
  covered per tool.
* `paper/methods.md`, `paper/results.md`, `paper/discussion.md` — draft
  paper sections composed by the Herald agent.
* `paper/references.bib` — BibTeX citations (HHS guidance, Heider 2020,
  Altalla 2025, Presidio, MIST).
* `benchmark/` — scaffolding for gold-annotated F1 comparisons; populated
  once the operator supplies a gold corpus.

Cite this bundle as: PHI Console, session `<session_id>`, generated on
`<generated_at>`. All artefacts are reproducible from the input study
package and the SHA-256 hashes in `attestation.json`.
