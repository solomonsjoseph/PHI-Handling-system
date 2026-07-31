import React from 'react';

export default function LiveCounters({ session, events }) {
  const spans = session?.spans || [];
  const files = session?.files || [];
  const filesByComp = files.reduce((acc, f) => {
    const k = f.component || 'other';
    acc[k] = (acc[k] || 0) + 1;
    return acc;
  }, {});
  const spansByStatus = spans.reduce((acc, s) => {
    acc[s.review_status] = (acc[s.review_status] || 0) + 1;
    return acc;
  }, {});
  const spansByCat = spans.reduce((acc, s) => {
    const k = s.hipaa_category || '?';
    acc[k] = (acc[k] || 0) + 1;
    return acc;
  }, {});
  const lastEvent = (events && events.length > 0) ? events[events.length - 1] : null;

  const cell = (label, value, testId) => (
    <div className="border border-border p-3" data-testid={testId}>
      <div className="text-text-muted uppercase text-[10px] tracking-widest">{label}</div>
      <div className="text-2xl font-mono text-text-primary mt-1">{value}</div>
    </div>
  );

  return (
    <div className="grid grid-cols-4 gap-2 mt-2" data-testid="live-counters">
      {cell('Files', files.length, 'counter-files')}
      {cell('Spans', spans.length, 'counter-spans')}
      {cell('Pending review', spansByStatus.pending || 0, 'counter-pending')}
      {cell('Accepted', (spansByStatus.accepted || 0) + (spansByStatus.reclassified || 0), 'counter-accepted')}
      <div className="border border-border p-3 col-span-2" data-testid="counter-by-component">
        <div className="text-text-muted uppercase text-[10px] tracking-widest mb-2">By component</div>
        <div className="flex gap-2 flex-wrap font-mono text-[11px]">
          {Object.entries(filesByComp).length === 0 && <span className="text-text-muted">none</span>}
          {Object.entries(filesByComp).map(([k, v]) => (
            <span key={k} className="border border-border px-2 py-1 text-text-secondary">{k} <span className="text-text-primary">{v}</span></span>
          ))}
        </div>
      </div>
      <div className="border border-border p-3 col-span-2" data-testid="counter-by-category">
        <div className="text-text-muted uppercase text-[10px] tracking-widest mb-2">By HIPAA category</div>
        <div className="flex gap-1 flex-wrap font-mono text-[11px]">
          {Object.entries(spansByCat).length === 0 && <span className="text-text-muted">none</span>}
          {Object.entries(spansByCat).sort(([a], [b]) => (a || '').localeCompare(b || '')).map(([k, v]) => (
            <span key={k} className="border border-phi-border text-phi px-2 py-1" title={`HIPAA ${k}`}>{k}:{v}</span>
          ))}
        </div>
      </div>
      {lastEvent && (
        <div className="border border-border p-3 col-span-4" data-testid="counter-last-event">
          <div className="text-text-muted uppercase text-[10px] tracking-widest mb-1">Latest activity</div>
          <div className="font-mono text-xs text-text-primary">
            <span className="text-text-muted">{(lastEvent.ts || '').slice(11, 19)}</span>{'  '}
            <span className="text-phi uppercase">{lastEvent.phase}</span>{'  '}
            {lastEvent.message}
          </div>
        </div>
      )}
    </div>
  );
}
