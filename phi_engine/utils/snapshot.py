"""Immutable study-snapshot subsystem (W1).

A *study snapshot* is a named, versioned record of a fully-clean publish pass.
When a maintainer re-runs the pipeline after resolving every held set, the
resolved state is committed into ``output/{STUDY}/snapshots/{snapshot_id}/``:

* ``llm_source/``            — copy of the PHI-scrubbed tree the LLM reads
* ``phi_handling_approval.json`` — the run's approval report (approved/held)
* ``verifier_report.json``   — the run's verifier assertion report
* ``snapshot_manifest.json`` — content hashes + provenance + form lists
* ``.NO_LLM_ZONE``           — defence-in-depth sentinel at the snapshot root

Snapshots are **immutable**: writing a snapshot whose directory already exists
raises :class:`SnapshotExistsError`. A new clean pass mints a new snapshot id;
it never overwrites a prior one.

Snapshot ids are **timestamp-based** (Note 14) — ``snap_<YYYYMMDDTHHMMSSZ>`` of
the UTC creation instant, with a ``-N`` disambiguator on the rare same-second
collision. The id is the human-readable label the UI shows ("2026-06-15 14:32 —
28 forms"); no hash id is surfaced to users. Redundant-run prevention no longer
relies on id collision — it is the **preflight input-fingerprint check** (see
:mod:`scripts.utils.input_fingerprint`): if a clean snapshot already exists for
the current input fingerprint the pipeline activates it instead of re-running,
and ``--force`` explicitly mints a fresh time-stamped snapshot on identical
inputs. The deterministic content hash of the ``llm_source/`` tree is retained
as the manifest ``content_hash`` field — it powers the snapshot diff and the
fingerprint match — but it is no longer the directory name.

SECURITY
--------
The snapshot ROOT is OUTSIDE the agent read zone (``llm_source/`` + ``agent/``).
``scripts.ai_assistant.file_access.validate_agent_read`` hard-rejects any path
under ``snapshots/`` except a ``snapshots/{id}/llm_source/`` subtree that has
been explicitly selected (``config.STUDY_LLM_SOURCE_DIR`` repointed at it).
The ``phi_handling_approval.json`` / ``verifier_report.json`` /
``snapshot_manifest.json`` files live at the snapshot root, a *sibling/parent*
of the selected ``llm_source/`` — never inside it — so a snapshot selection
never widens the read zone to include approval/manifest bytes.

Fail-closed: every error condition raises; nothing is silently skipped.
"""

from __future__ import annotations

import enum
import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
UTC = timezone.utc  # py3.9 compat
from pathlib import Path

import phi_engine.config.config as config
from phi_engine.audit.ledger import ensure_no_llm_sentinel
from phi_engine.utils._extraction_io import atomic_write_json

__all__ = [
    "CURRENT_POINTER_FILENAME",
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA",
    "SnapshotError",
    "SnapshotExistsError",
    "SnapshotNotFoundError",
    "SnapshotTamperedError",
    "StalenessFinding",
    "StalenessSeverity",
    "check_snapshot_staleness",
    "commit_run_snapshot",
    "current_pointer_path",
    "diff_snapshots",
    "evaluate_snapshot_staleness",
    "find_snapshot_by_fingerprint",
    "get_current_snapshot",
    "latest_snapshot",
    "list_snapshots",
    "load_snapshot",
    "select_snapshot_llm_source",
    "set_current_snapshot",
    "snapshot_diff_path",
    "snapshot_llm_source_path",
    "snapshot_path",
    "snapshots_root",
    "verify_snapshot_integrity",
    "write_snapshot",
]

MANIFEST_FILENAME = "snapshot_manifest.json"
APPROVAL_FILENAME = "phi_handling_approval.json"
VERIFIER_REPORT_FILENAME = "verifier_report.json"
CURRENT_POINTER_FILENAME = "current.json"
LLM_SOURCE_DIRNAME = "llm_source"
_SNAPSHOT_ID_PREFIX = "snap_"
#: Manifest schema version. Bumped from the implicit v1 (9 fields, content-hash
#: id) to v2 (timestamp id + config capture + staleness/provenance fields).
MANIFEST_SCHEMA = 2
#: Config files copied verbatim into every snapshot (Note 14 C5.2). Non-PHI
#: study metadata; captured so a snapshot is independently auditable/reproducible.
# Note 11: phi_scrub.yaml carries the per-study compliance_posture override the
# wizard writes (and any rule overrides), so it must be captured for snapshot
# reproducibility. Optional — _copy_config_files records None when absent.
_CONFIG_FILES = ("_study_privacy.yaml", "_forms_manifest.yaml", "phi_scrub.yaml")


class SnapshotError(Exception):
    """Base class for snapshot-subsystem failures."""


class SnapshotExistsError(SnapshotError):
    """Raised when writing a snapshot whose directory already exists.

    Snapshots are immutable; an existing id is never overwritten.
    """


class SnapshotNotFoundError(SnapshotError):
    """Raised when a requested snapshot id does not exist on disk."""


class SnapshotTamperedError(SnapshotError):
    """Raised when a snapshot's on-disk ``llm_source/`` no longer matches the
    content hashes recorded in its manifest (filesystem tampering, C5.7).

    Fail-closed: a tampered snapshot is NEVER activated under any circumstance.
    """


class StalenessSeverity(enum.Enum):
    """Severity of a snapshot-staleness finding (C5.4)."""

    WARN = "warn"  # surface a warning; activation may proceed (human decides)
    BLOCK = "block"  # hard-block activation (pseudonyms irrecoverable)


@dataclass(frozen=True)
class StalenessFinding:
    """One reason a committed snapshot may no longer be authoritative (C5.4)."""

    trigger: str  # rulebook_update | key_rotation | source_data_correction | config_change
    severity: StalenessSeverity
    detail: str  # human-readable, value-free explanation


# ---------------------------------------------------------------------------
# Path helpers — all routed through config.* constants.
# ---------------------------------------------------------------------------


def snapshots_root(study: str) -> Path:
    """Return ``output/{study}/snapshots/`` for *study*.

    Derived from ``config.OUTPUT_DIR`` so an explicit *study* argument resolves
    correctly even when ``config.STUDY_NAME`` (set at import time) differs.
    """
    if not study:
        raise SnapshotError("study must not be empty")
    return Path(config.OUTPUT_DIR) / study / "snapshots"


