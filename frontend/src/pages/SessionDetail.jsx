import React, { useEffect, useRef, useState } from 'react';
import { useParams, useSearchParams, useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import axios from 'axios';
import { API, getSession, streamUrl } from '../lib/api';
import { Btn, Panel, Tag } from '../components/ui';

function StatusChip({ status }) {
  const map = {
    complete: 'accept',
    partially_complete: 'signal',
    awaiting_human_review: 'signal',
    classifying: 'signal',
    anonymizing: 'signal',
    reading: 'signal',
    cancelled: 'default',
    intake_failed: 'reject',
    failed: 'reject',
  };
  return <Tag color={map[status] || 'default'} testId="status-chip">{status || 'unknown'}</Tag>;
}

// Simple SVG spinner for pending state
function Spinner() {
  return (
    <svg className="animate-spin w-3.5 h-3.5 text-oxblood" viewBox="0 0 24 24" fill="none">
      <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" strokeOpacity="0.25"/>
      <path d="M22 12a10 10 0 0 1-10 10" stroke="currentColor" strokeWidth="3" strokeLinecap="round"/>
    </svg>
  );
}

// ---- Live agent trace ---------------------------------------------------
//
// Agent metadata: which agent, what it does, why it does, how it does.
// Rendered in the expanded trace row so operators can see the pipeline's
// intent at every step -- not just the LLM prompt/reply text. Sir Q
// "add which agent, what it does, why it does, how it does under the
// agent traces". Keys match `m.agent` values emitted by the orchestrator.
const _AGENT_META = {
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

function _agentMetaFor(agent) {
  if (!agent) return null;
  // Match by exact agent name; special-case Publish Guard (no dot in the
  // orchestrator's phase key, so we synthesise the display key).
  if (agent === 'Publish Guard' || agent === 'PublishGuard') return _AGENT_META.PublishGuard;
  return _AGENT_META[agent] || null;
}

// ---- Live agent trace ---------------------------------------------------
//
// The pipeline emits one AgentMessage per LLM call (direction='in' before
// the call, 'out' after, with duration_ms). Rendering these live gives the
// operator continuous feedback -- Sir's Q4: "the user must feel like the
// process is moving further instead of them looking at a loading screen".
//
// Design: group by (agent, phase_key) so a Judge iteration shows as one
// row that flips from 'running' (only in) to 'done' (has out) once the
// LLM returns. Duration is read straight from the 'out' message. We keep
// prompt/reply previews collapsible so operators can drill in to WHY an
// agent decided what it did, without cluttering the default view.
function _groupTrace(rawMessages) {
  const groups = new Map();
  const order = [];
  // Tier 3: message id -> the group key that message ended up in, so a
  // child group's `parent_id` (another message's id) can be resolved to
  // the PARENT GROUP once every message has been walked at least once.
  const idToKey = new Map();
  for (const m of rawMessages || []) {
    // Sir Q "praxis trace overwhelm" -- collapse the 17 per-category Praxis
    // rows into a single "Praxis · N methods" group. Every category still
    // lives in the expanded detail panel below; the top row just fires once.
    let key;
    if (m.agent === 'Praxis') {
      key = 'Praxis::praxis.all';
    } else {
      key = `${m.agent}::${m.phase}`;
    }
    if (!groups.has(key)) order.push(key);
    const existing = groups.get(key) || { key, agent: m.agent, phase: m.phase, ts: m.ts, parentMsgId: null };
    if (m.agent === 'Praxis') {
      // Override phase label with a per-group counter.
      existing.phase = 'praxis.methods';
      existing.praxis_count = (existing.praxis_count || 0) + 1;
      existing.praxis_categories = existing.praxis_categories || [];
      const catMatch = (m.phase || '').match(/:([A-Z]|[a-z_]+)$/);
      if (catMatch) existing.praxis_categories.push(catMatch[1]);
    }
    if (m.id) idToKey.set(m.id, key);
    if (m.parent_id && !existing.parentMsgId) existing.parentMsgId = m.parent_id;
    if (m.direction === 'in') {
      existing.prompt_text = m.payload?.prompt_text || existing.prompt_text;
      existing.tool = m.payload?.tool || existing.tool;
      existing.status_text = m.status_text || existing.status_text;
      existing.started = m.ts;
      existing.state = existing.state || 'running';
    } else if (m.direction === 'out') {
      existing.reply_text = m.payload?.reply_text || existing.reply_text;
      existing.error = m.payload?.error;
      // For collapsed Praxis, sum durations so "N s of LLM time" reflects total.
      if (m.agent === 'Praxis') {
        existing.duration_ms = (existing.duration_ms || 0) + (m.duration_ms || 0);
      } else {
        existing.duration_ms = m.duration_ms;
      }
      existing.state = m.payload?.error ? 'errored' : 'done';
      existing.ended = m.ts;
    } else if (m.direction === 'info') {
      existing.state = existing.state || 'info';
      existing.info = { ...(existing.info || {}), ...(m.payload || {}) };
    }
    groups.set(key, existing);
  }
  const list = order.map(k => groups.get(k));
  for (const g of list) {
    if (g.parentMsgId) {
      const parentKey = idToKey.get(g.parentMsgId);
      // A parent id pointing outside this fetched page, or at itself,
      // renders at the top level rather than being dropped or mis-nested.
      g.parentGroupKey = parentKey && parentKey !== g.key ? parentKey : null;
    } else {
      g.parentGroupKey = null;
    }
  }
  return list.sort((a, b) => (a.ts || '').localeCompare(b.ts || ''));
}

function AgentTracePanel({ sid, trace, status, cancelRequested, advisory }) {
  const [openKeys, setOpenKeys] = useState(() => new Set());
  const grouped = _groupTrace(trace);
  const byKey = new Map(grouped.map(g => [g.key, g]));
  const topLevel = grouped.filter(g => !g.parentGroupKey || !byKey.has(g.parentGroupKey));
  const childrenByParent = new Map();
  for (const g of grouped) {
    if (g.parentGroupKey && byKey.has(g.parentGroupKey)) {
      if (!childrenByParent.has(g.parentGroupKey)) childrenByParent.set(g.parentGroupKey, []);
      childrenByParent.get(g.parentGroupKey).push(g);
    }
  }
  const running = status && !['complete', 'awaiting_human_review', 'partially_complete', 'failed', 'cancelled', 'intake_failed'].includes(status);
  const totalMs = grouped.reduce((s, g) => s + (g.duration_ms || 0), 0);

  // Sir Q "Trace Meta Deep-Link": on mount, if the URL hash points at a
  // specific agent (e.g. #trace-Judge), auto-expand that row so a shared
  // link lands the reviewer straight on the cited behaviour.
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const hash = window.location.hash || '';
    const m = hash.match(/^#trace-([A-Za-z]+)$/);
    if (!m) return;
    const wanted = m[1].toLowerCase();
    const target = grouped.find(g => (g.agent || '').toLowerCase() === wanted);
    if (target) {
      setOpenKeys(prev => new Set(prev).add(target.key));
      // Small toast so the operator knows why the page auto-scrolled and
      // which agent the shared link is citing (Sir Q "Deep-Link Anchor
      // Toast: linked from IRB reviewer").
      toast(`Deep-link opened: ${m[1]} · scroll below for the reviewer's citation`, {
        duration: 4500,
      });
      // Defer scroll so the DOM node exists.
      setTimeout(() => {
        const el = document.getElementById(`trace-${m[1]}`);
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }, 100);
    }
    // Only run once per grouped-length change, so late-arriving SSE events
    // still resolve the hash if the target agent hadn't reported yet.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [grouped.length]);

  const copyDeepLink = (agentName) => {
    const url = `${window.location.origin}${window.location.pathname}#trace-${agentName}`;
    try {
      navigator.clipboard.writeText(url);
      toast.success(`Link to ${agentName} copied`);
    } catch (err) {
      console.warn('clipboard write failed:', err);
      toast.error('Copy failed. Select the URL bar and copy manually.');
    }
  };

  const toggle = (key) => setOpenKeys(prev => {
    const next = new Set(prev);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });

  const renderRow = (g, depth) => (
    <div key={g.key} className={depth ? 'py-3 border-l-2 border-rule' : 'py-3'}
         style={depth ? { marginLeft: `${depth * 20}px`, paddingLeft: '12px' } : undefined}
         id={`trace-${g.agent}`} data-testid={`trace-row-${g.key}`}>
      <button
        className="w-full text-left flex items-center gap-4 hover:bg-paper-2 px-2 -mx-2 py-1 transition-colors"
        onClick={() => toggle(g.key)}
        data-testid={`trace-row-toggle-${g.key}`}
      >
        <div className="w-24 shrink-0">
          <TraceStateBadge state={g.state} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="font-display text-ink text-[14px]">
            <span className="text-oxblood">{g.agent}</span>
            <span className="text-ink-muted mx-2">·</span>
            <span className="font-mono text-[12px] text-ink-2">{g.phase}</span>
            {g.praxis_count && (
              <span className="ml-2 font-mono text-[11px] text-ink-muted">
                {g.praxis_count} method{g.praxis_count === 1 ? '' : 's'}
              </span>
            )}
          </div>
          {g.status_text && (
            <div className="text-[11px] text-ink-muted mt-0.5">{g.status_text}</div>
          )}
          {g.error && (
            <div className="font-mono text-[11px] text-oxblood mt-0.5">{g.error}</div>
          )}
        </div>
        <div className="text-right shrink-0">
          <div className="font-mono text-[12px] text-ink">
            {g.duration_ms ? `${(g.duration_ms / 1000).toFixed(1)} s` : (g.state === 'running' ? '…' : '')}
          </div>
          {g.tool && <div className="font-mono text-[10px] text-ink-muted">{g.tool}</div>}
        </div>
      </button>
      {openKeys.has(g.key) && (
        <div className="mt-3 pl-24 space-y-3" data-testid={`trace-row-details-${g.key}`}>
          {(() => {
            const meta = _agentMetaFor(g.agent);
            if (!meta) return null;
            return (
              <div className="border-l-2 border-oxblood pl-3" data-testid={`trace-row-meta-${g.key}`}>
                <div className="flex items-baseline justify-between gap-3">
                  <div className="kicker text-oxblood">{g.agent} · {meta.role}</div>
                  <button
                    onClick={(e) => { e.stopPropagation(); copyDeepLink(g.agent); }}
                    className="font-mono text-[10px] text-ink-muted hover:text-oxblood transition-colors"
                    data-testid={`trace-row-copylink-${g.key}`}
                    title={`Copy deep-link to ${g.agent}`}
                  >
                    # copy link
                  </button>
                </div>
                <div className="mt-2 grid grid-cols-1 md:grid-cols-3 gap-3 text-[12px] text-ink-2 leading-relaxed">
                  <div>
                    <div className="kicker text-ink-muted">What it does</div>
                    <div className="mt-1">{meta.what}</div>
                  </div>
                  <div>
                    <div className="kicker text-ink-muted">Why it does it</div>
                    <div className="mt-1">{meta.why}</div>
                  </div>
                  <div>
                    <div className="kicker text-ink-muted">How it does it</div>
                    <div className="mt-1">{meta.how}</div>
                  </div>
                </div>
              </div>
            );
          })()}
          {g.praxis_categories && g.praxis_categories.length > 0 && (
            <div>
              <div className="kicker text-ink-muted">HIPAA categories consulted</div>
              <div className="mt-1 flex flex-wrap gap-1">
                {g.praxis_categories.map((c, ci) => (
                  <span key={ci} className="font-mono text-[10px] px-2 py-0.5 bg-paper-2 border border-rule">
                    {c}
                  </span>
                ))}
              </div>
            </div>
          )}
          {g.prompt_text && (
            <div>
              <div className="kicker text-ink-muted">Prompt (full, uncapped)</div>
              <pre className="mt-1 text-[11px] text-ink-2 whitespace-pre-wrap font-mono bg-paper-2 p-3 rounded border border-rule">
                {g.prompt_text}
              </pre>
            </div>
          )}
          {g.reply_text && (
            <div>
              <div className="kicker text-ink-muted">Reply (full, uncapped)</div>
              <pre className="mt-1 text-[11px] text-ink-2 whitespace-pre-wrap font-mono bg-paper-2 p-3 rounded border border-rule">
                {g.reply_text}
              </pre>
            </div>
          )}
          {g.info && Object.keys(g.info).length > 0 && (
            <div>
              <div className="kicker text-ink-muted">Info</div>
              <pre className="mt-1 text-[11px] text-ink-2 whitespace-pre-wrap font-mono bg-paper-2 p-3 rounded border border-rule">
                {JSON.stringify(g.info, null, 2)}
              </pre>
            </div>
          )}
        </div>
      )}
      {(childrenByParent.get(g.key) || []).map(child => renderRow(child, depth + 1))}
    </div>
  );

  return (
    <div className="mt-10 rule-bottom pb-10" data-testid="agent-trace-panel">
      <div className="flex items-baseline justify-between">
        <div>
          <div className="kicker">Agent trace</div>
          <div className="font-display text-[22px] text-ink mt-1">
            {grouped.length} agent call{grouped.length === 1 ? '' : 's'} · {(totalMs / 1000).toFixed(1)} s of LLM time
          </div>
        </div>
        {running && (
          <div className="flex items-center gap-2 text-[12px] text-ink-2">
            <Spinner /> pipeline in phase <span className="font-mono">{status}</span>
            {cancelRequested && <span className="font-mono text-oxblood ml-2">· cancel pending</span>}
          </div>
        )}
      </div>

      {grouped.length === 0 && (
        <div className="mt-6 text-[13px] text-ink-muted">
          Waiting for the first agent to check in… (SSE stream is open, this list will populate live.)
        </div>
      )}

      <div className="mt-6 divide-y divide-rule">
        {topLevel.map(g => renderRow(g, 0))}
      </div>

      {advisory && advisory.length > 0 && (
        <div className="mt-6 border-l-2 border-signal pl-4" data-testid="trace-advisory">
          <div className="kicker text-signal">Sentinel advisory issues ({advisory.length})</div>
          <div className="mt-2 space-y-1 text-[12px] text-ink-2">
            {advisory.slice(0, 8).map((a, i) => (
              <div key={i} className="font-mono">
                {a.column || '?'}: {a.problem || a.suggested_action || '—'}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function TraceStateBadge({ state }) {
  const map = {
    running: { color: 'bg-signal text-paper', label: 'RUNNING' },
    done: { color: 'bg-clean text-paper', label: 'DONE' },
    errored: { color: 'bg-oxblood text-paper', label: 'ERROR' },
    info: { color: 'bg-ink-muted text-paper', label: 'INFO' },
  };
  const cfg = map[state] || map.info;
  return (
    <span className={`inline-block px-2 py-0.5 text-[10px] font-mono ${cfg.color}`}>
      {cfg.label}
    </span>
  );
}

// ---- Tier 1: live one-line narration ------------------------------------
//
// A single, always-visible, continuously-updating strip of plain-language
// status lines ("Reading the dictionary file enrollment.csv", "Judge is
// deciding how to handle every flagged column", ...), sourced from each
// call's `status_text` field. Never gated behind a click, and never
// cleared once the run finishes -- it becomes a readable scrollback of
// what happened, distinct from the detailed (tier 2/3) trace below it.
function LiveNarrationStrip({ trace }) {
  const bottomRef = useRef(null);
  const lines = (trace || [])
    .filter(m => m.direction === 'in' && m.status_text)
    .sort((a, b) => (a.ts || '').localeCompare(b.ts || ''));

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'end' });
  }, [lines.length]);

  if (lines.length === 0) return null;

  return (
    <div className="mt-6 border border-rule bg-paper-2/50 px-4 py-3" data-testid="live-narration-strip">
      <div className="kicker mb-2">What's happening</div>
      <div className="max-h-40 overflow-y-auto space-y-1 step-in" data-testid="live-narration-lines">
        {lines.map((m, i) => (
          <div key={m.id || i} className="text-[12px] text-ink-2 leading-5" data-testid={`narration-line-${i}`}>
            <span className="font-mono text-oxblood">{m.agent}</span>
            <span className="text-ink-muted mx-1.5">·</span>
            {m.status_text}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}

// ---- Phased progress bar ----------------------------------------------
//
// Sir Q "show the user a progress bar going through each phase and what
// agent is working on each phase". Short human descriptions render up
// front; drill-down into the agent trace panel below for detail.
//
// The phase keys below match the `on_phase(...)` labels emitted by the
// orchestrator. Order matches pipeline execution.
const _PHASES = [
  { key: 'specialists', label: 'Reading study files', blurb: 'Lexicon (dictionary), Schema (dataset headers only) and Instrument (forms) read the three study components in parallel.' },
  { key: 'statute',     label: 'Fetching regulations', blurb: 'Statute agent web-searches the current jurisdictional rules (HIPAA §164.514 for US).' },
  { key: 'praxis',      label: 'Compiling PHI methods', blurb: 'Praxis pulls the current best-practice technique per HIPAA identifier category from the web (cached across sessions).' },
  { key: 'judge_iter',  label: 'Classifying variables', blurb: 'Judge decides keep vs transform per column using Statute rules + Praxis techniques.' },
  { key: 'sentinel_iter', label: 'Verifying classification', blurb: 'Sentinel reviews every decision; iterates only when a real PHI leak is at stake.' },
  { key: 'human_review_required', label: 'Awaiting your review', blurb: 'Sentinel flagged cases the model cannot resolve; sign off in the panel below.' },
  { key: 'executor',    label: 'Applying transforms', blurb: 'Executor deterministically applies drop / year_only / cap_age_90 / pseudonymize / hash / scrub_text per Judge decision.' },
  { key: 'publish_guard', label: 'Last-mile PHI scan', blurb: 'Publish Guard scans every export byte for residual PHI shapes before any download is allowed.' },
  { key: 'auditor_scout', label: 'Auditing + research', blurb: 'Auditor scores completeness against the classification while Scout compiles the competitor landscape.' },
  { key: 'ledger',      label: 'Comparative benchmark', blurb: 'Ledger.Compare (deltas per competitor) + Ledger.Aggregate (headline + recommendations) build the benchmark report.' },
  { key: 'herald',      label: 'Drafting manuscript', blurb: 'Herald.Abstract (title + methods + refs) + Herald.Sections (results + discussion + limitations) draft the publication.' },
  { key: 'complete',    label: 'Ready to share', blurb: 'Safe-to-share bundle passes Publish Guard clean; attestation is signed; download below.' },
];

function _phaseIndexFromEvents(events, status) {
  if (status === 'complete' || status === 'partially_complete') return _PHASES.length - 1;
  if (status === 'cancelled' || status === 'failed') return -1;
  // Walk events newest-first, match the phase prefix.
  for (let i = (events || []).length - 1; i >= 0; i--) {
    const p = events[i]?.phase || '';
    for (let k = _PHASES.length - 1; k >= 0; k--) {
      if (p === _PHASES[k].key || p.startsWith(_PHASES[k].key)) return k;
    }
  }
  return 0;
}

function PipelineProgressBar({ events, status, phaseTimings, runElapsed, iterationCap }) {
  const currentIdx = _phaseIndexFromEvents(events, status);
  const isFailed = status === 'failed' || status === 'cancelled';
  const pct = currentIdx < 0
    ? 0
    : Math.round(((currentIdx + 1) / _PHASES.length) * 100);
  const current = currentIdx >= 0 ? _PHASES[currentIdx] : null;

  // Sir Q "Rigor Tooltip on SessionDetail": show the chosen rigor as a
  // chip near the progress bar so reviewers see the confidence trade-off
  // at a glance without leaving the trace page.
  const _RIGOR_META = {
    1: { label: 'Fast', blurb: '1 Sentinel pass · short studies, high-confidence headers' },
    2: { label: 'Balanced', blurb: '2 Sentinel passes · default rigor for most studies' },
    3: { label: 'Thorough', blurb: '3 Sentinel passes · max defensibility, longest wallclock' },
  };
  const rigor = iterationCap ? _RIGOR_META[iterationCap] : null;

  // Sir Q "Live Wallclock Measurement": show per-phase durations once the
  // orchestrator has emitted them. Values come from session.phase_timings
  // (persisted at pipeline exit) or are derived from live SSE events for
  // the running phase.
  const timings = phaseTimings || {};
  const currentPhaseKey = current && (() => {
    // Judge/Sentinel emit iteration-suffixed phase keys; match the base.
    for (let i = (events || []).length - 1; i >= 0; i--) {
      const p = events[i]?.phase || '';
      if (p.startsWith(current.key)) return p;
    }
    return current.key;
  })();
  const currentSec = currentPhaseKey && timings[currentPhaseKey]?.duration_ms
    ? (timings[currentPhaseKey].duration_ms / 1000).toFixed(1)
    : null;

  return (
    <div className="mt-8 mb-2" data-testid="pipeline-progress-bar">
      <div className="flex items-baseline justify-between mb-2">
        <div className="flex items-baseline gap-3">
          <div className="kicker">Pipeline progress · {pct}%</div>
          {rigor && (
            <span
              className="font-mono text-[10px] px-2 py-0.5 bg-paper-2 border border-rule text-ink-2"
              data-testid="pipeline-rigor-chip"
              title={rigor.blurb}
            >
              Rigor · {rigor.label} ({iterationCap})
            </span>
          )}
        </div>
        <div className="flex items-center gap-3 font-mono text-[11px] text-ink-muted">
          {runElapsed != null && (
            <span data-testid="pipeline-elapsed">
              {runElapsed.toFixed(1)} s elapsed
            </span>
          )}
          {current && (
            <span data-testid="pipeline-current-phase">
              phase {currentIdx + 1} of {_PHASES.length}
            </span>
          )}
        </div>
      </div>
      <div className="h-1 bg-paper-2 border border-rule relative overflow-hidden">
        <div
          className={`h-full transition-all duration-500 ${isFailed ? 'bg-oxblood' : 'bg-oxblood'}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      {current && (
        <div className="mt-3 flex items-start gap-3" data-testid="pipeline-current-description">
          <div className="font-mono text-[11px] text-oxblood uppercase tracking-wider mt-0.5 shrink-0">
            {String(currentIdx + 1).padStart(2, '0')}
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-baseline justify-between gap-3">
              <div className="font-display text-[14px] text-ink">{current.label}</div>
              {currentSec && (
                <div className="font-mono text-[11px] text-ink-muted shrink-0" data-testid="pipeline-current-duration">
                  {currentSec} s
                </div>
              )}
            </div>
            <div className="text-[12px] text-ink-2 leading-relaxed mt-0.5">{current.blurb}</div>
          </div>
        </div>
      )}
      {Object.keys(timings).length > 1 && (
        <details className="mt-3" data-testid="pipeline-phase-timings">
          <summary className="cursor-pointer text-[11px] font-mono text-ink-muted hover:text-oxblood">
            per-phase timings ({Object.keys(timings).length})
          </summary>
          <div className="mt-2 grid grid-cols-2 md:grid-cols-3 gap-x-6 gap-y-1 text-[11px] font-mono">
            {Object.entries(timings)
              .sort((a, b) => (a[1].start_s || 0) - (b[1].start_s || 0))
              .map(([k, v]) => (
                <div key={k} className="flex justify-between gap-2 text-ink-2">
                  <span className="truncate">{k}</span>
                  <span className="text-ink-muted shrink-0">
                    {v.duration_ms ? `${(v.duration_ms / 1000).toFixed(1)}s` : '…'}
                  </span>
                </div>
              ))}
          </div>
        </details>
      )}
    </div>
  );
}

export default function SessionDetail() {
  const { sid } = useParams();
  const navigate = useNavigate();
  const [sp] = useSearchParams();
  const wantPub = sp.get('bundle') === 'publication';
  const wantPdf = sp.get('pdf') === '1';

  const [session, setSession] = useState(null);
  const [results, setResults] = useState(null);
  const [trace, setTrace] = useState([]);
  // { [`${file_id}|${column}`]: { mode: 'approve'|'comment'|'defer', comment: string } }
  const [resolutions, setResolutions] = useState({});
  const [reviewer, setReviewer] = useState(() => {
    try { return window.localStorage.getItem('phi_reviewer_id') || ''; }
    catch (err) {
      // localStorage unavailable (e.g. sandboxed iframe / private mode)
      // — fall back to empty and let the operator retype the reviewer id.
      console.warn('phi_reviewer_id read failed:', err);
      return '';
    }
  });
  const [reviewComment, setReviewComment] = useState('');
  const [actualKnowledgeAck, setActualKnowledgeAck] = useState(false);
  const [fileReviewAck, setFileReviewAck] = useState(false);
  const [corpusReport, setCorpusReport] = useState(null);
  const [benchmarkReport, setBenchmarkReport] = useState(null);
  const [devOpen, setDevOpen] = useState(false);
  const [traceOpen, setTraceOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notFound, setNotFound] = useState(false);
  const esRef = useRef(null);
  const traceCursorRef = useRef(null);
  const traceFetchInFlightRef = useRef(false);
  const traceFetchPendingRef = useRef(false);

  // Tier 3: cursor-paginated, appended incrementally rather than a full
  // refetch on every SSE tick -- the trace now carries full, uncapped
  // prompt/reply text, so a full-history refetch per message would grow
  // unbounded over a long run.
  //
  // A live pipeline run emits several SSE ticks per second, each calling
  // `refresh()` -> `fetchTracePage()` unthrottled. Two overlapping calls
  // would both read the same stale `traceCursorRef.current`, both fetch
  // the same page, and both append it, duplicating rows (which then
  // double-counts durations/method counts in `_groupTrace`). Guard with an
  // in-flight flag and coalesce at most one trailing call so a tick that
  // arrives mid-fetch is never silently dropped.
  const fetchTracePage = async () => {
    if (traceFetchInFlightRef.current) {
      traceFetchPendingRef.current = true;
      return;
    }
    traceFetchInFlightRef.current = true;
    try {
      const params = new URLSearchParams({ limit: '500' });
      if (traceCursorRef.current) params.set('after', traceCursorRef.current);
      const { data } = await axios.get(`${API}/sessions/${sid}/agent-trace?${params.toString()}`);
      const page = data.messages || [];
      if (page.length > 0) {
        traceCursorRef.current = data.next_cursor || traceCursorRef.current;
        setTrace(prev => [...prev, ...page]);
      }
    } catch (err) {
      console.warn('agent-trace fetch failed:', err);
    } finally {
      traceFetchInFlightRef.current = false;
      if (traceFetchPendingRef.current) {
        traceFetchPendingRef.current = false;
        fetchTracePage();
      }
    }
  };

  const refresh = async () => {
    const [s, r] = await Promise.all([
      getSession(sid).catch(() => null),
      axios.get(`${API}/sessions/${sid}/results`).then(r => r.data).catch(() => null),
    ]);
    if (!s) { setNotFound(true); return; }
    setNotFound(false);
    setSession(s);
    setResults(r);
    await fetchTracePage();
    // Corpus session: fetch the verifier report once the pipeline is done
    // so the IRB reviewer can see 0-PHI-leak / 100 %-accuracy inline.
    // `corpus_summary` is present on any corpus-mode session (ground truth
    // itself is stripped from session reads for SEC-003).
    if (s.status === 'complete' && s.corpus_summary) {
      axios.get(`${API}/corpus/study/verify/${sid}`)
        .then(vr => setCorpusReport(vr.data))
        .catch(() => setCorpusReport(null));
      axios.get(`${API}/corpus/study/benchmark/${sid}`)
        .then(br => setBenchmarkReport(br.data))
        .catch(() => setBenchmarkReport(null));
    }
  };

  useEffect(() => {
    traceCursorRef.current = null;
    traceFetchInFlightRef.current = false;
    traceFetchPendingRef.current = false;
    setTrace([]);
    refresh();
    let es = null;
    let reconnectTimer = null;
    let disposed = false;
    const connectStream = () => {
      reconnectTimer = null;
      if (disposed) return;
      const stream = new EventSource(streamUrl(sid));
      stream.onmessage = () => refresh();
      stream.onerror = () => {
        stream.close();
        if (es !== stream || disposed || reconnectTimer !== null) return;
        reconnectTimer = window.setTimeout(connectStream, 1000);
      };
      es = stream;
      esRef.current = stream;
    };
    connectStream();
    return () => {
      disposed = true;
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      es?.close();
    };
    // Only re-open the stream when the session id changes; `refresh` closes
    // over `sid` and `setState` setters which are stable across renders.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sid]);

  const status = session?.status;
  const isComplete = status === 'complete';
  const isPartiallyComplete = status === 'partially_complete';
  const isPending = !isComplete && !isPartiallyComplete &&
    !['awaiting_human_review', 'failed', 'cancelled', 'intake_failed'].includes(status);
  // A reviewer may need to act whenever the session first pauses for review
  // OR whenever a partial round leaves columns still pending.
  const reviewNeeded = status === 'awaiting_human_review' || isPartiallyComplete;
  const guard = results?.guard || session?.guard_report;
  const decisions = results?.decisions || [];
  const humanRows = decisions.filter(d => d.action === 'human_review');
  const datasetFiles = (session?.files || []).filter(f => f.kind === 'dataset');
  // The reviewer must have opened at least one dataset file directly before
  // submitting -- required only when there's a file to open at all.
  const fileReviewRequired = datasetFiles.length > 0;
  const fileReviewSatisfied = !fileReviewRequired || fileReviewAck;

  const downloadBundle = async () => {
    setBusy(true);
    try {
      const q = new URLSearchParams();
      if (wantPub) q.set('publication', '1');
      if (wantPdf) q.set('attestation_pdf', '1');
      const url = `${API}/sessions/${sid}/bundle?${q.toString()}`;
      // Trigger a download via a temporary anchor
      const a = document.createElement('a');
      a.href = url; a.download = ''; document.body.appendChild(a); a.click(); a.remove();
    } finally { setBusy(false); }
  };

  const downloadBenchmark = async () => {
    setBusy(true);
    try {
      const url = `${API}/corpus/study/benchmark/${sid}/download`;
      const a = document.createElement('a');
      a.href = url; a.download = ''; document.body.appendChild(a); a.click(); a.remove();
    } finally { setBusy(false); }
  };

  const downloadDatasetFile = (fileId) => {
    const url = `${API}/sessions/${sid}/dataset-file/${fileId}`;
    const a = document.createElement('a');
    a.href = url; a.target = '_blank'; a.rel = 'noopener'; document.body.appendChild(a); a.click(); a.remove();
  };

  const submitReview = async () => {
    if (!reviewer.trim()) { toast.error('Reviewer id is required'); return; }
    const unresolved = humanRows.filter(d => !resolutions[`${d.file_id}|${d.column}`]?.mode);
    if (unresolved.length > 0) {
      toast.error(`Choose approve, comment, or defer for every flagged column (${unresolved.length} left)`);
      return;
    }
    const anyResolution = Object.values(resolutions).some(r => r.mode !== 'defer');
    if (anyResolution && !fileReviewSatisfied) {
      toast.error('You must download and review the original dataset file before submitting');
      return;
    }
    if (anyResolution && !actualKnowledgeAck) {
      toast.error('You must acknowledge the actual-knowledge attestation (45 CFR 164.514(b)(2)(ii)) before submitting');
      return;
    }
    const items = Object.entries(resolutions).map(([key, r]) => {
      const [file_id, ...rest] = key.split('|');
      return { file_id, column: rest.join('|'), mode: r.mode, comment: r.comment || '' };
    });
    try { window.localStorage.setItem('phi_reviewer_id', reviewer.trim()); }
    catch (err) { console.warn('phi_reviewer_id write failed:', err); }
    setBusy(true);
    try {
      const r = await axios.post(`${API}/sessions/${sid}/human-review`, {
        resolutions: items, client_event_id: crypto.randomUUID(), comment: reviewComment,
        actual_knowledge_ack: anyResolution,
      });
      toast.success(`Review submitted (${r.data.status})`);
      setResolutions({}); setReviewComment('');
      setActualKnowledgeAck(false); setFileReviewAck(false);
      await refresh();
    } catch (e) {
      toast.error(`review failed: ${e?.response?.data?.detail || e.message}`);
    } finally { setBusy(false); }
  };

  if (notFound) return (
    <div className="max-w-4xl mx-auto px-10 py-24" data-testid="session-not-found">
      <div className="kicker text-oxblood">Study not found</div>
      <div className="font-display text-display-lg text-ink mt-2">This run isn't here anymore.</div>
      <p className="text-ink-2 mt-4">The system is transient — runs aren't persisted after download. Start a new one.</p>
      <Btn variant="primary" size="lg" className="mt-8" onClick={() => navigate('/')}>Start a new run</Btn>
    </div>
  );
  if (!session) return (
    <div className="max-w-4xl mx-auto px-10 py-24">
      <div className="kicker text-ink-muted flex items-center gap-2"><Spinner/> Loading run</div>
    </div>
  );

  return (
    <div className="max-w-5xl mx-auto px-10 py-16">
      {/* Hero */}
      <div className="rule-bottom pb-10">
        <div className="kicker">Run receipt</div>
        <div className="mt-2 flex items-baseline gap-4 flex-wrap">
          <h1 className="font-display text-display-lg text-ink">
            {isComplete ? 'Handled.'
              : isPartiallyComplete ? 'Partially handled — some columns still pending.'
              : reviewNeeded ? 'Awaiting your review.'
              : status === 'cancelled' ? 'Run cancelled.'
              : isPending ? 'Working on it.'
              : 'Something went wrong.'}
          </h1>
          <StatusChip status={status} />
        </div>
        <div className="mt-3 text-[13px] text-ink-muted font-mono">session {sid}</div>

        {(isComplete || isPartiallyComplete) && (
          <div className="mt-10 flex items-center gap-4">
            <Btn variant="primary" size="lg" onClick={downloadBundle} disabled={busy || guard?.status === 'blocked'} testId="btn-download-bundle">
              {guard?.status === 'blocked' ? 'Bundle blocked'
                : isPartiallyComplete ? `Download partial bundle (${humanRows.length} column(s) withheld) ↓`
                : `Download ${wantPub ? 'publication bundle' : 'safe-to-share bundle'} ↓`}
            </Btn>
            {isComplete && <Btn variant="ghost" onClick={() => navigate('/')} testId="btn-new-run">Start another run</Btn>}
          </div>
        )}
        {isPending && (
          <div className="mt-10 flex items-center gap-4">
            <Btn
              variant="ghost"
              size="lg"
              disabled={busy || session?.cancel_requested}
              testId="btn-cancel-run"
              onClick={async () => {
                setBusy(true);
                try {
                  await axios.post(`${API}/sessions/${sid}/cancel`);
                  toast.info('Cancel requested — pipeline exits at next phase boundary.');
                  await refresh();
                } catch (e) {
                  toast.error(`cancel failed: ${e?.response?.data?.detail || e.message}`);
                } finally { setBusy(false); }
              }}
            >
              {session?.cancel_requested ? 'Cancel pending…' : '■ Stop this run'}
            </Btn>
          </div>
        )}
      </div>

      {/* Pipeline progress bar — high-level "what's happening right now" */}
      <PipelineProgressBar
        events={trace}
        status={status}
        phaseTimings={session?.phase_timings}
        runElapsed={session?.run_elapsed_s}
        iterationCap={session?.iteration_cap}
      />

      {/* Tier 1 — always-visible live narration, persists as history. */}
      <LiveNarrationStrip trace={trace} />

      {/* Tier 2 — full trace, collapsed by default; tier 3 (per-call detail,
          uncapped text, parent/child tree) lives inside once expanded. */}
      <div className="mt-6" data-testid="agent-trace-toggle-wrap">
        <button
          onClick={() => setTraceOpen(o => !o)}
          className="kicker text-ink-2 hover:text-oxblood"
          data-testid="btn-toggle-agent-trace"
        >
          {traceOpen ? '— hide full agent trace' : '+ show full agent trace'}
        </button>
        <div
          className="grid transition-[grid-template-rows] duration-700 [transition-timing-function:cubic-bezier(0.2,0.7,0.2,1)]"
          style={{ gridTemplateRows: traceOpen ? '1fr' : '0fr' }}
        >
          <div className="overflow-hidden">
            <AgentTracePanel
              sid={sid}
              trace={trace}
              status={status}
              cancelRequested={session?.cancel_requested}
              advisory={session?.advisory_issues || []}
            />
          </div>
        </div>
      </div>

      {/* Guard */}
      {guard && (
        <Panel title="Publish Guard" cite="Deterministic residual-PHI scan at the download boundary"
               testId="publish-guard-panel"
               right={
                 <Tag color={guard.status === 'clean' ? 'accept' : 'reject'} testId="publish-guard-status">
                   {guard.status === 'clean' ? 'PHI-handled · safe to share' : 'BLOCKED'}
                 </Tag>
               }>
          <div className="text-body text-ink">
            Scanned <span className="font-mono">{guard.scanned || 0}</span> file(s);
            blocked <span className="font-mono">{guard.blocked || 0}</span>.
          </div>
          {guard.blocked > 0 && (
            <div className="mt-4 space-y-3">
              {(guard.results || []).filter(r => r.status === 'blocked').map((r, i) => (
                <div key={i} className="border-l-2 border-oxblood pl-4" data-testid={`guard-block-${i}`}>
                  <div className="kicker text-oxblood">{(r.file_path || '').split('/').pop()}</div>
                  <div className="mt-2 space-y-1">
                    {(r.findings || []).slice(0, 5).map((f, j) => (
                      <div key={j} className="font-mono text-[12px] text-ink-2">
                        L{f.line} · {f.pattern_id} · cat {f.hipaa_category} · <span className="phi-mask">{f.sample}</span>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </Panel>
      )}

      {/* Corpus verifier report (only for corpus-mode sessions) */}
      {corpusReport && (
        <Panel title="Adversarial corpus verifier"
               cite="Every planted PHI cell scored against Judge decisions + redacted export text"
               testId="corpus-verifier-panel"
               right={
                 <Tag color={
                   (corpusReport.correctness?.overall_f1 || 0) >= 0.999 &&
                   (corpusReport.correctness?.false_negatives || []).length === 0
                     ? 'accept' : 'reject'
                 } testId="corpus-verifier-status">
                   F1 {((corpusReport.correctness?.overall_f1 || 0)).toFixed(4)}
                 </Tag>
               }>
          <div className="grid grid-cols-4 gap-6">
            <div>
              <div className="kicker">Precision</div>
              <div className="font-display text-display-md text-oxblood" data-testid="corpus-precision">
                {(corpusReport.correctness?.overall_precision || 0).toFixed(4)}
              </div>
            </div>
            <div>
              <div className="kicker">Recall</div>
              <div className="font-display text-display-md text-oxblood" data-testid="corpus-recall">
                {(corpusReport.correctness?.overall_recall || 0).toFixed(4)}
              </div>
            </div>
            <div>
              <div className="kicker">Accuracy</div>
              <div className="font-display text-display-md text-oxblood" data-testid="corpus-accuracy">
                {(corpusReport.correctness?.overall_accuracy || 0).toFixed(4)}
              </div>
            </div>
            <div>
              <div className="kicker">Deferrals</div>
              <div className="font-display text-display-md text-ink" data-testid="corpus-deferrals">
                {corpusReport.deferral?.count || 0}
              </div>
            </div>
          </div>
          <div className="mt-6 text-[12px] text-ink-muted">
            planted <span className="font-mono">{corpusReport.summary?.planted_columns || 0}</span> cells ·
            TP <span className="font-mono">{corpusReport.summary?.tp || 0}</span> ·
            FP <span className="font-mono">{corpusReport.summary?.fp || 0}</span> ·
            FN <span className="font-mono">{corpusReport.summary?.fn || 0}</span> ·
            TN <span className="font-mono">{corpusReport.summary?.tn || 0}</span>
          </div>
          <div className="mt-6 grid grid-cols-6 gap-2" data-testid="corpus-per-category">
            {(corpusReport.correctness?.per_category || []).map(pc => (
              <div key={pc.category} className="border border-rule px-3 py-2">
                <div className="font-mono text-[11px] text-ink-muted">cat {pc.category}</div>
                <div className="font-display text-[13px] text-ink mt-0.5">
                  {pc.tp}/{pc.tp + pc.fn} recalled
                </div>
                <div className="font-mono text-[10px] text-ink-muted">
                  fp {pc.fp} · tn {pc.tn}
                </div>
              </div>
            ))}
          </div>
          {(corpusReport.correctness?.false_negatives || []).length > 0 && (
            <div className="mt-6 border-l-2 border-oxblood pl-4">
              <div className="kicker text-oxblood">False negatives (PHI leaks)</div>
              <div className="mt-2 space-y-1">
                {corpusReport.correctness.false_negatives.slice(0, 10).map((m, i) => (
                  <div key={i} className="font-mono text-[12px] text-ink-2" data-testid={`corpus-fn-${i}`}>
                    {m.file} · {m.column} · cat {m.hipaa_category} · expected {m.expected_action} → got {m.actual_action}
                  </div>
                ))}
              </div>
            </div>
          )}
          {(corpusReport.correctness?.false_positives || []).length > 0 && (
            <div className="mt-4 border-l-2 border-signal pl-4">
              <div className="kicker text-signal">False positives (over-blocked)</div>
              <div className="mt-2 space-y-1">
                {corpusReport.correctness.false_positives.slice(0, 10).map((m, i) => (
                  <div key={i} className="font-mono text-[12px] text-ink-2" data-testid={`corpus-fp-${i}`}>
                    {m.file} · {m.column} · expected {m.expected_action} → got {m.actual_action}
                  </div>
                ))}
              </div>
            </div>
          )}
        </Panel>
      )}

      {/* Per-dataset benchmark report (only for corpus-mode sessions) */}
      {benchmarkReport && (
        <Panel title="Per-dataset benchmark"
               cite="Per-column method, why, how, confidence, and gold verdict; see the benchmark bundle for the full report"
               testId="benchmark-panel"
               right={
                 <Btn variant="ghost" size="sm" onClick={downloadBenchmark} disabled={busy} testId="benchmark-download">
                   Download benchmark ↓
                 </Btn>
               }>
          <div className="grid grid-cols-5 gap-6">
            <div>
              <div className="kicker">Leak rate</div>
              <div className="font-display text-display-md text-oxblood" data-testid="benchmark-leak-rate">
                {((benchmarkReport.totals?.leak_rate || 0) * 100).toFixed(2)}%
              </div>
            </div>
            <div>
              <div className="kicker">F1</div>
              <div className="font-display text-display-md text-oxblood" data-testid="benchmark-f1">
                {(benchmarkReport.totals?.f1 || 0).toFixed(4)}
              </div>
            </div>
            <div>
              <div className="kicker">Method-exact rate</div>
              <div className="font-display text-display-md text-ink" data-testid="benchmark-method-exact-rate">
                {((benchmarkReport.totals?.method_exact_rate || 0) * 100).toFixed(1)}%
              </div>
            </div>
            <div>
              <div className="kicker">Autonomy rate</div>
              <div className="font-display text-display-md text-ink" data-testid="benchmark-autonomy">
                {((benchmarkReport.totals?.autonomy_rate || 0) * 100).toFixed(1)}%
              </div>
            </div>
            <div>
              <div className="kicker">Identifiers removed before prompt</div>
              <div className="font-display text-display-md text-ink" data-testid="benchmark-scrub-count">
                {benchmarkReport.context_hygiene?.identifiers_removed_before_prompt ?? '—'}
              </div>
            </div>
          </div>
          <div className="mt-6 overflow-x-auto" data-testid="benchmark-columns-table">
            <table className="w-full text-[12px]">
              <thead>
                <tr className="text-ink-muted text-left border-b border-rule">
                  <th className="py-2 pr-3">Column</th>
                  <th className="py-2 pr-3">Gold category</th>
                  <th className="py-2 pr-3">Method</th>
                  <th className="py-2 pr-3">Why</th>
                  <th className="py-2 pr-3">Confidence</th>
                  <th className="py-2 pr-3">Decided by</th>
                  <th className="py-2 pr-3">Verdict</th>
                </tr>
              </thead>
              <tbody>
                {(benchmarkReport.columns || []).map((c, i) => (
                  <tr key={i} className="border-b border-rule/50" data-testid={`benchmark-column-${i}`}>
                    <td className="py-2 pr-3 font-mono">{c.file}:{c.column}</td>
                    <td className="py-2 pr-3">{c.gold_category}</td>
                    <td className="py-2 pr-3">{c.action_label}</td>
                    <td className="py-2 pr-3 text-ink-2 max-w-xs truncate" title={c.reason}>{c.reason}</td>
                    <td className="py-2 pr-3 font-mono">{c.confidence == null ? '—' : c.confidence.toFixed(2)}</td>
                    <td className="py-2 pr-3">{c.decided_by}</td>
                    <td className="py-2 pr-3">
                      <Tag color={c.verdict === 'correct' ? 'accept' : 'reject'}>{c.verdict}</Tag>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      )}
      {reviewNeeded && (
        <Panel title="Human review" cite="You are the reviewer of record; decisions carry your id + timestamp"
               testId="human-review-panel">
          {isPartiallyComplete && (
            <div className="mb-6 border-l-2 border-signal pl-4 py-2 bg-paper-2/50" data-testid="partially-complete-banner">
              <div className="text-[12px] text-ink-2">
                A partial bundle is ready above. <span className="font-mono">{humanRows.length}</span> column(s) below are
                still withheld from every export pending your decision — never defaulted, never blanked.
              </div>
            </div>
          )}
          <div className="grid grid-cols-2 gap-6 mb-6">
            <div>
              <div className="kicker">Reviewer identity <span className="text-oxblood">(required)</span></div>
              <input data-testid="reviewer-id" value={reviewer} onChange={e => setReviewer(e.target.value)}
                     placeholder="jane.doe@lab.edu"
                     className="mt-2 w-full h-10 bg-transparent border-b border-ink text-ink focus:border-oxblood"/>
            </div>
            <div>
              <div className="kicker">Comment</div>
              <input data-testid="reviewer-comment" value={reviewComment} onChange={e => setReviewComment(e.target.value)}
                     placeholder="general note for this submission"
                     className="mt-2 w-full h-10 bg-transparent border-b border-ink text-ink focus:border-oxblood"/>
            </div>
          </div>

          {/* Original file access: the system never opens or reads these on
              the reviewer's behalf -- only column headers ever reach a model. */}
          {datasetFiles.length > 0 && (
            <div className="rule-top pt-5 mb-6" data-testid="dataset-file-review-panel">
              <div className="kicker mb-3">Original dataset file(s) <span className="text-oxblood">(required)</span></div>
              <div className="text-[12px] text-ink-muted mb-3">
                Download the original file(s) and open them in your own tool to judge the flagged columns below.
              </div>
              <div className="space-y-2">
                {datasetFiles.map(f => (
                  <div key={f.file_id} className="flex items-center justify-between gap-4 data-cell" data-testid={`dataset-file-row-${f.file_id}`}>
                    <div className="font-mono text-[12px] text-ink">{f.original_name}</div>
                    <Btn size="sm" variant="ghost" onClick={() => downloadDatasetFile(f.file_id)} testId={`btn-download-dataset-file-${f.file_id}`}>
                      Download ↓
                    </Btn>
                  </div>
                ))}
              </div>
              <label className="mt-4 flex items-start gap-3 cursor-pointer" data-testid="file-review-ack-label">
                <input type="checkbox" checked={fileReviewAck}
                       onChange={e => setFileReviewAck(e.target.checked)}
                       data-testid="file-review-ack"
                       className="mt-[3px] h-4 w-4 accent-oxblood"/>
                <span className="text-[12px] text-ink-2 leading-5">
                  I have downloaded and reviewed the original file(s) above in my own tool.
                </span>
              </label>
            </div>
          )}

          {humanRows.length > 0 ? (() => {
            const setRowMode = (key, mode) => setResolutions(prev => ({
              ...prev, [key]: { ...(prev[key] || {}), mode, comment: mode === 'comment' ? (prev[key]?.comment || '') : '' },
            }));
            const setRowComment = (key, text) => setResolutions(prev => ({
              ...prev, [key]: { ...(prev[key] || {}), mode: 'comment', comment: text },
            }));
            const clearRow = (key) => setResolutions(prev => {
              const next = { ...prev }; delete next[key]; return next;
            });
            const activeRows = humanRows.filter(d => resolutions[`${d.file_id}|${d.column}`]?.mode !== 'defer');
            const setAsideRows = humanRows.filter(d => resolutions[`${d.file_id}|${d.column}`]?.mode === 'defer');
            const renderRow = (d) => {
              const key = `${d.file_id}|${d.column}`;
              const current = resolutions[key] || {};
              const pending = d.pending_confirmation;
              return (
                <div key={key} className="data-cell space-y-3" data-testid={`review-row-${d.column}`}>
                  <div>
                    <div className="font-mono text-[13px] text-ink">{d.column}</div>
                    <div className="text-[12px] text-ink-2 mt-1">{d.reviewer_prompt || d.reason || 'no rationale'}</div>
                    {d.needs_file_glance && (
                      <div className="text-[11px] text-oxblood mt-1">↑ open the original file above to judge this free-text column</div>
                    )}
                  </div>

                  {pending ? (
                    <div className="border-l-2 border-signal pl-3 py-2 bg-paper-2/50" data-testid={`review-row-confirm-${d.column}`}>
                      <div className="text-[12px] text-ink-2">
                        You said: <span className="italic">"{d.reviewer_comment}"</span> — I read that as:{' '}
                        <span className="font-mono text-oxblood">{pending.action || '(no clear action)'}</span>.
                        {pending.reason ? ` ${pending.reason}` : ''} Confirm?
                      </div>
                      <div className="mt-2 flex gap-2">
                        <Btn size="sm" variant={current.mode === 'approve' ? 'primary' : 'ghost'}
                             disabled={!pending.action}
                             onClick={() => setRowMode(key, 'approve')} testId={`btn-confirm-${d.column}`}>
                          Confirm
                        </Btn>
                        <Btn size="sm" variant={current.mode === 'comment' ? 'primary' : 'ghost'}
                             onClick={() => setRowMode(key, 'comment')} testId={`btn-recomment-${d.column}`}>
                          Re-comment
                        </Btn>
                        <Btn size="sm" variant="ghost" onClick={() => setRowMode(key, 'defer')} testId={`btn-defer-${d.column}`}>
                          Defer
                        </Btn>
                      </div>
                    </div>
                  ) : (
                    <div className="flex gap-2" data-testid={`review-row-buttons-${d.column}`}>
                      <Btn size="sm" variant={current.mode === 'approve' ? 'primary' : 'ghost'}
                           disabled={!d.suggested_action}
                           onClick={() => setRowMode(key, 'approve')} testId={`btn-approve-${d.column}`}
                           title={d.suggested_action ? `Apply: ${d.suggested_action}` : 'No suggested action available — use Comment'}>
                        Approve{d.suggested_action ? ` (${d.suggested_action})` : ''}
                      </Btn>
                      <Btn size="sm" variant={current.mode === 'comment' ? 'primary' : 'ghost'}
                           onClick={() => setRowMode(key, 'comment')} testId={`btn-comment-${d.column}`}>
                        Comment
                      </Btn>
                      <Btn size="sm" variant="ghost" onClick={() => setRowMode(key, 'defer')} testId={`btn-defer-${d.column}`}>
                        Defer
                      </Btn>
                    </div>
                  )}

                  {current.mode === 'comment' && (
                    <textarea
                      value={current.comment || ''}
                      onChange={e => setRowComment(key, e.target.value)}
                      placeholder="Tell the system what should happen to this column…"
                      className="w-full h-16 bg-transparent border border-rule text-ink px-2 py-1.5 text-[12px] focus:border-oxblood"
                      data-testid={`review-row-comment-${d.column}`}
                    />
                  )}
                </div>
              );
            };
            return (
              <div className="space-y-4">
                <div className="space-y-5">{activeRows.map(renderRow)}</div>
                {setAsideRows.length > 0 && (
                  <div className="rule-top pt-4" data-testid="set-aside-panel">
                    <div className="kicker text-ink-muted mb-2">Set aside for later ({setAsideRows.length})</div>
                    <div className="space-y-2">
                      {setAsideRows.map(d => {
                        const key = `${d.file_id}|${d.column}`;
                        return (
                          <div key={key} className="flex items-center justify-between gap-4 text-[12px] text-ink-muted" data-testid={`set-aside-row-${d.column}`}>
                            <span className="font-mono">{d.column}</span>
                            <button className="text-oxblood hover:underline" onClick={() => clearRow(key)} data-testid={`btn-unset-aside-${d.column}`}>
                              bring back
                            </button>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}
                <label className="mt-2 flex items-start gap-3 rule-top pt-4 cursor-pointer" data-testid="actual-knowledge-ack-label">
                  <input type="checkbox" checked={actualKnowledgeAck}
                         onChange={e => setActualKnowledgeAck(e.target.checked)}
                         data-testid="actual-knowledge-ack"
                         className="mt-[3px] h-4 w-4 accent-oxblood"/>
                  <span className="text-[12px] text-ink-2 leading-5">
                    <span className="font-mono text-oxblood">Required for approved/commented columns · 45 CFR 164.514(b)(2)(ii).</span>{' '}
                    I have no actual knowledge that the information I am resolving this round, alone or in combination
                    with other reasonably available information, could be used to identify an individual.
                  </span>
                </label>
                <div className="pt-4 flex justify-end">
                  <Btn variant="primary" onClick={submitReview}
                       disabled={busy || humanRows.some(d => !resolutions[`${d.file_id}|${d.column}`]?.mode)}
                       testId="btn-submit-human-review">
                    Submit ({humanRows.filter(d => resolutions[`${d.file_id}|${d.column}`]?.mode).length}/{humanRows.length}) →
                  </Btn>
                </div>
              </div>
            );
          })() : (
            <div className="space-y-4">
              <div className="text-[12px] text-ink-muted">
                Nothing is flagged for a specific column right now — resume the pipeline to continue.
              </div>
              <div className="flex justify-end">
                <Btn variant="primary" onClick={submitReview} disabled={busy || !reviewer.trim()} testId="btn-accept-globally">
                  Resume →
                </Btn>
              </div>
            </div>
          )}
        </Panel>
      )}

      {/* Pending state */}
      {isPending && (
        <div className="mt-16 rule-top pt-6" data-testid="pending-panel">
          <div className="kicker flex items-center gap-3"><Spinner/> Pipeline</div>
          <div className="mt-3 font-display text-display-sm text-ink">{status}</div>
          <div className="text-[12px] text-ink-muted mt-2">The download becomes available once every file clears the guard.</div>
        </div>
      )}

      {/* Dev logs */}
      <div className="mt-24 rule-top pt-6" data-testid="dev-toggle-panel">
        <button onClick={() => setDevOpen(o => !o)} className="kicker text-ink-2 hover:text-oxblood" data-testid="btn-toggle-dev">
          {devOpen ? '— hide agent details' : '+ show agent details'}
        </button>
        {devOpen && (
          <div className="mt-8 grid grid-cols-2 gap-10">
            <div>
              <div className="kicker mb-3">Agent decisions</div>
              <div className="space-y-2">
                {decisions.map((d, i) => (
                  <div key={i} className="data-cell flex justify-between gap-4 text-[12px]">
                    <span className="font-mono text-ink">{d.column}</span>
                    <span className="font-mono text-oxblood">{d.action}</span>
                    <span className="font-mono text-ink-muted">{typeof d.confidence === 'number' ? d.confidence.toFixed(2) : '—'}</span>
                  </div>
                ))}
                {decisions.length === 0 && <div className="text-[12px] text-ink-muted">No decisions yet.</div>}
              </div>
            </div>
            <div>
              <div className="kicker mb-3">Agent trace ({trace.length})</div>
              <div className="space-y-1 max-h-96 overflow-auto font-mono text-[11px]">
                {trace.map((m, i) => (
                  <div key={i} className="text-ink-2">
                    <span className="text-oxblood">{m.agent}</span> · {m.direction} · {m.phase} {m.error && <span className="text-oxblood">err</span>}
                  </div>
                ))}
                {trace.length === 0 && <div className="text-ink-muted">No trace yet.</div>}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
