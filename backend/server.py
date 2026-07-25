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
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from phi_core.benchmark import run_benchmark
from phi_core.db import get_db
from phi_core.generators import corpus_hash, generate, HIPAA_CATEGORIES
from phi_core.intake import (
    COMPONENT_SUFFIXES, MANDATORY, ANY_OF, build_manifest,
)
from phi_core.models import (
    BenchmarkRequest, CorpusRecord, CorpusRequest, DetectedSpan,
    FileArtifact, ProgressEvent, ReviewDecision, Session,
)
from phi_core.pipeline import (
    UPLOAD_DIR, anonymize_files, apply_reviews, classify_file, detect_file, ingest_file,
)


app = FastAPI(title="PHI Handling Console", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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

@app.post("/api/corpus/generate")
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

@app.post("/api/benchmark/run")
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


@app.post("/api/sessions")
async def session_create(body: SessionCreate):
    s = Session(jurisdiction=body.jurisdiction)
    await get_db().sessions.insert_one(s.model_dump())
    return s.model_dump()


@app.get("/api/sessions/{sid}")
async def session_get(sid: str):
    doc = await get_db().sessions.find_one({"id": sid}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "session not found")
    return doc


@app.get("/api/sessions")
async def session_list():
    cursor = get_db().sessions.find({}, {"_id": 0, "spans": 0, "progress": 0}).sort("created_at", -1).limit(50)
    return {"sessions": [s async for s in cursor]}


@app.post("/api/sessions/{sid}/upload")
async def session_upload(sid: str, file: UploadFile = File(...)):
    db = get_db()
    session = await db.sessions.find_one({"id": sid})
    if not session:
        raise HTTPException(404, "session not found")
    session_dir = UPLOAD_DIR / sid
    session_dir.mkdir(parents=True, exist_ok=True)
    dst = session_dir / (file.filename or "upload.bin")
    with dst.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    art = FileArtifact(
        original_name=file.filename or "upload.bin",
        size_bytes=dst.stat().st_size,
        sha256="",
        kind="narrative",
        subtype="txt",
        stored_path=str(dst),
    )
    await db.sessions.update_one(
        {"id": sid},
        {"$push": {"files": art.model_dump()}, "$set": {"updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    return {"file_id": art.file_id, "stored_path": str(dst), "size_bytes": art.size_bytes}


@app.post("/api/sessions/{sid}/intake")
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
    zip_path = session_dir / "intake.zip"
    with zip_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

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
            sha256="",
            kind=kind,
            subtype=ext,
            stored_path=e.stored_path,
            component=e.component,
        ))

    await db.sessions.update_one(
        {"id": sid},
        {"$set": {
            "files": [f.model_dump() for f in accepted],
            "intake_status": manifest.status,
            "intake_exit_code": manifest.exit_code,
            "intake_review": [
                {"relpath": e.relpath, "reason": e.reason} for e in manifest.entries if e.component == "_unclassified"
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
        "missing_components": manifest.missing_components,
        "review_entries": [
            {"relpath": e.relpath, "reason": e.reason} for e in manifest.entries if e.component == "_unclassified"
        ],
        "accepted_by_component": {
            comp: [{"file_id": a.file_id, "name": a.original_name, "size": a.size_bytes} for a in accepted if a.component == comp]
            for comp in COMPONENT_SUFFIXES
        },
        "error": manifest.error,
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
                "one_of_group": "forms_or_dictionary_or_mappings" if k in ANY_OF else None,
            }
            for k, v in COMPONENT_SUFFIXES.items()
        },
        "rules": [
            "datasets is mandatory",
            "at least one of forms, data_dictionary, or mappings is required",
            "dataset xlsx must be single-sheet",
            ".json and .jsonl are NOT accepted as datasets",
            "unsupported extensions land in the _unclassified review bucket and block the study",
            "symlinks and absolute paths in the ZIP are rejected",
            "per-file 200 MB cap",
        ],
        "exit_codes": {"0": "ready", "8": "review_required", "2": "failed"},
        "authority": "45 CFR 164.514(b)(2)(i) headers-only for datasets; classification runs across all components",
    }


@app.post("/api/sessions/{sid}/run")
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


@app.get("/api/sessions/{sid}/stream")
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


@app.post("/api/sessions/{sid}/review")
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


@app.post("/api/sessions/{sid}/finalize")
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


@app.get("/api/sessions/{sid}/export/{file_id}")
async def session_export(sid: str, file_id: str):
    db = get_db()
    session = await db.sessions.find_one({"id": sid}, {"_id": 0})
    if not session:
        raise HTTPException(404, "session not found")
    path = (session.get("export_paths") or {}).get(file_id)
    if not path or not Path(path).exists():
        raise HTTPException(404, "export not ready")
    return FileResponse(path, filename=Path(path).name)


# Root health for quick check
@app.get("/")
async def root():
    return {"service": "phi-handling-console", "docs": "/docs"}
