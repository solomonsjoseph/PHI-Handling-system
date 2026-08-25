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
    "created", "intake", "reading", "classifying", "anonymizing",
    "awaiting_human_review", "partially_complete", "complete", "cancelled", "failed",
    "blocked", "corpus_ready", "intake_failed",
]


class DetectedSpan(BaseModel):
    start: int
    end: int
    value: str
    entity_type: str
    hipaa_category: Optional[str] = None
    detector: str  # "presidio" | "rule" | "merged"
    confidence: float = 1.0
    authority: str = ""
    review_status: Literal["pending", "accepted", "rejected", "reclassified"] = "pending"
    replacement: Optional[str] = None


class FileArtifact(BaseModel):
    file_id: str = Field(default_factory=_uid)
    original_name_encrypted: str
    size_bytes: int
    sha256: str
    kind: Literal["dataset", "narrative", "metadata"]
    subtype: str  # csv, xlsx, parquet, pdf, docx, txt, eml, md
    stored_path: str
    component: Optional[str] = None  # datasets|forms|data_dictionary|mappings (from intake v3)
    columns: list[str] = []           # dataset only
    row_count: int = 0                # dataset only


class ProgressEvent(BaseModel):
    ts: str = Field(default_factory=_now)
    phase: str
    message: str
    percent: float = 0.0
    payload: dict[str, Any] = Field(default_factory=dict)


class Session(BaseModel):
    id: str = Field(default_factory=_uid)
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)
    status: SessionStatus = "created"
    owner: str = ""
    jurisdiction: str = "us"
    intake_status: Literal["none", "ready", "review_required", "failed"] = "none"
    intake_exit_code: int = 0
    intake_review: list[dict[str, Any]] = []
    intake_missing: list[str] = []
    files: list[FileArtifact] = []
    progress: list[ProgressEvent] = []
    export_paths: dict[str, str] = {}
    error: str = ""
    error_id: str = ""  # 4.23: correlation id for a "pipeline failed" error; log holds detail


