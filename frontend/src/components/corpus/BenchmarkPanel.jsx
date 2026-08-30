import React from 'react';
import { Panel, Btn, Tag } from '../ui';

// Corpus-mode only: per-dataset benchmark comparing Judge's method choice
// against the gold-standard verdict for every column in the corpus.
export default function BenchmarkPanel({ benchmarkReport, onDownload, busy }) {
  if (!benchmarkReport) return null;
  return (
    <Panel title="Per-dataset benchmark"
           cite="Per-column method, why, how, confidence, and gold verdict; see the benchmark bundle for the full report"
           testId="benchmark-panel"
           right={
             <Btn variant="ghost" size="sm" onClick={onDownload} disabled={busy} testId="benchmark-download">
               Download benchmark ↓
             </Btn>
           }>
      <div className="grid grid-cols-5 gap-6">
        <div>
          <div className="kicker">Leak rate</div>
          <div className="font-display text-display-md text-oxblood" data-testid="benchmark-leak-rate">
            {((benchmarkReport.totals?.leak_rate || 0) * 100).toFixed(2)}%
          </div>
        </div>
        <div>
          <div className="kicker">F1</div>
          <div className="font-display text-display-md text-oxblood" data-testid="benchmark-f1">
            {(benchmarkReport.totals?.f1 || 0).toFixed(4)}
          </div>
        </div>
        <div>
          <div className="kicker">Method-exact rate</div>
          <div className="font-display text-display-md text-ink" data-testid="benchmark-method-exact-rate">
            {((benchmarkReport.totals?.method_exact_rate || 0) * 100).toFixed(1)}%
          </div>
        </div>
        <div>
          <div className="kicker">Autonomy rate</div>
          <div className="font-display text-display-md text-ink" data-testid="benchmark-autonomy">
            {((benchmarkReport.totals?.autonomy_rate || 0) * 100).toFixed(1)}%
          </div>
        </div>
        <div>
          <div className="kicker">Identifiers removed before prompt</div>
          <div className="font-display text-display-md text-ink" data-testid="benchmark-scrub-count">
            {benchmarkReport.context_hygiene?.identifiers_removed_before_prompt ?? '—'}
          </div>
        </div>
      </div>
      <div className="mt-6 overflow-x-auto" data-testid="benchmark-columns-table">
        <table className="w-full text-[12px]">
          <thead>
            <tr className="text-ink-muted text-left border-b border-rule">
              <th className="py-2 pr-3">Column</th>
              <th className="py-2 pr-3">Gold category</th>
              <th className="py-2 pr-3">Method</th>
              <th className="py-2 pr-3">Why</th>
              <th className="py-2 pr-3">Confidence</th>
              <th className="py-2 pr-3">Decided by</th>
              <th className="py-2 pr-3">Verdict</th>
            </tr>
          </thead>
          <tbody>
            {(benchmarkReport.columns || []).map((c, i) => (
              <tr key={i} className="border-b border-rule/50" data-testid={`benchmark-column-${i}`}>
                <td className="py-2 pr-3 font-mono">{c.file}:{c.column}</td>
                <td className="py-2 pr-3">{c.gold_category}</td>
                <td className="py-2 pr-3">{c.action_label}</td>
                <td className="py-2 pr-3 text-ink-2 max-w-xs truncate" title={c.reason}>{c.reason}</td>
                <td className="py-2 pr-3 font-mono">{c.confidence == null ? '—' : c.confidence.toFixed(2)}</td>
                <td className="py-2 pr-3">{c.decided_by}</td>
                <td className="py-2 pr-3">
                  <Tag color={c.verdict === 'correct' ? 'accept' : 'reject'}>{c.verdict}</Tag>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}
