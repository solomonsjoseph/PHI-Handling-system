# PHI-Handling-System Audit Report

Date: 2026-07-06 (generated 2026-07-07T01:20Z UTC)
Commit: 7fef8c7 (branch feat/v2-multi-jurisdiction, plus uncommitted working tree)
Auditor: Claude Code (four parallel adversarial subsystem reviews plus independent verification)
Method: Truth Protocol. Every finding cites file and line. Nothing assumed from undocumented functionality.

## Executive Summary

The system is a well-engineered, unusually honest benchmark corpus and validation framework at roughly Phase 3-4 of the 8-phase CLAUDE.md plan. Core engineering (deterministic generation, span integrity, hash manifests, validation gating) is genuinely solid and was independently reproduced during this audit. Three findings prevent any "IRB-ready" or "world-leading" claim today: (1) the LLM security boundary described in THREAT_MODEL.md is a dormant library, not an enforced control; (2) several validators and the MIA framework are materially weaker than their names imply; (3) large planned scope (ICMR generators, 7 of 12 file formats, cloud benchmark adapters, CI, packaging) is unbuilt. The claim "best PHI handling system built" cannot be confirmed and is not supported by repo evidence.

Confidence scores:
- As an L2-partial multi-jurisdiction PHI benchmark corpus with validated span integrity: 72/100
- As an IRB-approval-ready system: 35/100
- "Best PHI handling system built": cannot confirm; no supporting evidence exists in the repo

## Independently Verified Facts

- 297/297 tests pass (pytest, 34s, Python 3.9.21).
- All 7 validators PASS on a fresh run of harness/run_all_validations.py against the live corpus (exit 0).
- Corpus: 886 records, ~2,506 gold spans, 7 jurisdiction groups (us 550, in 56, eu 40, br 36, au 28, ug 28, file_formats 148), seed 42.
- All 2,506 spans verified programmatically: text[start:end] matches span value with zero mismatches.
- release_evidence.json sha256 values match corpus/MANIFEST.json, validation_report.json, and mia_report.json byte-for-byte.
- MANIFEST self-labels claim level "L2-partial" (honest).
- Presidio benchmark artifacts in benchmarks/results/presidio-stock/ are genuine: 550 records / 1,314 gold spans match the manifest exactly; text_sha256 values in raw predictions match SHA-256 of corpus text (verified for all 40 hipaa_biometric records).

## Findings

### Critical

C1. LLM tool guards are not wired to anything.
- llm_safe_tool, guard_llm_output, validate_llm_read_path (phi_engine/security/llm_tool_guard.py:49,74,85) have zero non-test call sites.
- THREAT_MODEL.md:65-67 claims "LLM-visible tool reads first pass through validate_llm_read_path... outputs pass through guard_llm_output". No such enforcement point exists in the live tree.
- phi_gate.py:15-18 docstring claims every @tool function in scripts.ai_assistant.agent_tools runs through phi_gate_check; that module exists only under archive/. Stale and false for the live tree.
- The three actual LLM call paths (phi_engine/security/llm_detector.py:158; phi_engine/tools/regulation_fetcher.py:104,137; harness/generate_corpus.py:341,408) do not use llm_safe_tool or guard_llm_output.
- THREAT_MODEL.md:85 lists path guards as the prompt-injection control; nothing calls them. Prompt-injection defense is currently aspirational.

C2. Egress protection points the wrong direction.
- Gate: get_llm_client (phi_engine/config/config.py:698-709) raises unless PHI_ALLOW_EXTERNAL_LLM=true for external providers, and openai-oauth is correctly in the external set (config.py:703). This part works and matches SECURITY.md.
- But once the flag is true, all three call sites egress prompts unscreened. guard_llm_output only scans return values; prompts are what leave the machine. LLMClient.complete sends raw prompt text with no scrub; nothing structurally prevents a caller passing record text.

C3. Blocking regex tier has demonstrable false negatives.
- phi_patterns.py:215-265 blocking set misses: US phone numbers (only INDIAN_PHONE exists), unhyphenated SSNs (regex requires \d{3}-\d{2}-\d{4}), bare person names (WARN tier only, non-blocking per phi_gate.py:132-139,153), street addresses, ages over 89, most MRN formats, dates like "Jan 5 2013".
- tests/test_phi_llm_safety.py covers only the positive SSN case; the bypasses are invisible to CI.

### High

H1. no_real_phi_static_validator is close to a rubber stamp.
- validators/no_real_phi_static_validator.py:8-14: entire check is 5 substrings (gmail.com, yahoo.com, hotmail.com, "real patient", "actual patient").
- Lines 17-35: gold spans are masked before checking, so banned content inside a labeled span is never inspected. The name substantially overpromises.

