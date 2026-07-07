from __future__ import annotations

import json
import os
import re
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from harness.run_phi_system import main


REPO_ROOT = Path(__file__).resolve().parents[1]
STUDY_PREFIX = "PytestPhiSys"
TEST_PHI_KEY_HEX = "0" * 64  # Hermetic test key; never touch the shared operator key.
RID_RE = re.compile(r"^RID_[A-Z0-9]{1,16}_[a-p]{12}$")


@dataclass(frozen=True)
class PhiSystemRun:
    study: str
    jurisdiction: str
    out_dir: Path
    result: dict
    published_dir: Path
    ledger_path: Path


@pytest.fixture
def tmp_phi_system_study():
    original_study_name = os.environ.get("STUDY_NAME")
    original_phi_key_path = os.environ.get("PHI_KEY_PATH")
    created_studies: list[str] = []

    def run(jurisdiction: str, tmp_path: Path) -> PhiSystemRun:
        study = f"{STUDY_PREFIX}{jurisdiction.upper()}{uuid.uuid4().hex[:8]}"
        created_studies.append(study)
        os.environ["STUDY_NAME"] = study
        key_path = tmp_path / "phi_key"
        key_path.write_text(TEST_PHI_KEY_HEX, encoding="utf-8")
        key_path.chmod(0o600)
        os.environ["PHI_KEY_PATH"] = str(key_path)
        _drop_phi_runtime_modules()

        out_dir = tmp_path / f"out-{jurisdiction}"
        exit_code = main(
            [
                "--study",
                study,
                "--jurisdiction",
                jurisdiction,
                "--seed",
                "42",
                "--n-subjects",
                "8",
                "--out-dir",
                str(out_dir),
            ]
        )

        assert exit_code == 0
        result_path = out_dir / "phi_system_result.json"
        assert result_path.is_file()
        result = json.loads(result_path.read_text(encoding="utf-8"))

        return PhiSystemRun(
            study=study,
            jurisdiction=jurisdiction,
            out_dir=out_dir,
            result=result,
            # Standalone refactor: the pipeline PUBLISHES to
            # output/{STUDY}/llm_source/datasets/ (moved there from staging
            # only once the residual guard passes) -- staging itself is
            # emptied by a successful publish, so a smoke assertion must read
            # the publish tree, not the (now-drained) staging tree.
            published_dir=REPO_ROOT / "output" / study / "llm_source" / "datasets",
            ledger_path=out_dir / "gold_ledger.jsonl",
        )

    try:
        yield run
    finally:
        if original_study_name is None:
            os.environ.pop("STUDY_NAME", None)
        else:
            os.environ["STUDY_NAME"] = original_study_name
        if original_phi_key_path is None:
            os.environ.pop("PHI_KEY_PATH", None)
        else:
            os.environ["PHI_KEY_PATH"] = original_phi_key_path

        for study in created_studies:
            shutil.rmtree(REPO_ROOT / "phi_engine" / "config" / study, ignore_errors=True)
            shutil.rmtree(REPO_ROOT / "tmp" / study, ignore_errors=True)
            shutil.rmtree(REPO_ROOT / "output" / study, ignore_errors=True)
            # Standalone refactor: this test does not set PHI_WORKSPACE, so
            # intake_add/organize (called internally by the demoted harness
            # via run_pipeline) write into the repo root's own intake/,
            # organized/, and data/raw/ -- must be cleaned up too, or every
            # test run pollutes the working tree with per-study leftovers.
            shutil.rmtree(REPO_ROOT / "intake" / study, ignore_errors=True)
            shutil.rmtree(REPO_ROOT / "organized" / study, ignore_errors=True)
            shutil.rmtree(REPO_ROOT / "data" / "raw" / study, ignore_errors=True)
        _drop_phi_runtime_modules()


def _drop_phi_runtime_modules() -> None:
    """Force STUDY_NAME-derived phi_engine paths to resolve fresh per smoke run.

    Blanket sweep (standalone refactor): every ``phi_engine.*`` submodule
    that resolves a workspace-relative path at IMPORT time (most bind
    ``import phi_engine.config.config as config`` once at module load) must
    be dropped from ``sys.modules``, not just the handful the old single-form
    driver happened to import directly -- the standalone pipeline pulls in
    ``phi_engine.pipeline.*``, ``phi_engine.audit.review_paths``, and more.
    An explicit allowlist would silently go stale the next time a new
    pipeline module is added; a prefix sweep cannot.
    """
    for name in list(sys.modules):
        if name == "phi_engine" or name.startswith("phi_engine."):
            del sys.modules[name]


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _assert_phi_system_contract(run: PhiSystemRun) -> None:
    result = run.result

    assert {
        "ai_layer",
        "dates",
        "fail_closed",
        "pseudonyms",
        "redaction",
        "residual",
        "scrub_raised",
    }.issubset(result)
    assert result["scrub_raised"] is None

    redaction = result["redaction"]
    assert redaction["total_gold_phi_cells"] > 0
    assert isinstance(redaction["redacted"], int)
    assert 0 <= redaction["redacted"] <= redaction["total_gold_phi_cells"]
    assert isinstance(redaction["redaction_recall"], float)
    assert isinstance(redaction["leaks"], list)

    pseudonyms = result["pseudonyms"]
    assert pseudonyms["cells_checked"] > 0
    assert pseudonyms["regex_pass_count"] == pseudonyms["cells_checked"]
    assert pseudonyms["cross_form_linkage_ok"] == pseudonyms["cross_form_linkage_subjects_checked"]
    _assert_scrubbed_ids_match_rid_pattern(run.published_dir)

    fail_closed = result["fail_closed"]
    assert fail_closed["quarantine_matches_planted"] is True
    assert fail_closed["blank_matches_planted"] is True
    assert fail_closed["age_cap_matches_planted"] is True

    assert _published_jsonl_text(run.published_dir)
    leaked_values = _leaked_gold_values(run.ledger_path, run.published_dir)
    assert leaked_values == []


def _assert_scrubbed_ids_match_rid_pattern(published_dir: Path) -> None:
    checked = 0
    for path in sorted(published_dir.glob("*.jsonl")):
        for row in _read_jsonl(path):
            for column in ("SUBJID", "IC_SCRNNUM"):
                value = row.get(column)
                if value:
                    checked += 1
                    assert RID_RE.fullmatch(value), (path.name, column, value)
    assert checked > 0


def _published_jsonl_text(published_dir: Path) -> str:
    chunks: list[str] = []
    for path in sorted(published_dir.rglob("*.jsonl")):
        if "quarantine" in path.relative_to(published_dir).parts:
            continue
        chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks)


def _leaked_gold_values(ledger_path: Path, published_dir: Path) -> list[str]:
    published = _published_jsonl_text(published_dir)
    leaked: list[str] = []
    for entry in _read_jsonl(ledger_path):
        if "row_index" not in entry:
            continue
        if entry.get("expected_action") == "keep":
            continue
        original = entry["original_value"]
        if original and original in published:
            leaked.append(original)
    return leaked


def test_india_phi_system_smoke_scrubs_published_outputs(tmp_phi_system_study, tmp_path):
    run = tmp_phi_system_study("in", tmp_path)

    _assert_phi_system_contract(run)


def test_us_phi_system_smoke_scrubs_published_outputs(tmp_phi_system_study, tmp_path):
    run = tmp_phi_system_study("us", tmp_path)

    _assert_phi_system_contract(run)
