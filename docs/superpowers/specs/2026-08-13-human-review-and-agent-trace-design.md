# Human review redesign and agent trace transparency

Status: approved by Sir in conversation on 2026-08-13, pending written-spec review before
implementation planning.

## Context

Two related changes to the same review surface (`SessionDetail.jsx` and the pipeline that
feeds it):

1. The human review flow (`awaiting_human_review`, `POST /api/sessions/{sid}/human-review`)
   becomes conversational: the pipeline states its uncertainty in plain language, and a human
   can approve it, redirect it with a free-text comment that an agent interprets into a
   decision, or set it aside for a later pass without blocking the columns that are already
   resolved.
2. The agent trace (`AgentTracePanel`) becomes a three-tier transparency surface: a live
   one-line narration of what's happening right now, a hidden-by-default full trace, and
   per-call detail with no truncation and explicit parent/child linkage between calls.

Both changes touch the same page and the same underlying `AgentMessage` / decision-dict
plumbing, so they are specified together.

## Non-negotiable invariant (unchanged)

Restated from `memory/GOAL.md`, binding on every part of this design without exception:

> Dataset row values never reach a model at all. Only column HEADERS are ever placed in an
> LLM prompt.

Every mechanism below, including the new comment-interpretation call to Judge, operates only
on column headers, dictionary descriptions, prior AI-suggested actions, and the reviewer's own
comment text. None of it is ever given a dataset cell value. The literal `Manager` class
(`backend/phi_core/agents/manager.py`) keeps its existing, separate isolation: it supervises
call health (retry, timeout, escalation) and is never given a prompt, reply, decision, column
name, or file, per its current docstring. Nothing in this design changes that boundary.

## Part 1: Conversational human review

### Decision dict additions (`backend/phi_core/agents/reasoning.py`)

When Judge or Sentinel would coerce a decision to `human_review`, capture what it was about to
do before the coercion overwrites `action`:

- `suggested_action`, `suggested_confidence`, `suggested_reason`: the proposal that was about
  to be applied. This is what "Approve" applies directly.
- `reviewer_prompt`: a templated sentence built in Python from column name, dictionary
  description, `suggested_action`, and `suggested_reason` (no new LLM call). Example: "I think
  `site_of_disease` is TB-diagnosis data and should be kept because the dictionary describes it
  as a clinical field. Does that match your read?"
- `needs_file_glance`: true only for free-text/narrative columns, derived from the same
  dictionary-`type`/header-pattern signal Lexicon already uses, never from cell values.
- Every `reviewer_prompt` links to the exact agent call that produced the uncertainty, reusing
  the deep-link mechanism that already exists in `AgentTracePanel` (`#trace-{agent}`, auto-expand
  and scroll on load). Clicking "why do you think that" on a review row lands the reviewer
  directly on the cited reasoning in tier 3, rather than the two features staying disconnected.

### Three resolution modes

`HumanReviewSubmit.resolutions[]` gains a `mode: Literal["approve", "comment", "defer"]` and an
optional per-row `comment: str`. `action` becomes optional; the server fills it in depending on
mode.

- Approve: apply `suggested_action` directly. No extra input required.
- Comment: the reviewer's free text is the input to a fresh call to Judge for that one column
  only (see below). The interpreted action is applied, not the comment text itself.
- Defer: no action this round. The column is excluded from this export and added to
  `session["pending_review"]`, a persistent list the reviewer can return to later.

### Comment-driven inference (reuses Judge, not a new interpreter)

A new function in `reasoning.py`, `judge_resolve_with_comment(column_ctx, comment)`, re-invokes
Judge with `{column header, dictionary description, suggested_action, suggested_reason,
reviewer's comment}` and returns a fresh `{action, reason, confidence}`. This output passes
through every guardrail every other Judge decision already passes through:

- `validate_decisions` coerces anything outside the executable action vocabulary back to
  `human_review` rather than applying it.
- `apply_sentinel_hard_rules` force-corrects obvious direct identifiers regardless of what the
  comment said.
- `verify_keep_decisions` re-checks any resulting `keep` against the real cell values
  in-process before it is allowed to stand.

The `Manager` class supervises this specific call the same way it supervises every other agent
call (`run_supervised`: retry, extend timeout, escalate on repeated failure) without ever
seeing the column name, the comment, or the decision.

### Confidence gate

