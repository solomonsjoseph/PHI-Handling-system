import React from 'react';

// Blocked state (docs #96): `blocked` is the real, live D9 `publish_guard`
// terminal (`workflow.py` docstring, `_SETTLED_STATUSES`/gate_results
// status vocabulary in `server.py`) -- a session lands here when Publish
// Guard's last-mile scan finds residual PHI it cannot certify clean. This
// is a top-level narrative only; the per-file findings that actually
// explain *why* live in `PublishGuardPanel` below (`guard.results[]`),
// which this component intentionally does not duplicate.
export default function BlockedNotice({ status, guard }) {
  if (status !== 'blocked') return null;
  return (
    <div className="mt-10 border-l-2 border-oxblood pl-4 py-3 bg-paper-2/50" data-testid="blocked-notice">
      <div className="kicker text-oxblood">Run blocked</div>
      <p className="text-[12px] text-ink-2 mt-2 leading-relaxed">
        Publish Guard's last-mile scan found residual PHI it could not certify as clean
        {typeof guard?.blocked === 'number' && guard.blocked > 0
          ? ` in ${guard.blocked} file(s)` : ''}. No download is available until every
        blocked finding below is resolved and the pipeline is re-run.
      </p>
    </div>
  );
}