def snapshot_path(study: str, snapshot_id: str | None = None) -> Path:
    """Return the snapshot directory for *study*.

    With *snapshot_id* ``None``, returns the per-study ``snapshots/`` root.
    Otherwise returns ``snapshots/{snapshot_id}/``.
    """
    root = snapshots_root(study)
    if snapshot_id is None:
        return root
    _validate_snapshot_id(snapshot_id)
    return root / snapshot_id


def snapshot_llm_source_path(study: str, snapshot_id: str) -> Path:
    """Return ``snapshots/{snapshot_id}/llm_source/`` — the only LLM-readable
    subtree of a snapshot, and only once explicitly selected."""
    return snapshot_path(study, snapshot_id) / LLM_SOURCE_DIRNAME


def _snapshot_manifest_path(study: str, snapshot_id: str) -> Path:
    return snapshot_path(study, snapshot_id) / MANIFEST_FILENAME


def _validate_snapshot_id(snapshot_id: str) -> None:
    """Reject empty or path-bearing snapshot ids (no traversal, no separators)."""
    if not snapshot_id:
        raise SnapshotError("snapshot_id must not be empty")
    if snapshot_id in (".", ".."):
        raise SnapshotError(f"invalid snapshot_id: {snapshot_id!r}")
    if "/" in snapshot_id or "\\" in snapshot_id or "\x00" in snapshot_id:
        raise SnapshotError(f"snapshot_id must not contain path separators: {snapshot_id!r}")
    if Path(snapshot_id).name != snapshot_id:
        raise SnapshotError(f"snapshot_id must be a bare directory name: {snapshot_id!r}")


# ---------------------------------------------------------------------------
# Content hashing — deterministic, content-only (no mtimes, no randomness).
# ---------------------------------------------------------------------------


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_manifest(root: Path) -> dict[str, str]:
    """Return ``{relative_posix_path: sha256_hex}`` for every file under *root*.

    Sorted, content-only. Directories are walked; files (including symlinks to
    files) are hashed by their target content. The mapping is deterministic for
    identical content regardless of filesystem mtimes.
    """
    if not root.is_dir():
        raise SnapshotError(f"expected a directory to hash, got: {root}")
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(root).as_posix()
        manifest[rel] = _file_sha256(path)
    return manifest


def _assert_no_escaping_symlinks(root: Path) -> None:
    """Fail-closed if any symlink under *root* resolves OUTSIDE *root*.

    ``write_snapshot`` copies ``llm_source/`` with ``shutil.copytree`` (default
    ``symlinks=False``), which DEREFERENCES symlinks and copies their *target*
    content. A symlink inside a malformed/compromised ``llm_source/`` that
    points at a raw ``.xlsx``/``.jsonl`` outside the tree would otherwise bake
    that out-of-tree (possibly PHI) content into the immutable, re-exposable
    snapshot. Reject any such escape before a single byte is copied.
    """
    root_real = root.resolve()
    for path in sorted(root.rglob("*")):
        if not path.is_symlink():
            continue
        target_real = path.resolve()
        if target_real != root_real and root_real not in target_real.parents:
            raise SnapshotError(
                "llm_source contains a symlink that escapes the tree; refusing to "
                f"snapshot (fail-closed): {path.relative_to(root).as_posix()}"
            )


