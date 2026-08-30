import React from 'react';
import { Panel, Tag } from '../ui';

// Publish Guard: the deterministic (no-LLM) last-mile residual-PHI scan at
// the download boundary. `guard.results[].findings[].sample` is the one
// place this panel shows a data-derived excerpt; it is already masked
// server-side (`phi-mask` styling only decorates an already-redacted
// sample, it does not itself redact anything) and is a deliberate,
// intentional exception documented at D3/D4 (trace/status_text sanitized
// separately) -- Publish Guard's whole job is showing operators exactly
// what residual PHI shape blocked a file, so they can fix it.
export default function PublishGuardPanel({ guard }) {
  if (!guard) return null;
  return (
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
  );
}
