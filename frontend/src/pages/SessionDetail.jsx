import React, { useEffect, useRef, useState } from 'react';
import { useParams, useSearchParams, useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import axios from 'axios';
import { API, getSession, streamUrl, whoami } from '../lib/api';
import { Btn, Panel, Tag } from '../components/ui';
import Spinner from '../components/common/Spinner';
import StatusChip from '../components/common/StatusChip';
import AgentTracePanel from '../components/trace/AgentTracePanel';
import LiveNarrationStrip from '../components/trace/LiveNarrationStrip';
import PipelineProgressBar from '../components/progress/PipelineProgressBar';

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
  // D13 step 8: the reviewer identity shown here must be the actual
  // authenticated credential the backend records against every decision
  // (`resolve_principal`), never an operator-typed value -- a free-text
  // field could show a name that does not match what the server actually
  // stamps on the review event. Fetched once on mount below.
  const [principal, setPrincipal] = useState(null);
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
      if (traceCursorRef.current) params.set('after_seq', traceCursorRef.current);
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
    whoami().then(w => setPrincipal(w?.principal || ''));
  }, []);

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
    if (!principal) { toast.error('Not authenticated -- reload the page'); return; }
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

  const confirmAuditorConfidence = async () => {
    // D13 step 4/7/8: a confidence-only control, distinct from the
    // per-column resolutions above -- it answers the Auditor's own
    // second-review escalation and must echo the exact `audit_version`
    // that opened it, or the backend rejects it as a stale confirmation.
    if (!principal) { toast.error('Not authenticated -- reload the page'); return; }
    if (!session?.audit_version) { toast.error('No open Auditor confirmation on this run'); return; }
    setBusy(true);
    try {
      const r = await axios.post(`${API}/sessions/${sid}/human-review`, {
        resolutions: [], client_event_id: crypto.randomUUID(),
        confirm_auditor_confidence: true, audit_version: session.audit_version,
      });
      toast.success(`Auditor confidence confirmed (${r.data.status})`);
      await refresh();
    } catch (e) {
      toast.error(`confirmation failed: ${e?.response?.data?.detail || e.message}`);
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
              <div className="kicker">Reviewer identity</div>
              {/* D13 step 8: read-only -- this is the authenticated
                  credential from whoami(), never operator-editable text,
                  so what is displayed here always matches what the
                  backend actually stamps on the review event. */}
              <div data-testid="reviewer-id"
                   className="mt-2 w-full h-10 flex items-center border-b border-ink text-ink font-mono">
                {principal === null ? 'loading…' : principal || 'not authenticated'}
              </div>
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

          {/* D13 step 4/7/8: the Auditor's own second-review escalation.
              Distinct from the per-column resolutions below -- confidence
              is telemetry (D12), never evidence, so it is labelled as
              such; confirming answers only "I have seen this verdict",
              never "I agree the export is clean". */}
          {session?.audit_version && (
            <div className="rule-top pt-5 mb-6" data-testid="auditor-confirmation-panel">
              <div className="kicker mb-3">Auditor second review</div>
              <div className="text-[12px] text-ink-2 mb-2">
                Verdict: <span className="font-mono text-ink">{session.audit?.verdict || 'unknown'}</span>
                {' · '}Confidence (telemetry, not evidence):{' '}
                <span className="font-mono text-ink">
                  {typeof session.audit?.confidence === 'number' ? session.audit.confidence.toFixed(2) : '—'}
                </span>
              </div>
              {session.audit?.summary && (
                <div className="text-[12px] text-ink-2 mb-2">{session.audit.summary}</div>
              )}
              {(session.audit?.issues || []).length > 0 && (
                <ul className="text-[12px] text-ink-2 list-disc pl-5 mb-3 space-y-1" data-testid="auditor-issues-list">
                  {session.audit.issues.map((iss, i) => (
                    <li key={i}>
                      {iss.file ? <span className="font-mono">{iss.file}</span> : null}
                      {iss.column ? <span className="font-mono"> · {iss.column}</span> : null}
                      {': '}{iss.problem || JSON.stringify(iss)}
                    </li>
                  ))}
                </ul>
              )}
              <Btn size="sm" variant="ghost" onClick={confirmAuditorConfidence}
                   disabled={busy || !principal} testId="btn-confirm-auditor-confidence">
                Confirm I have reviewed this verdict
              </Btn>
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
                <Btn variant="primary" onClick={submitReview} disabled={busy || !principal} testId="btn-accept-globally">
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