def _content_hash(llm_source_manifest: dict[str, str], run_id: str) -> str:
    """Return the deterministic SHA-256 content hash of the llm_source manifest
    + ``run_id`` (the old snapshot-id seed, retained as a manifest field).

    Identical ``llm_source/`` content + run_id always yields the same hash, so it
    powers the snapshot diff and the input-fingerprint match — but it is no
    longer the directory name (see module docstring).
    """
    canonical = json.dumps(
        {"run_id": run_id, "llm_source_manifest": llm_source_manifest},
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _now_utc_iso() -> str:
    """Return the current UTC instant as ``YYYY-MM-DDTHH:MM:SSZ`` (second precision)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _id_from_timestamp(created_utc: str) -> str:
    """Map an ISO ``YYYY-MM-DDTHH:MM:SSZ`` instant to a ``snap_<compact>`` id stem.

    The id is the human-readable label (``snap_20260615T143200Z``); a same-second
    collision disambiguator (``-N``) is appended by the caller.
    """
    try:
        parsed = datetime.strptime(created_utc, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise SnapshotError(
            f"created_utc must be ISO8601 'YYYY-MM-DDTHH:MM:SSZ', got: {created_utc!r}"
        ) from exc
    return f"{_SNAPSHOT_ID_PREFIX}{parsed.strftime('%Y%m%dT%H%M%SZ')}"


def _mint_unique_snapshot_id(study: str, created_utc: str) -> str:
    """Mint a collision-free timestamp snapshot id for *study*.

    The base id is ``snap_<compact-timestamp>``; if that directory already exists
    (a forced re-run within the same UTC second), append ``-2``, ``-3``, … until
    a free name is found. The human label is still the timestamp.
    """
    base = _id_from_timestamp(created_utc)
    root = snapshots_root(study)
    candidate = base
    suffix = 1
    while (root / candidate).exists():
        suffix += 1
        candidate = f"{base}-{suffix}"
    return candidate


# ---------------------------------------------------------------------------
# Source-artifact resolution for the active run.
# ---------------------------------------------------------------------------


def _run_dir(study: str, run_id: str) -> Path:
    return Path(config.OUTPUT_DIR) / study / "runs" / run_id


def _read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise SnapshotError(f"expected a JSON object at {path}")
    return data


# ---------------------------------------------------------------------------
# Provenance gathering (C5.2) — every gatherer is FAIL-SOFT (returns None / {}).
# A missing rulebook / unloaded key / absent config must never block a snapshot
# commit, which happens AFTER a fully-clean publish. The captured provenance is
# for staleness detection + audit; its absence degrades those features, it does
# not corrupt the snapshot.
# ---------------------------------------------------------------------------


def _gather_rulebook_version() -> int | None:
    try:
        from phi_engine.security.phi_rulebook import RULEBOOK_CACHE_VERSION

        return int(RULEBOOK_CACHE_VERSION)
    except Exception:
        return None


def _gather_rulebook_rules_sha256(study: str) -> str | None:
    """Effective rule-set content hash from :func:`resolve_rulebook` (N7 staleness).

    Matches the classification gate's ``allow_network`` posture from
    ``_study_privacy.yaml``. Fail-soft → ``None`` when resolution fails.
    """
    try:
        from phi_engine.security.phi_review import load_study_privacy_config
        from phi_engine.security.phi_rulebook import resolve_rulebook

        privacy = load_study_privacy_config(study)
        resolution = resolve_rulebook(
            privacy,
            allow_network=privacy.rule_refresh == "online_preferred",
        )
        return resolution.bundle.rules_sha256
    except Exception:
        return None


def _gather_key_fingerprint() -> str | None:
    try:
        from phi_engine.security.phi_keystore import phi_key_fingerprint

        return phi_key_fingerprint()
    except Exception:
        return None


def _gather_input_fingerprint(study: str) -> tuple[str | None, dict | None]:
    """Return (fingerprint, components) from the run's recorded fingerprint file.

    Reads the JSON record directly (it carries both ``fingerprint`` and
    ``components``; the ``read_recorded_fingerprint`` helper returns only the
    combined hash). Fail-soft to ``(None, None)``.
    """
    try:
        from phi_engine.utils.input_fingerprint import fingerprint_record_path

        audit_dir = Path(config.OUTPUT_DIR) / study / "audit"
        path = fingerprint_record_path(audit_dir)
        if not path.is_file():
            return None, None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, None
        fp = data.get("fingerprint")
        components = data.get("components")
        return (
            fp if isinstance(fp, str) else None,
            dict(components) if isinstance(components, dict) else None,
        )
    except Exception:
        return None, None


def _gather_compliance_posture() -> str | None:
    try:
        from phi_engine.security.phi_scrub import load_scrub_config

        cfg = load_scrub_config()
        return getattr(cfg, "compliance_posture", None)
    except Exception:
        return None


def _gather_data_as_of(study: str) -> str | None:
    """Maintainer-declared data-recency date from _study_privacy.yaml (Note 14).

    The LLM cannot infer "data current through X" from row values (GR-1), so it is
    declared in config and copied into the manifest. Fail-soft → None when absent.
    """
    try:
        from phi_engine.security.phi_review import load_study_privacy_config

        return load_study_privacy_config(study).data_as_of
    except Exception:
        return None


def _gather_rejected_forms(study: str) -> list[str]:
    """Manifest ``reject:`` list — forms intentionally excluded (Note 14). Fail-soft []."""
    try:
        import yaml

        manifest_path = Path(config.study_config_path("_forms_manifest.yaml", study=study))
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        reject = data.get("reject", [])
        return [str(f) for f in reject] if isinstance(reject, list) else []
    except Exception:
        return []


def _copy_config_files(study: str, dest_dir: Path) -> dict[str, str | None]:
    """Copy the study's config files into *dest_dir*, returning ``{name: sha256|None}``.

    A config file that does not resolve / is absent records ``None`` (fail-soft):
    a partial study may legitimately lack one, and the snapshot must still commit.
    """
    captured: dict[str, str | None] = {}
    for name in _CONFIG_FILES:
        captured[name] = None
        try:
            src = config.study_config_path(name, study=study)
        except Exception:  # noqa: S112 — fail-soft: a config file is optional metadata
            continue
        if src and Path(src).is_file():
            shutil.copy2(src, dest_dir / name)
            captured[name] = _file_sha256(dest_dir / name)
    return captured


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def write_snapshot(
    study: str,
    run_id: str,
    *,
    snapshot_id: str | None = None,
    created_utc: str | None = None,
    snapshot_type: int = 1,
    human_review_records: list | None = None,
    partial: bool = False,
    absent_forms: list[str] | None = None,
    cleanup_verifier_passed: bool | None = None,
    write_diff: bool = True,
) -> Path:
    """Write an immutable snapshot of the run's clean publish pass.

    Copies the study's ``llm_source/`` tree, the run's
    ``phi_handling_approval.json`` and ``verifier_report.json``, and the study's
    config files (``_study_privacy.yaml`` / ``_forms_manifest.yaml``) into
    ``snapshots/{snapshot_id}/`` and writes ``snapshot_manifest.json`` (schema v2)
    plus a ``.NO_LLM_ZONE`` sentinel at the snapshot root.

    The *snapshot_id* is a timestamp label (``snap_<compact-utc>``) minted from
    *created_utc* (defaults to now); a same-second collision appends ``-N``. Pass
    *snapshot_id* explicitly to override (used by tests / fixed-id callers).

    Args:
        snapshot_type: ``1`` (clean first run, no human review) or ``2``
            (human-verified run — something was held, a human resolved it, the
            re-run is now clean). An IRB auditor distinguishes the two from the
            manifest alone (Note 14).
        human_review_records: Type-2 evidence (what was reviewed / decided /
            when). Stored verbatim in the manifest — must be value-free.
        partial: this snapshot covers only the explicitly approved forms; some
            forms are permanently absent (lost PDF, unresolvable dedup, …).
        absent_forms: the form NAMES omitted from a *partial* snapshot.
        cleanup_verifier_passed: proof the workspace was clean at commit.
        write_diff: emit a diff against the prior snapshot into the audit folder
            (C5.6). Fail-soft — a diff error never fails the commit.

    Raises:
        SnapshotError: a required source artifact is missing/unreadable, or an
            invalid *snapshot_type*.
        SnapshotExistsError: the target snapshot directory already exists
            (immutability — a clean pass never overwrites a prior snapshot).
    """
    if not study:
        raise SnapshotError("study must not be empty")
    if not run_id:
        raise SnapshotError("run_id must not be empty")
    if snapshot_type not in (1, 2):
        raise SnapshotError(f"snapshot_type must be 1 or 2, got {snapshot_type!r}")

    # Derive the source tree from the explicit *study* arg via config.OUTPUT_DIR
    # — NOT from the module-global config.STUDY_LLM_SOURCE_DIR. That global is
    # repointed when a snapshot is activated in the UI (see snapshot_select), so
    # reading it here would capture the *previously-activated* snapshot's tree
    # instead of the live publish; it would also read the wrong study when the
    # *study* arg differs from config.STUDY_NAME. Every other path in this module
    # already derives from the study arg via config.OUTPUT_DIR.
    llm_source_src = Path(config.OUTPUT_DIR) / study / LLM_SOURCE_DIRNAME
    if not llm_source_src.is_dir():
        raise SnapshotError(f"llm_source tree not found at {llm_source_src}; cannot snapshot")

    # Fail-closed: a symlink under llm_source/ that escapes the tree would be
    # dereferenced by copytree and bake out-of-tree (possibly PHI) content into
    # the immutable snapshot. Reject before hashing or copying anything.
    _assert_no_escaping_symlinks(llm_source_src)

    run_dir = _run_dir(study, run_id)
    approval_src = run_dir / APPROVAL_FILENAME
    verifier_src = run_dir / VERIFIER_REPORT_FILENAME
    if not approval_src.is_file():
        raise SnapshotError(f"approval report not found at {approval_src}; cannot snapshot")
    if not verifier_src.is_file():
        raise SnapshotError(f"verifier report not found at {verifier_src}; cannot snapshot")

    # Content manifest of the source llm_source tree — the diff/fingerprint seed.
    llm_source_manifest = _tree_manifest(llm_source_src)

    if created_utc is None:
        created_utc = _now_utc_iso()
    if snapshot_id is None:
        snapshot_id = _mint_unique_snapshot_id(study, created_utc)
    else:
        _validate_snapshot_id(snapshot_id)

    dest = snapshot_path(study, snapshot_id)
    # Immutability guard — fail-closed before touching disk.
    if dest.exists():
        raise SnapshotExistsError(
            f"snapshot {snapshot_id!r} already exists at {dest}; snapshots are immutable"
        )

    # Read provenance from the approval report (approved/held form lists) and
    # verifier_passed from the verifier report.
    approval_payload = _read_json(approval_src)
    verifier_payload = _read_json(verifier_src)
    approved_forms = [str(f) for f in approval_payload.get("approved_forms", [])]
    held_forms = [str(f) for f in approval_payload.get("held_forms", [])]
    # verifier_report.json has no "verifier_passed" key — its canonical pass
    # signal is "overall" == "pass" (with "exit_code" == 0 on pass). Accept
    # either positive signal; default to False (fail-closed) when neither says
    # pass.
    verifier_passed = (
        verifier_payload.get("overall") == "pass" or verifier_payload.get("exit_code") == 0
    )

    # ---- Build under a temp dir, then atomically rename into place. ---------
    # A partial copy must never become a visible snapshot. Build beside the
    # final dir and replace() the directory once complete.
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = dest.parent / f".{snapshot_id}.partial"
    if staging.exists():
        shutil.rmtree(staging)
    try:
        staging.mkdir(parents=True)
        # Copy the llm_source tree (already PHI-scrubbed).
        shutil.copytree(llm_source_src, staging / LLM_SOURCE_DIRNAME)
        # Copy approval + verifier report.
        shutil.copy2(approval_src, staging / APPROVAL_FILENAME)
        shutil.copy2(verifier_src, staging / VERIFIER_REPORT_FILENAME)

        # N9: capture the run-scoped AI-aligned scrub overlay when present, so the
        # exact aligned rules a published study used are reproducible from the
        # snapshot alone (no re-call to the LLM).
        overlay_src = run_dir / config.PHI_SCRUB_GENERATED_FILENAME
        overlay_captured = overlay_src.is_file()
        if overlay_captured:
            shutil.copy2(overlay_src, staging / config.PHI_SCRUB_GENERATED_FILENAME)

        # N14: capture the cleanup-verification REPORT itself (not just the
        # cleanup_verifier_passed bool), so the workspace-clean proof is
        # reproducible from the snapshot. Fail-soft (absent on a partial run).
        cleanup_report_src = (
            Path(config.OUTPUT_DIR) / study / "audit" / "cleanup_verification_report.json"
        )
        cleanup_report_captured = cleanup_report_src.is_file()
        if cleanup_report_captured:
            shutil.copy2(cleanup_report_src, staging / "cleanup_verification_report.json")

        # Re-hash the COPIED llm_source so the manifest reflects exactly what
        # landed in the snapshot (defence against a mid-copy mutation).
        copied_manifest = _tree_manifest(staging / LLM_SOURCE_DIRNAME)
        if copied_manifest != llm_source_manifest:
            raise SnapshotError(
                "llm_source tree changed during snapshot copy; aborting (fail-closed)"
            )

        # Capture config files (C5.2) into the snapshot root (no-LLM zone).
        config_files = _copy_config_files(study, staging)

        # Provenance for staleness detection + audit (all fail-soft).
        input_fingerprint, input_fingerprint_components = _gather_input_fingerprint(study)

        manifest = {
            "manifest_schema": MANIFEST_SCHEMA,
            "snapshot_id": snapshot_id,
            "study": study,
            "source_run_id": run_id,
            "created_utc": created_utc,
            "snapshot_type": snapshot_type,
            "verifier_passed": verifier_passed,
            "cleanup_verifier_passed": cleanup_verifier_passed,
            "partial": bool(partial),
            "absent_forms": [str(f) for f in (absent_forms or [])],
            # N14: the manifest reject: list (forms intentionally excluded).
            "rejected_forms": _gather_rejected_forms(study),
            "approved_forms": approved_forms,
            "held_forms": held_forms,
            "human_review_records": list(human_review_records or []),
            "phi_rulebook_version": _gather_rulebook_version(),
            # N7: durable record of the EXACT rule-set CONTENT this run used (the
            # version int alone can't distinguish two live-extracted rule sets).
            # Sourced from the approval payload's rule_bundle (value-free sha).
            "phi_rulebook_rules_sha256": (
                approval_payload.get("rule_bundle", {}).get("rules_sha256")
                if isinstance(approval_payload.get("rule_bundle"), dict)
                else None
            ),
            "phi_key_fingerprint": _gather_key_fingerprint(),
            "compliance_posture": _gather_compliance_posture(),
            "data_as_of": _gather_data_as_of(study),
            "input_fingerprint": input_fingerprint,
            "input_fingerprint_components": input_fingerprint_components,
            "config_files": config_files,
            "content_hash": _content_hash(copied_manifest, run_id),
            "llm_source_manifest": copied_manifest,
            "approval_sha256": _file_sha256(staging / APPROVAL_FILENAME),
            "verifier_report_sha256": _file_sha256(staging / VERIFIER_REPORT_FILENAME),
            "phi_scrub_generated_sha256": (
                _file_sha256(staging / config.PHI_SCRUB_GENERATED_FILENAME)
                if overlay_captured
                else None
            ),
            "cleanup_verification_report_sha256": (
                _file_sha256(staging / "cleanup_verification_report.json")
                if cleanup_report_captured
                else None
            ),
        }
        atomic_write_json(staging / MANIFEST_FILENAME, manifest)

        # Defence-in-depth: .NO_LLM_ZONE sentinel at the snapshot root. The
        # read-zone containment check is the primary control; this is belt-and-
        # suspenders so any future widening still trips a no-LLM signal.
        ensure_no_llm_sentinel(staging)

        # Atomic publish: rename the completed staging dir into place. Re-check
        # existence to close the TOCTOU window against a concurrent writer.
        if dest.exists():
            raise SnapshotExistsError(
                f"snapshot {snapshot_id!r} already exists at {dest}; snapshots are immutable"
            )
        staging.replace(dest)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    # Diff against the prior snapshot (C5.6) — written to the AUDIT folder, not
    # the snapshot. Fail-soft: a diff error must never undo a committed snapshot.
    if write_diff:
        try:
            prior = _prior_snapshot_id(study, exclude=snapshot_id)
            if prior is not None:
                diff = diff_snapshots(study, prior, snapshot_id)
                write_snapshot_diff(study, prior, snapshot_id, diff)
        except Exception:  # noqa: S110 — diff is advisory; never undo a committed snapshot
            pass

    return dest


def _assess_run_cleanliness(run_dir: Path, *, allow_review_notes: bool) -> tuple[bool, list[str]]:
    """Decide whether a run is FULLY clean and thus snapshot-eligible.

    A snapshot is an immutable milestone of a clean publish pass. Per policy it is
    committed ONLY when the run has zero outstanding issues:
      * no form held for review (``held_forms``),
      * no quarantined / elevated rows (``publish_status`` != ``partial``;
        ``partial_forms`` carry no quarantine),
      * no actionable human-review note awaiting a maintainer.

    A ``--resume-held`` (Type-2) commit is the maintainer's deliberate, human-
    verified republish: the notes under ``human_review/`` are the maintainer's
    working set, so ``allow_review_notes=True`` skips the note check — but the
    republish itself must still be free of new holds / quarantine.

    Returns ``(is_clean, reasons)``. ``reasons`` are value-free (counts / dir
    names only — never a row value).
    """
    import json as _json

    reasons: list[str] = []
    status_path = Path(run_dir) / "status.json"
    if status_path.is_file():
        try:
            status = _json.loads(status_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            status = {}
        if isinstance(status, dict):
            held_n = len(status.get("held_forms") or []) or int(status.get("held_forms_count") or 0)
            if held_n:
                reasons.append(f"{held_n} form(s) held for review")
            partial_forms = status.get("partial_forms") or []
            quarantined = sum(
                int((f or {}).get("quarantined") or 0) for f in partial_forms if isinstance(f, dict)
            )
            elevated = sum(1 for f in partial_forms if isinstance(f, dict) and f.get("elevated"))
            if quarantined:
                reasons.append(
                    f"{quarantined} quarantined row(s) across {len(partial_forms)} form(s)"
                )
            if elevated:
                reasons.append(f"{elevated} form(s) flagged elevated")
            # publish_status=partial with no partial_forms detail still signals a
            # non-clean publish (e.g. an older sidecar shape) — block on it too.
            if status.get("publish_status") == "partial" and not (quarantined or held_n):
                reasons.append("publish_status=partial")
    if not allow_review_notes:
        try:
            from phi_engine.audit.review_paths import human_review_root

            # Study-scoped: run_dir is ``output/{study}/runs/{run_id}``, so the
            # study's audit dir is ``run_dir.parent.parent / "audit"``. Deriving it
            # here (rather than reading the global ``config.STUDY_AUDIT_DIR``) keeps
            # the check pinned to the run being committed — never another study's
            # or a stale global.
            audit_dir = Path(run_dir).parent.parent / "audit"
            hr = human_review_root(audit_dir)
            notes = sorted(hr.rglob("*.md")) if hr.is_dir() else []
            if notes:
                reasons.append(f"{len(notes)} unresolved human-review note(s) under {hr.name}/")
        except Exception:  # noqa: S110 — advisory; a scan hiccup must not crash the committer
            pass
    return (not reasons, reasons)


def commit_run_snapshot(
    *,
    study: str,
    run_id: str,
    run_dir: Path,
    resume_held: bool = False,
    human_review_records: list | None = None,
    cleanup_verifier_passed: bool | None = None,
) -> str | None:
    """Commit a run's clean-pass snapshot and record ``snapshot_id`` in status.json.

    Shared committer for both standalone runs and orchestrator P10. It records
    cleanup proof when the caller passes ``cleanup_verifier_passed=True``; the
    caller is responsible for running the cleanup verifier before invoking this
    function. A ``--resume-held`` run commits a Type-2 (human-verified) snapshot.

    Never raises: a snapshot failure is non-fatal (the publish already
    succeeded). Returns the snapshot_id, or None on the immutability guard /
    failure (the failure reason is recorded in status.json).
    """
    import json as _json
    import sys as _sys

    status_path = Path(run_dir) / "status.json"

    def _update_status(key: str, value: str) -> None:
        if not status_path.is_file():
            return
        try:
            status = _json.loads(status_path.read_text(encoding="utf-8"))
            status[key] = value
            atomic_write_json(status_path, status)
        except (ValueError, OSError) as exc:
            print(f"Warning: status.json {key} update failed: {exc}", file=_sys.stderr)

    # N14: assemble the Type-2 human-review trail. Prefer an explicit
    # human_review_records.json the maintainer/resume flow placed in the run dir;
    # otherwise synthesize a minimal record for a --resume-held (Type-2) commit so
    # the snapshot is never an empty-trail Type-2 (an IRB auditor must be able to
    # see that something WAS human-reviewed). A clean Type-1 run carries none.
    records = list(human_review_records or [])
    review_file = Path(run_dir) / "human_review_records.json"
    if review_file.is_file():
        try:
            loaded = _json.loads(review_file.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                records.extend(loaded)
        except (ValueError, OSError) as exc:
            print(f"Warning: human_review_records.json unreadable: {exc}", file=_sys.stderr)
    if resume_held and not records:
        import getpass
        from datetime import datetime, timezone
        UTC = timezone.utc  # py3.9 compat

        try:
            who = getpass.getuser()
        except Exception:
            who = "unknown"
        records = [
            {
                "type": "resume_held_republish",
                "note": (
                    "Type-2 human-verified resume: previously-held forms were "
                    "resolved out-of-band and re-published cleanly."
                ),
                "committed_by": who,
                "committed_utc": datetime.now(UTC).isoformat(),
            }
        ]

    # Clean-pass-only gate (defense-in-depth). A snapshot is an immutable
    # milestone of a FULLY clean publish; refuse to commit one for a run with any
    # outstanding issue (held form, quarantined/elevated rows, or — for a non-
    # resume run — an unresolved human-review note). The partial publish remains
    # in llm_source/ but is never enshrined. Skip reason recorded value-free.
    _clean, _reasons = _assess_run_cleanliness(run_dir, allow_review_notes=resume_held)
    if not _clean:
        _msg = "; ".join(_reasons)
        _update_status("snapshot_skipped_reason", _msg)
        print(
            f"Snapshot not committed for {study} (run {run_id}): run is not fully clean [{_msg}].",
            file=_sys.stderr,
        )
        return None

    try:
        snap_dest = write_snapshot(
            study,
            run_id,
            snapshot_type=2 if resume_held else 1,
            human_review_records=records,
            cleanup_verifier_passed=cleanup_verifier_passed,
        )
        snapshot_id = snap_dest.name
        _update_status("snapshot_id", snapshot_id)
        print(f"Snapshot committed: {snapshot_id} → {snap_dest}")
        return snapshot_id
    except SnapshotExistsError as exc:
        print(f"Snapshot already exists (immutability guard): {exc}", file=_sys.stderr)
        return None
    except Exception as exc:  # non-fatal: publish already succeeded
        reason = f"{type(exc).__name__}: {exc}"
        print(
            f"Warning: snapshot commit failed (publish still succeeded): {reason}", file=_sys.stderr
        )
        _update_status("snapshot_failed", reason)
        return None


def list_snapshots(study: str) -> list[str]:
    """Return the sorted list of snapshot ids present for *study*.

    A snapshot is counted only when it has a readable ``snapshot_manifest.json``
    (a bare/partial directory is ignored). Returns ``[]`` when the snapshots
    root does not exist.
    """
    root = snapshots_root(study)
    if not root.is_dir():
        return []
    ids: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith("."):
            continue  # partial / temp dir
        if (child / MANIFEST_FILENAME).is_file():
            ids.append(child.name)
    return ids


def load_snapshot(study: str, snapshot_id: str) -> dict:
    """Return the parsed ``snapshot_manifest.json`` for *snapshot_id*.

    Raises:
        SnapshotNotFoundError: the snapshot dir or its manifest is absent.
        SnapshotError: the manifest is unreadable / not a JSON object.
    """
    _validate_snapshot_id(snapshot_id)
    dest = snapshot_path(study, snapshot_id)
    if not dest.is_dir():
        raise SnapshotNotFoundError(f"snapshot {snapshot_id!r} not found at {dest}")
    manifest_path = _snapshot_manifest_path(study, snapshot_id)
    if not manifest_path.is_file():
        raise SnapshotNotFoundError(f"snapshot {snapshot_id!r} has no manifest at {manifest_path}")
    try:
        return _read_json(manifest_path)
    except (json.JSONDecodeError, OSError) as exc:
        # A corrupt/unreadable manifest is a SnapshotError (a subclass of which
        # callers like ui.snapshot_select.available_snapshots already catch) so
        # one bad snapshot is skipped rather than hiding every other snapshot.
        raise SnapshotError(
            f"snapshot {snapshot_id!r} manifest is unreadable at {manifest_path}"
        ) from exc


def select_snapshot_llm_source(study: str, snapshot_id: str) -> Path:
    """Resolve and validate the ``llm_source/`` subtree of a selected snapshot.

    Returns the absolute path the Load Study loader assigns to
    ``config.STUDY_LLM_SOURCE_DIR`` when a maintainer selects this snapshot.
    Exposing the subtree is the caller's responsibility (repoint the config
    constant); this helper only resolves + fail-closed validates that the
    snapshot and its ``llm_source/`` exist.

    Raises:
        SnapshotNotFoundError: snapshot or its ``llm_source/`` is absent.
        SnapshotTamperedError: the on-disk ``llm_source/`` no longer matches the
            manifest content hashes (C5.7) — fail-closed, never exposed.
    """
    _validate_snapshot_id(snapshot_id)
    dest = snapshot_path(study, snapshot_id)
    if not dest.is_dir():
        raise SnapshotNotFoundError(f"snapshot {snapshot_id!r} not found at {dest}")
    llm_source = snapshot_llm_source_path(study, snapshot_id)
    if not llm_source.is_dir():
        raise SnapshotNotFoundError(
            f"snapshot {snapshot_id!r} has no llm_source tree at {llm_source}"
        )
    # C5.7: re-hash on every selection — a tampered snapshot is never exposed.
    verify_snapshot_integrity(study, snapshot_id)
    return llm_source


# ---------------------------------------------------------------------------
# C5.7 — tampering detection (re-hash on activation)
# ---------------------------------------------------------------------------


def verify_snapshot_integrity(study: str, snapshot_id: str) -> bool:
    """Re-hash the snapshot's ``llm_source/`` tree and compare to its manifest.

    Returns ``True`` when the on-disk content matches the recorded hashes.

    Raises:
        SnapshotNotFoundError: the snapshot / manifest / llm_source is absent.
        SnapshotTamperedError: the tree was modified after it was written — the
            file set or any content hash differs from the manifest. Hard stop;
            the caller must NOT activate a tampered snapshot (C5.7).
    """
    manifest = load_snapshot(study, snapshot_id)
    recorded = manifest.get("llm_source_manifest")
    if not isinstance(recorded, dict):
        raise SnapshotTamperedError(
            f"snapshot {snapshot_id!r} manifest has no llm_source_manifest to verify against"
        )
    llm_source = snapshot_llm_source_path(study, snapshot_id)
    if not llm_source.is_dir():
        raise SnapshotNotFoundError(
            f"snapshot {snapshot_id!r} has no llm_source tree at {llm_source}"
        )
    current = _tree_manifest(llm_source)
    if current != recorded:
        added = sorted(set(current) - set(recorded))
        removed = sorted(set(recorded) - set(current))
        changed = sorted(k for k in current.keys() & recorded.keys() if current[k] != recorded[k])
        # Value-free: report only relative file PATHS + counts, never content.
        raise SnapshotTamperedError(
            f"snapshot {snapshot_id!r} llm_source was modified after write "
            f"(added={len(added)} removed={len(removed)} changed={len(changed)}); "
            f"refusing to activate. paths_added={added} paths_removed={removed} "
            f"paths_changed={changed}"
        )
    return True


# ---------------------------------------------------------------------------
# C5.3 — per-study "current" snapshot pointer
# ---------------------------------------------------------------------------


def current_pointer_path(study: str) -> Path:
    """Return ``snapshots/current.json`` for *study* (a no-LLM-zone file)."""
    return snapshots_root(study) / CURRENT_POINTER_FILENAME


def set_current_snapshot(study: str, snapshot_id: str, *, updated_utc: str | None = None) -> Path:
    """Point *study*'s ``current`` pointer at *snapshot_id* (C5.3).

    The snapshot must exist (have a readable manifest). Activating a snapshot
    writes this pointer; the UI shows the current snapshot first.

    Raises:
        SnapshotNotFoundError: *snapshot_id* has no manifest on disk.
    """
    load_snapshot(study, snapshot_id)  # validates existence + manifest
    if updated_utc is None:
        updated_utc = _now_utc_iso()
    pointer = current_pointer_path(study)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(pointer, {"snapshot_id": snapshot_id, "updated_utc": updated_utc})
    return pointer


def get_current_snapshot(study: str) -> str | None:
    """Return *study*'s current snapshot id, or ``None`` if no pointer is set.

    Fail-soft: a missing / corrupt pointer yields ``None`` rather than raising.
    """
    pointer = current_pointer_path(study)
    if not pointer.is_file():
        return None
    try:
        data = _read_json(pointer)
    except (json.JSONDecodeError, OSError, SnapshotError):
        return None
    sid = data.get("snapshot_id")
    return str(sid) if sid else None


# ---------------------------------------------------------------------------
# Snapshot ordering helpers
# ---------------------------------------------------------------------------


def _created_utc_of(study: str, snapshot_id: str) -> str:
    """Return a snapshot's recorded ``created_utc`` (fallback to id for ordering)."""
    try:
        return str(load_snapshot(study, snapshot_id).get("created_utc") or snapshot_id)
    except SnapshotError:
        return snapshot_id


