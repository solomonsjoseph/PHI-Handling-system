"""Phase F — end-to-end IRB-readiness verification.

The pipeline may or may not require human review depending on Sentinel's
verdict on the exact input. This script covers BOTH paths deterministically
by manually flipping a completed session into ``awaiting_human_review``
to exercise the Phase D + E gates, then completing it back to done.

Steps:
  1. POST /api/sessions  -> sid
  2. POST /api/sessions/{sid}/intake  (manifest ZIP with datasets + dictionary)
  3. POST /api/sessions/{sid}/handle  -> classifies + anonymises + emits exports
  4. Poll to `complete` or `awaiting_human_review`
  5. If Sentinel approved (complete): manually flip Mongo to awaiting_human_review
     with one flagged decision so we can exercise Phase D + E gates
  6. GET /dataset-file/{file_id} -> confirm the raw original bytes are served
     byte-identical, no CSV/XLSX parsing in that code path
  7. POST /human-review with actual_knowledge_ack=false -> 400
  8. POST /human-review mode="approve" + actual_knowledge_ack=true -> 200
  9. Poll to `complete`
 10. GET /bundle -> unzip -> assert attestation.json.actual_knowledge_ack is True
     and is_partial is False (nothing deferred)
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "phi_handling")

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402


BASE = "http://localhost:8001"
TIMEOUT = 30


def _mk_manifest_zip() -> bytes:
    enrollment_csv = (
        "patient_id,name,dob,phone,zip,age,notes\n"
        "P001,James Smith,1975-03-15,415-555-1234,94103,50,Enrolled at UCSF\n"
        "P002,Mary Jones,1982-07-22,415-555-9876,94104,43,Screened at Kaiser\n"
        "P003,Peter Wong,1990-11-01,415-555-5555,94105,35,Withdrew after 2 visits\n"
    )
    columns_csv = (
        "column_name,description,type\n"
        "patient_id,Patient identifier (study-scoped),string\n"
        "name,Full patient name (PHI cat A),string\n"
        "dob,Date of birth (PHI cat C),date\n"
        "phone,Phone number (PHI cat D),string\n"
        "zip,ZIP code (PHI cat B),string\n"
        "age,Age in years,int\n"
        "notes,Free-text encounter notes,string\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("datasets/enrollment.csv", enrollment_csv)
        z.writestr("dictionary/columns.csv", columns_csv)
    return buf.getvalue()


def _poll(sid: str, want: set[str], timeout_s: int = 180) -> str:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        r = requests.get(f"{BASE}/api/sessions/{sid}", timeout=TIMEOUT)
        s = r.json()
        last = s.get("status")
        if last in want:
            return last
        time.sleep(2)
    raise TimeoutError(f"polling timed out at status={last}, want={want}")


async def _force_awaiting_human_review(sid: str) -> None:
    """Flip a completed session back into awaiting_human_review with one
    flagged decision so Phase D+E gates can be exercised deterministically."""
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]
    doc = await db.sessions.find_one({"id": sid})
    assert doc, f"session {sid} not found in mongo"
    decisions = list(doc.get("agent_decisions") or [])
    # Turn the first decision into `human_review` so the operator has one
    # tick to make; also clear session_review to simulate a pre-review state.
    # suggested_action mirrors what Judge would have set on its own
    # `human_review` proposal, so the e2e client can resolve it via the
    # real mode="approve" path rather than reaching around the contract.
    if decisions:
        decisions[0] = {
            **decisions[0],
            "action": "human_review",
            "reason": "forced into human_review by e2e harness",
            "suggested_action": "drop",
            "suggested_confidence": 0.5,
            "suggested_reason": "forced into human_review by e2e harness",
        }
    await db.sessions.update_one(
        {"id": sid},
        {"$set": {
            "status": "awaiting_human_review",
            "agent_decisions": decisions,
            "session_review": [],
            "pending_review": [],
            "guard_report": {"status": "clean", "scanned": 0, "blocked": 0, "results": []},
        }},
    )
    client.close()


def main() -> int:
    r = requests.post(f"{BASE}/api/sessions", json={"jurisdiction": "us"}, timeout=TIMEOUT)
    r.raise_for_status()
    sid = r.json()["id"]
    print("[1] session_id =", sid)

    zip_bytes = _mk_manifest_zip()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as _zsrc:
        enrollment_csv_bytes = _zsrc.read("datasets/enrollment.csv")
    r = requests.post(
        f"{BASE}/api/sessions/{sid}/intake",
        files={"file": ("study.zip", zip_bytes, "application/zip")},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    assert r.json().get("status") in ("ready", "ok", "review"), r.json()
    print("[2] intake status =", r.json().get("status"))

    r = requests.post(f"{BASE}/api/sessions/{sid}/handle", timeout=TIMEOUT)
    r.raise_for_status()
    print("[3] handle started =", r.json())

    st = _poll(sid, {"awaiting_human_review", "complete", "failed"}, timeout_s=240)
    print("[4] first stopping state =", st)
    assert st != "failed"

    if st == "complete":
        # Force human-review path so we can exercise Phase D + E gates.
        print("[5] Sentinel approved on first pass; flipping to awaiting_human_review "
              "to exercise Phase D/E gates.")
        asyncio.run(_force_awaiting_human_review(sid))
        st = "awaiting_human_review"

    # Phase D: raw dataset-file download -- byte-identical to the original
    # upload, no backend parsing, so a reviewer's own tool sees the real file.
    sess = requests.get(f"{BASE}/api/sessions/{sid}", timeout=TIMEOUT).json()
    dataset_file = next(f for f in sess["files"] if f.get("kind") == "dataset")
    r = requests.get(f"{BASE}/api/sessions/{sid}/dataset-file/{dataset_file['file_id']}", timeout=TIMEOUT)
    r.raise_for_status()
    assert r.content == enrollment_csv_bytes, "dataset-file download must be byte-identical to the upload"
    print("[6] dataset-file download byte-identical -> OK")

    # Phase E: gate MUST reject actual_knowledge_ack=false
    body = {
        "resolutions": [],
        "reviewer": "sir@lab.edu",
        "comment": "e2e",
        "actual_knowledge_ack": False,
    }
    r = requests.post(f"{BASE}/api/sessions/{sid}/human-review", json=body, timeout=TIMEOUT)
    assert r.status_code == 400, r.text
    assert "actual" in r.text.lower() and "knowledge" in r.text.lower()
    assert "45 CFR 164.514(b)(2)(ii)" in r.text
    print("[7] HTTP 400 gate on ack=False -> OK (cited 45 CFR 164.514(b)(2)(ii))")

    # Now approve every human_review decision (applies its own suggested_action)
    # and submit with ack=true.
    res = requests.get(f"{BASE}/api/sessions/{sid}/results", timeout=TIMEOUT).json()
    pending = [d for d in res.get("decisions", []) if d.get("action") == "human_review"]
    body["resolutions"] = [
        {"file_id": d["file_id"], "column": d["column"], "mode": "approve"} for d in pending
    ]
    body["actual_knowledge_ack"] = True
    r = requests.post(f"{BASE}/api/sessions/{sid}/human-review", json=body, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    print("[8] submit mode=approve, ack=True -> HTTP 200, status =", r.json().get("status"))

    st = _poll(sid, {"complete", "failed"}, timeout_s=240)
    print("[9] final state =", st)
    assert st == "complete", f"failed: {requests.get(f'{BASE}/api/sessions/{sid}').json()}"

    # Bundle
    r = requests.get(f"{BASE}/api/sessions/{sid}/bundle", timeout=60)
    assert r.status_code == 200, f"bundle failed: HTTP {r.status_code} {r.text[:400]}"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    print("[10] bundle entries:", len(names))
    assert "safe_to_share/attestation.json" in names
    att = json.loads(z.read("safe_to_share/attestation.json").decode("utf-8"))
    print("[10] attestation.actual_knowledge_ack =", att.get("actual_knowledge_ack"))
    print("[10] attestation.actual_knowledge_cite =", att.get("actual_knowledge_cite"))
    print("[10] attestation.reviewer =", att.get("reviewer"))
    print("[10] attestation.publish_guard =", att.get("publish_guard"))
    print("[10] attestation.is_partial =", att.get("is_partial"))
    assert att["actual_knowledge_ack"] is True, att
    assert att["actual_knowledge_cite"] == "45 CFR 164.514(b)(2)(ii)"
    assert att["reviewer"] == "sir@lab.edu"
    assert att["is_partial"] is False, "nothing was deferred; bundle must not read as partial"
    assert att["withheld_columns"] == []

    txt = z.read("safe_to_share/attestation.txt").decode("utf-8")
    assert "Actual-knowledge attestation" in txt and "YES" in txt
    print("[10] attestation.txt contains YES + citation -> OK")

    print("\n[OK] Phase F end-to-end passed. sid =", sid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
