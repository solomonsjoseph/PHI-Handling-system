"""Dataset-publish child skill — CLI + destruction helper for llm_source/.

This module serves two roles:

1. **Destruction helper** (:func:`destroy_staging_and_attest`) — the
   foundational helper from P0.6 that securely removes the per-study AMBER-zone
   staging directory after a successful publish and emits a destruction-
   attestation JSON.

2. **Cross-LLM dataset-publish child CLI** — the ``run / verify / status``
   argparse surface added in P3.1. This is the plugin's trusted host entry
   point for publishing raw workbook data into PHI-clean ``llm_source/``
   outputs via a plain subprocess call. It also preserves the host
   data-dictionary leg when raw dictionary files are present.

Atomicity dependency
--------------------
``destroy_staging_and_attest`` is only called after the publish step has
completed.  The publish step relies on
:func:`scripts.pipeline.host_pipeline._publish_leg`
being atomic: that function uses a sibling temp
directory under ``trio_dir.parent / ".llm_source.publishing"`` and a single
``os.rename`` syscall to promote the populated tree to its final
``llm_source/`` location, ensuring that ``llm_source/`` is either absent or
fully populated after any crash — never half.  The rename site is at the line
labelled ``atomic: rename site (cross-fs path)`` in ``_publish_leg``.  This
wrapper must not be invoked unless that rename has already been confirmed
durable.

IRB-grade context
-----------------
* HIPAA §164.310(c) — device + media controls: staged PHI is overwritten
  with random bytes and fsynced before unlink, then the tree is verified gone.
* DPDPA 2023 §8(7) — erasure: the attestation record provides evidence of
  deletion.
* APFS copy-on-write caveat: filesystem-level overwrite is performed; prior
  APFS snapshots or unreferenced blocks may persist until TRIM.  Skill scope
  is operational untraceability, not forensic erasure.

Exit codes (single source of truth)
------------------------------------
EXIT_OK                  = 0   — success
EXIT_MANIFEST_MISMATCH   = 2   — missing required / unknown / reject form
EXIT_LEDGER_HASH_NULL    = 3   — audit ledger hash is null or sentinel missing
EXIT_QUARANTINE_NON_EMPTY = 4  — quarantine directory non-empty
EXIT_VERIFIER_FAIL       = 5   — verifier assertion failed
EXIT_NEEDS_ADVICE        = 6   — paused — operator inspection required
EXIT_DESTRUCTION_INCOMPLETE = 7 — destruction incomplete
EXIT_PARTIAL_REVIEW      = 8   — partial publish; held forms need review
EXIT_DECISION_MISMATCH = 9 — approved form's applied action != phi_review decided action
EXIT_AUDIT_COVERAGE_INCOMPLETE = 10 — published column has neither a PHI ledger entry nor a non-keep configured scrub rule

Code 1 (generic error) is reserved for unexpected exceptions.

Public API
----------
* :data:`EXIT_OK`
* :data:`EXIT_MANIFEST_MISMATCH`
* :data:`EXIT_LEDGER_HASH_NULL`
* :data:`EXIT_QUARANTINE_NON_EMPTY`
* :data:`EXIT_VERIFIER_FAIL`
* :data:`EXIT_NEEDS_ADVICE`
* :data:`EXIT_DESTRUCTION_INCOMPLETE`
* :data:`EXIT_PARTIAL_REVIEW`
* :class:`DestructionIncompleteError`
* :func:`destroy_staging_and_attest`
* :func:`main` (argparse entry point)
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from scripts.audit.ledger import dataset_phi_ledger_path, iter_dataset_phi_ledger_paths
from scripts.extraction.dataset_pipeline import (
    ManifestMismatchError,
    check_forms_manifest,
)
from scripts.security.phi_guard_gate import run_phi_guard_gate
from scripts.security.phi_patterns import SUBJECT_ID_PATTERNS
from scripts.security.phi_review import _normalize_header as _normalize_hdr
from scripts.utils.secure_staging import secure_remove_tree

# NOTE: the former symmetric-strict `_DECIDED_APPLIED_EQUIV` table was replaced by
# the protection lattice `_PROTECTION_RANK` (defined near assertion 12). Assertion
# 12 now fails only the UNDER-protection direction (applied less protective than
# phi_review decided) instead of every over-protection / absent-header case.

__all__ = [
    "EXIT_AUDIT_COVERAGE_INCOMPLETE",
    "EXIT_DECISION_MISMATCH",
    "EXIT_DESTRUCTION_INCOMPLETE",
    "EXIT_LEDGER_HASH_NULL",
    "EXIT_MANIFEST_MISMATCH",
    "EXIT_NEEDS_ADVICE",
    "EXIT_OK",
    "EXIT_PARTIAL_REVIEW",
    "EXIT_QUARANTINE_NON_EMPTY",
    "EXIT_VERIFIER_FAIL",
    "DestructionIncompleteError",
    "FormGateResult",
    "ManifestMismatchError",
    "check_forms_manifest",
    "destroy_staging_and_attest",
    "main",
]

# ---------------------------------------------------------------------------
# Exit code constants — single source of truth
# ---------------------------------------------------------------------------

EXIT_OK: int = 0
EXIT_MANIFEST_MISMATCH: int = 2
EXIT_LEDGER_HASH_NULL: int = 3
EXIT_QUARANTINE_NON_EMPTY: int = 4
EXIT_VERIFIER_FAIL: int = 5
EXIT_NEEDS_ADVICE: int = 6
EXIT_DESTRUCTION_INCOMPLETE: int = 7
EXIT_PARTIAL_REVIEW: int = 8
EXIT_DECISION_MISMATCH: int = 9
EXIT_AUDIT_COVERAGE_INCOMPLETE: int = 10

# ---------------------------------------------------------------------------
# Destruction helper (P0.6) — kept verbatim
# ---------------------------------------------------------------------------

_APFS_COW_DISCLAIMER = (
    "Filesystem-level overwrite was performed via secrets.token_bytes + fsync; "
    "APFS copy-on-write means prior blocks may persist until trimmed. "
    "Skill scope is operational untraceability, not forensic erasure."
)


class DestructionIncompleteError(Exception):
    """Raised when staging_dir still exists after secure_remove_tree.

    The CLI wrapper (P3.1) translates this to exit code 7.
    """


def destroy_staging_and_attest(
    *,
    study: str,
    run_id: str,
    staging_dir: Path,
    output_dir: Path,
) -> Path:
    """Securely remove staging_dir and emit a destruction-attestation JSON.

    Returns the path of the attestation file.

    Raises DestructionIncompleteError if staging_dir still exists after
    secure_remove_tree (exit code 7 for the wrapper that calls this).

    Args:
        study: Study name (e.g. "Indo-VAP"); used for paths and the
            attestation ``study`` field.
        run_id: Opaque run identifier provided by the caller; will be
            generated by the CLI wrapper in P3.1.
        staging_dir: The ``config.STUDY_STAGING_DIR / STUDY`` path to destroy.
        output_dir: The ``output/{STUDY}/`` root where
            ``runs/{run_id}/destruction_attestation.json`` should land.

    This function is ONLY invoked on a successful publish.  On any earlier
    pipeline failure the caller skips it so staging is preserved for
    inspection.  No "should I run?" guard lives here — that is the caller's
    contract.
    """
    staging_dir = Path(staging_dir)
    output_dir = Path(output_dir)

    # ------------------------------------------------------------------
    # a. Snapshot what's about to be destroyed BEFORE deletion.
    # ------------------------------------------------------------------
    removed_paths: list[str] = []
    if staging_dir.exists():
        for p in sorted(staging_dir.rglob("*")):
            if p.is_file():
                # Store relative path so the attestation is portable.
                try:
                    rel = p.relative_to(staging_dir)
                except ValueError:
                    rel = p
                removed_paths.append(str(rel))

    # ------------------------------------------------------------------
    # a.5. Guard (I-5): assert no path segment leaks a subject ID into
    #      the attestation. If a filename was ever named after a subject,
    #      this would silently leak PHI into the destruction JSON.
    # ------------------------------------------------------------------
    for rp in removed_paths:
        for segment in Path(rp).parts:
            for pattern in SUBJECT_ID_PATTERNS:
                if re.search(pattern, segment):
                    raise ValueError(
                        "destroy_staging_and_attest: a removed path segment matches "
                        "SUBJECT_ID_PATTERNS — filename schema must be PHI-free. "
                        "Inspect the staging directory structure before retrying."
                    )

    files_destroyed = len(removed_paths)

    # ------------------------------------------------------------------
    # b. Record destruction start timestamp.
    # ------------------------------------------------------------------
    started_utc = datetime.now(UTC).isoformat()

    # ------------------------------------------------------------------
    # c. Securely remove the tree.
    # ------------------------------------------------------------------
    secure_remove_tree(staging_dir)

    # ------------------------------------------------------------------
    # d. Verify the tree is gone.
    # ------------------------------------------------------------------
    if staging_dir.exists():
        raise DestructionIncompleteError(
            f"staging_dir still exists after secure_remove_tree: {staging_dir}"
        )

    # ------------------------------------------------------------------
    # e. Record destruction completion timestamp.
    # ------------------------------------------------------------------
    completed_utc = datetime.now(UTC).isoformat()

    # ------------------------------------------------------------------
    # f. Write attestation atomically (write to .tmp sibling, then rename).
    # ------------------------------------------------------------------
    attest_dir = output_dir / "runs" / run_id
    attest_dir.mkdir(parents=True, exist_ok=True)
    attest_path = attest_dir / "destruction_attestation.json"

    payload = {
        "apfs_cow_disclaimer": _APFS_COW_DISCLAIMER,
        "completed_utc": completed_utc,
        "cryptographic_erasure": False,
        "files_destroyed": files_destroyed,
        "removed_paths": removed_paths,
        "run_id": run_id,
        "staging_path": str(staging_dir),
        "started_utc": started_utc,
        "study": study,
    }
    serialised = json.dumps(payload, indent=2, sort_keys=True)

    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=attest_dir,
        prefix=".destruction_attestation_",
        suffix=".tmp",
    )
    try:
        try:
            os.write(tmp_fd, serialised.encode("utf-8"))
            os.fsync(tmp_fd)
        finally:
            os.close(tmp_fd)
        Path(tmp_name).replace(attest_path)
    except Exception:
        # Best-effort cleanup of the .tmp file on error.
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise

    # ------------------------------------------------------------------
    # g. Return the attestation file path.
    # ------------------------------------------------------------------
    return attest_path


# ---------------------------------------------------------------------------
# Status banner text
# ---------------------------------------------------------------------------

_STATUS_BANNER = """\
extract_to_llm_source — skill scope and contract
=================================================

Scope: trusted host dataset publish into PHI-clean llm_source/ (one study)
Dictionary: host dictionary leg is preserved when raw dictionary files exist

PHI coverage: HIPAA Safe Harbor identifiers per config/_defaults/phi_scrub.yaml
              + project-specific patterns in scripts/security/phi_patterns.py
Out of scope (operator responsibility): DPDPA §16 cross-border egress,
                                        §12 right-to-erase, §8(6) breach
                                        notification, ICMR l-diversity gate.

Temp removal: operational untraceability after successful publish (APFS COW
              acknowledged in destruction attestation; not forensic erasure).

Exit codes:
  0 — ok
  2 — manifest mismatch (missing required / unknown / reject)
  3 — audit ledger hash is null or sentinel missing
  4 — quarantine directory non-empty
  5 — verifier assertion failed
  6 — needs-advice (paused — operator inspection required)
  7 — destruction incomplete
  8 — partial publish; held forms need human review
  9 — phi_review decision != ledger-applied scrub action (under-protection)
 10 — published column has neither a PHI ledger entry nor a non-keep scrub rule