def latest_snapshot(study: str) -> str | None:
    """Return the most-recent snapshot id (by ``created_utc``), or ``None``."""
    ids = list_snapshots(study)
    if not ids:
        return None
    return max(ids, key=lambda sid: (_created_utc_of(study, sid), sid))


def _prior_snapshot_id(study: str, *, exclude: str) -> str | None:
    """Return the most-recent snapshot id other than *exclude*, or ``None``."""
    ids = [sid for sid in list_snapshots(study) if sid != exclude]
    if not ids:
        return None
    return max(ids, key=lambda sid: (_created_utc_of(study, sid), sid))


# ---------------------------------------------------------------------------
# C5.5 — redundant-run: find an existing clean snapshot for an input fingerprint
# ---------------------------------------------------------------------------


def find_snapshot_by_fingerprint(study: str, fingerprint: str) -> str | None:
    """Return the most-recent clean snapshot whose recorded ``input_fingerprint``
    equals *fingerprint* (C5.5), or ``None`` if no such snapshot exists.

    Only snapshots that passed the verifier are considered — an unverified
    snapshot must never satisfy a redundant-run short-circuit. A snapshot with
    no recorded fingerprint (legacy v1) never matches.
    """
    if not fingerprint:
        return None
    matches: list[str] = []
    for sid in list_snapshots(study):
        try:
            manifest = load_snapshot(study, sid)
        except SnapshotError:
            continue
        if manifest.get("input_fingerprint") == fingerprint and manifest.get("verifier_passed"):
            matches.append(sid)
    if not matches:
        return None
    return max(matches, key=lambda sid: (_created_utc_of(study, sid), sid))


