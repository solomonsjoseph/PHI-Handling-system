import React, { useEffect, useRef, useState } from 'react';
import { useParams, useSearchParams, useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import axios from 'axios';
import { API, getSession, streamUrl, whoami, acknowledgeSession, getCleanupStatus } from '../lib/api';
import { Btn } from '../components/ui';
import Spinner from '../components/common/Spinner';
import AgentTracePanel from '../components/trace/AgentTracePanel';
import LiveNarrationStrip from '../components/trace/LiveNarrationStrip';
import PipelineProgressBar from '../components/progress/PipelineProgressBar';
import PublishGuardPanel from '../components/guard/PublishGuardPanel';
import CorpusVerifierPanel from '../components/corpus/CorpusVerifierPanel';
import BenchmarkPanel from '../components/corpus/BenchmarkPanel';
import HumanReviewPanel from '../components/review/HumanReviewPanel';
import DevLogsPanel from '../components/dev/DevLogsPanel';
import RunHero from '../components/status/RunHero';
import UserClarificationPanel from '../components/clarification/UserClarificationPanel';
import ReviewerCorrectionsPanel from '../components/review/ReviewerCorrectionsPanel';
import ExportReadyPanel from '../components/export/ExportReadyPanel';
import ExpiryWarningPanel from '../components/export/ExpiryWarningPanel';
import CleanupStatusPanel from '../components/cleanup/CleanupStatusPanel';

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
  const [cleanupStatus, setCleanupStatus] = useState(null);
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

  // Statuses where a terminal-path cleanup (docs #76) might plausibly
  // have already run -- avoids polling the cleanup-status endpoint on
  // every SSE tick of an otherwise-live run.
  const _CLEANUP_RELEVANT = new Set([
    'complete', 'partially_complete', 'failed', 'cancelled', 'intake_failed',
    'blocked', 'erasure_pending', 'security_incident', 'destroying', 'session_destroyed',
  ]);

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
    if (_CLEANUP_RELEVANT.has(s.status)) {
      getCleanupStatus(sid).then(cs => setCleanupStatus(cs?.cleanup || null));
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

  const cancelRun = async () => {
    setBusy(true);
    try {
      await axios.post(`${API}/sessions/${sid}/cancel`);
      toast.info('Cancel requested — pipeline exits at next phase boundary.');
      await refresh();
    } catch (e) {
      toast.error(`cancel failed: ${e?.response?.data?.detail || e.message}`);
    } finally { setBusy(false); }
  };

  const onAcknowledge = async () => {
    setBusy(true);
    try {
      await acknowledgeSession(sid);
      toast.success('Export receipt acknowledged');
      await refresh();
    } catch (e) {
      toast.error(`acknowledge failed: ${e?.response?.data?.detail || e.message}`);
    } finally { setBusy(false); }
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
      <RunHero
        sid={sid}
        status={status}
        isComplete={isComplete}
        isPartiallyComplete={isPartiallyComplete}
        reviewNeeded={reviewNeeded}
        isPending={isPending}
        guard={guard}
        humanRows={humanRows}
        busy={busy}
        wantPub={wantPub}
        downloadBundle={downloadBundle}
        navigate={navigate}
        cancelRequested={session?.cancel_requested}
        onCancel={cancelRun}
      />

      <UserClarificationPanel status={status} clarification={session?.clarification} />

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

      <ReviewerCorrectionsPanel trace={trace} advisoryIssues={session?.advisory_issues} />

      {/* Guard */}
      <PublishGuardPanel guard={guard} />

      <ExportReadyPanel sid={sid} guard={guard} session={session} busy={busy} onAcknowledge={onAcknowledge} />
      <ExpiryWarningPanel exportExpiresAt={session?.export_expires_at} />

      <CorpusVerifierPanel corpusReport={corpusReport} />

      <BenchmarkPanel benchmarkReport={benchmarkReport} onDownload={downloadBenchmark} busy={busy} />
      {reviewNeeded && (
        <HumanReviewPanel
          isPartiallyComplete={isPartiallyComplete}
          humanRows={humanRows}
          principal={principal}
          reviewComment={reviewComment}
          setReviewComment={setReviewComment}
          datasetFiles={datasetFiles}
          fileReviewAck={fileReviewAck}
          setFileReviewAck={setFileReviewAck}
          downloadDatasetFile={downloadDatasetFile}
          busy={busy}
          resolutions={resolutions}
          setResolutions={setResolutions}
          actualKnowledgeAck={actualKnowledgeAck}
          setActualKnowledgeAck={setActualKnowledgeAck}
          submitReview={submitReview}
        />
      )}

      {/* Pending state */}
      {isPending && (
        <div className="mt-16 rule-top pt-6" data-testid="pending-panel">
          <div className="kicker flex items-center gap-3"><Spinner/> Pipeline</div>
          <div className="mt-3 font-display text-display-sm text-ink">{status}</div>
          <div className="text-[12px] text-ink-muted mt-2">The download becomes available once every file clears the guard.</div>
        </div>
      )}

      <DevLogsPanel devOpen={devOpen} setDevOpen={setDevOpen} decisions={decisions} trace={trace} />

      <CleanupStatusPanel cleanup={cleanupStatus} />
    </div>
  );
}
