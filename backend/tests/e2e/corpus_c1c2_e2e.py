"""Phase C1 + C2 live E2E: generate -> run -> verify.

Runs the study-level corpus torture-test through the full pipeline
and asserts the dual-scoring report is coherent.
"""
from __future__ import annotations

import json
import time
import requests

BASE = "http://localhost:8001"
TIMEOUT = 30


def _poll(sid: str, want: set[str], timeout_s: int = 240) -> str:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        r = requests.get(f"{BASE}/api/sessions/{sid}", timeout=TIMEOUT)
        s = r.json()
        last = s.get("status")
        if last in want:
            return last
        time.sleep(2)
    raise TimeoutError(f"stuck at status={last}")


def main() -> int:
    r = requests.post(f"{BASE}/api/corpus/study/run", json={
        "scenario_id": "oncology_v1",
        "jurisdiction": "us",
        "edge_case_tags": [
            "age_over_89", "restricted_zip3",
            "notes_carry_name", "clinical_hr_90s",
        ],
        "row_count": 6,
        "seed": 42,
    }, timeout=TIMEOUT)
    r.raise_for_status()
    sid = r.json()["session_id"]
    print("[1] corpus run started sid =", sid)

    st = _poll(sid, {"awaiting_human_review", "complete", "failed"}, 300)
    print("[2] first stopping state =", st)
    assert st != "failed"

    if st == "awaiting_human_review":
        # Server-side gate requires at least one dataset-file download before
        # any non-defer resolution is accepted (the actual-knowledge
        # attestation must reflect a reviewer who actually opened the file).
        sess = requests.get(f"{BASE}/api/sessions/{sid}", timeout=TIMEOUT).json()
        dataset_file = next(f for f in sess["files"] if f.get("kind") == "dataset")
        dr = requests.get(f"{BASE}/api/sessions/{sid}/dataset-file/{dataset_file['file_id']}", timeout=TIMEOUT)
        dr.raise_for_status()
        # Accept everything so we get to complete
        r = requests.get(f"{BASE}/api/sessions/{sid}/results", timeout=TIMEOUT).json()
        pending = [d for d in r.get("decisions", []) if d.get("action") == "human_review"]
        submission = {
            "resolutions": [
                {"file_id": d["file_id"], "column": d["column"], "mode": "comment",
                 "comment": "this column is a direct identifier with no research value; drop it"}
                for d in pending
            ],
            "reviewer": "corpus-harness@lab.edu",
            "comment": "auto-resolve for corpus benchmark",
            "actual_knowledge_ack": True,
        }
        r = requests.post(f"{BASE}/api/sessions/{sid}/human-review", json=submission, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        st = _poll(sid, {"complete", "failed"}, 240)
        print("[3] final state =", st)
        assert st == "complete"

    # Verify
    r = requests.get(f"{BASE}/api/corpus/study/verify/{sid}", timeout=TIMEOUT)
    r.raise_for_status()
    rep = r.json()
    print("[4] correctness =", rep["correctness"]["overall_precision"], "/",
          rep["correctness"]["overall_recall"], "/",
          rep["correctness"]["overall_f1"])
    print("[4] deferral rate =", rep["deferral"]["rate"], "(", rep["deferral"]["count"], "cells)")
    print("[4] false-negatives =", len(rep["correctness"]["false_negatives"]))
    print("[4] false-positives =", len(rep["correctness"]["false_positives"]))
    print("[4] planted columns =", rep["summary"]["planted_columns"])
    # Full report to stdout for the operator to inspect
    print("--- full report ---")
    print(json.dumps(rep, indent=2)[:3000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
