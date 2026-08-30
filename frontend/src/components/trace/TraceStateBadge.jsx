import React from 'react';

export default function TraceStateBadge({ state }) {
  const map = {
    running: { color: 'bg-signal text-paper', label: 'RUNNING' },
    done: { color: 'bg-clean text-paper', label: 'DONE' },
    errored: { color: 'bg-oxblood text-paper', label: 'ERROR' },
    info: { color: 'bg-ink-muted text-paper', label: 'INFO' },
  };
  const cfg = map[state] || map.info;
  return (
    <span className={`inline-block px-2 py-0.5 text-[10px] font-mono ${cfg.color}`}>
      {cfg.label}
    </span>
  );
}
