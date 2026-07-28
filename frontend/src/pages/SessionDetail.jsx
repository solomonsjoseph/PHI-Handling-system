import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useParams, useSearchParams, useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import axios from 'axios';
import { API, getSession, streamUrl, getApiToken } from '../lib/api';
import { Btn, Panel, Tag } from '../components/ui';

const ACTION_OPTIONS = ['keep','drop','cap_age_90','year_only','zip3_truncate','hash','pseudonymize','scrub_text'];

function StatusChip({ status }) {
  const map = {
    complete: 'accept',
    awaiting_human_review: 'signal',
    classifying: 'signal',
    anonymizing: 'signal',
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

export default function SessionDetail() {
  const { sid } = useParams();
  const navigate = useNavigate();
  const [sp] = useSearchParams();
  const wantPub = sp.get('bundle') === 'publication';
  const wantPdf = sp.get('pdf') === '1';

  const [session, setSession] = useState(null);
  const [results, setResults] = useState(null);
  const [trace, setTrace] = useState([]);
  const [resolutions, setResolutions] = useState({});
  const [reviewer, setReviewer] = useState(() => {
    try { return window.localStorage.getItem('phi_reviewer_id') || ''; } catch (_) { return ''; }
  });
  const [reviewComment, setReviewComment] = useState('');
  const [devOpen, setDevOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notFound, setNotFound] = useState(false);
  const esRef = useRef(null);

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
  };

  useEffect(() => {
    refresh();
    const es = new EventSource(streamUrl(sid));
    es.onmessage = () => refresh();
    es.onerror = () => es.close();
    esRef.current = es;
    return () => es.close();
  }, [sid]);

  const status = session?.status;
  const isComplete = status === 'complete';
  const isPending = !isComplete && status !== 'awaiting_human_review' && status !== 'failed';
  const awaiting = status === 'awaiting_human_review';
  const guard = results?.guard || session?.guard_report;
  const decisions = results?.decisions || [];
  const humanRows = decisions.filter(d => d.action === 'human_review');

  const downloadBundle = async () => {
    setBusy(true);
    try {
      const q = new URLSearchParams();
      if (wantPub) q.set('publication', '1');
      if (wantPdf) q.set('attestation_pdf', '1');
      const t = getApiToken(); if (t) q.set('token', t);
      const url = `${API}/sessions/${sid}/bundle?${q.toString()}`;
      // Trigger a download via a temporary anchor
      const a = document.createElement('a');
      a.href = url; a.download = ''; document.body.appendChild(a); a.click(); a.remove();
    } finally { setBusy(false); }
  };

  const submitReview = async () => {
    if (!reviewer.trim()) { toast.error('Reviewer id is required'); return; }
    const items = Object.entries(resolutions).map(([key, action]) => {
      const [file_id, ...rest] = key.split('|');
      return { file_id, column: rest.join('|'), action };
    });
    try { window.localStorage.setItem('phi_reviewer_id', reviewer.trim()); } catch (_) {}
    setBusy(true);
    try {
      const r = await axios.post(`${API}/sessions/${sid}/human-review`, {
        resolutions: items, reviewer: reviewer.trim(), comment: reviewComment,
      });
      toast.success(`Review submitted (${r.data.status})`);
      setResolutions({}); setReviewComment('');
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
            {isComplete ? 'Handled.' : awaiting ? 'Awaiting your review.' : isPending ? 'Working on it.' : 'Something went wrong.'}
          </h1>
          <StatusChip status={status} />
        </div>
        <div className="mt-3 text-[13px] text-ink-muted font-mono">session {sid}</div>

        {isComplete && (
          <div className="mt-10 flex items-center gap-4">
            <Btn variant="primary" size="lg" onClick={downloadBundle} disabled={busy || guard?.status === 'blocked'} testId="btn-download-bundle">
              {guard?.status === 'blocked' ? 'Bundle blocked' : `Download ${wantPub ? 'publication bundle' : 'safe-to-share bundle'} ↓`}
            </Btn>
            <Btn variant="ghost" onClick={() => navigate('/')} testId="btn-new-run">Start another run</Btn>
          </div>
        )}
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

      {/* Human review */}
      {awaiting && (
        <Panel title="Human review" cite="You are the reviewer of record; decisions carry your id + timestamp"
               testId="human-review-panel">
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
                     placeholder="acceptance rationale"
                     className="mt-2 w-full h-10 bg-transparent border-b border-ink text-ink focus:border-oxblood"/>
            </div>
          </div>
          {humanRows.length > 0 ? (
            <div className="space-y-4">
              {humanRows.map(d => {
                const key = `${d.file_id}|${d.column}`;
                return (
                  <div key={key} className="data-cell flex items-center gap-6" data-testid={`review-row-${d.column}`}>
                    <div className="w-64 font-mono text-[13px] text-ink">{d.column}</div>
                    <div className="text-[12px] text-ink-muted flex-1">{d.reason || 'no rationale'}</div>
                    <select value={resolutions[key] || ''} onChange={e => setResolutions({ ...resolutions, [key]: e.target.value })}
                            className="h-9 bg-transparent border border-rule text-ink px-2 text-[12px] font-mono focus:border-oxblood">
                      <option value="">choose action…</option>
                      {ACTION_OPTIONS.map(a => <option key={a} value={a}>{a}</option>)}
                    </select>
                  </div>
                );
              })}
              <div className="pt-4 flex justify-end">
                <Btn variant="primary" onClick={submitReview}
                     disabled={busy || Object.keys(resolutions).length === 0 || !reviewer.trim()}
                     testId="btn-submit-human-review">
                  Submit ({Object.keys(resolutions).length}/{humanRows.length}) →
                </Btn>
              </div>
            </div>
          ) : (
            <div className="flex items-center justify-between">
              <div className="text-[12px] text-ink-muted">
                Sentinel flagged this session globally. No per-column overrides required.
              </div>
              <Btn variant="primary" onClick={submitReview} disabled={busy || !reviewer.trim()} testId="btn-accept-globally">
                Accept Judge decisions →
              </Btn>
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
