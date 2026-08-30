import React, { useEffect, useRef } from 'react';

// ---- Tier 1: live one-line narration ------------------------------------
//
// A single, always-visible, continuously-updating strip of plain-language
// status lines ("Reading the dictionary file enrollment.csv", "Judge is
// deciding how to handle every flagged column", ...), sourced from each
// call's `status_text` field. Never gated behind a click, and never
// cleared once the run finishes -- it becomes a readable scrollback of
// what happened, distinct from the detailed (tier 2/3) trace below it.
//
// `status_text` is server-sanitized narration text (see docs Phase 2's
// sanitize-then-hash-chain trace stack), never raw prompt/reply content.
export default function LiveNarrationStrip({ trace }) {
  const bottomRef = useRef(null);
  const lines = (trace || [])
    .filter(m => m.direction === 'in' && m.status_text)
    .sort((a, b) => (a.ts || '').localeCompare(b.ts || ''));

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'end' });
  }, [lines.length]);

  if (lines.length === 0) return null;

  return (
    <div className="mt-6 border border-rule bg-paper-2/50 px-4 py-3" data-testid="live-narration-strip">
      <div className="kicker mb-2">What's happening</div>
      <div className="max-h-40 overflow-y-auto space-y-1 step-in" data-testid="live-narration-lines">
        {lines.map((m, i) => (
          <div key={m.id || i} className="text-[12px] text-ink-2 leading-5" data-testid={`narration-line-${i}`}>
            <span className="font-mono text-oxblood">{m.agent}</span>
            <span className="text-ink-muted mx-1.5">·</span>
            {m.status_text}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
