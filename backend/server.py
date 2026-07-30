"""FastAPI server for PHI handling console.

Endpoints (all under /api):
  GET  /api/health
  POST /api/corpus/generate      -> generate synthetic corpus
  GET  /api/corpus               -> list corpora
  GET  /api/corpus/{id}          -> get corpus
  POST /api/benchmark/run        -> run benchmark against a corpus
  GET  /api/benchmark            -> list benchmarks
  GET  /api/benchmark/{id}       -> get benchmark
  POST /api/sessions             -> create session
  POST /api/sessions/{id}/upload -> upload one file
  POST /api/sessions/{id}/run    -> start reading/classifying/detecting
  GET  /api/sessions/{id}        -> session state
  GET  /api/sessions/{id}/stream -> SSE progress
  POST /api/sessions/{id}/review -> submit review decisions
  POST /api/sessions/{id}/finalize -> anonymize and export
  GET  /api/sessions/{id}/export/{file_id} -> download redacted file
"""
from __future__ import annotations

import asyncio
import uuid
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from phi_core.benchmark import run_benchmark
from phi_core.crypto import decrypt_api_key, encrypt_api_key
from phi_core.db import get_db
from phi_core.generators import corpus_hash, generate, HIPAA_CATEGORIES
from phi_core.intake import (
    COMPONENT_SUFFIXES, MANDATORY, ANY_OF, build_manifest,
)
from phi_core.models import (
    BenchmarkRequest, CorpusRecord, CorpusRequest, DetectedSpan,
    FileArtifact, ProgressEvent, ReviewDecision, Session,
)
from phi_core.paths import UnsafePath, safe_join
from phi_core.pipeline import (
    UPLOAD_DIR, anonymize_files, apply_reviews, classify_file, detect_file, ingest_file,
)
from phi_core.security import (
    allowed_providers, require_api_token, validate_llm_base_url, validate_llm_provider,
)
from phi_core.agents import AgentMessage, LlmConfig, run_pipeline as run_agent_pipeline


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

_progress_queues: dict[str, asyncio.Queue] = {}


def _queue_for(session_id: str) -> asyncio.Queue:
    q = _progress_queues.get(session_id)
    if q is None:
        q = asyncio.Queue()
        _progress_queues[session_id] = q
    return q


async def _emit(session_id: str, ev: ProgressEvent) -> None:
    await _queue_for(session_id).put(ev)
    db = get_db()
    await db.sessions.update_one(
        {"id": session_id},
        {"$push": {"progress": ev.model_dump()}, "$set": {"updated_at": datetime.now(timezone.utc).isoformat()}},
    )


# --- Health ----------------------------------------------------------------

@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "version": "2.0.0",
        "hipaa_categories": HIPAA_CATEGORIES,
        "supported_jurisdictions": ["us"],
    }


# --- Corpus ----------------------------------------------------------------