# ---------------------------------------------------------------------------
# C5.4 — snapshot staleness (four triggers)
# ---------------------------------------------------------------------------


def check_snapshot_staleness(
    manifest: dict,
    *,
    current_rulebook_version: int | None,
    current_key_fingerprint: str | None,
    current_input_components: dict | None = None,
    current_rulebook_rules_sha256: str | None = None,
) -> list[StalenessFinding]:
    """Classify why a committed snapshot may no longer be authoritative (C5.4).

    Pure function — no I/O. Compares the snapshot *manifest*'s recorded state
    against the supplied *current* values and returns zero or more
    :class:`StalenessFinding`. The four triggers (Note 14):

    1. **rulebook_update** (WARN) — recorded rulebook version != current.
    2. **key_rotation** (BLOCK) — recorded key fingerprint != current; every
       pseudonym in the snapshot is now irrecoverable without a re-scrub.
    3. **source_data_correction** (WARN) — the ``raw_datasets`` input-fingerprint
       component changed (raw data corrected after the snapshot).
    4. **config_change** (WARN) — a ``forms_manifest`` / ``study_privacy`` /
       ``scrub_config_effective`` input-fingerprint component changed
       (jurisdictions / form statuses / scrub rules or posture).

    A comparison is skipped (no false positive) when either side is unknown
    (``None`` / missing), so a legacy v1 snapshot with no provenance yields no
    findings rather than a spurious staleness alarm.
    """
    findings: list[StalenessFinding] = []

    rec_rulebook = manifest.get("phi_rulebook_version")
    if (
        rec_rulebook is not None
        and current_rulebook_version is not None
        and rec_rulebook != current_rulebook_version
    ):
        findings.append(
            StalenessFinding(
                trigger="rulebook_update",
                severity=StalenessSeverity.WARN,
                detail=(
                    f"built with PHI rulebook version {rec_rulebook}, current is "
                    f"{current_rulebook_version} — regulatory compliance may have changed."
                ),
            )
        )

    # N7: a rule-CONTENT change (same version int, different rules_sha256 — e.g.
    # a live AI-extracted rule set) also makes the snapshot stale.
    rec_rules_sha = manifest.get("phi_rulebook_rules_sha256")
    if (
        rec_rules_sha
        and current_rulebook_rules_sha256
        and rec_rules_sha != current_rulebook_rules_sha256
    ):
        findings.append(
            StalenessFinding(
                trigger="rulebook_update",
                severity=StalenessSeverity.WARN,
                detail=(
                    f"built with rule-set content {rec_rules_sha[:12]}, current is "
                    f"{current_rulebook_rules_sha256[:12]} — the effective PHI rules changed."
                ),
            )
        )

    rec_key = manifest.get("phi_key_fingerprint")
    if rec_key and current_key_fingerprint and rec_key != current_key_fingerprint:
        findings.append(
            StalenessFinding(
                trigger="key_rotation",
                severity=StalenessSeverity.BLOCK,
                detail=(
                    "PHI key was rotated after this snapshot — its pseudonyms are "
                    "irrecoverable without a re-scrub. Activation hard-blocked."
                ),
            )
        )

    rec_components = manifest.get("input_fingerprint_components")
    if isinstance(rec_components, dict) and isinstance(current_input_components, dict):
        if _component_changed(rec_components, current_input_components, "raw_datasets"):
            findings.append(
                StalenessFinding(
                    trigger="source_data_correction",
                    severity=StalenessSeverity.WARN,
                    detail=(
                        "raw dataset content changed after this snapshot — it may "
                        "reflect superseded source data."
                    ),
                )
            )
        if (
            _component_changed(rec_components, current_input_components, "forms_manifest")
            or _component_changed(rec_components, current_input_components, "study_privacy")
            or _component_changed(
                rec_components, current_input_components, "scrub_config_effective"
            )
        ):
            findings.append(
                StalenessFinding(
                    trigger="config_change",
                    severity=StalenessSeverity.WARN,
                    detail=(
                        "study config (forms manifest / privacy jurisdictions / scrub "
                        "config) changed after this snapshot — it may not reflect the "
                        "current study definition."
                    ),
                )
            )

    return findings


