"""SEC-001: verify the live server rejects path-traversal filenames.

Runs against the local uvicorn instance at http://localhost:8001 (managed by
supervisor). Skipped automatically if the backend is not reachable so the
suite still works in isolation.
"""
import io
import os
from pathlib import Path

import pytest
import requests
from phi_core.paths import UnsafePath, safe_join, sanitise_filename

BASE_URL = os.environ.get("PHI_TEST_BASE_URL", "http://localhost:8001")


def _backend_up() -> bool:
    try:
        return requests.get(f"{BASE_URL}/api/health", timeout=2).ok
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _backend_up(), reason="backend not reachable")


# ---------- unit ----------------------------------------------------------

def test_sanitise_rejects_traversal():
    with pytest.raises(UnsafePath):
        sanitise_filename("../etc/passwd")


def test_sanitise_rejects_absolute():
    with pytest.raises(UnsafePath):
        sanitise_filename("/etc/passwd")


def test_sanitise_rejects_windows_absolute():
    with pytest.raises(UnsafePath):
        sanitise_filename("C:\\Windows\\System32\\config\\SAM")


def test_sanitise_rejects_separator():
    with pytest.raises(UnsafePath):
        sanitise_filename("sub/dir/file.txt")


def test_sanitise_rejects_nul():
    with pytest.raises(UnsafePath):
        sanitise_filename("bad\x00name.txt")


def test_sanitise_rejects_dotdot_only():
    with pytest.raises(UnsafePath):
        sanitise_filename("..")


def test_sanitise_falls_back_when_empty():
    assert sanitise_filename("") == "upload.bin"
    assert sanitise_filename(None) == "upload.bin"


def test_safe_join_happy_path(tmp_path: Path):
    p = safe_join(tmp_path, "good_name.txt")
    assert p.parent == tmp_path.resolve()
    assert p.name == "good_name.txt"


# ---------- live endpoint --------------------------------------------------

@pytest.fixture
def session_id() -> str:
    r = requests.post(f"{BASE_URL}/api/sessions", json={"jurisdiction": "us"}, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _zip_with_evil_entry(evil_name: str) -> bytes:
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(evil_name, "hostile bytes")
    return buf.getvalue()


@pytest.mark.parametrize("evil", [
    "../../../.env",
    "/etc/passwd",
    "..\\..\\..\\backend\\.env",
    "../datasets/escaped.csv",
])
def test_intake_rejects_zip_with_evil_entry_path(session_id, evil):
    """SEC-001, current endpoint: intake requires a .zip, so the attack
    surface a stale filename check on a nonexistent /upload endpoint used
    to cover has moved to the ZIP's *internal* entry paths (zip-slip).
    `phi_core.intake.unpack_zip` rejects absolute paths and any ".."
    component before a single byte is written to disk (see
    `intake.py`, "unsafe path in zip"); this exercises that live, through
    the real endpoint, rather than only the unit-level `sanitise_filename`
    coverage above.

    Replaces the old `test_upload_rejects_evil_filename`, which posted to
    `/api/sessions/{sid}/upload` -- a route that no longer exists (intake
    is `/api/sessions/{sid}/intake`, .zip-only, and never uses the
    uploaded file's own name to build a path at all: the ZIP is always
    stored server-side as a fixed "intake.zip"). That test could only ever
    404, never actually exercising anything; the previously-\"passing\"
    parametrized cases were a false sense of security about live coverage
    of this attack class.
    """
    files = {"file": ("intake.zip", io.BytesIO(_zip_with_evil_entry(evil)), "application/zip")}
    r = requests.post(f"{BASE_URL}/api/sessions/{session_id}/intake", files=files, timeout=10)
    assert r.status_code == 200, r.text  # intake always 200s; failure is in the manifest body
    body = r.json()
    assert body["status"] == "failed", body
    assert body["exit_code"] == 2, body
