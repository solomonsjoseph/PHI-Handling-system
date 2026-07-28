# Methods

## System

PHI Console is a twelve-agent LLM pipeline for de-identifying study
packages that combine structured datasets, free-text forms, and a data
dictionary. The system enforces four invariants:

1. **Headers-only LLM on structured data.** The LLM receives column
   headers together with the data-dictionary row for that column and any
   accompanying form context. Row values are never sent to the LLM. Free
   text inside dataset cells is redacted by a deterministic pipeline of
   Presidio and category-specific regular expressions.
2. **Cross-file exact-match pseudonymisation.** A study-scoped salted
   registry ensures the same real value produces the same pseudonym in
   every dataset in the same study, and different values never collide.
3. **Fail-closed Publish Guard.** After the Executor emits files, a
   deterministic scanner (SSN, phone, email, full DOB, restricted ZIP3,
   age > 89) inspects every output; downloads are refused unless the
   guard clears.
4. **Human-review invariant.** Every changed decision carries a reviewer
   identity, an optional comment, and a UTC timestamp.

## Regulation

Jurisdiction: US. The de-identification method used is the HIPAA
Safe Harbor as defined at 45 CFR 164.514(b)(2)(i)(A)-(R):

* Ages > 89 aggregated to `90+` (Safe Harbor clause C).
* All dates directly related to an individual truncated to year.
* ZIP codes reduced to their initial three digits, with the seventeen
  restricted ZIP3 codes remapped to `000`.
* All eighteen categories A-R detected via a combination of Sentinel
  hard-rules, LLM classification on column headers, and deterministic
  scrubbing on free text.

## Comparators

The coverage matrix in Table 1 compares PHI Console against six
established de-identification tools: Amazon Comprehend PHId, Clinacuity
CliniDeID (Beyond HIPAA Safe Harbor mode), NLM Scrubber, Microsoft
Presidio, MITRE MIST, and GPT-4 in a zero-shot in-context-learning
setting.
