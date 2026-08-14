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
import uuid
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_log = logging.getLogger("phi_console")

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from phi_core.crypto import decrypt_api_key, encrypt_api_key
from phi_core.db import get_db
from phi_core.intake import (
    COMPONENT_SUFFIXES, MANDATORY, ANY_OF, build_manifest,
)
from phi_core.jurisdictions import get_pack
from phi_core.models import FileArtifact, ProgressEvent, Session
from phi_core.paths import UnsafePath, UPLOAD_DIR, cleanup_session_unpacked, safe_join, CHATGPT_TOKEN_DIR

# Redirect litellm's ChatGPT-provider Authenticator to the pinned token
# directory (backend/phi_core/paths.py) rather than the per-user home
# directory it defaults to. Must run before any request-time litellm call
# constructs an Authenticator, so it is set at import time here rather
# than in an on_event("startup") hook.
os.environ.setdefault("CHATGPT_TOKEN_DIR", str(CHATGPT_TOKEN_DIR))
from phi_core.security import (
    allowed_providers, require_api_token, resolve_principal, resolve_principal_soft,
    scrub_decision, token_principals, validate_llm_base_url, validate_llm_provider,
)
from phi_core.agents import AgentMessage, LlmConfig, run_pipeline as run_agent_pipeline
from phi_core import chatgpt_auth


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
_RATE_BUCKETS: dict[str, list[float]] = {}


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
        hits = _RATE_BUCKETS.setdefault(key, [])
        while hits and hits[0] < cutoff:
            hits.pop(0)
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


# --- In-memory progress queues per session (SSE) --------------------------
#
# Per-session queue lifecycle (SEC-002 fix, audit iteration_18):
#   * created lazily by `_queue_for` when the pipeline first emits OR the
#     client first subscribes to the stream
#   * removed by `_release_stream` when the LAST subscriber for that
#     session disconnects OR the pipeline emits the terminal `__end__`
#     event
# Client counts prevent premature GC when the pipeline outlives one
# client's connection (e.g. tab reload) but ensure the queue does not
# leak for sessions no one is watching any more.

_progress_queues: dict[str, asyncio.Queue] = {}
_progress_subscribers: dict[str, int] = {}

# Terminal statuses that guarantee the pipeline is done and no more
# events will arrive on the SSE queue.
_SETTLED_STATUSES = frozenset({"complete", "failed", "cancelled", "blocked",
                                "intake_failed", "awaiting_human_review", "partially_complete"})

# Cap of concurrent SSE subscribers per session. 4 is enough for the
# operator + a couple of secondary viewers + one connection retry. Beyond
# that we refuse new subscribers (returns HTTP 429) to prevent an
# attacker from opening thousands of streams and pinning memory.
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


def _queue_for(session_id: str) -> asyncio.Queue:
    q = _progress_queues.get(session_id)
    if q is None:
        q = asyncio.Queue()
        _progress_queues[session_id] = q
    return q


def _release_stream(session_id: str) -> None:
    """One subscriber has disconnected; free the queue if this was the last."""
    remaining = _progress_subscribers.get(session_id, 1) - 1
    if remaining <= 0:
        _progress_subscribers.pop(session_id, None)
        _progress_queues.pop(session_id, None)
    else:
        _progress_subscribers[session_id] = remaining