def _component_changed(recorded: dict, current: dict, key: str) -> bool:
    """True iff *key* is present in BOTH dicts with differing values."""
    return key in recorded and key in current and recorded[key] != current[key]


def evaluate_snapshot_staleness(study: str, snapshot_id: str) -> list[StalenessFinding]:
    """I/O wrapper around :func:`check_snapshot_staleness` (C5.4).

    Gathers the *current* rulebook version, key fingerprint, and live input
    fingerprint components, then classifies the snapshot. Fail-soft on the
    gathering side (an unknown current value simply skips its trigger).
    """
    manifest = load_snapshot(study, snapshot_id)
    current_components: dict | None = None
    try:
        from phi_engine.utils.input_fingerprint import compute_input_fingerprint

        current_components = dict(compute_input_fingerprint(study=study).components)
    except Exception:
        current_components = None
    return check_snapshot_staleness(
        manifest,
        current_rulebook_version=_gather_rulebook_version(),
        current_key_fingerprint=_gather_key_fingerprint(),
        current_input_components=current_components,
        current_rulebook_rules_sha256=_gather_rulebook_rules_sha256(study),
    )


# ---------------------------------------------------------------------------
# C5.6 — snapshot diff (written to the AUDIT folder, not the snapshot)
# ---------------------------------------------------------------------------