H2. MIA framework is not membership inference.
- harness/mia_framework.py:96-152: labels records by index parity after sorting, trains logistic regression on 7 metadata features, passes if AUC <= 0.60. Expected AUC ~0.5 by construction; observed 0.416. Pass is near-guaranteed.
- Trivial-pass escape hatches: under 20 records or class imbalance returns ok=True, auc=0.5 (lines 101-125).
- Self-discloses as a smoke test (line 146), but CLAUDE.md Phase 5 promised a shadow-model MIA per Nature Sci Rep 2024. Release evidence citing this as memorization validation would be misleading.
- tests/test_mia_framework.py never tests the failing branch; CLI test accepts returncode in {0,1}.

H3. Strict Presidio F1 (0.0884) is unfair and irreproducible. Do not quote it.
- benchmarks/presidio_adapter.py (~lines 186-190) sets mapped_type via next(iter(frozenset)) — an arbitrary pick. Presidio PERSON maps to {NAME_PATIENT, NAME_PROVIDER, NAME_HOUSEHOLD}; a correct exact-offset detection scores FP+FN whenever the pick differs from gold.
- frozenset-of-str iteration order depends on PYTHONHASHSEED, so the strict result is not reproducible across runs.
- benchmarks/results/presidio-stock/ records tool as "presidio-stock-unknown": Presidio version not captured.
- Strict profile also double-reports gap logic (adapter lines 324-328, metrics.py lines 420-425): gap types counted as FNs while gap_detection_rate is still reported from legacy logic.

H4. Weak validators (schema-level, not semantic).
- validators/taxonomy_validator.py: schema check only; entity_type may be any string (line 94). No closed vocabulary, so "taxonomy closure" (CLAUDE.md Phase 5) is not enforced.
- validators/citation_validator.py:24-33: any non-empty string passes; no cross-check against the authority matrix.
- validators/format_parse_validator.py: hl7v2 check is substring "MSH|" and "PID|" (line 58); eml is "Subject:" (line 61); unknown formats hit an else branch (lines 66-68) whose body is dead code, so unknown formats always pass; xlsx parsed only if it looks like JSON (lines 22-30,55-57).

### Medium

M1. Em-dash violations of the CLAUDE.md formatting ban: generators/hipaa_safe_harbor.py lines 14, 135, 151, 164, 190, 233 (U+2014). No em-dashes or emojis found in corpus JSONL or other generators.

M2. hash_validator gaps.
- validators/hash_validator.py:13-24: fallback path resolution can silently resolve a wrong manifest path to a different file.
- No reverse coverage check: a .jsonl added to corpus/ but absent from the manifest passes silently (tampering by addition undetected).

M3. Unguarded write and log risk in llm_detector.
- phi_engine/security/llm_detector.py:124-128: _write_review_queue writes audit/human_review/llm_uncertain.jsonl relative to cwd, bypassing zone guards (headers only, limited exposure). Untested.
- llm_detector.py:172-174: logs exception from failed LLM call at WARNING; SDK errors that echo request bodies could put prompt text into logs. Risk, not proven.

M4. Reproducibility gaps.
- requirements.txt uses >= floors (one ==), no lockfile; spaCy model referenced inconsistently (en_core_web_lg vs en_core_web_sm).
- docs/REPRODUCIBILITY.md gives commands and seed 42 but no expected hashes or F1 values to compare against.
- validation_report.json, mia_report.json, release_evidence.json carry no timestamps; provenance rests on the hash chain only. Reports also lack git-sha and seed provenance.
- release_evidence.py:44-53: claim_level logic counts file_formats as a jurisdiction; never verifies the validation report says PASS before hashing it as evidence (only downgrades claim level).

M5. Jurisdiction mixing in file_formats.
- validators/jurisdiction_separator.py:40-47 enforces folder==jurisdiction, but corpus/file_formats/ accepts any of the 6 jurisdictions in one file — in tension with the "strictly separated, never mixed" directive.

M6. Field name deviation: CLAUDE.md mandates authority_citation; corpus emits authority_citations (plural list) plus per-span authority. Substantively compliant, nominally nonconforming.

M7. Stray junk file "=1.5.2" at repo root (botched pip redirect), untracked.

M8. Test coverage gaps beyond those above: no unit failure-path tests for taxonomy_validator, format_parse_validator, or no_real_phi_static_validator; no test asserts any real tool is decorated with llm_safe_tool (none is); no test that prompts are screened (they are not).

M9. physionet_adapter.py:122 uses placeholder length as proxy for span end; its offsets are unreliable for exact-match scoring. benchmarks/modified_deidentify_adapter.py is an explicit stub (lines 161-179), honestly disclosed in KNOWN_LIMITATIONS.md:126-128.