Per Sir's decision: apply the interpreted action directly only when Judge's own confidence
score is high. Reuse the threshold already established in the Judge prompt for the identical
purpose (`reasoning.py:302`, `"Route to human_review whenever confidence < 0.60"`) rather than
inventing a new number. Below 0.60, do not apply anything: return to the reviewer with a
restated understanding as a one-click confirmation ("You said '...' -- I read that as: drop
this column. Confirm?"), which itself resolves via Approve. This directly satisfies "rank
everything based upon confidence, the one with the highest confidence must pass through."

### Provenance

Every applied decision already carries a "decided by" concept, surfaced today as a column in
the benchmark report. Extend its values: `ai_model` (no human involved), `human_explicit_action`
(Approve), `human_comment_inferred` (Comment, confidence >= 0.60), and record both the literal
comment text and Judge's interpreted `{action, reason, confidence}` on the decision for the
audit trail, so a later reviewer or IRB auditor can see exactly what happened, not a blurred
version of it.

### Partial export

Submit resolves every flagged row to approve, comment, or defer, never leaves anything
ambiguously still `human_review`. If any rows are deferred: Executor runs on the non-deferred
decisions only, and the deferred columns are omitted entirely from the written export (skipped
before reaching `_apply_action`, never given any transform, never defaulted). Session status
becomes `partially_complete`. The export manifest and README list the withheld columns by name.
Resuming later (same `POST /api/sessions/{sid}/human-review` endpoint, now presenting the
`pending_review` queue) re-runs Executor from the original uploaded files over the full decision
set, original plus newly resolved, producing one fresh complete bundle that supersedes the
partial one. No patching of an already-exported file.

`partially_complete` is added to `_LIVE_STATUSES` (blocks re-intake until resolved) and
`_SETTLED_STATUSES` (no new SSE subscribers; the session is not mid-pipeline, it is waiting on
a person).

### Retiring the masked preview

`GET /api/sessions/{sid}/preview` and its masked-value spot-check panel are removed. Backend
code stops reading dataset cell values for review purposes entirely. In their place: a new
`GET /api/sessions/{sid}/dataset-file/{file_id}` streams the original uploaded file byte for
byte, no CSV/XLSX parsing in that code path, so there is no code path left that reads a cell
value on behalf of a human reviewer. The reviewer downloads and opens it in their own tool. The
"I have reviewed the row-level sample" checkbox is replaced with "I have opened and reviewed the
original file." This applies to `datasets/*.csv|xlsx` only; dictionary and form text continue
to be read in-process for prompts exactly as today (`scrub_for_prompt`), unchanged and out of
scope.

## Part 2: Three-tier agent trace

### Tier 1: live narration, persists as history

A one-line status feed, always visible, no click required: "Reading the dictionary file" then
"Statute is researching HIPAA guidance online" then "Judge is deciding `ssn`" then "Applying
transforms to `enrollment.csv`." Per Sir's decision, following LangSmith's model where a trace
is always a permanent record: this list never clears when the run finishes, it simply stops
receiving new lines, becoming a plain-language scrollback of what happened.

Each of the roughly dozen agent call sites (Lexicon, Schema, Instrument, Statute, Praxis,
Judge, Sentinel, Executor, Auditor, Scout, Ledger.Compare, Ledger.Aggregate, Herald.Abstract,
Herald.Sections) passes one explicit, short, human-authored `status_text` string at the call
site, not a string derived by parsing `phase`. A generic fallback ("{agent} is working")
applies wherever a call site doesn't set one, so nothing breaks if a call site is missed.

### Tier 2: hidden-by-default full trace

`AgentTracePanel` is wrapped in a single collapsed-by-default toggle instead of being always
rendered on the page. One click opens the same grouped-by-agent-and-phase list that exists
today.

### Tier 3: per-row detail, uncapped, tree-linked

`AgentMessage` gains `parent_id: str | None`, the id of the call that triggered this one (e.g.
Judge's decision call as the parent of the specific Sentinel review it provoked), following the
LangSmith `run_id`/`parent_run_id` pattern. The frontend renders child rows indented under their
parent instead of a flat chronological list.

`prompt_preview`/`reply_preview` are renamed to `prompt_text`/`reply_text` and always store the
full, untruncated text; the `[:400]` truncation in `agents/base.py` and the `audit_prompts`
gate on `prompt_full` are removed. This text is already scrubbed of cell values before the
prompt is built (`scrub_for_prompt`), so persisting it in full introduces no new PHI exposure.

## Motion and interaction craft

Reviewed against the `high-end-visual-design` skill on Sir's request. Verdict: this product's
existing identity (`tailwind.config.js`: `borderRadius: 0` globally, hairline `.rule-top`/
`.rule-bottom` dividers, no shadows, no blur, no icon library, Fraunces/Inter/JetBrains Mono)
is a deliberate clinical/editorial register that the skill's glass-and-bento agency aesthetic
would actively work against for a compliance console read by IRB reviewers and auditors. None
of the skill's vibe or layout archetypes (Ethereal Glass, Editorial Luxury, Soft Structuralism,
Bento, Z-Axis Cascade) are adopted. Its motion-physics and performance discipline is, scoped to
the new elements this design introduces:

- Tier-2 collapse toggle: expand/collapse via the `grid-template-rows` `0fr` -> `1fr` technique
  with `overflow-hidden`, not by animating `height` directly, on a custom cubic-bezier matching
  the existing `step-in` curve (`cubic-bezier(0.2, 0.7, 0.2, 1)`) rather than default
  `linear`/`ease-in-out`.
- Tier-1 narration strip: each new line enters via `transform`/`opacity` only (a small fade with
  slight upward translate), triggered by SSE message arrival, not scroll. `IntersectionObserver`
  does not apply here since nothing is scroll-revealed on this page; it is reserved for the
  general rule of never using a raw `scroll` listener, which this design does not need anywhere.
- Deferred-row regrouping into the "Set aside" list, and the low-confidence confirmation prompt
  swapping in for a comment row: `transform`/`opacity` only, same custom easing curve, restrained
  amplitude. No spring/bounce/magnetic-hover choreography from Section 5B; that reads as
  consumer-app playfulness inconsistent with this product's tone and is not adopted.
- No new `backdrop-blur`, no new `z-[9999]`-style arbitrary stacking; this page has no fixed or
  sticky chrome to justify blur, and none is introduced.

## Risk mitigations, summary

| Risk | Mitigation |
|---|---|
| Vague comment silently treated as a firm decision | Confidence gate at 0.60 (existing threshold, reused); below it, confirm-my-read loop instead of auto-apply |
| Audit trail blurs human vs. AI authorship | `decided_by` provenance enum extended; literal comment and interpreted decision both recorded |
| Prompt injection via the comment field | Not applicable; the author is the authenticated reviewer, same trust level as a direct dropdown pick |
| Comment doesn't map to any valid action | Existing `validate_decisions` coercion to `human_review`, no crash, no silent default |
| New synchronous LLM call can fail or time out | `Manager.run_supervised` retry/timeout supervision, scoped to call health only, never to content |

## Out of scope

- Dictionary and form text handling: unchanged.
- The `Manager` class's content isolation: unchanged.
- Any change to jurisdictions other than the existing `us` pack.
- Any change to the corpus generator (`phi_corpus/`).

## Testing plan

- Unit: decision dict carries `suggested_action`/`reviewer_prompt`/`needs_file_glance`
  correctly when coerced to `human_review`.
- Unit: `judge_resolve_with_comment` output passes through `validate_decisions`,
  `apply_sentinel_hard_rules`, `verify_keep_decisions` identically to a normal Judge decision.
- Unit: confidence < 0.60 from comment interpretation produces a confirmation prompt, not an
  applied action.
- Unit: Executor omits `deferred` columns entirely from written output, never reaches
  `_apply_action` for them.
- Integration: mixed approve/comment/defer submission reaches `partially_complete`, export
  bundle excludes deferred columns, manifest lists them.
- Integration: resuming a `partially_complete` session and resolving the remainder reaches
  `complete` with a fresh full bundle.
- Integration: `GET /api/sessions/{sid}/dataset-file/{file_id}` returns byte-identical content
  to the original upload; a structurally corrupted CSV still downloads cleanly, proving the
  path never parses it.
- Frontend: updated `SessionDetail.jsx` tests for the three-button row, the deferred-set-aside
  grouping, the `partially_complete` banner, and the tier-1/tier-2 trace toggle.

## Files touched

- `backend/phi_core/agents/base.py`: `AgentMessage` fields (`parent_id`, `status_text`,
  `prompt_text`, `reply_text`); remove truncation and `audit_prompts` gating.
- `backend/phi_core/agents/reasoning.py`: suggested-action capture, `reviewer_prompt` template,
  `needs_file_glance` heuristic, `judge_resolve_with_comment`, Executor column omission for
  `deferred`.
- `backend/phi_core/agents/specialists.py`, `outward.py`: `status_text` at each call site.
- `backend/server.py`: `HumanReviewSubmit` model, `session_human_review` handler restructure,
  new `dataset-file` download endpoint, removal of the `preview` endpoint, `partially_complete`
  status wiring.
- `frontend/src/pages/SessionDetail.jsx`: three-button review rows, deferred queue UI,
  `partially_complete` banner, tier-1 narration strip, tier-2 collapse toggle, tier-3 tree
  rendering and uncapped text.