async def _emit(session_id: str, ev: ProgressEvent, run_id: str | None = None) -> None:
    db = get_db()
    query = {"id": session_id}
    if run_id is not None:
        query["_pipeline_run_id"] = run_id
    result = await db.sessions.update_one(
        query,
        {"$push": {"progress": ev.model_dump()}, "$set": {"updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    if run_id is not None and not getattr(result, "matched_count", 0):
        return
    await _queue_for(session_id).put(ev)


async def _fail_session_correlated(db, sid: str, run_filter: dict, e: Exception, *, run_id: str | None) -> None:
    """4.23: mark a pipeline worker's session failed without persisting or
    streaming raw exception text. Full detail (exception type, message,
    traceback) goes to the server log against a short correlation id; the
    stored session and the SSE client see only a fixed message plus that
    id, matching the global unhandled-exception handler's contract."""
    error_id = uuid.uuid4().hex[:12]
    _log.error(
        "session %s pipeline worker failure [%s]: %s: %s",
        sid, error_id, type(e).__name__, e, exc_info=True,
    )
    await db.sessions.update_one(run_filter, {"$set": {"status": "failed", "error": "pipeline failed", "error_id": error_id}})
    cleanup_session_unpacked(sid)
    await _emit(sid, ProgressEvent(phase="failed", message=f"pipeline error (id {error_id}); see server logs"), run_id=run_id)


# --- Health ----------------------------------------------------------------

@app.get("/api/health")
async def health():
    from phi_core.crypto import signing_public_key_pem
    import shutil as _shutil

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
    provider = llm_doc.get("provider", "")
    if provider == "chatgpt":
        llm_provider_ok = chatgpt_auth.read_auth() is not None
    elif provider == "emergent":
        llm_provider_ok = True
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
    from phi_core.security import token_principals
    from phi_core.crypto import sign_principal_cookie
    import hmac as _hmac
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
    from phi_core.security import scrub_nested as _scrub_nested, scrub_persisted_text as _scrub_text
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


@app.delete("/api/sessions/{sid}")
async def session_delete(sid: str, principal: str = Depends(resolve_principal)):
    """Right-to-erasure: remove the session document, its agent_log rows,
    its UPLOAD_DIR/<sid> tree, and every path in export_paths."""
    import shutil
    db = get_db()
    doc = await _owned_session(sid, principal, {"_id": 0, "export_paths": 1})
    for p in (doc.get("export_paths") or {}).values():
        if p:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass
    shutil.rmtree(UPLOAD_DIR / sid, ignore_errors=True)
    await db.agent_log.delete_many({"session_id": sid})
    await db.sessions.delete_one(_owned_filter(sid, principal))
    return {"deleted": True}


@app.post("/api/sessions/{sid}/intake", dependencies=[Depends(rate_limited("session_intake", 20, 3600))])
async def session_intake(sid: str, file: UploadFile = File(...), principal: str = Depends(resolve_principal)):
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
            original_name=Path(e.relpath).name,
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
        {"$set": {
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
        }},
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
                {"file_id": a.file_id, "name": a.original_name, "size": a.size_bytes, "sha256": a.sha256[:16]}
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
    doc = await _owned_session(sid, principal, {"status": 1})
    if doc.get("status") in _SETTLED_STATUSES:
        raise HTTPException(status_code=409,
                            detail=f"session already settled ({doc.get('status')}); "
                                   "no more stream events will arrive")
    if _progress_subscribers.get(sid, 0) >= _MAX_STREAM_SUBSCRIBERS_PER_SESSION:
        raise HTTPException(status_code=429,
                            detail="too many concurrent stream subscribers "
                                   "for this session")

    async def gen():
        _progress_subscribers[sid] = _progress_subscribers.get(sid, 0) + 1
        q = _queue_for(sid)
        try:
            # HANG PROTECTION: emit an SSE keep-alive comment every 15 s so
            # browsers / proxies do not close the connection during a long
            # LLM call (Herald.Sections and Statute web-search can each run
            # >30 s without a message). The comment starts with ":" per SSE
            # spec so the EventSource on the client ignores it silently.
            while True:
                try:
                    ev: ProgressEvent = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                yield f"data: {json.dumps(ev.model_dump())}\n\n"
                if ev.phase == "__end__":
                    break
        finally:
            # SEC-002 fix: release the queue on client disconnect or
            # stream end so an idle/nonexistent subscriber cannot pin
            # memory indefinitely.
            _release_stream(sid)

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
    agent_log_msgs = None
    if publication and session.get("corpus_ground_truth"):
        agent_log_msgs = await db.agent_log.find({"session_id": sid}, {"_id": 0}).to_list(length=None)
    data, filename = build_bundle(session, BundleOptions(
        include_publication=publication, include_attestation_pdf=attestation_pdf,
    ), agent_log=agent_log_msgs)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/sessions/{sid}/export/{file_id}")
async def session_export(sid: str, file_id: str, force: bool = False,
                         principal: str = Depends(resolve_principal)):
    """Download the PHI-handled export.

    GOAL boundary: this is the point where 'input PHI data' becomes 'output
    ready to share publicly'. Refuse the download unless exactly one Publish
    Guard result marked this specific file 'clean'. ``?force=true`` is an
    audited override only when that single result is `blocked`, after the
    operator has manually reviewed the findings.
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
    path = (session.get("export_paths") or {}).get(file_id)
    if not path or not Path(path).exists():
        raise HTTPException(404, "export not ready")
    guard = session.get("guard_report") or {}
    matching_results = [
        r for r in (guard.get("results") or [])
        if r.get("file_id") == file_id
    ]
    per_file = matching_results[0] if len(matching_results) == 1 else None
    status = per_file.get("status") if per_file else None
    # SEC-001 fix: fail-closed. Serve only if this file has exactly one
    # per-file guard result of `clean`. Missing, duplicate, `skipped`, or
    # `blocked` results refuse — `?force=true` overrides only a single
    # `blocked` result after manual review.
    if status != "clean":
        if force and status == "blocked":
            # Record the override on the session so the audit trail keeps it.
            await db.sessions.update_one(
                _owned_filter(sid, principal),
                {"$push": {"guard_overrides": {
                    "file_id": file_id,
                    "overridden_at": datetime.now(timezone.utc).isoformat(),
                }}},
            )
        else:
            return JSONResponse(status_code=403, content={
                "error": "publish_guard_not_certified",
                "message": (
                    "Publish Guard has not certified this file as clean "
                    f"(status={status or 'missing'}). Re-run the pipeline so "
                    "the last-mile PHI scan populates a passing result."
                ),
                "guard": per_file,
            })
    return FileResponse(path, filename=Path(path).name)


# --- LLM settings (BYO-key) ----------------------------------------------

from typing import Literal


class LlmSettings(BaseModel):
    provider: Literal["emergent", "anthropic", "openai", "gemini", "openrouter", "openai_compatible", "chatgpt"] = "emergent"
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.1
    max_tokens: int = 2000


def _first_boot_llm_defaults() -> dict:
    """First-boot defaults resolved from the environment.

    So a deploy with only ``ANTHROPIC_API_KEY`` set gets an Anthropic
    default instead of the ``emergent`` factory default, without the
    operator touching Settings.
    """
    from phi_core.agents.llm import _default_provider
    return LlmSettings(provider=_default_provider()).model_dump()


def _env_available_providers() -> list[str]:
    """Return the providers whose credentials are present in the environment.

    Sir Q "Ensure it is not locked to emergent only". If a self-hosted
    deploy only sets ``ANTHROPIC_API_KEY``, the Settings UI should not
    invite the user to pick ``emergent`` and crash on first call.
    """
    out: list[str] = []
    if os.environ.get("EMERGENT_LLM_KEY"):
        out.append("emergent")
    if os.environ.get("ANTHROPIC_API_KEY"):
        out.append("anthropic")
    if os.environ.get("OPENAI_API_KEY"):
        out.append("openai")
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        out.append("gemini")
    if os.environ.get("OPENROUTER_API_KEY"):
        out.append("openrouter")
    return out


def _providers_payload() -> dict:
    """Compose the providers block for /api/settings/llm.

    ``providers`` = providers the operator MAY configure. Excludes
    ``emergent`` when EMERGENT_LLM_KEY is not set in the pod (no BYO
    path exists for it). All other providers stay listed because they
    can be configured by pasting a BYO key.

    ``env_providers`` = subset with credentials already present in the
    pod environment (zero-setup path).
    """
    env = _env_available_providers()
    listed = set(allowed_providers())
    if "emergent" in listed and "emergent" not in env:
        listed.discard("emergent")
    return {
        "providers": sorted(listed),
        "env_providers": env,
    }


@app.get("/api/settings/llm", dependencies=[Depends(require_api_token)])
async def get_llm_settings():
    from phi_core.crypto import KeyRotated
    db = get_db()
    doc = await db.settings.find_one({"_id": "llm"}, {"_id": 0})
    if not doc:
        return _first_boot_llm_defaults() | _providers_payload()
    # never leak the api_key back verbatim
    if doc.get("api_key"):
        try:
            decrypt_api_key(doc["api_key"])
        except KeyRotated:
            raise HTTPException(409, "provider key cannot be decrypted; re-enter it in Settings")
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


# --- Corpus generator + verifier -------------------------------------------
#
# The corpus is a red-team torture-test rig: PHI is planted in realistic
# study data, run through the pipeline, and every decision compared
# against the planted ground truth. Ground truth stays in the session
# document only (Sir's Q1(iii)) — it is never persisted to disk.


@app.get("/api/corpus/study/catalog", dependencies=[Depends(require_api_token)])
async def corpus_study_catalog():
    from phi_corpus.scenarios import list_scenarios
    from phi_corpus.edge_cases import EDGE_CASES, HIPAA_MAX_EDGE_CASE_TAGS
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
    from phi_corpus.researcher import CorpusResearcher
    db = get_db()
    cfg = await _current_llm_cfg()
    agent = CorpusResearcher(session_id="corpus-researcher", llm=cfg, db=db)
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
    from phi_corpus.planters import plant
    from phi_core.intake import build_manifest

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
            original_name=Path(e.relpath).name,
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


# Keep task references alive so CPython does not GC them mid-flight.
_CORPUS_STUDY_TASKS: dict[str, asyncio.Task] = {}


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
        f.get("original_name", ""): f.get("file_id", "")
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
    agent_log_msgs = await db.agent_log.find({"session_id": sid}, {"_id": 0}).to_list(length=None)
    return _build_corpus_benchmark_report(doc, agent_log_msgs)


@app.get("/api/corpus/study/benchmark/{sid}/download")
async def corpus_study_benchmark_download(sid: str, principal: str = Depends(resolve_principal)):
    """Download the six benchmark artefacts (markdown, JSON, CSV, three
    PNGs) as one ZIP."""
    from phi_corpus.benchmark import bundle_zip

    db = get_db()
    doc = await _owned_session(sid, principal, {"_id": 0})
    agent_log_msgs = await db.agent_log.find({"session_id": sid}, {"_id": 0}).to_list(length=None)
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
    if doc.get("provider") == "chatgpt":
        # ChatGPTConfig supplies both api_key and base_url from the OAuth
        # auth file; nothing is persisted in the settings document for it.
        doc = {**doc, "api_key": "", "base_url": ""}
    elif doc.get("api_key"):
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


@app.post("/api/settings/chatgpt/login", dependencies=[Depends(require_api_token)])
async def chatgpt_login_start():
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

    warmup_sid = f"warmup:{uuid.uuid4().hex[:8]}"

    async def _noop_emit(_msg):  # pragma: no cover - trivial
        return None

    common = dict(session_id=warmup_sid, llm=cfg, db=db, emit=_noop_emit)
    hipaa_cats = ["A", "B", "C", "D", "F", "G", "H", "I", "J", "K",
                  "L", "M", "N", "O", "P", "Q", "R"]

    praxis_agent = Praxis(**common)
    statute_task = Statute(**common).run(jurisdiction="us")
    praxis_task = asyncio.gather(
        *[praxis_agent.method_for(c) for c in hipaa_cats],
        return_exceptions=True,
    )
    statute_res, praxis_res = await asyncio.gather(statute_task, praxis_task)

    praxis_ok = [c for c, r in zip(hipaa_cats, praxis_res)
                 if not isinstance(r, Exception)]
    praxis_err = [{"category": c, "error": type(r).__name__}
                  for c, r in zip(hipaa_cats, praxis_res)
                  if isinstance(r, Exception)]
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
    except asyncio.TimeoutError:
        raise HTTPException(504, "warmup exceeded 240s ceiling")


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


async def _purge_settled_sessions_loop():
    """Hourly: delete settled sessions older than RETENTION_DAYS, together
    with their UPLOAD_DIR/<sid> tree, their export_paths files, and their
    agent_log rows. Runs forever; a single bad iteration backs off and
    retries rather than killing the loop."""
    import shutil
    while True:
        try:
            db = get_db()
            cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
            cursor = db.sessions.find(
                {"status": {"$in": ["complete", "failed", "cancelled", "blocked", "intake_failed", "partially_complete"]},
                 "updated_at": {"$lt": cutoff}},
                {"_id": 0, "id": 1, "export_paths": 1},
            )
            async for doc in cursor:
                sid = doc.get("id")
                if not sid:
                    continue
                shutil.rmtree(UPLOAD_DIR / sid, ignore_errors=True)
                for p in (doc.get("export_paths") or {}).values():
                    if p:
                        try:
                            Path(p).unlink(missing_ok=True)
                        except OSError:
                            pass
                await db.agent_log.delete_many({"session_id": sid})
                await db.sessions.delete_one({"id": sid})
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise
        except Exception:  # pragma: no cover - defensive
            pass
        await asyncio.sleep(3600)


@app.on_event("startup")
async def _startup_maintenance():
    """Idempotent boot-time maintenance: indexes, orphaned-run reconciliation,
    and the retention purge loop. Never raises -- a down Mongo at boot
    should not crash the process; the health check already reports that."""
    try:
        db = get_db()
        await db.sessions.create_index("id", unique=True)
        await db.sessions.create_index("owner")
        await db.agent_log.create_index("session_id")
        await db.agent_log.create_index("ts", expireAfterSeconds=RETENTION_DAYS * 86400)

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

    async def emit_msg(msg: AgentMessage) -> None:
        # Persist to session progress in a compact form for the SSE consumer.
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


    async def worker():
        run_filter = {"id": sid, "_pipeline_run_id": run_id}
        try:
            # Populate dataset headers (LLM never sees rows). Persist onto session before pipeline runs.
            from phi_core.file_readers import read_csv_columns, read_xlsx_columns, read_parquet_columns
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
                        await _emit(sid, ProgressEvent(phase="reading", message=f"header extract failed for {f['original_name']}: {e}"), run_id=run_id)
                files_hydrated.append(f)
            session["files"] = files_hydrated
            await db.sessions.update_one(run_filter, {"$set": {"files": files_hydrated}})

            # 4.21: refuse an oversized study rather than sending an
            # unbounded Judge prompt / decision list.
            _enforce_column_cap(files_hydrated)

            # HANG PROTECTION: hard 15-minute wall-clock ceiling. If the
            # pipeline burns beyond this the worker is cancelled with a
            # clear "timeout" reason -- no orphaned tasks, no infinite
            # loading screens. 15 min is 5x the observed 190 s happy path
            # and 2x the worst historical case (~340 s + Herald 90 s x2).
            result = await asyncio.wait_for(
                run_agent_pipeline(session, db, cfg, emit_msg, on_phase, run_id=run_id),
                timeout=900,
            )
            await _emit(sid, ProgressEvent(phase="complete", message=f"Pipeline done: {result.get('status')}", percent=100.0), run_id=run_id)
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
        except Exception as e:
            # Import here to keep this endpoint's cold-start light.
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
            else:
                await _fail_session_correlated(db, sid, run_filter, e, run_id=run_id)
        finally:
            _release_pipeline_run()
            await _emit(sid, ProgressEvent(phase="__end__", message="stream end"), run_id=run_id)

    asyncio.create_task(worker())
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
    doc = await _owned_session(sid, principal, {"status": 1})
    if doc.get("status") in ("complete", "failed", "cancelled", "blocked", "partially_complete"):
        return {"status": doc.get("status"), "already_settled": True}
    await db.sessions.update_one(
        _owned_filter(sid, principal),
        {"$set": {
            "cancel_requested": True,
            "cancel_requested_at": datetime.now(timezone.utc).isoformat(),
        }},
    )
    await _emit(sid, ProgressEvent(
        phase="cancel_requested",
        message="Cancel requested by operator; pipeline will exit at next phase boundary.",
    ))
    return {"status": "cancel_requested", "already_settled": False}


class HumanReviewSubmit(BaseModel):
    # Each resolution: {file_id, column, mode: "approve"|"comment"|"defer", comment?: str}.
    # `action` is deliberately not client-supplied here -- "approve" always
    # applies the server's own suggested_action / pending_confirmation.action
    # for that column, never a value the client could smuggle in unvalidated.
    resolutions: list[dict]
    reviewer: str = ""        # unused; identity is the authenticated principal
    comment: str = ""         # optional submission-level note for the audit trail
    # HHS §164.514(b)(2)(ii) "actual knowledge" attestation. IRB-required
    # procedural step separate from the technical Safe Harbor method.
    # Required only when this submission resolves (approves/comments) at
    # least one column -- a submission that only defers makes no
    # actual-knowledge claim about anything.
    actual_knowledge_ack: bool = False


@app.post("/api/sessions/{sid}/human-review")
async def session_human_review(sid: str, body: HumanReviewSubmit, principal: str = Depends(resolve_principal)):
    """Operator resolves human_review decisions conversationally and resumes
    the pipeline tail (Executor -> Auditor -> Scout -> Ledger -> Herald).

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
        ACTION_TYPES, Auditor, Executor, Judge, annotate_pending_review,
        apply_sentinel_hard_rules, validate_decisions, verify_keep_decisions,
    )
    from phi_core.agents.outward import Scout, Ledger, Herald
    from phi_core.paths import cleanup_session_unpacked
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
    prior_run_id = session.get("_pipeline_run_id")
    review_filter = _owned_filter(sid, principal)
    review_filter["status"] = {"$in": ["awaiting_human_review", "partially_complete"]}
    if prior_run_id is None:
        review_filter["_pipeline_run_id"] = {"$exists": False}
    else:
        review_filter["_pipeline_run_id"] = prior_run_id

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
        judge = Judge(session_id=sid, llm=cfg, db=db, emit=None)
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
            if action is None or confidence < 0.60:
                # Held for confirmation -- stays on human_review, not resolved this round.
                d["pending_confirmation"] = {"action": action, "reason": reason, "confidence": confidence}
                d["reviewer_comment"] = row_comment
                d["reviewer"] = reviewer
                d["reviewed_at"] = ts
                continue
            d["action"] = action
            d["reason"] = f"human comment (interpreted) by {reviewer}: {reason}"
            d["confidence"] = confidence
            d["reviewer_comment"] = row_comment
            d["reviewer"] = reviewer
            d["reviewed_at"] = ts
            d["provenance"] = "human_comment_inferred"
            d.pop("pending_confirmation", None)
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
    # other decision path passes through. Close that gap here: the hard-rule
    # table can still force-correct an obvious direct identifier regardless
    # of what the human chose, and keep-verification re-checks any decision
    # left as "keep" against the real dataset values.
    decisions, hard_rule_overrides = apply_sentinel_hard_rules(decisions)
    for ov in hard_rule_overrides:
        for d in decisions:
            if d.get("file_id") == ov.get("file_id") and d.get("column") == ov.get("column"):
                if d.get("provenance") in ("human_explicit_action", "human_comment_inferred"):
                    d["human_overridden_action"] = ov.get("from")
                    d["provenance"] = "human_overridden_by_hard_rule"
                break
    dataset_paths = {f["file_id"]: Path(f["stored_path"]) for f in session.get("files", []) if f.get("kind") == "dataset"}
    decisions, keep_demotions = verify_keep_decisions(decisions, dataset_paths, jurisdiction=session.get("jurisdiction", "us"))
    decisions = annotate_pending_review(decisions, dictionary_by_column)

    session_review_entry = {
        "reviewer": reviewer,
        "comment": scrub_persisted_text(body.comment) if body.comment else "",
        "reviewed_at": ts,
        "resolved_columns": [{"file_id": k[0], "column": k[1]} for k, r in by_key.items() if r.get("mode") != "defer"],
        "deferred_columns": [{"file_id": k[0], "column": k[1]} for k, r in by_key.items() if r.get("mode") == "defer"],
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
            return {"status": "still_awaiting", "unresolved": len(pending_review)}
        current = await db.sessions.find_one(_owned_filter(sid, principal), {"status": 1})
        if not current:
            raise HTTPException(404, "session not found")
        raise HTTPException(
            409,
            f"human-review update conflicts with active session (status={current.get('status') or 'missing'})",
        )

    files = session.get("files", [])
    cfg = await _current_llm_cfg()
    if not _admit_pipeline_run():
        raise HTTPException(
            429,
            f"pipeline capacity exhausted ({_MAX_CONCURRENT_PIPELINES} concurrent runs); retry shortly",
            headers={"Retry-After": "30"},
        )
    resume_run_id = uuid.uuid4().hex
    claim = await db.sessions.update_one(
        review_filter,
        {"$set": {
            "status": "anonymizing",
            "agent_decisions": decisions,
            "pending_review": pending_review,
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
    run_filter = {"id": sid, "_pipeline_run_id": resume_run_id}

    async def emit_msg(msg: AgentMessage) -> None:
        ev = ProgressEvent(
            phase=f"agent:{msg.agent}:{msg.direction}",
            message=f"{msg.agent} {msg.phase}",
            payload={"agent": msg.agent, "phase_key": msg.phase, "direction": msg.direction,
                     "duration_ms": msg.duration_ms, "status_text": msg.status_text,
                     "parent_id": msg.parent_id, "id": msg.id},
        )
        await _emit(sid, ev, run_id=resume_run_id)

    async def worker():
        async def _run_tail():
            common = dict(session_id=sid, llm=cfg, db=db, emit=emit_msg)
            resolved_decisions = [d for d in decisions if d.get("action") != "human_review"]
            scrubbed_decisions = [scrub_decision(d) for d in resolved_decisions]
            omit_by_file: dict[str, set[str]] = {}
            for entry in pending_review:
                omit_by_file.setdefault(entry["file_id"], set()).add(entry["column"])
            exec_out = await Executor(**common).run(files=files, decisions=scrubbed_decisions, omit_by_file=omit_by_file)
            from phi_core.publish_guard import scan_all_exports as _scan_all_exports
            if exec_out["exports"]:
                guard_report = _scan_all_exports(exec_out["exports"], decisions=scrubbed_decisions,
                                                 jurisdiction=session.get("jurisdiction", "us")).to_dict()
            else:
                # Nothing resolved into an exportable file yet this round
                # (e.g. every column of the only dataset is still deferred).
                # This is a legitimate empty-so-far state, not a leak --
                # Publish Guard's own "no exports to scan" reading would
                # otherwise report `blocked`, which is wrong here.
                guard_report = {"status": "clean", "results": [], "scanned": 0, "blocked": 0}
            if guard_report["status"] != "clean":
                await db.sessions.update_one(run_filter, {"$set": {
                    "status": "blocked",
                    "guard_report": guard_report,
                    "export_paths": exec_out["exports"],
                    "agent_decisions": decisions,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }})
                cleanup_session_unpacked(sid)
                await _emit(sid, ProgressEvent(phase="blocked", message="publish guard blocked this run", percent=100.0), run_id=resume_run_id)
                return
            audit = await Auditor(**common).run(decisions=scrubbed_decisions, exports=exec_out["exports"], files=files)
            scout = await Scout(**common).run()
            ledger = await Ledger(**common).run(decisions=scrubbed_decisions, audit=audit, scout=scout, benchmark_result=None)
            herald = await Herald(**common).run(ledger=ledger, audit=audit,
                                                target_venue=session.get("target_venue") or "JAMIA Open")
            final_status = "partially_complete" if pending_review else "complete"
            completion_update = {
                "$set": {
                    "agent_audit": audit,
                    "agent_ledger": ledger,
                    "agent_herald": herald,
                    "agent_scout": scout,
                    "guard_report": guard_report,
                    "session_review": session_review_history,
                    "pending_review": pending_review,
                    "export_paths": exec_out["exports"],
                    "status": final_status,
                    "human_review_required": bool(pending_review),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            }
            await db.sessions.update_one(run_filter, completion_update)
            if final_status == "complete":
                # Only a fully-resolved session releases the original files --
                # a partially_complete session keeps them so a later
                # resolution round can resume Executor against them.
                cleanup_session_unpacked(sid)
            await _emit(sid, ProgressEvent(
                phase=final_status,
                message="pipeline complete after human review" if final_status == "complete"
                        else f"partial export ready; {len(pending_review)} column(s) still pending review",
                percent=100.0), run_id=resume_run_id)

        try:
            await asyncio.wait_for(_run_tail(), timeout=900)
        except asyncio.TimeoutError:
            await db.sessions.update_one(run_filter, {"$set": {
                "status": "failed",
                "error": "resume worker exceeded 15-minute wall-clock ceiling",
            }})
            cleanup_session_unpacked(sid)
            await _emit(sid, ProgressEvent(phase="failed", message="Resume hit the 15-minute wall-clock ceiling."), run_id=resume_run_id)
        except Exception as e:
            await _fail_session_correlated(db, sid, run_filter, e, run_id=resume_run_id)
        finally:
            _release_pipeline_run()
            await _emit(sid, ProgressEvent(phase="__end__", message="stream end"), run_id=resume_run_id)

    asyncio.create_task(worker())
    return {"status": "resuming"}


@app.get("/api/sessions/{sid}/agent-trace")
async def session_agent_trace(sid: str, limit: int = 200, after: str | None = None,
                              principal: str = Depends(resolve_principal)):
    """Return one page of the audit log of every agent message on this session.

    Cursor-paginated: ``after`` is the ``ts`` (ISO-8601, as returned in a
    prior page's last message) of the newest message the caller already
    has; this page returns strictly newer messages only. Tier 3's full,
    uncapped per-message text (see ``AgentMessage``) makes a naive
    full-history refetch on every SSE tick expensive at scale; the frontend
    appends pages incrementally instead (see ``SessionDetail.jsx``).
    """
    from phi_core.security import scrub_nested as _scrub_nested
    db = get_db()
    await _owned_session(sid, principal, {"id": 1})
    query: dict[str, Any] = {"session_id": sid}
    if after:
        try:
            query["ts"] = {"$gt": datetime.fromisoformat(after)}
        except ValueError:
            raise HTTPException(400, f"invalid cursor: {after!r} is not an ISO-8601 timestamp")
    limit = max(1, min(int(limit), 2000))
    cursor = db.agent_log.find(query, {"_id": 0}).sort("ts", 1).limit(limit)
    msgs: list[dict] = []
    async for m in cursor:
        ts = m.get("ts")
        if hasattr(ts, "isoformat"):
            m["ts"] = ts.isoformat()
        # SEC-006: agent-trace payloads are nested dicts (`prompt_text`,
        # `reply_text`) that echo dictionary/form/comment PHI. Scrub every
        # string leaf recursively rather than only top-level string fields.
        msgs.append(_scrub_nested(m))
    return {
        "messages": msgs,
        "next_cursor": msgs[-1]["ts"] if msgs else after,
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
    flagged columns. Each download is recorded (principal + timestamp) so
    the "I have opened and reviewed the original file" attestation has a
    server-side fact behind it.
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
    await db.sessions.update_one(
        _owned_filter(sid, principal),
        {"$push": {"dataset_file_downloads": {
            "file_id": file_id,
            "downloaded_by": principal,
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
        }}},
    )
    return FileResponse(path, filename=f.get("original_name") or path.name)


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
    }


@app.get("/api/version")
async def version():
    return {"service": "phi-handling-console", "version": app.version}


# 4.15: serve the built frontend from this same process, same origin. Must
# be the very last statement in the module -- a mount at "/" shadows any
# route registered after it. Guarded so a source checkout without a built
# frontend still starts and still serves /api.
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException


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
