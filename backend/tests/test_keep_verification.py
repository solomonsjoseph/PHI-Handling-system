"""Value-level verification tests for Sentinel keep decisions."""
from types import SimpleNamespace

from phi_core.agents.reasoning import verify_keep_decisions


def _decision(column: str, action: str = "keep", file_id: str = "dataset.csv") -> dict:
    return {
        "file_id": file_id,
        "column": column,
        "action": action,
        "reason": "Judge decision",
    }


def test_detector_hit_demotes_keep_without_recording_cell_contents(tmp_path, monkeypatch):
    source = tmp_path / "dataset.csv"
    source.write_text("barcode\nsafe-token\n", encoding="utf-8")
    monkeypatch.setattr(
        "phi_core.agents.reasoning.detect_text",
        lambda *_args, **_kwargs: [SimpleNamespace(hipaa_category="A")],
    )

    decisions, demotions = verify_keep_decisions([_decision("barcode")], {"dataset.csv": source})

    assert decisions[0]["action"] == "human_review"
    assert demotions == [{
        "file_id": "dataset.csv",
        "column": "barcode",
        "from": "keep",
        "to": "human_review",
        "detector": "A",
        "citation": "45 CFR 164.514(b)(2)(i)",
    }]
    assert "safe-token" not in str(decisions)
    assert "safe-token" not in str(demotions)


def test_jurisdiction_pattern_hit_demotes_keep(tmp_path, monkeypatch):
    source = tmp_path / "dataset.csv"
    source.write_text("age\nage 95\n", encoding="utf-8")
    monkeypatch.setattr("phi_core.agents.reasoning.detect_text", lambda *_args, **_kwargs: [])

    decisions, demotions = verify_keep_decisions([_decision("age")], {"dataset.csv": source})

    assert decisions[0]["action"] == "human_review"
    assert demotions[0]["detector"] == "AGE_OVER_89"


def test_unreadable_source_demotes_every_keep_for_that_file(tmp_path):
    missing = tmp_path / "unreadable.csv"
    decisions = [_decision("first"), _decision("second")]

    verified, demotions = verify_keep_decisions(decisions, {"dataset.csv": missing})

    assert [d["action"] for d in verified] == ["human_review", "human_review"]
    assert [d["detector"] for d in demotions] == ["unreadable", "unreadable"]


def test_non_keep_and_missing_source_decisions_are_unchanged(tmp_path):
    source = tmp_path / "dataset.csv"
    source.write_text("value\nsafe-token\n", encoding="utf-8")
    decisions = [
        _decision("value", action="drop"),
        _decision("value", file_id="not-in-datasets.csv"),
    ]

    verified, demotions = verify_keep_decisions(decisions, {"dataset.csv": source})

    assert verified == decisions
    assert demotions == []
