import React from 'react';

// 8 pipeline phases mapped to the agent flow. Names match Sir's spec.
const PHASES = [
  { key: 'intake',                 label: 'Intake',       agents: ['Intake'] },
  { key: 'specialists',            label: 'Specialists',  agents: ['Lexicon', 'Schema', 'Instrument'] },
  { key: 'statute',                label: 'Regulations',  agents: ['Statute', 'Praxis'] },
  { key: 'judge_sentinel',         label: 'Classify',     agents: ['Judge', 'Sentinel'] },
  { key: 'awaiting_human_review',  label: 'Human Review', agents: ['Human'] },
  { key: 'executor_auditor',       label: 'Apply + Audit', agents: ['Executor', 'Auditor'] },
  { key: 'ledger_scout',           label: 'Benchmark',    agents: ['Scout', 'Ledger'] },
  { key: 'herald',                 label: 'Manuscript',   agents: ['Herald'] },
];

// Map raw session.status + latest agent trace phase to a phase index.
function phaseIndex(session, latestTracePhase) {
  const s = (session?.status || '').toLowerCase();
  if (s === 'complete') return PHASES.length;
  if (s === 'failed')   return -1;
  if (s === 'created' || s === 'intake') return 0;
  if (s === 'reading' || s === 'classifying') {
    if (latestTracePhase?.startsWith('lexicon') || latestTracePhase?.startsWith('schema') || latestTracePhase?.startsWith('instrument')) return 1;
    if (latestTracePhase?.startsWith('statute') || latestTracePhase?.startsWith('praxis')) return 2;
    if (latestTracePhase?.startsWith('judge') || latestTracePhase?.startsWith('sentinel')) return 3;
    return 1;
  }
  if (s === 'awaiting_human_review') return 4;
  if (s === 'applying_review' || s === 'anonymizing') {
    if (latestTracePhase?.startsWith('scout') || latestTracePhase?.startsWith('ledger')) return 6;
    if (latestTracePhase?.startsWith('herald')) return 7;
    return 5;
  }
  return 0;
}

export default function AgentPhaseStepper({ session, trace }) {
  const latest = trace?.length ? trace[trace.length - 1].phase : '';
  const currentIdx = phaseIndex(session, latest);
  const failed = session?.status === 'failed';
  return (
    <div className="border border-border bg-surface" data-testid="agent-phase-stepper">
      <div className="flex">
        {PHASES.map((p, i) => {
          const done    = !failed && i < currentIdx;
          const current = !failed && i === currentIdx;
          const cls = failed
            ? (i === 0 ? 'text-reject border-reject' : 'text-text-muted border-border')
            : done
              ? 'text-accept border-accept'
              : current
                ? 'text-phi border-phi-border bg-phi-bg'
                : 'text-text-muted border-border';
          return (
            <div
              key={p.key}
              className={`flex-1 border-r last:border-r-0 border-t border-b ${cls}`}
              data-testid={`stepper-${p.key}`}
            >
              <div className="px-3 py-2">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-[10px] text-text-muted">{String(i + 1).padStart(2, '0')}</span>
                  <span className="font-mono text-[10px] uppercase tracking-widest">{p.label}</span>
                  {done && <span className="ml-auto font-mono text-[9px] text-accept">OK</span>}
                  {current && <span className="ml-auto font-mono text-[9px] text-phi animate-pulse">RUN</span>}
                </div>
                <div className="mt-1 font-mono text-[9px] text-text-secondary">{p.agents.join(' + ')}</div>
              </div>
              <div className={`h-0.5 ${done ? 'bg-accept' : current ? 'bg-phi' : 'bg-border'}`} />
            </div>
          );
        })}
      </div>
    </div>
  );
}
