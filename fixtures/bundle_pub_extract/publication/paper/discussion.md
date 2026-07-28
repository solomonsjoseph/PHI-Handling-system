# Discussion

The coverage advantage in Table 1 is driven by design choices that were
absent from prior work:

* **Header-only reasoning.** Existing free-text de-identifiers assume the
  input is unstructured clinical text. When the input is a structured
  dataset — the majority case for study packages — reading every row
  through an LLM both inflates cost and creates a leakage surface. PHI
  Console classifies at the header layer only.
* **Dictionary and codebook scrubbing.** Data dictionaries frequently
  quote a real patient name or contact detail as an "example". None of
  the reviewed tools scrub these files. PHI Console applies the same
  deterministic detectors used on free text.
* **Cross-file linkage.** Analytical utility depends on being able to
  join two anonymised tables on a shared identifier. Prior tools either
  drop the identifier (destroying joinability) or hash it without
  study-scoped salting (allowing cross-study linkage). PHI Console emits
  a stable per-study pseudonym.
* **Publish Guard.** No prior tool refuses to serve an export it just
  produced. PHI Console runs a deterministic residual-PHI scan on the
  emitted artefacts and requires either a clean verdict or an explicit
  operator override before releasing the download URL.
* **Human-review invariant.** Prior tools log decisions but do not carry
  reviewer identity into the artefact. PHI Console persists reviewer
  identity, comment, and timestamp on every changed decision and inside
  the signed attestation.
