import React from 'react';
import { Btn } from '../ui';
import StatusChip from '../common/StatusChip';
import BlockedNotice from './BlockedNotice';
import SecurityIncidentNotice from './SecurityIncidentNotice';

// The run's top-level status narrative plus its primary actions (download,
// cancel, start another run). Differentiates every terminal/paused status
// the live runtime can report -- `blocked`, `security_incident`,
// `intake_failed`, and `failed` previously all collapsed into one generic
// "Something went wrong." message; each now gets its own honest headline,
// with `BlockedNotice`/`SecurityIncidentNotice` supplying the safe detail
// underneath for the two that need it.
function heroTitle({ isComplete, isPartiallyComplete, reviewNeeded, isPending, status }) {
  if (isComplete) return 'Handled.';
  if (isPartiallyComplete) return 'Partially handled — some columns still pending.';
  if (reviewNeeded) return 'Awaiting your review.';
  if (status === 'cancelled') return 'Run cancelled.';
  if (status === 'blocked') return 'Run blocked.';
  if (status === 'security_incident') return 'Under security review.';
  if (status === 'intake_failed') return 'Upload failed validation.';
  if (status === 'failed') return 'Run failed.';
  if (isPending) return 'Working on it.';
  return 'Something went wrong.';
}

export default function RunHero({
  sid, status, isComplete, isPartiallyComplete, reviewNeeded, isPending,
  guard, humanRows, busy, wantPub, downloadBundle, navigate,
  cancelRequested, onCancel,
}) {
  return (
    <div className="rule-bottom pb-10">
      <div className="kicker">Run receipt</div>
      <div className="mt-2 flex items-baseline gap-4 flex-wrap">
        <h1 className="font-display text-display-lg text-ink">
          {heroTitle({ isComplete, isPartiallyComplete, reviewNeeded, isPending, status })}
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
            disabled={busy || cancelRequested}
            testId="btn-cancel-run"
            onClick={onCancel}
          >
            {cancelRequested ? 'Cancel pending…' : '■ Stop this run'}
          </Btn>
        </div>
      )}

      <BlockedNotice status={status} guard={guard} />
      <SecurityIncidentNotice status={status} />
    </div>
  );
}
