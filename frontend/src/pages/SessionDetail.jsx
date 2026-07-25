import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import { toast } from 'sonner';
import { API, exportUrl, finalizeSession, getSession, streamUrl, submitReview } from '../lib/api';
import { Btn, MonoProgress, Panel, Tag } from '../components/ui';

const STATUS_ORDER = ['created','reading','classifying','detecting','awaiting_review','applying_review','anonymizing','complete'];

function statusPercent(status, events) {
  if (status === 'complete') return 100;
  if (status === 'failed') return 100;
  const idx = STATUS_ORDER.indexOf(status);
  if (idx < 0) return 0;
  const base = (idx / (STATUS_ORDER.length - 1)) * 100;
  const last = events[events.length - 1];
  const local = last && last.percent ? last.percent : 0;
  return Math.min(100, Math.round(base + (local / (STATUS_ORDER.length - 1))));
}

export default function SessionDetail() {
  const { sid } = useParams();
  const [session, setSession] = useState(null);
  const [events, setEvents] = useState([]);
  const [reviewMap, setReviewMap] = useState({});
  const esRef = useRef(null);

  const refresh = async () => {
    const s = await getSession(sid);
    setSession(s);
    setEvents(s.progress || []);
  };

  useEffect(() => { refresh(); }, [sid]);

  useEffect(() => {
    // Open SSE stream whenever the session is in an active phase
    if (!session) return;
    const activePhases = ['created','reading','classifying','detecting','applying_review','anonymizing'];
    if (!activePhases.includes(session.status)) return;
    if (esRef.current) esRef.current.close();
    const es = new EventSource(streamUrl(sid));
    esRef.current = es;
    es.onmessage = (m) => {
      try {
        const ev = JSON.parse(m.data);
        if (ev.phase === '__end__') {
          es.close();
          refresh();
          return;
        }
        setEvents(prev => [...prev, ev]);
        if (['awaiting_review','complete','failed'].includes(ev.phase)) refresh();
      } catch (_) {}
    };
    es.onerror = () => { es.close(); };
    return () => es.close();
  }, [session?.status, sid]);

  const setDecision = (span_id, action, extra = {}) => {
    setReviewMap(prev => ({ ...prev, [span_id]: { span_id, action, ...extra } }));
  };

  const submit = async (continueIter) => {
    const decisions = Object.values(reviewMap);
    if (decisions.length === 0 && !continueIter) {
      toast('No decisions selected');
      return;
    }
    await submitReview(sid, decisions, [], continueIter);
    toast(continueIter ? 'Review submitted. Iteration continues.' : 'Review submitted. Ready to finalize.');
    setReviewMap({});
    await refresh();
  };

  const finalize = async () => {
    await finalizeSession(sid);
    toast('Anonymizing and preparing exports');
    await refresh();
  };

  if (!session) return <div className="p-4 font-mono text-xs text-text-muted">loading...</div>;

  const pct = statusPercent(session.status, events);

  return (
    <div>
      <Panel title="Study" cite={session.id} testId="session-header"
        right={<Tag color={session.status === 'complete' ? 'accept' : session.status === 'failed' ? 'reject' : 'default'} testId="session-status">{session.status}</Tag>}
      >
        <div className="flex items-center gap-4">
          <MonoProgress percent={pct} />
          <span className="font-mono text-[10px] text-text-muted">iter {session.review_iteration}</span>
          <span className="font-mono text-[10px] text-text-muted">files {(session.files || []).length}</span>
          <span className="font-mono text-[10px] text-text-muted">spans {(session.spans || []).length}</span>
        </div>
        {session.error && (
          <div className="mt-3 border border-reject text-reject px-3 py-2 font-mono text-xs" data-testid="session-error">
            {session.error}
          </div>
        )}
      </Panel>

      {session.intake_status && session.intake_status !== 'none' && (
        <Panel title="Intake" cite="manifest-v3" testId="intake-panel"
          right={<Tag color={session.intake_status === 'ready' ? 'accept' : session.intake_status === 'review_required' ? 'phi' : 'reject'} testId="intake-status">{session.intake_status} (exit {session.intake_exit_code})</Tag>}
        >
          {(session.intake_missing || []).length > 0 && (
            <div className="mb-3 border border-reject text-reject px-3 py-2 font-mono text-xs" data-testid="intake-missing-components">
              Missing components: {session.intake_missing.join(', ')}
            </div>
          )}
          {(session.intake_review || []).length > 0 && (
            <div className="mb-3 border border-phi-border px-3 py-2 font-mono text-xs text-phi">
              <div className="uppercase text-[10px] tracking-widest mb-1">Unclassified intake entries ({session.intake_review.length})</div>
              {session.intake_review.slice(0, 20).map((e, i) => (
                <div key={i} className="text-[11px]" data-testid={`intake-review-item-${i}`}>
                  <span className="text-text-primary">{e.relpath}</span> - <span className="text-text-secondary">{e.reason}</span>
                </div>
              ))}
            </div>
          )}
          {(session.intake_missing || []).length === 0 && (session.intake_review || []).length === 0 && (
            <div className="font-mono text-xs text-text-secondary">All entries routed to components. Ready for classification.</div>
          )}
        </Panel>
      )}

      <Panel title="Files" testId="files-panel">
        <table className="w-full text-xs font-mono border border-border">
          <thead className="bg-surface">
            <tr>
              <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Name</th>
              <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Component</th>
              <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Kind</th>
              <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Size</th>
              <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">SHA-256</th>
              <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Columns / Preview</th>
              <th className="text-left px-3 py-2 border-b border-border text-text-muted">Export</th>
            </tr>
          </thead>
          <tbody>
            {(session.files || []).map(f => (
              <tr key={f.file_id} data-testid={`file-row-${f.file_id}`}>
                <td className="px-3 py-2 border-b border-r border-border text-text-primary">{f.original_name}</td>
                <td className="px-3 py-2 border-b border-r border-border"><Tag color={f.component === 'datasets' ? 'phi' : f.component === 'forms' ? 'info' : 'default'}>{f.component || '-'}</Tag></td>
                <td className="px-3 py-2 border-b border-r border-border"><Tag>{f.kind} / {f.subtype}</Tag></td>
                <td className="px-3 py-2 border-b border-r border-border text-text-secondary">{f.size_bytes} B</td>
                <td className="px-3 py-2 border-b border-r border-border text-text-muted">{(f.sha256 || '').slice(0, 12)}</td>
                <td className="px-3 py-2 border-b border-r border-border text-text-secondary max-w-md">
                  {f.kind === 'dataset' ? (
                    <div><span className="text-text-muted">columns:</span> {(f.columns || []).slice(0, 12).join(', ')}{(f.columns || []).length > 12 ? ` +${f.columns.length - 12}` : ''}</div>
                  ) : (
                    <div className="truncate max-w-md" title={f.text_preview}>{(f.text_preview || '').slice(0, 120)}</div>
                  )}
                  {f.llm_classification && f.llm_classification.content_type && (
                    <div className="mt-1"><Tag color="phi">llm: {f.llm_classification.content_type}</Tag> <span className="text-[10px] text-text-muted">{f.llm_classification.notes}</span></div>
                  )}
                </td>
                <td className="px-3 py-2 border-b border-border">
                  {session.status === 'complete' && session.export_paths && session.export_paths[f.file_id] ? (
                    <a href={exportUrl(sid, f.file_id)} className="text-accept underline decoration-dotted font-mono text-[11px]" data-testid={`export-link-${f.file_id}`}>download</a>
                  ) : (
                    <span className="text-text-muted">pending</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      <Panel title="Progress Log" cite={`${events.length} events`} testId="progress-panel">
        <div className="max-h-64 overflow-auto border border-border">
          <table className="w-full text-xs font-mono">
            <tbody>
              {events.slice(-40).reverse().map((e, i) => (
                <tr key={i} className={`border-b border-border ${i % 2 === 0 ? '' : 'bg-white/[0.02]'}`} data-testid={`progress-event-${i}`}>
                  <td className="px-3 py-1.5 border-r border-border text-text-muted whitespace-nowrap">{(e.ts || '').slice(11, 19)}</td>
                  <td className="px-3 py-1.5 border-r border-border text-phi uppercase whitespace-nowrap">{e.phase}</td>
                  <td className="px-3 py-1.5 text-text-primary">{e.message}</td>
                </tr>
              ))}
              {events.length === 0 && <tr><td className="px-3 py-2 text-text-muted">no events yet</td></tr>}
            </tbody>
          </table>
        </div>
      </Panel>

      {(session.spans || []).length > 0 && (
        <Panel
          title={`Human Review Checkpoint (${(session.spans || []).filter(s => s.review_status === 'pending').length} pending)`}
          cite="164.514(b)(2)(ii) actual knowledge"
          testId="review-panel"
          right={
            <div className="flex gap-2">
              <Btn variant="default" onClick={() => submit(true)} testId="btn-continue-review" disabled={session.status === 'complete'}>Save + Iterate</Btn>
              <Btn variant="primary" onClick={() => submit(false)} testId="btn-save-review" disabled={session.status === 'complete'}>Save Review</Btn>
              <Btn variant="danger" onClick={finalize} testId="btn-finalize" disabled={session.status === 'complete' || session.status === 'awaiting_review' ? false : true}>Finalize + Export</Btn>
            </div>
          }
        >
          <div className="border border-border">
            <table className="w-full text-xs font-mono">
              <thead className="bg-surface">
                <tr>
                  <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Value</th>
                  <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Category</th>
                  <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Where</th>
                  <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Detector</th>
                  <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Conf.</th>
                  <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Auth</th>
                  <th className="text-left px-3 py-2 border-b border-r border-border text-text-muted">Status</th>
                  <th className="text-left px-3 py-2 border-b border-border text-text-muted">Decision</th>
                </tr>
              </thead>
              <tbody>
                {(session.spans || []).map(s => {
                  const dec = reviewMap[s.span_id];
                  const chosen = dec?.action || null;
                  return (
                    <tr key={s.span_id} data-testid={`span-row-${s.span_id}`}>
                      <td className="px-3 py-2 border-b border-r border-border">
                        <span className="phi-highlight">{s.value.length > 80 ? s.value.slice(0, 80) + '...' : s.value}</span>
                      </td>
                      <td className="px-3 py-2 border-b border-r border-border">
                        <Tag color="phi" testId={`cat-${s.span_id}`}>{s.hipaa_category || '-'} / {s.entity_type}</Tag>
                      </td>
                      <td className="px-3 py-2 border-b border-r border-border text-text-secondary">
                        {s.column ? `col:${s.column}${s.row_index !== null && s.row_index !== undefined ? ` row:${s.row_index}` : ''}` : `pos:${s.start}-${s.end}`}
                      </td>
                      <td className="px-3 py-2 border-b border-r border-border text-text-muted uppercase">{s.detector}</td>
                      <td className="px-3 py-2 border-b border-r border-border text-text-secondary">{Number(s.confidence).toFixed(2)}</td>
                      <td className="px-3 py-2 border-b border-r border-border text-text-muted text-[10px]">{s.authority}</td>
                      <td className="px-3 py-2 border-b border-r border-border">
                        <Tag color={s.review_status === 'accepted' || s.review_status === 'reclassified' ? 'accept' : s.review_status === 'rejected' ? 'reject' : 'default'}>{s.review_status}</Tag>
                      </td>
                      <td className="px-3 py-2 border-b border-border">
                        <div className="flex gap-1">
                          <button
                            onClick={() => setDecision(s.span_id, 'accept')}
                            className={`h-6 px-2 border text-[10px] font-mono uppercase ${chosen === 'accept' ? 'bg-accept text-white border-accept' : 'border-accept text-accept hover:bg-accept hover:text-white'}`}
                            data-testid={`btn-accept-${s.span_id}`}
                          >
                            accept
                          </button>
                          <button
                            onClick={() => setDecision(s.span_id, 'reject')}
                            className={`h-6 px-2 border text-[10px] font-mono uppercase ${chosen === 'reject' ? 'bg-reject text-white border-reject' : 'border-reject text-reject hover:bg-reject hover:text-white'}`}
                            data-testid={`btn-reject-${s.span_id}`}
                          >
                            reject
                          </button>
                          <input
                            placeholder="comment"
                            className="h-6 px-2 bg-transparent border border-border text-[10px] w-32"
                            defaultValue={s.review_comment || ''}
                            onChange={(e) => setDecision(s.span_id, chosen || 'accept', { comment: e.target.value })}
                            data-testid={`comment-${s.span_id}`}
                          />
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="mt-3 font-mono text-[10px] text-text-muted">
            Rule: 164.514(b)(2)(ii) requires reviewer judgement on any residual re-identification risk.
            Datasets: header values are shown; row-level cell values shown only when a detector matched.
          </div>
        </Panel>
      )}
    </div>
  );
}
