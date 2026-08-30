import React from 'react';
import { Panel } from '../ui';

// User Clarification (docs #16): distinct from Human Review. A specialist
// (Schema/Lexicon/Instrument) or Judge can raise a lightweight in-flow
// question mid-run (`control/handoff.py`'s `specialist_clarification`
// edge) without pausing for a formal, authorized-human decision -- that
// formal gate is `HumanReviewPanel` (`HumanReviewPacket`/`HumanDecision`,
// Phase 8). `awaiting_user_clarification` is a real `RunState` value
// (`workflow.py`) with no live orchestrator call site or API payload
// shape yet, so this panel only ever shows the safe, generic narrative
// below plus an optional plain-string `session.clarification.question`
// if a future backend ever supplies one -- never a raw nested object.
export default function UserClarificationPanel({ status, clarification }) {
  if (status !== 'awaiting_user_clarification') return null;
  const question = typeof clarification?.question === 'string' ? clarification.question : null;
  const fromAgent = typeof clarification?.from_agent === 'string' ? clarification.from_agent : null;
  return (
    <Panel title="Clarification needed" cite="A specialist asked a lightweight question mid-run -- distinct from a formal Human Review"
           testId="user-clarification-panel">
      {question ? (
        <div className="text-[13px] text-ink">
          {fromAgent && <span className="font-mono text-oxblood mr-2">{fromAgent}</span>}
          {question}
        </div>
      ) : (
        <div className="text-[12px] text-ink-muted">
          A specialist or Judge has a question about this study that needs a quick answer
          before the pipeline can continue. This is a routine in-flow check, not a formal
          review -- it will resolve automatically once answered.
        </div>
      )}
    </Panel>
  );
}
