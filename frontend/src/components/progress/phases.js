// ---- Phased progress bar ----------------------------------------------
//
// Sir Q "show the user a progress bar going through each phase and what
// agent is working on each phase". Short human descriptions render up
// front; drill-down into the agent trace panel below for detail.
//
// The phase keys below match the `on_phase(...)` labels emitted by the
// orchestrator. Order matches pipeline execution. These are the live
// SSE `event.phase` prefixes the running orchestrator actually emits
// today, distinct from the newer `RUN_LIFECYCLE_STATES` session-status
// vocabulary (`workflow.py`) -- the two are not the same axis: this list
// tracks live per-call progress inside one pipeline run, while
// `session.status` tracks the run's own lifecycle state.
export const PHASES = [
  { key: 'specialists', label: 'Reading study files', blurb: 'Lexicon (dictionary), Schema (dataset headers only) and Instrument (forms) read the three study components in parallel.' },
  { key: 'statute',     label: 'Fetching regulations', blurb: 'Statute agent web-searches the current jurisdictional rules (HIPAA §164.514 for US).' },
  { key: 'praxis',      label: 'Compiling PHI methods', blurb: 'Praxis pulls the current best-practice technique per HIPAA identifier category from the web (cached across sessions).' },
  { key: 'judge_iter',  label: 'Classifying variables', blurb: 'Judge decides keep vs transform per column using Statute rules + Praxis techniques.' },
  { key: 'sentinel_iter', label: 'Verifying classification', blurb: 'Sentinel reviews every decision; iterates only when a real PHI leak is at stake.' },
  { key: 'human_review_required', label: 'Awaiting your review', blurb: 'Sentinel flagged cases the model cannot resolve; sign off in the panel below.' },
  { key: 'executor',    label: 'Applying transforms', blurb: 'Executor deterministically applies drop / year_only / cap_age_90 / pseudonymize / hash / scrub_text per Judge decision.' },
  { key: 'publish_guard', label: 'Last-mile PHI scan', blurb: 'Publish Guard scans every export byte for residual PHI shapes before any download is allowed.' },
  { key: 'auditor_scout', label: 'Auditing + research', blurb: 'Auditor scores completeness against the classification while Scout compiles the competitor landscape.' },
  { key: 'ledger',      label: 'Comparative benchmark', blurb: 'Ledger.Compare (deltas per competitor) + Ledger.Aggregate (headline + recommendations) build the benchmark report.' },
  { key: 'herald',      label: 'Drafting manuscript', blurb: 'Herald.Abstract (title + methods + refs) + Herald.Sections (results + discussion + limitations) draft the publication.' },
  { key: 'complete',    label: 'Ready to share', blurb: 'Safe-to-share bundle passes Publish Guard clean; attestation is signed; download below.' },
];

export function phaseIndexFromEvents(events, status) {
  if (status === 'complete' || status === 'partially_complete') return PHASES.length - 1;
  if (status === 'cancelled' || status === 'failed') return -1;
  // Walk events newest-first, match the phase prefix.
  for (let i = (events || []).length - 1; i >= 0; i--) {
    const p = events[i]?.phase || '';
    for (let k = PHASES.length - 1; k >= 0; k--) {
      if (p === PHASES[k].key || p.startsWith(PHASES[k].key)) return k;
    }
  }
  return 0;
}
