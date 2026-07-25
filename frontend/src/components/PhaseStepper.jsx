import React from 'react';

const PHASES = [
  { key: 'intake',           label: 'Intake' },
  { key: 'reading',          label: 'Read' },
  { key: 'classifying',      label: 'Classify' },
  { key: 'detecting',        label: 'Detect' },
  { key: 'awaiting_review',  label: 'Review' },
  { key: 'applying_review',  label: 'Apply' },
  { key: 'anonymizing',      label: 'Scrub' },
  { key: 'complete',         label: 'Export' },
];

const PHASE_INDEX = Object.fromEntries(PHASES.map((p, i) => [p.key, i]));

export default function PhaseStepper({ status, iteration = 0 }) {
  const currentIdx = PHASE_INDEX[status] ?? -1;
  const failed = status === 'failed';
  return (
    <div className="border border-border" data-testid="phase-stepper">
      <div className="flex">
        {PHASES.map((p, i) => {
          const done = !failed && i < currentIdx;
          const current = !failed && i === currentIdx;
          const upcoming = !failed && i > currentIdx;
          const cls = failed && i > 0
            ? 'text-text-muted border-border'
            : done
              ? 'text-accept border-accept bg-surface'
              : current
                ? 'text-phi border-phi-border bg-phi-bg animate-pulse'
                : upcoming
                  ? 'text-text-muted border-border'
                  : 'text-text-secondary border-border';
          return (
            <div
              key={p.key}
              className={`flex-1 border-r last:border-r-0 ${cls}`}
              data-testid={`phase-step-${p.key}`}
            >
              <div className="px-3 py-2 flex items-center gap-2">
                <span className="font-mono text-[10px] text-text-muted">{String(i + 1).padStart(2, '0')}</span>
                <span className="font-mono text-[10px] uppercase tracking-widest">{p.label}</span>
                {done && <span className="ml-auto font-mono text-[10px] text-accept">OK</span>}
                {current && <span className="ml-auto font-mono text-[10px] text-phi">.....</span>}
              </div>
              <div className={`h-1 ${done ? 'bg-accept' : current ? 'bg-phi' : 'bg-border'}`} />
            </div>
          );
        })}
      </div>
      <div className="flex items-center gap-4 px-4 py-2 border-t border-border bg-surface">
        <span className="font-mono text-[10px] uppercase tracking-widest text-text-muted">Status</span>
        <span className={`font-mono text-[10px] uppercase tracking-widest ${failed ? 'text-reject' : status === 'complete' ? 'text-accept' : 'text-phi'}`} data-testid="phase-current">{status}</span>
        {iteration > 0 && <span className="font-mono text-[10px] text-text-muted">iteration {iteration}</span>}
      </div>
    </div>
  );
}
