"""FastAPI server for PHI handling console.

Endpoints (all under /api unless noted):
  GET  /api/health                         -> mongo/llm/tesseract/signing-key readiness
  GET  /api/version                        -> service banner
  POST /api/auth/session                   -> exchange an API token for the phi_session cookie
  POST /api/auth/logout                    -> clear the phi_session cookie
  GET  /api/auth/whoami                    -> resolved principal for the current credential
  POST /api/sessions                       -> create session
  GET  /api/sessions                       -> list sessions
  GET  /api/sessions/{id}                  -> session state
  DELETE /api/sessions/{id}                -> right-to-erasure: delete session, files, logs
  POST /api/sessions/{id}/intake           -> upload manifest-v3 ZIP, run intake
  GET  /api/sessions/{id}/intake/receipt   -> redacted intake receipt
  GET  /api/intake/spec                    -> intake-manifest/v3 spec
  GET  /api/sessions/{id}/stream           -> SSE progress
  POST /api/sessions/{id}/handle           -> run the 12-agent pipeline
  POST /api/sessions/{id}/cancel           -> request pipeline cancellation
  POST /api/sessions/{id}/human-review     -> resolve human_review decisions
  GET  /api/sessions/{id}/agent-trace      -> per-message audit log
  GET  /api/sessions/{id}/dataset-file/{file_id} -> raw original dataset file, byte-identical
  GET  /api/sessions/{id}/results          -> consolidated agent outputs
  GET  /api/sessions/{id}/bundle           -> shareable bundle download
  GET  /api/sessions/{id}/export/{file_id} -> download one redacted file
  GET  /api/coverage-matrix                -> static coverage matrix
  GET  /api/classification-accuracy        -> hard-rule layer P/R/F1
  GET  /api/corpus/study/catalog           -> available corpus scenarios
  POST /api/corpus/study/research          -> discover a scenario via CorpusResearcher
  POST /api/corpus/study/generate          -> generate a corpus, attach to a session
  GET  /api/corpus/study/{id}/zip          -> download the generated/run intake ZIP
  POST /api/corpus/study/run               -> create session, plant corpus, run pipeline
  GET  /api/corpus/study/verify/{id}       -> grade decisions against planted ground truth
  GET  /api/corpus/study/benchmark/{id}          -> per-dataset benchmark report
  GET  /api/corpus/study/benchmark/{id}/download -> benchmark artefact bundle ZIP
  GET  /api/settings/llm                   -> current LLM settings
  POST /api/settings/llm                   -> update LLM settings
  GET  /api/settings/llm/catalog           -> multi-provider model catalog
  GET  /api/settings/warmup/schedule       -> auto-warmup toggle state
  POST /api/settings/warmup/schedule       -> set auto-warmup toggle
  POST /api/settings/warmup                -> prime Statute + Praxis caches
  POST /api/settings/chatgpt/login         -> start ChatGPT OAuth device-code login
  GET  /api/settings/chatgpt/login/{id}    -> poll device-code login status (one poll)
  GET  /api/settings/chatgpt/status        -> current ChatGPT connection status
  DELETE /api/settings/chatgpt             -> disconnect the ChatGPT account

Everything else (/, /static/...) is the built frontend SPA, mounted last
(4.15) so no API route above is ever shadowed.
"""
from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from phi_core import chatgpt_auth
from phi_core.agents import AgentMessage, LlmConfig
from phi_core.agents import run_pipeline as run_agent_pipeline
from phi_core.control import limits
from phi_core.control.events import EventBroker
from phi_core.crypto import (
    decrypt_api_key,
    decrypt_display_name,
    encrypt_api_key,
    encrypt_display_name,
    signing_public_key_pem,
)
from phi_core.db import get_db
from phi_core.intake import (
    ANY_OF,
    COMPONENT_SUFFIXES,
    MANDATORY,
    build_manifest,
)
from phi_core.jurisdictions import REGISTRY, get_pack
from phi_core.models import FileArtifact, ProgressEvent, Session
from phi_core.paths import CHATGPT_TOKEN_DIR, UPLOAD_DIR, cleanup_session_unpacked, safe_join
from phi_core.security import (
    allowed_providers,
    require_api_token,
    resolve_principal,
    resolve_principal_soft,
    reviewer_principals,
    reviewer_role,
    scrub_decision,
    token_principals,
    validate_llm_base_url,
    validate_llm_provider,
)
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

if TYPE_CHECKING:
    from typing import Any


load_dotenv()

# Redirect litellm's ChatGPT-provider Authenticator to the pinned token
# directory (backend/phi_core/paths.py) rather than the per-user home
# directory it defaults to. Must run before any request-time litellm call
# constructs an Authenticator, so it is set at import time here rather
# than in an on_event("startup") hook.
os.environ.setdefault("CHATGPT_TOKEN_DIR", str(CHATGPT_TOKEN_DIR))

_log = logging.getLogger("phi_console")

_INTAKE_ZIP_FILE = File(...)


def _refuse_to_boot_insecure() -> None:
    """Refuse to start with an insecure production configuration.

    A no-op in ``PHI_ENV=dev``. Otherwise collects every violated
    requirement before raising, so an operator fixes everything in one
    pass instead of restarting five times to discover each failure.
    """
    if os.environ.get("PHI_ENV", "production") == "dev":
        return
    problems: list[str] = []
    if not token_principals():
        problems.append("API_TOKENS (or legacy API_TOKEN) must be set")
    if not reviewer_principals():
        problems.append("REVIEWER_PRINCIPALS must be set (name:role,... with role in "
                         "reviewer|lead_reviewer)")
    cors_raw = os.environ.get("CORS_ALLOWED_ORIGINS", "").strip()
    if not cors_raw or "*" in {o.strip() for o in cors_raw.split(",")}:
        problems.append("CORS_ALLOWED_ORIGINS must be set to a specific origin list, not '*'")
    mongo_url = os.environ.get("MONGO_URL", "")
    if "@" not in mongo_url:
        problems.append("MONGO_URL must include authentication credentials (mongodb://user:pass@host/...)")
    if not os.environ.get("APP_ENCRYPTION_KEY", "").strip():
        problems.append("APP_ENCRYPTION_KEY must be set")
    if not os.environ.get("ATTESTATION_SIGNING_KEY", "").strip():
        problems.append("ATTESTATION_SIGNING_KEY must be set")
    elif signing_public_key_pem() is None:
        problems.append("ATTESTATION_SIGNING_KEY is set but is not a valid base64 PKCS8 Ed25519 private key")
    if problems:
        raise RuntimeError(
            "Refusing to start with PHI_ENV=" + os.environ.get("PHI_ENV", "production")
            + " and an insecure configuration:\n- " + "\n- ".join(problems)
            + "\nSet PHI_ENV=dev for local development, or fix the above for production."
        )


_refuse_to_boot_insecure()

app = FastAPI(title="PHI Handling Console", version="2.0.0")

_cors_env = os.environ.get("CORS_ALLOWED_ORIGINS", "").strip()
_cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()] if _cors_env else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 4.20: response headers. The app now owns its own headers (it serves the
# built SPA itself, per 4.15) and set none before this.
_HSTS = os.environ.get("PHI_ENV", "production") != "dev"


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; object-src 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    if _HSTS:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# 4.20: rate limits. Process-local sliding window keyed by resolved
# principal when one is present, else client address -- a single container
# needs no shared store. Exceeding a limit returns 429 with Retry-After.
# D15 4b: an `OrderedDict` capped at `MAX_RATE_BUCKET_KEYS`, least-recently
# -used eviction, with any key whose window has emptied out removed
# immediately rather than left as a dead entry -- an attacker rotating
# through distinct identities cannot grow this dict without bound.
_RATE_BUCKETS: "collections.OrderedDict[str, list[float]]" = collections.OrderedDict()


def _rate_limit_identity(request: Request) -> str:
    principal = resolve_principal_soft(
        request.headers.get("x-api-token"), request.cookies.get("phi_session"),
    )
    if principal:
        return principal
    return request.client.host if request.client else "unknown"


def rate_limited(bucket: str, limit: int, window_seconds: int):
    async def _dep(request: Request) -> None:
        key = f"{bucket}:{_rate_limit_identity(request)}"
        now = time.monotonic()
        cutoff = now - window_seconds
        hits = _RATE_BUCKETS.get(key)
        if hits is not None:
            while hits and hits[0] < cutoff:
                hits.pop(0)
            if hits:
                _RATE_BUCKETS.move_to_end(key)
            else:
                del _RATE_BUCKETS[key]
                hits = None
        if hits is None:
            hits = []
            _RATE_BUCKETS[key] = hits
            if len(_RATE_BUCKETS) > limits.MAX_RATE_BUCKET_KEYS:
                _RATE_BUCKETS.popitem(last=False)
        if len(hits) >= limit:
            retry_after = max(1, int(hits[0] + window_seconds - now))
            raise HTTPException(
                429, "rate limit exceeded; try again later",
                headers={"Retry-After": str(retry_after)},
            )
        hits.append(now)
    return _dep


# 4.23: errors must not describe internals. FastAPI's default handler for
# an uncaught exception returns a 500 with whatever the exception carried,
# which can echo stack-trace-adjacent detail (file paths, library names,
# occasionally an argument value) to the client. Every unhandled exception
# gets a short correlation id, the full detail goes to the server log
# against that id, and the client sees only the id. HTTPException already
# has its own, more specific handler (FastAPI's default), so the
# deliberately precise 4xx messages already in the code -- intake
# validation errors, Publish Guard refusals, rate limits -- are untouched.
@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    error_id = uuid.uuid4().hex[:12]
    _log.error(
        "unhandled exception [%s] on %s %s: %s: %s",
        error_id, request.method, request.url.path, type(exc).__name__, exc,
        exc_info=True,
    )
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL", "message": "unexpected error", "error_id": error_id}},
    )


# Hard cap on individual upload bytes (defense in depth alongside intake ZIP caps).
_MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", 250 * 1024 * 1024))


async def _stream_to_disk(upload_file: UploadFile, dst_path: Path, max_bytes: int) -> int:
    """Copy an UploadFile to disk with a hard byte cap. Returns bytes written."""
    written = 0
    with dst_path.open("wb") as out:
        while True:
            chunk = await upload_file.read(1 << 20)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                out.close()
                try:
                    dst_path.unlink()
                except OSError:
                    pass
                raise HTTPException(413, f"upload exceeds {max_bytes} byte limit")
            out.write(chunk)
    return written


# --- Run-scoped SSE fan-out (D15) ------------------------------------------
#
# Replaces the single shared `asyncio.Queue` per session_id that used to
# split each event between whichever concurrent subscriber called
# `queue.get()` next instead of delivering it to all of them.
# `EventBroker` keys subscriptions by run_id when one exists (so a session
# resuming into a new run_id naturally gets a fresh subscription rather
# than eavesdropping on a superseded run) and falls back to the session_id
# itself for the few early-failure `_emit` call sites that fire before a
# durable run_id exists.

_event_broker = EventBroker()


def _stream_key(session_id: str, run_id: str | None) -> str:
    return run_id or session_id


# Terminal statuses that guarantee the pipeline is done and no more
# events will arrive on the SSE queue. `expired_awaiting_review` (D15
# step 4: raw PHI erased after REVIEW_RETENTION_DAYS with no reviewer
# action) and `erasure_pending` (Phase 7: right-to-erasure filesystem
# work not yet confirmed) are both dead ends -- no worker resumes either.
_SETTLED_STATUSES = frozenset({"complete", "failed", "cancelled", "blocked",
                                "intake_failed", "awaiting_human_review", "partially_complete",
                                "expired_awaiting_review", "erasure_pending"})

# Cap of concurrent SSE subscribers per run. 4 is enough for the operator
# + a couple of secondary viewers + one connection retry. Beyond that we
# refuse new subscribers (returns HTTP 429) to prevent an attacker from
# opening thousands of streams and pinning memory.
_MAX_STREAM_SUBSCRIBERS_PER_SESSION = 4

# Admission control: cap concurrent pipeline runs on this process. Each
# run holds an LLM connection open for up to the 15-minute wall-clock
# ceiling; without a cap an operator can queue unbounded background tasks.
# `/handle` and the human-review resume worker both check this at launch
# and return 429 immediately rather than queueing past the cap.
_MAX_CONCURRENT_PIPELINES = int(os.environ.get("MAX_CONCURRENT_PIPELINES", "2"))
_active_pipeline_count = 0

# 4.21: bound the width of a single study. Prompt sizes are already capped
# per column (specialists.py), but the column count itself was not, so a
# wide dataset could still produce an unbounded Judge prompt and decision
# list. Checked after header hydration in session_handle, before any LLM
# call is made.
_MAX_COLUMNS_PER_STUDY = int(os.environ.get("MAX_COLUMNS_PER_STUDY", "500"))


def _enforce_column_cap(files: list[dict]) -> int:
    """4.21: sum dataset columns across a study and raise when it exceeds
    ``_MAX_COLUMNS_PER_STUDY``. Returns the total on success so the caller
    can log/report it if useful."""
    total_columns = sum(len(f.get("columns") or []) for f in files if f.get("kind") == "dataset")
    if total_columns > _MAX_COLUMNS_PER_STUDY:
        raise ValueError(
            f"study has {total_columns} dataset columns, exceeding the "
            f"{_MAX_COLUMNS_PER_STUDY}-column-per-study limit"
        )
    return total_columns


def _admit_pipeline_run() -> bool:
    global _active_pipeline_count
    if _active_pipeline_count >= _MAX_CONCURRENT_PIPELINES:
        return False
    _active_pipeline_count += 1
    return True


def _release_pipeline_run() -> None:
    global _active_pipeline_count
    _active_pipeline_count = max(0, _active_pipeline_count - 1)


async def _emit(session_id: str, ev: ProgressEvent, run_id: str | None = None) -> None:
    db = get_db()
    query = {"id": session_id}
    if run_id is not None:
        query["_pipeline_run_id"] = run_id
    result = await db.sessions.update_one(
        query,
        {
            # D15 4b: the persisted array is bounded to the most recent
            # MAX_SESSION_PROGRESS_EVENTS entries -- the complete history
            # lives in `trace_events`, which has no such cap.
            "$push": {"progress": {"$each": [ev.model_dump()], "$slice": -limits.MAX_SESSION_PROGRESS_EVENTS}},
            "$set": {"updated_at": datetime.now(timezone.utc).isoformat()},
        },
    )
    if run_id is not None and not getattr(result, "matched_count", 0):
        return
    _event_broker.publish(_stream_key(session_id, run_id), ev.model_dump())


async def _fail_session_correlated(db, sid: str, run_filter: dict, e: Exception, *, run_id: str | None) -> None:
    """4.23: mark a pipeline worker's session failed without persisting or
    streaming raw exception text. Full detail (exception type, message,
    traceback) goes to the server log against a short correlation id; the
    stored session and the SSE client see only a fixed message plus that
    id, matching the global unhandled-exception handler's contract.

    Phase 4 step 6: ``cleanup_session_unpacked`` only runs when this
    handler's own run-filtered ``update_one`` actually matched a document.
    ``run_filter`` includes ``_pipeline_run_id``, so a stale worker from an
    already-superseded run (the session moved on to a newer run, or was
    reset) loses this race and must not delete a newer run's unpacked
    input tree out from under it.
    """
    error_id = uuid.uuid4().hex[:12]
    _log.error(
        "session %s pipeline worker failure [%s]: %s: %s",
        sid, error_id, type(e).__name__, e, exc_info=True,
    )
    result = await db.sessions.update_one(run_filter, {"$set": {"status": "failed", "error": "pipeline failed", "error_id": error_id}})
    if getattr(result, "matched_count", 0):
        cleanup_session_unpacked(sid)
    await _emit(sid, ProgressEvent(phase="failed", message=f"pipeline error (id {error_id}); see server logs"), run_id=run_id)


async def _validate_rerun_inputs(files: list[dict]) -> list[str]:
    """Phase 4 step 6: rerun admission check. Returns the ``file_id``s
    whose ``stored_path`` is missing or no longer re-hashes to the
    recorded ``sha256`` -- a file deleted, truncated, or modified on disk
    since intake. A non-empty result means ``/handle`` must refuse with
    ``409 error="reintake_required"`` rather than let the pipeline run
    against silently-changed or absent input bytes."""
    stale: list[str] = []
    for f in files:
        stored_path = f.get("stored_path")
        expected_sha256 = f.get("sha256")
        file_id = f.get("file_id", "")
        if not stored_path or not expected_sha256:
            continue
        path = Path(stored_path)
        if not path.exists():
            stale.append(file_id)
            continue
        digest = hashlib.sha256()
        try:
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
        except OSError:
            stale.append(file_id)
            continue
        if digest.hexdigest() != expected_sha256:
            stale.append(file_id)
    return stale


async def _emit_terminal_trace(store, *, sid: str, run_id: str, task_id: str, fence: int, status: str) -> None:
    """D15: append the audit-grade counterpart of the terminal
    ``ProgressEvent`` into ``trace_events``. Fenced against ``task_id``'s
    own ``WorkItem``: a worker whose lease was reconciled away while it
    kept running past its 15-minute ceiling (a zombie) cannot publish a
    terminal trace event for a task another worker has since completed --
    ``TraceEventStore.append`` raises ``EventAppendError`` and the stale
    attempt is discarded rather than recorded as if it still applied.
    ``work_item.fence`` never changes between claim and this handler
    returning (only ``TaskService.complete``/``fail`` bump it, and that
    happens after this call, in ``Worker._execute``), so the current
    holder's own fence always matches unless it has already been
    superseded."""
    if status not in _SETTLED_STATUSES:
        return
    from phi_core.control.events import EventAppendError, TraceEventStore
    from phi_core.control.records import TraceEvent

    event = TraceEvent(
        run_id=run_id, seq=0, session_id=sid, task_id=task_id, outcome=status,
        input_class="internal", output_class="internal",
    )
    try:
        await TraceEventStore(store, run_id=run_id, session_id=sid).append(event, fence=fence)
    except EventAppendError:
        _log.info("terminal trace event for run_id=%s task_id=%s discarded (fenced or stale)", run_id, task_id)


