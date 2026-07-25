"""Pydantic + BSON models for PHI pipeline sessions.

Every record maps to AUTHORITY_MATRIX.md. Ids are string uuids so MongoDB
ObjectIds never leak to the API.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


def _uid() -> str:
    return uuid4().hex


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


SessionStatus = Literal[
    "created",
    "intake",
    "reading",
    "classifying",
    "detecting",
    "awaiting_review",
    "applying_review",
    "anonymizing",
    "complete",
    "failed",
]


class GoldSpan(BaseModel):
    start: int
    end: int
    value: str
    category: str
    hipaa_category: Optional[str] = None
    entity_type: str
    jurisdiction: str = "us"
    authority: str = ""


class DetectedSpan(BaseModel):
    span_id: str = Field(default_factory=_uid)
    file_id: Optional[str] = None      # populated by the pipeline for anonymizer filtering
    start: int
    end: int
    value: str
    entity_type: str
    hipaa_category: Optional[str] = None
    detector: str  # "presidio" | "rule" | "merged"
    confidence: float = 1.0
    authority: str = ""
    column: Optional[str] = None      # column name for dataset cells
    row_index: Optional[int] = None   # dataset row index
    file_offset: Optional[int] = None # for narrative files
    review_status: Literal["pending", "accepted", "rejected", "reclassified"] = "pending"
    review_comment: str = ""
    replacement: Optional[str] = None


class FileArtifact(BaseModel):
    file_id: str = Field(default_factory=_uid)
    original_name: str
    size_bytes: int
    sha256: str
    kind: Literal["dataset", "narrative", "metadata"]
    subtype: str  # csv, xlsx, parquet, pdf, docx, txt, eml, md
    stored_path: str
    component: Optional[str] = None  # datasets|forms|data_dictionary|mappings (from intake v3)
    columns: list[str] = []           # dataset only
    row_count: int = 0                # dataset only
    text_preview: str = ""            # narrative/metadata only
    llm_classification: dict[str, Any] = Field(default_factory=dict)


class ProgressEvent(BaseModel):
    ts: str = Field(default_factory=_now)
    phase: str
    message: str
    percent: float = 0.0
    payload: dict[str, Any] = Field(default_factory=dict)


class ReviewDecision(BaseModel):
    span_id: str
    action: Literal["accept", "reject", "reclassify"]
    replacement: Optional[str] = None
    new_category: Optional[str] = None
    comment: str = ""


class Session(BaseModel):
    id: str = Field(default_factory=_uid)
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)
    status: SessionStatus = "created"
    jurisdiction: str = "us"
    intake_status: Literal["none", "ready", "review_required", "failed"] = "none"
    intake_exit_code: int = 0
    intake_review: list[dict[str, Any]] = []
    intake_missing: list[str] = []
    files: list[FileArtifact] = []
    spans: list[DetectedSpan] = []
    progress: list[ProgressEvent] = []
    review_iteration: int = 0
    export_paths: dict[str, str] = {}
    error: str = ""


class CorpusRequest(BaseModel):
    jurisdiction: str = "us"
    seed: int = 20260420
    count_per_category: int = 5
    include_quasi_identifiers: bool = True


class CorpusRecord(BaseModel):
    record_id: str
    text: str
    layer: str
    jurisdiction: str
    gold_spans: list[GoldSpan]
    authority_citations: list[str]
    metadata: dict[str, Any] = Field(default_factory=dict)


class BenchmarkRequest(BaseModel):
    corpus_id: str
    detectors: list[str] = ["presidio", "rule"]


class BenchmarkResult(BaseModel):
    id: str = Field(default_factory=_uid)
    created_at: str = Field(default_factory=_now)
    corpus_id: str
    detectors: list[str]
    total_records: int
    total_gold_spans: int
    tp: int
    fp: int
    fn: int
    precision: float
    recall: float
    f1: float
    per_category: dict[str, dict[str, float]] = {}
    per_detector: dict[str, dict[str, float]] = {}
