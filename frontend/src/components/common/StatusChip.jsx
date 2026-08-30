import React from 'react';
import { Tag } from '../ui';

// Status vocabulary in live use today (server.py's actual `status` field
// values) plus the newer `RunState` lifecycle names (`workflow.py`'s
// `RUN_LIFECYCLE_STATES`) that `Session.status` is typed to accept even
// where no live code path assigns them yet -- e.g. `blocked` (the D9
// `publish_guard` terminal, real today), `security_incident` (only ever
// set as an internal `FinalAssuranceGate` input today, docs #57, never a
// live session status -- included here so the chip renders sanely the
// moment it is). The status string itself is a short controlled-vocabulary
// token, never raw LLM output or PHI, so it is always safe to render as-is.
const STATUS_COLOR = {
  // Live today.
  complete: 'accept',
  partially_complete: 'signal',
  awaiting_human_review: 'signal',
  classifying: 'signal',
  anonymizing: 'signal',
  reading: 'signal',
  cancelled: 'default',
  intake_failed: 'reject',
  failed: 'reject',
  blocked: 'reject',
  erasure_pending: 'signal',
  // Newer `RunState` lifecycle vocabulary (workflow.py), not yet emitted
  // by any live code path but a valid `Session.status` value today.
  awaiting_user_clarification: 'signal',
  correction_required: 'signal',
  preview_review: 'signal',
  human_review_pending: 'signal',
  executing: 'signal',
  execution_verifying: 'signal',
  final_review: 'signal',
  final_assurance: 'signal',
  ready_for_export: 'accept',
  export_confirmed: 'accept',
  destroying: 'signal',
  session_destroyed: 'default',
  security_incident: 'reject',
};

export default function StatusChip({ status }) {
  return <Tag color={STATUS_COLOR[status] || 'default'} testId="status-chip">{status || 'unknown'}</Tag>;
}
