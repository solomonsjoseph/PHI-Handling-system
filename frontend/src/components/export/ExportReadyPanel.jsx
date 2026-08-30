import React from 'react';
import { Panel, Btn } from '../ui';
import { exportFileUrl, reversalKeyUrl } from '../../lib/api';

// Export ready (docs #96, READY_FOR_EXPORT per Phase 11a's
// FinalAssuranceGate): once Publish Guard certifies the run clean, this
// is where the study team actually retrieves their deliverables -- the
// per-file PHI-handled export (frozen `GET /export/{file_id}`), the
// reversal key (frozen `GET /reversal-key`, the closest existing
// acknowledgment analog), and Phase 12's dedicated acknowledgment action.
// Every field rendered here (`file_path`'s basename, `acknowledged_by`,
// timestamps) is short, server-controlled metadata -- never dataset
// content.
export default function ExportReadyPanel({ sid, guard, session, busy, onAcknowledge }) {
  if (!guard || guard.status !== 'clean') return null;
  const cleanFiles = (guard.results || []).filter(r => r.status === 'clean');
  const ack = session?.acknowledgment;

  return (
    <Panel title="Export ready" cite="Publish-Guard-clean deliverables -- each gated independently on the frozen export/reversal-key surface"
           testId="export-ready-panel">
      {cleanFiles.length > 0 && (
        <div className="space-y-2" data-testid="export-file-list">
          {cleanFiles.map((r, i) => {
            const name = (r.file_path || '').split('/').pop() || r.file_id;
            return (
              <div key={r.file_id || i} className="flex items-center justify-between gap-4 data-cell" data-testid={`export-file-row-${r.file_id || i}`}>
                <div className="font-mono text-[12px] text-ink">{name}</div>
                <a href={exportFileUrl(sid, r.file_id)} target="_blank" rel="noopener noreferrer">
                  <Btn size="sm" variant="ghost" testId={`btn-download-export-${r.file_id || i}`}>Download ↓</Btn>
                </a>
              </div>
            );
          })}
        </div>
      )}

      <div className="rule-top pt-4 mt-4 flex items-center justify-between gap-4" data-testid="reversal-key-row">
        <div className="text-[12px] text-ink-2">
          Reversal key -- lets the study team re-identify their own pseudonymized data. One-time
          download; the key is deleted server-side once served.
        </div>
        <a href={reversalKeyUrl(sid)} target="_blank" rel="noopener noreferrer">
          <Btn size="sm" variant="ghost" testId="btn-download-reversal-key">Download ↓</Btn>
        </a>
      </div>

      <div className="rule-top pt-4 mt-4" data-testid="acknowledgment-row">
        {ack?.acknowledged ? (
          <div className="text-[12px] text-ink-2" data-testid="acknowledgment-confirmed">
            Acknowledged by <span className="font-mono">{ack.acknowledged_by}</span> at{' '}
            <span className="font-mono">{ack.acknowledged_at}</span>.
          </div>
        ) : (
          <div className="flex items-center justify-between gap-4">
            <div className="text-[12px] text-ink-2">
              Confirm you have received this export.
            </div>
            <Btn size="sm" variant="ghost" onClick={onAcknowledge} disabled={busy} testId="btn-acknowledge-export">
              Acknowledge receipt
            </Btn>
          </div>
        )}
      </div>
    </Panel>
  );
}