async def _handle_pipeline_run(store, work_item) -> dict[str, Any]:
    """Phase 4 step 2/4: the ``pipeline_run`` ``TaskService`` handler.

    Runs a fresh pipeline for ``work_item.session_id``/``work_item.run_id``,
    claimed and dispatched by one of the ``Worker`` instances
    ``_startup_maintenance`` starts. Replaces ``session_handle``'s former
    per-request ``asyncio.create_task(worker())`` closure: the route now
    only validates, claims the session, and enqueues; this function is
    where the actual work -- and every outcome the session document must
    end up recording -- happens. ``store`` (the ``ControlStore``) is
    unused here; the session document lives in the plain Motor ``db``.
    """
    sid = work_item.session_id
    run_id = work_item.run_id
    run_filter = {"id": sid, "_pipeline_run_id": run_id}
    db = get_db()
    session = await db.sessions.find_one(run_filter)
    if session is None:
        # The claim that enqueued this task has since been superseded
        # (a later run claimed the session, or it was deleted/tombstoned)
        # -- nothing to do, and nothing to fail; not this task's session
        # anymore.
        return {"status": "superseded"}
    cfg = await _current_llm_cfg()

    async def emit_msg(msg: AgentMessage) -> None:
        ev = ProgressEvent(
            phase=f"agent:{msg.agent}:{msg.direction}",
            message=f"{msg.agent} {msg.phase}",
            payload={"agent": msg.agent, "phase_key": msg.phase, "direction": msg.direction,
                     "duration_ms": msg.duration_ms, "status_text": msg.status_text,
                     "parent_id": msg.parent_id, "id": msg.id},
        )
        await _emit(sid, ev, run_id=run_id)

    async def on_phase(phase: str, payload: dict):
        await _emit(sid, ProgressEvent(phase=f"agent_phase:{phase}", message=phase, payload=payload), run_id=run_id)

    try:
        # Populate dataset headers (LLM never sees rows). Persist onto session before pipeline runs.
        from phi_core.file_readers import read_csv_columns, read_parquet_columns, read_xlsx_columns
        files_hydrated = []
        for f in session.get("files", []):
            if f.get("kind") == "dataset" and not f.get("columns"):
                p = Path(f["stored_path"])
                ext = f.get("subtype", "").lower()
                try:
                    if ext in ("csv", "tsv"):
                        cols, rows = read_csv_columns(p)
                    elif ext in ("xlsx", "xls"):
                        cols, rows = read_xlsx_columns(p)
                    elif ext == "parquet":
                        cols, rows = read_parquet_columns(p)
                    else:
                        cols, rows = [], 0
                    f["columns"] = cols
                    f["row_count"] = rows
                except Exception as e:
                    await _emit(sid, ProgressEvent(phase="reading", message=f"header extract failed for {f['file_id']}: {e}"), run_id=run_id)
            files_hydrated.append(f)
        session["files"] = files_hydrated
        await db.sessions.update_one(run_filter, {"$set": {"files": files_hydrated}})

        # SEC: reject duplicate physical column headers before the pipeline
        # runs. csv.DictReader (and the xlsx path's positional header list)
        # collapse duplicate header names, so the Executor's transform loop
        # later writes one merged/last-wins value into every matching output
        # slot -- silent row-value corruption that Operator/Reviewer can't
        # catch because they key decisions by header name too. Fail closed.
        for f in files_hydrated:
            if f.get("kind") != "dataset":
                continue
            cols = f.get("columns") or []
            dupes = sorted({c for c in cols if cols.count(c) > 1})
            if dupes:
                raise ValueError(
                    f"dataset {f.get('filename', f.get('file_id'))!r} has duplicate "
                    f"column header(s): {', '.join(dupes)}"
                )

        # 4.21: refuse an oversized study rather than sending an
        # unbounded Judge prompt / decision list.
        _enforce_column_cap(files_hydrated)

        # HANG PROTECTION: hard 15-minute wall-clock ceiling. If the
        # pipeline burns beyond this the worker is cancelled with a
        # clear "timeout" reason -- no orphaned tasks, no infinite
        # loading screens. 15 min is 5x the observed 190 s happy path
        # and 2x the worst historical case (~340 s + Herald 90 s x2).
        result = await asyncio.wait_for(
            run_agent_pipeline(
                session, db, cfg, emit_msg, on_phase, run_id=run_id, control_store=store,
                root_task_id=work_item.task_id,
            ),
            timeout=900,
        )
        await _emit(sid, ProgressEvent(phase="complete", message=f"Pipeline done: {result.get('status')}", percent=100.0), run_id=run_id)
        return {"status": result.get("status")}
    except asyncio.TimeoutError:
        await db.sessions.update_one(
            run_filter,
            {"$set": {"status": "failed",
                      "error": "pipeline exceeded 15-minute wall-clock ceiling"}},
        )
        cleanup_session_unpacked(sid)
        await _emit(sid, ProgressEvent(
            phase="failed",
            message="Pipeline hit the 15-minute wall-clock ceiling. "
                    "This usually means an LLM call is stuck; try again "
                    "or switch model in Settings.",
            payload={"reason": "wall_clock_ceiling_exceeded"},
        ), run_id=run_id)
        return {"status": "failed", "reason": "wall_clock_ceiling_exceeded"}
    except Exception as e:
        from phi_core.agents.orchestrator import PipelineCancelled
        if isinstance(e, PipelineCancelled):
            await db.sessions.update_one(
                run_filter,
                {"$set": {"status": "cancelled",
                          "cancelled_at": datetime.now(timezone.utc).isoformat()}},
            )
            cleanup_session_unpacked(sid)
            await _emit(sid, ProgressEvent(
                phase="cancelled",
                message="Pipeline cancelled by operator.",
                payload={"reason": "operator_cancel"},
            ), run_id=run_id)
            return {"status": "cancelled"}
        await _fail_session_correlated(db, sid, run_filter, e, run_id=run_id)
        return {"status": "failed"}
    finally:
        _release_pipeline_run()
        if session is not None:
            final_doc = await db.sessions.find_one(run_filter, {"status": 1})
            if final_doc:
                await _emit_terminal_trace(
                    store, sid=sid, run_id=run_id, task_id=work_item.task_id,
                    fence=work_item.fence, status=final_doc.get("status", ""),
                )
        await _emit(sid, ProgressEvent(phase="__end__", message="stream end"), run_id=run_id)


async def _handle_pipeline_resume(store, work_item) -> dict[str, Any]:
    """Phase 4 step 2/4: the ``pipeline_resume`` ``TaskService`` handler.

    Runs ``phi_core.agents.orchestrator.execute_decisions`` -- the same
    D9 ``execute`` node a fresh run reaches, per ``docs/adr/0001-
    workflow-engine.md`` -- for a resumed human-review round. Every
    decision, ``pending_review``, and ``session_review`` field this needs
    was already persisted onto the session document by
    ``session_human_review`` before enqueuing this task; nothing is
    threaded through ``work_item.input_ref``.
    """
    from phi_core.agents import orchestrator
    from phi_core.agents.manager import Manager
    from phi_core.control.activation import ActivationFactory

    sid = work_item.session_id
    run_id = work_item.run_id
    run_filter = {"id": sid, "_pipeline_run_id": run_id}
    db = get_db()
    session = await db.sessions.find_one(run_filter)
    if session is None:
        return {"status": "superseded"}
    cfg = await _current_llm_cfg()
    decisions = session.get("agent_decisions") or []
    pending_review = session.get("pending_review") or []
    session_review_history = session.get("session_review") or []
    dictionary_by_column = {c.get("name"): c.get("description", "")
                            for c in (session.get("agent_specialists") or {}).get("lexicon", {}).get("columns", [])
                            if c.get("name")}
    files = session.get("files", [])

    async def emit_msg(msg: AgentMessage) -> None:
        ev = ProgressEvent(
            phase=f"agent:{msg.agent}:{msg.direction}",
            message=f"{msg.agent} {msg.phase}",
            payload={"agent": msg.agent, "phase_key": msg.phase, "direction": msg.direction,
                     "duration_ms": msg.duration_ms, "status_text": msg.status_text,
                     "parent_id": msg.parent_id, "id": msg.id},
        )
        await _emit(sid, ev, run_id=run_id)

    async def on_phase(phase: str, payload: dict):
        await _emit(sid, ProgressEvent(phase=f"agent_phase:{phase}", message=phase, payload=payload), run_id=run_id)

    phase_timings: dict[str, dict[str, float]] = {}
    last_phase: dict[str, str | float | None] = {"key": None, "t0": 0.0}
    run_started = time.perf_counter()
    manager_box: dict[str, "Manager | None"] = {"value": None}

    async def timed_on_phase(phase: str, payload: dict) -> None:
        now = time.perf_counter()
        previous = last_phase["key"]
        if previous and previous != phase:
            timing = phase_timings.setdefault(
                str(previous), {"start_s": float(last_phase["t0"]) - run_started},
            )
            timing["end_s"] = now - run_started
            timing["duration_ms"] = (now - float(last_phase["t0"])) * 1000
        phase_timings.setdefault(phase, {"start_s": now - run_started})
        last_phase["key"] = phase
        last_phase["t0"] = now
        if manager_box["value"] is not None:
            await manager_box["value"].note_phase(phase, now - run_started)
        await on_phase(phase, payload)

    async def close_last_phase() -> None:
        previous = last_phase["key"]
        if not previous:
            return
        now = time.perf_counter()
        timing = phase_timings.setdefault(
            str(previous), {"start_s": float(last_phase["t0"]) - run_started},
        )
        timing.setdefault("end_s", now - run_started)
        timing.setdefault("duration_ms", (now - float(last_phase["t0"])) * 1000)

    _factory = ActivationFactory(db, cfg, store=store)

    async def _actx(agent: str):
        return await _factory.activate_child(
            session_id=sid, run_id=run_id, parent_task_id=work_item.task_id, agent=agent,
            emit=emit_msg, manager=manager_box["value"],
        )

    async def _child_actx(agent: str, parent_task_id: str):
        return await _factory.activate_child(
            session_id=sid, run_id=run_id, parent_task_id=parent_task_id, agent=agent,
            emit=emit_msg, manager=manager_box["value"],
        )

    async def _complete_and_accept(ctx, result: dict) -> bool:
        return await _factory.complete_and_accept(ctx, result)

    async def _run_resume() -> dict[str, Any]:
        manager = Manager(await _actx("Manager"), db=db)
        manager_box["value"] = manager
        await manager.run(
            roster=["Executor", "Operator", "Reviewer", "Auditor", "Scout", "Ledger", "Herald"],
            phase_plan=["executor", "operator", "reviewer", "publish_guard",
                        "auditor_scout", "ledger", "herald"],
        )
        resolved_decisions = [d for d in decisions if d.get("action") != "human_review"]
        scrubbed_decisions = [scrub_decision(d) for d in resolved_decisions]
        omit_by_file: dict[str, set[str]] = {}
        for entry in pending_review:
            omit_by_file.setdefault(entry["file_id"], set()).add(entry["column"])
        return await orchestrator.execute_decisions(
            db=db, sid=sid, session=session, session_filter=run_filter,
            files=files, decisions=scrubbed_decisions,
            statute=session.get("agent_statute"), praxis_methods=session.get("agent_praxis"),
            dictionary_by_column=dictionary_by_column,
            make_ctx=_actx, make_child_ctx=_child_actx, complete_and_accept=_complete_and_accept,
            manager=manager, on_phase=timed_on_phase,
            close_last_phase=close_last_phase, phase_timings=phase_timings,
            run_started=run_started, omit_by_file=omit_by_file,
            extra_completion_fields={
                "session_review": session_review_history,
                "pending_review": pending_review,
                "human_review_required": bool(pending_review),
            },
            run_id=run_id, store=store,
        )

    try:
        result = await asyncio.wait_for(_run_resume(), timeout=900)
        await _emit(sid, ProgressEvent(
            phase="complete", message=f"Resume done: {result.get('status')}", percent=100.0,
        ), run_id=run_id)
        return {"status": result.get("status")}
    except asyncio.TimeoutError:
        await db.sessions.update_one(run_filter, {"$set": {
            "status": "failed",
            "error": "resume worker exceeded 15-minute wall-clock ceiling",
        }})
        cleanup_session_unpacked(sid)
        await _emit(sid, ProgressEvent(phase="failed", message="Resume hit the 15-minute wall-clock ceiling."), run_id=run_id)
        return {"status": "failed", "reason": "wall_clock_ceiling_exceeded"}
    except Exception as e:
        from phi_core.agents.orchestrator import PipelineCancelled
        if isinstance(e, PipelineCancelled):
            await db.sessions.update_one(run_filter, {"$set": {
                "status": "cancelled",
                "cancelled_at": datetime.now(timezone.utc).isoformat(),
            }})
            cleanup_session_unpacked(sid)
            await _emit(sid, ProgressEvent(
                phase="cancelled", message="Resume cancelled by operator.",
                payload={"reason": "operator_cancel"},
            ), run_id=run_id)
            return {"status": "cancelled"}
        await _fail_session_correlated(db, sid, run_filter, e, run_id=run_id)
        return {"status": "failed"}
    finally:
        _release_pipeline_run()
        if session is not None:
            final_doc = await db.sessions.find_one(run_filter, {"status": 1})
            if final_doc:
                await _emit_terminal_trace(
                    store, sid=sid, run_id=run_id, task_id=work_item.task_id,
                    fence=work_item.fence, status=final_doc.get("status", ""),
                )
        await _emit(sid, ProgressEvent(phase="__end__", message="stream end"), run_id=run_id)


# --- Health ----------------------------------------------------------------

@app.get("/api/health")
async def health():
    import shutil as _shutil

    from phi_core.crypto import signing_public_key_pem

    mongo_ok = False
    try:
        await asyncio.wait_for(get_db().command("ping"), timeout=2.0)
        mongo_ok = True
    except Exception:
        mongo_ok = False

    llm_doc = {}
    try:
        llm_doc = await get_db().settings.find_one({"_id": "llm"}, {"_id": 0}) or {}
    except Exception:
        llm_doc = {}
    provider = (llm_doc.get("provider") or "").strip()
    if not provider:
        from phi_core.agents.llm import _default_provider
        provider = _default_provider()
    elif provider == "emergent":
        # Legacy session document from before Emergent support was
        # removed; normalize the same way get_llm_settings/_current_llm_cfg
        # do, so health doesn't misreport on an unmigrated doc.
        provider = "anthropic"
    if provider == "chatgpt":
        llm_provider_ok = chatgpt_auth.read_auth() is not None
    elif provider == "openai":
        llm_provider_ok = bool(llm_doc.get("api_key") or os.environ.get("OPENAI_API_KEY"))
    elif provider == "anthropic":
        llm_provider_ok = bool(llm_doc.get("api_key") or os.environ.get("ANTHROPIC_API_KEY"))
    elif provider == "gemini":
        llm_provider_ok = bool(
            llm_doc.get("api_key")
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
        )
    elif provider == "openrouter":
        llm_provider_ok = bool(llm_doc.get("api_key") or os.environ.get("OPENROUTER_API_KEY"))
    else:
        llm_provider_ok = bool(llm_doc.get("api_key"))

    checks = {
        "mongo": mongo_ok,
        "llm_provider": llm_provider_ok,
        "tesseract": _shutil.which("tesseract") is not None,
        "pdftoppm": _shutil.which("pdftoppm") is not None,
        "signing_key": signing_public_key_pem() is not None,
    }
    body = {
        "status": "ok" if mongo_ok else "degraded",
        "version": "2.0.0",
        "hipaa_categories": get_pack("us").identifier_categories,
        "supported_jurisdictions": ["us"],
        "checks": checks,
    }
    if not mongo_ok:
        return JSONResponse(status_code=503, content=body)
    return body


# --- Cookie-based auth (SEC hardening 4.3) ----------------------------------
#
# The operator token never lives in localStorage or a URL query string.
# The browser POSTs it once to exchange it for an httponly, samesite=strict
# cookie; every subsequent request authenticates via that cookie. Scripted
# clients keep using the X-API-Token header directly.

class AuthSessionBody(BaseModel):
    token: str


@app.post("/api/auth/session", dependencies=[Depends(rate_limited("auth_session", 10, 900))])
async def auth_session(body: AuthSessionBody):
    import hmac as _hmac

    from phi_core.crypto import sign_principal_cookie
    from phi_core.security import token_principals
    principals = token_principals()
    principal = None
    for tok, name in principals.items():
        if _hmac.compare_digest(body.token, tok):
            principal = name
            break
    if principal is None:
        raise HTTPException(401, "invalid token")
    resp = JSONResponse({"principal": principal})
    resp.set_cookie(
        "phi_session", sign_principal_cookie(principal),
        httponly=True,
        secure=os.environ.get("PHI_ENV", "production") != "dev",
        samesite="strict", max_age=43200, path="/",
    )
    return resp


@app.post("/api/auth/logout")
async def auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("phi_session", path="/")
    return resp


@app.get("/api/auth/whoami")
async def auth_whoami(principal: str = Depends(resolve_principal)):
    return {"principal": principal}


# --- Sessions --------------------------------------------------------------

def _owned_filter(sid: str, principal: str) -> dict:
    return {"id": sid, "owner": principal}


async def _owned_session(sid: str, principal: str, projection: dict | None = None) -> dict:
    """Load a session the caller owns, or 404. Never 403: a wrong owner and a
    missing id are indistinguishable, so session ids stay unguessable."""
    doc = await get_db().sessions.find_one(_owned_filter(sid, principal), projection)
    if not doc:
        raise HTTPException(404, "session not found")
    return doc


class SessionCreate(BaseModel):
    jurisdiction: str = "us"


@app.post("/api/sessions")
async def session_create(body: SessionCreate, principal: str = Depends(resolve_principal)):
    key = (body.jurisdiction or "us").strip().lower()
    pack = get_pack(key)
    if key not in REGISTRY or not pack.supported:
        raise HTTPException(400, f"jurisdiction '{body.jurisdiction}' is not supported "
                             "(US HIPAA is the only active jurisdiction)")
    s = Session(jurisdiction=body.jurisdiction, owner=principal)
    await get_db().sessions.insert_one(s.model_dump())
    return s.model_dump()


def _scrub_session_document(doc: dict) -> dict:
    """Strip internal filesystem paths and scrub free-text fields before serving.

    SEC-002 completion + SEC-006: read endpoints must never leak internal
    layout (`stored_path`, `export_paths`) or raw PHI substrings the LLM may
    have echoed into agent notes. Uses recursive `scrub_nested` so PHI
    hiding in nested dicts/lists is caught too.
    """
    from phi_core.security import scrub_nested as _scrub_nested
    from phi_core.security import scrub_persisted_text as _scrub_text
    if not doc:
        return doc
    if isinstance(doc.get("error"), str) and doc["error"]:
        doc["error"] = _scrub_text(doc["error"])
    for f in doc.get("files", []) or []:
        f.pop("stored_path", None)
    if "export_paths" in doc:
        # Convert absolute paths to opaque blank strings; keys retained so
        # the UI still shows which file_ids are downloadable.
        doc["export_paths"] = {k: "" for k in (doc.get("export_paths") or {})}
    # SEC-003: strip the corpus ground-truth answer key from session reads.
    # The verifier endpoint reads it directly from Mongo, so no consumer
    # needs it via session_get / session_list / session_results. Leaving
    # it out prevents an attacker from post-hoc grading their own attempt.
    doc.pop("corpus_ground_truth", None)
    doc.pop("_pipeline_run_id", None)
    doc.pop("corpus_zip_path", None)
    guard_report = doc.get("guard_report")
    if isinstance(guard_report, dict):
        for r in guard_report.get("results") or []:
            if isinstance(r, dict) and "file_path" in r:
                r["file_path"] = ""

    for k in (
        "agent_decisions", "agent_herald", "agent_ledger",
        "agent_scout", "agent_audit", "agent_sentinel_last",
        "agent_specialists", "agent_statute", "pending_review", "session_review",
        # `audit` (distinct from `agent_audit`): the orchestrator's
        # Auditor-escalation path writes the raw Auditor verdict under
        # this key while the run is still `awaiting_human_review`, before
        # `agent_audit` is ever set at normal completion. Reviewers now
        # read this field directly (D13 step 8's Auditor confirmation
        # control), so it needs the exact same scrub as its post-
        # completion sibling, not a free pass because it is a different
        # key name for the same content.
        "audit",
    ):
        if k in doc:
            doc[k] = _scrub_nested(doc[k])
    return doc


