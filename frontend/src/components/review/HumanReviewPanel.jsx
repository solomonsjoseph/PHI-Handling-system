import React from 'react';
import { Panel, Btn } from '../ui';

// The real Phase 8 Human Review flow: the formal, authorized-human gate
// (`HumanReviewPacket`/`HumanDecision` on the backend) for every column
// Sentinel/Judge could not resolve on their own. Distinct from the
// lighter-weight, in-flow `UserClarificationPanel` a specialist/Judge
// exchange can raise mid-run (docs #16) -- this panel only ever renders
// once the run has actually paused (`awaiting_human_review` /
// `partially_complete`) waiting on an authorized human's decision of
// record, never for a routine specialist clarification.
export default function HumanReviewPanel({
  isPartiallyComplete,
  humanRows,
  principal,
  reviewComment,
  setReviewComment,
  datasetFiles,
  fileReviewAck,
  setFileReviewAck,
  downloadDatasetFile,
  busy,
  resolutions,
  setResolutions,
  actualKnowledgeAck,
  setActualKnowledgeAck,
  submitReview,
}) {
  return (
    <Panel title="Human review" cite="You are the reviewer of record; decisions carry your id + timestamp"
           testId="human-review-panel">
      {isPartiallyComplete && (
        <div className="mb-6 border-l-2 border-signal pl-4 py-2 bg-paper-2/50" data-testid="partially-complete-banner">
          <div className="text-[12px] text-ink-2">
            A partial bundle is ready above. <span className="font-mono">{humanRows.length}</span> column(s) below are
            still withheld from every export pending your decision — never defaulted, never blanked.
          </div>
        </div>
      )}
      <div className="grid grid-cols-2 gap-6 mb-6">
        <div>
          <div className="kicker">Reviewer identity</div>
          {/* D13 step 8: read-only -- this is the authenticated
              credential from whoami(), never operator-editable text,
              so what is displayed here always matches what the
              backend actually stamps on the review event. */}
          <div data-testid="reviewer-id"
               className="mt-2 w-full h-10 flex items-center border-b border-ink text-ink font-mono">
            {principal === null ? 'loading…' : principal || 'not authenticated'}
          </div>
        </div>
        <div>
          <div className="kicker">Comment</div>
          <input data-testid="reviewer-comment" value={reviewComment} onChange={e => setReviewComment(e.target.value)}
                 placeholder="general note for this submission"
                 className="mt-2 w-full h-10 bg-transparent border-b border-ink text-ink focus:border-oxblood"/>
        </div>
      </div>

      {/* Original file access: the system never opens or reads these on
          the reviewer's behalf -- only column headers ever reach a model. */}
      {datasetFiles.length > 0 && (
        <div className="rule-top pt-5 mb-6" data-testid="dataset-file-review-panel">
          <div className="kicker mb-3">Original dataset file(s) <span className="text-oxblood">(required)</span></div>
          <div className="text-[12px] text-ink-muted mb-3">
            Download the original file(s) and open them in your own tool to judge the flagged columns below.
          </div>
          <div className="space-y-2">
            {datasetFiles.map(f => (
              <div key={f.file_id} className="flex items-center justify-between gap-4 data-cell" data-testid={`dataset-file-row-${f.file_id}`}>
                <div className="font-mono text-[12px] text-ink">{f.original_name}</div>
                <Btn size="sm" variant="ghost" onClick={() => downloadDatasetFile(f.file_id)} testId={`btn-download-dataset-file-${f.file_id}`}>
                  Download ↓
                </Btn>
              </div>
            ))}
          </div>
          <label className="mt-4 flex items-start gap-3 cursor-pointer" data-testid="file-review-ack-label">
            <input type="checkbox" checked={fileReviewAck}
                   onChange={e => setFileReviewAck(e.target.checked)}
                   data-testid="file-review-ack"
                   className="mt-[3px] h-4 w-4 accent-oxblood"/>
            <span className="text-[12px] text-ink-2 leading-5">
              I have downloaded and reviewed the original file(s) above in my own tool.
            </span>
          </label>
        </div>
      )}

      {humanRows.length > 0 ? (() => {
        const setRowMode = (key, mode) => setResolutions(prev => ({
          ...prev, [key]: { ...(prev[key] || {}), mode, comment: mode === 'comment' ? (prev[key]?.comment || '') : '' },
        }));
        const setRowComment = (key, text) => setResolutions(prev => ({
          ...prev, [key]: { ...(prev[key] || {}), mode: 'comment', comment: text },
        }));
        const clearRow = (key) => setResolutions(prev => {
          const next = { ...prev }; delete next[key]; return next;
        });
        const activeRows = humanRows.filter(d => resolutions[`${d.file_id}|${d.column}`]?.mode !== 'defer');
        const setAsideRows = humanRows.filter(d => resolutions[`${d.file_id}|${d.column}`]?.mode === 'defer');
        const renderRow = (d) => {
          const key = `${d.file_id}|${d.column}`;
          const current = resolutions[key] || {};
          const pending = d.pending_confirmation;
          return (
            <div key={key} className="data-cell space-y-3" data-testid={`review-row-${d.column}`}>
              <div>
                <div className="font-mono text-[13px] text-ink">{d.column}</div>
                <div className="text-[12px] text-ink-2 mt-1">{d.reviewer_prompt || d.reason || 'no rationale'}</div>
                {d.needs_file_glance && (
                  <div className="text-[11px] text-oxblood mt-1">↑ open the original file above to judge this free-text column</div>
                )}
              </div>

              {pending ? (
                <div className="border-l-2 border-signal pl-3 py-2 bg-paper-2/50" data-testid={`review-row-confirm-${d.column}`}>
                  <div className="text-[12px] text-ink-2">
                    You said: <span className="italic">"{d.reviewer_comment}"</span> — I read that as:{' '}
                    <span className="font-mono text-oxblood">{pending.action || '(no clear action)'}</span>.
                    {pending.reason ? ` ${pending.reason}` : ''} Confirm?
                  </div>
                  <div className="mt-2 flex gap-2">
                    <Btn size="sm" variant={current.mode === 'approve' ? 'primary' : 'ghost'}
                         disabled={!pending.action}
                         onClick={() => setRowMode(key, 'approve')} testId={`btn-confirm-${d.column}`}>
                      Confirm
                    </Btn>
                    <Btn size="sm" variant={current.mode === 'comment' ? 'primary' : 'ghost'}
                         onClick={() => setRowMode(key, 'comment')} testId={`btn-recomment-${d.column}`}>
                      Re-comment
                    </Btn>
                    <Btn size="sm" variant="ghost" onClick={() => setRowMode(key, 'defer')} testId={`btn-defer-${d.column}`}>
                      Defer
                    </Btn>
                  </div>
                </div>
              ) : (
                <div className="flex gap-2" data-testid={`review-row-buttons-${d.column}`}>
                  <Btn size="sm" variant={current.mode === 'approve' ? 'primary' : 'ghost'}
                       disabled={!d.suggested_action}
                       onClick={() => setRowMode(key, 'approve')} testId={`btn-approve-${d.column}`}
                       title={d.suggested_action ? `Apply: ${d.suggested_action}` : 'No suggested action available — use Comment'}>
                    Approve{d.suggested_action ? ` (${d.suggested_action})` : ''}
                  </Btn>
                  <Btn size="sm" variant={current.mode === 'comment' ? 'primary' : 'ghost'}
                       onClick={() => setRowMode(key, 'comment')} testId={`btn-comment-${d.column}`}>
                    Comment
                  </Btn>
                  <Btn size="sm" variant="ghost" onClick={() => setRowMode(key, 'defer')} testId={`btn-defer-${d.column}`}>
                    Defer
                  </Btn>
                </div>
              )}

              {current.mode === 'comment' && (
                <textarea
                  value={current.comment || ''}
                  onChange={e => setRowComment(key, e.target.value)}
                  placeholder="Tell the system what should happen to this column…"
                  className="w-full h-16 bg-transparent border border-rule text-ink px-2 py-1.5 text-[12px] focus:border-oxblood"
                  data-testid={`review-row-comment-${d.column}`}
                />
              )}
            </div>
          );
        };
        return (
          <div className="space-y-4">
            <div className="space-y-5">{activeRows.map(renderRow)}</div>
            {setAsideRows.length > 0 && (
              <div className="rule-top pt-4" data-testid="set-aside-panel">
                <div className="kicker text-ink-muted mb-2">Set aside for later ({setAsideRows.length})</div>
                <div className="space-y-2">
                  {setAsideRows.map(d => {
                    const key = `${d.file_id}|${d.column}`;
                    return (
                      <div key={key} className="flex items-center justify-between gap-4 text-[12px] text-ink-muted" data-testid={`set-aside-row-${d.column}`}>
                        <span className="font-mono">{d.column}</span>
                        <button className="text-oxblood hover:underline" onClick={() => clearRow(key)} data-testid={`btn-unset-aside-${d.column}`}>
                          bring back
                        </button>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
            <label className="mt-2 flex items-start gap-3 rule-top pt-4 cursor-pointer" data-testid="actual-knowledge-ack-label">
              <input type="checkbox" checked={actualKnowledgeAck}
                     onChange={e => setActualKnowledgeAck(e.target.checked)}
                     data-testid="actual-knowledge-ack"
                     className="mt-[3px] h-4 w-4 accent-oxblood"/>
              <span className="text-[12px] text-ink-2 leading-5">
                <span className="font-mono text-oxblood">Required for approved/commented columns · 45 CFR 164.514(b)(2)(ii).</span>{' '}
                I have no actual knowledge that the information I am resolving this round, alone or in combination
                with other reasonably available information, could be used to identify an individual.
              </span>
            </label>
            <div className="pt-4 flex justify-end">
              <Btn variant="primary" onClick={submitReview}
                   disabled={busy || humanRows.some(d => !resolutions[`${d.file_id}|${d.column}`]?.mode)}
                   testId="btn-submit-human-review">
                Submit ({humanRows.filter(d => resolutions[`${d.file_id}|${d.column}`]?.mode).length}/{humanRows.length}) →
              </Btn>
            </div>
          </div>
        );
      })() : (
        <div className="space-y-4">
          <div className="text-[12px] text-ink-muted">
            Nothing is flagged for a specific column right now — resume the pipeline to continue.
          </div>
          <div className="flex justify-end">
            <Btn variant="primary" onClick={submitReview} disabled={busy || !principal} testId="btn-accept-globally">
              Resume →
            </Btn>
          </div>
        </div>
      )}
    </Panel>
  );
}
