# Human review redesign and agent trace transparency

Status: revised after adversarial review (two independent fresh-context passes against the
actual codebase, 2026-08-13). The prior version made several claims about existing code that
turned out to be false; this version names the real mechanisms and the fixes each one needs.
Pending Sir's read before implementation planning.

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

Every mechanism below operates only on column headers, dictionary descriptions, prior
AI-suggested actions, and the reviewer's own comment text -- never a dataset cell value. This
now explicitly includes the comment itself: the reviewer holds the actual file (see "Retiring
the masked preview" below), so "paste what you're looking at" is the expected failure mode, not
an edge case, and the comment is scrubbed exactly like dictionary/form text before it goes
anywhere near a prompt (see "Comment scrubbing"). The literal `Manager` class
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
  description, `suggested_action`, and `suggested_reason` (no new LLM call).
- `needs_file_glance`: true only for free-text/narrative columns. The prior version cited "the
  same signal Lexicon already uses" -- that signal does not exist; Lexicon's output contract
  (`specialists.py:31-36`, `{name, description, phi_flag_hint, clinical_utility, notes}`) has no
  type or header-pattern field. The real, implementable source is the free-text column-name
  list already in Sentinel's hard-rule table (`notes`, `comments`, `remarks`, `observations`,
  `free_text`, `reasoning.py`'s hard-rule row) plus Judge's own prompt instruction listing
  narrative fields (`reasoning.py:300-302`). When no dictionary file was uploaded at all (legal
  input -- `GOAL.md` requires only `datasets/` plus one of `forms/` or `dictionary/`),
  `needs_file_glance` falls back to the header-name list alone.

### Agent-trace link (scoped down from the prior claim)

`reviewer_prompt` links to the flagged agent's row in tier 2, reusing the existing
`#trace-{agent}` anchor. This places the reviewer at the right agent's expanded panel, not a
scroll-perfect single call -- today's grouping key (`agent::phase`) collapses every iteration of
an agent's calls into one row, and the id `trace-${agent}` is duplicated across groups sharing
an agent, so `getElementById` resolves to whichever one exists first in the DOM
(`SessionDetail.jsx:200-214,259`). True per-call precision requires the unique per-message
anchors specified under Part 2 tier 3 (`parent_id` plus unique row ids), not a reuse of the
existing mechanism. This spec no longer claims the deep-link lands on "the exact agent call."

### Three resolution modes

`HumanReviewSubmit.resolutions[]` gains a `mode: Literal["approve", "comment", "defer"]` and an
optional per-row `comment: str`. `action` becomes optional; the server fills it in depending on
mode.

- Approve: apply `suggested_action` directly. No extra input required.
- Comment: the reviewer's free text is the input to a fresh call to Judge for that one column
  only (see below). The interpreted action is applied, not the comment text itself.
- Defer: no action this round. The column is excluded from this export and added to
  `session["pending_review"]`, a persistent list the reviewer can return to later.

A fourth internal state, the low-confidence confirmation (see "Confidence gate"), is not a
fourth mode from the caller's perspective -- it is what a `comment` resolves to procedurally when
Judge's interpretation isn't confident enough to apply outright, and it itself resolves back to
approve or a fresh comment.

### Comment scrubbing (new)

Before `judge_resolve_with_comment` builds its prompt, the reviewer's comment passes through
`scrub_for_prompt` exactly like dictionary and form text does today (`specialists.py:44,124`).
Before persistence, `reviewer_comment` is added to `scrub_decision`'s field list
(`phi_core/security.py:253-256`, currently `reason, citation, notes, evidence` only) and
independently run through `scrub_persisted_text`. This closes the gap where a reviewer, now
holding the actual file per this same design, could paste a literal cell value into the comment
box and have it reach a model, or an at-rest log, unredacted. Verified against the current code:
`server.py:1864` writes `d["reviewer_comment"] = body.comment` with no scrub step today.

### Comment-driven inference (reuses Judge, closes a pre-existing gap)

A new function in `reasoning.py`, `judge_resolve_with_comment(column_ctx, comment)`, re-invokes
Judge with `{column header, dictionary description, suggested_action, suggested_reason, scrubbed
comment}` and returns a fresh `{action, reason, confidence}`.

Verified against the code: `apply_sentinel_hard_rules` and `verify_keep_decisions` are called
today only from `orchestrator.py`'s Judge/Sentinel loop and from `phi_corpus/replay.py` --
`session_human_review` never calls either. This is a pre-existing gap in the human-review
endpoint, not something this design can assume is already covered. This design closes it:
`session_human_review` gains explicit calls to `apply_sentinel_hard_rules` and
`verify_keep_decisions` over the full resolved decision set (approve, comment-derived, and any
explicit picks) before anything reaches Executor, mirroring the orchestrator's own sequence.

What this does NOT add: Sentinel's full adversarial LLM cross-check is not re-run per column
(cost and latency for a single-column resolution). The deterministic hard-rule table and
keep-verification are the floor every path gets; the risk table below states this honestly
rather than claiming full parity with the orchestrator's Sentinel loop.

The `Manager` class supervises this specific call the same way it supervises every other agent
call (`run_supervised`: retry, extend timeout, escalate on repeated failure) without ever
seeing the column name, the comment, or the decision.

### Handling a keep-verification demotion (new)

If `verify_keep_decisions` demotes a comment-derived or approved "keep" back to `human_review`
(unreadable dataset file, or a real Safe-Harbor violation past the hard-rule table), the column
returns to the reviewer's queue with the demotion's stated reason surfaced verbatim, not a
silent resubmit loop. A demoted column joins the deferred set procedurally (see "Partial
export") rather than blocking the columns that resolved cleanly in the same submission.

### Confidence gate

Apply the interpreted action directly only when Judge's own confidence score is high, reusing
the numeric threshold already established for column-classification confidence in Judge's
prompt (`reasoning.py:302`, "confidence < 0.60"). That threshold is prose inside Judge's
`PROMPT` -- a model instruction, not a code-enforced gate today -- and it was set for a
structurally different judgment (classifying a column) than the one this design adds (parsing a
human's instruction). Reusing the same number is a deliberate choice for consistency, not a
proven equivalence: a model asked to interpret a clear instruction tends to self-report high
confidence, so whether this gate fires as often as intended needs to be measured (see Testing
plan), not assumed.

Below 0.60, do not apply anything. `judge_resolve_with_comment`'s interpreted `{action, reason,
confidence}` is stored on the decision as `pending_confirmation`, and the reviewer sees a
one-click confirmation ("You said '...' -- I read that as: drop this column. Confirm?").
Confirming applies `pending_confirmation.action`, not `suggested_action` -- the two are different
values (the AI's original pre-comment guess versus what the comment was just interpreted as),
and conflating them would silently apply the wrong action on the one path the confidence gate
exists to protect. Confirming is recorded as `human_comment_inferred`, the same provenance value
a high-confidence comment gets.

### Provenance

Every applied decision already carries a "decided by" concept, surfaced today as a column in
the benchmark report. Extend its values to four: `ai_model` (no human involved),
`human_explicit_action` (Approve), `human_comment_inferred` (Comment or its confirmation),
`human_overridden_by_hard_rule` (when `apply_sentinel_hard_rules` rewrites a human-sourced
decision -- the hard-rule table force-corrects obvious direct identifiers regardless of what a
human said, per `reasoning.py:154-186`, and its rewrite also raises confidence to at least 0.95,
which would otherwise misread as high AI confidence on a row the human actually disagreed with).
When an override happens, the human's original choice is retained alongside the overriding one
so the audit trail shows both what the person wanted and what the deterministic rule enforced.

### Partial export

Submit resolves every flagged row to approve, comment, or defer. If any rows are deferred:

- Deferred decisions are filtered out of the list passed to Executor and kept in
  `session["pending_review"]`. `Executor.run` raises on any decision still tagged
  `human_review` in its input (`reasoning.py:392-395`); simply leaving a deferred row unresolved
  in that list would abort the entire run, not just skip that column.
- `apply_column_actions_to_dataset`'s existing SEC-004 fail-closed default
  (`reasoning.py:606-616`) treats any dataset column absent from the decisions list as an
  implicit `drop` and writes it through `_apply_action` -- exactly the silent default this
  design forbids. Deferred columns are therefore handled by a new explicit omission path: column
  names to exclude entirely from output, implemented as fieldname filtering in the CSV writer
  and `ws.delete_cols` in the openpyxl branch, never routed through the decisions list at all.
- Publish Guard still runs on the resulting partial export before status becomes
  `partially_complete`; `scan_all_exports` is not skipped. Degenerate case: if every column of a
  dataset is deferred, that file has nothing left to scan (`scan_all_exports` only certifies
  clean when `scanned > 0`). A dataset reduced to zero surviving columns is excluded from the
  bundle and noted in the manifest as fully deferred, rather than handed to the scanner, so this
  case does not fall into the existing `blocked` status by accident.
- `human_review_required` reflects the true `pending_review` state (today set unconditionally
  `False` on resume, `server.py:1919`) -- it must be `true` whenever `pending_review` is
  non-empty, so `/results` correctly reports a session still waiting on a person.
- The signed attestation (`bundle.py`, Ed25519-signed) explicitly lists withheld column names in
  its own payload, not only the human-readable manifest/README -- a partial bundle's
  cryptographic attestation must not be indistinguishable from a complete run's once the README
  is separated from it.
- `actual_knowledge_ack`'s bound statement ("no actual knowledge the remaining information could
  identify someone") does not apply to columns a submission explicitly defers -- the reviewer
  has not made that determination for those. A submission containing any deferral uses narrower
  attestation language scoped to the columns actually resolved this round; the full statement is
  required only on a submission with zero deferrals.
- `session_review` becomes an append-only list (reviewer, comment, resolved columns, timestamp
  per submission) instead of a single overwritten dict (today: `server.py:1879,1971`), so a
  two-pass review retains both reviewers' identities and comments distinctly.

Resuming (same `POST /api/sessions/{sid}/human-review` endpoint, presenting the `pending_review`
queue) re-runs Executor from the original uploaded files over the full decision set, original
plus newly resolved, producing one fresh complete bundle that supersedes the partial one. This
requires the original files to still exist: `cleanup_session_unpacked` is called today at every
existing terminal-status transition (`orchestrator.py:367,433`, `server.py:1746,1763,1959,
1981,1991`) and would otherwise delete them before a resume could run. `partially_complete` is
explicitly excluded from every one of those cleanup call sites.

Retention: because deferral is the case where a reviewer may not return promptly, this is a
deliberate trade-off, not an oversight -- `partially_complete` sessions are added to the
existing retention purge query (`server.py`'s `$in` list, currently `complete, failed,
cancelled, blocked, intake_failed`) with the same `RETENTION_DAYS` grace period as any other
settled session. A deferred session does not get indefinite retention by omission; after the
grace period it is purged along with its uploaded PHI, same as any abandoned session. The
alternative (exempt indefinitely) accumulates raw PHI on disk with no expiry, which is worse.
Sir should confirm this grace-period choice explicitly before implementation.

`partially_complete` is wired into six named call sites, not two: `_LIVE_STATUSES` (local to
`session_intake`, blocks re-intake, `server.py:560`), `_SETTLED_STATUSES` (blocks new SSE
subscribers, `server.py:257-258`), the human-review resume filter (`server.py:1844`'s
`review_filter`, currently hardcoded to `awaiting_human_review` alone), the bundle-download gate
(`server.py:797`, currently hardcoded to `complete` alone), the export-download gate
(`server.py:839`, same), the cancel-endpoint's settled-status tuple, and the frontend's
`isComplete`/`isPending` derivation and download-button gating (`SessionDetail.jsx:619-621,
715-719`). Every one is named here so implementation doesn't silently miss one and strand a
`partially_complete` session with no way to download or resume.

### Retiring the masked preview

`GET /api/sessions/{sid}/preview` and its masked-value spot-check panel are removed. In their
place: `GET /api/sessions/{sid}/dataset-file/{file_id}` streams the original uploaded file byte
for byte, no CSV/XLSX parsing in that code path. `file_id` resolves only against this session's
own `session["files"]` entries -- never a path-joined URL segment -- reusing the same
`_owned_session` + stored-path-lookup pattern the existing export endpoint already follows
(`server.py:838-851`) and `safe_join` for the actual filesystem access; this is the single
highest-stakes new egress path in the design (unredacted PHI, no Publish Guard in front of it)
and is held to the same owner-scoping every other download endpoint already has, not less. Each
download is recorded on the session (principal, timestamp) so the "I have opened and reviewed
the original file" checkbox has a server-side fact behind it.

This is a deliberate trade-off, stated plainly rather than presented as a pure win: replacing
five masked sample cells with the entire unredacted original file on the reviewer's own
workstation is a larger raw-PHI exposure surface than what it replaces, traded for removing all
backend code that reads a cell value on a human's behalf. This applies to `datasets/*.csv|xlsx`
only; dictionary and form text continue to be read in-process for prompts exactly as today
(`scrub_for_prompt`), unchanged and out of scope.

## Part 2: Three-tier agent trace

### Tier 1: live narration, persists as history

A one-line status feed, always visible, no click required, sourced from an explicit per-call-site
`status_text` string. A generic fallback (`"{agent} is working"`) applies wherever a call site
doesn't set one. This list never clears when the run finishes; it becomes a plain-language
scrollback of what happened.

Transport gap this design closes: `status_text` (and tier 3's `parent_id`) do not reach the
browser today by simply adding the field to `AgentMessage`. Both existing SSE adapters
hand-build a fixed-field `ProgressEvent` payload (`server.py:1686-1692` for a live run, and a
second, already-divergent adapter for the resume path at `server.py:1935-1940` that currently
omits `duration_ms` the first one includes) -- an unlisted field is silently dropped. Both
adapters' field lists are updated to include `status_text` and `parent_id`, reconciling the
existing duration_ms discrepancy between them at the same time.

### Tier 2: hidden-by-default full trace

`AgentTracePanel` is wrapped in a single collapsed-by-default toggle instead of being always
rendered on the page.

### Tier 3: per-row detail, uncapped, tree-linked

`AgentMessage` gains `parent_id: str | None`. This cannot be an ambient "current call" pointer:
the pipeline runs calls concurrently (Praxis fans out under one `asyncio.gather`, Statute runs
alongside the specialists), so an ambient pointer would attribute children to whichever call
happened to start last under interleaved awaits, producing a plausible but false causal tree in
the surface whose entire purpose is auditability. `parent_id` is threaded explicitly as a
parameter through `call`/`call_json`/`call_with_web_search` and set explicitly at each call
site. Sentinel reviews the whole decision list once per iteration, not once per column, so the
parent/child relationship this design captures is iteration-level (a Judge iteration as parent
of the Sentinel review it provoked), not per-decision.

Transport and cost: the existing agent-trace endpoint defaults to a 200-row cap with no
pagination or cursor (`server.py:2005-2012`; the frontend requests 500), and the frontend's SSE
handler currently refetches the entire trace on every single SSE message
(`SessionDetail.jsx:607`). Storing full untruncated prompt/reply text without addressing either
of these turns each refetch into a multi-megabyte response, repeated per message, per connected
viewer (up to four per session). This design adds a cursor/pagination parameter to the trace
endpoint and changes the SSE handler to append incrementally rather than refetch the full
history. A row whose parent falls outside the fetched window renders at the top level rather
than being dropped or mis-nested.

`prompt_preview`/`reply_preview` are renamed to `prompt_text`/`reply_text` and always store the
full, untruncated text; the `[:400]` truncation in `agents/base.py` and the `audit_prompts` gate
on `prompt_full` are removed. This reopens a known, already-recorded risk rather than
introducing a new one: `memory/PRD.md` records SEC-006 (models echoing raw identifiers into
`reason`/`prompt_preview`/`reply_preview`) as accepted-low specifically because the exposure
window was small (400 characters, mitigated at read time by `scrub_nested`). Removing the cap
without a corresponding write-time scrub would widen an already-imperfect mitigation, and the
full agent log is included in the publication bundle for corpus runs where Publish Guard does
not scan it (Publish Guard scans `export_paths` only). This design adds a write-time
`scrub_for_prompt` pass over stored `prompt_text`/`reply_text` before persistence, mirroring
what already happens for dictionary/form text, closing the gap rather than widening it.

Renaming the fields is not cosmetic: `phi_corpus/benchmark.py:132` reads exactly
`prompt_full`/`prompt_preview` to compute `prompts_audited` and `literals_found_in_prompts`,
the metric `ARCHITECTURE.md` cites as enforcement evidence for the no-raw-identifier invariant.
Left unchanged, the rename would silently zero that metric (0 audited, 0 found -- the same
false-clean pattern `GOAL.md` forbids elsewhere). `phi_corpus/benchmark.py`'s reader is updated
to the new field names as part of this same change; "out of scope: the corpus generator" (below)
means no functional change to corpus generation itself, not that this rename is exempt from
updating what already consumes the renamed fields.

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
  slight upward translate), triggered by SSE message arrival, not scroll.
- Deferred-row regrouping into the "Set aside" list, and the low-confidence confirmation prompt
  swapping in for a comment row: `transform`/`opacity` only, same custom easing curve, restrained
  amplitude. No spring/bounce/magnetic-hover choreography; that reads as consumer-app
  playfulness inconsistent with this product's tone and is not adopted.
- No new `backdrop-blur`, no new `z-[9999]`-style arbitrary stacking; this page has no fixed or
  sticky chrome to justify blur, and none is introduced.

## Risk mitigations, summary

| Risk | Mitigation |
|---|---|
| Reviewer comment carries a literal cell value into a prompt or at-rest log | `scrub_for_prompt` before the Judge call; `reviewer_comment` added to `scrub_decision`'s field list plus `scrub_persisted_text` before storage |
| Vague comment silently treated as a firm decision | Confidence gate at 0.60 (reused threshold, explicitly flagged as needing empirical validation); below it, `pending_confirmation` holds the interpreted action separately from `suggested_action` until the reviewer confirms |
| `session_human_review` bypasses Sentinel's hard-rule and keep-verification guardrails | Both are now called explicitly over the full resolved decision set before Executor; full per-column Sentinel LLM cross-check is not re-run (cost/latency), stated honestly rather than claimed as full parity |
| Prompt injection via the comment field | Partially mitigated by downstream deterministic checks (hard rules, keep-verification) now that they're actually wired in; not "not applicable," since Sentinel's full adversarial loop still isn't re-run per column |
| Deferred columns silently defaulted (dropped-and-blanked) by the existing writer | New explicit column-omission path in the CSV/XLSX writers; deferred decisions never reach the decisions list Executor sees |
| `partially_complete` is an unreachable dead end (no download, no resume) | Six call sites named and updated explicitly: both status sets, the resume filter, both download gates, the cancel tuple, and frontend status branching |
| Resume has no source files to re-run Executor against | `cleanup_session_unpacked` explicitly excluded for `partially_complete`; retention purge query updated instead of relying on indefinite exemption |
| Full prompt/reply persistence widens an already-accepted-low residual (SEC-006) | Write-time `scrub_for_prompt` pass added on `prompt_text`/`reply_text`, not just relying on existing read-time `scrub_nested` |
| Field rename silently zeroes the corpus benchmark's leak-audit metric | `phi_corpus/benchmark.py`'s field-name consumption updated in the same change |
| Raw-file download endpoint is the highest-stakes new egress path | Owner-scoped `file_id` resolution via `session["files"]`, same pattern as the existing export endpoint; each download recorded on the session |
| `parent_id` misattributes causality under concurrent agent calls | Threaded explicitly through every call site, not an ambient pointer |
| Provenance can't represent a human decision overridden by a hard rule | Fourth `decided_by` value, human's original choice retained alongside the override |
| Trace transport cost balloons once prompts/replies are stored in full | Cursor/pagination on the trace endpoint; SSE handler appends incrementally instead of refetching the full history per message |

## Out of scope

- Dictionary and form text handling: unchanged.
- The `Manager` class's content isolation: unchanged.
- Any change to jurisdictions other than the existing `us` pack.
- No functional change to corpus generation itself; `phi_corpus/benchmark.py`'s consumption of
  the renamed trace fields is updated as part of this change, not exempted by this line.

## Testing plan

- Unit: decision dict carries `suggested_action`/`reviewer_prompt`/`needs_file_glance`/
  `pending_confirmation` correctly through coercion and confirmation.
- Unit: comment text passes through `scrub_for_prompt` before the Judge call and through
  `scrub_decision`/`scrub_persisted_text` before storage; a comment containing a synthetic name/
  SSN/phone pattern is redacted in both places.
- Unit: `session_human_review`'s resolved decision set passes through `apply_sentinel_hard_rules`
  and `verify_keep_decisions` identically to the orchestrator's own sequence.
- Unit: a keep-verification demotion surfaces its reason to the reviewer and joins the deferred
  set rather than blocking or silently resubmitting.
- Unit: Executor never receives a `deferred` decision; the CSV/XLSX writers omit deferred columns
  entirely rather than applying SEC-004's default.
- Integration: mixed approve/comment/defer submission reaches `partially_complete`, export
  bundle excludes deferred columns, manifest and signed attestation both list them.
- Integration: an all-columns-deferred submission for a single-dataset study does not fall into
  `blocked`.
- Integration: resuming a `partially_complete` session finds the original files still present
  (cleanup skipped) and reaches `complete` with a fresh full bundle; `session_review` retains
  both passes' reviewer identities.
- Integration: a session left in `partially_complete` past `RETENTION_DAYS` is purged by the
  existing retention job.
- Integration: `GET /api/sessions/{sid}/dataset-file/{file_id}` returns byte-identical content to
  the original upload, refuses a `file_id` not present in the session's own `files` list, and
  records the download.
- Empirical: measure how often the 0.60 confidence gate actually fires against a sample of real
  free-text comments, rather than assuming the reused threshold behaves the same as it does for
  column classification.
- Frontend: updated `SessionDetail.jsx` tests for the three-button row, the deferred-set-aside
  grouping, the `partially_complete` banner and download path, and the tier-1/tier-2 trace
  toggle receiving live `status_text`.
- `phi_corpus/benchmark.py`: context-hygiene metric (`prompts_audited`, `literals_found_in_prompts`)
  still counts correctly after the field rename.

## Files touched

- `backend/phi_core/agents/base.py`: `AgentMessage` fields (`parent_id`, `status_text`,
  `prompt_text`, `reply_text`); remove truncation and `audit_prompts` gating; write-time
  `scrub_for_prompt` pass before persisting prompt/reply text; `parent_id` threaded through
  `call`/`call_json`/`call_with_web_search`.
- `backend/phi_core/agents/reasoning.py`: suggested-action capture, `reviewer_prompt` template,
  `needs_file_glance` heuristic (Sentinel hard-rule list / Judge prompt text, not a nonexistent
  Lexicon signal), `judge_resolve_with_comment`, `pending_confirmation` handling, provenance
  enum's fourth value, explicit column-omission path in `apply_column_actions_to_dataset`'s
  CSV/XLSX writers.
- `backend/phi_core/security.py`: `reviewer_comment` added to `scrub_decision`'s field list.
- `backend/phi_core/agents/specialists.py`, `outward.py`: `status_text` at each call site.
- `backend/phi_core/paths.py` / retention job: `partially_complete` excluded from
  `cleanup_session_unpacked` call sites, included in the retention purge query.
- `backend/bundle.py`: attestation payload includes withheld column names; narrower
  `actual_knowledge_ack` language for submissions containing a deferral.
- `backend/server.py`: `HumanReviewSubmit` model, `session_human_review` handler restructure
  (guardrail calls, append-only `session_review`, `human_review_required` fix), new
  `dataset-file` download endpoint (owner-scoped, download-recorded), removal of the `preview`
  endpoint, `partially_complete` wired into all six named status gates, both `emit_msg` adapters'
  field lists reconciled and extended, agent-trace endpoint pagination/cursor.
- `phi_corpus/benchmark.py`: read the renamed `prompt_text`/`reply_text` fields.
- `frontend/src/pages/SessionDetail.jsx`: three-button review rows, deferred queue UI,
  `partially_complete` banner and download path, tier-1 narration strip (incremental SSE
  append, not full refetch), tier-2 collapse toggle, tier-3 tree rendering with dangling-parent
  handling and uncapped text.
