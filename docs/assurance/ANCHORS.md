# Assurance source anchors

- Superseded pre-Phase-0 baseline description; refreshed at the close of Phase 9 to describe the current, hardened control plane.

## Workflow and authority

| Area | Source anchors | Verified behavior |
| --- | --- | --- |
| Fixed pipeline | `backend/phi_core/agents/orchestrator.py:1-24,557-1084` | `run_pipeline` retains the fixed pipeline and capped Judge/Sentinel loop. Execution from the Executor node onward is shared by fresh and resumed runs through `execute_decisions`, rather than a separate resume-only tail. |
| Initial execution | `backend/server.py:460-580,2579-2681`; `backend/phi_core/control/superorchestrator.py:118-166` | `/handle` validates intake and input freshness, atomically claims an owner-scoped session with a new `run_id`, and asks `SuperOrchestrator.start_run` to create the durable run and root work item. A worker executes `pipeline_run` under a 900-second ceiling, correlates failures to the run, and emits an SSE end frame in `finally`. |
| Human resume | `backend/server.py:582-747,2812-3277`; `backend/phi_core/control/superorchestrator.py:456-489` | Human review atomically claims the session for a `pipeline_resume` work item. When a durable request exists, it writes one validated, idempotent review event; the resume worker reloads persisted review state and calls the shared `execute_decisions` path. `SuperOrchestrator` consumes that event and accepts material child results. |
| Manager | `backend/phi_core/agents/manager.py:26-75,180-300`; `backend/phi_core/agents/orchestrator.py:121-190`; `backend/phi_core/control/superorchestrator.py:359-452` | Manager supervises bounded call retries and escalation advice using count, enum, timing, and error-kind signals. It has no workflow-transition authority. The orchestration path records session escalation data, while `SuperOrchestrator.request_human_review`, `supersede_human_review`, and `advance` own durable review and workflow transitions. |
| Delegation | `backend/phi_core/control/activation.py:83-104`; `backend/phi_core/control/superorchestrator.py:273-354`; `backend/phi_core/agents/outward.py:174-209,299-333` | Ledger Compare/Aggregate and Herald Abstract/Sections are activated as durable child work through `create_child_work`, which enforces parent grant, depth, fanout, parallelism, and budget limits before enqueueing. Ledger and Herald accept each child result through their supplied completion hook. |
| Cancellation | `backend/phi_core/agents/orchestrator.py:91-118,267-271,345-349,398-406` | The pipeline checks the persisted cancellation request between phases. After Scout starts as a background task, every early return or cancellation path calls `_cancel_and_await(scout_task)`; the normal path awaits Scout in the Auditor/Scout gather. |
| Startup recovery | `backend/server.py:2513-2578` | Startup creates control-plane indexes, marks stale non-settled sessions failed and clears their stale run token, then starts retention, durable workers for `pipeline_run` and `pipeline_resume`, outbox draining, and task reconciliation. |

## Provider and tool control paths

| Area | Source anchors | Verified behavior |
| --- | --- | --- |
| Provider call entry | `backend/phi_core/agents/base.py:139-221,223-300`; `backend/phi_core/control/context.py:130-186` | `Agent._request` binds session, run, task, grant, provider, model, endpoint, and budget metadata. `Agent._log` emits through the context trace writer, not `agent_log`; `Agent.call` routes plain and web-search attempts through the gateway and supervised Manager closures. |
| LiteLLM inference | `backend/phi_core/control/gateway.py:1-26,179-342` | `ProviderGateway` is the production LiteLLM and research-tool boundary. It validates the grant, data class, provider, endpoint, budgets, granted tools, and scrubbed payload before its sole `litellm.completion` call. A requested tool is rejected unless it is granted and supported by the configured provider. |
| Research provenance | `backend/phi_core/agents/experts.py:97-130,163-175`; `backend/phi_core/agents/outward.py:79-113`; `backend/phi_core/control/context.py:107-127` | Statute and Scout are cache-first. Research URLs are accepted as verified only when they are both response tool citations and allowed primary-source URLs; Scout writes cache provenance as `web_search` only when a citation verifies. `StoreResearchCache` derives cache evidence state from that source rather than a model-authored sources list. |
| Cache research | `backend/phi_core/control/context.py:66-127` | `StoreResearchCache` is the current cache implementation. It uses `ControlStore`'s `web_cache` collection, treats stale policy versions and entries older than `WEB_CACHE_REFRESH_DAYS` as misses, and records a native-datetime fetch time for TTL eligibility. There is no `backend/phi_core/agents/cache.py`. |
| Direct provider entry points | `backend/phi_core/control/gateway.py:42-63,186-312`; `backend/phi_core/agents/base.py:139-162` | Production provider requests are `GatewayRequest` values carrying run, task, grant, agent, provider, endpoint, data-class, and budget identity. `ProviderGateway.complete` requires the stored grant to match the request and rejects identity, policy, tool, endpoint, and budget violations before inference. |

