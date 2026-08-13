"""Attestation bundle + coverage matrix tests."""
from __future__ import annotations

import io
import json
import zipfile

import pytest

from phi_core.bundle import BundleOptions, build_bundle
from phi_core.coverage_matrix import COVERAGE, TOOLS, coverage_counts


# ---------- coverage matrix ------------------------------------------------

def test_coverage_matrix_has_23_rows_and_7_tools():
    assert len(COVERAGE) == 23
    assert len(TOOLS) == 7
    ids = {t["id"] for t in TOOLS}
    assert {"amazon_comprehend", "clinideid", "nlm_scrubber", "presidio",
            "mist", "gpt4_icl", "phi_console"} <= ids


def test_phi_console_covers_every_row():
    for row in COVERAGE:
        assert row["phi_console"], f"phi_console missing coverage for {row['category']!r}"


def test_phi_console_beats_every_competitor():
    counts = coverage_counts()
    for t in TOOLS:
        if t["id"] == "phi_console":
            continue
        assert counts["phi_console"] > counts[t["id"]], f"phi_console must beat {t['id']}"


def test_hipaa_letters_a_to_r_present():
    letters = {row["hipaa_letter"] for row in COVERAGE if row["hipaa_letter"]}
    assert letters == set("ABCDEFGHIJKLMNOPQR"), f"missing HIPAA letters: {set('ABCDEFGHIJKLMNOPQR')-letters}"


# ---------- bundle builder ------------------------------------------------

def _fake_session(tmp_path, with_exports=True):
    if with_exports:
        d = tmp_path / "exports"
        d.mkdir()
        (d / "enroll.csv").write_text("patient_id,dob\nPabc,1975\n", encoding="utf-8")
        (d / "dict.csv").write_text("column_name,description\npatient_id,study id\n", encoding="utf-8")
        export_paths = {"eid": str(d / "enroll.csv"), "did": str(d / "dict.csv")}
    else:
        export_paths = {}
    return {
        "id": "sess_bundle_test",
        "jurisdiction": "us",
        "files": [
            {"file_id": "eid", "kind": "dataset", "original_name": "enroll.csv"},
            {"file_id": "did", "kind": "metadata", "original_name": "dict.csv"},
        ],
        "export_paths": export_paths,
        "guard_report": {
            "status": "clean",
            "scanned": 2,
            "blocked": 0,
            "results": [
                {"file_id": "eid", "status": "clean"},
                {"file_id": "did", "status": "clean"},
            ],
        },
        "session_review": {"reviewer": "jane@lab.edu", "comment": "per QA v1",
                           "reviewed_at": "2026-07-28T00:00:00+00:00", "changed_decisions": False},
    }


def test_default_bundle_contains_safe_to_share_only(tmp_path):
    sess = _fake_session(tmp_path)
    data, name = build_bundle(sess, BundleOptions())
    assert name.endswith(".zip")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
    assert "safe_to_share/README.md" in names
    assert "safe_to_share/attestation.json" in names
    assert "safe_to_share/attestation.txt" in names
    assert "safe_to_share/datasets/enroll.csv" in names
    assert "safe_to_share/dictionary/dict.csv" in names
    assert not any(n.startswith("publication/") for n in names)


def test_publication_bundle_adds_paper_folder(tmp_path):
    sess = _fake_session(tmp_path)
    data, _ = build_bundle(sess, BundleOptions(include_publication=True))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
    assert "publication/README.md" in names
    assert "publication/paper/tables/table_1_category_coverage.csv" in names
    assert "publication/paper/figures/fig1_category_coverage.png" in names
    assert "publication/paper/figures/fig2_category_totals.png" in names
    assert "publication/paper/methods.md" in names
    assert "publication/paper/results.md" in names
    assert "publication/paper/discussion.md" in names
    assert "publication/paper/references.bib" in names
    assert "publication/benchmark/README.md" in names


def test_attestation_json_has_sha256_and_reviewer(tmp_path):
    sess = _fake_session(tmp_path)
    data, _ = build_bundle(sess, BundleOptions())
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        att = json.loads(zf.read("safe_to_share/attestation.json"))
    assert att["session_id"] == "sess_bundle_test"
    assert att["jurisdiction"] == "us"
    assert att["reviewer"] == "jane@lab.edu"
    assert att["publish_guard"]["status"] == "clean"
    # every packaged file listed with a sha256:… prefix
    assert att["files"], "at least one file must be in attestation.files"
    for path, digest in att["files"].items():
        assert digest.startswith("sha256:"), path
        assert len(digest) == 7 + 64, f"unexpected digest length for {path}"


def test_publication_coverage_csv_has_our_column_and_all_23_rows(tmp_path):
    import csv as _csv
    sess = _fake_session(tmp_path)
    data, _ = build_bundle(sess, BundleOptions(include_publication=True))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        csv_body = zf.read("publication/paper/tables/table_1_category_coverage.csv").decode()
    rows = list(_csv.reader(io.StringIO(csv_body)))
    assert len(rows) == 24  # header + 23 rows
    header = rows[0]
    assert "phi_console" in header
    idx = header.index("phi_console")
    for row in rows[1:]:
        assert row[idx] == "1", f"phi_console 0 for row: {row[1]}"


def test_bundle_empty_exports_still_produces_attestation(tmp_path):
    sess = _fake_session(tmp_path, with_exports=False)
    data, _ = build_bundle(sess, BundleOptions())
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
    assert "safe_to_share/attestation.json" in names
    assert "safe_to_share/README.md" in names


def _generate_signing_key_b64() -> str:
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption
    k = Ed25519PrivateKey.generate()
    der = k.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption())
    return base64.b64encode(der).decode()


def test_bundle_attestation_signature_verifies_against_shipped_pubkey(tmp_path, monkeypatch):
    monkeypatch.setenv("ATTESTATION_SIGNING_KEY", _generate_signing_key_b64())
    sess = _fake_session(tmp_path)
    data, _ = build_bundle(sess, BundleOptions())
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        att_json = zf.read("safe_to_share/attestation.json")
        sig_b64 = zf.read("safe_to_share/attestation.sig")
        pubkey_pem = zf.read("safe_to_share/attestation_pubkey.pem")
        att = json.loads(att_json)
    assert att["signed"] is True

    import base64
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    pub = load_pem_public_key(pubkey_pem)
    pub.verify(base64.b64decode(sig_b64), att_json)  # raises on failure


def test_bundle_attestation_signature_fails_after_tamper(tmp_path, monkeypatch):
    monkeypatch.setenv("ATTESTATION_SIGNING_KEY", _generate_signing_key_b64())
    sess = _fake_session(tmp_path)
    data, _ = build_bundle(sess, BundleOptions())
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        att_json = zf.read("safe_to_share/attestation.json")
        sig_b64 = zf.read("safe_to_share/attestation.sig")
        pubkey_pem = zf.read("safe_to_share/attestation_pubkey.pem")

    tampered = att_json[:-1] + (b"0" if att_json[-1:] != b"0" else b"1")

    import base64
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    pub = load_pem_public_key(pubkey_pem)
    with pytest.raises(InvalidSignature):
        pub.verify(base64.b64decode(sig_b64), tampered)


def test_bundle_attestation_unsigned_when_no_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ATTESTATION_SIGNING_KEY", raising=False)
    sess = _fake_session(tmp_path)
    data, _ = build_bundle(sess, BundleOptions())
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        att = json.loads(zf.read("safe_to_share/attestation.json"))
    assert att["signed"] is False
    assert "safe_to_share/attestation.sig" not in names
    assert "safe_to_share/attestation_pubkey.pem" not in names
