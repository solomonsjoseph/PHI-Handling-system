# Assurance source anchors

These anchors describe the verified baseline at `6a11cfae42f10faf26c186e25cae73ef9735a718`. They are updated when a phase changes the named control.

## Workflow and authority

| Area | Baseline source anchors | Verified behavior |
| --- | --- | --- |
| Fixed pipeline | `backend/phi_core/agents/orchestrator.py:96-766` | `run_pipeline` owns the fixed sequence; its per-session iteration cap clamps to `1..3` at 153-154. |
| Initial execution | `backend/server.py:1737-1891` | `session_handle` claims a session, hydrates headers, wraps the pipeline in a 900-second `wait_for`, and starts a detached task. |
| Human resume | `backend/server.py:1957-2508` | `session_human_review` contains a separate `_run_tail` from 2246-2508 with divergent gates and detached scheduling. |
| Manager | `backend/phi_core/agents/manager.py:129-336` | Bounded supervised retries and advisory consultation coexist with `escalate_to_human_review`, which has workflow authority. |
| Delegation | `backend/phi_core/agents/outward.py:95-141,198-240` | Ledger and Herald construct specialist subagents directly without a durable child-work record. |
| Cancellation | `backend/phi_core/agents/orchestrator.py:88-93,262,353,649,711,718` | Cancellation is a cooperative session flag. Scout is an unmanaged task at 544-545 and is not cancelled on every exit. |
| Startup recovery | `backend/server.py:1709-1732` | The 900-second orphan sweep marks older sessions failed without lease or checkpoint recovery. |

## Provider and tool control paths

| Area | Baseline source anchors | Verified behavior |
| --- | --- | --- |
| Provider call entry | `backend/phi_core/agents/base.py:111-267` | `Agent.call*` logs a separately scrubbed prompt while forwarding the raw prompt through supervised call closures. Web-search escalation defaults to allowed. |
| LiteLLM inference | `backend/phi_core/agents/llm.py:20-215` | `litellm.completion` occurs at 113 and 180. Native web search is at 113. ChatGPT and all other non-Anthropic research paths silently fall back to plain completion at 134-153. |
| Research provenance | `backend/phi_core/agents/experts.py:190-222,265-281,465-479` | Statute and Praxis treat a non-empty model-authored `sources` list as web-search provenance and cache it as such. |
| Cache research | `backend/phi_core/agents/outward.py:29-45`, `backend/phi_core/agents/cache.py:13` | Scout reads the seven-day cache before provider work. |
| Direct provider entry points | `backend/server.py:1121-1147,1506-1541,1545,1610` | Corpus research and warmup construct agents with shared db and LLM configuration, without run, task, grant, or owner identity. |

## Human transitions and state writes

| Area | Baseline source anchors | Verified behavior |
| --- | --- | --- |
| Session intake | `backend/server.py:569-673` | Intake creates and resets session state and files. |
| Initial claim and terminal updates | `backend/server.py:1737-1891` | `/handle` uses a session claim and writes terminal session state, then cleans unpacked input in failure paths. |
| Human review | `backend/server.py:1957-2246` | Review verifies a single session-level download record, concurrently interprets comments, auto-applies confidence >= 0.60, and reruns only selected gates. |
| Resume tail | `backend/server.py:2246-2508` | Tail writes reversal material, guard results, human-review state, and completion fields separately from initial handling. |
| Failure correlation | `backend/server.py:340-353` | A failure status update is followed by unconditional unpacked-tree cleanup outside the run filter. |
| Cancellation route | `backend/server.py:1895-1953` | `/cancel` sets only `cancel_requested`; work stops on later cooperative checks. |
| Session deletion | `backend/server.py:550-565` | Filesystem removal precedes `agent_log` and session deletion, with ignored filesystem errors. |
| Progress and SSE | `backend/server.py:243-338,721-766` | A session shares one queue among subscribers; progress is persisted with an unbounded push. |

## Artifact writes and cleanup

| Area | Baseline source anchors | Verified behavior |
| --- | --- | --- |
| Artifact roots | `backend/phi_core/paths.py:74-96` | Flat `EXPORT_DIR` and session unpacked-tree cleanup use `ignore_errors=True`. |
| Executor writes | `backend/phi_core/agents/reasoning.py:17,1065-1163,1479-1546` | Dataset and narrative output use flat export paths containing original filenames. Only dataset action writing uses temp-plus-`os.replace`. |
| Guard | `backend/phi_core/publish_guard.py:142-186,301-352` | Supported text and spreadsheet formats are scanned; unsupported formats block. Category A names are not scanned. |
| Bundle | `backend/phi_core/bundle.py:543-616` | A bundle includes exactly one clean guard match for each member and emits attestation material. |
| Reversal material | `backend/phi_core/crypto.py:31-124` | Fernet-only reversal encryption has no plaintext fallback. Pseudonym salt never leaves the process. |
| Retention | `backend/server.py:1667-1706` | Settled-session retention excludes awaiting human review and removes filesystem paths before database records, suppressing unlink errors. |

## Download checks

| Route | Source anchors | Baseline check |
| --- | --- | --- |
| `session_bundle` | `backend/server.py:800-838` | Requires terminal status and clean session guard, then builds a bundle. |
| `session_reversal_key` | `backend/server.py:844-877` | Requires guard checks but deletes the reversal blob before response delivery. |
| `session_export` | `backend/server.py:880-935` | Requires session and per-file guard; `force=true` records an override and bypasses blocking. No hash check precedes `FileResponse`. |
| `corpus_study_data_zip` | `backend/server.py:1061-1074` | Builds a pre-pipeline intake ZIP without session ownership or Publish Guard. |
| `corpus_study_zip` | `backend/server.py:1210-1223` | Serves corpus ZIP bytes without Publish Guard. |
| `corpus_study_benchmark_download` | `backend/server.py:1381-1395` | Builds a benchmark download from `agent_log`, without Publish Guard. |
| `session_dataset_file` | `backend/server.py:2551-2589` | Streams original bytes and records a non-versioned session download. |

## Existing deterministic spine

- `backend/phi_core/agents/reasoning.py:23-98,270-341,361-408,447-490,518-562,582-670,1065-1087,1172,1205-1216,1479-1546`: decision coercion, hard rules, age and cardinality handling, confidence and blocking floors, Executor's unresolved-review refusal, Auditor floor, and atomic dataset writes.
- `backend/phi_core/agents/orchestrator.py:153-154`: iteration cap clamp and effective bound.
- `backend/phi_core/agents/manager.py:129-133`: retry, timeout, note, lexicon, and per-agent bounds.
- `backend/phi_core/agents/base.py:25-28`: plain and research call timeouts.
- `backend/phi_core/agents/batching.py`: batch size, pool size, exactly-one-result contract, and cancellation behavior.
- `backend/phi_core/agents/operator.py:37-52` and `_verify_record`: fail-closed operator checks.
- `backend/phi_core/agents/reviewer.py`: coverage audit and export filtering.
- `backend/phi_core/security.py`, `backend/phi_core/intake.py`, `backend/phi_core/paths.py`: provider URL, persisted-text, intake traversal and resource, and path controls.
- `backend/server.py`: secure boot, owner-scoped access, actual-knowledge attestation, delivery record, atomic run claim, run-id fencing, rate limits, and security headers.