@app.post("/api/corpus/generate", dependencies=[Depends(require_api_token)])
async def corpus_generate(req: CorpusRequest):
    records = generate(req.jurisdiction, req.seed, req.count_per_category, req.include_quasi_identifiers)
    corpus_id = f"corpus_{req.jurisdiction}_{req.seed}_{req.count_per_category}"
    h = corpus_hash(records)
    doc = {
        "id": corpus_id,
        "jurisdiction": req.jurisdiction,
        "seed": req.seed,
        "count_per_category": req.count_per_category,
        "include_quasi_identifiers": req.include_quasi_identifiers,
        "hash": h,
        "total_records": len(records),
        "total_gold_spans": sum(len(r.gold_spans) for r in records),
        "records": [r.model_dump() for r in records],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    db = get_db()
    await db.corpora.replace_one({"id": corpus_id}, doc, upsert=True)
    return {k: v for k, v in doc.items() if k != "records"} | {"sample_records": doc["records"][:5]}


@app.get("/api/corpus")
async def corpus_list():
    db = get_db()
    cursor = db.corpora.find({}, {"records": 0, "_id": 0})
    return {"corpora": [c async for c in cursor]}


@app.get("/api/corpus/{corpus_id}")
async def corpus_get(corpus_id: str, limit: int = 20):
    db = get_db()
    doc = await db.corpora.find_one({"id": corpus_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "corpus not found")
    doc["records"] = doc.get("records", [])[:limit]
    return doc


# --- Benchmark -------------------------------------------------------------

@app.post("/api/benchmark/run", dependencies=[Depends(require_api_token)])
async def benchmark_run(req: BenchmarkRequest):
    db = get_db()
    corpus = await db.corpora.find_one({"id": req.corpus_id}, {"_id": 0})
    if not corpus:
        raise HTTPException(404, "corpus not found")
    records = [CorpusRecord(**r) for r in corpus["records"]]
    result = run_benchmark(records, req.corpus_id, req.detectors)
    await db.benchmarks.insert_one(result.model_dump())
    return result.model_dump()


@app.get("/api/benchmark")
async def benchmark_list():
    db = get_db()
    cursor = db.benchmarks.find({}, {"_id": 0}).sort("created_at", -1).limit(50)
    return {"benchmarks": [b async for b in cursor]}


@app.get("/api/benchmark/{bid}")
async def benchmark_get(bid: str):
    db = get_db()
    doc = await db.benchmarks.find_one({"id": bid}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "benchmark not found")
    return doc


# --- Sessions --------------------------------------------------------------

class SessionCreate(BaseModel):
    jurisdiction: str = "us"


@app.post("/api/sessions", dependencies=[Depends(require_api_token)])
async def session_create(body: SessionCreate):
    s = Session(jurisdiction=body.jurisdiction)
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
    if not doc:
        return doc
    for f in doc.get("files", []) or []:
        f.pop("stored_path", None)
    if "export_paths" in doc:
        # Convert absolute paths to opaque blank strings; keys retained so
        # the UI still shows which file_ids are downloadable.
        doc["export_paths"] = {k: "" for k in (doc.get("export_paths") or {})}
    for k in (
        "agent_decisions", "agent_herald", "agent_ledger",
        "agent_scout", "agent_audit", "agent_sentinel_last",
        "agent_specialists", "agent_statute",
    ):
        if k in doc:
            doc[k] = _scrub_nested(doc[k])
    return doc


@app.get("/api/sessions/{sid}", dependencies=[Depends(require_api_token)])
async def session_get(sid: str):
    doc = await get_db().sessions.find_one({"id": sid}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "session not found")
    return _scrub_session_document(doc)


@app.get("/api/sessions", dependencies=[Depends(require_api_token)])
async def session_list():
    cursor = get_db().sessions.find({}, {"_id": 0, "spans": 0, "progress": 0}).sort("created_at", -1).limit(50)
    out = []
    async for s in cursor:
        out.append(_scrub_session_document(s))
    return {"sessions": out}


@app.post("/api/sessions/{sid}/upload", dependencies=[Depends(require_api_token)])
async def session_upload(sid: str, file: UploadFile = File(...)):
    db = get_db()
    session = await db.sessions.find_one({"id": sid})
    if not session:
        raise HTTPException(404, "session not found")
    session_dir = UPLOAD_DIR / sid
    session_dir.mkdir(parents=True, exist_ok=True)
    try:
        dst = safe_join(session_dir, file.filename, fallback="upload.bin")
    except UnsafePath as e:
        raise HTTPException(400, f"unsafe filename: {e}")
    written = await _stream_to_disk(file, dst, _MAX_UPLOAD_BYTES)
    art = FileArtifact(
        original_name=dst.name,
        size_bytes=written,
        sha256="",
        kind="narrative",
        subtype="txt",
        stored_path=str(dst),
    )
    await db.sessions.update_one(
        {"id": sid},
        {"$push": {"files": art.model_dump()}, "$set": {"updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    return {"file_id": art.file_id, "size_bytes": art.size_bytes}


@app.post("/api/sessions/{sid}/intake", dependencies=[Depends(require_api_token)])
async def session_intake(sid: str, file: UploadFile = File(...)):
    """Default entry: upload a ZIP with intake-manifest/v3 structure.

    ZIP must contain top-level `datasets/`, `forms/`, and one of
    `data_dictionary/` or `mappings/`. Fails closed on missing components or
    unsupported files.
    """
    db = get_db()
    session = await db.sessions.find_one({"id": sid})
    if not session:
        raise HTTPException(404, "session not found")
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

    # Re-intake resets downstream state (files, spans, progress, exports).
    await db.sessions.update_one(
        {"id": sid},
        {"$set": {
            "files": [f.model_dump() for f in accepted],
            "spans": [],
            "progress": [],
            "export_paths": {},
            "review_iteration": 0,
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


@app.get("/api/sessions/{sid}/intake/receipt", dependencies=[Depends(require_api_token)])
async def session_intake_receipt(sid: str):
    """CLI-style redacted receipt (never leaks entry paths).

    Mirrors the `phi_engine intake` stdout contract from
    feat/v2-multi-jurisdiction: {study, status, linked, review, errors, manifest}.
    """
    db = get_db()
    session = await db.sessions.find_one({"id": sid}, {"_id": 0})
    if not session:
        raise HTTPException(404, "session not found")
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


@app.post("/api/sessions/{sid}/run", dependencies=[Depends(require_api_token)])
async def session_run(sid: str):
    db = get_db()
    session = await db.sessions.find_one({"id": sid})
    if not session:
        raise HTTPException(404, "session not found")

    async def emit(ev: ProgressEvent):
        await _emit(sid, ev)

    async def worker():
        try:
            # Refuse to run if intake status is not ready when intake was attempted.
            if session.get("intake_status") in ("failed", "review_required"):
                await db.sessions.update_one({"id": sid}, {"$set": {"status": "failed", "error": f"intake_status={session.get('intake_status')} (exit={session.get('intake_exit_code')})"}})
                await emit(ProgressEvent(phase="failed", message=f"cannot run: intake {session.get('intake_status')}"))
                await emit(ProgressEvent(phase="__end__", message="stream end"))
                return
            await db.sessions.update_one({"id": sid}, {"$set": {"status": "reading"}})
            fresh_files: list[FileArtifact] = []
            for raw in session.get("files", []):
                path = Path(raw["stored_path"])
                art = await ingest_file(sid, path, raw["original_name"], emit, component=raw.get("component"))
                art.file_id = raw["file_id"]
                fresh_files.append(art)
            await db.sessions.update_one(
                {"id": sid},
                {"$set": {"files": [f.model_dump() for f in fresh_files], "status": "classifying"}},
            )

            for art in fresh_files:
                cls = await classify_file(art, emit)
                art.llm_classification = cls
            await db.sessions.update_one(
                {"id": sid},
                {"$set": {"files": [f.model_dump() for f in fresh_files], "status": "detecting"}},
            )

            all_spans: list[DetectedSpan] = []
            for art in fresh_files:
                spans = await detect_file(art, ["presidio", "rule"], emit)
                for s in spans:
                    s.file_id = art.file_id
                    s.review_comment = ""
                    all_spans.append(s)

            await db.sessions.update_one(
                {"id": sid},
                {"$set": {
                    "spans": [s.model_dump() for s in all_spans],
                    "status": "awaiting_review",
                }},
            )
            await emit(ProgressEvent(phase="awaiting_review", message=f"{len(all_spans)} spans awaiting human review", percent=100.0))
        except Exception as e:
            await db.sessions.update_one({"id": sid}, {"$set": {"status": "failed", "error": f"{type(e).__name__}: {e}"}})
            await emit(ProgressEvent(phase="failed", message=f"pipeline failed: {e}"))
        finally:
            await emit(ProgressEvent(phase="__end__", message="stream end"))

    asyncio.create_task(worker())
    return {"status": "started"}


@app.get("/api/sessions/{sid}/stream", dependencies=[Depends(require_api_token)])
async def session_stream(sid: str):
    async def gen():
        q = _queue_for(sid)
        while True:
            ev: ProgressEvent = await q.get()
            yield f"data: {json.dumps(ev.model_dump())}\n\n"
            if ev.phase == "__end__":
                break

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ReviewSubmit(BaseModel):
    decisions: list[ReviewDecision]
    add_manual_spans: list[DetectedSpan] = []
    continue_iteration: bool = False


@app.post("/api/sessions/{sid}/review", dependencies=[Depends(require_api_token)])
async def session_review(sid: str, body: ReviewSubmit):
    db = get_db()
    session = await db.sessions.find_one({"id": sid})
    if not session:
        raise HTTPException(404, "session not found")
    spans = [DetectedSpan(**s) for s in session.get("spans", [])]
    spans = apply_reviews(spans, body.decisions)
    for m in body.add_manual_spans:
        m.detector = "manual"
        m.review_status = "accepted"
        spans.append(m)
    iteration = int(session.get("review_iteration", 0)) + 1
    new_status = "awaiting_review" if body.continue_iteration else "applying_review"
    await db.sessions.update_one(
        {"id": sid},
        {"$set": {
            "spans": [s.model_dump() for s in spans],
            "status": new_status,
            "review_iteration": iteration,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
    )
    return {"status": new_status, "iteration": iteration, "span_count": len(spans)}


@app.post("/api/sessions/{sid}/finalize", dependencies=[Depends(require_api_token)])
async def session_finalize(sid: str):
    db = get_db()
    session = await db.sessions.find_one({"id": sid})
    if not session:
        raise HTTPException(404, "session not found")
    files = [FileArtifact(**f) for f in session.get("files", [])]
    spans = [DetectedSpan(**s) for s in session.get("spans", [])]

    async def emit(ev: ProgressEvent):
        await _emit(sid, ev)

    exports = await anonymize_files(files, spans, emit)
    await db.sessions.update_one(
        {"id": sid},
        {"$set": {"status": "complete", "export_paths": exports, "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    await emit(ProgressEvent(phase="complete", message="All files anonymized and ready to export", percent=100.0))
    await emit(ProgressEvent(phase="__end__", message="stream end"))
    return {"status": "complete", "exports": exports}


@app.get("/api/coverage-matrix")
async def coverage_matrix_endpoint():
    """Static coverage matrix used by the wizard result page and the bundle."""
    from phi_core.coverage_matrix import COVERAGE, TOOLS, coverage_counts
    return {"rows": COVERAGE, "tools": TOOLS, "counts": coverage_counts()}


@app.get("/api/classification-accuracy")
async def classification_accuracy_endpoint(details: bool = False):
    """Run the deterministic hard-rule layer over the shipped labelled corpus
    and return per-category precision/recall/F1 + method-appropriateness.

    Query params:
      - details=1 : include per-column predictions (useful for regression debugging).
    """
    from phi_core.validation import run_validation
    rep = run_validation()
    body = rep.to_dict()
    if not details:
        body.pop("predictions", None)
    return body


@app.get("/api/sessions/{sid}/bundle", dependencies=[Depends(require_api_token)])
async def session_bundle(sid: str, publication: bool = False, attestation_pdf: bool = False):
    """Assemble and stream the shareable bundle.

    Query params:
      - publication=1 : include the publication/ folder (coverage tables +
        figures + paper drafts + benchmark scaffold).
      - attestation_pdf=1 : reserved for signed PDF attestation.
    """
    from phi_core.bundle import BundleOptions, build_bundle
    db = get_db()
    session = await db.sessions.find_one({"id": sid}, {"_id": 0})
    if not session:
        raise HTTPException(404, "session not found")
    guard = session.get("guard_report") or {}
    if guard.get("status") == "blocked":
        raise HTTPException(403, "Publish Guard blocked one or more files; fix the pipeline first.")
    data, filename = build_bundle(session, BundleOptions(
        include_publication=publication, include_attestation_pdf=attestation_pdf,
    ))
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/sessions/{sid}/export/{file_id}", dependencies=[Depends(require_api_token)])
async def session_export(sid: str, file_id: str, force: bool = False):
    """Download the PHI-handled export.

    GOAL boundary: this is the point where 'input PHI data' becomes 'output
    ready to share publicly'. Refuse the download unless the Publish Guard
    marked this specific file 'clean' or 'skipped'. Set ``?force=true`` to
    override (recorded on the session document; use only after the operator
    has manually reviewed the findings).
    """
    db = get_db()
    session = await db.sessions.find_one({"id": sid}, {"_id": 0})
    if not session:
        raise HTTPException(404, "session not found")
    path = (session.get("export_paths") or {}).get(file_id)
    if not path or not Path(path).exists():
        raise HTTPException(404, "export not ready")
    guard = session.get("guard_report") or {}
    per_file = next((r for r in (guard.get("results") or []) if r.get("file_id") == file_id), None)
    status = (per_file or {}).get("status") if per_file else guard.get("status")
    if status == "blocked" and not force:
        # Use a JSONResponse so the body is not double-nested inside `detail`.
        return JSONResponse(status_code=403, content={
            "error": "publish_guard_blocked",
            "message": "Publish Guard blocked this export: residual PHI detected.",
            "guard": per_file,
        })
    return FileResponse(path, filename=Path(path).name)


# --- LLM settings (BYO-key) ----------------------------------------------

from typing import Literal


class LlmSettings(BaseModel):
    provider: Literal["emergent", "anthropic", "openai", "gemini", "openrouter", "openai_compatible"] = "emergent"
    model: str = "claude-sonnet-4-5-20250929"
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.1
    max_tokens: int = 2000


@app.get("/api/settings/llm")
async def get_llm_settings():
    db = get_db()
    doc = await db.settings.find_one({"_id": "llm"}, {"_id": 0})
    if not doc:
        return LlmSettings().model_dump() | {"providers": sorted(allowed_providers())}
    # never leak the api_key back verbatim
    if doc.get("api_key"):
        doc["api_key_set"] = True
        doc["api_key"] = ""
    return doc | {"providers": sorted(allowed_providers())}


@app.get("/api/settings/llm/catalog")
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


@app.get("/api/corpus/study/catalog")
async def corpus_study_catalog():
    from phi_corpus.scenarios import list_scenarios
    from phi_corpus.edge_cases import EDGE_CASES
    return {
        "scenarios": list_scenarios(),
        "edge_cases": [
            {"tag": e.tag, "label": e.label, "applies_to_column": e.applies_to_column}
            for e in EDGE_CASES.values()
        ],
    }


class CorpusStudyGenerateBody(BaseModel):
    scenario_id: str
    jurisdiction: str = "us"
    edge_case_tags: list[str] = []
    row_count: int = 8
    seed: int = 42


@app.post("/api/corpus/study/generate", dependencies=[Depends(require_api_token)])
async def corpus_study_generate(body: CorpusStudyGenerateBody):
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
        row_count=max(1, min(int(body.row_count or 8), 100)),
        seed=int(body.seed or 42),
    )
    # Reuse the existing session-create flow to get a canonical session
    # document with all defaults populated.
    sid = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    db = get_db()
    session_doc = {
        "id": sid,
        "created_at": now,
        "status": "corpus_ready",
        "jurisdiction": body.jurisdiction,
        "files": [],
        "agent_decisions": [],
        "corpus_ground_truth": art.ground_truth,
        "corpus_summary": art.ground_truth_summary,
    }
    await db.sessions.insert_one(session_doc)

    # Stash the zip bytes in a temp path so /intake can pick it up.
    tmp_path = Path("/app/data/corpus") / f"{sid}.zip"
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path.write_bytes(art.zip_bytes)

    return {
        "session_id": sid,
        "jurisdiction": body.jurisdiction,
        "scenario_id": body.scenario_id,
        "edge_case_tags": body.edge_case_tags,
        "summary": art.ground_truth_summary,
        "corpus_zip_size_bytes": len(art.zip_bytes),
        "corpus_zip_path": str(tmp_path),
    }


class CorpusStudyRunBody(BaseModel):
    scenario_id: str
    jurisdiction: str = "us"
    edge_case_tags: list[str] = []
    row_count: int = 8
    seed: int = 42


@app.post("/api/corpus/study/run", dependencies=[Depends(require_api_token)])
async def corpus_study_run(body: CorpusStudyRunBody):
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
        row_count=max(1, min(int(body.row_count or 8), 100)),
        seed=int(body.seed or 42),
    )
    sid = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    db = get_db()

    # Persist session with ground truth first (idempotent).
    await db.sessions.insert_one({
        "id": sid, "created_at": now, "status": "intake",
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
        {"id": sid},
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
    await session_handle(sid)

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
async def corpus_study_verify(sid: str):
    """Compare the pipeline's actual decisions against the corpus ground
    truth stored on the session document. Returns the full scored report
    from :func:`phi_corpus.verify.verify`."""
    from phi_corpus.verify import verify as _verify
    db = get_db()
    doc = await db.sessions.find_one({"id": sid}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "session not found")
    gt = doc.get("corpus_ground_truth")
    if not gt:
        raise HTTPException(400, "session has no corpus_ground_truth (not a corpus run)")

    # Map ground-truth file names to the pipeline's file_id so the
    # verifier can look up decisions correctly.
    name_map: dict[str, str] = {
        f.get("original_name", ""): f.get("file_id", "")
        for f in doc.get("files") or []
    }
    report = _verify(gt, doc.get("agent_decisions") or [], file_name_map=name_map)
    report["session_id"] = sid
    report["status"] = doc.get("status")
    return report


@app.post("/api/settings/llm", dependencies=[Depends(require_api_token)])
async def set_llm_settings(body: LlmSettings):
    validate_llm_provider(body.provider)
    validate_llm_base_url(body.base_url, body.provider)
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
    db = get_db()
    doc = await db.settings.find_one({"_id": "llm"}, {"_id": 0}) or {}
    if doc.get("api_key"):
        doc["api_key"] = decrypt_api_key(doc["api_key"])
    return LlmConfig.from_dict(doc)


# --- Agent-driven PHI handling -------------------------------------------

@app.post("/api/sessions/{sid}/handle", dependencies=[Depends(require_api_token)])
async def session_handle(sid: str):
    """Run the full 12-agent PHI handling pipeline for this study."""
    db = get_db()
    session = await db.sessions.find_one({"id": sid})
    if not session:
        raise HTTPException(404, "session not found")
    if session.get("intake_status") not in ("ready",):
        raise HTTPException(400, f"intake not ready (status={session.get('intake_status')})")

    async def emit_msg(msg: AgentMessage) -> None:
        # Persist to session progress in a compact form for the SSE consumer.
        ev = ProgressEvent(
            phase=f"agent:{msg.agent}:{msg.direction}",
            message=f"{msg.agent} {msg.phase}",
            payload={"agent": msg.agent, "phase_key": msg.phase, "direction": msg.direction, "duration_ms": msg.duration_ms},
        )
        await _emit(sid, ev)

    async def on_phase(phase: str, payload: dict):
        await _emit(sid, ProgressEvent(phase=f"agent_phase:{phase}", message=phase, payload=payload))

    cfg = await _current_llm_cfg()

    async def worker():
        try:
            await db.sessions.update_one({"id": sid}, {"$set": {"status": "reading"}})
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
                        await _emit(sid, ProgressEvent(phase="reading", message=f"header extract failed for {f['original_name']}: {e}"))
                files_hydrated.append(f)
            session["files"] = files_hydrated
            await db.sessions.update_one({"id": sid}, {"$set": {"files": files_hydrated, "status": "classifying"}})

            result = await run_agent_pipeline(session, db, cfg, emit_msg, on_phase)
            await _emit(sid, ProgressEvent(phase="complete", message=f"Pipeline done: {result.get('status')}", percent=100.0))
        except Exception as e:
            await db.sessions.update_one({"id": sid}, {"$set": {"status": "failed", "error": f"{type(e).__name__}: {e}"}})
            await _emit(sid, ProgressEvent(phase="failed", message=f"pipeline error: {e}"))
        finally:
            await _emit(sid, ProgressEvent(phase="__end__", message="stream end"))

    asyncio.create_task(worker())
    return {"status": "started", "llm": {"provider": cfg.provider, "model": cfg.model}}


class HumanReviewSubmit(BaseModel):
    resolutions: list[dict]   # [{column, file_id, action, reason?, confidence?}]
    reviewer: str = ""        # required: identity of the reviewer (email / initials / handle)
    comment: str = ""         # optional narrative for the audit trail
    # HHS §164.514(b)(2)(ii) "actual knowledge" attestation. IRB-required
    # procedural step separate from the technical Safe Harbor method.
    actual_knowledge_ack: bool = False


@app.post("/api/sessions/{sid}/human-review", dependencies=[Depends(require_api_token)])
async def session_human_review(sid: str, body: HumanReviewSubmit):
    """Operator resolves human_review decisions and resumes the pipeline tail
    (Executor -> Auditor -> Scout -> Ledger -> Herald).

    Per GOAL "human review invariant": every human decision must carry
    reviewer id + comment + timestamp. `reviewer` is required non-empty.
    Per HHS §164.514(b)(2)(ii): the reviewer must attest actual-knowledge
    that the remaining information alone or in combination cannot identify
    an individual. `actual_knowledge_ack` must be true.
    """
    from phi_core.agents.reasoning import Executor, Auditor
    from phi_core.agents.outward import Scout, Ledger, Herald

    reviewer = (body.reviewer or "").strip()
    if not reviewer:
        raise HTTPException(400, "reviewer identity is required (GOAL human review invariant)")
    if not body.actual_knowledge_ack:
        raise HTTPException(
            400,
            "actual-knowledge attestation is required (HHS 45 CFR 164.514(b)(2)(ii)): "
            "reviewer must confirm no actual knowledge that the remaining information "
            "alone or in combination could identify an individual.",
        )

    db = get_db()
    session = await db.sessions.find_one({"id": sid})
    if not session:
        raise HTTPException(404, "session not found")

    decisions = list(session.get("agent_decisions", []))
    ts = datetime.now(timezone.utc).isoformat()
    by_key = {(r.get("file_id",""), r.get("column","")): r for r in body.resolutions}
    per_decision_reviewed = False
    for d in decisions:
        k = (d.get("file_id",""), d.get("column",""))
        if d.get("action") == "human_review" and k in by_key:
            r = by_key[k]
            d["action"] = r.get("action", "human_review")
            d["reason"] = f"human decision by {reviewer}: " + (r.get("reason") or body.comment or "")
            d["confidence"] = 1.0
            d["reviewer"] = reviewer
            d["reviewer_comment"] = body.comment
            d["reviewed_at"] = ts
            d["actual_knowledge_ack"] = True  # HHS 164.514(b)(2)(ii) — gated at endpoint
            per_decision_reviewed = True

    # GOAL human review invariant: capture reviewer + comment + timestamp on
    # the session even when the operator accepted Sentinel-flagged decisions
    # globally without changing any individual action.
    session_review = {
        "reviewer": reviewer,
        "comment": body.comment,
        "reviewed_at": ts,
        "changed_decisions": per_decision_reviewed,
        "actual_knowledge_ack": True,  # gated at endpoint entry above
        "actual_knowledge_cite": "45 CFR 164.514(b)(2)(ii)",
    }

    # Any remaining unresolved?
    unresolved = [d for d in decisions if d.get("action") == "human_review"]
    if unresolved:
        await db.sessions.update_one({"id": sid}, {"$set": {"agent_decisions": decisions}})
        return {"status": "still_awaiting", "unresolved": len(unresolved)}

    files = session.get("files", [])
    cfg = await _current_llm_cfg()

    async def emit_msg(msg: AgentMessage) -> None:
        ev = ProgressEvent(
            phase=f"agent:{msg.agent}:{msg.direction}",
            message=f"{msg.agent} {msg.phase}",
            payload={"agent": msg.agent, "phase_key": msg.phase, "direction": msg.direction},
        )
        await _emit(sid, ev)

    async def worker():
        try:
            common = dict(session_id=sid, llm=cfg, db=db, emit=emit_msg)
            await db.sessions.update_one({"id": sid}, {"$set": {"status": "anonymizing", "agent_decisions": decisions, "human_review_required": False}})
            exec_out = await Executor(**common).run(files=files, decisions=decisions)
            # Publish Guard on the fresh exports before we mark complete.
            from phi_core.publish_guard import scan_all_exports as _scan_all_exports
            guard_report = _scan_all_exports(exec_out["exports"], decisions=decisions).to_dict()
            audit = await Auditor(**common).run(decisions=decisions, exports=exec_out["exports"], files=files)
            scout = await Scout(**common).run()
            ledger = await Ledger(**common).run(decisions=decisions, audit=audit, scout=scout, benchmark_result=None)
            herald = await Herald(**common).run(ledger=ledger, audit=audit,
                                                target_venue=session.get("target_venue") or "JAMIA Open")
            await db.sessions.update_one(
                {"id": sid},
                {"$set": {
                    "agent_audit": audit,
                    "agent_ledger": ledger,
                    "agent_herald": herald,
                    "agent_scout": scout,
                    "guard_report": guard_report,
                    "session_review": session_review,
                    "export_paths": exec_out["exports"],
                    "status": "complete",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }},
            )
            await _emit(sid, ProgressEvent(phase="complete", message="pipeline complete after human review", percent=100.0))
        except Exception as e:
            await db.sessions.update_one({"id": sid}, {"$set": {"status": "failed", "error": f"{type(e).__name__}: {e}"}})
            await _emit(sid, ProgressEvent(phase="failed", message=f"pipeline error: {e}"))
        finally:
            await _emit(sid, ProgressEvent(phase="__end__", message="stream end"))

    asyncio.create_task(worker())
    return {"status": "resuming"}


@app.get("/api/sessions/{sid}/agent-trace", dependencies=[Depends(require_api_token)])
async def session_agent_trace(sid: str, limit: int = 200):
    """Return the audit log of every agent message on this session."""
    from phi_core.security import scrub_nested as _scrub_nested
    db = get_db()
    cursor = db.agent_log.find({"session_id": sid}, {"_id": 0}).sort("ts", 1).limit(limit)
    msgs: list[dict] = []
    async for m in cursor:
        # SEC-006: agent-trace payloads are nested dicts (`prompt_preview`,
        # `reply_preview`) that echo dictionary/form PHI. Scrub every
        # string leaf recursively rather than only top-level string fields.
        msgs.append(_scrub_nested(m))
    return {"messages": msgs}


@app.get("/api/sessions/{sid}/preview", dependencies=[Depends(require_api_token)])
async def session_preview(sid: str, samples: int = 5):
    """Row-level review preview (Phase D).

    Returns up to ``samples`` (original-masked, redacted) cell pairs per
    dataset file so the reviewer can spot-check that the pipeline's
    per-column decisions are actually applied correctly. Original values
    are partial-masked; only the redacted column carries the string that
    will be written to the export.
    """
    from phi_core.preview import build_preview, MAX_SAMPLES_PER_FILE
    db = get_db()
    doc = await db.sessions.find_one({"id": sid}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "session not found")
    n = max(1, min(int(samples or MAX_SAMPLES_PER_FILE), 20))
    return build_preview(doc, max_samples_per_file=n)


@app.get("/api/sessions/{sid}/results", dependencies=[Depends(require_api_token)])
async def session_results(sid: str):
    """Consolidated agent outputs (decisions, audit, ledger, herald)."""
    db = get_db()
    doc = await db.sessions.find_one({"id": sid}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "session not found")
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
        "human_review_required": scrubbed.get("human_review_required", False),
    }


# Root health for quick check
@app.get("/")
async def root():
    return {"service": "phi-handling-console", "version": app.version}
