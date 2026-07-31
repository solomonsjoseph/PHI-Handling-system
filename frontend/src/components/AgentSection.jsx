import React, { useEffect, useState } from 'react';
import axios from 'axios';
import { toast } from 'sonner';
import { API } from '../lib/api';
import { Btn, Panel, Tag } from './ui';

const ACTION_OPTIONS = [
  'keep', 'drop', 'cap_age_90', 'year_only', 'zip3_truncate', 'hash', 'pseudonymize', 'human_review',
];

export default function AgentSection({ sid, session, onRefresh }) {
  const [results, setResults] = useState(null);
  const [trace, setTrace] = useState([]);
  const [traceOpen, setTraceOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [resolutions, setResolutions] = useState({});

  const load = async () => {
    const [r, t] = await Promise.all([
      axios.get(`${API}/sessions/${sid}/results`).then(r => r.data).catch(() => null),
      axios.get(`${API}/sessions/${sid}/agent-trace`).then(r => r.data.messages).catch(() => []),
    ]);
    setResults(r);
    setTrace(t || []);
  };

  useEffect(() => {
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
    // `load` closes over `sid` + stable setState setters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sid]);

  const startHandle = async () => {
    setBusy(true);
    try {
      const r = await axios.post(`${API}/sessions/${sid}/handle`);
      toast(`Agent pipeline started with ${r.data.llm.model}`);
      onRefresh && onRefresh();
    } catch (e) {
      toast(`start failed: ${e?.response?.data?.detail || e.message}`);
    } finally { setBusy(false); }
  };

  const submitReview = async () => {
    const items = Object.entries(resolutions).map(([key, action]) => {
      const [file_id, ...rest] = key.split('|');
      return { file_id, column: rest.join('|'), action };
    });
    if (!items.length) { toast('Nothing to resolve'); return; }
    setBusy(true);
    try {
      const r = await axios.post(`${API}/sessions/${sid}/human-review`, { resolutions: items });
      toast(`Review submitted (${r.data.status})`);
      setResolutions({});
      await load();
      onRefresh && onRefresh();
    } catch (e) {
      toast(`review failed: ${e?.response?.data?.detail || e.message}`);
    } finally { setBusy(false); }
  };

  const decisions = results?.decisions || [];
  const humanNeeded = results?.human_review_required;
  const humanRows = decisions.filter(d => d.action === 'human_review');
  const canStart = session?.intake_status === 'ready' && !['classifying','anonymizing'].includes(session?.status) && session?.status !== 'complete';

  return (
    <>
      <Panel title="Agent Pipeline" cite="12-agent multi-step handling" testId="agent-panel"
        right={
          <div className="flex gap-2">
            <Btn variant="primary" onClick={startHandle} disabled={busy || !canStart} testId="btn-agent-handle">
              {session?.status === 'complete' ? 'Restart Pipeline' : 'Run Agent Pipeline'}
            </Btn>
          </div>
        }>
        <div className="grid grid-cols-6 gap-2 font-mono text-xs">
          <AgentTile name="Lexicon"    hint="dictionary" />
          <AgentTile name="Schema"     hint="headers" />
          <AgentTile name="Instrument" hint="forms" />
          <AgentTile name="Statute"    hint="regulations" />
          <AgentTile name="Praxis"     hint="methods" />
          <AgentTile name="Judge"      hint="classifier" />
          <AgentTile name="Sentinel"   hint="preview" />
          <AgentTile name="Executor"   hint="applier" />
          <AgentTile name="Auditor"    hint="verifier" />
          <AgentTile name="Scout"      hint="competitors" />
          <AgentTile name="Ledger"     hint="benchmark" />
          <AgentTile name="Herald"     hint="publishing" />
        </div>
      </Panel>

      {decisions.length > 0 && (
        <Panel title="Agent Decisions" cite={`${decisions.length} columns classified`} testId="agent-decisions-panel"
          right={humanNeeded ? <Tag color="phi" testId="human-review-flag">HUMAN REVIEW REQUIRED</Tag> : <Tag color="accept">approved</Tag>}
        >
          <table className="w-full text-xs font-mono border border-border">
            <thead className="bg-surface">
              <tr>
                <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Column</th>
                <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">PHI category</th>
                <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Action</th>
                <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Reason</th>
                <th className="text-left px-3 py-2 border-b border-border text-text-muted">Conf.</th>
              </tr>
            </thead>
            <tbody>
              {decisions.map((d, i) => {
                const key = `${d.file_id || ''}|${d.column || ''}`;
                const isHR = d.action === 'human_review';
                return (
                  <tr key={i} data-testid={`decision-row-${i}`}>
                    <td className="px-3 py-2 border-b border-r border-border text-text-primary">{d.column}</td>
                    <td className="px-3 py-2 border-b border-r border-border text-text-secondary">{d.phi_category || '-'}</td>
                    <td className="px-3 py-2 border-b border-r border-border">
                      {isHR ? (
                        <select
                          value={resolutions[key] || ''}
                          onChange={e => setResolutions({ ...resolutions, [key]: e.target.value })}
                          className="bg-surface border border-border px-2 h-6 text-[11px] text-text-primary"
                          data-testid={`resolve-${i}`}
                        >
                          <option value="">choose...</option>
                          {ACTION_OPTIONS.filter(a => a !== 'human_review').map(a => <option key={a}>{a}</option>)}
                        </select>
                      ) : (
                        <Tag color={d.action === 'keep' ? 'accept' : d.action === 'drop' ? 'reject' : 'phi'}>{d.action}</Tag>
                      )}
                    </td>
                    <td className="px-3 py-2 border-b border-r border-border text-text-secondary max-w-lg truncate" title={d.reason}>{d.reason}</td>
                    <td className="px-3 py-2 border-b border-border text-text-muted">{Number(d.confidence || 0).toFixed(2)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {humanNeeded && humanRows.length > 0 && (
            <div className="mt-3 flex gap-2">
              <Btn variant="primary" onClick={submitReview} disabled={busy || Object.keys(resolutions).length === 0} testId="btn-submit-human-review">
                Submit review ({Object.keys(resolutions).length}/{humanRows.length})
              </Btn>
            </div>
          )}
          {results?.audit && (
            <div className="mt-3 border border-border p-3 font-mono text-xs" data-testid="audit-summary">
              <div className="uppercase text-[10px] tracking-widest text-text-muted mb-1">Auditor</div>
              <div className="text-text-primary">verdict: <span className={results.audit.verdict === 'clean' ? 'text-accept' : 'text-reject'}>{results.audit.verdict}</span></div>
              <div className="text-text-secondary mt-1">{results.audit.summary}</div>
            </div>
          )}
        </Panel>
      )}

      {results?.ledger && (
        <Panel title="Benchmark (Ledger)" cite={results.ledger.headline || '-'} testId="ledger-panel">
          <div className="font-mono text-xs whitespace-pre-wrap text-text-secondary">{results.ledger.metrics_narrative}</div>
          {(results.ledger.comparisons || []).length > 0 && (
            <table className="mt-3 w-full text-xs font-mono border border-border">
              <thead className="bg-surface"><tr>
                <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Competitor</th>
                <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Reads rows</th>
                <th className="text-left px-3 py-2 border-b border-border text-text-muted">Delta notes</th>
              </tr></thead>
              <tbody>
                {results.ledger.comparisons.map((c, i) => (
                  <tr key={i}>
                    <td className="px-3 py-2 border-b border-r border-border text-text-primary">{c.competitor}</td>
                    <td className="px-3 py-2 border-b border-r border-border">{c.reads_row_values ? <Tag color="reject">yes</Tag> : <Tag color="accept">no</Tag>}</td>
                    <td className="px-3 py-2 border-b border-border text-text-secondary">{c.delta_notes}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
      )}

      {results?.herald && results.herald.title && (
        <Panel title="Manuscript Draft (Herald)" cite={results.herald.target_venue} testId="herald-panel">
          <div className="font-display text-lg mb-2">{results.herald.title}</div>
          <div className="font-mono text-xs text-text-secondary italic mb-3">{results.herald.abstract}</div>
          {(results.herald.sections || []).map((s, i) => (
            <div key={i} className="mb-3">
              <div className="font-mono text-[10px] uppercase tracking-widest text-phi">{s.heading}</div>
              <div className="font-mono text-xs text-text-primary whitespace-pre-wrap">{s.body}</div>
            </div>
          ))}
        </Panel>
      )}

      <Panel title={`Agent Trace (${trace.length})`} testId="agent-trace-panel"
        right={<button className="font-mono text-[10px] uppercase tracking-widest text-phi" onClick={() => setTraceOpen(o => !o)} data-testid="btn-toggle-trace">{traceOpen ? 'hide' : 'show'}</button>}
      >
        {traceOpen && (
          <div className="max-h-96 overflow-auto border border-border">
            <table className="w-full text-xs font-mono">
              <tbody>
                {trace.map((m, i) => (
                  <tr key={i} className="border-b border-border" data-testid={`trace-${i}`}>
                    <td className="px-3 py-1.5 border-r border-border text-text-muted whitespace-nowrap">{(m.ts || '').slice(11, 19)}</td>
                    <td className="px-3 py-1.5 border-r border-border text-phi whitespace-nowrap">{m.agent}</td>
                    <td className="px-3 py-1.5 border-r border-border text-text-muted">{m.direction}</td>
                    <td className="px-3 py-1.5 text-text-primary max-w-2xl truncate">{m.phase} {m.duration_ms ? `(${Math.round(m.duration_ms)}ms)` : ''}</td>
                  </tr>
                ))}
                {trace.length === 0 && <tr><td className="px-3 py-2 text-text-muted">no agent messages yet</td></tr>}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </>
  );
}

function AgentTile({ name, hint }) {
  return (
    <div className="border border-border p-2" data-testid={`agent-tile-${name}`}>
      <div className="text-phi font-mono">{name}</div>
      <div className="text-text-muted text-[10px] uppercase tracking-widest">{hint}</div>
    </div>
  );
}
