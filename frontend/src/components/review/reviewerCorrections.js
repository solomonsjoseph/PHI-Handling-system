// Reviewer corrections (docs #96): the Judge<->Sentinel/Reviewer
// correction loop from Phase 8 (docs Phase 8's `[Sentinel]->[Reviewer]`
// rename; the orchestrator's live trace still emits agent name
// `Sentinel`, see the note in `agentMeta.js`). Derives a plain iteration
// count directly from the real, already-sanitized trace stream -- no new
// backend field needed, and nothing here duplicates `AgentTracePanel`'s
// own per-call detail.
export function correctionSummary(trace) {
  const judgePhases = new Set();
  const sentinelPhases = new Set();
  for (const m of trace || []) {
    if (m.agent === 'Judge' && m.phase) judgePhases.add(m.phase);
    if (m.agent === 'Sentinel' && m.phase) sentinelPhases.add(m.phase);
  }
  return {
    judgeIterations: judgePhases.size,
    sentinelIterations: sentinelPhases.size,
    // Each Judge pass beyond the first was triggered by a Sentinel
    // correction on the prior pass.
    correctionRounds: Math.max(0, judgePhases.size - 1),
  };
}
