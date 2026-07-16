# SOTA Comparison and Positioning

Date: 2026-07-07. Every number in this document was verified via web search this session (2026-07-07) against its primary source before being cited here; one correction to the originally-drafted evidence plan is noted explicitly in Section 1, row 1.

> **Current scope:** USA-only. Non-USA generators (`generators/in|eu|br|au|ug`), non-USA corpus slices, and `docs/JURISDICTION_EVIDENCE_REPORT_IN.md` are removed from the active tree. Historical multi-jurisdiction figures below that referenced those artifacts are struck or dropped.

## 1. Related work: free-text clinical PHI de-identification (NOT this system's competitive claim -- see Section 3)

| System | Method class | Corpus | Reported metric | Source |
|---|---|---|---|---|
| DeIDClinic (ClinicalBERT-integrated MASK framework) | Transformer NER + rule/dictionary hybrid | i2b2 2014 | F1 = **0.9732** | arXiv 2410.01648 |
| RoBERTa (external test) | Transformer NER | i2b2 2014, external test set | F1 = 0.887 (0.932 on internal test) | cited via i2b2 2014 benchmarking literature (ResearchGate/PMC4989908 family) |
| Bi-LSTM-CRF + neural LM | Sequence-tagging NER | i2b2 2014 | strict micro-F1 = 95.50% (91.82% on CEGS N-GRID) | ResearchGate/340816929 |
| John Snow Labs Healthcare NLP v6.3.0 | Domain-tuned clinical NLP pipeline | 2025 Text2Story workshop @ ECIR benchmark | F1 = 96% (vs GPT-4o 79%, Azure Health 91% on the same benchmark) | intuitionlabs.ai technical review, 2025/2026 |
| Microsoft Azure de-identification service | Cloud transformer service | UK multi-specialty hospital corpus, 3,650 records | F1 = 0.939 (P 0.928, R 0.950) -- highest of the 9 systems evaluated in this paper | PMC12719064 |
| FT-AnonCAT (fine-tuned, concept-expanded) | Transformer NER | same corpus as above | F1 = 0.910 (P 0.978, R 0.850) | PMC12719064 |
| GPT-4-0125, ten-shot prompting | LLM, in-context | same corpus as above | F1 = 0.898 (P 0.874, R 0.924) | PMC12719064 |
| LLM-Anonymizer (local Llama-3 70B) | Local LLM | 250 real clinical letters | 99.24% PHI-character removal success rate | intuitionlabs.ai review, citing NEJM AI 2025 |
| Philter (UCSF, Norgeot et al. 2020) | Rule + statistical-LM hybrid | 2,000-note UCSF corpus | 99.46% recall, F2 = 94.36 | npj Digital Medicine 2020 (Norgeot et al.) |
| Microsoft Presidio (stock, out-of-the-box) | Rule/NER hybrid, general-purpose PII | 200 neurosurgical documents, 10-year span | P 0.51, R 0.74, F1 = 0.60 | PMC12477974 |
| Philter (independent re-evaluation) | Rule + statistical-LM hybrid | same 200-document corpus as above | P 0.35, R 0.79, F1 = 0.49 | PMC12477974 |
| RedactOR (Oracle Health) | Multi-modal LLM + rule hybrid, structured + unstructured + clinical audio | Oracle Health internal EHR corpora | Automated framework; ACL 2025 Industry Track | arXiv 2505.18380 |
| Expert Small AI Models ("LLMs-in-the-loop Part 2") | LLM-distilled small NER models, 8 languages | multi-lingual clinical de-id corpora | f1-micro 0.953-0.978 across 8 languages (English 0.966) | arXiv 2412.10918 |

**Correction to the original evidence-plan draft**: the plan's stress-test section cited "RoBERTa F1 0.9675 on i2b2 2014 (DeIDClinic, arXiv 2410.01648)". Direct verification of arXiv 2410.01648 this session found the paper's own headline result is **F1 = 0.9732**, achieved by its ClinicalBERT-integrated pipeline (not a bare RoBERTa model) -- the 0.9675 figure could not be located anywhere in that paper or its citing literature and is not repeated here. RoBERTa's own reported i2b2 2014 external-test F1 (0.887, from separate i2b2-2014-benchmarking sources) is cited instead, attributed correctly. This is the kind of number a claim about this repo's system must never inherit uncritically -- hence the direct-source verification pass before this document was written.

## 2. This repo's own measured numbers (for context, not for direct comparison to Section 1 -- different corpora, different tasks; see Section 3)

phi_engine's own pattern-detection surface (`benchmarks/phi_engine_adapter.py`), evaluated on this repo's synthetic USA corpus (`corpus/us`, seed 42, `harness/generate_corpus.py`), strict-protocol F1 (`benchmarks/results/comparison_table.md`, generated 2026-07-07):

| Jurisdiction | phi_engine strict F1 | Presidio stock strict F1 | Presidio tuned strict F1 | spaCy en_core_web_sm legacy F1 |
|---|---|---|---|---|
| us (550 rec / 1314 spans) | 0.3730 | 0.3178 | 0.3442 | 0.3884 (strict N/A, adapter limitation) |

~~Historical multi-jurisdiction F1 rows (in/eu/br/au/ug) removed — those corpus slices and generators are no longer in scope.~~