@app.get("/api/sessions/{sid}")
async def session_get(sid: str, principal: str = Depends(resolve_principal)):
    doc = await _owned_session(sid, principal, {"_id": 0})
    return _scrub_session_document(doc)


@app.get("/api/sessions")
async def session_list(principal: str = Depends(resolve_principal)):
    cursor = get_db().sessions.find({"owner": principal}, {"_id": 0, "progress": 0}).sort("created_at", -1).limit(50)
    out = []
    async for s in cursor:
        out.append(_scrub_session_document(s))
    return {"sessions": out}


def _erase_session_from_disk(sid: str, export_paths: dict | None) -> dict[str, str]:
    """Every filesystem erasure a session's right-to-erasure or expiry
    needs: its artifact-registry roots (``erase_session_artifacts``), its
    raw ``UPLOAD_DIR/<sid>`` tree, and every path in ``export_paths``.

    Returns a mapping of failures (empty on full success) rather than the
    ``ignore_errors=True``/``except OSError: pass`` this replaces --
    ``session_delete`` and ``_purge_settled_sessions_loop`` both use this
    to decide whether a session's erasure is confirmed or must be
    recorded and retried."""
    import shutil

    from phi_core.control.artifacts import erase_session_artifacts

    errors = erase_session_artifacts(sid)
    try:
        shutil.rmtree(UPLOAD_DIR / sid)
    except FileNotFoundError:
        pass
    except OSError as exc:
        errors["uploads"] = str(exc)
    for p in (export_paths or {}).values():
        if p:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError as exc:
                errors[f"export:{p}"] = str(exc)
    return errors


@app.delete("/api/sessions/{sid}")
async def session_delete(sid: str, principal: str = Depends(resolve_principal)):
    """Right-to-erasure: remove the session document, its agent_log rows,
    its UPLOAD_DIR/<sid> tree, every path in export_paths, and every
    registered artifact (Phase 4 step 7).

    Coordinates with active work: the session is tombstoned before
    anything is deleted, so ``ArtifactService.stage`` refuses for this
    session from this point forward. When the session has a durable
    ``WorkflowRun``, ``SuperOrchestrator.cancel_run`` then fences its root
    task and every durable descendant through ``TaskService.cancel_subtree``
    before artifact erasure. A pre-Phase-5 session can have only the legacy
    ``_pipeline_run_id`` token, no durable run record; tombstoning remains
    its compatible anti-resurrection boundary and its cancellation request
    cannot be reconstructed after the fact.

    Phase 7: the session document is deleted only once every filesystem
    erasure is confirmed. A failure (permission error, concurrent external
    change) leaves the session as ``status="erasure_pending"`` with the
    exact errors and an attempt count recorded, rather than silently
    reporting success -- ``_purge_settled_sessions_loop`` retries it on
    the next sweep. The session stays tombstoned throughout, so it cannot
    resurrect or accept new work while erasure is pending.
    """
    from phi_core.control.artifacts import ArtifactService, tombstone_session
    from phi_core.control.policy import CapabilityPolicy
    from phi_core.control.store import MongoControlStore
    from phi_core.control.superorchestrator import SuperOrchestrator
    from phi_core.control.tasks import TaskService
    from phi_core.control.workflow import WorkflowError

    db = get_db()
    doc = await _owned_session(
        sid, principal, {"_id": 0, "export_paths": 1, "_pipeline_run_id": 1, "erasure_attempts": 1},
    )
    control_store = MongoControlStore(db)
    await tombstone_session(control_store, sid)
    if run_id := doc.get("_pipeline_run_id"):
        try:
            await SuperOrchestrator(
                control_store, TaskService(control_store, CapabilityPolicy(None))
            ).cancel_run(
                session_id=sid,
                run_id=run_id,
                principal=principal,
                reason="session deleted",
            )
        except WorkflowError as exc:
            # A session from before Phase 5 can have the legacy run token
            # without a durable WorkflowRun. It has already been tombstoned,
            # so no worker can stage a replacement artifact for it.
            if not str(exc).startswith("unknown run_id:"):
                raise
    run_id = doc.get("_pipeline_run_id") or sid
    await _erase_opaque_map_best_effort(db, run_id)
    await ArtifactService(control_store, session_id=sid, run_id=run_id).erase_session_records(sid)
    errors = _erase_session_from_disk(sid, doc.get("export_paths"))
    if errors:
        await db.sessions.update_one(_owned_filter(sid, principal), {"$set": {
            "status": "erasure_pending",
            "erasure_error": "; ".join(f"{k}: {v}" for k, v in errors.items()),
            "erasure_attempts": int(doc.get("erasure_attempts", 0)) + 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }})
        return {"deleted": False, "erasure_pending": True}
    await db.agent_log.delete_many({"session_id": sid})  # pre-migration rows, if any remain
    await db.trace_events.delete_many({"session_id": sid})
    await db.sessions.delete_one(_owned_filter(sid, principal))
    return {"deleted": True}


