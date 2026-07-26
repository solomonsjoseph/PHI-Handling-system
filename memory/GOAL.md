# PROJECT GOAL - Persistent north-star spec

**Set by:** Sir on 2026-07-26.
**Status:** Standing directive. Every future session must load this before any edit.

## Input

A study package containing at least 2 of these 3 elements (`datasets/` is always mandatory):
- **datasets/** - the actual PHI-carrying tables (CSV, XLS, XLSX single-sheet).
- **forms/** - PDFs of the data-collection instruments.
- **data_dictionary/** or **mappings/** - column definitions and code maps (CSV, XLSX).

## Output

Study data with every PHI variable HANDLED per the applicable jurisdiction such that:
- The result can be shared with any AI/LLM without PHI leak.
- Clinically and epidemiologically needed information is PRESERVED, not blindly removed.
- Zero PHI variables slipped through, ever.

## Core inviolable constraint

The AI/LLM never reads dataset row values. Only column HEADERS. Full content permitted for
forms and dictionary/mappings because those are collection instruments and code definitions,
not the PHI-carrying rows. Context for header classification comes from:
1. The column header token itself.
2. The form the column was collected from.
3. The dictionary/mapping row that describes the column.

## Handling policy (not just redaction)

PHI handling is not blanket redaction. Per HIPAA Safe Harbor 45 CFR 164.514(b)(2)(i) and
equivalent statutes in other jurisdictions, transformations preserve research signal:
- **Direct identifiers (A, D, E, F, G, H, I, J, K, L, M, N, O, P, Q, R)**: REMOVE or PSEUDONYMIZE.
- **Age**: values < 90 kept as-is (clinically needed). Values > 89 aggregate to "90+" (Safe Harbor).
- **Dates**: truncate to year only (Safe Harbor allows year).
- **ZIP**: truncate to first 3 digits, and the 17 restricted ZIP3 codes further blocked.
- **Geographic**: state kept, sub-state units removed.
- **Clinically-needed non-PHI**: PRESERVED (diagnoses, procedures, vitals, labs, etc.).

## Jurisdiction

Start with USA/HIPAA. Progress to multi-jurisdiction. Every rule is jurisdiction-pinned;
loading the wrong jurisdiction rulebook exits non-zero rather than silently downgrading.

## Trust bar

More trustable than existing PHI handling tools that require actually READING the data (a
direct privacy violation). Our LLM only sees headers + context files. Detection on cells is
regex/pattern only. When the AI/LLM is uncertain, it routes to human review with the exact
reason. The human decision is applied on the next iteration.

## Corpus generator + benchmark

- **Corpus generator** plants known-value PHI in synthetic records with `expected_handling` in
  the gold annotation. Example: plant age 96, expected handling is transform to "90+".
  A record passes only when the system produced exactly the expected handled value.
- **Benchmark** computes precision, recall, F1, per HIPAA category, per jurisdiction. Numbers
  + plots for paper publication. Compares against open baselines (Presidio, spaCy). Stubs
  for commercial baselines when credentials are available.

## Human review invariant

The system may say "handled" only after either (a) high-confidence AI decision, or (b)
explicit human decision recorded with reviewer id + comment + timestamp. Never silently
default to a redaction or a preservation.

## Non-goals

- No HIPAA/GDPR/etc. certification claim. This is fail-closed tooling, not attestation.
- No reading row values by LLM. Ever. Regex/pattern is not "reading".
- No blanket redaction that erases research signal.
