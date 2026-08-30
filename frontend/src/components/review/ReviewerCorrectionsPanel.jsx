import React from 'react';
import { Panel } from '../ui';
import { correctionSummary } from './reviewerCorrections';

// Surfaces the Judge<->Sentinel/Reviewer correction loop (docs #96,
// Phase 8) as a concise, top-level signal -- distinct from
// `AgentTracePanel`'s full per-call detail (which this deliberately does
// not repeat) and distinct from `HumanReviewPanel` (an authorized-human
// decision, not an agent-to-agent correction).
export default function ReviewerCorrectionsPanel({ trace, advisoryIssues }) {
  const { judgeIterations, correctionRounds } = correctionSummary(trace);
  const advisoryCount = (advisoryIssues || []).length;
  if (correctionRounds === 0 && advisoryCount === 0) return null;
  return (
    <Panel title="Reviewer corrections" cite="The Judge/Sentinel correction loop -- see the full agent trace above for every call"
           testId="reviewer-corrections-panel">
      <div className="text-[13px] text-ink">
        {correctionRounds > 0 ? (
          <>Sentinel required <span className="font-mono text-oxblood">{correctionRounds}</span> correction
            round{correctionRounds === 1 ? '' : 's'}: Judge revised its classification across{' '}
            <span className="font-mono">{judgeIterations}</span> iteration{judgeIterations === 1 ? '' : 's'}
            before Sentinel signed off.</>
        ) : (
          <>Sentinel signed off on the first pass -- no corrections were required.</>
        )}
      </div>
      {advisoryCount > 0 && (
        <div className="mt-2 text-[12px] text-ink-muted">
          <span className="font-mono text-signal">{advisoryCount}</span> advisory issue{advisoryCount === 1 ? '' : 's'}{' '}
          remain(s) open (non-blocking) -- see "Sentinel advisory issues" in the agent trace above.
        </div>
      )}
    </Panel>
  );
}