M10. config.py:661 silently rewrites openai-oauth base_url to http://127.0.0.1:10531/v1 whenever "10531" is absent, which can mask a misconfigured base_url. config.py:603 accepts literal "oauth" as api_key (benign, localhost proxy). No hardcoded secrets anywhere.

### Documentation truth audit (mostly positive)

- Docs are unusually honest. No "IRB-ready", "world-leading", or coverage-percentage claims survive. README caps claims at L1 strong / L2-L3 partial / L4-L5 unsupported (README.md:4,25-31) and disclaims certification (README.md:12,121). KNOWN_LIMITATIONS.md:71 correctly calls clinician review a blocker.
- Soft spots: README.md:10 "IRB-oriented" is defensible but clinician and counsel review docs are templates with zero completed reviews; PHI_SYSTEM_TRUST_REPORT.md:7 cites "258 passed" which is stale (currently 297) and not re-verifiable without CI; README understates jurisdiction coverage relative to the current manifest (conservative direction).
- Comparative F1s in docs/IMPLEMENTATION_PLAN.md:47-49 are cited from literature (PMC12719064), not repo runs. No unsupported "we beat X" claims found.

## Alignment vs CLAUDE.md Plan

- Phase 1 (legacy v1.0.1 import): not done. No corpus/legacy_v1.0.1/.
- Phase 2: all 10 HIPAA generators done, deep coverage. DPDPA: only Rule 14 identifiers (in_dpdpa.py); second_schedule, pediatric_exemption, algorithmic_dd, consent_manager missing; breach timing only as narrative text (in_dpdpa.py:128-129). ICMR: all four generators absent. Indian identifiers: ration card and ESI/CGHS/BPL absent (UAN exists); driving license state-variant coverage unconfirmed. Quasi-identifiers covered inside hipaa_safe_harbor.py:575. Out-of-plan additions: au, br, eu, ug jurisdictions.
- Phase 3: 5 of 12 file-format generators present (xlsx, dicom_header, fhir, hl7v2, eml). Missing: csv, pdf, docx, cda, exif, parquet, sqlite.
- Phase 4: Presidio adapter fully implemented (stock/tuned, strict/legacy); spaCy, Philter, PyDeID implemented with import guards; CliniDeID and PhysioNet wrap external tools; Modified Deidentify stubbed; Comprehend, Azure, JSL absent (registry "planned" only). metrics.py arithmetic correct, IoU with half-open spans, greedy first-fit matching (minor).
- Phase 5: generate_corpus, run_all_validations, mia_framework (smoke only), release_evidence exist; clinical_plausibility_review.py absent.
- Phase 6: all listed docs exist.
- Phase 7: essentially absent. No .github/ (no CI, no issue templates), no CONTRIBUTING.md, CODE_OF_CONDUCT.md, or CHANGELOG.md; SECURITY.md exists but no maintainer contact configured.
- Phase 8: not started. Version 2.0.0-dev, no tarball.

## Recommendations (priority order)

1. Wire the LLM guards into the three live call paths and screen prompts (egress direction), not just returns. Add tests asserting the boundary is active and covering the known regex bypasses (C1, C2, C3).
2. Fix the mapped_type arbitrary-pick bug in presidio_adapter.py (score against the full mapped set), record tool versions in artifacts, re-run the strict benchmark (H3).
3. Strengthen or rename weak validators: closed entity-type vocabulary for taxonomy closure; citation check against the authority matrix; reverse manifest coverage in hash_validator; remove the dead else in format_parse_validator; rename or rebuild no_real_phi_static_validator (H1, H4, M2).
4. Implement a real shadow-model MIA or rename the artifact so release evidence cannot be read as memorization validation (H2).
5. Pin dependencies (lockfile), add expected hashes and metrics to REPRODUCIBILITY.md, add timestamps and git-sha to all report JSONs, stand up CI (M4, Phase 7).
6. Close or formally descope the Phase 2/3 gaps (ICMR, remaining DPDPA, 7 file formats, legacy import) in KNOWN_LIMITATIONS.md.
7. Housekeeping: delete "=1.5.2", remove em-dashes in hipaa_safe_harbor.py, update the stale "258 passed" figure in PHI_SYSTEM_TRUST_REPORT.md (M1, M7).

## Confidence Statement

I cannot confirm this is the best PHI handling system built. The repo contains no comparative evidence supporting that claim, its one head-to-head strict benchmark number is biased by a scoring bug, and the system is a corpus and benchmark framework rather than a de-identification engine competing on detection quality. The repo's own documentation correctly refuses the claim; this audit sustains that refusal. The strongest true statement supported by evidence: this is a deterministic, integrity-verified, multi-jurisdiction PHI benchmark corpus at claim level L2-partial with an honest evidence chain and a security layer that is designed but not yet enforced.
