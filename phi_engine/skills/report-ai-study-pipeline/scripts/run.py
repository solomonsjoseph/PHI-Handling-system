#!/usr/bin/env python3
"""Orchestrator: the 10-phase host publish state machine (Wave 4 B3.7).

This is the top-level entry point of the consolidated pipeline — "the plugin IS
the pipeline". It holds the per-study lock for the whole run, drives the ordered
phases, records a durable ``run_state.json``, and supports the maintainer
human-review resume loop (``--resume-held``).

Topology (the conceptual 10 runtime phases map onto the supervisory steps this
orchestrator drives; the contiguous publish phases 2-7 are executed by the
proven ``dataset-to-llm-source`` publish supervisor in one locked subprocess):

    P0  preflight   — config validation, rulebook resolve + drift, input-
                      fingerprint redundant-run check, dir pre-creation, lock
    P1c dictionary — dictionary-to-llm-source --leg extract (∥ P0 rulebook)
    P2  dedup        — dataset-deduplication (raw-file tiers; internal header reads)
    P1  headers      — header-extraction skill (shared store; column NAMES only)
    P1b SoT         — generate_lean_outputs (policy/schema → audit/SoT_construction/; only joined view → llm_source/SoT/)
    P2  publish     — dataset-to-llm-source `run` (classify → extract → scrub →
                      dedup → PHI guard gate → promote → destroy → inline verify
                      → snapshot), under the lock baton
    P8  cleanup     — cleanup_verifier over the published tree + cleanup ledgers
    P9  verify      — audit-verification skill (idempotent 17-assertion re-verify)
    P10 finalize    — record input fingerprint, finalize run_state, release lock

**Lock baton (risk #7).** The orchestrator holds the lock for the whole run and
hands a validated baton (``REPORTAL_PIPELINE_LOCK_HELD_BY_PARENT`` +
``REPORTAL_PIPELINE_LOCK_PARENT_PID = our pid``) to every skill subprocess, so
they skip re-acquisition rather than racing the same flock. Assertion 11 was
taught to accept a valid parent baton.

**Per-form state machine + crash-recovery readback (Note 16).** Beyond the
phase records, ``run_state.json`` carries a per-form state map — every form is
in exactly one of ``not_started → running → complete`` /
``held_for_review`` / ``re_running`` / ``failed_pipeline_level`` — written on
every transition the orchestrator can observe (init after P1 header store;
``running`` before
the publish leg; authoritative ``complete``/``held_for_review`` absorbed from the
run's ``phi_handling_approval.json`` after). Each form record also carries a
per-form input fingerprint (:func:`compute_per_form_fingerprint`). On restart the
preflight reads any prior ``run_state.json`` left ``in_progress`` (the per-study
lock guarantees such a run is dead, not live) and APPLIES the readback rules to
the new run's state: ``running`` forms reset to ``not_started`` (re-run);
``held_for_review`` forms remain held; prior ``complete`` forms are re-validated
by per-form fingerprint (cache-valid → kept ``complete``; inputs changed → reset
to ``not_started``). It also writes a value-free ``run_recovery.json`` and
atomically marks the crashed run recovered. This is broader than the
``cleanup.in_progress``/``scrub.in_progress`` tokens (which only catch crashes in
those sub-phases). Per-form fingerprints drive this readback classification +
observability — they do NOT drive a work-skip: the new run still re-publishes the
full surviving set, because promotion is a whole-leg atomic replace that always
re-scrubs from raw (fail-closed) (accepted deviation D4, CLAUDE.md §4).

Value-free: ``run_state.json`` carries phase names, statuses, exit codes, form
NAMES, per-form states, fingerprints (hashes), and counts — never a row value.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from phi_engine.utils.skill_protocol import SkillResult, invoke_skill  # noqa: E402

# Schema 2 adds the per-form state machine (``forms``) — Note 16.
# V1→V2 compatibility: if a prior run_state.json is missing the ``forms`` key
# (schema v1, pre-Note-16), the crash-recovery readback defaults it to {} and
# proceeds with no carried-held state or fingerprint revalidation (lines 547–549).
RUN_STATE_SCHEMA = 2

# ── Per-form lifecycle states (Note 16 state machine) ─────────────────────────
# Every tracked form is always in exactly one of these states.
FORM_NOT_STARTED = "not_started"
FORM_RUNNING = "running"
FORM_RE_RUNNING = "re_running"  # a held form being re-run after operator resolution
FORM_COMPLETE = "complete"
FORM_HELD = "held_for_review"
FORM_FAILED = "failed_pipeline_level"

_NON_TERMINAL_FORM_STATES = frozenset({FORM_NOT_STARTED, FORM_RUNNING, FORM_RE_RUNNING})

#: Recognized raw dataset extensions, stripped to normalize a form NAME to its stem.
_DATASET_SUFFIXES = (".xlsx", ".xls", ".csv")


def _form_stem(name: str) -> str:
    """Normalize a form NAME or filename to its bare canonical stem (Note 22).

    Strips only a recognized dataset extension, so a stem that legitimately
    contains a dot is preserved: ``9_EEval.xlsx`` and ``9_EEval`` both → ``9_EEval``.
    """
    s = str(name)
    low = s.lower()
    for ext in _DATASET_SUFFIXES:
        if low.endswith(ext):
            return s[: -len(ext)]
    return s


@dataclass
class _PhaseRecord:
    phase: str
    status: str = "pending"  # pending | running | complete | held | failed | skipped
    detail: str = ""
    exit_code: int | None = None

    def to_json(self) -> dict:
        return {
            "phase": self.phase,
            "status": self.status,
            "detail": self.detail,
            "exit_code": self.exit_code,
        }


@dataclass
class _FormRecord:
    """Per-form state machine record (Note 16). Value-free: name + state + hashes."""

    name: str  # bare form stem (canonical key, Note 22)
    state: str = FORM_NOT_STARTED
    fingerprint: str | None = None  # per-form input fingerprint (hash, never a value)
    detail: str = ""

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "fingerprint": self.fingerprint,
            "detail": self.detail,
        }


@dataclass
class _RunState:
    study: str
    run_id: str
    status: str = "in_progress"  # in_progress | complete | held | failed | skipped_redundant
    phases: list[_PhaseRecord] = field(default_factory=list)
    input_fingerprint: str | None = None
    snapshot_id: str | None = None
    held_forms: list[str] = field(default_factory=list)
    partial: bool = False
    forms: dict[str, _FormRecord] = field(default_factory=dict)  # Note 16 per-form state
    path: Path | None = None

    def to_json(self) -> dict:
        return {
            "schema": RUN_STATE_SCHEMA,
            "study": self.study,
            "run_id": self.run_id,
            "status": self.status,
            "phases": [p.to_json() for p in self.phases],
            "input_fingerprint": self.input_fingerprint,
            "snapshot_id": self.snapshot_id,
            "held_forms": sorted(self.held_forms),
            "partial": self.partial,
            "forms": {name: self.forms[name].to_json() for name in sorted(self.forms)},
        }

    def flush(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(tmp, self.path)

    def phase(self, name: str) -> _PhaseRecord:
        rec = _PhaseRecord(phase=name, status="running")
        self.phases.append(rec)
        self.flush()
        return rec

    # ── Per-form state machine (Note 16) ─────────────────────────────────────
    def init_forms(self, names, fingerprints=None) -> None:
        """Register the form set as ``not_started`` (idempotent), recording each
        form's per-form input fingerprint. Existing records keep their state but
        have their fingerprint refreshed. Written immediately (crash-safe)."""
        fps = fingerprints or {}
        for raw in names:
            stem = _form_stem(raw)
            rec = self.forms.get(stem)
            if rec is None:
                self.forms[stem] = _FormRecord(name=stem, fingerprint=fps.get(stem))
            elif stem in fps:
                rec.fingerprint = fps[stem]
        self.flush()

    def advance_forms(self, new_state: str, *, from_states) -> None:
        """Transition every form currently in *from_states* to *new_state*,
        flushing once if anything changed (written on every transition)."""
        changed = False
        for rec in self.forms.values():
            if rec.state in from_states:
                rec.state = new_state
                changed = True
        if changed:
            self.flush()


def _baton_env(*, run_id: str, study: str) -> dict[str, str]:
    """Child env: lock baton (our pid) + shared run id + study."""
    env = dict(os.environ)
    env["REPORTAL_PIPELINE_LOCK_HELD_BY_PARENT"] = "1"
    env["REPORTAL_PIPELINE_LOCK_PARENT_PID"] = str(os.getpid())
    env["REPORTAL_RUN_ID"] = run_id
    env["STUDY_NAME"] = study
    return env


def _preflight(state: _RunState, *, study: str, run_id: str, resume_held: bool, force: bool) -> int:
    """Phase 0: config validation, rulebook drift, redundant-run check, dirs."""
    import config

    rec = state.phase("P0:preflight")

    # Required inputs.
    forms_manifest = Path(config.study_config_path("_forms_manifest.yaml", study=study))
    study_privacy = Path(config.study_config_path("_study_privacy.yaml", study=study))
    datasets_dir = Path(config.RAW_DATA_DIR) / study / "datasets"
    missing = [
        label
        for label, present in (
            ("_forms_manifest.yaml", forms_manifest.is_file()),
            ("_study_privacy.yaml", study_privacy.is_file()),
            ("datasets/", datasets_dir.is_dir()),
        )
        if not present
    ]
    if missing:
        # Note 11/16: actionable guidance when the study config is absent.
        detail = f"missing inputs: {missing}"
        if any(m.endswith(".yaml") for m in missing):
            detail += (
                f" — no study config; run the study-setup wizard "
                f"(study-setup --study {study} --interactive) or add "
                f"config/{study}/_forms_manifest.yaml + _study_privacy.yaml"
            )
        rec.status, rec.detail, rec.exit_code = "failed", detail, 2
        state.flush()
        return 2

    config.ensure_run_directories(study=study, run_id=run_id)

    # Accumulation guard (Note 13): a surviving cleanup.in_progress token from a
    # prior run means a previous cleanup was interrupted mid-way — halt rather
    # than build on an unknown workspace state. --force overrides for operator
    # recovery (after `make rebuild-llm-source` clears the runs/ dir).
    if not force:
        from phi_engine.utils.run_context import (
            CLEANUP_RECOVERY_MESSAGE,
            scan_for_in_progress_cleanups,
        )

        stale = scan_for_in_progress_cleanups(Path(config.STUDY_OUTPUT_DIR) / "runs")
        if stale:
            print(CLEANUP_RECOVERY_MESSAGE.format(path=stale[0]), file=sys.stderr)
            rec.status = "failed"
            rec.detail = "interrupted cleanup token present"
            rec.exit_code = 6
            state.flush()
            return 6

    # Crash-recovery readback (Note 16): detect a prior run left in_progress (a
    # crash in ANY phase) and record a value-free recovery note + per-form
    # readback. Broader than the cleanup/scrub tokens above (which only catch
    # crashes in those sub-phases). The lock we hold guarantees any in_progress
    # run is dead, not live. Advisory — never blocks a run.
    try:
        run_dir = Path(config.STUDY_OUTPUT_DIR) / "runs" / run_id
        _recover_interrupted_run(state, study=study, run_dir=run_dir)
    except Exception as exc:
        print(f"P0:preflight — crash-recovery readback skipped: {exc}", file=sys.stderr)

    # Rulebook drift (advisory — never blocks).
    try:
        from phi_engine.security.phi_review import load_study_privacy_config
        from phi_engine.security.phi_rulebook import resolve_rulebook

        privacy = load_study_privacy_config(Path(config.RAW_DATA_DIR) / study)
        resolution = resolve_rulebook(privacy, allow_network=False)
        if resolution.drift_detected:
            print(
                f"WARNING: PHI rulebook drift detected for {study} "
                f"(baseline {resolution.baseline_sha256}); confirm the change is intended.",
                file=sys.stderr,
            )
    except Exception as exc:  # advisory only
        print(f"rulebook resolve skipped: {type(exc).__name__}: {exc}", file=sys.stderr)

    # Input-fingerprint redundant-run check (skipped on --resume-held / --force).
    from phi_engine.utils.input_fingerprint import (
        compute_input_fingerprint,
        fingerprint_record_path,
        is_redundant_run,
        read_recorded_fingerprint,
    )

    fp = compute_input_fingerprint(study=study)
    state.input_fingerprint = fp.fingerprint
    if not resume_held and not force:
        recorded = read_recorded_fingerprint(fingerprint_record_path(Path(config.STUDY_AUDIT_DIR)))
        if is_redundant_run(fp, recorded):
            # C5.5: identical inputs → activate the existing clean snapshot for
            # this fingerprint (point `current` at it) instead of re-running.
            detail = "inputs unchanged since last clean run (use --force to re-run)"
            stale_block = None
            try:
                from phi_engine.utils import snapshot as _snapshot

                existing = _snapshot.find_snapshot_by_fingerprint(study, fp.fingerprint)
                if existing is not None:
                    # Defense-in-depth (Note 14): never silently re-activate a snapshot
                    # whose PHI key has rotated (pseudonyms irrecoverable). The key is
                    # now in the fingerprint, so this is a belt-and-suspenders guard.
                    # Fail-soft: if staleness can't be determined, fall back to the
                    # prior activate behavior (don't block on an inability to check).
                    try:
                        manifest = _snapshot.load_snapshot(study, existing)
                        stale_block = next(
                            (
                                f
                                for f in _snapshot.check_snapshot_staleness(
                                    manifest,
                                    current_rulebook_version=_snapshot._gather_rulebook_version(),
                                    current_key_fingerprint=_snapshot._gather_key_fingerprint(),
                                )
                                if f.severity == _snapshot.StalenessSeverity.BLOCK
                            ),
                            None,
                        )
                    except Exception as exc:  # staleness check is best-effort
                        print(f"redundant-run staleness check skipped: {exc}", file=sys.stderr)
                    if stale_block is not None:
                        print(
                            f"P0:preflight — snapshot {existing} is stale "
                            f"({stale_block.trigger}); forcing a full re-run",
                            file=sys.stderr,
                        )
                    else:
                        _snapshot.set_current_snapshot(study, existing)
                        state.snapshot_id = existing
                        detail = f"identical inputs detected, activating snapshot {existing}"
                        print(f"P0:preflight — {detail}", file=sys.stderr)
            except Exception as exc:  # advisory — short-circuit either way
                print(f"redundant-run snapshot activation skipped: {exc}", file=sys.stderr)
            if stale_block is None:
                rec.status = "skipped"
                rec.detail = detail
                rec.exit_code = 0
                state.status = "skipped_redundant"
                state.flush()
                return -1  # sentinel: redundant, short-circuit cleanly
            # else: a BLOCK-level staleness was found → fall through to a full re-run

    # Note 29 follow-up: reconcile the human-review queue so it reflects ONLY this
    # run's holds. The per-form/cross-form notes under audit/human_review/ persist
    # across runs and are NOT otherwise cleaned, so a clean re-run leaves stale
    # notes (e.g. a form that now publishes still showing a prior kept=0 quarantine
    # note). We only reach here on a real full run — a redundant run short-circuits
    # above (returns -1), so notes are never wiped without being repopulated. The
    # later phases recreate each note dir on demand (writers mkdir parents). A
    # --resume-held run is exempt: those notes are the maintainer's working set.
    if not resume_held:
        try:
            from phi_engine.audit.review_paths import human_review_root

            _hr = human_review_root(Path(config.STUDY_AUDIT_DIR))
            if _hr.exists():
                shutil.rmtree(_hr, ignore_errors=True)
        except Exception as exc:  # advisory — never blocks the run
            print(f"P0:preflight — human-review reconciliation skipped: {exc}", file=sys.stderr)

    rec.status, rec.exit_code = "complete", 0
    state.flush()
    return 0


def _record_skill_phase(state: _RunState, name: str, result: SkillResult) -> _PhaseRecord:
    rec = _PhaseRecord(
        phase=name,
        status="complete" if result.ok else "failed",
        detail=result.summary,
        exit_code=result.exit_code,
    )
    state.phases.append(rec)
    state.flush()
    return rec


# ── Per-form state machine helpers (Note 16) ─────────────────────────────────


def _enumerate_form_stems(study: str, run_dir: Path) -> list[str]:
    """Value-free list of form stems for per-form state init (Note 16).

    Prefers the Phase-1 header store; falls back to enumerating the raw datasets
    dir when header extraction was skipped. Names/counts only — never reads rows.
    """
    import config

    try:
        from scripts.extraction.header_store import load_header_store

        store = load_header_store(run_dir)
    except Exception:  # header store optional — fall back to datasets-dir listing
        store = None
    if store:
        stems = list((store.get("forms") or {}).keys())
        if stems:
            return sorted({_form_stem(s) for s in stems})
    try:
        datasets = Path(config.DATASETS_DIR)
        return sorted(
            {
                _form_stem(p.name)
                for p in datasets.iterdir()
                if p.is_file() and p.suffix.lower() in _DATASET_SUFFIXES
            }
        )
    except (OSError, FileNotFoundError):
        return []


def _compute_form_fingerprints(study: str, stems: list[str]) -> dict[str, str]:
    """Per-form input fingerprints for *stems* (Note 16). Fail-soft per form."""
    out: dict[str, str] = {}
    try:
        from phi_engine.utils.input_fingerprint import compute_per_form_fingerprint
    except Exception:
        return out
    for stem in stems:
        try:
            out[stem] = compute_per_form_fingerprint(stem, study=study)
        except Exception:
            out[stem] = ""
    return out


def _init_per_form_state(state: _RunState, *, study: str, run_dir: Path) -> None:
    """Initialize the per-form state machine after dedup + header extraction (Note 16).

    Every discovered form starts ``not_started`` with its per-form input
    fingerprint recorded. Fail-soft: a discovery hiccup leaves the map empty
    rather than failing the run.
    """
    stems = _enumerate_form_stems(study, run_dir)
    if not stems:
        return
    state.init_forms(stems, _compute_form_fingerprints(study, stems))


def _absorb_form_outcomes(state: _RunState, run_dir: Path, *, pipeline_failed: bool) -> None:
    """Set authoritative per-form terminal states after the publish leg (Note 16).

    On a pipeline-level failure, every non-terminal form becomes
    ``failed_pipeline_level``. Otherwise the run's ``phi_handling_approval.json``
    is the source of truth: ``approved_forms → complete``,
    ``held_forms → held_for_review``. Forms removed before the publish leg (dedup
    or manifest rejection) are absent from the report and drop out of the map, so
    it reflects exactly the publish set. Per-form fingerprints are preserved.
    """
    if pipeline_failed:
        changed = False
        for rec in state.forms.values():
            if rec.state in _NON_TERMINAL_FORM_STATES:
                rec.state = FORM_FAILED
                changed = True
        if changed:
            state.flush()
        return

    approved: list[str] = []
    held: list[str] = []
    try:
        data = json.loads((run_dir / "phi_handling_approval.json").read_text(encoding="utf-8"))
        if isinstance(data, dict):
            approved = [_form_stem(f) for f in data.get("approved_forms") or []]
            held = [_form_stem(f) for f in data.get("held_forms") or []]
    except (OSError, ValueError):
        held = [_form_stem(h) for h in state.held_forms]  # coarse fallback

    if not approved and not held:
        return  # nothing authoritative — leave running states for crash visibility

    fps = {name: rec.fingerprint for name, rec in state.forms.items()}
    new_forms: dict[str, _FormRecord] = {}
    for stem in held:
        new_forms[stem] = _FormRecord(name=stem, state=FORM_HELD, fingerprint=fps.get(stem))
    for stem in approved:
        if stem not in new_forms:  # held wins if a form appears in both (defensive)
            new_forms[stem] = _FormRecord(name=stem, state=FORM_COMPLETE, fingerprint=fps.get(stem))
    state.forms = new_forms
    state.flush()


def _scan_for_interrupted_run(study_runs_dir: Path, *, exclude_run_id: str) -> Path | None:
    """Newest prior ``run_state.json`` still marked ``in_progress`` (a crash).

    The per-study lock guarantees no live concurrent run, so any ``in_progress``
    run_state is from a process that died before writing a terminal status.
    Returns the most-recently-modified such file, or ``None``. Value-free.
    """
    if not study_runs_dir.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for p in study_runs_dir.glob("*/run_state.json"):
        if p.parent.name == exclude_run_id or not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("status") == "in_progress":
            try:
                candidates.append((p.stat().st_mtime, p))
            except OSError:
                continue
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][1]


def _recover_interrupted_run(state: _RunState, *, study: str, run_dir: Path) -> dict | None:
    """Crash-recovery readback (Note 16): detect a prior run left ``in_progress``
    and APPLY the per-form readback rules to the new run's state.

    Reads the crashed run's ``run_state.json`` and, per spec, applies each rule to
    ``state.forms`` (so ``run_state.json`` reflects the recovery immediately and
    held state survives repeated crashes):
    - ``running``/``re_running`` → reset to ``not_started`` (re-run);
    - ``held_for_review`` → carried forward (remains held, awaits resolution);
    - ``complete`` → re-validated by per-form fingerprint: cache-valid → kept
      ``complete``; inputs changed → reset to ``not_started`` (re-run).

    Then writes a value-free ``run_recovery.json`` into *run_dir* and atomically
    marks the crashed run ``failed`` (recovered) so it is unambiguous and not
    re-detected. Returns the recovery summary, or ``None`` when no crash is found.

    The publish leg still re-publishes the full surviving set (whole-leg atomic
    promotion); per-form fingerprints drive the readback's state classification +
    observability, NOT a work-skip (accepted deviation D4). Fail-soft throughout.
    """
    import config

    runs_dir = Path(config.STUDY_OUTPUT_DIR) / "runs"
    prior_path = _scan_for_interrupted_run(runs_dir, exclude_run_id=state.run_id)
    if prior_path is None:
        return None
    try:
        prior = json.loads(prior_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(prior, dict):
        return None
    # V1→V2 compatibility: if ``forms`` is missing or not a dict (v1 run_state),
    # default to {} for fail-soft upgrade. No forms to carry or revalidate.
    prior_forms = prior.get("forms")
    if not isinstance(prior_forms, dict):
        prior_forms = {}

    reset_running: list[str] = []
    carried_held: list[str] = []
    revalidated: list[dict] = []
    for name, rec in prior_forms.items():
        if not isinstance(rec, dict):
            continue
        stem = _form_stem(name)
        st = rec.get("state")
        if st in (FORM_RUNNING, FORM_RE_RUNNING):
            # Spec: running at crash time → reset to not_started, re-run.
            reset_running.append(stem)
            state.forms[stem] = _FormRecord(
                name=stem, state=FORM_NOT_STARTED, detail="reset after crash"
            )
        elif st == FORM_HELD:
            # Spec: held_for_review → remain held, await operator resolution.
            carried_held.append(stem)
            state.forms[stem] = _FormRecord(
                name=stem, state=FORM_HELD, detail="carried from interrupted run"
            )
        elif st == FORM_COMPLETE:
            # Spec: complete → check per-form fingerprint; unchanged = cache hit.
            prior_fp = rec.get("fingerprint")
            try:
                from phi_engine.utils.input_fingerprint import compute_per_form_fingerprint

                cur_fp = compute_per_form_fingerprint(stem, study=study)
            except Exception:
                cur_fp = None
            cache_valid = bool(prior_fp) and prior_fp == cur_fp
            revalidated.append({"form": stem, "cache_valid": cache_valid})
            state.forms[stem] = (
                _FormRecord(
                    name=stem,
                    state=FORM_COMPLETE,
                    fingerprint=cur_fp,
                    detail="cache-valid carried from interrupted run",
                )
                if cache_valid
                else _FormRecord(
                    name=stem, state=FORM_NOT_STARTED, detail="inputs changed since interrupted run"
                )
            )
    if reset_running or carried_held or revalidated:
        state.flush()

    summary = {
        "recovered_from_run": prior.get("run_id") or prior_path.parent.name,
        "recovered_by_run": state.run_id,
        "reset_running": sorted(reset_running),
        "carried_held": sorted(carried_held),
        "revalidated_complete": sorted(revalidated, key=lambda r: r["form"]),
    }
    try:
        from phi_engine.utils._extraction_io import atomic_write_json

        atomic_write_json(run_dir / "run_recovery.json", summary)
        # Mark the crashed run recovered atomically so a crash mid-mark cannot
        # corrupt its run_state.json and hide it from future recovery.
        prior["status"] = "failed"
        prior["recovered_by_run"] = state.run_id
        atomic_write_json(prior_path, prior)
    except Exception as exc:  # advisory record only — never blocks the run
        print(f"P0:preflight — run-recovery bookkeeping skipped: {exc}", file=sys.stderr)

    print(
        f"P0:preflight — recovered interrupted run {summary['recovered_from_run']}: "
        f"{len(reset_running)} running→re-run, {len(carried_held)} held carried forward, "
        f"{len(revalidated)} complete re-validated.",
        file=sys.stderr,
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the consolidated 10-phase host publish pipeline for a study."
    )
    parser.add_argument("--study", required=True, help="Study name (folder under data/raw/).")
    parser.add_argument("--run-id", dest="run_id", default=None, help="Override the run id.")
    parser.add_argument(
        "--force", action="store_true", help="Run even if inputs are unchanged (skip redundancy)."
    )
    parser.add_argument(
        "--resume-held",
        dest="resume_held",
        action="store_true",
        help="Maintainer: re-publish the full surviving set after resolving held forms.",
    )
    parser.add_argument("--max-workers", type=int, default=None, dest="max_workers")
    parser.add_argument("--form", action="append", default=None, dest="forms")
    parser.add_argument(
        "--strict-abort",
        action="store_true",
        help="Abort the whole study on the first un-scrubbable row (default: partial publish).",
    )
    parser.add_argument(
        "--skip-header-extraction",
        action="store_true",
        help="Skip the standalone header-extraction phase (publish reads headers anyway).",
    )
    args = parser.parse_args(argv)

    import config
    from scripts.skills.extract_to_llm_source import (
        EXIT_NEEDS_ADVICE,
        EXIT_OK,
        EXIT_PARTIAL_REVIEW,
    )
    from phi_engine.utils.input_fingerprint import (
        compute_input_fingerprint,
        fingerprint_record_path,
        write_fingerprint_record,
    )
    from phi_engine.utils.pipeline_lock import acquire_pipeline_lock, release_pipeline_lock
    from phi_engine.utils.run_context import resolve_run_id

    study = args.study
    os.environ.setdefault("STUDY_NAME", study)
    # ``config`` resolves all study-scoped paths (STUDY_OUTPUT_DIR, STUDY_AUDIT_DIR,
    # …) from STUDY_NAME at *import* time. If the ambient STUDY_NAME differs from
    # the ``--study`` we were asked to run, the orchestrator would write its
    # run_state.json / fingerprint / current-pointer under the wrong study tree
    # while the publish skill (which honours ``--study``) writes status.json under
    # the right one — so ``_absorb_status`` would silently read a non-existent
    # status.json and the snapshot/current-pointer wiring would no-op. Fail closed
    # with an actionable message rather than diverging silently. The production
    # entry point (`make study STUDY=<name>`) exports STUDY_NAME, so this passes.
    if study != config.STUDY_NAME:
        print(
            f"Refusing to run: --study is '{study}' but config resolved study "
            f"'{config.STUDY_NAME}' (from the STUDY_NAME env at import time). "
            f"Re-run with STUDY_NAME={study} set in the environment "
            f"(e.g. `make study STUDY={study}`).",
            file=sys.stderr,
        )
        return EXIT_NEEDS_ADVICE
    run_id = args.run_id or resolve_run_id()
    run_dir = Path(config.STUDY_OUTPUT_DIR) / "runs" / run_id

    state = _RunState(study=study, run_id=run_id)
    state.path = run_dir / "run_state.json"

    # ── Acquire the lock for the whole orchestrated run ───────────────────────
    try:
        acquire_pipeline_lock(study)
    except RuntimeError as exc:
        state.phases.append(
            _PhaseRecord("P0:lock", "failed", f"lock unavailable: {exc}", exit_code=6)
        )
        state.status = "failed"
        state.flush()
        print(f"Pipeline lock unavailable: {exc}", file=sys.stderr)
        return 6

    child_env = _baton_env(run_id=run_id, study=study)
    try:
        # ── P0 preflight ─────────────────────────────────────────────────────
        pf = _preflight(
            state, study=study, run_id=run_id, resume_held=args.resume_held, force=args.force
        )
        if pf == -1:  # redundant short-circuit
            print(f"Skipping {study}: inputs unchanged since the last clean run.")
            return EXIT_OK
        if pf != 0:
            state.status = "failed"
            state.flush()
            return pf

        # ── P1c dictionary extraction (Note 1 — parallel with P0 rulebook) ───
        dict_ext = invoke_skill(
            "dictionary-to-llm-source",
            ["--study", study, "--run-id", run_id, "--run-dir", str(run_dir), "--leg", "extract"],
            env=child_env,
        )
        derec = _record_skill_phase(state, "P1c:dictionary-extract", dict_ext)
        if not dict_ext.ok:
            state.status = "failed"
            derec.detail = dict_ext.summary
            state.flush()
            return dict_ext.exit_code or 1

        # ── P2 raw-file deduplication (Note 4 — before shared header store / SoT) ─
        dedup = invoke_skill(
            "dataset-deduplication",
            ["--study", study, "--run-id", run_id, "--run-dir", str(run_dir)],
            env=child_env,
        )
        drec = _record_skill_phase(state, "P2:dataset-deduplication", dedup)
        if not dedup.ok:
            state.status = "failed"
            drec.detail = dedup.summary
            state.flush()
            return dedup.exit_code or 1

        # ── P1 header extraction (shared store; column NAMES only) ───────────
        if not args.skip_header_extraction:
            hdr = invoke_skill(
                "header-extraction",
                ["--study", study, "--run-id", run_id, "--run-dir", str(run_dir)],
                env=child_env,
            )
            hrec = _record_skill_phase(state, "P1:header-extraction", hdr)
            if not hdr.ok:
                state.status = "failed"
                hrec.detail = hdr.summary
                state.flush()
                return hdr.exit_code or 1

        # Note 16: initialize the per-form state machine after dedup + header
        # extraction so the form set reflects the deduplicated raw file list.
        _init_per_form_state(state, study=study, run_dir=run_dir)

        # ── P1b SoT lean outputs (joined views before publish gate) ─────────
        from scripts.source_truth.generate_lean_outputs import main as generate_lean_outputs_main

        sot_rec = state.phase("P1b:sot-lean-generate")
        sot_rc = generate_lean_outputs_main(
            ["--study", study, "--repo-root", str(config.BASE_DIR), "--run-dir", str(run_dir)]
        )
        sot_rec.exit_code = sot_rc
        if sot_rc != 0:
            sot_rec.status = "failed"
            state.status = "failed"
            state.flush()
            return sot_rc or 1
        sot_rec.status = "complete"

        # ── P2 publish (delegated supervisor under the baton) ────────────────
        publish_args = ["run", "--study", study]
        if args.max_workers is not None:
            publish_args += ["--max-workers", str(args.max_workers)]
        for form in args.forms or []:
            publish_args += ["--form", form]
        if args.resume_held:
            publish_args += ["--resume-held"]
        if args.strict_abort:
            child_env = dict(child_env, REPORTAL_SCRUB_STRICT_ABORT="1")

        # Note 13: defer the supervisor's Step-7 snapshot commit so the orchestrator
        # commits at P10, only after the cleanup (P8) + audit (P9) verifiers pass.
        # (A scrub-only-partial publish still commits inline — see Step 7.)
        publish_env = dict(child_env, REPORTAL_DEFER_SNAPSHOT_COMMIT="1")
        # Note 16: mark all pending forms running (re_running on a resume) before
        # the publish leg — written on the transition so a crash here is visible.
        state.advance_forms(
            FORM_RE_RUNNING if args.resume_held else FORM_RUNNING,
            from_states={FORM_NOT_STARTED},
        )
        publish = invoke_skill("dataset-to-llm-source", publish_args, env=publish_env)
        prec = _record_skill_phase(state, "P2:publish", publish)

        # Surface held/partial state from the run's status.json (form names only).
        _absorb_status(state, run_dir)
        # Note 16: set authoritative per-form terminal states from the approval
        # report (approved → complete, held → held_for_review); a pipeline-level
        # failure marks every non-terminal form failed_pipeline_level.
        _absorb_form_outcomes(
            state,
            run_dir,
            pipeline_failed=publish.exit_code not in {EXIT_OK, EXIT_PARTIAL_REVIEW},
        )

        # Note 6: destroy the shared header store after the publish leg — it was
        # consumed by PHI-classification (inside the publish supervisor).
        try:
            from scripts.extraction.header_store import destroy_header_store

            destroy_header_store(run_dir)
        except Exception as exc:  # best-effort cleanup, never blocks the run
            print(f"P7: header-store destroy skipped: {exc}", file=sys.stderr)

        if publish.exit_code not in {EXIT_OK, EXIT_PARTIAL_REVIEW}:
            state.status = "failed"
            prec.status = "failed"
            state.flush()
            return publish.exit_code

        if publish.exit_code == EXIT_PARTIAL_REVIEW:
            # Published forms ARE published; held forms await maintainer review.
            prec.status = "held"
            state.status = "held"
            state.partial = True
            state.flush()
            print(
                f"Partial publish for {study}: {len(state.held_forms)} form(s) held for review. "
                f"Resolve, then re-run with --resume-held.",
                file=sys.stderr,
            )
            return EXIT_PARTIAL_REVIEW

        # ── P8 cleanup verifier (native): ledger consistency + two-list purge ──
        from dataclasses import asdict

        from phi_engine.utils._extraction_io import atomic_write_json
        from phi_engine.utils.cleanup_verifier import verify_cleanup, verify_workspace_cleanup
        from phi_engine.utils.run_context import delete_cleanup_token, write_cleanup_token

        # Gap 7 token: written before the verifier, deleted only on a clean pass;
        # a surviving token on the next run signals an interrupted cleanup.
        write_cleanup_token(run_dir)
        crec = state.phase("P8:cleanup-verifier")
        # Ledger consistency: published JSONL lives under llm_source/dataset_schema/
        # files/ (config.TRIO_DATASETS_DIR), the same location assertion 10 checks.
        ledger_report = verify_cleanup(Path(config.STUDY_AUDIT_DIR), Path(config.TRIO_DATASETS_DIR))
        # Two-list workspace purge (Note 13): must-be-gone temporaries absent +
        # must-remain permanents present. The live cleanup token is held during the
        # walk, so it is excluded from the must-be-gone set.
        ws_report = verify_workspace_cleanup(
            study=study, run_dir=run_dir, expect_cleanup_token_present=True
        )
        ok = ledger_report.ok and ws_report.ok
        # Persist the names-only combined record to the audit zone (permanent).
        try:
            atomic_write_json(
                Path(config.STUDY_AUDIT_DIR) / "cleanup_verification_report.json",
                {
                    "run_id": run_id,
                    "ledger_ok": ledger_report.ok,
                    "ledger_findings": [asdict(f) for f in ledger_report.findings],
                    "workspace_ok": ws_report.ok,
                    "workspace_findings": [asdict(f) for f in ws_report.findings],
                    "checked_must_gone": ws_report.checked_must_gone,
                    "checked_must_remain": ws_report.checked_must_remain,
                    "checked_anomaly": ws_report.checked_anomaly,
                },
            )
        except Exception as exc:  # advisory record; never fail the run on a write hiccup
            print(f"P8: cleanup_verification_report write skipped: {exc}", file=sys.stderr)
        n_find = len(ledger_report.findings) + len(ws_report.findings)
        crec.status = "complete" if ok else "failed"
        crec.detail = "clean" if ok else f"{n_find} cleanup finding(s)"
        crec.exit_code = 0 if ok else 1
        state.flush()
        if not ok:
            state.status = "failed"
            state.flush()
            return 5  # EXIT_VERIFIER_FAIL family — token LEFT in place (interrupted cleanup)
        delete_cleanup_token(run_dir)  # only after BOTH verifiers pass

        # ── P9 full verifier (idempotent re-verify under the baton) ──────────
        verify = invoke_skill(
            "audit-verification",
            ["--study", study, "--run-id", run_id],
            env=child_env,
        )
        vrec = _record_skill_phase(state, "P9:verify", verify)
        if not verify.ok:
            state.status = "failed"
            vrec.status = "failed"
            state.flush()
            return verify.exit_code or 5

        # ── P10 finalize ─────────────────────────────────────────────────────
        frec = state.phase("P10:finalize")
        fp = compute_input_fingerprint(study=study)
        write_fingerprint_record(fingerprint_record_path(Path(config.STUDY_AUDIT_DIR)), fp)
        state.input_fingerprint = fp.fingerprint
        # Commit the snapshot NOW (Note 13) — only after P8 (cleanup) + P9 (audit)
        # both passed. The supervisor deferred its Step-7 commit via
        # REPORTAL_DEFER_SNAPSHOT_COMMIT, so the snapshot is created only on a
        # fully-verified clean pass. cleanup_verifier_passed=True records the proof
        # in the manifest (Note 14). Fail-soft — a commit hiccup must not fail an
        # otherwise-complete run.
        try:
            from phi_engine.utils.snapshot import commit_run_snapshot

            commit_run_snapshot(
                study=study,
                run_id=run_id,
                run_dir=run_dir,
                resume_held=args.resume_held,
                cleanup_verifier_passed=True,
            )
        except Exception as exc:  # advisory: never fail a complete run on commit hiccup
            print(f"P10:finalize — snapshot commit skipped: {exc}", file=sys.stderr)
        _absorb_status(state, run_dir)  # pick up snapshot_id committed at P10
        # C5.3: phase-10 points `current` at the freshly committed snapshot so a
        # clean publish becomes the study's designated active one. Fail-soft — a
        # pointer-write hiccup must not fail an otherwise-complete run.
        if state.snapshot_id:
            try:
                from phi_engine.utils import snapshot as _snapshot

                _snapshot.set_current_snapshot(study, state.snapshot_id)
            except Exception as exc:  # advisory: never fail a complete run
                print(f"P10:finalize — current-pointer write skipped: {exc}", file=sys.stderr)
        frec.status, frec.exit_code = "complete", 0
        state.status = "complete"
        state.flush()
        print(f"Pipeline complete for {study} (run {run_id}).")
        return EXIT_OK
    finally:
        release_pipeline_lock(study)


def _absorb_status(state: _RunState, run_dir: Path) -> None:
    """Best-effort copy of held_forms / snapshot_id from the run's status.json."""
    status_path = run_dir / "status.json"
    if not status_path.is_file():
        return
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(status, dict):
        return
    held = status.get("held_forms")
    if isinstance(held, list) and held:
        state.held_forms = [str(h) for h in held]
    elif int(status.get("held_forms_count") or 0) > 0:
        # Older runs wrote counts only; merge SoT joined-view holds when present.
        sot_path = run_dir / "sot_joined_gate_outcome.json"
        try:
            if sot_path.is_file():
                sot_raw = json.loads(sot_path.read_text(encoding="utf-8"))
                if isinstance(sot_raw, dict) and sot_raw.get("run_id") == state.run_id:
                    sot_held = sot_raw.get("held_forms")
                    if isinstance(sot_held, list):
                        state.held_forms = sorted({str(h) for h in sot_held})
        except (OSError, ValueError):
            pass
    snap = status.get("snapshot_id")
    if isinstance(snap, str):
        state.snapshot_id = snap


if __name__ == "__main__":
    raise SystemExit(main())