## Human transitions and state writes

| Area | Source anchors | Verified behavior |
| --- | --- | --- |
| Session intake | `backend/server.py:1051-1149` | Intake accepts only a ZIP, streams it to the fixed intake path under the upload cap, builds a manifest, removes the raw ZIP after unpacking, and conditionally resets an owner-scoped non-live session. A concurrent active pipeline wins the claim rather than being reset. |
| Initial claim and terminal updates | `backend/server.py:460-580,2579-2675` | `/handle` atomically claims only ready sessions in an eligible status, clears prior guard and export state, and starts a durable run. Its worker uses a run filter for terminal writes, maps timeout and cancellation to explicit states, and releases admission capacity in `finally`. |
| Human review | `backend/server.py:2812-3053` | Review requires an authorized reviewer, valid resolution modes, actual-knowledge acknowledgement for a resolution, and a version-matched dataset-file download for each resolved dataset column. It uses `client_event_id` plus body hash for idempotency, never auto-applies comment interpretation, and reruns canonical decision gates before accepting mutations. |
| Resume tail | `backend/server.py:3053-3277`; `backend/phi_core/control/superorchestrator.py:456-489` | Once a resolution exists, the route atomically writes the review-derived session state and claims `anonymizing`. It consumes the durable review event when present, starts a `pipeline_resume` durable run, and returns `resuming`; it does not run a detached local tail. |
| Failure correlation | `backend/server.py:376-399` | A failed worker stores a fixed error message plus short correlation ID, logs detailed exception material only on the server, and updates by `run_filter`. It cleans unpacked input only if that run-filtered update matched, so a stale worker cannot remove a newer run's files. |
| Cancellation route | `backend/server.py:2684-2740`; `backend/phi_core/control/superorchestrator.py:197-229` | `/cancel` is idempotent for settled sessions. Otherwise it persists the request, cancels the durable run subtree when one exists, and emits `cancel_requested`; only a legacy missing durable run falls back to the persisted session flag. |
| Session deletion | `backend/server.py:951-1050`; `backend/phi_core/control/artifacts.py:97-140` | Deletion tombstones the session before cancellation and erasure, preventing new artifact staging. Filesystem failures are returned and recorded as `erasure_pending` with retry data; the session, trace rows, and legacy rows are removed only after complete erasure. |
| Progress and SSE | `backend/server.py:281-306,356-374,1206-1257`; `backend/phi_core/control/events.py:222-291` | `_emit` persists bounded progress with a run filter, then publishes to `EventBroker` under `run_id` or session ID. `session_stream` subscribes per current run, caps subscribers, sends heartbeats, closes slow subscribers with `__resync__`, and always unsubscribes. Each subscriber has its own bounded queue, so events fan out rather than being consumed by one peer. |

## Artifact writes and cleanup

| Area | Source anchors | Verified behavior |
| --- | --- | --- |
| Artifact roots | `backend/phi_core/paths.py:75-96,107-146`; `backend/phi_core/control/artifacts.py:48-55,114-140` | No `EXPORT_DIR` exists. Artifact roots are registry-mapped to intake, staging, evidence, reversal, published, and cache directories, with run-scoped safe-ID paths. Session-wide artifact erasure returns per-root failures for recording and retry; `cleanup_session_unpacked` remains a separate unpacked-input cleanup path. |
| Executor writes | `backend/phi_core/agents/reasoning.py:1147-1175`; `backend/phi_core/control/artifacts.py:156-255` | Executor finalizes a provisional artifact through `ArtifactService`, which hashes staged bytes, checks the run budget, atomically replaces into a run-scoped path, and CAS-transitions the record to `staged`. The Executor records a same-inode suffix alias for Guard dispatch; canonical stored and served paths use bare artifact IDs, never original filenames. |
| Guard | `backend/phi_core/publish_guard.py:163-205,208-282,447-497` | Publish Guard scans supported text and spreadsheet surfaces, blocks unsupported or unreadable files, and runs Presidio PERSON scanning for category-A names on every supported surface. Each result carries the canonical artifact ID and scanned-byte SHA-256; an empty or blocked export set is not clean. |
| Bundle | `backend/server.py:1422-1487`; `backend/phi_core/bundle.py:542-615` | Bundle download requires terminal shareable status, a clean Guard report, and registry hash verification of every clean artifact before assembly. `build_bundle` includes only file IDs with exactly one clean Guard result, hashes included bytes into attestation material, and writes a signature when a signing key is available. |
| Reversal material | `backend/phi_core/crypto.py:62-63,106-132`; `backend/server.py:1490-1528` | Reversal maps use server-key Fernet encryption with no plaintext fallback and stay outside exports and publication bundles. The owner-only download repeats status, Guard, and artifact checks, then unsets the encrypted blob after one response. |
| Retention | `backend/server.py:951-978,2373-2511`; `backend/phi_core/control/artifacts.py:392-486` | The hourly loop checks workflow holds before four independent sweeps: terminal-session erasure, expired-review raw-input erasure, retry of `erasure_pending`, and artifact reconciliation. Session-wide erasure reports failures into `erasure_pending` for retry instead of suppressing them; reconciliation marks deletion pending before unlinking so failures remain retryable. |

