"""Row-level review preview tests (Phase D).

The reviewer must be able to spot-check the pipeline's per-column
decisions without the preview endpoint itself becoming a PHI leak surface.
Two invariants:

1. ``samples`` count per file is bounded by ``max_samples_per_file``.
2. ``original_masked`` never returns a raw value verbatim (must be masked)
   even when the raw cell contains PHI.
"""
from __future__ import annotations

from pathlib import Path

from phi_core.preview import (
    _mask_original, build_preview, MAX_SAMPLES_PER_FILE
)


def _write_csv(tmp_path: Path, name: str, rows: list[list[str]]) -> Path:
    p = tmp_path / name
    import csv as _csv
    with p.open("w", newline="") as f:
        w = _csv.writer(f)
        for r in rows:
            w.writerow(r)
    return p


def test_mask_original_hides_middle():
    """Partial-mask must reveal only first + last two chars."""
    assert _mask_original("James Smith") == "Ja*******th"
    assert _mask_original("415-555-1234") == "41********34"
    assert _mask_original("abc") == "***"
    assert _mask_original("") == ""


def test_build_preview_returns_bounded_sample_count(tmp_path: Path):
    """samples per file must not exceed max_samples_per_file."""
    rows = [["patient_id", "name", "phone"]]
    for i in range(50):
        rows.append([f"P{i:03d}", f"Name{i}", f"415-555-{i:04d}"])
    p = _write_csv(tmp_path, "enrollment.csv", rows)

    session = {
        "id": "sess-preview-001",
        "files": [{
            "file_id": "f_enroll",
            "original_name": "enrollment.csv",
            "kind": "dataset",
            "subtype": "csv",
            "stored_path": str(p),
        }],
        "agent_decisions": [
            {"file_id": "f_enroll", "column": "patient_id", "action": "pseudonymize"},
            {"file_id": "f_enroll", "column": "name", "action": "drop"},
            {"file_id": "f_enroll", "column": "phone", "action": "drop"},
        ],
    }
    prev = build_preview(session, max_samples_per_file=5)
    assert prev["session_id"] == "sess-preview-001"
    assert len(prev["files"]) == 1
    assert len(prev["files"][0]["samples"]) <= 5


def test_preview_original_never_returned_raw(tmp_path: Path):
    """No sample's `original_masked` may equal the raw cell value.
    This is the anti-leak invariant of the preview surface."""
    raw = "james.smith@example.edu"
    p = _write_csv(tmp_path, "leaky.csv", [
        ["patient_id", "email"],
        ["P001", raw],
    ])
    session = {
        "id": "sess-preview-002",
        "files": [{
            "file_id": "f_leak",
            "original_name": "leaky.csv",
            "kind": "dataset",
            "subtype": "csv",
            "stored_path": str(p),
        }],
        "agent_decisions": [
            {"file_id": "f_leak", "column": "email", "action": "drop"},
            {"file_id": "f_leak", "column": "patient_id", "action": "pseudonymize"},
        ],
    }
    prev = build_preview(session, max_samples_per_file=5)
    samples = prev["files"][0]["samples"]
    assert samples, "preview should have at least one sample"
    for s in samples:
        assert s.get("original_masked") != raw, "PHI leaked verbatim via preview"


def test_preview_redacted_reflects_chosen_action(tmp_path: Path):
    """The `redacted` field must match what the export actually contains."""
    p = _write_csv(tmp_path, "leaky.csv", [
        ["patient_id", "email"],
        ["P001", "james.smith@example.edu"],
    ])
    session = {
        "id": "sess-preview-003",
        "files": [{
            "file_id": "f_leak",
            "original_name": "leaky.csv",
            "kind": "dataset",
            "subtype": "csv",
            "stored_path": str(p),
        }],
        "agent_decisions": [
            {"file_id": "f_leak", "column": "email", "action": "drop"},
            {"file_id": "f_leak", "column": "patient_id", "action": "pseudonymize"},
        ],
    }
    prev = build_preview(session, max_samples_per_file=5)
    by_col = {s["column"]: s for s in prev["files"][0]["samples"]}
    # drop action -> empty string
    assert by_col["email"]["redacted"] == ""
    # pseudonymize -> P + hex
    assert by_col["patient_id"]["redacted"].startswith("P")



def test_preview_keep_action_masks_redacted_value(tmp_path: Path):
    """A `keep` decision must not leak the raw cell through `redacted`;
    the reviewer only needs the shape, and `masked` must say so."""
    p = _write_csv(tmp_path, "clinical.csv", [
        ["heart_rate_bpm"],
        ["oo88oo"],
    ])
    session = {
        "id": "sess-preview-004",
        "files": [{
            "file_id": "f_keep",
            "original_name": "clinical.csv",
            "kind": "dataset",
            "subtype": "csv",
            "stored_path": str(p),
        }],
        "agent_decisions": [
            {"file_id": "f_keep", "column": "heart_rate_bpm", "action": "keep"},
        ],
    }
    prev = build_preview(session, max_samples_per_file=5)
    for s in prev["files"][0]["samples"]:
        assert s["redacted"] != "oo88oo", "keep action leaked the raw cell via preview"
        assert s["masked"] is True


def test_preview_skips_non_dataset_files(tmp_path: Path):
    """Metadata and narrative files should not appear in preview."""
    p = tmp_path / "consent.txt"
    p.write_text("Patient: James Smith")
    session = {
        "id": "sess-preview-004",
        "files": [{
            "file_id": "f_narr",
            "original_name": "consent.txt",
            "kind": "narrative",
            "subtype": "txt",
            "stored_path": str(p),
        }],
        "agent_decisions": [],
    }
    prev = build_preview(session)
    assert prev["files"] == []


def test_preview_default_max_samples_constant():
    """API surface: constant used by the endpoint clamp."""
    assert MAX_SAMPLES_PER_FILE == 5
