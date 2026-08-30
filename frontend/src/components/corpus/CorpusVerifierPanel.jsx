import React from 'react';
import { Panel, Tag } from '../ui';

// Corpus-mode only: scores Judge decisions against a planted-PHI ground
// truth corpus. Never rendered for a real study run (`corpusReport` is
// only ever fetched when `session.corpus_summary` is present -- a
// synthetic adversarial-testing session, never real patient data).
export default function CorpusVerifierPanel({ corpusReport }) {
  if (!corpusReport) return null;
  return (
    <Panel title="Adversarial corpus verifier"
           cite="Every planted PHI cell scored against Judge decisions + redacted export text"
           testId="corpus-verifier-panel"
           right={
             <Tag color={
               (corpusReport.correctness?.overall_f1 || 0) >= 0.999 &&
               (corpusReport.correctness?.false_negatives || []).length === 0
                 ? 'accept' : 'reject'
             } testId="corpus-verifier-status">
               F1 {((corpusReport.correctness?.overall_f1 || 0)).toFixed(4)}
             </Tag>
           }>
      <div className="grid grid-cols-4 gap-6">
        <div>
          <div className="kicker">Precision</div>
          <div className="font-display text-display-md text-oxblood" data-testid="corpus-precision">
            {(corpusReport.correctness?.overall_precision || 0).toFixed(4)}
          </div>
        </div>
        <div>
          <div className="kicker">Recall</div>
          <div className="font-display text-display-md text-oxblood" data-testid="corpus-recall">
            {(corpusReport.correctness?.overall_recall || 0).toFixed(4)}
          </div>
        </div>
        <div>
          <div className="kicker">Accuracy</div>
          <div className="font-display text-display-md text-oxblood" data-testid="corpus-accuracy">
            {(corpusReport.correctness?.overall_accuracy || 0).toFixed(4)}
          </div>
        </div>
        <div>
          <div className="kicker">Deferrals</div>
          <div className="font-display text-display-md text-ink" data-testid="corpus-deferrals">
            {corpusReport.deferral?.count || 0}
          </div>
        </div>
      </div>
      <div className="mt-6 text-[12px] text-ink-muted">
        planted <span className="font-mono">{corpusReport.summary?.planted_columns || 0}</span> cells ·
        TP <span className="font-mono">{corpusReport.summary?.tp || 0}</span> ·
        FP <span className="font-mono">{corpusReport.summary?.fp || 0}</span> ·
        FN <span className="font-mono">{corpusReport.summary?.fn || 0}</span> ·
        TN <span className="font-mono">{corpusReport.summary?.tn || 0}</span>
      </div>
      <div className="mt-6 grid grid-cols-6 gap-2" data-testid="corpus-per-category">
        {(corpusReport.correctness?.per_category || []).map(pc => (
          <div key={pc.category} className="border border-rule px-3 py-2">
            <div className="font-mono text-[11px] text-ink-muted">cat {pc.category}</div>
            <div className="font-display text-[13px] text-ink mt-0.5">
              {pc.tp}/{pc.tp + pc.fn} recalled
            </div>
            <div className="font-mono text-[10px] text-ink-muted">
              fp {pc.fp} · tn {pc.tn}
            </div>
          </div>
        ))}
      </div>
      {(corpusReport.correctness?.false_negatives || []).length > 0 && (
        <div className="mt-6 border-l-2 border-oxblood pl-4">
          <div className="kicker text-oxblood">False negatives (PHI leaks)</div>
          <div className="mt-2 space-y-1">
            {corpusReport.correctness.false_negatives.slice(0, 10).map((m, i) => (
              <div key={i} className="font-mono text-[12px] text-ink-2" data-testid={`corpus-fn-${i}`}>
                {m.file} · {m.column} · cat {m.hipaa_category} · expected {m.expected_action} → got {m.actual_action}
              </div>
            ))}
          </div>
        </div>
      )}
      {(corpusReport.correctness?.false_positives || []).length > 0 && (
        <div className="mt-4 border-l-2 border-signal pl-4">
          <div className="kicker text-signal">False positives (over-blocked)</div>
          <div className="mt-2 space-y-1">
            {corpusReport.correctness.false_positives.slice(0, 10).map((m, i) => (
              <div key={i} className="font-mono text-[12px] text-ink-2" data-testid={`corpus-fp-${i}`}>
                {m.file} · {m.column} · expected {m.expected_action} → got {m.actual_action}
              </div>
            ))}
          </div>
        </div>
      )}
    </Panel>
  );
}