"""

# ---------------------------------------------------------------------------
# Subcommand: status
# ---------------------------------------------------------------------------


def _cmd_status(_args: argparse.Namespace) -> int:
    """Print scope banner and exit 0."""
    print(_STATUS_BANNER, end="")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Subcommand: verify — 17-assertion verifier (assertions 1-12, 14, 15, 16, 17, 13 in execution order)
# ---------------------------------------------------------------------------

# Determinism-check: these keys must not appear in any llm_source/ artifact.
_FORBIDDEN_RUNTIME_KEYS: frozenset[str] = frozenset({"extraction_utc", "run_id", "generated_utc"})

# Required fields for destruction_attestation.json (assertion 4).
_ATTESTATION_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {
        "run_id",
        "study",
        "started_utc",
        "completed_utc",
        "staging_path",
        "removed_paths",
        "files_destroyed",
        "cryptographic_erasure",
        "apfs_cow_disclaimer",
    }
)

# Rough ISO-8601 check (YYYY-MM-DDT or YYYY-MM-DD).
_ISO8601_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]")


def _resolve_run_id(study_output_dir: Path, run_id_arg: str | None) -> tuple[str | None, str]:
    """Return (run_id, error_message).

    error_message is empty when resolution succeeds.  When resolution fails,
    run_id is None and error_message describes the problem.
    """
    if run_id_arg:
        return run_id_arg, ""

    runs_dir = study_output_dir / "runs"
    if not runs_dir.is_dir():
        return None, f"runs/ directory not found at {runs_dir}; no run to verify"

    candidates = sorted(
        [d for d in runs_dir.iterdir() if d.is_dir()],
        key=lambda d: d.name,
        reverse=True,
    )
    if not candidates:
        return None, f"runs/ is empty at {runs_dir}; no run to verify"

    terminal_candidates: list[tuple[str, str]] = []
    for run_dir in candidates:
        status_path = run_dir / "status.json"
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        if status.get("exit_code") not in {EXIT_OK, EXIT_PARTIAL_REVIEW}:
            continue
        if status.get("publish_status") not in {"complete", "partial"}:
            continue
        completed = status.get("completed_utc") or status.get("started_utc") or ""
        terminal_candidates.append((str(completed), run_dir.name))

    if not terminal_candidates:
        return (
            None,
            f"runs/ at {runs_dir} contains no successful or partial-safe run to verify",
        )

    terminal_candidates.sort(reverse=True)
    return terminal_candidates[0][1], ""


# ── individual assertion helpers ────────────────────────────────────────────

_AssertionResult = tuple[str, str]  # ("pass"|"fail"|"skipped", detail_str)


def _verify_assertion_1_manifest_exists_parses(
    study_raw_dir: Path,
) -> _AssertionResult:
    """Assertion 1: _forms_manifest.yaml exists and parses as a dict."""
    import config

    manifest_path = config.study_config_path("_forms_manifest.yaml", study=study_raw_dir.name)
    if not manifest_path.exists():
        return "fail", f"_forms_manifest.yaml not found at {manifest_path}"
    try:
        with manifest_path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if not isinstance(data, dict):
            return "fail", (
                f"_forms_manifest.yaml at {manifest_path} parsed but is not a dict "
                f"(got {type(data).__name__})"
            )
    except yaml.YAMLError as exc:
        return "fail", f"_forms_manifest.yaml at {manifest_path} failed to parse: {exc}"
    return "pass", ""


def _verify_assertion_2_manifest_reconciles(
    datasets_dir: Path,
) -> _AssertionResult:
    """Assertion 2: manifest reconciles with datasets/ actual contents."""
    try:
        check_forms_manifest(datasets_dir)
    except ManifestMismatchError as exc:
        return "fail", str(exc)
    except Exception as exc:
        return "fail", f"check_forms_manifest raised unexpected error: {exc}"
    return "pass", ""


def _verify_assertion_3_staging_absent(staging_dir: Path) -> _AssertionResult:
    """Assertion 3: study_staging_dir must not exist."""
    if staging_dir.exists():
        return "fail", f"staging dir still present: {staging_dir}"
    return "pass", ""


def _verify_assertion_4_attestation_valid(run_dir: Path) -> _AssertionResult:
    """Assertion 4: destruction_attestation.json exists, parses, has required fields."""
    attest_path = run_dir / "destruction_attestation.json"
    if not attest_path.exists():
        return "fail", f"destruction_attestation.json not found at {attest_path}"

    try:
        data = json.loads(attest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return "fail", f"destruction_attestation.json failed to parse: {exc}"

    missing = _ATTESTATION_REQUIRED_FIELDS - set(data.keys())
    if missing:
        return "fail", f"destruction_attestation.json missing required fields: {sorted(missing)}"

    # Timestamps look like ISO-8601
    for ts_field in ("started_utc", "completed_utc"):
        val = data.get(ts_field, "")
        if not isinstance(val, str) or not _ISO8601_RE.match(val):
            return "fail", (
                f"destruction_attestation.json field {ts_field!r} does not look "
                f"like ISO-8601: {val!r}"
            )

    return "pass", ""


def _verify_assertion_5_ledger_hashes(
    audit_dir: Path, phi_scrub_config_path: Path
) -> _AssertionResult:
    """Assertion 5: every per-dataset PHI ledger has required hashes;
    scrub_config_hash matches SHA-256 of the current phi_scrub.yaml.
    """
    ledger_paths = iter_dataset_phi_ledger_paths(audit_dir)
    if not ledger_paths:
        return (
            "fail",
            f"no per-dataset phi_handling_ledger.as_written.json files under {audit_dir / 'datasets'}",
        )

    # Hash the MERGED EFFECTIVE scrub config (defaults + per-study override) via
    # the shared helper, NOT a single file — this MUST match the hash the ledger
    # writer (phi_scrub.run_scrub) sealed in, which uses the same helper.
    import scripts.security.phi_scrub as _phi_scrub

    actual_hash = _phi_scrub.effective_scrub_config_hash()
    if not actual_hash:
        return (
            "fail",
            f"phi_scrub config not resolvable (checked {phi_scrub_config_path}); cannot verify hash",
        )

    for ledger_path in ledger_paths:
        try:
            data = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return "fail", f"{ledger_path} failed to parse: {exc}"

        for field in ("run_id", "scrub_config_hash", "input_dataset_hash"):
            if not data.get(field):
                return "fail", f"{ledger_path}: {field!r} is null or absent"

        persisted_hash = data["scrub_config_hash"]
        if persisted_hash != actual_hash:
            return "fail", (
                f"scrub_config_hash mismatch: {ledger_path} has {persisted_hash!r}, "
                f"current phi_scrub.yaml hashes to {actual_hash!r}"
            )

    return "pass", ""


def _verify_assertion_6_no_llm_zone(audit_dir: Path) -> _AssertionResult:
    """Assertion 6: .NO_LLM_ZONE sentinel file exists."""
    sentinel = audit_dir / ".NO_LLM_ZONE"
    if not sentinel.exists():
        return "fail", f".NO_LLM_ZONE sentinel not found at {sentinel}"
    return "pass", ""


def _verify_assertion_7_no_quarantine(
    tmp_dir: Path, study: str, study_output_dir: Path
) -> _AssertionResult:
    """Assertion 7: no quarantine/ directory under tmp/ or output/{STUDY}/."""
    # Check tmp/<study>/quarantine/
    staging_quarantine = tmp_dir / study / "quarantine"
    if staging_quarantine.is_dir() and any(staging_quarantine.iterdir()):
        return "fail", f"non-empty quarantine dir found: {staging_quarantine}"
    # Check output/{STUDY}/quarantine/ (anywhere under output dir)
    for quarantine_dir in study_output_dir.rglob("quarantine"):
        if quarantine_dir.is_dir() and any(quarantine_dir.iterdir()):
            return "fail", f"non-empty quarantine dir found: {quarantine_dir}"
    return "pass", ""


def _verify_assertion_8_phi_absence(dataset_files_dir: Path) -> _AssertionResult:
    """Assertion 8: no published dataset JSONL matches PHI patterns (blocking).

    D2: routed through the OR-combined PHI guard gate (Presidio primary +
    study-calibrated ``scan_tree_for_phi`` secondary) — fails if either scanner
    finds PHI. Detail names file path + line number + entity/pattern, never the
    matched text.
    """
    result = run_phi_guard_gate(dataset_files_dir)
    if not result.ok:
        return "fail", result.detail
    return "pass", ""


def _verify_assertion_9_no_runtime_keys(llm_source_dir: Path) -> _AssertionResult:
    """Assertion 9: no runtime keys in published dataset artifacts.

    Reads JSONL line-by-line; also checks JSON files.
    """
    if not llm_source_dir.is_dir():
        return "pass", ""

    for fpath in sorted(llm_source_dir.rglob("*")):
        if not fpath.is_file():
            continue
        suffix = fpath.suffix.lower()
        if suffix not in {".jsonl", ".json"}:
            continue
        try:
            with fpath.open(encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    found = _FORBIDDEN_RUNTIME_KEYS & set(obj.keys())
                    if found:
                        rel = fpath.relative_to(llm_source_dir)
                        return "fail", (
                            f"forbidden runtime key(s) {sorted(found)} found in "
                            f"{rel} line {lineno} (determinism violation)"
                        )
        except OSError as exc:
            return "fail", f"could not read {fpath}: {exc}"

    return "pass", ""


def _verify_assertion_10_required_jsonls_present(
    manifest_path: Path, llm_source_dir: Path, run_dir: Path | None = None
) -> _AssertionResult:
    """Assertion 10: every required form in manifest has exactly one JSONL under
    llm_source/dataset_schema/files/.
    """
    try:
        with manifest_path.open(encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        required_forms: list[str] = raw.get("required") or []
    except Exception as exc:
        return "fail", f"could not load manifest for assertion 10: {exc}"

    if run_dir is not None:
        approval_path = run_dir / "phi_handling_approval.json"
        if approval_path.exists():
            try:
                approval = json.loads(approval_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                return "fail", f"could not load PHI approval report: {exc}"
            approved_forms = [str(item) for item in approval.get("approved_forms", [])]
            if approved_forms:
                required_forms = approved_forms

    datasets_out = llm_source_dir / "dataset_schema" / "files"
    missing: list[str] = []
    for form in required_forms:
        stem = Path(form).stem
        expected = datasets_out / f"{stem}.jsonl"
        if not expected.exists():
            missing.append(form)

    if missing:
        return "fail", (
            f"required form(s) missing from llm_source/dataset_schema/files/: {missing}"
        )

    return "pass", ""


def _verify_assertion_11_no_pipeline_lock(tmp_dir: Path, study: str) -> _AssertionResult:
    """Assertion 11: pipeline lock file must be absent.

    Exception: when THIS process is the lock holder (Step 7 inline verify
    runs while the wrapper still holds its own flock; it is released only
    in the run flow's ``finally``), the lock is evidence of the run in
    progress, not a stale leftover — pass with a detail note. A standalone
    ``verify`` in a separate process never holds the flock, so a genuinely
    stale lock file still fails.
    """
    lock_path = tmp_dir / f".{study}.pipeline.lock"
    if lock_path.exists():
        # The lock implementation lives in scripts.utils.pipeline_lock (Wave 4);
        # ask it whether THIS process holds the very lock file we found.
        from scripts.utils.pipeline_lock import baton_is_valid, held_lock_path

        held = held_lock_path()
        if held is not None and held.resolve() == lock_path.resolve():
            return "pass", "lock held by this process (inline verify during run)"
        # Risk #7: under the orchestrator the lock is held by the PARENT and the
        # baton was handed to this skill subprocess. A valid baton means the
        # present lock is the evidence of the in-progress orchestrated run, not a
        # stale leftover from a dead process.
        if baton_is_valid():
            return "pass", "lock held by parent orchestrator (valid baton, run in progress)"
        return "fail", f"pipeline lock file still present: {lock_path}"
    return "pass", ""


def _hold_run_for_review(run_dir: Path, forms: list[str]) -> None:
    """Flip the run's status.json to held + record the offending forms (fail-closed routing)."""
    status_path = run_dir / "status.json"
    if not status_path.is_file():
        return
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    status["publish_status"] = "held"
    existing = list(status.get("held_forms", []))
    for f in forms:
        if f not in existing:
            existing.append(f)
    status["held_forms"] = existing
    status["verifier_passed"] = False
    _atomic_write_json(status_path, status)


# Protection lattice for the decided-vs-applied check (assertion 12). Higher rank
# = more protective. The verifier fails ONLY the under-protection direction
# (scrub did LESS than phi_review decided = potential leak). Scrub being MORE
# protective than decided (e.g. phi_review keep, scrub drop) is always acceptable —
# it cannot leak. This replaces the old symmetric-strict equivalence table, which
# falsely flagged ~every over-protection and every classified-but-absent header.
_PROTECTION_RANK: dict[str, int] = {
    "keep": 0,
    # value retained but coarsened / bounded
    "generalize": 1,
    "band": 1,
    "cap": 1,
    "suppress_small_cell": 1,
    "suppress": 1,
    # value replaced by a non-identifying token, column retained
    "jitter_date": 2,
    "pseudonymize": 2,
    # value / column removed entirely (maximally protective)
    "drop": 3,
    "birthdate_drop": 3,
}


def _configured_scrub_action(cfg: Any, name: str) -> str:
    """The action the scrub CONFIG would apply to *name*, by rule priority.

    Fallback for assertion 12 when the ledger carries no event for a PUBLISHED
    column: a CONDITIONAL transform (cap fires only for age > 89; pseudonymize
    only for a non-null id; suppress_small_cell only above the threshold)
    legitimately emits no event when no value qualifies, yet the column is still
    PROTECTED by the configured rule. Mirrors the rule priority in
    ``phi_scrub._scrub_row`` (keep→birthdate→drop→cap→generalize→band→
    suppress→date→id). Returns ``"keep"`` when nothing matches (genuine keep).
    """
    if cfg is None:
        return "keep"
    if cfg.field_is_keep(name):
        return "keep"
    if cfg.field_is_birthdate(name):
        return "birthdate_drop" if cfg.compliance_posture == "safe_harbor" else "jitter_date"
    if cfg.field_is_drop(name):
        return "drop"
    if cfg.cap_rule_for(name) is not None:
        return "cap"
    if cfg.generalize_rule_for(name) is not None:
        return "generalize"
    if cfg.band_rule_for(name) is not None:
        return "band"
    if cfg.field_is_suppress_small_cell(name):
        return "suppress_small_cell"
    if cfg.field_is_date(name):
        return "jitter_date"
    if cfg.id_label_for(name) is not None:
        return "pseudonymize"
    return "keep"


def _verify_assertion_decided_vs_applied(
    audit_dir: Path,
    run_dir: Path,
    dataset_files_dir: Path,
    study: str,
) -> _AssertionResult:
    """Each approved form's APPLIED protection must be ≥ phi_review's DECIDED protection.

    Fail-closed on the UNDER-protection direction only (scrub did less than the
    regulation classifier decided → potential leak). Over-protection (scrub did
    MORE) passes. Two scoping rules prevent false positives:

      * A classified header NOT present in the published dataset is skipped — a
        dropped / renamed / duplicate-collapsed upstream column cannot be
        under-protected in an output it is absent from.
      * When a published column has no ledger event/keep_decision, the CONFIGURED
        scrub action (``_configured_scrub_action``) is used — so a conditional
        transform that did not fire (cap with no age > 89, pseudonymize on a null
        id) still counts as its configured protection, not a false "keep".

    No approval file → pass (legacy/disabled scrub; nothing to cross-check).
    """
    approval_path = run_dir / "phi_handling_approval.json"
    if not approval_path.is_file():
        return "pass", ""
    try:
        approval = json.loads(approval_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return "fail", f"phi_handling_approval.json unreadable: {exc}"

    cfg: Any = None
    try:
        import scripts.security.phi_scrub as _phi_scrub

        # Merged-config load (risk #8): deep-merge config/_defaults/phi_scrub.yaml
        # with the per-study config/<study>/phi_scrub.yaml override, so this
        # decided-vs-applied check evaluates the SAME effective config run_scrub
        # actually applied. (When no per-study override exists the merged config ==
        # defaults, identical to the prior single-file behaviour.)
        cfg = _phi_scrub.load_scrub_config(study=study)
    except Exception:  # config load failure → conservative keep fallback
        cfg = None

    approved = set(approval.get("approved_forms", []))
    mismatches: list[str] = []
    bad_forms: list[str] = []
    for form in approval.get("forms", []):
        form_name = str(form.get("form_name", ""))
        if form_name not in approved:
            continue  # held forms are already routed to review
        # Only verify columns ACTUALLY PRESENT in the published output.
        jsonl_path = dataset_files_dir / f"{Path(form_name).stem}.jsonl"
        published = {_normalize_hdr(h) for h in _published_header_keys(jsonl_path)}
        if not published:
            continue
        applied: dict[str, str] = {}
        ledger_path = dataset_phi_ledger_path(audit_dir, form_name)
        if ledger_path.is_file():
            try:
                led = json.loads(ledger_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                led = {}
            for ev in led.get("events", []):
                applied[_normalize_hdr(str(ev.get("variable_id", "")))] = str(ev.get("action", ""))
            for kd in led.get("keep_decisions", []):
                applied.setdefault(_normalize_hdr(str(kd.get("variable_id", ""))), "keep")
        for cls in form.get("classifications", []):
            raw_header = str(cls.get("header", ""))
            hdr = _normalize_hdr(raw_header)
            if hdr not in published:
                continue  # not in published output — nothing to under-protect
            decided = str(cls.get("action", ""))
            applied_action = applied.get(hdr)
            if applied_action is None:
                applied_action = _configured_scrub_action(cfg, raw_header)
            if _PROTECTION_RANK.get(applied_action, 0) < _PROTECTION_RANK.get(decided, 0):
                mismatches.append(
                    f"{form_name}:{raw_header} decided={decided} applied={applied_action}"
                )
                if form_name not in bad_forms:
                    bad_forms.append(form_name)
    if mismatches:
        _hold_run_for_review(run_dir, bad_forms)
        return "fail", "; ".join(mismatches[:10])
    return "pass", ""


# Pipeline-internal / provenance columns that may appear in a published JSONL but
# are NOT study variables — they require no per-variable PHI ledger accounting.
# Mirrors cleanup_propagation.PROVENANCE_FIELDS (agent_tools._INTERNAL_COLUMNS plus
# _metadata / _phi_scrubbed); kept as a local literal to avoid importing the
# agent-tools dependency chain into the trusted host CLI.
_INTERNAL_PUBLISHED_COLUMNS: frozenset[str] = frozenset(
    {"source_file", "_provenance", "_source_row", "_ingestion_ts", "_metadata", "_phi_scrubbed"}
)


def _published_header_keys(jsonl_path: Path) -> set[str]:
    """Return row-1 STUDY-VARIABLE header KEYS from a published JSONL — metadata only.

    Reads a single line and discards values; the published tree is already
    PHI-scrubbed (the agent reads it directly), so enumerating its column names
    is metadata. ``__`` marker fields and pipeline-internal/provenance columns
    (:data:`_INTERNAL_PUBLISHED_COLUMNS`) are excluded — they are not study
    variables and carry no per-variable PHI ledger entry.
    """
    try:
        with jsonl_path.open(encoding="utf-8") as handle:
            first_line = handle.readline()
    except OSError:
        return set()
    if not first_line.strip():
        return set()
    try:
        record = json.loads(first_line)
    except json.JSONDecodeError:
        return set()
    if not isinstance(record, dict):
        return set()
    return {
        key
        for key in record
        if not str(key).startswith("__") and key not in _INTERNAL_PUBLISHED_COLUMNS
    }


def _ledger_accounted_headers(ledger_path: Path) -> set[str]:
    """Return the normalized header set accounted for in one PHI ledger.

    A header is accounted when it has a PHI ``event`` (dropped/transformed) OR a
    ``keep_decision`` (deliberately retained).
    """
    if not ledger_path.is_file():
        return set()
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    accounted: set[str] = set()
    for event in ledger.get("events", []):
        accounted.add(_normalize_hdr(str(event.get("variable_id", ""))))
    for keep in ledger.get("keep_decisions", []):
        accounted.add(_normalize_hdr(str(keep.get("variable_id", ""))))
    return accounted


def _verify_assertion_15_sot_joined_view_present(
    llm_source_dir: Path,
    dataset_files_dir: Path,
    *,
    study: str,
    repo_root: Path,
) -> _AssertionResult:
    """Every published dataset form has a SoT joined query view under llm_source/SoT/."""
    from scripts.ai_assistant.sot_joined_view import resolve_sot_joined_view_path

    sot_root = llm_source_dir / "SoT"
    if not dataset_files_dir.is_dir():
        return "pass", ""
    from scripts.source_truth.generate_lean_outputs import pdf_backed_dataset_stems

    required = pdf_backed_dataset_stems(study, repo_root)
    missing: list[str] = []
    for jsonl_path in sorted(dataset_files_dir.glob("*.jsonl")):
        stem = jsonl_path.stem
        if stem not in required:
            continue
        joined = resolve_sot_joined_view_path(sot_root, stem)
        if not joined.is_file():
            missing.append(stem)
    if missing:
        return "fail", f"published form(s) missing SoT joined view: {', '.join(missing)}"
    return "pass", ""


def _verify_assertion_14_audit_coverage(
    audit_dir: Path, dataset_files_dir: Path, run_dir: Path, study: str
) -> _AssertionResult:
    """Every PUBLISHED dataset column has a per-variable audit accounting.

    A column is accounted when it has a PHI ledger ``event`` / ``keep_decision``
    OR the scrub CONFIG defines a non-keep rule for it (``_configured_scrub_action``
    != "keep") — a conditional transform (e.g. an all-null date column the date
    rule processed but found nothing to jitter) fires no event yet is still a
    handled variable. A column the config would KEEP must carry an explicit
    keep_decision; otherwise it was silently retained with no audit record.
    Fail-closed — hold the run and exit ``EXIT_AUDIT_COVERAGE_INCOMPLETE``.

    No approval file → pass (legacy/disabled scrub path carries no provenance,
    consistent with assertion 12). Held forms are not published, so they are not
    checked here; risky KEEP columns were already held pre-publish by the PHI
    coverage gate (phi_review.is_phi_risky_header).
    """
    approval_path = run_dir / "phi_handling_approval.json"
    if not approval_path.is_file() or not dataset_files_dir.is_dir():
        return "pass", ""
    try:
        approval = json.loads(approval_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return "fail", f"phi_handling_approval.json unreadable: {exc}"

    cfg: Any = None
    try:
        import scripts.security.phi_scrub as _phi_scrub

        # Merged-config load (risk #8): coverage is checked against the SAME
        # deep-merged defaults+per-study config run_scrub applied — see the
        # matching note in _verify_assertion_decided_vs_applied.
        cfg = _phi_scrub.load_scrub_config(study=study)
    except Exception:  # config load failure → conservative (keep) classification
        cfg = None

    gaps: list[str] = []
    bad_forms: list[str] = []
    for form_name in sorted(set(approval.get("approved_forms", []))):
        jsonl_path = dataset_files_dir / f"{Path(form_name).stem}.jsonl"
        if not jsonl_path.is_file():
            continue  # not published (held/optional) — nothing to verify
        headers = _published_header_keys(jsonl_path)
        if not headers:
            continue
        accounted = _ledger_accounted_headers(dataset_phi_ledger_path(audit_dir, form_name))
        for header in sorted(headers):
            if _normalize_hdr(header) in accounted:
                continue
            # No ledger entry: a CONFIGURED non-keep rule (conditional transform
            # that fired no event on this data) still counts as handled; a
            # config-keep column with no keep_decision is a genuine silent-keep gap.
            if _configured_scrub_action(cfg, header) != "keep":
                continue
            gaps.append(f"{form_name}:{header}")
            if form_name not in bad_forms:
                bad_forms.append(form_name)
    if gaps:
        _hold_run_for_review(run_dir, bad_forms)
        return "fail", "; ".join(gaps[:10])
    return "pass", ""


def _assertion_13_update_status(
    run_dir: Path,
) -> _AssertionResult:
    """Assertion 13: status.json exists; on pass, set verifier_passed: true."""
    status_path = run_dir / "status.json"
    if not status_path.exists():
        return "fail", f"status.json not found at {status_path}"
    try:
        status_data = json.loads(status_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return "fail", f"status.json failed to parse: {exc}"

    status_data["verifier_passed"] = True
    _atomic_write_json(status_path, status_data)
    return "pass", ""


# ── dispatcher ───────────────────────────────────────────────────────────────


def _verify_assertion_16_ledger_fields_complete(audit_dir: Path) -> _AssertionResult:
    """Assertion 16 (N10): every PHI ledger EVENT is fully documented —
    what/why/which-regulation/which-method.

    For each event in a dataset PHI ledger, requires: a ``method`` (which method),
    ``rule.jurisdictions`` (the regulatory scope), and a WHY — either a specific
    ``rule.taxonomy`` (rulebook-rule match) OR a ``rationale``. Config-driven drops
    (``drop_fields`` patterns) legitimately carry no rulebook taxonomy but document
    jurisdictions + method + rationale, so they pass; a genuinely under-documented
    event (no method, or no jurisdictions, or neither taxonomy nor rationale) fails.
    Keep-decisions are documented separately and excluded. Reads ledger metadata
    only — counts + variable NAMES, never values.
    """
    incomplete: list[str] = []
    total = 0
    for ledger_path in iter_dataset_phi_ledger_paths(audit_dir):
        try:
            data = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        events = data.get("events")
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, dict):
                continue
            total += 1
            rule = event.get("rule") if isinstance(event.get("rule"), dict) else {}
            why = rule.get("taxonomy") or event.get("rationale")
            if not event.get("method") or not rule.get("jurisdictions") or not why:
                incomplete.append(f"{ledger_path.parent.name}/{event.get('variable_id', '?')}")
    if incomplete:
        shown = ", ".join(incomplete[:5])
        return (
            "fail",
            f"{len(incomplete)}/{total} PHI ledger event(s) under-documented "
            f"(need method + jurisdictions + taxonomy-or-rationale; e.g. {shown})",
        )
    return "pass", f"all {total} PHI ledger event(s) carry taxonomy + jurisdictions + method"


def _verify_assertion_17_cap_application_complete(
    dataset_files_dir: Path, study: str
) -> _AssertionResult:
    """Assertion 17: no un-capped age survives in a cap-ruled published column.

    Output invariant that COMPLETES assertion 12's coverage. Assertion 12 confirms a
    cap column is PROTECTED at the cap LEVEL (a configured cap rule exists, even when
    no event fired), but it does not confirm the cap RESULT holds. ``cap_numeric``
    clamps every numeric value strictly above the threshold — HIPAA Safe Harbor
    §164.514(b)(2)(i)(C), age > 89 → a single 90+ category — to the label. This
    re-runs that exact predicate over the PUBLISHED output and fails if any value
    ``cap_numeric`` WOULD still clamp is present, i.e. capping did not run on a
    cap-ruled column. No gate else checks this: the residual PHI scanner cannot flag
    a bare age without false-positiving on every glucose / height / lab value.

    Reuses ``cap_numeric`` itself (zero drift from the scrub) and is therefore scoped
    to bare-numeric values by that function's own contract — categorical text in an
    age-named field (e.g. a coded ``NC_AGE``) returns ``was_capped=False`` and never
    false-positives. Reads the scrubbed, LLM-readable JSONL; count-only — detail names
    form:column + count, never a value. No scrub config → pass (mirrors assertion 12).
    """
    if not dataset_files_dir.is_dir():
        return "pass", ""
    try:
        import scripts.security.phi_scrub as _phi_scrub

        cfg = _phi_scrub.load_scrub_config(study=study)
    except Exception:
        return "pass", ""  # config load failure → nothing to cross-check
    if cfg is None:
        return "pass", ""

    offenders: list[str] = []
    for fpath in sorted(dataset_files_dir.glob("*.jsonl")):
        cap_rule_for: dict[str, Any] = {}  # col -> CapRule|None (resolved once)
        counts: dict[str, int] = {}
        try:
            with fpath.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    for col, val in row.items():
                        rule = cap_rule_for.get(col, False)
                        if rule is False:
                            rule = cfg.cap_rule_for(col)
                            cap_rule_for[col] = rule
                        if rule is None:
                            continue
                        _, was_capped = _phi_scrub.cap_numeric(
                            val, threshold=rule.threshold, label=rule.label
                        )
                        if was_capped:
                            counts[col] = counts.get(col, 0) + 1
        except OSError as exc:
            return "fail", f"could not read {fpath}: {exc}"
        for col, c in sorted(counts.items()):
            offenders.append(f"{fpath.stem}:{col}={c}")

    if offenders:
        return "fail", (
            "un-capped numeric value(s) above the age threshold survived in "
            f"cap-ruled column(s): {', '.join(offenders)} (count-only)"
        )
    return "pass", ""


def _cmd_verify(args: argparse.Namespace) -> int:
    """Run 16 verifier assertions for the given study.

    Exit codes mirror the assertion failure modes:
        EXIT_OK (0)                     — all assertions passed
        EXIT_MANIFEST_MISMATCH (2)      — manifest assertions 1, 2, or 10
        EXIT_LEDGER_HASH_NULL (3)       — ledger/sentinel assertions 5, 6
        EXIT_QUARANTINE_NON_EMPTY (4)   — assertion 7
        EXIT_VERIFIER_FAIL (5)          — PHI/determinism assertions 8, 9
        EXIT_NEEDS_ADVICE (6)           — assertion 11 (lock file present)
        EXIT_DESTRUCTION_INCOMPLETE (7) — assertions 3, 4
        EXIT_DECISION_MISMATCH (9)      — assertion 12 (decided vs applied)
    """
    import config  # lazy — keeps module testable without full config bootstrap

    study = args.study
    run_id_arg: str | None = getattr(args, "run_id", None)

    study_output_dir = Path(config.OUTPUT_DIR) / study
    study_raw_dir = Path(config.RAW_DATA_DIR) / study
    datasets_dir = study_raw_dir / "datasets"
    staging_dir = Path(config.TMP_DIR) / study
    tmp_dir = Path(config.TMP_DIR)
    phi_scrub_config_path = Path(config.PHI_SCRUB_CONFIG_PATH)

    # ── Resolve run_id ──────────────────────────────────────────────────────
    run_id, resolve_err = _resolve_run_id(study_output_dir, run_id_arg)
    if run_id is None:
        print(f"FAIL [resolve_run_id]: {resolve_err}", file=sys.stderr)
        return EXIT_NEEDS_ADVICE

    run_dir = study_output_dir / "runs" / run_id
    llm_source_dir = study_output_dir / "llm_source"
    dataset_files_dir = llm_source_dir / "dataset_schema" / "files"
    audit_dir = study_output_dir / "audit"
    manifest_path = config.study_config_path("_forms_manifest.yaml", study=study_raw_dir.name)

    checked_utc = datetime.now(UTC).isoformat()

    # ── Assertion table ─────────────────────────────────────────────────────
    # Each entry: (n, name, callable, failure_exit_code)
    _assertion_table: list[tuple[int, str, Any, int]] = [
        (
            1,
            "forms_manifest_exists_parses",
            lambda: _verify_assertion_1_manifest_exists_parses(study_raw_dir),
            EXIT_MANIFEST_MISMATCH,
        ),
        (
            2,
            "manifest_reconciles_with_dir",
            lambda: _verify_assertion_2_manifest_reconciles(datasets_dir),
            EXIT_MANIFEST_MISMATCH,
        ),
        (
            3,
            "staging_dir_absent",
            lambda: _verify_assertion_3_staging_absent(staging_dir),
            EXIT_DESTRUCTION_INCOMPLETE,
        ),
        (
            4,
            "destruction_attestation_valid",
            lambda: _verify_assertion_4_attestation_valid(run_dir),
            EXIT_DESTRUCTION_INCOMPLETE,
        ),
        (
            5,
            "ledger_hashes_valid",
            lambda: _verify_assertion_5_ledger_hashes(audit_dir, phi_scrub_config_path),
            EXIT_LEDGER_HASH_NULL,
        ),
        (
            6,
            "no_llm_zone_sentinel_present",
            lambda: _verify_assertion_6_no_llm_zone(audit_dir),
            EXIT_LEDGER_HASH_NULL,
        ),
        (
            7,
            "no_quarantine_dir",
            lambda: _verify_assertion_7_no_quarantine(tmp_dir, study, study_output_dir),
            EXIT_QUARANTINE_NON_EMPTY,
        ),
        (
            8,
            "llm_source_phi_absence",
            lambda: _verify_assertion_8_phi_absence(dataset_files_dir),
            EXIT_VERIFIER_FAIL,
        ),
        (
            9,
            "llm_source_no_runtime_keys",
            lambda: _verify_assertion_9_no_runtime_keys(dataset_files_dir),
            EXIT_VERIFIER_FAIL,
        ),
        (
            10,
            "required_forms_have_jsonl",
            lambda: _verify_assertion_10_required_jsonls_present(
                manifest_path, llm_source_dir, run_dir
            ),
            EXIT_MANIFEST_MISMATCH,
        ),
        (
            11,
            "pipeline_lock_absent",
            lambda: _verify_assertion_11_no_pipeline_lock(tmp_dir, study),
            EXIT_NEEDS_ADVICE,
        ),
        (
            12,
            "decided_action_matches_applied",
            lambda: _verify_assertion_decided_vs_applied(
                audit_dir, run_dir, dataset_files_dir, study
            ),
            EXIT_DECISION_MISMATCH,
        ),
        # Assertion 14 runs BEFORE 13 by list position: 13 (status write) must
        # stay terminal. The numeric label is 14; execution order is 12→14→13.
        (
            14,
            "ledger_covers_all_columns",
            lambda: _verify_assertion_14_audit_coverage(
                audit_dir, dataset_files_dir, run_dir, study
            ),
            EXIT_AUDIT_COVERAGE_INCOMPLETE,
        ),
        (
            15,
            "sot_joined_view_present",
            lambda: _verify_assertion_15_sot_joined_view_present(
                llm_source_dir, dataset_files_dir, study=study, repo_root=Path(config.BASE_DIR)
            ),
            EXIT_VERIFIER_FAIL,
        ),
        (
            16,
            "ledger_entry_fields_complete",
            lambda: _verify_assertion_16_ledger_fields_complete(audit_dir),
            EXIT_AUDIT_COVERAGE_INCOMPLETE,
        ),
        (
            17,
            "cap_application_complete",
            lambda: _verify_assertion_17_cap_application_complete(dataset_files_dir, study),
            EXIT_VERIFIER_FAIL,
        ),
        (
            13,
            "status_json_updated",
            lambda: _assertion_13_update_status(run_dir),
            EXIT_VERIFIER_FAIL,  # exit code unused for assertion 13 (always last)
        ),
    ]

    results: list[dict[str, Any]] = []
    overall_exit_code: int = EXIT_OK
    failed_at: int | None = None

    for n, name, fn, fail_exit in _assertion_table:
        if failed_at is not None:
            results.append({"n": n, "name": name, "result": "skipped", "detail": ""})
            continue
        try:
            result, detail = fn()
        except Exception as exc:
            result = "fail"
            detail = f"assertion raised unexpected exception: {exc}"

        results.append({"n": n, "name": name, "result": result, "detail": detail})

        if result == "fail":
            failed_at = n
            overall_exit_code = fail_exit
            print(
                f"FAIL [assertion {n} — {name}]: {detail}",
                file=sys.stderr,
            )
        else:
            print(f"PASS [assertion {n} — {name}]")

    overall = "pass" if failed_at is None else "fail"

    report_payload: dict[str, Any] = {
        "run_id": run_id,
        "study": study,
        "checked_utc": checked_utc,
        "assertions": results,
        "overall": overall,
        "exit_code": overall_exit_code,
    }

    run_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(run_dir / "verifier_report.json", report_payload)

    # N22: a blocking verifier failure deposits a count-only note into the
    # consolidated human-review queue (keyed by run — the verifier is a run-level
    # gate, like the publish gate). Assertion details are already value-free
    # (assertion id + form/column + counts, never a row value).
    if overall == "fail":
        try:
            from scripts.audit.review_paths import verifier_review_path

            failed = [r for r in results if r.get("result") == "fail"]
            note_lines = [
                "# Audit verifier — human review required",
                "",
                f"**Run:** {run_id}  ·  **Failed assertion(s):** {len(failed)}",
                "",
                "## Failed assertions (ids + value-free detail)",
                *[f"- [{r['n']}] {r['name']}: {r['detail']}" for r in failed],
                "",
                "Fix the named ledger/config/column, then re-run the verifier.",
            ]
            note_path = verifier_review_path(audit_dir, run_id)
            note_path.parent.mkdir(parents=True, exist_ok=True)
            note_path.write_text("\n".join(note_lines) + "\n", encoding="utf-8")
        except Exception as exc:  # best-effort; verifier_report.json is the record
            print(f"verifier review note write skipped: {type(exc).__name__}", file=sys.stderr)

    return overall_exit_code


# ---------------------------------------------------------------------------
# Subcommand: run
# ---------------------------------------------------------------------------


class _SkillInterrupted(BaseException):
    """Raised by SIGINT/SIGTERM handler to unwind the run subcommand cleanly."""


@dataclass(frozen=True)
class FormGateResult:
    """Result of the header-only PHI handling approval gate."""

    approved_forms: tuple[str, ...]
    held_forms: tuple[str, ...]
    approval_report_path: Path | None
    partial: bool


def _install_signal_handlers() -> None:
    """Install SIGINT and SIGTERM handlers that raise _SkillInterrupted.

    Restores the default SIGINT handler before raising so a subsequent
    Ctrl-C during cleanup cannot be silently swallowed by a nested handler.
    """

    def _handler(signum: int, _frame: object) -> None:
        # Restore default so a second interrupt during cleanup propagates.
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        raise _SkillInterrupted(f"interrupted by signal {signum}")

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def _restore_default_signal_handlers() -> None:
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write *payload* to *path* atomically using a sibling .tmp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    serialised = json.dumps(payload, indent=2, sort_keys=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}_",
        suffix=".tmp",
    )
    try:
        try:
            os.write(tmp_fd, serialised.encode("utf-8"))
            os.fsync(tmp_fd)
        finally:
            os.close(tmp_fd)
        Path(tmp_name).replace(path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _write_run_status(
    *,
    run_dir: Path,
    run_id: str,
    study: str,
    exit_code: int,
    started_utc: str,
    failed_stage: str | None = None,
    reason: str | None = None,
    staging_preserved: bool | None = None,
    verifier_passed: bool | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "run_id": run_id,
        "study": study,
        "exit_code": exit_code,
        "started_utc": started_utc,
        "completed_utc": datetime.now(UTC).isoformat(),
        "failed_stage": failed_stage,
        "reason": reason,
        "staging_preserved": staging_preserved,
        "verifier_passed": verifier_passed,
    }
    if extra:
        payload.update(extra)
    _atomic_write_json(run_dir / "status.json", payload)


def _auto_worker_count(max_workers: int | None) -> int:
    if max_workers is not None:
        return max(1, max_workers)
    cpu = os.cpu_count() or 1
    return max(1, min(cpu, 8))


def _manifest_review_forms(
    manifest_path: Path,
    *,
    datasets_dir: Path,
    selected_forms: tuple[str, ...] = (),
) -> list[str]:
    with manifest_path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    required = raw.get("required") or []
    optional = raw.get("optional") or []
    if not isinstance(required, list):
        raise ValueError(f"{manifest_path} required: must be a list")
    if not isinstance(optional, list):
        raise ValueError(f"{manifest_path} optional: must be a list")

    declared = [str(item) for item in (*required, *optional)]
    declared_set = set(declared)

    def _normalize_selected(name: str) -> str:
        candidate = str(name)
        if candidate in declared_set:
            return candidate
        if Path(candidate).suffix:
            raise ValueError(f"selected form is not declared in manifest: {candidate}")
        for suffix in (".xlsx", ".csv"):
            suffixed = f"{candidate}{suffix}"
            if suffixed in declared_set:
                return suffixed
        raise ValueError(f"selected form is not declared in manifest: {candidate}")

    if selected_forms:
        review_forms = [_normalize_selected(item) for item in selected_forms]
    else:
        review_forms = [str(item) for item in required]
        review_forms.extend(str(item) for item in optional if (datasets_dir / str(item)).exists())

    missing = [name for name in review_forms if not (datasets_dir / name).is_file()]
    if missing:
        raise ValueError(f"selected form file(s) missing: {', '.join(missing)}")

    return list(dict.fromkeys(review_forms))


def _apply_cross_form_consistency(
    approvals: list[Any],
) -> tuple[list[Any], dict[str, Any]]:
    """Cross-form PHI-classification consistency barrier (Note 8, Break 3).

    A column NAME kept raw in one form but pseudonymized/dropped in another lets
    an attacker de-anonymize the protected form by cross-referencing the raw one.
    Require each column to carry the SAME protection LEVEL across every form it
    appears in; on an under-protection conflict HOLD ONLY the weaker forms (never
    weaken protection) so the consistent majority still proceeds — the SUBJID
    27-vs-1 example. Comparison is by protection RANK (``_PROTECTION_RANK``), not
    raw action name, so equally-protective but differently named actions (cap vs
    generalize) are not false conflicts. Column NAMES + counts only — no row
    values ever touch this barrier.

    Returns the (possibly rebuilt) approvals list and a value-free audit record
    ``{"conflicts": {form: [columns]}, "checked_columns": n}``.
    """
    from collections import defaultdict

    per_col: dict[str, dict[str, str]] = defaultdict(dict)
    for ap in approvals:
        for col, act in ap.actions.items():
            per_col[col.upper()][ap.form_name] = act
    conflicts: dict[str, list[str]] = defaultdict(list)  # form -> [columns]
    for col, byform in per_col.items():
        ranks = {_PROTECTION_RANK.get(a, 0) for a in byform.values()}
        if len(ranks) <= 1:
            continue  # identical protection level across forms — not a leak risk
        max_rank = max(ranks)
        for form, act in byform.items():
            if _PROTECTION_RANK.get(act, 0) < max_rank:
                conflicts[form].append(col)
    info = {
        "conflicts": {f: sorted(c) for f, c in conflicts.items()},
        "checked_columns": len(per_col),
    }
    if not conflicts:
        return approvals, info

    from dataclasses import replace as dc_replace

    from scripts.security.phi_review import HeldReason

    rebuilt: list[Any] = []
    for ap in approvals:
        cols = sorted(conflicts.get(ap.form_name, ()))
        if cols and ap.status == "approved":
            reason = f"cross_form_action_conflict: columns={cols}"
            held = ap.held_reason or HeldReason(
                what_was_tried="cross-form PHI action consistency check across all forms",
                what_was_ambiguous=(
                    f"{len(cols)} column name(s) classified less protectively here than "
                    "the strictest action peer forms apply to the same column"
                ),
                what_would_resolve=(
                    "reconcile the per-form PHI action for these column names so every "
                    "form applies the same (strictest) protection (header names only)"
                ),
            )
            ap = dc_replace(
                ap,
                status="held",
                reasons=tuple(dict.fromkeys((*ap.reasons, reason))),
                held_reason=held,
            )
        rebuilt.append(ap)
    return rebuilt, info


def _write_scrub_quarantine_note(path: Path, pf: dict[str, Any]) -> None:
    """Write a value-free PHI-scrub quarantine/elevated note (Note 22).

    Counts + reason codes only — never a row value.
    """
    elevated = "  ⚠ ELEVATED" if pf.get("elevated") else ""
    columns = pf.get("columns") or []
    lines = [
        "# PHI scrub — quarantine / review",
        "",
        f"**Form:** {pf['form']}",
        f"**Kept rows:** {pf.get('kept', 0)} · **Quarantined rows:** {pf.get('quarantined', 0)}{elevated}",
        "",
        "## Variables involved (column names only — no row values)",
        *(
            [f"- `{col}`" for col in columns]
            if columns
            else ["- (none — whole-row hold, e.g. missing subject ID)"]
        ),
        "",
        "## Quarantine reason codes (counts only — no row values)",
        *[f"- {reason}" for reason in pf.get("reasons", [])],
        "",
        "Fix the source data or scrub config for the quarantined rows, then re-run "
        "with `--resume-held`.",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_classification_hold_note(path: Path, item: Any) -> None:
    """Write a value-free PHI-classification hold note (Note 22).

    Header NAMES, reason codes, and counts only — never a row value.
    """
    lines = [
        "# PHI classification — human review required",
        "",
        f"**Form:** {item.form_name}",
        f"**Status:** {item.status}",
        "",
        "## Why held (reason codes / header names only — no row values)",
        *[f"- {reason}" for reason in item.reasons],
    ]
    held = getattr(item, "held_reason", None)
    if held is not None:
        lines += [
            "",
            "## Reviewer guidance",
            f"- tried: {held.what_was_tried}",
            f"- ambiguous: {held.what_was_ambiguous}",
            f"- to resolve: {held.what_would_resolve}",
        ]
    force_drop = getattr(item, "force_drop_headers", ())
    if force_drop:
        lines += ["", f"## Force-dropped direct-identifier columns: {len(force_drop)}"]
    lines += ["", "Resolve via config/policy, then re-run with `--resume-held`."]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _llm_key_available() -> bool:
    """True only when the configured cloud provider has a key in the KeyStore.

    Keyless/local/unknown providers and any lookup error → False, so AI alignment
    (default ON) auto-falls-back to deterministic classification wherever an LLM is
    not actually reachable. No values are read; this inspects provider config + the
    role-gated KeyStore only.
    """
    import config

    try:
        from scripts.ai_assistant.keystore import get_keystore, provider_slug_for

        slug = provider_slug_for(config.LLM_PROVIDER)
        return slug is not None and get_keystore().has(slug)
    except Exception:
        return False


def _should_align() -> bool:
    """Whether to construct the LLM aligner: only outside pytest and when an LLM
    key is available. Pure + tiny so the fallback contract is unit-testable."""
    import config

    return not config.is_test_context() and _llm_key_available()


def _run_form_approval_gate(
    *,
    study: str,
    study_raw_dir: Path,
    run_dir: Path,
    max_workers: int | None,
    selected_forms: tuple[str, ...] = (),
) -> FormGateResult:
    """Run header-only PHI handling review before any row values are opened."""
    import config
    from scripts.extraction.header_store import load_header_store, resolve_headers
    from scripts.security.phi_review import (
        load_sot_variable_signals,
        load_study_privacy_config,
        review_form_headers,
        verify_approval_payload,
    )
    from scripts.security.phi_rulebook import resolve_rulebook
    from scripts.security.phi_scrub import load_scrub_config

    privacy_config = load_study_privacy_config(study_raw_dir)
    # Note 6: read column headers from the shared header-extraction store (Phase 1)
    # when present; resolve_headers falls back to a direct row-1 read on a miss.
    _header_store = load_header_store(run_dir)
    resolution = resolve_rulebook(
        privacy_config,
        allow_network=privacy_config.rule_refresh == "online_preferred",
    )
    rule_bundle = resolution.bundle
    datasets_dir = study_raw_dir / "datasets"
    review_forms = _manifest_review_forms(
        config.study_config_path("_forms_manifest.yaml", study=study_raw_dir.name),
        datasets_dir=datasets_dir,
        selected_forms=selected_forms,
    )
    worker_count = _auto_worker_count(max_workers)

    # SoT cross-verification inputs: the SoT (generated FIRST) gives each variable
    # an independent meaning + PHI signal, and the scrub's keep_fields record the
    # deliberate human keep decisions. Both let review_form_headers clear
    # false-positive coverage holds and catch SoT/name-rule disagreements.
    sot_root = Path(config.OUTPUT_DIR) / study / "llm_source" / "SoT"
    # Merged-config load (risk #8): the approval gate's published-raw / keep
    # determination must reflect the SAME deep-merged defaults+per-study config
    # the scrub applies (defaults == merged when no per-study override exists).
    _scrub_cfg = load_scrub_config(study=study)

    # N9: AI header→rule alignment for uncovered headers — default ON, but it only
    # RUNS when an LLM is reachable: flag on, not under pytest, and a provider key
    # is present (entered via the UI/KeyStore). Otherwise _aligner stays None and the
    # gate falls back to deterministic pinned-rules classification — fail-closed, no
    # LLM constructed, byte-identical to the deterministic path.
    _aligner = None
    if config.PHI_ALIGNMENT_ENABLED and _should_align():
        from scripts.security.phi_alignment import LLMHeaderAligner

        _aligner = LLMHeaderAligner()

    approvals: list[Any] = []

    def _review_one(form_name: str) -> Any:
        headers = resolve_headers(_header_store, Path(form_name).stem, datasets_dir / form_name)
        # published_raw: the scrub's configured action is keep → the column reaches
        # llm_source unchanged (a coverage/disagreement hold only matters for these;
        # a dropped/scrubbed column is never leaked). confirmed_keeps: a deliberate
        # documented keep_fields rule (a human keep decision that clears a false
        # coverage hold for a no-PDF column like IC_RATION).
        published_raw = frozenset(
            h for h in headers if _configured_scrub_action(_scrub_cfg, h) == "keep"
        )
        confirmed_keeps = frozenset(
            h for h in headers if _scrub_cfg is not None and _scrub_cfg.field_is_keep(h)
        )
        return review_form_headers(
            form_name=form_name,
            headers=headers,
            privacy_config=privacy_config,
            rule_bundle=rule_bundle,
            sot_signals=load_sot_variable_signals(sot_root, form_name),
            published_raw_headers=published_raw,
            confirmed_keep_headers=confirmed_keeps,
            aligner=_aligner,
        )

    if review_forms:
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = {pool.submit(_review_one, form): form for form in review_forms}
            approvals.extend(future.result() for future in as_completed(futures))

    approvals = sorted(approvals, key=lambda item: item.form_name)

    # Cross-form PHI-classification consistency barrier (Note 8, Break 3): hold
    # only forms that under-protect a column relative to its strictest peer.
    approvals, cross_form_info = _apply_cross_form_consistency(approvals)

    approved_forms = tuple(item.form_name for item in approvals if item.status == "approved")
    held_forms = tuple(item.form_name for item in approvals if item.status != "approved")

    payload = {
        "run_id": run_dir.name,
        "study": study,
        "created_utc": datetime.now(UTC).isoformat(),
        "jurisdictions": list(privacy_config.jurisdictions),
        "conflict_policy": privacy_config.conflict_policy,
        "rule_bundle": rule_bundle.to_json(),
        "worker_count": worker_count,
        "forms": [item.to_json() for item in approvals],
        "approved_forms": list(approved_forms),
        "held_forms": list(held_forms),
        "cross_form_consistency": cross_form_info,
        "status": "partial" if held_forms else "approved",
    }
    verify_approval_payload(payload)
    report_path = run_dir / "phi_handling_approval.json"
    _atomic_write_json(report_path, payload)

    # N9: freeze the AI-aligned rules into the run-scoped scrub overlay so the
    # deterministic run_scrub engine applies them and the snapshot captures them.
    aligned_all: list[dict[str, Any]] = []
    for item in approvals:
        aligned_all.extend(getattr(item, "aligned_rules", ()) or ())
    if aligned_all:
        from scripts.security.phi_scrub import write_generated_scrub_overlay

        write_generated_scrub_overlay(aligned_all, run_dir=run_dir, study=study)

    # N22: a PHI-classification hold deposits a count-only note into the per-form
    # human_review queue (not only into phi_handling_approval.json) so a reviewer
    # finds it in the single consolidated human_review/{form}/ location.
    if held_forms:
        from scripts.audit.review_paths import classification_review_path

        audit_dir = run_dir.parents[1] / "audit"
        for item in approvals:
            if item.status != "approved":
                # Key by the bare form stem so every producer's note for a form
                # colocates in one human_review/{stem}/ dir (Note 22).
                _write_classification_hold_note(
                    classification_review_path(audit_dir, Path(item.form_name).stem), item
                )

    return FormGateResult(
        approved_forms=approved_forms,
        held_forms=held_forms,
        approval_report_path=report_path,
        partial=bool(held_forms),
    )


def _acquire_pipeline_lock_for_skill(study: str) -> None:
    """Acquire the pipeline lock via the canonical ``scripts.utils.pipeline_lock``.

    Imports the lock module lazily so the test suite can mock this wrapper.

    Passes *study* explicitly so the lock-file name is keyed on the study
    supplied via ``--study``, not on ``config.STUDY_NAME`` (which may differ
    when the caller runs against a different study than the one auto-detected
    at import time).

    Raises RuntimeError (from ``acquire_pipeline_lock``) if the lock is
    already held by another process.

    Wave 6 note: this previously delegated to ``main._acquire_pipeline_lock``;
    after the thin-main cutover the lock primitives live only in
    ``scripts.utils.pipeline_lock`` (which the engine and this wrapper share).
    """
    from scripts.utils.pipeline_lock import acquire_pipeline_lock

    acquire_pipeline_lock(study)


def _release_pipeline_lock_for_skill() -> None:
    """Release the pipeline lock via ``scripts.utils.pipeline_lock``."""
    from scripts.utils.pipeline_lock import release_pipeline_lock

    release_pipeline_lock()


def _try_commit_snapshot(
    *,
    study: str,
    run_id: str,
    run_dir: Path,
    resume_held: bool = False,
    human_review_records: list | None = None,
    cleanup_verifier_passed: bool | None = None,
) -> str | None:
    """Thin wrapper around the shared committer (Note 13).

    Retained as the supervisor's Step-7 call site so existing tests that patch
    this symbol keep working; the orchestrator P10 calls
    ``scripts.utils.snapshot.commit_run_snapshot`` directly. A standalone run
    (defer flag unset) calls this only after its inline cleanup verifier passes.
    """
    from scripts.utils.snapshot import commit_run_snapshot

    return commit_run_snapshot(
        study=study,
        run_id=run_id,
        run_dir=run_dir,
        resume_held=resume_held,
        human_review_records=human_review_records,
        cleanup_verifier_passed=cleanup_verifier_passed,
    )


def _verify_cleanup_before_inline_snapshot(*, study: str, run_dir: Path) -> bool:
    """Run the standalone cleanup verifier before an inline snapshot commit.

    The orchestrator performs this as P8 before P10. Standalone
    ``dataset-to-llm-source run`` has no orchestrator P8, so it must run the same
    ledger + workspace checks here and persist the same names-only audit record.
    """
    from dataclasses import asdict

    import config
    from scripts.extraction.header_store import destroy_header_store
    from scripts.extraction.io import atomic_write_json
    from scripts.utils.cleanup_verifier import verify_cleanup, verify_workspace_cleanup

    # Note 6: header store is consumed by classification inside the publish leg;
    # destroy before the must-gone walk (orchestrator P7 does the same post-publish).
    destroy_header_store(run_dir)

    ledger_report = verify_cleanup(Path(config.STUDY_AUDIT_DIR), Path(config.TRIO_DATASETS_DIR))
    ws_report = verify_workspace_cleanup(study=study, run_dir=run_dir)
    report = {
        "run_id": run_dir.name,
        "ledger_ok": ledger_report.ok,
        "ledger_findings": [asdict(f) for f in ledger_report.findings],
        "workspace_ok": ws_report.ok,
        "workspace_findings": [asdict(f) for f in ws_report.findings],
        "checked_must_gone": ws_report.checked_must_gone,
        "checked_must_remain": ws_report.checked_must_remain,
        "checked_anomaly": ws_report.checked_anomaly,
    }
    atomic_write_json(Path(config.STUDY_AUDIT_DIR) / "cleanup_verification_report.json", report)
    return ledger_report.ok and ws_report.ok


def _cmd_run(args: argparse.Namespace) -> int:
    """Drive the trusted host publish path for the dataset child skill.

    Steps
    -----
    1. Pre-flight checks (run_id, in-progress token, lock, manifest).
    2. Install SIGINT/SIGTERM handlers.
    3. Invoke ``python -m scripts.pipeline.host_pipeline --pipeline`` in a
       subprocess with ``STUDY_NAME`` set.
    4. Post-run gates (ledger hashes, quarantine).
    5. Destruction (destroy_staging_and_attest).
    6. Write status.json.
    7. If --resume-held and terminal state is fully clean: commit snapshot.
    8. Exit EXIT_OK.

    --resume-held flag
    ------------------
    ``--resume-held`` resumes after a maintainer has resolved the held forms of
    a prior partial run.  It re-processes the FULL surviving form set (prior
    ``approved_forms`` | ``held_forms``) — NOT only the held forms — because
    promotion (``host_pipeline._publish_leg``) is a whole-leg atomic replace:
    publishing only the held subset would securely delete every previously
    approved form from ``llm_source/``.  Re-processing the union reproduces every
    surviving form; the now-resolved held forms are re-reviewed and, on a fully
    clean pass (no held forms left + verifier OK), the run is snapshotted.  This
    is a CLI/maintainer-only path: the flag is refused when
    ``REPORTAL_PROCESS_ROLE=llm-agent``.  It requires a prior run that actually
    had held forms (otherwise use a plain ``run``).
    """
    import config  # lazy — avoids import at module level for testability
    from scripts.utils.run_context import (
        SCRUB_RECOVERY_MESSAGE,
        resolve_run_id,
        scan_for_in_progress_scrubs,
    )

    study = args.study
    resume_held: bool = bool(getattr(args, "resume_held", False))

    # ── Guard: refuse --resume-held from within the LLM agent process ────────
    if resume_held and os.environ.get("REPORTAL_PROCESS_ROLE") == "llm-agent":
        msg = (
            "--resume-held is a CLI/maintainer-only operation and must not be "
            "triggered from the LLM agent process role. "
            "Refusing (REPORTAL_PROCESS_ROLE=llm-agent)."
        )
        print(msg, file=sys.stderr)
        # Do not write a status.json — there is no run_dir yet at this point.
        return EXIT_NEEDS_ADVICE

    started_utc = datetime.now(UTC).isoformat()

    # Derive all study-scoped paths from the explicit --study argument so that
    # the correct paths are used even when config.STUDY_NAME (resolved at
    # import time) refers to a different study.
    study_output_dir = Path(config.OUTPUT_DIR) / study
    study_staging_dir = Path(config.TMP_DIR) / study
    study_datasets_dir = Path(config.RAW_DATA_DIR) / study / "datasets"

    # ── Step 1a: resolve run_id ────────────────────────────────────────────
    # For --resume-held: locate the most-recent partial/held prior run and
    # extract its held_forms BEFORE minting the new run_id.  This must happen
    # early so any error here exits before any lock is acquired.
    resume_surviving_forms: tuple[str, ...] = ()
    if resume_held:
        prior_run_id, resolve_err = _resolve_run_id(study_output_dir, None)
        if prior_run_id is None:
            msg = f"--resume-held: no prior terminal run found: {resolve_err}"
            print(msg, file=sys.stderr)
            return EXIT_NEEDS_ADVICE
        # The prior run's status.json records only COUNTS
        # (approved_forms_count / held_forms_count), never the form-name lists.
        # The authoritative per-form lists live in the prior run's
        # phi_handling_approval.json (written by _run_form_approval_gate); the
        # status.json even records its location as "approval_report_path". Read
        # the lists from there so --resume-held works against the real on-disk
        # shape rather than from list keys that status.json never writes.
        prior_run_dir = study_output_dir / "runs" / prior_run_id
        prior_approval_path = prior_run_dir / "phi_handling_approval.json"
        try:
            prior_approval = json.loads(prior_approval_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            msg = (
                "--resume-held: could not read prior approval report "
                f"({prior_approval_path}): {exc}"
            )
            print(msg, file=sys.stderr)
            return EXIT_NEEDS_ADVICE
        prior_held: list[str] = [str(f) for f in prior_approval.get("held_forms", [])]
        if not prior_held:
            msg = (
                f"--resume-held: prior run {prior_run_id!r} has no held forms; "
                "nothing to resume (use a plain `run` to re-publish all forms)."
            )
            print(msg, file=sys.stderr)
            return EXIT_NEEDS_ADVICE
        prior_approved: list[str] = [str(f) for f in prior_approval.get("approved_forms", [])]
        # Re-process the FULL surviving set (prior approved | held), NOT only the
        # held forms. Promotion (host_pipeline._publish_leg) is a whole-leg atomic
        # replace, so publishing only the held subset would securely DELETE every
        # previously-approved form from llm_source/. Passing the union lets the
        # whole-leg rebuild reproduce every surviving form; the gate re-reviews
        # the now-resolved held forms and a fully-clean pass is snapshotted.
        resume_surviving_forms = tuple(sorted(set(prior_approved) | set(prior_held)))
        print(
            f"--resume-held: re-processing {len(prior_held)} resolved held form(s) "
            f"within the full surviving set of {len(resume_surviving_forms)} form(s) "
            f"from prior run {prior_run_id!r}: {list(resume_surviving_forms)}",
        )

    run_id = resolve_run_id()
    run_dir = study_output_dir / "runs" / run_id

    def _finish(
        code: int,
        *,
        stage: str | None = None,
        reason: str | None = None,
        staging_preserved: bool | None = None,
        extra: dict[str, Any] | None = None,
    ) -> int:
        _write_run_status(
            run_dir=run_dir,
            run_id=run_id,
            study=study,
            exit_code=code,
            started_utc=started_utc,
            failed_stage=stage,
            reason=reason,
            staging_preserved=staging_preserved,
            extra=extra,
        )
        return code

    # ── Step 1a.1: fail closed on disabled-scrub bypass ────────────────────
    # Key on PRESENCE, not truthiness: the variable's mere presence signals a
    # weakened scrub invocation, so even REPORTALIN_ALLOW_DISABLED_SCRUB="" (an
    # empty but present value) must hard-fail here. (The value-gated, test-context
    # floor lives separately in phi_scrub.run_scrub.)
    if "REPORTALIN_ALLOW_DISABLED_SCRUB" in os.environ:
        msg = "REPORTALIN_ALLOW_DISABLED_SCRUB is forbidden for extract_to_llm_source"
        print(msg, file=sys.stderr)
        return _finish(
            EXIT_NEEDS_ADVICE,
            stage="preflight.disabled_scrub",
            reason=msg,
            staging_preserved=False,
        )

    # ── Step 1b: scan for in-progress scrubs ──────────────────────────────
    study_runs_dir = study_output_dir / "runs"
    in_progress = scan_for_in_progress_scrubs(study_runs_dir)
    if in_progress:
        print(
            SCRUB_RECOVERY_MESSAGE.format(path=in_progress[0]),
            file=sys.stderr,
        )
        return _finish(
            EXIT_NEEDS_ADVICE,
            stage="preflight.scrub_recovery",
            reason=f"in-progress token found: {in_progress[0]}",
            staging_preserved=True,
        )

    # ── Step 1c: acquire pipeline lock ────────────────────────────────────
    try:
        _acquire_pipeline_lock_for_skill(study)
    except RuntimeError as exc:
        print(f"Lock error: {exc}", file=sys.stderr)
        return _finish(
            EXIT_NEEDS_ADVICE,
            stage="preflight.lock",
            reason=str(exc),
            staging_preserved=True,
        )

    lock_held = True
    final_code = EXIT_OK

    # ── Steps 1d + 2 are inside the try so the lock is always released ────
    # (Minor-1 fix: manifest check is inside the try/finally so a raise there
    # cannot leak the lock.  Minor-2 fix: signal handlers are installed as the
    # first step inside the try so a SIGINT in that window is caught by the
    # _SkillInterrupted handler rather than bypassing the finally clause.)
    try:
        # ── Step 2: install signal handlers (first step inside try) ───────
        _install_signal_handlers()

        # ── Step 1d: validate forms manifest ──────────────────────────────
        try:
            check_forms_manifest(study_datasets_dir)
        except ManifestMismatchError as exc:
            print(f"Manifest mismatch: {exc}", file=sys.stderr)
            return _finish(
                EXIT_MANIFEST_MISMATCH,
                stage="preflight.manifest",
                reason=str(exc),
                staging_preserved=False,
            )

        # ── Step 1e: header-only PHI handling approval gate ───────────────
        # For --resume-held: re-review the FULL surviving set (prior approved |
        # held). The gate re-approves the now-resolved held forms and re-approves
        # the prior-approved forms, so REPORTAL_ALLOWED_DATASET_FORMS — and thus
        # the whole-leg republish — covers every surviving form. Passing only the
        # held subset here would let the whole-leg replace delete approved forms.
        if resume_held:
            gate_selected_forms = resume_surviving_forms
        else:
            gate_selected_forms = tuple(getattr(args, "forms", None) or ())
        try:
            form_gate = _run_form_approval_gate(
                study=study,
                study_raw_dir=Path(config.RAW_DATA_DIR) / study,
                run_dir=run_dir,
                max_workers=getattr(args, "max_workers", None),
                selected_forms=gate_selected_forms,
            )
        except Exception as exc:
            msg = f"PHI form approval gate failed: {exc}"
            print(msg, file=sys.stderr)
            return _finish(
                EXIT_NEEDS_ADVICE,
                stage="preflight.form_approval",
                reason=msg,
                staging_preserved=False,
            )

        if form_gate.held_forms and not form_gate.approved_forms:
            msg = "all forms held for human PHI handling review"
            print(msg, file=sys.stderr)
            return _finish(
                EXIT_NEEDS_ADVICE,
                stage="preflight.form_approval",
                reason=msg,
                staging_preserved=False,
                extra={
                    "publish_status": "held",
                    "approved_forms_count": 0,
                    "held_forms": sorted(form_gate.held_forms),
                    "held_forms_count": len(form_gate.held_forms),
                    "approval_report_path": str(form_gate.approval_report_path)
                    if form_gate.approval_report_path
                    else None,
                },
            )

        # ── Step 3: subprocess invocation of the host publish engine ──────
        env = dict(os.environ)
        # Defensive: the bypass env var was already refused above.
        env.pop("REPORTALIN_ALLOW_DISABLED_SCRUB", None)
        # Propagate run_id so all sub-processes share the same run identifier.
        env["REPORTAL_RUN_ID"] = run_id
        if form_gate.approved_forms or form_gate.held_forms:
            env["REPORTAL_ALLOWED_DATASET_FORMS"] = ",".join(form_gate.approved_forms)

        # The engine resolves study from STUDY_NAME env var (it has no --study flag).
        env["STUDY_NAME"] = study
        # The wrapper already holds the pipeline lock (acquired above); signal
        # the subprocess so the engine's _acquire_pipeline_lock skips re-acquisition
        # rather than racing itself on the same fcntl flock. We pass our PID so
        # the engine can VALIDATE the baton (live parent == os.getppid()) instead of
        # honoring an inherited/stale env var unconditionally — otherwise a leaked
        # REPORTAL_PIPELINE_LOCK_HELD_BY_PARENT would silently disable the lock for
        # an unrelated direct engine run (GAP-3).
        env["REPORTAL_PIPELINE_LOCK_HELD_BY_PARENT"] = "1"
        env["REPORTAL_PIPELINE_LOCK_PARENT_PID"] = str(os.getpid())
        # Wave 6 cutover: the host publish path moved out of the repo-root main.py
        # into scripts.pipeline.host_pipeline. Invoke it as a module
        # (``python -m scripts.pipeline.host_pipeline --pipeline``) from the repo
        # root so the scripts.* meta_path shim and config resolution work exactly
        # as they did for the old ``main.py --pipeline`` subprocess.
        import config as _config

        repo_root = Path(_config.BASE_DIR)
        # stdout/stderr are not captured here; the engine installs its own PHI log
        # redactor at startup. If that install fails non-fatally (non-production
        # mode), raw log lines bypass this process's redactor and go directly to
        # the terminal/log. In production mode, the engine exits non-zero on redactor
        # failure, which is caught below.
        cmd = [sys.executable, "-m", "scripts.pipeline.host_pipeline", "--pipeline"]
        if getattr(args, "confirm_rotation", False):
            cmd.append("--confirm-rotation")
        result = subprocess.run(
            cmd,
            cwd=str(repo_root),
            env=env,
            check=False,
        )
        if result.returncode != 0:
            print(
                f"Pipeline subprocess exited with code {result.returncode}; "
                "staging preserved for inspection.",
                file=sys.stderr,
            )
            return _finish(
                EXIT_NEEDS_ADVICE,
                stage="pipeline.subprocess",
                reason=f"subprocess exited with code {result.returncode}",
                staging_preserved=True,
            )

        # ── Step 3.5: read scrub_outcome.json (best-effort) ──────────────────
        # the engine writes output/{STUDY}/runs/{run_id}/scrub_outcome.json when
        # partial_on_review=True.  If the sidecar reports partial=True we surface
        # EXIT_PARTIAL_REVIEW (8) — the rows themselves were still published, but
        # some were quarantined inside the form's published JSONL.  The sidecar
        # carries counts + reason strings only, never row values.
        # Absent / unreadable / run_id-mismatched sidecar → treat as clean (no error).
        _scrub_partial_forms: list[dict[str, Any]] = []
        _scrub_partial: bool = False
        _scrub_outcome_path = study_output_dir / "runs" / run_id / "scrub_outcome.json"
        try:
            if _scrub_outcome_path.is_file():
                _scrub_raw = json.loads(_scrub_outcome_path.read_text(encoding="utf-8"))
                # Guard: sidecar must belong to this run (run_id + study match).
                if (
                    isinstance(_scrub_raw, dict)
                    and _scrub_raw.get("run_id") == run_id
                    and _scrub_raw.get("study") == study
                    and _scrub_raw.get("partial") is True
                ):
                    _scrub_partial = True
                    for _form_name, _counts in (_scrub_raw.get("partial_forms") or {}).items():
                        if not isinstance(_counts, dict):
                            continue
                        _scrub_partial_forms.append(
                            {
                                "form": str(_form_name),
                                "kept": int(_counts.get("kept", 0)),
                                "quarantined": int(_counts.get("quarantined", 0)),
                                "reasons": [str(r) for r in (_counts.get("reasons") or [])],
                                "elevated": bool(_counts.get("elevated", False)),
                                "columns": [str(c) for c in (_counts.get("columns") or [])],
                            }
                        )
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            # Best-effort: a corrupt or missing sidecar is treated as clean.
            _scrub_partial = False
            _scrub_partial_forms = []

        # N22: every form with quarantined rows (or flagged 'elevated') gets a
        # count-only note in the per-form human_review queue, so the scrub hold is
        # found in human_review/{form}/ — not only in scrub_outcome.json.
        if _scrub_partial_forms:
            from scripts.audit.review_paths import scrub_quarantine_review_path

            _scrub_audit_dir = study_output_dir / "audit"
            for _pf in _scrub_partial_forms:
                # Key by the bare form stem (strip .jsonl) so the scrub note
                # colocates with the form's other producer notes (Note 22).
                _write_scrub_quarantine_note(
                    scrub_quarantine_review_path(_scrub_audit_dir, Path(_pf["form"]).stem), _pf
                )

        # ── Step 3.55: read sot_joined_gate_outcome.json (best-effort) ─────
        # The engine holds forms lacking a SoT joined query view before publish
        # and records the held form NAMES here (counts only — never row values).
        _sot_joined_held: list[str] = []
        _sot_joined_path = study_output_dir / "runs" / run_id / "sot_joined_gate_outcome.json"
        try:
            if _sot_joined_path.is_file():
                _sot_raw = json.loads(_sot_joined_path.read_text(encoding="utf-8"))
                if (
                    isinstance(_sot_raw, dict)
                    and _sot_raw.get("run_id") == run_id
                    and _sot_raw.get("study") == study
                    and _sot_raw.get("held") is True
                ):
                    _sot_joined_held = [str(f) for f in (_sot_raw.get("held_forms") or [])]
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            _sot_joined_held = []
        if _sot_joined_held:
            _hold_run_for_review(run_dir, _sot_joined_held)

        # ── Step 4a: assert per-dataset PHI ledger hashes are non-null ───────
        ledger_paths = iter_dataset_phi_ledger_paths(study_output_dir / "audit")
        if not ledger_paths:
            msg = "No per-dataset PHI as-written ledgers found."
            print(msg, file=sys.stderr)
            return _finish(
                EXIT_LEDGER_HASH_NULL,
                stage="postrun.ledger",
                reason=msg,
                staging_preserved=True,
            )

        for ledger_path in ledger_paths:
            try:
                ledger_data = json.loads(ledger_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError) as exc:
                print(f"Ledger read error: {exc}", file=sys.stderr)
                return _finish(
                    EXIT_LEDGER_HASH_NULL,
                    stage="postrun.ledger",
                    reason=str(exc),
                    staging_preserved=True,
                )

            if not ledger_data.get("scrub_config_hash") or not ledger_data.get(
                "input_dataset_hash"
            ):
                print(
                    f"Ledger hash null in {ledger_path}: "
                    "scrub_config_hash or input_dataset_hash is absent/null.",
                    file=sys.stderr,
                )
                return _finish(
                    EXIT_LEDGER_HASH_NULL,
                    stage="postrun.ledger",
                    reason=f"{ledger_path}: scrub_config_hash or input_dataset_hash is absent/null",
                    staging_preserved=True,
                )

        # ── Step 4b: assert quarantine is empty or absent ─────────────────
        # In partial-publish mode the scrub leg INTENTIONALLY quarantines the
        # un-scrubbable rows it could not jitter/map, recording them in
        # scrub_outcome.json (``_scrub_partial``). A non-empty quarantine is the
        # EXPECTED steady state there — not an error — and it is still securely
        # destroyed in Step 5 below (so no PHI persists) before the verifier's
        # assertion 7 re-checks emptiness post-destruction. Only an UNEXPECTED
        # non-empty quarantine (strict mode, or with no partial sidecar) is a hard
        # fail. Fail-closed bias preserved: when in doubt (no sidecar) → exit 4.
        quarantine_dir = study_staging_dir / "quarantine"
        if quarantine_dir.is_dir() and any(quarantine_dir.iterdir()) and not _scrub_partial:
            print(
                f"Quarantine non-empty: {quarantine_dir}",
                file=sys.stderr,
            )
            return _finish(
                EXIT_QUARANTINE_NON_EMPTY,
                stage="postrun.quarantine",
                reason=f"quarantine non-empty: {quarantine_dir}",
                staging_preserved=True,
            )

        # ── Step 5: destruction ───────────────────────────────────────────
        try:
            attest_path = destroy_staging_and_attest(
                study=study,
                run_id=run_id,
                staging_dir=study_staging_dir,
                output_dir=study_output_dir,
            )
        except DestructionIncompleteError as exc:
            print(f"Destruction incomplete: {exc}", file=sys.stderr)
            return _finish(
                EXIT_DESTRUCTION_INCOMPLETE,
                stage="postrun.destruction",
                reason=str(exc),
                staging_preserved=True,
            )

        # ── Step 6: write status.json ─────────────────────────────────────
        # Determine the final exit code.  Form-gate partial (held_forms) already
        # sets EXIT_PARTIAL_REVIEW.  Scrub-leg partial (some rows quarantined
        # inside a published form) also sets EXIT_PARTIAL_REVIEW — but only when
        # the current code is still EXIT_OK, so we never DOWNGRADE a worse code.
        final_code = EXIT_PARTIAL_REVIEW if (form_gate.partial or _sot_joined_held) else EXIT_OK
        if _scrub_partial and final_code == EXIT_OK:
            final_code = EXIT_PARTIAL_REVIEW

        # publish_status: "complete" when everything is clean; "partial" when
        # either the form gate held forms OR the scrub leg quarantined rows.
        _is_partial_run = form_gate.partial or _scrub_partial or bool(_sot_joined_held)
        _all_held_forms = sorted(set(form_gate.held_forms) | set(_sot_joined_held))
        _status_extra: dict[str, Any] = {
            "scope": "HIPAA Safe Harbor + configured study jurisdictions",
            "ledger_hash_present": True,
            "destruction_attestation_path": str(attest_path),
            "publish_status": "partial" if _is_partial_run else "complete",
            "approved_forms_count": len(form_gate.approved_forms),
            "held_forms": _all_held_forms,
            "held_forms_count": len(_all_held_forms),
            "approval_report_path": str(form_gate.approval_report_path)
            if form_gate.approval_report_path
            else None,
        }
        # partial_forms: scrub-leg quarantine summary (counts + reasons per form).
        # This is DISTINCT from held_forms (which are phi-gate-held = not published
        # at all). A partial_form IS published; only some of its rows were
        # quarantined.  Empty list on a fully-clean scrub leg.
        if _scrub_partial_forms:
            _status_extra["partial_forms"] = _scrub_partial_forms
        _finish(
            final_code,
            staging_preserved=False,
            extra=_status_extra,
        )

        # ── Step 7: run verifier + commit snapshot ONLY on a fully-clean pass ──
        # EXIT_OK: no form-gate holds and no scrub-leg quarantine.
        # Scrub-only partial (EXIT_PARTIAL_REVIEW, zero held forms, some rows
        # quarantined): every approved form IS published, but the run is NOT fully
        # clean. A snapshot is an immutable milestone of a clean pass, so a partial
        # run is NOT snapshot-eligible — the published tree stays in llm_source/
        # but is never enshrined as a snapshot. We still run the verifier for its
        # audit value. (commit_run_snapshot also fail-closes on a non-clean run as
        # defense-in-depth.)
        _scrub_only_partial = (
            final_code == EXIT_PARTIAL_REVIEW and not _all_held_forms and _scrub_partial
        )
        if final_code == EXIT_OK or _scrub_only_partial:
            # Build a minimal Namespace that _cmd_verify accepts.
            verify_args = argparse.Namespace(study=study, run_id=run_id)
            verify_exit = _cmd_verify(verify_args)
            if verify_exit == EXIT_OK and final_code != EXIT_OK:
                # Scrub-only-partial run: verifier/audit recorded, but per the
                # clean-pass-only policy NO snapshot is committed.
                print(
                    "Scrub-only-partial publish (rows quarantined): snapshot not "
                    "committed — snapshots mark fully-clean passes only.",
                    file=sys.stderr,
                )
            elif verify_exit == EXIT_OK:
                # Fully clean. Under the orchestrator the commit is DEFERRED to
                # P10 (Note 13) so it happens only after the cleanup + audit
                # verifiers pass; a standalone run commits here.
                _defer = os.environ.get("REPORTAL_DEFER_SNAPSHOT_COMMIT") == "1"
                if _defer:
                    print("Snapshot commit deferred to orchestrator P10.", file=sys.stderr)
                else:
                    if not _verify_cleanup_before_inline_snapshot(study=study, run_dir=run_dir):
                        print(
                            "Standalone cleanup verifier failed; snapshot not committed.",
                            file=sys.stderr,
                        )
                        final_code = EXIT_VERIFIER_FAIL
                        _finish(
                            final_code,
                            stage="cleanup.inline",
                            reason="cleanup verifier failed before snapshot commit",
                            staging_preserved=False,
                            extra=_status_extra,
                        )
                        return final_code
                    _try_commit_snapshot(
                        study=study,
                        run_id=run_id,
                        run_dir=run_dir,
                        resume_held=resume_held,
                        cleanup_verifier_passed=True,
                    )
            else:
                print(
                    f"Inline verifier exited {verify_exit}; snapshot not committed.",
                    file=sys.stderr,
                )
                final_code = verify_exit
                # Re-write status.json so the on-disk exit_code reflects the
                # verifier failure. Step 6 above already wrote exit_code=0 (the
                # publish succeeded); without this re-write the process would
                # return non-zero while status.json on disk falsely claimed
                # exit_code=0. Fail-closed: persisted state must match the
                # returned code.
                _finish(
                    verify_exit,
                    stage="postrun.inline_verify",
                    reason=f"inline verifier exited {verify_exit}",
                    staging_preserved=False,
                    extra={
                        "scope": "HIPAA Safe Harbor + configured study jurisdictions",
                        "ledger_hash_present": True,
                        "destruction_attestation_path": str(attest_path),
                        "publish_status": "complete",
                        "approved_forms_count": len(form_gate.approved_forms),
                        "held_forms_count": len(form_gate.held_forms),
                        "approval_report_path": str(form_gate.approval_report_path)
                        if form_gate.approval_report_path
                        else None,
                    },
                )

    except _SkillInterrupted:
        # SIGINT/SIGTERM — clean up lock; do NOT invoke destruction.
        print(
            "Run interrupted by signal; staging preserved. "
            "Exiting with EXIT_DESTRUCTION_INCOMPLETE.",
            file=sys.stderr,
        )
        lock_held = False
        _restore_default_signal_handlers()
        _release_pipeline_lock_for_skill()
        return _finish(
            EXIT_DESTRUCTION_INCOMPLETE,
            stage="pipeline.interrupted",
            reason="interrupted by signal",
            staging_preserved=True,
        )

    finally:
        _restore_default_signal_handlers()
        if lock_held:
            _release_pipeline_lock_for_skill()
            lock_held = False

    return final_code


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="extract_to_llm_source",
        description=(
            "Dataset child-skill entry point: publish raw workbooks into PHI-clean "
            "llm_source/ via the trusted host path."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # ── run ────────────────────────────────────────────────────────────────
    run_p = sub.add_parser(
        "run",
        help="Run the trusted host publish path for a single study.",
        description="Run raw workbook publish -> PHI scrub -> llm_source/ for one study.",
    )
    run_p.add_argument(
        "--study",
        required=True,
        metavar="STUDY",
        help="Study name matching data/raw/{STUDY}/ (e.g. Indo-VAP)",
    )
    run_p.add_argument(
        "--max-workers",
        type=int,
        default=None,
        metavar="N",
        help="Maximum parallel form-review workers before real-data extraction.",
    )
    run_p.add_argument(
        "--form",
        dest="forms",
        action="append",
        default=None,
        metavar="DATASET",
        help=(
            "Limit this run to one manifest-declared dataset filename or stem. "
            "May be repeated. Omit to process all manifest review forms."
        ),
    )
    run_p.add_argument(
        "--resume-held",
        dest="resume_held",
        action="store_true",
        default=False,
        help=(
            "CLI/maintainer-only: re-process the FULL surviving form set "
            "(prior approved + held) of the most recent partial/held run, so the "
            "whole-leg republish reproduces every surviving form (publishing only "
            "the held subset would delete previously-approved forms on the "
            "whole-leg atomic replace). "
            "Refused when REPORTAL_PROCESS_ROLE=llm-agent. "
            "On a fully-clean pass (no remaining held forms, verifier passes) "
            "commits an immutable snapshot."
        ),
    )
    run_p.add_argument(
        "--confirm-rotation",
        dest="confirm_rotation",
        action="store_true",
        default=False,
        help=(
            "Confirm deliberate PHI HMAC key rotation and pass the confirmation "
            "through to the trusted host publish path."
        ),
    )

    # ── verify ─────────────────────────────────────────────────────────────
    verify_p = sub.add_parser(
        "verify",
        help="Run post-run verifier assertions for a study.",
        description="Verify that a completed run is clean and destruction is attestable.",
    )
    verify_p.add_argument(
        "--study",
        required=True,
        metavar="STUDY",
        help="Study name to verify.",
    )
    verify_p.add_argument(
        "--run",
        dest="run_id",
        default=None,
        metavar="RUN_ID",
        help="Specific run_id to verify (defaults to most recent).",
    )

    # ── status ─────────────────────────────────────────────────────────────
    sub.add_parser(
        "status",
        help="Print skill scope and exit-code contract.",
        description="Print the HIPAA Safe Harbor scope banner and exit 0.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """Argparse entry point.  Returns an integer exit code (does not call sys.exit)."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.subcommand == "run":
            return _cmd_run(args)
        if args.subcommand == "verify":
            return _cmd_verify(args)
        if args.subcommand == "status":
            return _cmd_status(args)
    except Exception as exc:
        print(f"Unexpected error: {exc}", file=sys.stderr)
        return 1

    return 1  # unreachable; satisfies mypy


if __name__ == "__main__":
    sys.exit(main())