@app.post("/api/sessions/{sid}/intake", dependencies=[Depends(rate_limited("session_intake", 20, 3600))])
async def session_intake(
    sid: str, file: UploadFile = _INTAKE_ZIP_FILE, principal: str = Depends(resolve_principal)
):
    """Default entry: upload a ZIP with intake-manifest/v3 structure.

    ZIP must contain top-level `datasets/`, `forms/`, and one of
    `data_dictionary/` or `mappings/`. Fails closed on missing components or
    unsupported files.
    """
    db = get_db()
    session = await _owned_session(sid, principal)
    _LIVE_STATUSES = ("classifying", "anonymizing", "awaiting_human_review", "partially_complete")
    if session.get("status") in _LIVE_STATUSES:
        raise HTTPException(
            409,
            f"session has a pipeline run in progress (status={session.get('status')}); "
            "cancel it before re-uploading",
        )
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(400, "intake requires a .zip archive")

    # Claim the session for this intake before touching any path /handle's
    # claim filter (intake_status=="ready", status in intake/complete/failed/
    # cancelled) can also match. Without this, /handle can claim the *old*
    # ready state while this call is still overwriting intake.zip and the
    # shared unpacked/ tree underneath it, letting the pipeline read a
    # mixture of old and new files despite this call later returning 409.
    intake_claim_filter = dict(_owned_filter(sid, principal))
    intake_claim_filter["status"] = {"$nin": list(_LIVE_STATUSES)}
    claimed = await db.sessions.update_one(
        intake_claim_filter,
        {"$set": {"intake_status": "in_progress", "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    if not getattr(claimed, "matched_count", 0):
        raise HTTPException(409, "session has a pipeline run in progress; cancel it before re-uploading")

    session_dir = UPLOAD_DIR / sid
    session_dir.mkdir(parents=True, exist_ok=True)
    # Filename is fixed server-side to close SEC-001; stream with a hard cap.
    zip_path = safe_join(session_dir, "intake.zip", fallback="intake.zip")
    await _stream_to_disk(file, zip_path, _MAX_UPLOAD_BYTES)

    manifest = build_manifest(sid, zip_path, session_dir / "unpacked")

    # Convert accepted intake entries into FileArtifacts on the session.
    accepted: list[FileArtifact] = []
    for e in manifest.entries:
        if e.component == "_unclassified":
            continue
        ext = Path(e.relpath).suffix.lstrip(".").lower()
        if e.component == "datasets":
            kind = "dataset"
        elif e.component == "forms":
            kind = "narrative"
        else:
            kind = "metadata"
        accepted.append(FileArtifact(
            original_name_encrypted=encrypt_display_name(Path(e.relpath).name),
            size_bytes=e.size_bytes,
            sha256=e.sha256,
            kind=kind,
            subtype=ext,
            stored_path=e.stored_path,
            component=e.component,
        ))

    # SEC hardening 4.4: raw uploaded PHI bytes are no longer needed once
    # build_manifest has unpacked and classified them; unpacked/ stays
    # because the Executor reads stored_path from it later.
    try:
        zip_path.unlink(missing_ok=True)
    except OSError:
        pass

    # Re-intake resets downstream state (files, spans, progress, exports).
    # Conditional on the session still being idle: a claim taken by /handle
    # between the check above and here must not be silently overwritten.
    intake_claim_filter = dict(_owned_filter(sid, principal))
    intake_claim_filter["status"] = {"$nin": list(_LIVE_STATUSES)}
    reset = await db.sessions.update_one(
        intake_claim_filter,
        {
            "$set": {
                "files": [f.model_dump() for f in accepted],
                "progress": [],
                "export_paths": {},
                "intake_status": manifest.status,
                "intake_exit_code": manifest.exit_code,
                "intake_review": [
                    {"relpath": e.relpath, "reason": e.reason, "blocking": e.blocking}
                    for e in manifest.entries if e.component == "_unclassified"
                ],
                "intake_missing": manifest.missing_components,
                "status": "intake",
                "error": manifest.error,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            "$unset": {
                "reversal_key_blob": "",
                "reversal_key_created_at": "",
            },
        },
    )
    if not getattr(reset, "matched_count", 0):
        raise HTTPException(409, "session has a pipeline run in progress; cancel it before re-uploading")

    return {
        "study": sid,
        "status": manifest.status,
        "exit_code": manifest.exit_code,
        "linked": manifest.linked,
        "review": manifest.review,
        "errors": manifest.errors,
        "missing_components": manifest.missing_components,
        "review_entries": [
            {"relpath": e.relpath, "reason": e.reason, "blocking": e.blocking}
            for e in manifest.entries if e.component == "_unclassified"
        ],
        "accepted_by_component": {
            comp: [
                {"file_id": a.file_id, "name": a.file_id, "size": a.size_bytes, "sha256": a.sha256[:16]}
                for a in accepted if a.component == comp
            ]
            for comp in COMPONENT_SUFFIXES
        },
        "error": manifest.error,
    }


@app.get("/api/sessions/{sid}/intake/receipt")
async def session_intake_receipt(sid: str, principal: str = Depends(resolve_principal)):
    """CLI-style redacted receipt (never leaks entry paths).

    Mirrors the `phi_engine intake` stdout contract from
    feat/v2-multi-jurisdiction: {study, status, linked, review, errors, manifest}.
    """
    session = await _owned_session(sid, principal, {"_id": 0})
    review = session.get("intake_review") or []
    return {
        "study": sid,
        "status": session.get("intake_status", "none"),
        "linked": len(session.get("files") or []),
        "review": len(review),
        "errors": 0,
        "manifest": f"data/uploads/{sid}/unpacked/{sid}",
    }


@app.get("/api/intake/spec")
async def intake_spec():
    """Public spec for the intake-manifest/v3 ZIP structure."""
    return {
        "manifest_version": 3,
        "components": {
            k: {
                "extensions": sorted(v),
                "required": k in MANDATORY,
                "one_of_group": "forms_or_dictionary" if k in ANY_OF else None,
            }
            for k, v in COMPONENT_SUFFIXES.items()
        },
        "rules": [
            "datasets is mandatory",
            "at least one of forms or dictionary is required",
            "dictionary folder may be named: dictionary, mapping, mappings, codebook, workbook, data_dictionary (all aliases)",
            "dataset xlsx must be single-sheet",
            ".json and .jsonl are NOT accepted as datasets",
            "unsupported extensions land in the _unclassified review bucket and block the study",
            "symlinks and absolute paths in the ZIP are rejected",
            "per-file 200 MB cap",
        ],
        "exit_codes": {"0": "ready", "8": "review_required", "2": "failed"},
        "authority": "45 CFR 164.514(b)(2)(i) headers-only for datasets; classification runs across all components",
    }


@app.get("/api/sessions/{sid}/stream")
async def session_stream(sid: str, principal: str = Depends(resolve_principal)):
    # SEC-002 fix: refuse new subscribers for already-settled sessions so
    # attackers cannot open thousands of streams to random ids and pin a
    # queue per id. Settled sessions serve their history over the regular
    # GET endpoints; there is nothing more to stream.
    doc = await _owned_session(sid, principal, {"status": 1, "_pipeline_run_id": 1})
    if doc.get("status") in _SETTLED_STATUSES:
        raise HTTPException(status_code=409,
                            detail=f"session already settled ({doc.get('status')}); "
                                   "no more stream events will arrive")
    # D15: keyed by the session's current run_id when one exists, so a
    # human-review resume (a new run_id for the same session) subscribes
    # fresh rather than sharing a bucket with a superseded run.
    stream_key = _stream_key(sid, doc.get("_pipeline_run_id"))
    if _event_broker.subscriber_count(stream_key) >= _MAX_STREAM_SUBSCRIBERS_PER_SESSION:
        raise HTTPException(status_code=429,
                            detail="too many concurrent stream subscribers "
                                   "for this session")

    async def gen():
        sub = _event_broker.subscribe(stream_key)
        try:
            # HANG PROTECTION: emit an SSE keep-alive comment every 15 s so
            # browsers / proxies do not close the connection during a long
            # LLM call (Herald.Sections and Statute web-search can each run
            # >30 s without a message). The comment starts with ":" per SSE
            # spec so the EventSource on the client ignores it silently.
            while True:
                try:
                    event: dict = await asyncio.wait_for(sub.queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n"
                # `__end__` (the pipeline finished) and `__resync__` (this
                # subscriber overflowed and was told to refetch) both end
                # the stream; the browser's native EventSource reconnects
                # on its own, landing on a fresh `subscribe` call.
                if event.get("phase") in ("__end__", "__resync__"):
                    break
        finally:
            # SEC-002 fix: release the subscription on client disconnect or
            # stream end so an idle/nonexistent subscriber cannot pin
            # memory indefinitely.
            _event_broker.unsubscribe(sub)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/coverage-matrix")
async def coverage_matrix_endpoint():
    """Static coverage matrix used by the wizard result page and the bundle."""
    from phi_core.coverage_matrix import COVERAGE, TOOLS, coverage_counts
    return {"rows": COVERAGE, "tools": TOOLS, "counts": coverage_counts()}

@app.get("/api/admin/assurance", dependencies=[Depends(rate_limited("admin_assurance", 30, 60))])
async def admin_assurance(principal: str = Depends(resolve_principal)):
    """D15 step 5: one operator-facing snapshot of everything the control
    plane's own durability and policy machinery is currently unhappy
    about, gated to ``lead_reviewer`` since it surfaces cross-session
    operational detail (task ids, error text) no ordinary reviewer needs.

    Every list is capped at 50 rows, newest/most-relevant first -- this
    is a triage dashboard, not an export.
    """
    if reviewer_role(principal) != "lead_reviewer":
        raise HTTPException(403, "principal is not a lead_reviewer (see REVIEWER_PRINCIPALS)")
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()

    stuck_leases = await db.work_items.find(
        {"state": "leased", "lease_expires_at": {"$lt": now}},
        {"_id": 0, "task_id": 1, "task_type": 1, "run_id": 1, "lease_owner": 1, "lease_expires_at": 1},
    ).limit(50).to_list(length=None)

    denial_counts: dict[str, int] = {}
    total_denials = 0
    async for row in db.trace_events.find(
        {"outcome": {"$in": ["budget_exceeded", "denied"]}}, {"_id": 0, "outcome": 1, "status_text": 1},
    ).limit(2000):
        total_denials += 1
        reason = row.get("status_text") or row.get("outcome") or "unknown"
        denial_counts[reason] = denial_counts.get(reason, 0) + 1

    gate_failures = await db.gate_results.find(
        {"status": {"$in": ["fail", "blocked"]}},
        {"_id": 0, "gate_id": 1, "run_id": 1, "task_id": 1, "gate": 1, "status": 1, "detail": 1, "created_at": 1},
    ).sort("created_at", -1).limit(50).to_list(length=None)

    orphan_artifacts = await db.artifacts.find(
        {"$or": [{"state": "deletion_pending"}, {"delete_attempts": {"$gt": 0}}]},
        {"_id": 0, "artifact_id": 1, "session_id": 1, "run_id": 1, "state": 1,
         "delete_attempts": 1, "delete_error": 1},
    ).limit(50).to_list(length=None)

    erasure_failures = await db.sessions.find(
        {"status": "erasure_pending"},
        {"_id": 0, "id": 1, "erasure_error": 1, "erasure_attempts": 1, "updated_at": 1},
    ).limit(50).to_list(length=None)

    publication_outcomes = await db.publication_pointers.find(
        {}, {"_id": 0, "session_id": 1, "run_id": 1, "generation": 1, "certified_at": 1},
    ).sort("certified_at", -1).limit(50).to_list(length=None)

    return {
        "generated_at": now,
        "stuck_leases": stuck_leases,
        "policy_denials": {"total": total_denials, "by_reason": denial_counts},
        "gate_failures": gate_failures,
        "orphan_artifacts": orphan_artifacts,
        "erasure_failures": erasure_failures,
        "publication_outcomes": publication_outcomes,
    }


class AdminHoldBody(BaseModel):
    session_id: str
    reason: str = ""


async def _propagate_hold_to_artifacts(control_store, *, run_id: str, hold: str) -> None:
    """Apply ``hold`` to every ``ArtifactRecord`` belonging to ``run_id``.

    The hourly reconciler (``phi_core.control.artifacts.reconcile``) only
    consults ``ArtifactRecord.hold``, never the owning ``WorkflowRun.hold``
    set by admin_set_hold/admin_clear_hold -- so without this, an artifact
    from a held run is still eligible for deletion. Best-effort: a record
    that fails to replace (concurrent update) is retried on the next
    set/clear call rather than blocking the admin hold response.
    """
    from phi_core.control.records import ArtifactRecord

    records = await control_store.find_many("artifact_records", {"run_id": run_id})
    for doc in records:
        record = ArtifactRecord.model_validate(doc)
        if record.hold == hold:
            continue
        updated = record.model_copy(update={"hold": hold})
        await control_store.replace_one(
            "artifact_records", {"artifact_id": record.artifact_id}, updated
        )


async def _record_hold_trace_event(db, *, run_id: str, session_id: str, principal: str, reason: str, action: str) -> None:
    """Best-effort audit record for a hold set/clear: "set
    and clear events include principal and reason in a trace event".
    Non-terminal, so no fence is required."""
    from phi_core.control.events import EventAppendError, TraceEventStore
    from phi_core.control.records import TraceEvent
    from phi_core.control.store import MongoControlStore

    event = TraceEvent(
        run_id=run_id, seq=0, session_id=session_id, outcome=action,
        status_text=f"principal={principal} reason={reason}",
        input_class="internal", output_class="internal",
    )
    try:
        await TraceEventStore(MongoControlStore(db), run_id=run_id, session_id=session_id).append(event)
    except EventAppendError:
        pass


@app.post("/api/admin/hold", dependencies=[Depends(rate_limited("admin_hold", 30, 60))])
async def admin_set_hold(body: AdminHoldBody, principal: str = Depends(resolve_principal)):
    """Set a D14 legal/administrative hold on a session's
    run, suspending every retention timer that checks
    ``WorkflowRun.hold``/``ArtifactRecord.hold`` (terminal-session purge,
    review-retention expiry, artifact reconciliation, the reversal-key
    migration) until cleared. Gated to ``lead_reviewer``."""
    if reviewer_role(principal) != "lead_reviewer":
        raise HTTPException(403, "principal is not a lead_reviewer (see REVIEWER_PRINCIPALS)")
    if not body.reason.strip():
        raise HTTPException(400, "a hold requires a reason")
    db = get_db()
    doc = await db.sessions.find_one({"id": body.session_id}, {"_id": 0, "_pipeline_run_id": 1})
    if doc is None:
        raise HTTPException(404, "session not found")
    run_id = doc.get("_pipeline_run_id")
    if not run_id:
        raise HTTPException(409, "session has no durable run to hold")
    from phi_core.control.policy import CapabilityPolicy
    from phi_core.control.store import MongoControlStore
    from phi_core.control.superorchestrator import SuperOrchestrator
    from phi_core.control.tasks import TaskService
    from phi_core.control.workflow import WorkflowError

    control_store = MongoControlStore(db)
    try:
        await SuperOrchestrator(control_store, TaskService(control_store, CapabilityPolicy(None))).set_hold(
            run_id=run_id, reason=body.reason,
        )
    except WorkflowError as exc:
        raise HTTPException(404, "run not found") from exc
    await _propagate_hold_to_artifacts(control_store, run_id=run_id, hold=body.reason)
    await _record_hold_trace_event(
        db, run_id=run_id, session_id=body.session_id, principal=principal, reason=body.reason, action="hold_set",
    )
    return {"run_id": run_id, "hold": body.reason}


@app.delete("/api/admin/hold", dependencies=[Depends(rate_limited("admin_hold", 30, 60))])
async def admin_clear_hold(session_id: str, reason: str = "", principal: str = Depends(resolve_principal)):
    """Clear a hold set by `admin_set_hold`, resuming every suspended
    retention timer at the next sweep. Gated to `lead_reviewer`."""
    if reviewer_role(principal) != "lead_reviewer":
        raise HTTPException(403, "principal is not a lead_reviewer (see REVIEWER_PRINCIPALS)")
    db = get_db()
    doc = await db.sessions.find_one({"id": session_id}, {"_id": 0, "_pipeline_run_id": 1})
    if doc is None:
        raise HTTPException(404, "session not found")
    run_id = doc.get("_pipeline_run_id")
    if not run_id:
        raise HTTPException(409, "session has no durable run to clear a hold from")
    from phi_core.control.policy import CapabilityPolicy
    from phi_core.control.store import MongoControlStore
    from phi_core.control.superorchestrator import SuperOrchestrator
    from phi_core.control.tasks import TaskService
    from phi_core.control.workflow import WorkflowError

    control_store = MongoControlStore(db)
    try:
        await SuperOrchestrator(control_store, TaskService(control_store, CapabilityPolicy(None))).clear_hold(
            run_id=run_id,
        )
    except WorkflowError as exc:
        raise HTTPException(404, "run not found") from exc
    await _propagate_hold_to_artifacts(control_store, run_id=run_id, hold="")
    await _record_hold_trace_event(
        db, run_id=run_id, session_id=session_id, principal=principal, reason=reason, action="hold_cleared",
    )
    return {"run_id": run_id, "hold": ""}




_classification_accuracy_cache: dict[str, Any] | None = None


@app.get("/api/classification-accuracy", dependencies=[Depends(require_api_token)])
async def classification_accuracy_endpoint(details: bool = False):
    """Run the deterministic hard-rule layer over the shipped labelled corpus
    and return per-category precision/recall/F1 + method-appropriateness.

    Query params:
      - details=1 : include per-column predictions (useful for regression debugging).

    The full validation corpus never changes at runtime, so the result is
    memoised for the process lifetime rather than recomputed per request.
    """
    global _classification_accuracy_cache
    if _classification_accuracy_cache is None:
        from phi_core.validation import run_validation
        _classification_accuracy_cache = run_validation().to_dict()
    body = dict(_classification_accuracy_cache)
    if not details:
        body.pop("predictions", None)
    return body


def _clean_export_artifact_ids(session: dict) -> dict[str, str]:
    """Map file_id -> canonical (extension-less) artifact_id for every
    Publish-Guard-clean export in this session's most recent run.

    ``session["export_paths"]`` still holds Executor's suffix-bearing
    guard-scannable alias (see ``reasoning.py::Executor._finalize_export``);
    ``artifact_id_from_export_alias`` recovers the canonical, hash-tracked
    artifact_id from its basename with no filesystem access. Returns
    ``{}`` unless the aggregate guard report is ``"clean"``.
    """
    from phi_core.paths import artifact_id_from_export_alias

    guard = session.get("guard_report") or {}
    if guard.get("status") != "clean":
        return {}
    clean_ids = {r.get("file_id") for r in (guard.get("results") or []) if r.get("status") == "clean"}
    export_paths = session.get("export_paths") or {}
    out: dict[str, str] = {}
    for file_id in clean_ids:
        p = export_paths.get(file_id)
        if not p:
            continue
        artifact_id = artifact_id_from_export_alias(p)
        if artifact_id:
            out[file_id] = artifact_id
    return out


async def _open_published_artifact(service, sid: str, run_id: str, artifact_id: str,
                                    all_artifact_ids: list[str]):
    """Resolve one artifact through ``ArtifactService.open_for_download``,
    hash-bound to its ``artifact_id`` rather than any filesystem path.

    Nothing yet calls ``ArtifactService.certify_publication`` on the
    pipeline's behalf (that becomes Phase 5's
    ``SuperOrchestrator.authorize_publication``); until it lands, the
    first download request against a fresh Publish-Guard-clean result
    lazily certifies the *entire* current clean set together as one
    publication generation, so every clean file in this run shares one
    generation and a bundle download sees a mutually consistent set.
    Raises :class:`~phi_core.control.artifacts.ArtifactError` with its
    typed refusal reason (``artifact_missing``, ``artifact_hash_mismatch``,
    ...) on any refusal that recertifying cannot fix.
    """
    from phi_core.control.artifacts import ArtifactError

    try:
        return await service.open_for_download(sid, artifact_id)
    except ArtifactError as exc:
        if exc.reason not in ("artifact_not_promoted", "generation_mismatch"):
            raise
    try:
        await service.certify_publication(
            run_id=run_id, artifact_ids=all_artifact_ids, gate_result_ids=[],
            fence=int(time.time() * 1_000_000),
        )
    except ArtifactError as exc:
        if exc.reason != "stale_fence":
            raise
        # A concurrent request already certified an equal-or-newer
        # generation covering the same artifact set; fall through and let
        # the re-open below settle against whichever generation won.
    return await service.open_for_download(sid, artifact_id)


def _artifact_service(db, sid: str, run_id: str):
    from phi_core.control.artifacts import ArtifactService
    from phi_core.control.store import MongoControlStore
    return ArtifactService(MongoControlStore(db), session_id=sid, run_id=run_id)


async def _verify_clean_artifacts(db, sid: str, session: dict,
                                   clean_ids: dict[str, str]) -> dict[str, Path]:
    """Hash-verify every currently clean export through the artifact
    registry before serving a bundle or a reversal key, raising
    ``HTTPException(409)`` on a genuine artifact refusal (missing bytes,
    hash mismatch). A no-op when there is nothing clean to verify.

    Returns ``{file_id: published_path}`` for the paths that were actually
    hash-verified, so a caller (``build_bundle``) can read bundle bytes from
    those exact verified paths rather than from ``session["export_paths"]``'s
    staging alias (an independent file after promotion, not what the hash
    check above verified).
    """
    from phi_core.control.artifacts import ArtifactError

    if not clean_ids:
        return {}
    run_id = session.get("_pipeline_run_id") or sid
    service = _artifact_service(db, sid, run_id)
    all_ids = list(clean_ids.values())
    verified: dict[str, Path] = {}
    for file_id, artifact_id in clean_ids.items():
        try:
            verified[file_id] = await _open_published_artifact(
                service, sid, run_id, artifact_id, all_ids)
        except ArtifactError as exc:
            raise HTTPException(409, f"export artifact unavailable: {exc.reason}") from exc
    return verified


@app.get("/api/sessions/{sid}/bundle")
async def session_bundle(sid: str, publication: bool = False, attestation_pdf: bool = False,
                         principal: str = Depends(resolve_principal)):
    """Assemble and stream the shareable bundle.

    Query params:
      - publication=1 : include the publication/ folder (coverage tables +
        figures + paper drafts + benchmark scaffold).
      - attestation_pdf=1 : reserved for signed PDF attestation.
    """
    from phi_core.bundle import BundleOptions, build_bundle
    db = get_db()
    session = await _owned_session(sid, principal, {"_id": 0})
    if session.get("status") not in ("complete", "partially_complete"):
        raise HTTPException(
            403,
            "Publish Guard has not certified this session as clean "
            f"(session status={session.get('status') or 'missing'}). Re-run "
            "the pipeline so the last-mile PHI scan populates a passing guard report.",
        )
    guard = session.get("guard_report") or {}
    guard_status = guard.get("status")
    # SEC-001 fix: fail-closed. Missing guard result → refuse. Only serve
    # bundles built from a fully-scanned "clean" pipeline. `blocked` and
    # any other unrecognised status remain refused.
    if guard_status != "clean":
        raise HTTPException(
            403,
            "Publish Guard has not certified this session as clean "
            f"(status={guard_status or 'missing'}). Re-run the pipeline "
            "so the last-mile PHI scan populates a passing guard report.",
        )
    # D14: bind the certified guard status to a hash-verified artifact
    # before assembling anything -- a tampered or missing export on disk
    # refuses the whole bundle rather than silently shipping stale bytes.
    verified_paths = await _verify_clean_artifacts(db, sid, session, _clean_export_artifact_ids(session))
    agent_log_msgs = None
    if publication and session.get("corpus_ground_truth"):
        agent_log_msgs = await _session_trace_messages(db, sid)
    data, filename = build_bundle(session, BundleOptions(
        include_publication=publication, include_attestation_pdf=attestation_pdf,
    ), agent_log=agent_log_msgs, verified_paths=verified_paths)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/sessions/{sid}/reversal-key")
async def session_reversal_key(sid: str, principal: str = Depends(resolve_principal)):
    """Download the reversal key: the mapping that would let the study
    team re-identify their own pseudonymized data. This is the second
    mandatory deliverable alongside the PHI-handled bundle, gated the same
    way (session owned by the caller, Publish Guard clean).

    Retention: once downloaded, the blob is deleted from the session
    document. It also never survives session erasure regardless of whether
    it was downloaded (DELETE /sessions/{sid} removes the whole session
    document). This console does not keep a study's re-identification key
    on file indefinitely by default.
    """
    from phi_core.crypto import decrypt_reversal_map
    db = get_db()
    session = await _owned_session(sid, principal, {"_id": 0})
    if session.get("status") not in ("complete", "partially_complete"):
        raise HTTPException(403, "This session has not completed a clean run yet.")
    guard = session.get("guard_report") or {}
    if guard.get("status") != "clean":
        raise HTTPException(403, "Publish Guard has not certified this session as clean.")
    # D14: the reversal key is only meaningful alongside a hash-verified
    # publication -- if any clean-guarded export has since been tampered
    # with or gone missing on disk, refuse the key too rather than trust
    # the (mutable) guard_report field alone.
    await _verify_clean_artifacts(db, sid, session, _clean_export_artifact_ids(session))
    blob = session.get("reversal_key_blob")
    if not blob:
        raise HTTPException(404, "No reversal key was generated for this run (no column was "
                                 "pseudonymized or hashed, so there is nothing to reverse).")
    payload = decrypt_reversal_map(blob)
    await db.sessions.update_one(_owned_filter(sid, principal), {"$unset": {"reversal_key_blob": ""}})
    data = json.dumps({"session_id": sid, "salt": payload.get("salt", ""),
                       "pseudonym_map": payload.get("map", {})}, indent=2).encode()
    return Response(
        content=data,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{sid}_reversal_key.json"'},
    )


@app.get("/api/sessions/{sid}/export/{file_id}")
async def session_export(sid: str, file_id: str, principal: str = Depends(resolve_principal)):
    """Download the PHI-handled export.

    GOAL boundary: this is the point where 'input PHI data' becomes 'output
    ready to share publicly'. Refuse the download unless exactly one
    Publish Guard result marked this specific file 'clean'. There is no
    override: a `blocked` per-file result is unconditionally unservable,
    regardless of any caller-supplied parameter -- this route accepts none.
    """
    db = get_db()
    session = await _owned_session(sid, principal, {"_id": 0})
    if session.get("status") not in ("complete", "partially_complete"):
        return JSONResponse(status_code=403, content={
            "error": "publish_guard_not_certified",
            "message": (
                "Publish Guard has not certified this file as clean "
                f"(session status={session.get('status') or 'missing'}). Re-run "
                "the pipeline so the last-mile PHI scan populates a passing result."
            ),
            "guard": None,
        })
    guard = session.get("guard_report") or {}
    matching_results = [
        r for r in (guard.get("results") or [])
        if r.get("file_id") == file_id
    ]
    per_file = matching_results[0] if len(matching_results) == 1 else None
    status = per_file.get("status") if per_file else None
    # SEC-001 fix: fail-closed. Serve only if this file has exactly one
    # per-file guard result of `clean`. Missing, duplicate, `skipped`, or
    # `blocked` results refuse unconditionally -- there is no override.
    if status != "clean":
        return JSONResponse(status_code=403, content={
            "error": "publish_guard_not_certified",
            "message": (
                "Publish Guard has not certified this file as clean "
                f"(status={status or 'missing'}). Re-run the pipeline so "
                "the last-mile PHI scan populates a passing result."
            ),
            "guard": per_file,
        })
    # D14: resolve and serve the canonical, hash-tracked artifact through
    # ArtifactService.open_for_download rather than a raw filesystem path
    # lookup -- the served bytes, path, and filename are all keyed by
    # artifact_id alone.
    clean_ids = _clean_export_artifact_ids(session)
    artifact_id = clean_ids.get(file_id)
    if not artifact_id:
        raise HTTPException(404, "export not ready")
    run_id = session.get("_pipeline_run_id") or sid
    from phi_core.control.artifacts import ArtifactError
    service = _artifact_service(db, sid, run_id)
    try:
        path = await _open_published_artifact(service, sid, run_id, artifact_id, list(clean_ids.values()))
    except ArtifactError as exc:
        raise HTTPException(409, f"export artifact unavailable: {exc.reason}") from exc
    return FileResponse(path, filename=artifact_id)


# --- LLM settings (BYO-key) ----------------------------------------------



class LlmSettings(BaseModel):
    # Settings UI advertises openrouter|openai|anthropic|gemini only.
    # Legacy values remain accepted so existing Mongo docs still load.
    provider: Literal[
        "openrouter", "openai", "anthropic", "gemini",
        "openai_compatible", "chatgpt",
    ] = "openai"
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.1
    max_tokens: int = 2000


def _first_boot_llm_defaults() -> dict:
    """First-boot defaults resolved from the environment.

    So a deploy with only ``OPENAI_API_KEY`` (or Anthropic / Gemini /
    OpenRouter) gets a matching provider + catalog model without the
    operator touching Settings first.
    """
    from phi_core.agents.llm import _default_provider
    from phi_core.llm_catalog import default_model_for
    provider = _default_provider()
    # Settings no longer advertises ChatGPT-OAuth as a first-boot default;
    # map it onto the OpenAI API provider.
    if provider == "chatgpt":
        provider = "openai"
    return LlmSettings(
        provider=provider,
        model=default_model_for(provider),
    ).model_dump()


def _env_available_providers() -> list[str]:
    """Return the providers whose credentials are present in the environment.

    Order matches Settings UI: Open Router, ChatGPT, Claude, Gemini.
    """
    out: list[str] = []
    if os.environ.get("OPENROUTER_API_KEY"):
        out.append("openrouter")
    if os.environ.get("OPENAI_API_KEY"):
        out.append("openai")
    if os.environ.get("ANTHROPIC_API_KEY"):
        out.append("anthropic")
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        out.append("gemini")
    return out


def _providers_payload() -> dict:
    """Compose the providers block for /api/settings/llm.

    ``providers`` = the four Settings UI providers (stable order).
    ``env_providers`` = subset with credentials already present in the
    pod environment (zero-setup path).
    """
    from phi_core.llm_catalog import UI_PROVIDERS
    env = _env_available_providers()
    listed = [pid for pid, _label in UI_PROVIDERS if pid in allowed_providers()]
    return {
        "providers": listed,
        "env_providers": [p for p in listed if p in env],
    }


@app.get("/api/settings/llm", dependencies=[Depends(require_api_token)])
async def get_llm_settings():
    from phi_core.crypto import KeyRotated
    from phi_core.llm_catalog import default_model_for
    db = get_db()
    doc = await db.settings.find_one({"_id": "llm"}, {"_id": 0})
    if not doc:
        return _first_boot_llm_defaults() | _providers_payload()
    # Normalize legacy provider ids for the four-provider Settings menu.
    if doc.get("provider") == "chatgpt":
        doc["provider"] = "openai"
    elif doc.get("provider") == "emergent":
        # Legacy session document from before Emergent support was
        # removed. Present as Claude so the Settings dropdown stays valid.
        doc["provider"] = "anthropic"
    if not str(doc.get("model") or "").strip():
        doc["model"] = default_model_for(doc.get("provider") or "openai")
    # never leak the api_key back verbatim
    if doc.get("api_key"):
        try:
            decrypt_api_key(doc["api_key"])
        except KeyRotated as exc:
            raise HTTPException(409, "provider key cannot be decrypted; re-enter it in Settings") from exc
        doc["api_key_set"] = True
        doc["api_key"] = ""
    return doc | _providers_payload()


@app.get("/api/settings/llm/catalog", dependencies=[Depends(require_api_token)])
async def get_llm_catalog():
    """Curated multi-provider model catalog for the Settings UI.

    Lets operators pick a model from a real list (grouped by provider,
    with tier and web-search-capability flags) instead of typing a raw
    model ID. Returns provider families and their members from
    ``phi_core/llm_catalog.py``.
    """
    from phi_core.llm_catalog import catalog_for_ui
    return catalog_for_ui()


@app.get("/api/corpus/study-data", dependencies=[Depends(require_api_token)])
async def corpus_study_data_list():
    """List hand-curated static study packages under ``phi_corpus/study_data/``."""
    from phi_corpus.study_data import list_packages
    return {"packages": list_packages()}


@app.get("/api/corpus/study-data/{package_id}/zip", dependencies=[Depends(require_api_token)])
async def corpus_study_data_zip(package_id: str):
    """Download a curated package as a manifest-v3 intake ZIP."""
    from phi_corpus.study_data import build_intake_zip
    try:
        zip_bytes = build_intake_zip(package_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, f"unknown study-data package: {package_id}") from exc
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{package_id}.zip"',
        },
    )


# --- Corpus generator + verifier -------------------------------------------
#
# The corpus is a red-team torture-test rig: PHI is planted in realistic
# study data, run through the pipeline, and every decision compared
# against the planted ground truth. Ground truth stays in the session
# document only (Sir's Q1(iii)) — it is never persisted to disk.


@app.get("/api/corpus/study/catalog", dependencies=[Depends(require_api_token)])
async def corpus_study_catalog():
    from phi_corpus.edge_cases import EDGE_CASES, HIPAA_MAX_EDGE_CASE_TAGS
    from phi_corpus.scenarios import list_scenarios
    db = get_db()
    # Discovered scenarios (from CorpusResearcher) live alongside the
    # hand-curated library so the catalog reflects both.
    researched = []
    async for doc in db.corpus_scenarios.find({}, {"_id": 0}):
        researched.append({
            "id": doc.get("scenario_id"),
            "label": doc.get("label"),
            "source": "researcher",
            "jurisdictions": doc.get("jurisdictions", []),
            "source_study": doc.get("source_study"),
        })
    return {
        "scenarios": list_scenarios() + researched,
        "edge_cases": [
            {"tag": e.tag, "label": e.label, "applies_to_column": e.applies_to_column}
            for e in EDGE_CASES.values()
        ],
        "presets": {
            "hipaa_max_adversarial": {
                "scenario_id": "hipaa_max_adversarial_v1",
                "edge_case_tags": list(HIPAA_MAX_EDGE_CASE_TAGS),
                "label": "Every HIPAA A-R identifier + every torture edge case",
            },
        },
    }


class CorpusStudyResearchBody(BaseModel):
    domain: str


@app.post("/api/corpus/study/research", dependencies=[Depends(require_api_token), Depends(rate_limited("corpus_research", 5, 3600))])
async def corpus_study_research(body: CorpusStudyResearchBody):
    """Run the CorpusResearcher agent to discover a real-life scenario
    for the requested study domain and persist it to the ``corpus_scenarios``
    Mongo collection so it survives restart and shows up in the catalog.
    """
    from phi_core.control.activation import ActivationFactory
    from phi_core.control.policy import CapabilityPolicy
    from phi_core.control.store import MongoControlStore
    from phi_core.control.superorchestrator import SuperOrchestrator
    from phi_core.control.tasks import TaskService
    from phi_corpus.researcher import CorpusResearcher
    db = get_db()
    cfg = await _current_llm_cfg()
    control_store = MongoControlStore(db)
    run_id = uuid.uuid4().hex
    # Phase 5 step 2: a real WorkflowRun (run_type="maintenance"), not just
    # the ActivationFactory-issued grant CorpusResearcher already had --
    # this is what makes D5's run-level bounds (MAX_TOKENS_PER_RUN and
    # friends) apply to a research call at all.
    await SuperOrchestrator(control_store, TaskService(control_store, CapabilityPolicy(cfg))).start_run(
        session_id="corpus-researcher", principal="api-token", run_type="maintenance", run_id=run_id,
    )
    factory = ActivationFactory(db, cfg, store=control_store)
    ctx = await factory.activate(session_id="corpus-researcher", run_id=run_id, agent="CorpusResearcher")
    agent = CorpusResearcher(ctx)
    reply = await agent.research(body.domain)

    if reply.get("error"):
        # Do not persist an errored/ungrounded scenario.
        raise HTTPException(422, f"corpus researcher: {reply.get('error')}")

    # Persist keyed by scenario_id so repeated research on the same domain
    # updates the same row.
    sid_key = reply.get("scenario_id") or body.domain.strip().lower()
    reply["scenario_id"] = sid_key
    await db.corpus_scenarios.update_one(
        {"scenario_id": sid_key},
        {"$set": {**reply, "domain": body.domain, "updated_at":
                  datetime.now(timezone.utc).isoformat()}},
        upsert=True,
    )
    return reply


class CorpusStudyGenerateBody(BaseModel):
    scenario_id: str
    jurisdiction: str = "us"
    edge_case_tags: list[str] = []
    row_count: int = 12
    seed: int = 42


@app.post("/api/corpus/study/generate", dependencies=[Depends(rate_limited("corpus_generate", 20, 3600))])
async def corpus_study_generate(body: CorpusStudyGenerateBody, principal: str = Depends(resolve_principal)):
    """Generate a corpus in memory and attach it to a fresh session so the
    intake -> handle -> verify flow can run entirely from a single call.

    Returns the new ``session_id``, the ground-truth summary, and the
    upload token needed to trigger the intake step. Ground truth itself
    is stored in the session document under ``corpus_ground_truth`` and
    never emitted to a filesystem path.
    """
    from phi_corpus.planters import plant
    art = plant(
        scenario_id=body.scenario_id,
        jurisdiction=body.jurisdiction,
        edge_case_tags=body.edge_case_tags,
        row_count=max(10, min(int(body.row_count or 12), 100)),
        seed=int(body.seed or 42),
    )
    # Reuse the existing session-create flow to get a canonical session
    # document with all defaults populated.
    sid = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    db = get_db()
    session_dir = UPLOAD_DIR / sid
    session_dir.mkdir(parents=True, exist_ok=True)
    zip_path = safe_join(session_dir, "intake.zip", fallback="intake.zip")
    zip_path.write_bytes(art.zip_bytes)
    session_doc = {
        "id": sid,
        "created_at": now,
        "status": "corpus_ready",
        "owner": principal,
        "jurisdiction": body.jurisdiction,
        "files": [],
        "agent_decisions": [],
        "corpus_ground_truth": art.ground_truth,
        "corpus_summary": art.ground_truth_summary,
        "corpus_zip_path": str(zip_path),
    }
    await db.sessions.insert_one(session_doc)


    return {
        "session_id": sid,
        "jurisdiction": body.jurisdiction,
        "scenario_id": body.scenario_id,
        "edge_case_tags": body.edge_case_tags,
        "summary": art.ground_truth_summary,
        "corpus_zip_size_bytes": len(art.zip_bytes),
    }

@app.get("/api/corpus/study/{sid}/zip")
async def corpus_study_zip(sid: str, principal: str = Depends(resolve_principal)):
    """Download the intake ZIP produced by a corpus generate/run, so the
    described handoff (generate, then feed the ZIP to the pipeline) is
    actually possible rather than only computing a byte count."""
    doc = await _owned_session(sid, principal, {"_id": 0})
    zip_path = doc.get("corpus_zip_path")
    if not zip_path or not Path(zip_path).exists():
        raise HTTPException(404, "corpus zip not found")
    scenario_id = (doc.get("corpus_ground_truth") or {}).get("scenario_id", "corpus")
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"corpus_{scenario_id}_{sid[:8]}.zip",
    )


class CorpusStudyRunBody(BaseModel):
    scenario_id: str
    jurisdiction: str = "us"
    edge_case_tags: list[str] = []
    row_count: int = 12
    seed: int = 42
    # Same rigor selector the Wizard exposes. Balanced (2) is the default
    # so corpus runs match a typical operator run's iteration count
    # instead of silently maxing to 3.
    iteration_cap: int = 2


@app.post("/api/corpus/study/run", dependencies=[Depends(rate_limited("corpus_run", 20, 3600))])
async def corpus_study_run(body: CorpusStudyRunBody, principal: str = Depends(resolve_principal)):
    """One-shot: create session, plant corpus, run intake + pipeline.

    Piggybacks on the existing intake + orchestrator paths so the corpus
    goes through exactly the same code path as an operator-uploaded
    manifest. Client polls ``GET /api/sessions/{sid}`` and, once
    ``status='complete'``, calls ``GET /api/corpus/study/verify/{sid}``.
    """
    from phi_core.intake import build_manifest
    from phi_corpus.planters import plant

    art = plant(
        scenario_id=body.scenario_id,
        jurisdiction=body.jurisdiction,
        edge_case_tags=body.edge_case_tags,
        row_count=max(10, min(int(body.row_count or 12), 100)),
        seed=int(body.seed or 42),
    )
    sid = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    db = get_db()

    # Persist session with ground truth first (idempotent).
    await db.sessions.insert_one({
        "id": sid, "created_at": now, "status": "intake", "owner": principal,
        "jurisdiction": body.jurisdiction,
        "files": [], "agent_decisions": [],
        "corpus_ground_truth": art.ground_truth,
        "corpus_summary": art.ground_truth_summary,
    })

    # Mirror the /intake endpoint flow so the corpus travels through the
    # same code path as an operator upload.
    session_dir = UPLOAD_DIR / sid
    session_dir.mkdir(parents=True, exist_ok=True)
    zip_path = safe_join(session_dir, "intake.zip", fallback="intake.zip")
    zip_path.write_bytes(art.zip_bytes)

    manifest = build_manifest(sid, zip_path, session_dir / "unpacked")
    accepted: list[FileArtifact] = []
    for e in manifest.entries:
        if e.component == "_unclassified":
            continue
        ext = Path(e.relpath).suffix.lstrip(".").lower()
        if e.component == "datasets":
            kind = "dataset"
        elif e.component == "forms":
            kind = "narrative"
        else:
            kind = "metadata"
        accepted.append(FileArtifact(
            original_name_encrypted=encrypt_display_name(Path(e.relpath).name),
            size_bytes=e.size_bytes, sha256=e.sha256,
            kind=kind, subtype=ext,
            stored_path=e.stored_path, component=e.component,
        ))

    await db.sessions.update_one(
        _owned_filter(sid, principal),
        {"$set": {
            "files": [f.model_dump() for f in accepted],
            "intake_status": manifest.status,
            "intake_exit_code": manifest.exit_code,
            "intake_missing": manifest.missing_components,
            "status": "intake",
            "error": manifest.error,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
    )
    if manifest.status != "ready":
        raise HTTPException(400, f"corpus intake not ready: {manifest.status} / {manifest.error}")

    # Delegate to the existing /handle endpoint so the corpus goes through
    # the exact same 12-agent pipeline path as an operator-uploaded run.
    await session_handle(sid, iteration_cap=body.iteration_cap, principal=principal)

    return {
        "session_id": sid, "status": "started",
        "scenario_id": body.scenario_id,
        "jurisdiction": body.jurisdiction,
        "edge_case_tags": body.edge_case_tags,
        "summary": art.ground_truth_summary,
    }


@app.get("/api/corpus/study/verify/{sid}")
async def corpus_study_verify(sid: str, principal: str = Depends(resolve_principal)):
    """Compare the pipeline's actual decisions against the corpus ground
    truth stored on the session document. Returns the full scored report
    from :func:`phi_corpus.verify.verify`."""
    from phi_corpus.verify import verify as _verify
    doc = await _owned_session(sid, principal, {"_id": 0})
    gt = doc.get("corpus_ground_truth")
    if not gt:
        raise HTTPException(400, "session has no corpus_ground_truth (not a corpus run)")

    # Map ground-truth file names to the pipeline's file_id so the
    # verifier can look up decisions correctly.
    name_map: dict[str, str] = {
        f.get("file_id", ""): f.get("file_id", "")
        for f in doc.get("files") or []
    }
    report = _verify(
        gt,
        doc.get("agent_decisions") or [],
        file_name_map=name_map,
        export_paths=doc.get("export_paths") or {},
        guard_report=doc.get("guard_report") or {},
    )
    report["session_id"] = sid
    report["status"] = doc.get("status")
    return report


def _build_corpus_benchmark_report(doc: dict, agent_log_msgs: list[dict]) -> dict:
    """Thin HTTP wrapper around :func:`phi_corpus.benchmark.report_from_session`,
    which does the actual file_id-to-file_name remap and report assembly.
    Shared with the publication bundle (phi_core.bundle)."""
    from phi_corpus.benchmark import report_from_session

    report = report_from_session(doc, agent_log_msgs)
    if report is None:
        raise HTTPException(400, "session has no corpus_ground_truth (not a corpus run)")
    return report


@app.get("/api/corpus/study/benchmark/{sid}")
async def corpus_study_benchmark(sid: str, principal: str = Depends(resolve_principal)):
    """Per-dataset benchmark report for a corpus run: per column, the
    method chosen, why, how, confidence, gold verdict, plus headline
    leak/precision/autonomy figures. See :func:`phi_corpus.benchmark.build_report`."""
    db = get_db()
    doc = await _owned_session(sid, principal, {"_id": 0})
    agent_log_msgs = await _session_trace_messages(db, sid)
    return _build_corpus_benchmark_report(doc, agent_log_msgs)


@app.get("/api/corpus/study/benchmark/{sid}/download")
async def corpus_study_benchmark_download(sid: str, principal: str = Depends(resolve_principal)):
    """Download the six benchmark artefacts (markdown, JSON, CSV, three
    PNGs) as one ZIP."""
    from phi_corpus.benchmark import bundle_zip

    db = get_db()
    doc = await _owned_session(sid, principal, {"_id": 0})
    agent_log_msgs = await _session_trace_messages(db, sid)
    report = _build_corpus_benchmark_report(doc, agent_log_msgs)
    scenario_id = (doc.get("corpus_ground_truth") or {}).get("scenario_id", "corpus")
    zip_bytes = bundle_zip(report)
    return Response(
        content=zip_bytes, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="benchmark_{scenario_id}_{sid[:8]}.zip"'},
    )


@app.post("/api/settings/llm", dependencies=[Depends(require_api_token)])
async def set_llm_settings(body: LlmSettings):
    if not body.model.strip():
        raise HTTPException(422, "select a model before saving LLM settings")
    validate_llm_provider(body.provider)
    validate_llm_base_url(body.base_url, body.provider)
    if body.provider == "chatgpt" and chatgpt_auth.read_auth() is None:
        raise HTTPException(
            400, "ChatGPT account not connected; run POST /api/settings/chatgpt/login first"
        )
    db = get_db()
    payload = body.model_dump()
    # Encrypt at rest (SEC-003). Empty string means "keep existing".
    existing = await db.settings.find_one({"_id": "llm"}, {"_id": 0}) or {}
    if payload.get("api_key"):
        payload["api_key"] = encrypt_api_key(payload["api_key"])
    else:
        payload["api_key"] = existing.get("api_key", "")
    await db.settings.replace_one({"_id": "llm"}, {"_id": "llm", **payload}, upsert=True)
    return {"ok": True}


async def _current_llm_cfg() -> LlmConfig:
    from phi_core.crypto import KeyRotated
    db = get_db()
    doc = await db.settings.find_one({"_id": "llm"}, {"_id": 0}) or {}
    if not doc or not str(doc.get("model") or "").strip():
        # First pipeline run before Settings save: seed from env + catalog.
        seeded = _first_boot_llm_defaults()
        doc = {**seeded, **{k: v for k, v in doc.items() if v not in (None, "")}}

    # Settings "ChatGPT" is the OpenAI API. Legacy OAuth ``chatgpt`` docs
    # keep the OAuth path only while a live auth file remains.
    if doc.get("provider") == "chatgpt":
        if chatgpt_auth.read_auth() is None:
            doc = {**doc, "provider": "openai"}
        else:
            doc = {**doc, "api_key": "", "base_url": ""}
    # Fold legacy ``emergent`` docs onto Claude/Anthropic now that
    # Emergent Universal Key support has been removed.
    elif doc.get("provider") == "emergent":
        doc = {**doc, "provider": "anthropic"}

    if doc.get("provider") != "chatgpt" and doc.get("api_key"):
        try:
            doc["api_key"] = decrypt_api_key(doc["api_key"])
        except KeyRotated:
            # Background workers cannot surface a 409 to a browser; degrade
            # to an empty key so the call fails at the provider auth
            # boundary with a clear error, same as no key configured.
            doc["api_key"] = ""
    return LlmConfig.from_dict(doc)



class ChatGptLoginPollOut(BaseModel):
    status: str
    detail: str = ""
    account_id: str = ""


# Process-local device-login state, keyed by an opaque id handed to the
# browser. Intentionally not persisted in Mongo: a device code is valid
# for 15 minutes, so a server restart should force a fresh login rather
# than resume polling a code that may already be dead.
_chatgpt_logins: dict[str, chatgpt_auth.DeviceLogin] = {}


def _prune_chatgpt_logins() -> None:
    """D15 4b: expire entries past ``chatgpt_auth.DEVICE_CODE_EXPIRES_IN_S``
    and, if still at the cap after that, evict the single oldest survivor
    to make room -- an operator starting a new login never gets refused,
    but the dict never grows without bound either."""
    now = time.time()
    expired = [lid for lid, login in _chatgpt_logins.items()
               if now - login.started_at > chatgpt_auth.DEVICE_CODE_EXPIRES_IN_S]
    for lid in expired:
        del _chatgpt_logins[lid]
    if len(_chatgpt_logins) >= limits.MAX_CHATGPT_LOGINS:
        oldest = min(_chatgpt_logins, key=lambda lid: _chatgpt_logins[lid].started_at)
        del _chatgpt_logins[oldest]


@app.post("/api/settings/chatgpt/login", dependencies=[Depends(require_api_token)])
async def chatgpt_login_start():
    _prune_chatgpt_logins()
    login = await chatgpt_auth.start_device_login()
    login_id = uuid.uuid4().hex
    _chatgpt_logins[login_id] = login
    return {
        "login_id": login_id,
        "user_code": login.user_code,
        "verify_url": login.verify_url,
        "interval_s": login.interval_s,
        "expires_in_s": 900,
    }


@app.get("/api/settings/chatgpt/login/{login_id}", dependencies=[Depends(require_api_token)])
async def chatgpt_login_poll(login_id: str) -> ChatGptLoginPollOut:
    login = _chatgpt_logins.get(login_id)
    if login is None:
        return ChatGptLoginPollOut(status="error", detail="unknown login_id")
    # One poll per request -- never loop server-side, which is exactly
    # the 15-minute blocking hazard this endpoint exists to avoid.
    login = await chatgpt_auth.poll_once(login)
    _chatgpt_logins[login_id] = login
    account_id = ""
    if login.status == "connected":
        account_id = chatgpt_auth.auth_status().get("account_id", "")
    return ChatGptLoginPollOut(status=login.status, detail=login.detail, account_id=account_id)


@app.get("/api/settings/chatgpt/status", dependencies=[Depends(require_api_token)])
async def chatgpt_status():
    return chatgpt_auth.auth_status()


@app.delete("/api/settings/chatgpt", dependencies=[Depends(require_api_token)])
async def chatgpt_disconnect():
    chatgpt_auth.clear_auth()
    return {"ok": True}


async def _run_warmup(db, cfg) -> dict:
    """Run Statute + all 17 Praxis warmups. Shared by manual and scheduled paths."""
    from phi_core.agents.experts import Praxis, Statute
    from phi_core.control.activation import ActivationFactory
    from phi_core.control.policy import CapabilityPolicy
    from phi_core.control.store import MongoControlStore
    from phi_core.control.superorchestrator import SuperOrchestrator
    from phi_core.control.tasks import TaskService

    warmup_sid = f"warmup:{uuid.uuid4().hex[:8]}"
    warmup_run_id = uuid.uuid4().hex

    async def _noop_emit(_msg):  # pragma: no cover - trivial
        return None

    control_store = MongoControlStore(db)
    # Phase 5 step 2: a real WorkflowRun (run_type="warmup") so D5's
    # run-level bounds apply across these 18 provider calls, not only the
    # per-task ceiling each ActivationFactory-issued grant already had.
    await SuperOrchestrator(control_store, TaskService(control_store, CapabilityPolicy(cfg))).start_run(
        session_id=warmup_sid, principal="api-token", run_type="warmup", run_id=warmup_run_id,
    )
    factory = ActivationFactory(db, cfg, store=control_store)
    hipaa_cats = ["A", "B", "C", "D", "F", "G", "H", "I", "J", "K",
                  "L", "M", "N", "O", "P", "Q", "R"]

    async def _activate(agent: str):
        return await factory.activate(session_id=warmup_sid, run_id=warmup_run_id, agent=agent, emit=_noop_emit)

    praxis_agent = Praxis(await _activate("Praxis"))
    statute_task = Statute(await _activate("Statute")).run(jurisdiction="us")
    praxis_task = asyncio.gather(
        *[praxis_agent.method_for(c) for c in hipaa_cats],
        return_exceptions=True,
    )
    statute_res, praxis_res = await asyncio.gather(statute_task, praxis_task)
    praxis_ok = [c for c, r in zip(hipaa_cats, praxis_res, strict=True)
                 if not isinstance(r, Exception)]
    praxis_err = [{"category": c, "error": type(r).__name__}
                  for c, r in zip(hipaa_cats, praxis_res, strict=True)
                  if isinstance(r, Exception)]
    # Praxis is called via `method_for`, never `run`, so it never gets
    # `Agent.__init_subclass__`'s completion wrap; complete its one shared
    # task explicitly, matching orchestrator.py's per-category equivalent.
    if praxis_agent.ctx.tasks is not None:
        await praxis_agent.ctx.tasks.complete({"primed": praxis_ok, "failed": praxis_err})
    return {
        "ok": True,
        "statute": {"jurisdiction": statute_res.get("jurisdiction", "us"),
                    "as_of": statute_res.get("as_of", "cache")},
        "praxis": {
            "primed": praxis_ok,
            "failed": praxis_err,
            "total": len(hipaa_cats),
        },
    }


@app.post("/api/settings/warmup", dependencies=[Depends(require_api_token), Depends(rate_limited("settings_warmup", 5, 3600))])
async def settings_warmup():
    """Prime the Statute + Praxis caches for supported jurisdictions.

    Sir Q "Cold-Cache Warmup": the first study of the day pays for 10+ web
    searches (Praxis E, I..R) plus Statute. This endpoint pre-runs those
    with an ephemeral session id so the caches are hot before an operator
    kicks off a real run. Uses the current LLM config and returns per-task
    outcome so the UI can report which categories cached vs. failed.

    Only US-HIPAA is warmed for now; extra jurisdictions light up
    automatically once `jurisdictions.py` graduates them from stub.
    """
    db = get_db()
    cfg = await _current_llm_cfg()
    try:
        return await asyncio.wait_for(_run_warmup(db, cfg), timeout=240.0)
    except asyncio.TimeoutError as exc:
        raise HTTPException(504, "warmup exceeded 240s ceiling") from exc


class AutoWarmupCfg(BaseModel):
    enabled: bool = False


@app.get("/api/settings/warmup/schedule", dependencies=[Depends(require_api_token)])
async def get_warmup_schedule():
    """Return the auto-warmup toggle and last-run bookkeeping.

    Auto-warmup fires every Monday at 09:00 UTC so the cache is hot for
    the workweek. See `_warmup_scheduler_loop` for the timing loop.
    """
    db = get_db()
    doc = await db.settings.find_one({"_id": "warmup"}, {"_id": 0}) or {}
    return {
        "enabled": bool(doc.get("enabled", False)),
        "last_run_at": doc.get("last_run_at"),
        "last_run_status": doc.get("last_run_status"),
        "next_run_at": _next_monday_0900_iso(),
    }


@app.post("/api/settings/warmup/schedule", dependencies=[Depends(require_api_token)])
async def set_warmup_schedule(body: AutoWarmupCfg):
    db = get_db()
    await db.settings.update_one(
        {"_id": "warmup"},
        {"$set": {"enabled": bool(body.enabled)}},
        upsert=True,
    )
    return {"ok": True, "enabled": bool(body.enabled),
            "next_run_at": _next_monday_0900_iso()}


def _next_monday_0900_iso() -> str:
    """Compute the next Monday 09:00 UTC as an ISO string."""
    now = datetime.now(timezone.utc)
    # Monday=0, ..., Sunday=6
    days_ahead = (0 - now.weekday()) % 7
    target = now.replace(hour=9, minute=0, second=0, microsecond=0)
    target = target + timedelta(days=days_ahead)
    if target <= now:
        target = target + timedelta(days=7)
    return target.isoformat()


async def _warmup_scheduler_loop():
    """Background loop: every Monday 09:00 UTC, warm the cache if enabled.

    Runs forever. Sleeps until the next Monday 09:00 UTC, checks the
    ``settings.warmup.enabled`` flag, and if true calls ``_run_warmup``.
    Failures are logged to Mongo but never crash the loop -- the scheduler
    must survive individual warmup errors.
    """
    while True:
        try:
            next_run_iso = _next_monday_0900_iso()
            next_run = datetime.fromisoformat(next_run_iso)
            delay = (next_run - datetime.now(timezone.utc)).total_seconds()
            if delay > 0:
                await asyncio.sleep(delay)
            db = get_db()
            doc = await db.settings.find_one({"_id": "warmup"}, {"_id": 0}) or {}
            if not doc.get("enabled"):
                # Sleep an extra minute so the same window doesn't fire again.
                await asyncio.sleep(60)
                continue
            cfg = await _current_llm_cfg()
            try:
                res = await asyncio.wait_for(_run_warmup(db, cfg), timeout=300.0)
                await db.settings.update_one(
                    {"_id": "warmup"},
                    {"$set": {
                        "last_run_at": datetime.now(timezone.utc).isoformat(),
                        "last_run_status": "ok",
                        "last_run_result": res,
                    }},
                )
            except Exception as e:  # pragma: no cover - infrastructure dependent
                await db.settings.update_one(
                    {"_id": "warmup"},
                    {"$set": {
                        "last_run_at": datetime.now(timezone.utc).isoformat(),
                        "last_run_status": f"error:{type(e).__name__}",
                    }},
                )
            # Move past the fired minute.
            await asyncio.sleep(60)
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise
        except Exception:  # pragma: no cover - defensive
            # Any unexpected error: back off a minute and keep the loop alive.
            await asyncio.sleep(60)


@app.on_event("startup")
async def _start_warmup_scheduler():
    asyncio.create_task(_warmup_scheduler_loop())


RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))
# Interim decision, accepted deliberately: REVIEW_RETENTION_DAYS
# defaults to the same window as every other terminal-state retention
# timer until an operator sets a different one explicitly.
REVIEW_RETENTION_DAYS = int(os.environ.get("REVIEW_RETENTION_DAYS", str(RETENTION_DAYS)))

_TERMINAL_RETENTION_STATUSES = ["complete", "failed", "cancelled", "blocked", "intake_failed",
                                "partially_complete", "expired_awaiting_review"]


async def _run_hold(db, run_id: str | None) -> str:
    """The active D14 hold reason on ``run_id``'s ``WorkflowRun``, or ``""``
    when there is none -- including when ``run_id`` is falsy or no
    durable run exists (a pre-Phase-5 session has nothing to hold
    against). Every retention timer in this module checks this before
    any deletion."""
    if not run_id:
        return ""
    run_doc = await db.workflow_runs.find_one({"run_id": run_id}, {"hold": 1})
    return (run_doc or {}).get("hold") or ""


async def _erase_opaque_map_best_effort(db, run_id: str | None) -> None:
    """D5 right-to-erasure/retention: clear ``run_id``'s encrypted opaque
    map (see ``SuperOrchestrator.erase_opaque_map``). Best-effort by
    design, mirroring every other step in this purge path: a session
    whose only identifier is the legacy ``_pipeline_run_id`` token (no
    durable ``WorkflowRun``) has nothing to erase here, and that is not
    an error condition for the caller."""
    if not run_id:
        return
    from phi_core.control.policy import CapabilityPolicy
    from phi_core.control.store import MongoControlStore
    from phi_core.control.superorchestrator import SuperOrchestrator
    from phi_core.control.tasks import TaskService
    from phi_core.control.workflow import WorkflowError

    control_store = MongoControlStore(db)
    try:
        await SuperOrchestrator(
            control_store, TaskService(control_store, CapabilityPolicy(None))
        ).erase_opaque_map(run_id=run_id)
    except WorkflowError as exc:
        if not str(exc).startswith("unknown run_id:"):
            raise


async def _purge_settled_sessions_loop():
    """Hourly: four independent retention sweeps, each backed off and
    retried rather than allowed to kill the loop on a bad iteration.

    1. Terminal sessions (``_TERMINAL_RETENTION_STATUSES``) older than
       ``RETENTION_DAYS``: erase filesystem bytes, then the session
       document -- only once erasure is confirmed (see
       ``_erase_session_from_disk``); a failure records
       ``status="erasure_pending"`` for step 3 to retry instead of either
       silently losing track of it or deleting the document with PHI
       still on disk. ``partially_complete`` sessions remain resumable
       until this window expires.
    2. ``awaiting_human_review`` sessions older than
       ``REVIEW_RETENTION_DAYS``: raw PHI under
       ``UPLOAD_DIR/<sid>`` is erased and the session moves to the
       terminal ``expired_awaiting_review`` status -- a stalled human
       review does not retain raw PHI indefinitely.
    3. Sessions already ``erasure_pending`` from a prior failed sweep or
       a failed ``session_delete`` call: retried unconditionally.
    4. ``ArtifactService.reconcile`` (Phase 7 step 3): the registry-wide
       artifact collection sweep, run from the same interval rather than
       a fifth background task.

    Every step skips a session (or, for reconcile, an artifact) whose
    ``WorkflowRun``/``ArtifactRecord`` carries a non-empty ``hold``.
    """
    from phi_core.control.artifacts import reconcile as reconcile_artifacts
    from phi_core.control.store import MongoControlStore

    while True:
        db = get_db()

        # Step 1: terminal-state sessions past RETENTION_DAYS.
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
            cursor = db.sessions.find(
                {"status": {"$in": _TERMINAL_RETENTION_STATUSES}, "updated_at": {"$lt": cutoff}},
                {"_id": 0, "id": 1, "export_paths": 1, "_pipeline_run_id": 1, "erasure_attempts": 1},
            )
            async for doc in cursor:
                sid = doc.get("id")
                if not sid:
                    continue
                if await _run_hold(db, doc.get("_pipeline_run_id")):
                    continue
                errors = _erase_session_from_disk(sid, doc.get("export_paths"))
                if errors:
                    await db.sessions.update_one({"id": sid}, {"$set": {
                        "status": "erasure_pending",
                        "erasure_error": "; ".join(f"{k}: {v}" for k, v in errors.items()),
                        "erasure_attempts": int(doc.get("erasure_attempts", 0)) + 1,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }})
                    continue
                await _erase_opaque_map_best_effort(db, doc.get("_pipeline_run_id") or sid)
                await db.agent_log.delete_many({"session_id": sid})  # pre-migration rows, if any remain
                await db.trace_events.delete_many({"session_id": sid})
                await db.sessions.delete_one({"id": sid})
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise
        except Exception:  # pragma: no cover - defensive
            pass

        # Step 2: awaiting_human_review sessions past REVIEW_RETENTION_DAYS.
        try:
            review_cutoff = (datetime.now(timezone.utc) - timedelta(days=REVIEW_RETENTION_DAYS)).isoformat()
            cursor = db.sessions.find(
                {"status": "awaiting_human_review", "updated_at": {"$lt": review_cutoff}},
                {"_id": 0, "id": 1, "_pipeline_run_id": 1},
            )
            async for doc in cursor:
                sid = doc.get("id")
                if not sid:
                    continue
                if await _run_hold(db, doc.get("_pipeline_run_id")):
                    continue
                import shutil
                try:
                    shutil.rmtree(UPLOAD_DIR / sid)
                except FileNotFoundError:
                    pass
                except OSError:
                    continue  # retried next sweep; status stays awaiting_human_review
                await db.sessions.update_one({"id": sid}, {"$set": {
                    "status": "expired_awaiting_review",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }})
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise
        except Exception:  # pragma: no cover - defensive
            pass

        # Step 3: retry sessions already erasure_pending.
        try:
            cursor = db.sessions.find(
                {"status": "erasure_pending"},
                {"_id": 0, "id": 1, "export_paths": 1, "_pipeline_run_id": 1, "erasure_attempts": 1},
            )
            async for doc in cursor:
                sid = doc.get("id")
                if not sid:
                    continue
                errors = _erase_session_from_disk(sid, doc.get("export_paths"))
                if errors:
                    await db.sessions.update_one({"id": sid}, {"$set": {
                        "erasure_error": "; ".join(f"{k}: {v}" for k, v in errors.items()),
                        "erasure_attempts": int(doc.get("erasure_attempts", 0)) + 1,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }})
                    continue
                await _erase_opaque_map_best_effort(db, doc.get("_pipeline_run_id") or sid)
                await db.agent_log.delete_many({"session_id": sid})  # pre-migration rows, if any remain
                await db.trace_events.delete_many({"session_id": sid})
                await db.sessions.delete_one({"id": sid})
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise
        except Exception:  # pragma: no cover - defensive
            pass

        # Step 4: the artifact-registry-wide reconcile sweep.
        try:
            await reconcile_artifacts(MongoControlStore(db))
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise
        except Exception:  # pragma: no cover - defensive
            pass

        await asyncio.sleep(3600)


@app.on_event("startup")
async def _startup_maintenance():
    """Idempotent boot-time maintenance: indexes, orphaned-run reconciliation,
    the retention purge loop, and the durable control-plane loops (worker
    claim-and-lease dispatch, outbox relay, lease reconciler). Never raises --
    a down Mongo at boot should not crash the process; the health check
    already reports that."""
    try:
        db = get_db()
        from phi_core.control.limits import WEB_CACHE_REFRESH_DAYS
        from phi_core.control.migrate import create_control_plane_indexes
        await create_control_plane_indexes(
            db, retention_days=RETENTION_DAYS, web_cache_refresh_days=WEB_CACHE_REFRESH_DAYS,
        )

        # Reconcile orphaned runs: an in-process asyncio.create_task pipeline
        # dies silently on restart, leaving the session stuck outside a
        # settled status forever (/handle 409s on a non-terminal status).
        orphan_cutoff = (datetime.now(timezone.utc) - timedelta(seconds=900)).isoformat()
        await db.sessions.update_many(
            {"status": {"$nin": list(_SETTLED_STATUSES)}, "updated_at": {"$lt": orphan_cutoff}},
            {"$set": {"status": "failed", "error": "orphaned by process restart"},
             "$unset": {"_pipeline_run_id": ""}},
        )
    except Exception:  # pragma: no cover - infrastructure dependent
        pass
    asyncio.create_task(_purge_settled_sessions_loop())

    # Durable control-plane loops (D2/D3/D9): the claim-and-lease worker,
    # the outbox relay, and the lease reconciler. Constructing these needs
    # no Mongo round trip (MongoControlStore wraps the lazy Motor client), so
    # they start unconditionally here, the same way the purge loop above
    # does, even when the index/reconciliation block above failed against a
    # down Mongo at boot. Each loop only logs and continues on its own
    # iteration failures -- see phi_core/control/worker.py.
    from phi_core.control.policy import CapabilityPolicy
    from phi_core.control.store import MongoControlStore
    from phi_core.control.tasks import TaskService
    from phi_core.control.worker import Worker, drain_outbox_forever, reconcile_forever

    control_store = MongoControlStore(get_db())
    control_tasks = TaskService(control_store, CapabilityPolicy(None))
    # Phase 4 step 2/4: `_MAX_CONCURRENT_PIPELINES` `Worker` instances, not
    # one -- a single worker claims and executes tasks strictly one at a
    # time, so matching the concurrency the route-level `_admit_pipeline_run`
    # cap already promises (and the immediate 429 it returns past that cap)
    # requires exactly that many workers polling the same `work_items`
    # collection. `TaskService.claim`'s CAS makes two workers racing the
    # same task safe regardless of worker count.
    pipeline_handlers = {
        "pipeline_run": _handle_pipeline_run,
        "pipeline_resume": _handle_pipeline_resume,
    }
    for _ in range(_MAX_CONCURRENT_PIPELINES):
        asyncio.create_task(
            Worker(
                control_store, control_tasks, worker_id=f"worker:{uuid.uuid4().hex[:12]}",
                handlers=pipeline_handlers,
            ).run_forever()
        )
    asyncio.create_task(drain_outbox_forever(control_store))
    asyncio.create_task(reconcile_forever(control_tasks))


# --- Agent-driven PHI handling -------------------------------------------

@app.post("/api/sessions/{sid}/handle")
async def session_handle(sid: str, iteration_cap: int | None = None,
                         principal: str = Depends(resolve_principal)):
    """Run the full 12-agent PHI handling pipeline for this study.

    Optional ``iteration_cap`` (1..3) selects the Judge<->Sentinel rigor:
      1 = fast lane (short studies, high-confidence headers)
      2 = balanced (default)
      3 = thorough (max defensibility, longest wallclock)
    """
    db = get_db()
    session = await _owned_session(sid, principal)
    if session.get("intake_status") not in ("ready",):
        raise HTTPException(400, f"intake not ready (status={session.get('intake_status')})")
    # Phase 4 step 6: rerun admission. A file deleted, truncated, or
    # modified on disk since intake (including a stale/incomplete
    # reintake) must never silently run through the pipeline; refuse
    # with a distinct, actionable error rather than let Executor read
    # missing or changed bytes.
    stale_file_ids = await _validate_rerun_inputs(session.get("files") or [])
    if stale_file_ids:
        raise HTTPException(
            409,
            {"error": "reintake_required",
             "detail": f"{len(stale_file_ids)} input file(s) missing or changed since intake",
             "file_ids": stale_file_ids},
        )
    if not _admit_pipeline_run():
        raise HTTPException(
            429,
            f"pipeline capacity exhausted ({_MAX_CONCURRENT_PIPELINES} concurrent runs); retry shortly",
            headers={"Retry-After": "30"},
        )
    cfg = await _current_llm_cfg()

    cap = max(1, min(int(iteration_cap), 3)) if iteration_cap is not None else None
    run_id = uuid.uuid4().hex
    claim_set = {
        "status": "classifying",
        "_pipeline_run_id": run_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if cap is not None:
        claim_set["iteration_cap"] = cap
    claim = await db.sessions.update_one(
        {
            "id": sid,
            "owner": principal,
            "intake_status": "ready",
            "status": {"$in": ("intake", "complete", "failed", "cancelled")},
        },
        {
            "$set": claim_set,
            "$unset": {
                "guard_report": "",
                "export_paths": "",
                "cancel_requested": "",
                "cancel_requested_at": "",
                "reversal_key_blob": "",
                "reversal_key_created_at": "",
            },
        },
    )
    if not getattr(claim, "matched_count", 0):
        _release_pipeline_run()
        current = await db.sessions.find_one(_owned_filter(sid, principal), {"intake_status": 1, "status": 1})
        if not current:
            raise HTTPException(404, "session not found")
        if current.get("intake_status") != "ready":
            raise HTTPException(400, f"intake not ready (status={current.get('intake_status')})")
        raise HTTPException(
            409,
            f"pipeline launch conflicts with active session (status={current.get('status') or 'missing'})",
        )
    session["_pipeline_run_id"] = run_id
    if cap is not None:
        session["iteration_cap"] = cap
    from phi_core.control.opaque import OpaqueMap
    from phi_core.control.policy import CapabilityPolicy
    from phi_core.control.store import MongoControlStore
    from phi_core.control.superorchestrator import SuperOrchestrator
    from phi_core.control.tasks import TaskService

    control_store = MongoControlStore(db)
    orchestrator_service = SuperOrchestrator(
        control_store, TaskService(control_store, CapabilityPolicy(cfg))
    )
    workflow_run = await orchestrator_service.start_run(
        session_id=sid,
        principal=principal,
        run_type="study",
        iteration_cap=cap or 0,
        correlation_id=run_id,
        run_id=run_id,
    )
    opaque = OpaqueMap(run_id, workflow_run.opaque_map)
    for file_record in session.get("files") or []:
        file_record["opaque_file_id"] = opaque.to_opaque("file", file_record["file_id"])
    await db.sessions.update_one({"id": sid, "_pipeline_run_id": run_id}, {"$set": {"files": session.get("files") or []}})
    await orchestrator_service.record_opaque_map(run_id=run_id, opaque_map=workflow_run.opaque_map)

    # Phase 5 step 2/9: the route owns its legacy session admission claim
    # and then submits the command. SuperOrchestrator owns the WorkflowRun
    # and creates the durable root Pipeline work item; this route never
    # calls TaskService.enqueue directly.
    return {"status": "started", "llm": {"provider": cfg.provider, "model": cfg.model}}


@app.post("/api/sessions/{sid}/cancel")
async def session_cancel(sid: str, principal: str = Depends(resolve_principal)):
    """Request cancellation of a running pipeline.

    The pipeline worker checks the ``cancel_requested`` flag between
    phases and exits cleanly with ``status='cancelled'``. In-flight LLM
    calls finish (they are subject to a 90-180 s hard timeout in
    ``base.Agent``) but no further calls are issued. Idempotent.
    """
    db = get_db()
    doc = await _owned_session(sid, principal, {"status": 1, "_pipeline_run_id": 1})
    if doc.get("status") in ("complete", "failed", "cancelled", "blocked", "intake_failed"):
        return {"status": doc.get("status"), "already_settled": True}
    # partially_complete and awaiting_human_review are both "paused": no
    # running phase boundary will ever observe cancel_requested for them
    # (the resume tail in session_human_review only matches status in
    # {awaiting_human_review, partially_complete}, not cancelled), so a
    # flag-only cancel here would be silently ignored and a later human
    # review submission would resume and keep processing PHI. Transition
    # straight to cancelled instead; this also makes session_human_review's
    # own status filter reject the resume with 409.
    if doc.get("status") in ("awaiting_human_review", "partially_complete"):
        await db.sessions.update_one(
            _owned_filter(sid, principal),
            {"$set": {
                "status": "cancelled",
                "cancel_requested": True,
                "cancel_requested_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }},
        )
        return {"status": "cancelled", "already_settled": False}
    await db.sessions.update_one(
        _owned_filter(sid, principal),
        {"$set": {
            "cancel_requested": True,
            "cancel_requested_at": datetime.now(timezone.utc).isoformat(),
        }},
    )
    run_id = doc.get("_pipeline_run_id")
    if run_id:
        from phi_core.control.policy import CapabilityPolicy
        from phi_core.control.store import MongoControlStore
        from phi_core.control.superorchestrator import SuperOrchestrator
        from phi_core.control.tasks import TaskService
        from phi_core.control.workflow import WorkflowError

        control_store = MongoControlStore(db)
        try:
            await SuperOrchestrator(
                control_store, TaskService(control_store, CapabilityPolicy(None))
            ).cancel_run(
                session_id=sid,
                run_id=run_id,
                principal=principal,
                reason="operator requested cancel via /api/sessions/{sid}/cancel",
            )
        except WorkflowError as exc:
            # Pre-Phase-5 sessions have a legacy `_pipeline_run_id` but no
            # durable WorkflowRun to cancel. The session flag above remains
            # their compatible cancellation signal; any other transition
            # failure remains an error rather than being hidden.
            if not str(exc).startswith("unknown run_id:"):
                raise
    await _emit(sid, ProgressEvent(
        phase="cancel_requested",
        message="Cancel requested by operator; pipeline will exit at next phase boundary.",
    ), run_id=run_id)
    return {"status": "cancel_requested", "already_settled": False}

class HumanReviewSubmit(BaseModel):
    # Each resolution: {file_id, column, mode: "approve"|"comment"|"defer", comment?: str}.
    # `action` is deliberately not client-supplied here -- "approve" always
    # applies the server's own suggested_action / pending_confirmation.action
    # for that column, never a value the client could smuggle in unvalidated.
    resolutions: list[dict]
    # D13 step 4: a client-generated idempotency key. Required so a retried
    # or double-clicked submission can be recognized and answered from the
    # stored result rather than reprocessed -- see the idempotency check in
    # session_human_review below. The old `reviewer` field is removed: it
    # was already inert (identity is always the authenticated principal,
    # never a client-supplied value) and this endpoint never read it.
    client_event_id: str
    comment: str = ""         # optional submission-level note for the audit trail
    # Auditor's confidence-floor gate (design doc "second human review") can
    # fire with zero actionable per-column issues -- just a bare self-
    # reported number below the floor. There is no per-column decision to
    # resolve in that case, so approve/comment/defer cannot clear it.
    #
    # D13 step 4/7: `audit_version` (below) must be supplied and must match
    # the open `HumanReviewRequest.audit_version` whenever this is true --
    # see the check in `session_human_review` -- so a confirmation always
    # binds to the exact Auditor verdict the reviewer actually saw, never a
    # stale one superseded by a later run.
    confirm_auditor_confidence: bool = False
    # D13 step 4: a content hash of the Auditor verdict this confirmation
    # answers, minted by the orchestrator at escalation time and echoed
    # back by the client from the `HumanReviewRequest` it was given.
    # Required (non-empty) exactly when `confirm_auditor_confidence` is
    # true; ignored otherwise.
    audit_version: str = ""
    # HHS §164.514(b)(2)(ii) "actual knowledge" attestation. Required only
    # when this submission resolves (approves/comments) at least one column;
    # a submission that only defers makes no actual-knowledge claim.
    actual_knowledge_ack: bool = False


async def _build_review_event(
    control_store, *, request_id: str | None, run_id: str | None, session_id: str,
    principal: str, body: "HumanReviewSubmit", body_hash: str, decision_version: int, result: dict,
):
    """D13 steps 5/9: construct (never insert -- the caller decides how)
    one `HumanReviewEvent` for this submission. Returns None when
    `request_id` is None -- no durable `HumanReviewRequest` exists for
    this run (a pre-D9-migration session), so there is nothing to bind
    this event to; the session document remains the sole record, exactly
    as before this check existed."""
    if request_id is None:
        return None
    from phi_core.control.records import HumanReviewEvent, ResolutionEntry

    resolutions_typed = [
        ResolutionEntry(file_id=r.get("file_id", ""), column=r.get("column", ""),
                        mode=r.get("mode", "defer"), comment=(r.get("comment") or ""))
        for r in body.resolutions
    ]
    kind = ("audit_confidence_confirmation" if body.confirm_auditor_confidence
            else "defer" if all(r.get("mode") == "defer" for r in body.resolutions)
            else "resolution")
    prior_events = await control_store.find_many("human_review_events", {"request_id": request_id})
    return HumanReviewEvent(
        request_id=request_id, run_id=run_id or "", session_id=session_id,
        workflow_version="wf/1", task_id="", seq=len(prior_events) + 1,
        client_event_id=body.client_event_id, principal=principal, kind=kind,
        body_hash=body_hash, resolutions=resolutions_typed,
        actual_knowledge_ack=body.actual_knowledge_ack, decision_version=decision_version,
        audit_version=body.audit_version, result=result,
    )


@app.post("/api/sessions/{sid}/human-review")
async def session_human_review(sid: str, body: HumanReviewSubmit, principal: str = Depends(resolve_principal)):
    """Operator resolves human_review decisions conversationally and resumes
    the pipeline tail (Executor -> Operator -> Reviewer -> Publish Guard ->
    Auditor/Scout -> Ledger -> Herald).

    Three resolution modes per column:
      - approve: apply the server's own suggested_action (Judge's original
        guess, or a comment's interpreted action once confirmed).
      - comment: free text is interpreted by Judge for that ONE column.
        High confidence (>=0.60) applies directly; below that the
        interpretation is held as `pending_confirmation` for the reviewer
        to confirm (mode="approve") or refine (another mode="comment") on
        a later submission.
      - defer: excluded from this export round, tracked in
        `pending_review`, resolvable on a later submission.

    Per GOAL "human review invariant": every human decision carries
    reviewer id + comment + timestamp. The reviewer identity is the
    authenticated principal, never an operator-supplied field.
    Per HHS §164.514(b)(2)(ii): resolving any column requires an
    actual-knowledge attestation, scoped to the columns resolved this
    round -- never to columns this same submission defers.
    """
    from phi_core.agents.reasoning import (
        ACTION_TYPES,
        CONFIDENCE_FLOOR,
        Judge,
        annotate_pending_review,
        validate_decisions,
    )
    from phi_core.control.context import AgentContext
    from phi_core.control.gates import DecisionGateFailure, run_decision_gates
    from phi_core.control.store import MongoControlStore
    from phi_core.security import scrub_persisted_text

    reviewer = principal
    ts = datetime.now(timezone.utc).isoformat()
    resolvable_actions = ACTION_TYPES - {"human_review"}

    by_key: dict[tuple[str, str], dict] = {}
    for r in body.resolutions:
        mode = r.get("mode")
        if mode not in ("approve", "comment", "defer"):
            raise HTTPException(422, f"resolution mode for column {r.get('column')!r} must be "
                                     f"approve|comment|defer, got {mode!r}")
        if mode == "comment" and not (r.get("comment") or "").strip():
            raise HTTPException(422, f"comment mode requires non-empty comment for column {r.get('column')!r}")
        by_key[(r.get("file_id", ""), r.get("column", ""))] = r
    any_resolution = any(r.get("mode") != "defer" for r in by_key.values())
    if any_resolution and not body.actual_knowledge_ack:
        raise HTTPException(
            400,
            "actual-knowledge attestation is required (HHS 45 CFR 164.514(b)(2)(ii)) for any "
            "approved or comment-resolved column this round: reviewer must confirm no actual "
            "knowledge that the remaining information alone or in combination could identify "
            "an individual. A submission that only defers does not require this attestation.",
        )

    db = get_db()
    session = await _owned_session(sid, principal)
    if reviewer_role(principal) is None:
        raise HTTPException(403, "principal is not an authorized reviewer (see REVIEWER_PRINCIPALS)")
    prior_run_id = session.get("_pipeline_run_id")
    control_store = MongoControlStore(db)
    # D13 step 8: the run's current decision_version, when a durable
    # `WorkflowRun` already exists for it (Phase 5 migrated this session's
    # start/resume through `SuperOrchestrator`). A pre-migration session,
    # or one whose run never opened a `WorkflowRun`, has none -- 0, same
    # as `_next_decision_version`'s own no-store fallback below.
    workflow_run_doc = (await control_store.get_one("workflow_runs", {"run_id": prior_run_id})
                         if prior_run_id else None)
    current_decision_version = int(workflow_run_doc.get("decision_version", 0)) if workflow_run_doc else 0
    dataset_file_ids = {f.get("file_id") for f in (session.get("files") or []) if f.get("kind") == "dataset"}
    if any_resolution and dataset_file_ids:
        # D13 step 8: a download must be scoped to this exact principal,
        # file, and decision_version -- not merely "some file was
        # downloaded at some point" -- so a reviewer cannot satisfy the
        # attestation for a column whose source file changed underneath
        # them since they last opened it. Replaces the prior session-wide
        # "at least one download exists" check.
        downloads = session.get("dataset_file_downloads") or []
        resolved_file_ids = {r.get("file_id", "") for r in by_key.values()
                              if r.get("mode") != "defer" and r.get("file_id") in dataset_file_ids}
        for file_id in sorted(resolved_file_ids):
            if not any(d.get("downloaded_by") == principal and d.get("file_id") == file_id
                       and int(d.get("decision_version", 0)) == current_decision_version
                       for d in downloads):
                raise HTTPException(
                    400,
                    f"dataset file {file_id!r} must be downloaded via GET .../dataset-file/{{file_id}} "
                    "at the current decision version before resolving its column(s): the "
                    "actual-knowledge attestation is only meaningful if the reviewer has "
                    "actually opened the current original data.",
                )
    review_filter = _owned_filter(sid, principal)
    review_filter["status"] = {"$in": ["awaiting_human_review", "partially_complete"]}
    if prior_run_id is None:
        review_filter["_pipeline_run_id"] = {"$exists": False}
    else:
        review_filter["_pipeline_run_id"] = prior_run_id

    # D13 step 5 (partial) and step 3 (idempotency only, not the full
    # work-item fence/lease_owner comparison D13 specifies -- there is no
    # WorkItem yet at this point in the flow, so there is nothing to fence
    # against; tracked as a gap, not silently claimed done). When a durable
    # `HumanReviewRequest` is open for this run, an already-processed
    # `client_event_id` is answered from its stored result instead of
    # reprocessing it (no repeated provider call for a retried or
    # double-clicked submission); a different body under the same key is
    # 409. Additive: a pre-D9-migration session with no durable request at
    # all, or a resubmission after the request has already resolved, falls
    # through unchanged -- `request_id` stays None and no idempotency
    # protection applies, same as this route's behavior before this check
    # existed. `open_requests` holds at most one entry:
    # `SuperOrchestrator.request_human_review` supersedes any prior open
    # request for a run_id before opening a new one (D13's "only supersede
    # closes" invariant), so a rerun escalation never leaves two competing
    # open requests for this query to pick between.
    request_id: str | None = None
    open_request_doc: dict | None = None
    if prior_run_id:
        open_requests = await control_store.find_many(
            "human_review_requests", {"run_id": prior_run_id, "state": "open"}
        )
        if open_requests:
            open_request_doc = open_requests[0]
            request_id = open_request_doc["request_id"]
    if body.confirm_auditor_confidence:
        # D13 step 4/7: required field, and (when a durable request is
        # open and actually carries a minted audit_version) must match it
        # exactly -- a reviewer confirming against a since-superseded
        # Auditor verdict is a stale confirmation, not a valid one.
        if not body.audit_version:
            raise HTTPException(
                400,
                "audit_version is required when confirm_auditor_confidence is true",
            )
        request_audit_version = (open_request_doc or {}).get("audit_version") or ""
        if request_audit_version and body.audit_version != request_audit_version:
            raise HTTPException(
                409,
                "audit_version does not match the open human-review request's audit "
                "verdict; the Auditor's report has changed since this confirmation was "
                "prepared -- reload and confirm against the current verdict",
            )
    body_hash = hashlib.sha256(json.dumps(
        {"resolutions": body.resolutions, "confirm_auditor_confidence": body.confirm_auditor_confidence,
         "audit_version": body.audit_version, "actual_knowledge_ack": body.actual_knowledge_ack},
        sort_keys=True, default=str,
    ).encode("utf-8")).hexdigest()
    if request_id is not None:
        existing_event = await control_store.get_one(
            "human_review_events", {"request_id": request_id, "client_event_id": body.client_event_id}
        )
        if existing_event is not None:
            if existing_event.get("body_hash") != body_hash:
                raise HTTPException(409, "client_event_id was already used with a different submission body")
            return existing_event.get("result") or {"status": "duplicate_submission_replayed"}

    decisions = list(session.get("agent_decisions", []))
    dictionary_by_column = {c.get("name"): c.get("description", "")
                            for c in (session.get("agent_specialists") or {}).get("lexicon", {}).get("columns", [])
                            if c.get("name")}

    # Resolve every mode="comment" row concurrently -- one Judge call per
    # column, never a dataset cell value, always the scrubbed comment text.
    comment_targets = [d for d in decisions
                       if d.get("action") == "human_review"
                       and by_key.get((d.get("file_id", ""), d.get("column", "")), {}).get("mode") == "comment"]
    comment_results: dict[tuple[str, str], dict] = {}
    if comment_targets:
        cfg = await _current_llm_cfg()
        from phi_core.control.activation import ActivationFactory
        judge_ctx = await ActivationFactory(db, cfg).activate(
            session_id=sid, run_id=prior_run_id or sid, agent="Judge",
        )
        judge = Judge(judge_ctx)
        async def _resolve(d: dict) -> tuple[tuple[str, str], dict]:
            key = (d.get("file_id", ""), d.get("column", ""))
            reply = await judge.resolve_comment(
                column=d.get("column", ""),
                description=dictionary_by_column.get(d.get("column", ""), ""),
                suggested_action=d.get("suggested_action"),
                suggested_reason=d.get("suggested_reason"),
                comment=by_key[key].get("comment") or "",
            )
            return key, reply
        for key, reply in await asyncio.gather(*[_resolve(d) for d in comment_targets]):
            comment_results[key] = reply

    live_keys = {(d.get("file_id", ""), d.get("column", "")) for d in decisions if d.get("action") == "human_review"}
    for d in decisions:
        key = (d.get("file_id", ""), d.get("column", ""))
        if d.get("action") != "human_review" or key not in by_key:
            continue
        r = by_key[key]
        mode = r.get("mode")
        row_comment = scrub_persisted_text((r.get("comment") or "").strip()) or None
        if mode == "defer":
            # Explicitly left as action="human_review" -- joins pending_review
            # below. Never silently defaulted to drop/keep.
            if row_comment:
                d["reviewer_comment"] = row_comment
                d["reviewer"] = reviewer
                d["reviewed_at"] = ts
            continue
        if mode == "comment":
            reply = comment_results.get(key) or {}
            action = str(reply.get("action") or "").strip().lower()
            reason = str(reply.get("reason") or "").strip()
            try:
                confidence = max(0.0, min(1.0, float(reply.get("confidence") or 0.0)))
            except (TypeError, ValueError):
                confidence = 0.0
            if action not in resolvable_actions:
                action = None
            # D13 step 6: every model interpretation of free text requires an
            # explicit reviewer confirmation submission, regardless of the
            # model's self-reported confidence. The prior >=0.60 auto-apply
            # let an LLM's guess become the operative de-identification
            # decision with no human ever confirming it -- inconsistent with
            # the HHS 45 CFR 164.514(b)(2)(ii) actual-knowledge attestation
            # this same request already requires. A reviewer confirms with a
            # separate mode="approve" submission (see below).
            d["pending_confirmation"] = {"action": action, "reason": reason, "confidence": confidence}
            d["reviewer_comment"] = row_comment
            d["reviewer"] = reviewer
            d["reviewed_at"] = ts
            continue
        elif mode == "approve":
            pending = d.get("pending_confirmation")
            if pending:
                if not pending.get("action"):
                    raise HTTPException(422, f"nothing to confirm for column {d.get('column')!r}: "
                                             "the interpreted action was itself invalid; comment again")
                action, reason, confidence = pending["action"], pending.get("reason") or "confirmed by reviewer", \
                    pending.get("confidence") or 0.6
                provenance = "human_comment_inferred"
            else:
                action = d.get("suggested_action")
                if not action:
                    raise HTTPException(422, f"cannot approve column {d.get('column')!r}: "
                                             "no suggested action is available; use a comment instead")
                action, reason, confidence = action, d.get("suggested_reason") or "approved by reviewer", 1.0
                provenance = "human_explicit_action"
            d["action"] = action
            d["reason"] = f"human decision by {reviewer}: {reason}"
            d["confidence"] = confidence
            d["reviewer_comment"] = row_comment
            d["reviewer"] = reviewer
            d["reviewed_at"] = ts
            d["provenance"] = provenance
            d.pop("pending_confirmation", None)

    resolved_now = [d for d in decisions if (d.get("file_id", ""), d.get("column", "")) in by_key
                    and d.get("action") != "human_review"]
    _, resolution_rejections = validate_decisions(resolved_now)
    bad_action_cols = sorted({r["column"] for r in resolution_rejections if r.get("field") == "action"})
    if bad_action_cols:
        raise HTTPException(422, f"invalid resolution action for column(s): {', '.join(bad_action_cols)}")

    # session_human_review never previously re-ran the guardrails every
    # other decision path passes through. Close that gap here: every
    # decision mutation -- including a human resolution -- goes through
    # the canonical D11 gate sequence, which also proves exact per-column
    # coverage before this decision set is ever allowed near Executor.
    #
    # A decision a human just explicitly resolved this round (approve, or
    # a confirmed comment interpretation) already IS the human review
    # apply_confidence_floor exists to trigger. Re-running that floor on
    # the LLM's own comment-interpretation confidence would silently
    # revert the human's approval back to human_review with no way for
    # the reviewer to ever get past it -- floor the gating confidence at
    # CONFIDENCE_FLOOR for exactly those freshly-resolved rows; every
    # other decision keeps its real confidence unchanged.
    for d in decisions:
        if d.get("provenance") in ("human_explicit_action", "human_comment_inferred"):
            confidence = d.get("confidence")
            if not isinstance(confidence, (int, float)) or confidence < CONFIDENCE_FLOOR:
                d["confidence"] = CONFIDENCE_FLOOR
    dataset_files_for_gates = [f for f in session.get("files", []) if f.get("kind") == "dataset"]
    # A bare bookkeeping context, not a real activation: `run_decision_gates`
    # only reads `ctx.run_id`/`ctx.task_id` for its audit stamps and never
    # touches the grant/gateway/tools/trace fields, so building a full
    # `ActivationFactory` activation here would cost a real provider-policy
    # check and a `capability_grants`/`work_items` write for no reason this
    # call site needs. `store` is `control_store` when a durable
    # `WorkflowRun` already exists for this run (Phase 5's
    # `SuperOrchestrator.start_run` migration) so `decision_version` is the
    # real, CAS-incremented one `dataset_file_downloads` scoping above
    # compares against; `None` (decision_version stays 0) for a
    # pre-migration session with no such record, same tolerant fallback
    # `_next_decision_version` documents for its own no-store case.
    gates_ctx = AgentContext(
        session_id=sid, run_id=prior_run_id or sid, task_id=uuid.uuid4().hex,
        agent="Judge", attempt=1, grant=None, gateway=None, tools=None, trace=None,
    )
    gate_outcome = await run_decision_gates(
        decisions=decisions,
        files=dataset_files_for_gates,
        jurisdiction=session.get("jurisdiction", "us"),
        stage="human_review.regate",
        ctx=gates_ctx,
        store=control_store if workflow_run_doc is not None else None,
    )
    for gate_result in gate_outcome.gate_results:
        await MongoControlStore(db).insert("gate_results", gate_result)
    keep_demotions = gate_outcome.demotions
    decisions = annotate_pending_review(gate_outcome.decisions, dictionary_by_column)
    # Hard-rule overrides are the only ones in this call site's gate
    # sequence that can fire (age/DOB and site-cardinality rules apply to
    # Judge-shaped proposals, not human-resolved decisions in practice,
    # but are structurally possible too) -- both are identifiable by
    # carrying `citation` with no `rule` key, unlike every other gate's
    # override shape, so a human's explicit resolution is only annotated
    # `human_overridden_by_hard_rule` for the gate that can genuinely
    # override a human choice on safety grounds.
    for ov in gate_outcome.overrides:
        if "citation" not in ov or "rule" in ov:
            continue
        for d in decisions:
            if d.get("file_id") == ov.get("file_id") and d.get("column") == ov.get("column"):
                if d.get("provenance") in ("human_explicit_action", "human_comment_inferred"):
                    d["human_overridden_action"] = ov.get("from")
                    d["provenance"] = "human_overridden_by_hard_rule"
                break
    if not gate_outcome.ok:
        raise DecisionGateFailure(gate_outcome)

    # `by_key` reflects raw client submission -- a resubmission for an
    # already-resolved or nonexistent (file_id, column) pair must not be
    # recorded as newly resolved/deferred in the permanent audit trail;
    # only entries that matched a live human_review decision this round did
    # anything, so scope the audit entry to `live_keys` (the same filter
    # the per-decision mutation loop above already applies).
    acted_keys = live_keys & by_key.keys()
    session_review_entry = {
        "reviewer": reviewer,
        "comment": scrub_persisted_text(body.comment) if body.comment else "",
        "reviewed_at": ts,
        "resolved_columns": [{"file_id": k[0], "column": k[1]} for k in acted_keys if by_key[k].get("mode") != "defer"],
        "deferred_columns": [{"file_id": k[0], "column": k[1]} for k in acted_keys if by_key[k].get("mode") == "defer"],
        "actual_knowledge_ack": bool(any_resolution and body.actual_knowledge_ack),
        "actual_knowledge_cite": "45 CFR 164.514(b)(2)(ii)",
    }
    session_review_history = list(session.get("session_review") or [])
    if session_review_history and isinstance(session_review_history[0], dict) and "reviewer" not in session_review_history[0]:
        session_review_history = []  # defensive: unexpected legacy shape, do not propagate
    elif isinstance(session.get("session_review"), dict):
        session_review_history = [session["session_review"]]  # migrate the old single-dict shape
    session_review_history.append(session_review_entry)

    pending_review = [{"file_id": d.get("file_id"), "column": d.get("column")}
                      for d in decisions if d.get("action") == "human_review"]
    ever_resolved = any(d.get("action") != "human_review" for d in decisions)

    if pending_review and not ever_resolved:
        # Nothing has ever been resolved on this session -- persist the
        # deferrals/pending-confirmations and wait; running the pipeline
        # tail on a fully-empty decision set would produce nothing.
        update = await db.sessions.update_one(
            review_filter,
            {"$set": {
                "agent_decisions": decisions,
                "pending_review": pending_review,
                "session_review": session_review_history,
                "keep_demotions": keep_demotions,
                "human_review_required": True,
            }},
        )
        if getattr(update, "matched_count", 0):
            result = {"status": "still_awaiting", "unresolved": len(pending_review)}
            review_event = await _build_review_event(
                control_store, request_id=request_id, run_id=prior_run_id, session_id=sid,
                principal=principal, body=body, body_hash=body_hash,
                decision_version=gate_outcome.decision_version, result=result,
            )
            if review_event is not None:
                # The request stays open (nothing terminal happened this
                # round): record the submission directly rather than
                # through consume_review_event, which would prematurely
                # resolve the request.
                await control_store.insert("human_review_events", review_event)
            return result
        current = await db.sessions.find_one(_owned_filter(sid, principal), {"status": 1})
        if not current:
            raise HTTPException(404, "session not found")
        raise HTTPException(
            409,
            f"human-review update conflicts with active session (status={current.get('status') or 'missing'})",
        )

    cfg = await _current_llm_cfg()
    if not _admit_pipeline_run():
        raise HTTPException(
            429,
            f"pipeline capacity exhausted ({_MAX_CONCURRENT_PIPELINES} concurrent runs); retry shortly",
            headers={"Retry-After": "30"},
        )
    # A human decision resumes the same workflow run. Legacy sessions that
    # predate durable WorkflowRun records receive one now, under a fresh id.
    resume_run_id = prior_run_id or uuid.uuid4().hex
    claim = await db.sessions.update_one(
        review_filter,
        {"$set": {
            "status": "anonymizing",
            "agent_decisions": decisions,
            "pending_review": pending_review,
            "session_review": session_review_history,
            "keep_demotions": keep_demotions,
            "human_review_required": bool(pending_review),
            "_pipeline_run_id": resume_run_id,
        }},
    )
    if not getattr(claim, "matched_count", 0):
        _release_pipeline_run()
        current = await db.sessions.find_one(_owned_filter(sid, principal), {"status": 1})
        if not current:
            raise HTTPException(404, "session not found")
        raise HTTPException(
            409,
            f"human-review resume conflicts with active session (status={current.get('status') or 'missing'})",
        )

    # Phase 5 step 2/9: submit and return. The atomic `review_filter`
    # claim preserves the existing session fence. SuperOrchestrator either
    # reuses its matching WorkflowRun or opens one for a pre-migration
    # session, then creates the durable `pipeline_resume` root task; this
    # route never calls TaskService.enqueue directly.
    from phi_core.control.policy import CapabilityPolicy
    from phi_core.control.superorchestrator import SuperOrchestrator
    from phi_core.control.tasks import TaskService
    from phi_core.control.workflow import WorkflowError

    result = {"status": "resuming"}
    review_event = await _build_review_event(
        control_store, request_id=request_id, run_id=resume_run_id, session_id=sid,
        principal=principal, body=body, body_hash=body_hash,
        decision_version=gate_outcome.decision_version, result=result,
    )
    orchestrator_service = SuperOrchestrator(control_store, TaskService(control_store, CapabilityPolicy(cfg)))
    if review_event is not None:
        # D13's actual resolution authority: consume_review_event both
        # persists this event and resolves the durable request (do not
        # also insert it directly here -- that would double-write the
        # (request_id, client_event_id)-unique collection). Best-effort:
        # the session document (updated just above) remains this
        # synchronous path's load-bearing state machine, so a WorkflowRun
        # state mismatch (e.g. a pre-migration run this call is opening
        # for the first time) must not fail an otherwise-successful
        # human-review resume.
        try:
            await orchestrator_service.consume_review_event(run_id=resume_run_id, event=review_event)
        except WorkflowError:
            pass
    await orchestrator_service.start_run(
        session_id=sid,
        principal=principal,
        run_type="study",
        correlation_id=resume_run_id,
        run_id=resume_run_id,
        root_task_type="pipeline_resume",
    )
    return result


def _trace_event_to_message(doc: dict) -> dict:
    """Reconstruct the ``AgentMessage``-shaped dict the frontend's
    ``_groupTrace`` and ``phi_corpus.benchmark.report_from_session``
    already expect, from a persisted ``TraceEvent`` document (D15
    agent_log migration). ``TraceEvent.ts`` is stored as a plain string
    (unlike the legacy ``agent_log`` collection's native BSON date), so
    no post-read ``isoformat()`` conversion is needed here."""
    return {
        "id": doc.get("event_id", ""),
        "seq": doc.get("seq", 0),
        "session_id": doc.get("session_id", ""),
        "agent": doc.get("agent", ""),
        "phase": doc.get("phase", ""),
        "ts": doc.get("ts", ""),
        "direction": doc.get("direction", ""),
        "payload": doc.get("payload") or {},
        "duration_ms": doc.get("latency_ms", 0),
        "parent_id": doc.get("parent_msg_id") or None,
        "status_text": doc.get("status_text", ""),
    }


async def _session_trace_messages(
    db, sid: str, *, after_seq: int | None = None, limit: int | None = None,
) -> list[dict]:
    """Every ``trace_events`` row for ``sid``, oldest first, mapped back to
    the ``AgentMessage`` shape. Replaces the legacy ``db.agent_log.find``
    read path shared by ``session_bundle``, ``corpus_study_benchmark``,
    and ``session_agent_trace``."""
    query: dict[str, Any] = {"session_id": sid}
    if after_seq is not None:
        query["seq"] = {"$gt": after_seq}
    cursor = db.trace_events.find(query, {"_id": 0}).sort("seq", 1)
    if limit is not None:
        cursor = cursor.limit(limit)
    return [_trace_event_to_message(doc) async for doc in cursor]


@app.get("/api/sessions/{sid}/agent-trace")
async def session_agent_trace(sid: str, limit: int = 200, after_seq: int = 0,
                              principal: str = Depends(resolve_principal)):
    """Return one page of the audit log of every agent message on this session.

    Cursor-paginated: ``after_seq`` is the ``trace_events.seq`` of the
    newest message the caller already has (D15 step 2: replaces the
    former ISO-timestamp ``after`` cursor, which lost ties); this page
    returns strictly newer messages only. Tier 3's full, uncapped
    per-message text (see ``AgentMessage``) makes a naive full-history
    refetch on every SSE tick expensive at scale; the frontend appends
    pages incrementally instead (see ``SessionDetail.jsx``).
    """
    from phi_core.security import scrub_nested as _scrub_nested
    db = get_db()
    await _owned_session(sid, principal, {"id": 1})
    limit = max(1, min(int(limit), 2000))
    raw = await _session_trace_messages(db, sid, after_seq=after_seq or None, limit=limit)
    # SEC-006: agent-trace payloads are nested dicts (`prompt_text`,
    # `reply_text`) that echo dictionary/form/comment PHI. Scrub every
    # string leaf recursively rather than only top-level string fields.
    msgs = [_scrub_nested(m) for m in raw]
    next_seq = max((m.get("seq", 0) for m in raw), default=after_seq) if raw else after_seq
    return {
        "messages": msgs,
        "next_cursor": next_seq,
        "has_more": len(msgs) == limit,
    }


@app.get("/api/sessions/{sid}/dataset-file/{file_id}")
async def session_dataset_file(sid: str, file_id: str, principal: str = Depends(resolve_principal)):
    """Stream one dataset file's original uploaded bytes, byte-identical.

    Replaces the old masked row-level preview: rather than backend code
    reading and partial-masking cell values on a reviewer's behalf, the
    reviewer downloads the untouched original file and opens it in their
    own tool. This code path does zero CSV/XLSX parsing -- it never reads
    a single cell value -- and never opens the file itself; it only
    resolves ``file_id`` against this session's own ``files`` list (never
    a client-supplied path) and streams the bytes already on disk, exactly
    like the existing export endpoint's own file_id lookup pattern.

    Available at any session status the caller owns: a reviewer may want
    to glance at the source file before, during, or after resolving the
    flagged columns. Each download is recorded (principal + timestamp +
    the run's current decision_version, D13 step 8) so the "I have opened
    and reviewed the current original file" attestation has a
    server-side fact behind it, scoped to exactly the decision state the
    reviewer actually saw.
    """
    db = get_db()
    session = await _owned_session(sid, principal, {"_id": 0})
    matches = [f for f in (session.get("files") or []) if f.get("file_id") == file_id]
    if not matches:
        raise HTTPException(404, "no such file on this session")
    f = matches[0]
    if f.get("kind") != "dataset":
        raise HTTPException(404, "only dataset files are served through this endpoint")
    path = Path(f["stored_path"])
    if not path.exists():
        raise HTTPException(404, "original file is no longer available (session settled and cleaned up)")
    from phi_core.control.store import MongoControlStore
    prior_run_id = session.get("_pipeline_run_id")
    workflow_run_doc = (await MongoControlStore(db).get_one("workflow_runs", {"run_id": prior_run_id})
                         if prior_run_id else None)
    decision_version = int(workflow_run_doc.get("decision_version", 0)) if workflow_run_doc else 0
    await db.sessions.update_one(
        _owned_filter(sid, principal),
        {"$push": {"dataset_file_downloads": {
            "file_id": file_id,
            "downloaded_by": principal,
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
            "decision_version": decision_version,
        }}},
    )
    return FileResponse(path, filename=decrypt_display_name(f.get("original_name_encrypted", "")) or path.name)


@app.get("/api/sessions/{sid}/results")
async def session_results(sid: str, principal: str = Depends(resolve_principal)):
    """Consolidated agent outputs (decisions, audit, ledger, herald)."""
    doc = await _owned_session(sid, principal, {"_id": 0})
    scrubbed = _scrub_session_document(doc)
    return {
        "status": scrubbed.get("status"),
        "decisions": scrubbed.get("agent_decisions", []),
        "sentinel_last": scrubbed.get("agent_sentinel_last"),
        "audit": scrubbed.get("agent_audit"),
        "ledger": scrubbed.get("agent_ledger"),
        "herald": scrubbed.get("agent_herald"),
        "scout": scrubbed.get("agent_scout"),
        "guard": scrubbed.get("guard_report"),
        "session_review": scrubbed.get("session_review"),
        "pending_review": scrubbed.get("pending_review", []),
        "human_review_required": scrubbed.get("human_review_required", False),
        "phase_timings": scrubbed.get("phase_timings", {}),
        "run_elapsed_s": scrubbed.get("run_elapsed_s"),
    }


@app.get("/api/version")
async def version():
    return {"service": "phi-handling-console", "version": app.version}


# 4.15: serve the built frontend from this same process, same origin. Must
# be the very last statement in the module -- a mount at "/" shadows any
# route registered after it. Guarded so a source checkout without a built
# frontend still starts and still serves /api.


class _SPAStaticFiles(StaticFiles):
    """`StaticFiles(html=True)` only serves index.html for a directory
    match ("/") or a literal 404.html; a React Router deep link like
    `/sessions/<id>` has no matching file on disk and would 404 on a hard
    refresh. Every request that reaches this mount has already missed
    every /api/* route (those are registered first and matched before the
    mount), so any 404 here means "client-side route, let the SPA's own
    router resolve it" -- fall back to index.html rather than a bare 404.
    """
    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise


_FRONTEND_BUILD_DIR = Path(__file__).resolve().parent.parent / "frontend" / "build"
if _FRONTEND_BUILD_DIR.exists():
    app.mount("/", _SPAStaticFiles(directory=str(_FRONTEND_BUILD_DIR), html=True), name="ui")