_DATASET_FILES_PREFIX = "dataset_schema/files/"


def _jsonl_header_keys(path: Path) -> list[str]:
    """Return the row-1 JSON object KEYS (column names) of a JSONL file — NEVER
    values. Returns ``[]`` on any read/parse error (metadata-only, fail-soft)."""
    try:
        with path.open(encoding="utf-8") as fh:
            first = fh.readline()
        if not first.strip():
            return []
        obj = json.loads(first)
        return list(obj.keys()) if isinstance(obj, dict) else []
    except (OSError, json.JSONDecodeError):
        return []


def _form_stem(rel_path: str) -> str | None:
    """Map a published dataset relpath to its form stem, or ``None`` if not one."""
    if not rel_path.startswith(_DATASET_FILES_PREFIX) or not rel_path.endswith(".jsonl"):
        return None
    return rel_path[len(_DATASET_FILES_PREFIX) : -len(".jsonl")]


def diff_snapshots(study: str, old_id: str, new_id: str) -> dict:
    """Diff two snapshots' published content (C5.6) — METADATA ONLY.

    Compares the two manifests' ``llm_source_manifest`` maps. Returns a dict:

    * ``forms_added`` / ``forms_removed`` — dataset form stems present in only
      one snapshot.
    * ``files_changed`` — any file (form or otherwise) whose content hash
      differs between the two.
    * ``variables_changed`` — for each changed dataset form present in both,
      ``{stem: {"added": [...], "removed": [...]}}`` of row-1 column NAMES
      (never values).

    Reads only file hashes + row-1 header keys; no row values are read.
    """
    old_m = load_snapshot(study, old_id).get("llm_source_manifest") or {}
    new_m = load_snapshot(study, new_id).get("llm_source_manifest") or {}

    old_forms = {s for s in (_form_stem(p) for p in old_m) if s}
    new_forms = {s for s in (_form_stem(p) for p in new_m) if s}
    forms_added = sorted(new_forms - old_forms)
    forms_removed = sorted(old_forms - new_forms)

    files_changed = sorted(p for p in old_m.keys() & new_m.keys() if old_m[p] != new_m[p])

    variables_changed: dict[str, dict[str, list[str]]] = {}
    for rel in files_changed:
        stem = _form_stem(rel)
        if stem is None:
            continue
        old_keys = set(_jsonl_header_keys(snapshot_path(study, old_id) / LLM_SOURCE_DIRNAME / rel))
        new_keys = set(_jsonl_header_keys(snapshot_path(study, new_id) / LLM_SOURCE_DIRNAME / rel))
        added = sorted(new_keys - old_keys)
        removed = sorted(old_keys - new_keys)
        if added or removed:
            variables_changed[stem] = {"added": added, "removed": removed}

    return {
        "study": study,
        "old_snapshot_id": old_id,
        "new_snapshot_id": new_id,
        "forms_added": forms_added,
        "forms_removed": forms_removed,
        "files_changed": files_changed,
        "variables_changed": variables_changed,
    }


def snapshot_diff_path(study: str, old_id: str, new_id: str) -> Path:
    """Return the audit-zone path a snapshot diff is written to (C5.6)."""
    audit_dir = Path(config.OUTPUT_DIR) / study / "audit" / "snapshot_diffs"
    return audit_dir / f"{old_id}__{new_id}.json"


def write_snapshot_diff(study: str, old_id: str, new_id: str, diff: dict) -> Path:
    """Write a snapshot *diff* to the audit folder (C5.6), NOT into the snapshot."""
    path = snapshot_diff_path(study, old_id, new_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, diff)
    return path
