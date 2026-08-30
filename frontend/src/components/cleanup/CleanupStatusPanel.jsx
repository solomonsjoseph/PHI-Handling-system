import React from 'react';
import { Panel, Tag } from '../ui';

const VERIFICATION_COLOR = { verified: 'accept', pending: 'signal', failed: 'reject' };

// Cleanup/destruction status (docs #96, #77 CleanupManifest): every field
// here is a structured, typed `CleanupManifest` field (run_id, timestamps,
// category lists, booleans, a controlled-vocabulary status, and
// `failure_details` -- a server-authored diagnostic string about the
// cleanup process itself, never dataset/PHI content). Renders nothing
// until a terminal-path cleanup has actually run for this session
// (`cleanup` is null until then, per Phase 12's endpoint contract).
export default function CleanupStatusPanel({ cleanup }) {
  if (!cleanup) return null;
  const destroyed = cleanup.destroyed_categories || [];
  const retained = cleanup.retained_safe_categories || [];
  return (
    <Panel title="Cleanup and destruction" cite="Section 76/77: every terminal path destroys raw uploads, datasets, and working state; verified before the session is ever marked destroyed"
           testId="cleanup-status-panel"
           right={
             <Tag color={VERIFICATION_COLOR[cleanup.verification_status] || 'default'} testId="cleanup-verification-status">
               {cleanup.verification_status || 'unknown'}
             </Tag>
           }>
      <div className="grid grid-cols-2 gap-6 text-[12px] text-ink-2">
        <div>
          <div className="kicker text-ink-muted">Credentials revoked</div>
          <div className="mt-1 font-mono">{cleanup.credentials_revoked ? 'yes' : 'no'}</div>
        </div>
        <div>
          <div className="kicker text-ink-muted">Run keys destroyed</div>
          <div className="mt-1 font-mono">{cleanup.keys_destroyed ? 'yes' : 'no'}</div>
        </div>
        <div>
          <div className="kicker text-ink-muted">Sandbox destroyed</div>
          <div className="mt-1 font-mono">{cleanup.sandbox_destroyed ? 'yes' : 'no'}</div>
        </div>
        <div>
          <div className="kicker text-ink-muted">Storage sanitization</div>
          <div className="mt-1 font-mono">{cleanup.storage_sanitization_status || 'unknown'}</div>
        </div>
      </div>
      {destroyed.length > 0 && (
        <div className="mt-4">
          <div className="kicker text-ink-muted">Destroyed</div>
          <div className="mt-1 flex flex-wrap gap-1" data-testid="cleanup-destroyed-categories">
            {destroyed.map((c, i) => (
              <span key={i} className="font-mono text-[10px] px-2 py-0.5 bg-paper-2 border border-rule">{c}</span>
            ))}
          </div>
        </div>
      )}
      {retained.length > 0 && (
        <div className="mt-3">
          <div className="kicker text-ink-muted">Retained (safe, non-PHI)</div>
          <div className="mt-1 flex flex-wrap gap-1" data-testid="cleanup-retained-categories">
            {retained.map((c, i) => (
              <span key={i} className="font-mono text-[10px] px-2 py-0.5 bg-paper-2 border border-rule">{c}</span>
            ))}
          </div>
        </div>
      )}
      {cleanup.verification_status === 'failed' && typeof cleanup.failure_details === 'string' && cleanup.failure_details && (
        <div className="mt-4 border-l-2 border-oxblood pl-4" data-testid="cleanup-failure-details">
          <div className="kicker text-oxblood">Cleanup incident</div>
          <div className="text-[12px] text-ink-2 mt-1">{cleanup.failure_details}</div>
        </div>
      )}
      {(cleanup.cleanup_started_at || cleanup.cleanup_completed_at) && (
        <div className="mt-4 text-[11px] text-ink-muted font-mono">
          {cleanup.cleanup_started_at && <span>started {cleanup.cleanup_started_at}</span>}
          {cleanup.cleanup_started_at && cleanup.cleanup_completed_at && <span> · </span>}
          {cleanup.cleanup_completed_at && <span>completed {cleanup.cleanup_completed_at}</span>}
        </div>
      )}
    </Panel>
  );
}
