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


@pytest.mark.parametrize("evil", [
    "../../../.env",
    "/etc/passwd",
    "..\\..\\..\\backend\\.env",
    "..",
    "sub/dir.txt",
])
def test_upload_rejects_evil_filename(session_id, evil):
    files = {"file": (evil, io.BytesIO(b"hostile bytes"), "application/octet-stream")}
    r = requests.post(f"{BASE_URL}/api/sessions/{session_id}/upload", files=files, timeout=10)
    assert r.status_code == 400, r.text
