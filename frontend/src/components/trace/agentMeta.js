// ---- Live agent trace ---------------------------------------------------
//
// Agent metadata: which agent, what it does, why it does, how it does.
// Rendered in the expanded trace row so operators can see the pipeline's
// intent at every step -- not just the LLM prompt/reply text. Sir Q
// "add which agent, what it does, why it does, how it does under the
// agent traces". Keys match `m.agent` values emitted by the orchestrator.
//
// Note: `Sentinel` remains a live key here (the orchestrator still emits
// trace rows under that agent name) even though docs #43-46/Phase 8 later
// renamed the role to `Reviewer`. Phase 17 (docs #17/section on retiring
// dead-agent names, `trace_projection.py:19-28`) is the phase scoped to
// reconciling this -- not touched here per this phase's own instructions.
export const AGENT_META = {
  Lexicon: {
    role: 'Dictionary / codebook specialist',
    what: 'Reads the data dictionary or mapping file (.xlsx / .csv / .docx) and extracts column names, definitions, and value code maps.',
    why: 'Judge needs the human-authored definition of every column to classify it. Without a dictionary, a header like "acct" is ambiguous; with one, it may say "hospital account number" — clearly HIPAA §164.514(b)(2)(i)(H).',
    how: 'Safe XLSX/DOCX parsing (defusedxml, zip-bomb caps), header/row normalisation, one LLM call to summarise column semantics. Never reads dataset rows.',
  },
  Schema: {
    role: 'Dataset headers specialist',
    what: 'Reads ONLY the column headers of every dataset in the study package. Row values are never sent to the LLM.',
    why: 'This is the zero-row-read invariant from `GOAL.md`. The LLM cannot leak PHI it never saw. Judge classifies from header + dictionary + form context alone.',
    how: 'Deterministic CSV/XLSX header extraction, then one LLM call that receives ONLY the header list plus Lexicon enrichment. Row values stay on disk.',
  },
  Instrument: {
    role: 'Collection form specialist',
    what: 'Reads collection form PDFs (CRFs, intake sheets) end-to-end, extracting field labels, groupings, and instructions.',
    why: 'Forms tell us the original clinical intent of each variable — critical when a header alone is ambiguous. Forms are collection instruments, not PHI-carrying rows, so full read is safe.',
    how: 'pypdf text extraction with OCR fallback (tesseract) for scanned forms. One LLM call summarises fields and cross-references dataset headers.',
  },
  Statute: {
    role: 'Jurisdictional rulebook expert',
    what: 'Fetches the current statutory PHI-handling rules for the session\'s jurisdiction (US = HIPAA §164.514 Safe Harbor).',
    why: 'Rules change. A cached-from-training rulebook goes stale; a live authoritative fetch keeps every classification defensible in an IRB submission.',
    how: 'Cache-first (weekly refresh), Claude native web-search on miss, deterministic fallback pack when the network denies. Every rule is citation-pinned.',
  },
  Praxis: {
    role: 'PHI transformation methods expert',
    what: 'For each HIPAA identifier category (A through R), retrieves the current best-practice transformation technique with a paper reference.',
    why: 'Handling is not blanket redaction. Age 96 should become "90+" (retains signal); dates should truncate to year; ZIP truncates to ZIP3 minus 17 restricted prefixes. Praxis tells Judge which technique applies where.',
    how: 'Cache-first (weekly refresh). Well-defined categories (A/B/C/D/F/G/H) use deterministic fallbacks and skip the web (free + fast). E/I..R hit web-search with source citations.',
  },
  Judge: {
    role: 'Per-column classifier',
    what: 'Assigns one action per column: keep, drop, cap_age_90, year_only, zip3_truncate, hash, pseudonymize, scrub_text, or human_review.',
    why: 'This is the core decision point. It fuses Schema headers + Instrument forms + Lexicon dictionary + Statute rules + Praxis techniques into a single defensible per-column verdict.',
    how: 'One LLM call per iteration. Sees only header context (never rows). Emits JSON with action, reason, citation, and confidence per column.',
  },
  Sentinel: {
    role: 'Zero-leak reviewer',
    what: 'Reviews every Judge decision for PHI leaks and method appropriateness. Returns blocking or advisory issues.',
    why: 'The whole system\'s promise is 0% PHI leak and 100% method appropriateness. Sentinel is the guardrail — it makes Judge revise (up to `ITERATION_CAP=2` iterations) or escalates to human review.',
    how: 'Deterministic hard-rule pass (dob→year_only, ssn→drop, mrn→pseudonymize, phone/email→drop, name→drop, zip→zip3_truncate) FIRST, then an LLM Sentinel to catch the non-obvious.',
  },
  Executor: {
    role: 'Deterministic applier (no LLM)',
    what: 'Applies the approved per-column actions to the actual dataset rows and writes handled CSV exports.',
    why: 'This is the only phase that touches raw rows. It is deterministic on purpose — no LLM decision at write time means no LLM leak path at write time.',
    how: 'Pure Python. `drop` empties, `year_only` truncates dates, `cap_age_90` collapses >89 to "90+", `pseudonymize` = HMAC-SHA256(session_salt, value), `scrub_text` = Presidio + regex on free-text cells.',
  },
  PublishGuard: {
    role: 'Last-mile boundary scan (no LLM)',
    what: 'Scans every export byte for residual PHI shapes (SSN, phone, email, ZIP, dates, names) before any download is authorised.',
    why: 'Belt-and-braces. If Executor missed a cell, or a scrub_text left a residue, Guard catches it and blocks download until the operator resolves it.',
    how: 'Deterministic regex + Presidio pass over every exported artifact. Returns clean or blocked with per-file findings.',
  },
  Auditor: {
    role: 'Compliance metrics scorer',
    what: 'Scores the handled bundle against the classification: precision, recall, F1 per HIPAA category; completeness against Sentinel advisories.',
    why: 'Turns "we ran the pipeline" into "here are the numbers a reviewer can cite." Feeds the paper draft and the attestation.',
    how: 'One LLM call for narrative + a deterministic metric pass over Executor exports vs. approved decisions.',
  },
  Scout: {
    role: 'Competitor landscape',
    what: 'Compiles current alternative PHI-handling tools (Presidio, Comprehend Medical, Azure, John Snow Labs, etc.) with headline capability deltas.',
    why: 'Publication requires a comparison. Scout supplies the "against which baselines?" context so Ledger can compute meaningful deltas.',
    how: 'Cache-first, one Claude web-search call, JSON list of {name, vendor, url, capability_summary}.',
  },
  Ledger: {
    role: 'Comparative benchmark writer',
    what: 'Produces Ledger.Compare (per-competitor delta table) and Ledger.Aggregate (headline metrics + recommendations).',
    why: 'The paper needs a benchmark section that says "we score X, competitors score Y, here is why." Ledger is that section, drafted from Auditor + Scout inputs.',
    how: 'Two LLM sub-calls (split so no single call exceeds the 90s timeout). Compare emits deltas per competitor; Aggregate emits the headline + recommendations.',
  },
  Herald: {
    role: 'Manuscript drafter',
    what: 'Drafts a publication-grade manuscript: title, abstract, methods, results, discussion, limitations, target venue.',
    why: 'Handoff to publishing. Delivers the study team a first-pass paper draft rather than raw metrics.',
    how: 'Two LLM sub-calls: Herald.Abstract (title + abstract + methods + refs) and Herald.Sections (results + discussion + limitations). Split to stay under the LLM timeout.',
  },
};

export function agentMetaFor(agent) {
  if (!agent) return null;
  // Match by exact agent name; special-case Publish Guard (no dot in the
  // orchestrator's phase key, so we synthesise the display key).
  if (agent === 'Publish Guard' || agent === 'PublishGuard') return AGENT_META.PublishGuard;
  return AGENT_META[agent] || null;
}
