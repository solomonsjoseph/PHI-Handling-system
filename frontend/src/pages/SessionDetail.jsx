import React, { useEffect, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import { toast } from 'sonner';
import axios from 'axios';
import { API, exportUrl, getSession, streamUrl } from '../lib/api';
import { Btn, Panel, Tag } from '../components/ui';
import AgentPhaseStepper from '../components/AgentPhaseStepper';

const ACTION_OPTIONS = ['keep','drop','cap_age_90','year_only','zip3_truncate','hash','pseudonymize','scrub_text'];
const ACTION_COLOR = {
  keep: 'accept', drop: 'reject', cap_age_90: 'phi', year_only: 'phi',
  zip3_truncate: 'phi', hash: 'phi', pseudonymize: 'phi', scrub_text: 'phi',
  human_review: 'phi',
};

export default function SessionDetail() {
  const { sid } = useParams();
  const [session, setSession] = useState(null);
  const [results, setResults] = useState(null);
  const [trace, setTrace] = useState([]);
  const [events, setEvents] = useState([]);
  const [resolutions, setResolutions] = useState({});
  const [devOpen, setDevOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const esRef = useRef(null);

  const [notFound, setNotFound] = useState(false);
  const refresh = async () => {
    const [s, r, t] = await Promise.all([
      getSession(sid).catch(() => null),
      axios.get(`${API}/sessions/${sid}/results`).then(r => r.data).catch(() => null),
      axios.get(`${API}/sessions/${sid}/agent-trace?limit=500`).then(r => r.data.messages).catch(() => []),
    ]);
    if (!s) { setNotFound(true); return; }
    setNotFound(false);
    setSession(s);
    setResults(r);
    setTrace(t || []);
    setEvents(s.progress || []);
  };

  useEffect(() => { refresh(); }, [sid]);

  useEffect(() => {
    if (!session) return;
    const active = ['created','intake','reading','classifying','applying_review','anonymizing'];
    if (!active.includes(session.status)) return;
    if (esRef.current) esRef.current.close();
    const es = new EventSource(streamUrl(sid));
    esRef.current = es;
    es.onmessage = (m) => {
      try {
        const ev = JSON.parse(m.data);
        if (ev.phase === '__end__') { es.close(); refresh(); return; }
        setEvents(prev => [...prev, ev]);
        if (['awaiting_review','awaiting_human_review','complete','failed'].includes(ev.phase)) refresh();
      } catch (_) {}
    };
    es.onerror = () => es.close();
    return () => es.close();
  }, [session?.status, sid]);

  useEffect(() => {
    if (!session) return;
    const active = ['created','intake','reading','classifying','applying_review','anonymizing'];
    if (!active.includes(session.status)) return;
    const t = setInterval(refresh, 3000);
    return () => clearInterval(t);
  }, [session?.status, sid]);

  const startHandle = async () => {
    setBusy(true);
    try {
      const r = await axios.post(`${API}/sessions/${sid}/handle`);
      toast(`Agent pipeline started with ${r.data.llm.provider} ${r.data.llm.model}`);
      await refresh();
    } catch (e) {
      toast(`start failed: ${e?.response?.data?.detail || e.message}`);
    } finally { setBusy(false); }
  };

  const [reviewer, setReviewer] = useState(() => {
    try { return window.localStorage.getItem('phi_reviewer_id') || ''; } catch (_) { return ''; }
  });
  const [reviewComment, setReviewComment] = useState('');

  const submitReview = async () => {
    const items = Object.entries(resolutions).map(([key, action]) => {
      const [file_id, ...rest] = key.split('|');
      return { file_id, column: rest.join('|'), action };
    });
    if (!items.length) { toast('Choose an action for each row first'); return; }
    if (!reviewer.trim()) { toast('Reviewer id is required'); return; }
    try { window.localStorage.setItem('phi_reviewer_id', reviewer.trim()); } catch (_) {}
    setBusy(true);
    try {
      const r = await axios.post(`${API}/sessions/${sid}/human-review`, {
        resolutions: items, reviewer: reviewer.trim(), comment: reviewComment,
      });
      toast(`Review submitted (${r.data.status})`);
      setResolutions({});
      setReviewComment('');
      await refresh();
    } catch (e) {
      toast(`review failed: ${e?.response?.data?.detail || e.message}`);
    } finally { setBusy(false); }
  };

  if (notFound) return (
    <div className="p-6 font-mono text-xs" data-testid="session-not-found">
      <div className="text-reject uppercase tracking-widest text-[10px] mb-2">Study not found</div>
      <div className="text-text-secondary">No study exists with id <span className="text-text-primary">{sid}</span>.</div>
      <div className="mt-3"><a href="/studies" className="text-phi underline">Back to studies</a></div>
    </div>
  );
  if (!session) return <div className="p-4 font-mono text-xs text-text-muted">loading...</div>;

  const decisions = results?.decisions || [];
  const humanNeeded = results?.human_review_required;
  const humanRows = decisions.filter(d => d.action === 'human_review');
  const exports = session.export_paths || {};
  const intakeReady = session.intake_status === 'ready';
  const canStart = intakeReady && !['classifying','reading','anonymizing','applying_review'].includes(session.status);
  const isComplete = session.status === 'complete';
  const isFailed = session.status === 'failed';

  return (
    <div className="min-h-full">
      {/* Hero: study identity + one CTA. */}
      <section className="border-b border-border bg-bg p-6" data-testid="study-hero">
        <div className="flex items-baseline gap-4">
          <div className="font-mono text-[10px] uppercase tracking-widest text-text-muted">Study</div>
          <div className="font-mono text-xs text-text-primary" data-testid="study-id">{sid}</div>
          <Tag color={isComplete ? 'accept' : isFailed ? 'reject' : 'default'} testId="study-status">{session.status}</Tag>
          <Tag color={intakeReady ? 'accept' : 'reject'} testId="intake-status">
            intake: {session.intake_status || 'none'} (exit {session.intake_exit_code ?? '-'})
          </Tag>
          <div className="ml-auto flex gap-3">
            {canStart && (
              <Btn variant="primary" onClick={startHandle} disabled={busy} testId="btn-agent-handle">
                {isComplete ? 'Restart pipeline' : 'Run PHI handling'}
              </Btn>
            )}
            <button onClick={() => setDevOpen(o => !o)} className="h-9 px-4 border border-border font-mono text-[10px] uppercase tracking-widest text-text-muted hover:text-phi hover:border-phi" data-testid="btn-toggle-dev">
              {devOpen ? 'Hide details' : 'Developer details'}
            </button>
          </div>
        </div>
        {session.error && (
          <div className="mt-4 border border-reject text-reject px-3 py-2 font-mono text-xs" data-testid="hero-error">
            {session.error}
          </div>
        )}
      </section>

      {/* Phase stepper */}
      <div className="p-4">
        <AgentPhaseStepper session={session} trace={trace} />
      </div>

      {/* Decisions */}
      {decisions.length > 0 && (
        <Panel title="Agent Decisions" cite={`${decisions.length} columns classified across ${(session.files || []).filter(f => f.kind === 'dataset').length} datasets`} testId="decisions-panel"
          right={humanNeeded ? <Tag color="phi" testId="human-review-flag">HUMAN REVIEW REQUIRED</Tag> : isComplete ? <Tag color="accept">complete</Tag> : <Tag>in progress</Tag>}
        >
          <table className="w-full text-xs font-mono border border-border">
            <thead className="bg-surface">
              <tr>
                <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Column</th>
                <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Subject</th>
                <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">HIPAA</th>
                <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Action</th>
                <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Reason</th>
                <th className="text-left px-3 py-2 border-b border-border text-text-muted">Conf</th>
              </tr>
            </thead>
            <tbody>
              {decisions.map((d, i) => {
                const key = `${d.file_id || ''}|${d.column || ''}`;
                const isHR = d.action === 'human_review';
                return (
                  <tr key={i} data-testid={`decision-row-${i}`}>
                    <td className="px-3 py-2 border-b border-r border-border text-text-primary">{d.column}</td>
                    <td className="px-3 py-2 border-b border-r border-border text-text-secondary">{d.subject || '-'}</td>
                    <td className="px-3 py-2 border-b border-r border-border text-phi">{d.phi_category || '-'}</td>
                    <td className="px-3 py-2 border-b border-r border-border">
                      {isHR && !isComplete ? (
                        <select
                          value={resolutions[key] || ''}
                          onChange={e => setResolutions({ ...resolutions, [key]: e.target.value })}
                          className="bg-surface border border-border px-2 h-6 text-[11px] text-text-primary"
                          data-testid={`resolve-${i}`}
                        >
                          <option value="">choose...</option>
                          {ACTION_OPTIONS.map(a => <option key={a}>{a}</option>)}
                        </select>
                      ) : (
                        <Tag color={ACTION_COLOR[d.action] || 'default'} testId={`action-${i}`}>{d.action}</Tag>
                      )}
                    </td>
                    <td className="px-3 py-2 border-b border-r border-border text-text-secondary max-w-lg" title={d.reason}>
                      <div className="truncate">{d.reason}</div>
                    </td>
                    <td className="px-3 py-2 border-b border-border text-text-muted">{Number(d.confidence || 0).toFixed(2)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {humanNeeded && humanRows.length > 0 && !isComplete && (
            <div className="mt-3 space-y-2" data-testid="human-review-bar">
              <div className="grid grid-cols-2 gap-2">
                <input
                  data-testid="reviewer-id"
                  placeholder="Reviewer id (required)"
                  value={reviewer}
                  onChange={e => setReviewer(e.target.value)}
                  className="h-8 bg-surface border border-border px-2 font-mono text-xs text-text-primary"
                />
                <input
                  data-testid="reviewer-comment"
                  placeholder="Comment (optional)"
                  value={reviewComment}
                  onChange={e => setReviewComment(e.target.value)}
                  className="h-8 bg-surface border border-border px-2 font-mono text-xs text-text-primary"
                />
              </div>
              <div className="flex items-center gap-3">
                <div className="font-mono text-[10px] text-text-muted uppercase tracking-widest">Human decision required on {humanRows.length} column(s)</div>
                <Btn variant="primary" onClick={submitReview} disabled={busy || Object.keys(resolutions).length === 0 || !reviewer.trim()} testId="btn-submit-human-review">
                  Submit ({Object.keys(resolutions).length}/{humanRows.length})
                </Btn>
              </div>
            </div>
          )}
        </Panel>
      )}

      {/* Exports */}
      {/* Publish Guard: last-mile deterministic PHI scan on emitted exports */}
      {isComplete && results?.guard && (
        <Panel
          title="Publish Guard"
          cite="Deterministic residual-PHI scan on emitted exports — GOAL boundary"
          testId="publish-guard-panel"
          right={
            <Tag
              color={results.guard.status === 'clean' ? 'accept' : 'reject'}
              testId="publish-guard-status"
            >
              {results.guard.status === 'clean' ? 'PHI-HANDLED ✓ SAFE TO SHARE' : 'BLOCKED'}
            </Tag>
          }
        >
          <div className="font-mono text-xs text-text-primary">
            Scanned {results.guard.scanned} file(s); blocked {results.guard.blocked}.
          </div>
          {results.guard.blocked > 0 && (
            <div className="mt-3 space-y-2">
              {(results.guard.results || []).filter(r => r.status === 'blocked').map((r, i) => (
                <div key={i} className="border border-reject p-2 font-mono text-xs" data-testid={`guard-block-${i}`}>
                  <div className="text-reject uppercase text-[10px] tracking-widest mb-1">
                    {r.file_path.split('/').pop()} — {(r.findings || []).length} finding(s)
                  </div>
                  {(r.findings || []).slice(0, 5).map((f, j) => (
                    <div key={j} className="text-text-secondary">
                      L{f.line} • {f.pattern_id} • cat {f.hipaa_category} • sample {f.sample}
                    </div>
                  ))}
                  {(r.findings || []).length > 5 && (
                    <div className="text-text-muted">…{(r.findings || []).length - 5} more</div>
                  )}
                </div>
              ))}
              <div className="text-[10px] text-text-muted">
                Downloads are blocked until the pipeline handles these columns. Add a rule, tighten a decision, and re-run.
              </div>
            </div>
          )}
        </Panel>
      )}

      {isComplete && Object.keys(exports).length > 0 && (
        <Panel title="Exports Ready" cite="PHI-handled outputs safe to share with any AI" testId="exports-panel"
          right={<Tag color="accept" testId="exports-count">{Object.keys(exports).length} files</Tag>}
        >
          <div className="grid grid-cols-3 gap-3">
            {(session.files || []).filter(f => exports[f.file_id]).map(f => {
              const guardResult = (results?.guard?.results || []).find(r => r.file_id === f.file_id);
              const blocked = guardResult && guardResult.status === 'blocked';
              const cls = blocked
                ? 'border border-reject p-3 opacity-60 cursor-not-allowed block'
                : 'border border-accept p-3 hover:bg-accept hover:text-white transition-colors block';
              const inner = (
                <>
                  <div className="font-mono text-[10px] uppercase tracking-widest text-text-muted mb-1">{f.component || f.kind}</div>
                  <div className="font-mono text-xs text-text-primary break-all">{f.original_name}</div>
                  <div className="font-mono text-[10px] mt-1 opacity-70">
                    {blocked ? 'blocked by publish guard' : 'download ↓'}
                  </div>
                </>
              );
              return blocked ? (
                <div key={f.file_id} className={cls} data-testid={`export-blocked-${f.file_id}`} title="Publish Guard blocked this file">
                  {inner}
                </div>
              ) : (
                <a
                  key={f.file_id}
                  href={exportUrl(sid, f.file_id)}
                  className={cls}
                  data-testid={`export-card-${f.file_id}`}
                >
                  {inner}
                </a>
              );
            })}
          </div>
        </Panel>
      )}

      {/* Auditor summary */}
      {results?.audit && (
        <Panel title="Auditor Report" cite="0% PHI leak enforcement" testId="audit-panel"
          right={<Tag color={results.audit.verdict === 'clean' ? 'accept' : 'reject'} testId="audit-verdict">{results.audit.verdict}</Tag>}
        >
          <div className="font-mono text-xs text-text-primary whitespace-pre-wrap">{results.audit.summary}</div>
          {results.audit.metrics && (
            <div className="mt-3 grid grid-cols-5 gap-2 font-mono text-xs">
              {Object.entries(results.audit.metrics).map(([k, v]) => (
                <div key={k} className="border border-border p-2">
                  <div className="text-text-muted uppercase text-[10px] tracking-widest">{k.replace(/_/g, ' ')}</div>
                  <div className="text-lg text-text-primary">{typeof v === 'number' ? v : String(v)}</div>
                </div>
              ))}
            </div>
          )}
        </Panel>
      )}

      {/* Ledger */}
      {results?.ledger && results.ledger.headline && (
        <Panel title="Benchmark - Comparative Analysis (Ledger)" cite="headers-only advantage vs competitors" testId="ledger-panel">
          <div className="font-mono text-sm text-text-primary mb-2">{results.ledger.headline}</div>
          <div className="font-mono text-xs text-text-secondary whitespace-pre-wrap mb-3">{results.ledger.metrics_narrative}</div>
          {(results.ledger.comparisons || []).length > 0 && (
            <table className="w-full text-xs font-mono border border-border">
              <thead className="bg-surface">
                <tr>
                  <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Competitor</th>
                  <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Reads rows?</th>
                  <th className="text-left px-3 py-2 border-b border-border text-text-muted">Delta</th>
                </tr>
              </thead>
              <tbody>
                {results.ledger.comparisons.map((c, i) => (
                  <tr key={i} data-testid={`ledger-row-${i}`}>
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

      {/* Herald manuscript */}
      {results?.herald && results.herald.title && (
        <Panel title="Manuscript Draft (Herald)" cite={`target venue: ${results.herald.target_venue}`} testId="herald-panel">
          <div className="max-w-3xl">
            <div className="font-display text-2xl text-text-primary leading-tight mb-1" data-testid="herald-title">{results.herald.title}</div>
            <div className="font-mono text-[11px] text-text-muted uppercase tracking-widest mb-4">Abstract</div>
            <div className="font-mono text-xs text-text-secondary italic mb-6 leading-relaxed" data-testid="herald-abstract">{results.herald.abstract}</div>
            {(results.herald.sections || []).map((s, i) => (
              <div key={i} className="mb-5" data-testid={`herald-section-${i}`}>
                <div className="font-mono text-[10px] uppercase tracking-widest text-phi mb-1">{s.heading}</div>
                <div className="font-mono text-xs text-text-primary whitespace-pre-wrap leading-relaxed">{s.body}</div>
              </div>
            ))}
            {(results.herald.alt_venues || []).length > 0 && (
              <div className="mt-6 border-t border-border pt-4">
                <div className="font-mono text-[10px] uppercase tracking-widest text-text-muted mb-2">Alternative venues suggested by Herald</div>
                {results.herald.alt_venues.map((v, i) => (
                  <div key={i} className="font-mono text-[11px] text-text-secondary">
                    <span className="text-text-primary">{v.venue}</span> - {v.rationale}
                  </div>
                ))}
              </div>
            )}
          </div>
        </Panel>
      )}

      {/* Developer details (collapsed by default) */}
      {devOpen && (
        <>
          <Panel title="Intake Receipt" cite="manifest v3" testId="intake-panel">
            {(session.intake_missing || []).length > 0 && (
              <div className="mb-3 border border-reject text-reject px-3 py-2 font-mono text-xs">
                Missing: {session.intake_missing.join(', ')}
              </div>
            )}
            {(session.intake_review || []).length > 0 && (
              <div className="mb-3 border border-phi-border px-3 py-2 font-mono text-xs text-phi">
                <div className="uppercase text-[10px] tracking-widest mb-1">Unclassified ({session.intake_review.length})</div>
                {session.intake_review.slice(0, 20).map((e, i) => (
                  <div key={i} className="text-[11px]"><span className="text-text-primary">{e.relpath}</span> - <span className="text-text-secondary">{e.reason}</span></div>
                ))}
              </div>
            )}
            <table className="w-full text-xs font-mono border border-border">
              <thead className="bg-surface"><tr>
                <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Name</th>
                <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Component</th>
                <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Kind</th>
                <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Size</th>
                <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">SHA</th>
                <th className="text-left px-3 py-2 border-b border-border text-text-muted">Columns</th>
              </tr></thead>
              <tbody>
                {(session.files || []).map(f => (
                  <tr key={f.file_id} data-testid={`file-row-${f.file_id}`}>
                    <td className="px-3 py-2 border-b border-r border-border text-text-primary">{f.original_name}</td>
                    <td className="px-3 py-2 border-b border-r border-border"><Tag color={f.component === 'datasets' ? 'phi' : 'default'}>{f.component}</Tag></td>
                    <td className="px-3 py-2 border-b border-r border-border text-text-secondary">{f.kind} / {f.subtype}</td>
                    <td className="px-3 py-2 border-b border-r border-border text-text-muted">{f.size_bytes} B</td>
                    <td className="px-3 py-2 border-b border-r border-border text-text-muted">{(f.sha256 || '').slice(0, 12)}</td>
                    <td className="px-3 py-2 border-b border-border text-text-secondary max-w-md truncate" title={(f.columns || []).join(', ')}>
                      {f.kind === 'dataset' ? (f.columns || []).slice(0, 6).join(', ') + ((f.columns || []).length > 6 ? ` +${f.columns.length - 6}` : '') : '-'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Panel>

          <Panel title={`Agent Trace (${trace.length} messages)`} testId="agent-trace-panel">
            <div className="max-h-96 overflow-auto border border-border">
              <table className="w-full text-xs font-mono">
                <tbody>
                  {trace.map((m, i) => (
                    <tr key={i} className="border-b border-border" data-testid={`trace-${i}`}>
                      <td className="px-3 py-1.5 border-r border-border text-text-muted whitespace-nowrap">{(m.ts || '').slice(11, 19)}</td>
                      <td className="px-3 py-1.5 border-r border-border text-phi whitespace-nowrap">{m.agent}</td>
                      <td className="px-3 py-1.5 border-r border-border text-text-muted uppercase text-[10px]">{m.direction}</td>
                      <td className="px-3 py-1.5 text-text-primary">{m.phase} {m.duration_ms ? <span className="text-text-muted">({Math.round(m.duration_ms)}ms)</span> : null}</td>
                    </tr>
                  ))}
                  {trace.length === 0 && <tr><td className="px-3 py-2 text-text-muted">no agent messages yet</td></tr>}
                </tbody>
              </table>
            </div>
          </Panel>

          <Panel title={`Progress Log (${events.length} events)`} testId="progress-panel">
            <div className="max-h-64 overflow-auto border border-border">
              <table className="w-full text-xs font-mono">
                <tbody>
                  {events.slice(-40).reverse().map((e, i) => (
                    <tr key={i} className={`border-b border-border ${i % 2 === 0 ? '' : 'bg-white/[0.02]'}`}>
                      <td className="px-3 py-1.5 border-r border-border text-text-muted whitespace-nowrap">{(e.ts || '').slice(11, 19)}</td>
                      <td className="px-3 py-1.5 border-r border-border text-phi uppercase whitespace-nowrap text-[10px]">{e.phase}</td>
                      <td className="px-3 py-1.5 text-text-primary">{e.message}</td>
                    </tr>
                  ))}
                  {events.length === 0 && <tr><td className="px-3 py-2 text-text-muted">no events yet</td></tr>}
                </tbody>
              </table>
            </div>
          </Panel>
        </>
      )}
    </div>
  );
}
