"""Post-cleanup consistency verifier for the dataset-cleanup leg (Wave 3 C4.2).

The dataset-cleanup leg (``emit_dataset_cleanup_audit_envelope`` + the upstream
``dedup`` column drops) records extraction-time column drops in the per-dataset
``dataset_cleanup_ledger.as_written.json``. Raw-file dedup runs earlier in
orchestrator phase 2. This module independently checks that what the ledger
*says* happened matches what is actually in the published tree — the audit
analogue of "trust, but verify".

Three phases
------------
* **must-gone** — every file/column the ledger recorded as removed must be
  absent from the published datasets tree (a junk file still present, a dropped
  duplicate file still present, or a dropped column still in its form's header is
  a removal that did not take effect).
* **must-remain** — a form that was processed but *not* removed wholesale (it had
  only column drops, or no events) must still have a published dataset (cleanup
  must not have silently deleted surviving data).
* **anomaly** — states that should be impossible after a clean run: both members
  of a known suspected-duplicate pair published side by side, a junk-pattern file
  in the published tree, or a zero-byte published dataset (Step 1.9 should have
  pruned a fully-quarantined form before promotion).

Metadata only
-------------
The verifier reads ledger JSON (counts + names), file existence, and the
**first-line JSON keys** of each published ``.jsonl`` (column names only — never a
value, the same metadata-safe read the retrieval eval uses). Findings carry only
file/column *names* + a reason string, so a verifier report can never become a
PHI side-channel.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from phi_engine.audit.ledger import CLEANUP_LEDGER_FILENAME, DATASET_LEDGER_DIRNAME
from phi_engine.utils.logging_system import get_logger

__all__ = [
    "CleanupFinding",
    "CleanupVerifyReport",
    "WorkspaceCleanupReport",
    "WorkspacePathFinding",
    "verify_cleanup",
    "verify_workspace_cleanup",
]

_logger = get_logger(__name__)

#: Cleanup-ledger actions that mean a *file* was removed from the published tree.
_FILE_REMOVAL_ACTIONS = frozenset({"dataset_junk_file", "dataset_duplicate_file"})
#: The action that means a *column* was dropped from a surviving file.
_COLUMN_DROP_ACTION = "dataset_column_drop"

_PHASE_MUST_GONE = "must_gone"
_PHASE_MUST_REMAIN = "must_remain"
_PHASE_ANOMALY = "anomaly"


@dataclass(frozen=True)
class CleanupFinding:
    """One cleanup-consistency problem (names + reason only — never values)."""

    phase: str  # must_gone | must_remain | anomaly
    kind: str  # machine-readable problem code
    target: str  # offending file or column NAME
    detail: str  # human-readable reason


@dataclass(frozen=True)
class CleanupVerifyReport:
    """Aggregate verifier outcome."""

    ok: bool
    findings: tuple[CleanupFinding, ...]
    checked_ledgers: int
    checked_datasets: int

    @property
    def findings_by_phase(self) -> dict[str, list[CleanupFinding]]:
        out: dict[str, list[CleanupFinding]] = {
            _PHASE_MUST_GONE: [],
            _PHASE_MUST_REMAIN: [],
            _PHASE_ANOMALY: [],
        }
        for f in self.findings:
            out.setdefault(f.phase, []).append(f)
        return out


@dataclass
class _LedgerEntry:
    """Parsed view of one per-dataset cleanup ledger."""

    stem: str
    file_removed: bool = False
    removal_action: str | None = None
    dropped_columns: dict[str, str] = field(default_factory=dict)  # column -> source file stem


def _published_columns(path: Path) -> set[str]:
    """Return the first-line JSON keys of a published ``.jsonl`` (names only).

    Reads at most the first line and decodes only its *keys*; never inspects a
    value. Returns an empty set for an empty file or a parse failure.
    """
    try:
        with path.open("r", encoding="utf-8") as fh:
            first = fh.readline()
    except OSError:
        return set()
    first = first.strip()
    if not first:
        return set()
    try:
        obj = json.loads(first)
    except ValueError:
        return set()
    return set(obj.keys()) if isinstance(obj, dict) else set()


def _is_empty_jsonl(path: Path) -> bool:
    """True if the file is zero-byte or its first line is blank."""
    try:
        if path.stat().st_size == 0:
            return True
        with path.open("r", encoding="utf-8") as fh:
            return not fh.readline().strip()
    except OSError:
        return True


def _load_cleanup_ledgers(audit_dir: Path) -> list[_LedgerEntry]:
    """Parse every per-dataset cleanup ledger under ``audit_dir``."""
    ledger_root = Path(audit_dir) / DATASET_LEDGER_DIRNAME
    entries: list[_LedgerEntry] = []
    for ledger_path in sorted(ledger_root.glob(f"*/{CLEANUP_LEDGER_FILENAME}")):
        stem = ledger_path.parent.name
        try:
            data = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _logger.warning("cleanup ledger unreadable: %s (skipped)", ledger_path)
            continue
        entry = _LedgerEntry(stem=stem)
        for event in data.get("events", []):
            action = event.get("action")
            var = event.get("variable_id", "")
            if action in _FILE_REMOVAL_ACTIONS:
                entry.file_removed = True
                entry.removal_action = action
            elif action == _COLUMN_DROP_ACTION:
                src = Path((event.get("where") or {}).get("dataset_file") or "").stem or stem
                entry.dropped_columns[var] = src
        entries.append(entry)
    return entries


def verify_cleanup(
    audit_dir: Path,
    published_datasets_dir: Path,
    *,
    junk_patterns: frozenset[str] | None = None,
    duplicate_pairs: list[tuple[str, str]] | None = None,
) -> CleanupVerifyReport:
    """Verify the published tree against the recorded cleanup decisions.

    Args:
        audit_dir: the study audit dir holding ``datasets/<stem>/`` ledgers.
        published_datasets_dir: the published JSONL tree (``llm_source/.../files``).
        junk_patterns: known junk stems (defaults to the dataset-cleanup set);
            any present in the published tree is an anomaly.
        duplicate_pairs: known suspected-duplicate stem pairs; both-present is an
            anomaly.
    """
    audit_dir = Path(audit_dir)
    pub = Path(published_datasets_dir)
    findings: list[CleanupFinding] = []

    published_files = {p.stem: p for p in pub.glob("*.jsonl")} if pub.is_dir() else {}
    entries = _load_cleanup_ledgers(audit_dir)

    # ── Phase 1: must-gone ────────────────────────────────────────────────────
    for entry in entries:
        if entry.file_removed and entry.stem in published_files:
            findings.append(
                CleanupFinding(
                    phase=_PHASE_MUST_GONE,
                    kind=f"removed_file_present:{entry.removal_action}",
                    target=f"{entry.stem}.jsonl",
                    detail=(
                        f"ledger recorded {entry.removal_action} for '{entry.stem}' but the "
                        "file is still in the published tree"
                    ),
                )
            )
        for column, src_stem in entry.dropped_columns.items():
            target_file = published_files.get(src_stem)
            if target_file is not None and column in _published_columns(target_file):
                findings.append(
                    CleanupFinding(
                        phase=_PHASE_MUST_GONE,
                        kind="dropped_column_present",
                        target=column,
                        detail=(
                            f"column '{column}' was recorded as dropped from "
                            f"'{src_stem}' but still appears in its published header"
                        ),
                    )
                )

    # ── Phase 2: must-remain ──────────────────────────────────────────────────
    # A form that was not removed wholesale must still be published; if its file
    # is gone, cleanup may have removed surviving data.
    findings.extend(
        CleanupFinding(
            phase=_PHASE_MUST_REMAIN,
            kind="surviving_dataset_missing",
            target=f"{entry.stem}.jsonl",
            detail=(
                f"'{entry.stem}' was processed (no file-removal event) but has no "
                "published dataset — cleanup may have removed surviving data"
            ),
        )
        for entry in entries
        if not entry.file_removed and entry.stem not in published_files
    )

    # ── Phase 3: anomaly ──────────────────────────────────────────────────────
    junk = junk_patterns if junk_patterns is not None else _default_junk_patterns()
    for stem, path in published_files.items():
        if stem in junk:
            findings.append(
                CleanupFinding(
                    phase=_PHASE_ANOMALY,
                    kind="junk_file_published",
                    target=f"{stem}.jsonl",
                    detail=f"junk-pattern file '{stem}' present in the published tree",
                )
            )
        if _is_empty_jsonl(path):
            findings.append(
                CleanupFinding(
                    phase=_PHASE_ANOMALY,
                    kind="empty_published_dataset",
                    target=f"{stem}.jsonl",
                    detail=(
                        f"published dataset '{stem}' is empty — a fully-quarantined form "
                        "should have been pruned (Step 1.9) before promotion"
                    ),
                )
            )

    pairs = duplicate_pairs if duplicate_pairs is not None else _default_duplicate_pairs()
    for a, b in pairs:
        if a in published_files and b in published_files:
            findings.append(
                CleanupFinding(
                    phase=_PHASE_ANOMALY,
                    kind="duplicate_pair_both_published",
                    target=f"{a}.jsonl|{b}.jsonl",
                    detail=(
                        f"both members of suspected-duplicate pair ('{a}', '{b}') are "
                        "published — duplicate handling did not resolve or hold the pair"
                    ),
                )
            )

    return CleanupVerifyReport(
        ok=not findings,
        findings=tuple(findings),
        checked_ledgers=len(entries),
        checked_datasets=len(published_files),
    )


# ── Note 13: two-list workspace purge verifier ───────────────────────────────
# Distinct concern from verify_cleanup (which checks dataset-cleanup ledger
# consistency). This walks the note's two lists: every "must be gone" temporary
# path must be absent, and every "must remain" permanent path must be present.
# Names only — never a row value.


@dataclass(frozen=True)
class WorkspacePathFinding:
    """A single two-list violation (names only)."""

    phase: str  # 'must_gone' | 'must_remain' | 'anomaly'
    target: str  # path string, relative to BASE_DIR when possible
    detail: str


@dataclass(frozen=True)
class WorkspaceCleanupReport:
    ok: bool
    findings: tuple[WorkspacePathFinding, ...]
    checked_must_gone: int
    checked_must_remain: int
    checked_anomaly: int = 0


def _safe_rel(p: Path) -> str:
    """Path relative to BASE_DIR when possible (names only, never a value)."""
    import config

    try:
        return str(Path(p).resolve().relative_to(Path(config.BASE_DIR).resolve()))
    except (ValueError, OSError):
        return Path(p).name


def verify_workspace_cleanup(
    *, study: str, run_dir: Path, expect_cleanup_token_present: bool = False
) -> WorkspaceCleanupReport:
    """Workspace purge check (Note 13 Phase 1 + Phase 2 + Phase 3).

    Phase 1 (must-be-gone): every temporary artifact — tmp/{STUDY} staging, SoT
    intermediates (sot_source_pack_* and sot_render_* per-form /tmp dirs), the
    header-extraction store, and the scrub/cleanup in-progress tokens — must be
    absent. Phase 2 (must-remain): every permanent path —
    llm_source/, audit/, snapshots/, config/{STUDY}/, data/raw/{STUDY}/ — must be
    present (a missing one is a possible data-loss event). Phase 3 (anomaly scan):
    any UNEXPECTED entry surviving under the ephemeral staging root (not a known
    staging subdir) is flagged for human review — never deleted; an anomaly fails
    the check so the snapshot is not committed.

    ``expect_cleanup_token_present`` lets the orchestrator hold the live
    cleanup.in_progress token during the walk (it deletes it only after a pass),
    so that token is not flagged while legitimately held.
    """
    import config
    from scripts.extraction.header_store import header_store_path

    findings: list[WorkspacePathFinding] = []

    must_gone: list[Path] = [
        Path(config.STUDY_STAGING_DIR),
        Path(config.STAGING_DATASETS_DIR),
        Path(config.STAGING_SOT_DIR),
        Path(config.STAGING_HEADERS_DIR),
        run_dir / "scrub.in_progress",
    ]
    hs = header_store_path(run_dir)
    if hs is not None:
        must_gone.append(hs)
    if not expect_cleanup_token_present:
        must_gone.append(run_dir / "cleanup.in_progress")
    findings.extend(
        WorkspacePathFinding(
            phase=_PHASE_MUST_GONE,
            target=_safe_rel(p),
            detail="temporary artifact still present after cleanup",
        )
        for p in must_gone
        if p.exists()
    )
    # NOTE (A3): the SoT per-form intermediates (sot_source_pack_*/sot_render_*)
    # are written under the SYSTEM temp dir with no run/study id in their names,
    # so a global glob here cross-contaminates across concurrent runs (and across
    # this verifier's own test runs). They are cleaned by _cleanup_sot_temps in
    # the SoT generator (generate_form); a run-scoped check would require the
    # generator to write them under run_dir first. Intentionally NOT globbing
    # /tmp here — that scan was removed after it false-flagged unrelated leftovers.

    must_remain: list[Path] = [
        Path(config.STUDY_LLM_SOURCE_DIR),
        Path(config.STUDY_AUDIT_DIR),
        Path(config.STUDY_SNAPSHOTS_OUTPUT_DIR),
        Path(config.STUDY_CONFIG_DIR),
        Path(config.STUDY_DATA_DIR),
    ]
    findings.extend(
        WorkspacePathFinding(
            phase=_PHASE_MUST_REMAIN,
            target=_safe_rel(p),
            detail="permanent path missing — possible data-loss event",
        )
        for p in must_remain
        if not p.exists()
    )

    # Phase 3 — anomaly scan (Note 13): unexpected leftovers in the EPHEMERAL
    # staging root. After a clean run tmp/{STUDY} is gone entirely, so any entry
    # surviving there that is NOT a known staging subdir (those are already
    # covered by Phase 1) is garbage from a failed run, an artifact from a prior
    # pipeline version, or something an external process placed. Scoped to the
    # ephemeral root ONLY — the permanent output trees (llm_source/audit/
    # snapshots) legitimately hold many files and are never anomaly-scanned.
    # Flag-only: never deletes; an anomaly halts the run (snapshot not committed).
    staging_root = Path(config.STUDY_STAGING_DIR)
    known_ephemeral = {
        Path(config.STAGING_DATASETS_DIR).resolve(),
        Path(config.STAGING_SOT_DIR).resolve(),
        Path(config.STAGING_HEADERS_DIR).resolve(),
    }
    checked_anomaly = 0
    if staging_root.is_dir():
        for child in sorted(staging_root.iterdir()):
            checked_anomaly += 1
            if child.resolve() in known_ephemeral:
                continue  # known staging dir — already a Phase 1 must_gone finding
            findings.append(
                WorkspacePathFinding(
                    phase=_PHASE_ANOMALY,
                    target=_safe_rel(child),
                    detail=("unexpected path present after cleanup — review before proceeding"),
                )
            )

    return WorkspaceCleanupReport(
        ok=not findings,
        findings=tuple(findings),
        checked_must_gone=len(must_gone),
        checked_must_remain=len(must_remain),
        checked_anomaly=checked_anomaly,
    )


def _default_junk_patterns() -> frozenset[str]:
    """No hardcoded junk-file list any more (Note 18).

    Junk/test files are excluded by the ``_forms_manifest.yaml`` ``reject:`` gate
    (Note 11) and the dataset-deduplication skill's ``~$`` lock-file rule (Note 4)
    BEFORE extraction, so no file-level junk anomaly list is needed here.
    """
    return frozenset()


def _default_duplicate_pairs() -> list[tuple[str, str]]:
    """No hardcoded suspected-duplicate list any more (Note 18).

    File-level duplicates are resolved by the dataset-deduplication skill at
    orchestrator phase 2 (raw-file filename normalization) before extraction.
    """
    return []