## Download checks

| Route | Source anchors | Current check |
| --- | --- | --- |
| `session_bundle` | `backend/server.py:1422-1487` | Requires an owner-scoped `complete` or `partially_complete` session, `guard_report.status == "clean"`, and hash-verifiable clean artifacts before building the bundle. |
| `session_reversal_key` | `backend/server.py:1490-1528` | Requires the same terminal-status, clean-Guard, and artifact checks, requires a reversal blob, then unsets that blob after decrypting it for the response. |
| `session_export` | `backend/server.py:1531-1588`; `backend/phi_core/control/artifacts.py:340-370` | Requires a terminal shareable session and exactly one clean per-file Guard result. It accepts no `force` or other override, resolves only the clean artifact ID, and serves through `open_for_download`, which checks promotion state, publication generation, and live hash. |
| `corpus_study_data_zip` | `backend/server.py:1703-1724` | Requires the API token dependency and returns a named curated package as a manifest-v3 intake ZIP. It is corpus intake material, not a session export. |
| `corpus_study_zip` | `backend/server.py:1875-1889` | Requires an owner-scoped session and streams its generated corpus intake ZIP only if the recorded path exists. It is not a Publish Guard-certified export route. |
| `corpus_study_benchmark_download` | `backend/server.py:2031-2057` | Requires an owner-scoped session, derives the report from current session data and `_session_trace_messages`, then returns the benchmark ZIP. It no longer reads `agent_log`. |
| `session_dataset_file` | `backend/server.py:3347-3393` | Intentionally permits an owner to download original dataset bytes at any session status. It validates the session-owned dataset ID and existing stored path, records principal, timestamp, and current decision version, then streams the file; that record supports the human-review attestation gate. |

## Existing deterministic spine

- `backend/phi_core/agents/reasoning.py:1140-1303`: deterministic Executor applies accepted decisions and stages artifacts through the registry; incomplete writes remain provisional.
- `backend/phi_core/agents/orchestrator.py:91-118,267-271,345-406`: persisted cancellation checks and bounded Scout-task shutdown at every early exit after Scout starts.
- `backend/phi_core/agents/manager.py:180-300`: bounded, metadata-only supervised retry and escalation advice.
- `backend/phi_core/agents/base.py:139-221,223-300` and `backend/phi_core/control/gateway.py:179-342`: grant-bound request construction, supervised provider calls, payload scrubbing, and provider, tool, and budget enforcement.
- `backend/phi_core/agents/batching.py`: bounded batch/pool execution, exactly-one-result contract, and cancellation behavior.
- `backend/phi_core/agents/operator.py` and `backend/phi_core/agents/reviewer.py`: deterministic operator verification plus independent reviewer coverage audit before publication.
- `backend/phi_core/publish_guard.py:163-205,208-282,447-497`: fail-closed export scanning, including category-A person-name detection and artifact-hash binding.
- `backend/phi_core/control/superorchestrator.py:233-354,359-489`: fail-closed workflow advancement, durable child-work authorization, review request lifecycle, and accepted-result authority.
- `backend/phi_core/control/artifacts.py:114-140,156-370,392-486`: registry-owned writes, promotion, hash-bound download access, erasure failure reporting, and reconciliation.
- `backend/phi_core/control/events.py:73-135,222-291`: append-only hash-chained trace events and bounded run-scoped SSE fan-out.
- `backend/phi_core/security.py`, `backend/phi_core/intake.py`, and `backend/phi_core/paths.py:75-161`: provider URL, persisted-text, intake traversal and resource, and safe scoped-path controls.
- `backend/server.py:356-374,460-747,951-1050,1206-1257,2373-2740,2812-3277,3347-3393`: owner-scoped routing, run fencing, durable workers, human-review evidence gates, delivery records, retention holds, and secure artifact downloads.
