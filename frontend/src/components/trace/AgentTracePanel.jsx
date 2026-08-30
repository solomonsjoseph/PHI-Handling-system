import React, { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { groupTrace } from './groupTrace';
import { agentMetaFor } from './agentMeta';
import TraceStateBadge from './TraceStateBadge';
import Spinner from '../common/Spinner';

// Sir Q "0% PHI in the trace" / the backend's own sanitize-then-hash-chain
// trace stack (docs Phase 2, `gateway.py`'s outbound-payload scrub):
// `prompt_text`/`reply_text` below are the SANITIZED text the gateway
// already scrubbed and hash-chained before this ever reaches the browser
// -- never a raw, unsanitized LLM payload. This component renders exactly
// those two already-safe fields, plus `status_text` (also sanitized
// server-side, see `TraceEvent.status_text` in docs) and `info` (agent-
// emitted structured metadata, never raw dataset content). It never
// renders anything else off a trace message.
export default function AgentTracePanel({ sid, trace, status, cancelRequested, advisory }) {
  const [openKeys, setOpenKeys] = useState(() => new Set());
  const grouped = groupTrace(trace);
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
            const meta = agentMetaFor(g.agent);
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
