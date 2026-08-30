// ---- Live agent trace ---------------------------------------------------
//
// The pipeline emits one AgentMessage per LLM call (direction='in' before
// the call, 'out' after, with duration_ms). Rendering these live gives the
// operator continuous feedback -- Sir's Q4: "the user must feel like the
// process is moving further instead of them looking at a loading screen".
//
// Design: group by (agent, phase_key) so a Judge iteration shows as one
// row that flips from 'running' (only in) to 'done' (has out) once the
// LLM returns. Duration is read straight from the 'out' message. We keep
// prompt/reply previews collapsible so operators can drill in to WHY an
// agent decided what it did, without cluttering the default view.
export function groupTrace(rawMessages) {
  const groups = new Map();
  const order = [];
  // Tier 3: message id -> the group key that message ended up in, so a
  // child group's `parent_id` (another message's id) can be resolved to
  // the PARENT GROUP once every message has been walked at least once.
  const idToKey = new Map();
  for (const m of rawMessages || []) {
    // Sir Q "praxis trace overwhelm" -- collapse the 17 per-category Praxis
    // rows into a single "Praxis · N methods" group. Every category still
    // lives in the expanded detail panel below; the top row just fires once.
    let key;
    if (m.agent === 'Praxis') {
      key = 'Praxis::praxis.all';
    } else {
      key = `${m.agent}::${m.phase}`;
    }
    if (!groups.has(key)) order.push(key);
    const existing = groups.get(key) || { key, agent: m.agent, phase: m.phase, ts: m.ts, parentMsgId: null };
    if (m.agent === 'Praxis') {
      // Override phase label with a per-group counter.
      existing.phase = 'praxis.methods';
      existing.praxis_count = (existing.praxis_count || 0) + 1;
      existing.praxis_categories = existing.praxis_categories || [];
      const catMatch = (m.phase || '').match(/:([A-Z]|[a-z_]+)$/);
      if (catMatch) existing.praxis_categories.push(catMatch[1]);
    }
    if (m.id) idToKey.set(m.id, key);
    if (m.parent_id && !existing.parentMsgId) existing.parentMsgId = m.parent_id;
    if (m.direction === 'in') {
      existing.prompt_text = m.payload?.prompt_text || existing.prompt_text;
      existing.tool = m.payload?.tool || existing.tool;
      existing.status_text = m.status_text || existing.status_text;
      existing.started = m.ts;
      existing.state = existing.state || 'running';
    } else if (m.direction === 'out') {
      existing.reply_text = m.payload?.reply_text || existing.reply_text;
      existing.error = m.payload?.error;
      // For collapsed Praxis, sum durations so "N s of LLM time" reflects total.
      if (m.agent === 'Praxis') {
        existing.duration_ms = (existing.duration_ms || 0) + (m.duration_ms || 0);
      } else {
        existing.duration_ms = m.duration_ms;
      }
      existing.state = m.payload?.error ? 'errored' : 'done';
      existing.ended = m.ts;
    } else if (m.direction === 'info') {
      existing.state = existing.state || 'info';
      existing.info = { ...(existing.info || {}), ...(m.payload || {}) };
    }
    groups.set(key, existing);
  }
  const list = order.map(k => groups.get(k));
  for (const g of list) {
    if (g.parentMsgId) {
      const parentKey = idToKey.get(g.parentMsgId);
      // A parent id pointing outside this fetched page, or at itself,
      // renders at the top level rather than being dropped or mis-nested.
      g.parentGroupKey = parentKey && parentKey !== g.key ? parentKey : null;
    } else {
      g.parentGroupKey = null;
    }
  }
  return list.sort((a, b) => (a.ts || '').localeCompare(b.ts || ''));
}
