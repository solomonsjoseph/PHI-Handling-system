import React from 'react';

// Operator-facing developer detail: per-column decisions and the raw
// trace event stream (agent/direction/phase/error only -- never
// prompt/reply text, which stays inside AgentTracePanel's own gated,
// already-sanitized rows).
export default function DevLogsPanel({ devOpen, setDevOpen, decisions, trace }) {
  return (
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
  );
}
