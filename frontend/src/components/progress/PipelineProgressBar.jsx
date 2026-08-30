import React from 'react';
import { PHASES, phaseIndexFromEvents } from './phases';

export default function PipelineProgressBar({ events, status, phaseTimings, runElapsed, iterationCap }) {
  const currentIdx = phaseIndexFromEvents(events, status);
  const isFailed = status === 'failed' || status === 'cancelled';
  const pct = currentIdx < 0
    ? 0
    : Math.round(((currentIdx + 1) / PHASES.length) * 100);
  const current = currentIdx >= 0 ? PHASES[currentIdx] : null;

  // Sir Q "Rigor Tooltip on SessionDetail": show the chosen rigor as a
  // chip near the progress bar so reviewers see the confidence trade-off
  // at a glance without leaving the trace page.
  const _RIGOR_META = {
    1: { label: 'Fast', blurb: '1 Sentinel pass · short studies, high-confidence headers' },
    2: { label: 'Balanced', blurb: '2 Sentinel passes · default rigor for most studies' },
    3: { label: 'Thorough', blurb: '3 Sentinel passes · max defensibility, longest wallclock' },
  };
  const rigor = iterationCap ? _RIGOR_META[iterationCap] : null;

  // Sir Q "Live Wallclock Measurement": show per-phase durations once the
  // orchestrator has emitted them. Values come from session.phase_timings
  // (persisted at pipeline exit) or are derived from live SSE events for
  // the running phase.
  const timings = phaseTimings || {};
  const currentPhaseKey = current && (() => {
    // Judge/Sentinel emit iteration-suffixed phase keys; match the base.
    for (let i = (events || []).length - 1; i >= 0; i--) {
      const p = events[i]?.phase || '';
      if (p.startsWith(current.key)) return p;
    }
    return current.key;
  })();
  const currentSec = currentPhaseKey && timings[currentPhaseKey]?.duration_ms
    ? (timings[currentPhaseKey].duration_ms / 1000).toFixed(1)
    : null;

  return (
    <div className="mt-8 mb-2" data-testid="pipeline-progress-bar">
      <div className="flex items-baseline justify-between mb-2">
        <div className="flex items-baseline gap-3">
          <div className="kicker">Pipeline progress · {pct}%</div>
          {rigor && (
            <span
              className="font-mono text-[10px] px-2 py-0.5 bg-paper-2 border border-rule text-ink-2"
              data-testid="pipeline-rigor-chip"
              title={rigor.blurb}
            >
              Rigor · {rigor.label} ({iterationCap})
            </span>
          )}
        </div>
        <div className="flex items-center gap-3 font-mono text-[11px] text-ink-muted">
          {runElapsed != null && (
            <span data-testid="pipeline-elapsed">
              {runElapsed.toFixed(1)} s elapsed
            </span>
          )}
          {current && (
            <span data-testid="pipeline-current-phase">
              phase {currentIdx + 1} of {PHASES.length}
            </span>
          )}
        </div>
      </div>
      <div className="h-1 bg-paper-2 border border-rule relative overflow-hidden">
        <div
          className={`h-full transition-all duration-500 ${isFailed ? 'bg-oxblood' : 'bg-oxblood'}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      {current && (
        <div className="mt-3 flex items-start gap-3" data-testid="pipeline-current-description">
          <div className="font-mono text-[11px] text-oxblood uppercase tracking-wider mt-0.5 shrink-0">
            {String(currentIdx + 1).padStart(2, '0')}
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-baseline justify-between gap-3">
              <div className="font-display text-[14px] text-ink">{current.label}</div>
              {currentSec && (
                <div className="font-mono text-[11px] text-ink-muted shrink-0" data-testid="pipeline-current-duration">
                  {currentSec} s
                </div>
              )}
            </div>
            <div className="text-[12px] text-ink-2 leading-relaxed mt-0.5">{current.blurb}</div>
          </div>
        </div>
      )}
      {Object.keys(timings).length > 1 && (
        <details className="mt-3" data-testid="pipeline-phase-timings">
          <summary className="cursor-pointer text-[11px] font-mono text-ink-muted hover:text-oxblood">
            per-phase timings ({Object.keys(timings).length})
          </summary>
          <div className="mt-2 grid grid-cols-2 md:grid-cols-3 gap-x-6 gap-y-1 text-[11px] font-mono">
            {Object.entries(timings)
              .sort((a, b) => (a[1].start_s || 0) - (b[1].start_s || 0))
              .map(([k, v]) => (
                <div key={k} className="flex justify-between gap-2 text-ink-2">
                  <span className="truncate">{k}</span>
                  <span className="text-ink-muted shrink-0">
                    {v.duration_ms ? `${(v.duration_ms / 1000).toFixed(1)}s` : '…'}
                  </span>
                </div>
              ))}
          </div>
        </details>
      )}
    </div>
  );
}