phi_engine end-to-end scrub system (`harness/run_phi_system.py`, `docs/JURISDICTION_EVIDENCE_REPORT_US.md`): redaction recall 99.58% (USA, seed 42, 719 gold PHI cells, 3 unredacted -- all attributable to a disclosed 1/61 zero-jitter-offset property, not a detection miss). Zero residual PHI findings in the published USA output tree.

## 3. Positioning argument: three axes, not free-text NER

**Do NOT read Section 2's numbers as "beats SOTA on free-text de-identification."** They do not, and the system is not designed to. Section 1's SOTA systems are transformer NER / LLM pipelines evaluated on *real, license-gated, narrative clinical text* (i2b2 2014, UK hospital records, UCSF notes). `phi_engine`'s detection surface (`phi_engine/security/phi_patterns.py`) is a **regex/pattern catalog with checksum/format validation**, evaluated here on a **synthetic, generator-authored USA corpus** the system's own author also built. It has no chance of beating fine-tuned ClinicalBERT/RoBERTa or GPT-4-with-ten-shot-prompting on narrative free text, and this document does not claim otherwise.

Three axes where the surveyed literature above has no directly comparable system, and where this repo's evidence (Sections above, plus `docs/JURISDICTION_EVIDENCE_REPORT_US.md`) is genuinely load-bearing:

**(a) Fail-closed structured/tabular clinical-study pipeline with utility-preserving transforms.** None of the systems in Section 1 operate on CRF-style tabular clinical-study data with a fail-closed contract (unparseable value -> blank/quarantine, never silently published raw) plus utility-preserving transforms specifically: SANT interval-preserving per-subject date jitter (verified this session: 56/56 `VISITDAT`-`COLLDAT` intervals preserved exactly, 60/60 subjects' per-subject offset internally constant across all their own dates), HMAC domain-separated pseudonymization (238/238 measured tokens format-valid, 60/60 subjects' pseudonym linked identically across three CRF forms), and count-only audit ledgers.

**(b) GR-1 -- no LLM ever reads a row value.** `phi_engine.security.llm_detector.classify_headers` reads column HEADERS only, never row data (verified by direct code reading this session, `phi_engine/security/llm_detector.py`); the measured runs in this repo used the deterministic fallback classifier exclusively (`classifier_path: "fallback"`, `llm.provider: none`, `PHI_ALLOW_EXTERNAL_LLM` unset -- no cloud LLM was reachable or used). This is structurally different from RedactOR (LLM-based processing for unstructured text, per its own description), GPT-4-ten-shot, and LLM-Anonymizer (Llama-3 70B reads full clinical letter text), all of which send row/document content to an LLM.

**(c) A synthetic USA PHI benchmark with span-level gold + statutory authority citations, evidence-first from generation through publication.** i2b2 2014 is US-only, real-patient-derived, and DUA-gated (a genuine methodological strength for realism). This repo's USA corpus slice (550 records / 1314 spans under `corpus/us`, seed 42, `corpus/MANIFEST.json` sha256 `5b413b9df27985c961a9b4b688efd9706e194607f2d1bdfeb97eb7054c34e2be`, `validation_status: PASS`) is fully synthetic, seed-reproducible, and every span carries a statutory authority citation (`GoldSpan.authority`) traceable to `authorities/AUTHORITY_MATRIX.md`. ~~Former multi-jurisdiction expansion (India/EU/BR/AU/UG generators and evidence) is out of current scope.~~

## 4. Known attacks on this positioning, and mitigations actually applied

**Circularity** (the system evaluated on its own synthetic corpus, generated by the same codebase). Mitigations applied and measured this session, not merely asserted: both scoring profiles reported for every benchmark run (strict + legacy, `benchmarks/results/comparison_table.md`); baselines (Presidio stock/tuned, spaCy) run on the identical USA corpus files as phi_engine, same `benchmarks.metrics` scoring code; every artifact hashed (sha256 recorded in every `phi_system_result.json`, `release_evidence.json`). ~~India held-out-seed replication artifacts are removed with the India evidence report.~~

~~**Non-US fairness** comparisons that depended on deleted non-USA corpus slices and generators are out of current scope.~~

## 5. What this repo does NOT claim

- Not a free-text clinical NER competitor. Not evaluated against, and not claimed to beat, ClinicalBERT/RoBERTa/GPT-4/Azure/JSL on narrative clinical text.
- Not validated on real patient data. Every number in Section 2 and in `docs/JURISDICTION_EVIDENCE_REPORT_US.md` is synthetic-corpus-scoped.
- Not clinician-reviewed or counsel-reviewed (tracked as open blockers in `.phi-build-status`).
- Not multi-jurisdiction: non-USA generators and evidence reports are removed; current claims are USA/HIPAA-scoped only.
- Not "fail-proof" or unscoped "100% accurate" anywhere, per repo Claim Discipline (CLAUDE.md Truth Protocol). The bounded equivalents used throughout this repo's reports: "redacted M/M (100%) of gold-annotated PHI cells on this corpus" (only printed when actually measured -- see the 99.58% USA figure measured this session) and "outperforms all N evaluated baselines on this benchmark" (scoped to the named baselines and named benchmark, never generalized).
