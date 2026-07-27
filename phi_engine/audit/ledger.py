from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
UTC = timezone.utc  # py3.9 compat
from pathlib import Path
from uuid import uuid4

import phi_engine.config.config as config

__all__ = [
    "CLEANUP_LEDGER_FILENAME",
    "PHI_LEDGER_FILENAME",
    "PHI_LEDGER_TIMING_FILENAME",
    "LedgerWriter",
    "dataset_cleanup_ledger_path",
    "dataset_phi_ledger_path",
    "ensure_no_llm_sentinel",
    "iter_dataset_phi_ledger_paths",
    "remove_dataset_no_llm_sentinels",
]

DATASET_LEDGER_DIRNAME = "datasets"
PHI_LEDGER_FILENAME = "phi_handling_ledger.as_written.json"
PHI_LEDGER_TIMING_FILENAME = "phi_handling_ledger_timing.json"
CLEANUP_LEDGER_FILENAME = "dataset_cleanup_ledger.as_written.json"

_PHI_ACTIONS: frozenset[str] = frozenset(
    {
        "drop",
        "pseudonymize",
        "jitter_date",
        "generalize",
        "suppress_small_cell",
        "cap",
        "birthdate_drop",
        "band",
    }
)

_CLEANUP_ACTIONS: frozenset[str] = frozenset(
    {
        "dataset_column_drop",
        "dataset_junk_file",
        "dataset_duplicate_file",
    }
)


def _resolve_run_id() -> str:
    env_val = os.environ.get("REPORTAL_RUN_ID")
    if env_val:
        return env_val
    return f"run_{uuid4().hex}"


def _atomic_write_json(output_path: Path, payload: dict) -> None:
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=out.parent,
            prefix=f".{out.name}.",
            suffix=".tmp",
            delete=False,
        ) as fh:
            tmp_path = Path(fh.name)
            json.dump(payload, fh, indent=2, sort_keys=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        tmp_path.replace(out)
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _dataset_folder_name(dataset_file: str | Path) -> str:
    """Return the stable audit folder name for one dataset file."""
    return Path(str(dataset_file)).stem or "_unknown_dataset"


def dataset_ledger_dir(audit_dir: Path, dataset_file: str | Path) -> Path:
    """Return ``audit/datasets/<dataset>/`` for one dataset file."""
    return Path(audit_dir) / DATASET_LEDGER_DIRNAME / _dataset_folder_name(dataset_file)


def dataset_phi_ledger_path(audit_dir: Path, dataset_file: str | Path) -> Path:
    """Return the PHI as-written ledger path for one dataset file."""
    return dataset_ledger_dir(audit_dir, dataset_file) / PHI_LEDGER_FILENAME


def dataset_cleanup_ledger_path(audit_dir: Path, dataset_file: str | Path) -> Path:
    """Return the cleanup as-written ledger path for one dataset file."""
    return dataset_ledger_dir(audit_dir, dataset_file) / CLEANUP_LEDGER_FILENAME


def iter_dataset_phi_ledger_paths(audit_dir: Path) -> list[Path]:
    """Return all per-dataset PHI as-written ledgers under an audit directory."""
    return sorted((Path(audit_dir) / DATASET_LEDGER_DIRNAME).glob(f"*/{PHI_LEDGER_FILENAME}"))


def ensure_no_llm_sentinel(directory: Path) -> None:
    """Ensure a no-LLM sentinel exists in an audit directory."""
    from phi_engine.audit import is_llm_agent

    if is_llm_agent():
        raise PermissionError("audit ledger write refused: REPORTAL_PROCESS_ROLE=llm-agent")
    directory.mkdir(parents=True, exist_ok=True)
    sentinel = directory / config.AUDIT_NO_LLM_SENTINEL_NAME
    if not sentinel.is_file():
        sentinel.write_text("")


def remove_dataset_no_llm_sentinels(audit_dir: Path) -> None:
    """Remove stale dataset-folder sentinels; the audit root carries this marker."""
    for sentinel in sorted(
        (Path(audit_dir) / DATASET_LEDGER_DIRNAME).glob(f"*/{config.AUDIT_NO_LLM_SENTINEL_NAME}")
    ):
        sentinel.unlink(missing_ok=True)


class LedgerWriter:
    """Collects audit events and writes them atomically to a JSON ledger file."""

    def __init__(
        self,
        *,
        output_path: Path,
        run_id: str | None = None,
        scrub_config_hash: str | None = None,
        input_dataset_hash: str | None = None,
        study: str | None = None,
        leg: str | None = None,
        compliance_posture: str | None = None,
        sentinel_dir: Path | None = None,
    ) -> None:
        self._output_path = Path(output_path)
        self._sentinel_dir = (
            Path(sentinel_dir) if sentinel_dir is not None else self._output_path.parent
        )
        self._run_id: str = run_id if run_id is not None else _resolve_run_id()
        self._scrub_config_hash = scrub_config_hash
        self._input_dataset_hash = input_dataset_hash
        self._study = study
        self._leg = leg
        self._compliance_posture = compliance_posture
        self._events: list[dict] = []
        self._keeps: list[dict] = []
        self._sentinel_seen: bool = False

    # ------------------------------------------------------------------
    # Phase 4 runtime guard + sentinel
    # ------------------------------------------------------------------

    def _phase4_guard(self) -> None:
        """Phase 4: refuse writes when LLM-agent role; ensure sentinel."""
        from phi_engine.audit import is_llm_agent

        if is_llm_agent():
            raise PermissionError("audit ledger write refused: REPORTAL_PROCESS_ROLE=llm-agent")
        self._ensure_sentinel()

    def _ensure_sentinel(self) -> None:
        """Ensure the .NO_LLM_ZONE sentinel exists in the audit dir.

        First call (per LedgerWriter instance): if sentinel missing, create it.
        No tampering alarm — co-tenant writers (phi_scrub, dataset_cleanup)
        legitimately populate the audit dir before the first ledger write.

        Subsequent calls: if the sentinel was previously confirmed and is now
        missing, treat it as tampering — alarm + refuse the write.
        """
        sentinel = self._sentinel_dir / config.AUDIT_NO_LLM_SENTINEL_NAME
        if sentinel.is_file():
            self._sentinel_seen = True
            return
        if self._sentinel_seen:
            # Sentinel disappeared after we saw it — tampering.
            self._emit_sentinel_alarm()
            raise PermissionError(f"audit sentinel missing at {sentinel}; ledger write refused")
        # First time we look and sentinel is missing — create it. Idempotent.
        self._sentinel_dir.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("")  # presence is the signal
        self._sentinel_seen = True

    def _emit_sentinel_alarm(self) -> None:
        alarm = {
            "event": "sentinel_missing",
            "path": str(self._output_path),
            "timestamp_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        config.AUDIT_SENTINEL_ALARM_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(config.AUDIT_SENTINEL_ALARM_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(alarm, sort_keys=True) + "\n")

    def add_phi_event(
        self,
        *,
        form: str,
        variable_id: str,
        action: str,
        rule_taxonomy: str | None,
        rule_project_category: str | None,
        rationale: str,
        dataset_file: str | None,
        pdf_source: str | None,
        count: int | None,
        matched_rules: list[str] | None = None,
        jurisdictions: list[str] | None = None,
        rule_bundle_sha256: str | None = None,
        method_name: str | None = None,
        method_parameters: dict | None = None,
    ) -> None:
        """Append one PHI handling event. Raises ValueError on unknown action."""
        self._phase4_guard()
        if not form:
            raise ValueError("form must not be empty")
        if not variable_id:
            raise ValueError("variable_id must not be empty")
        if action not in _PHI_ACTIONS:
            raise ValueError(f"Unknown action: {action!r}")
        if count is not None and count < 0:
            raise ValueError(f"count must be >= 0, got {count}")
        self._events.append(
            {
                "form": form,
                "variable_id": variable_id,
                "action": action,
                "rule": {
                    "taxonomy": rule_taxonomy,
                    "project_category": rule_project_category,
                    "matched_rules": list(matched_rules or []),
                    "jurisdictions": list(jurisdictions or []),
                    "rule_bundle_sha256": rule_bundle_sha256,
                },
                "method": (
                    {"name": method_name, "parameters": dict(method_parameters or {})}
                    if (method_name is not None or method_parameters)
                    else None
                ),
                "rationale": rationale,
                "where": {
                    "dataset_file": dataset_file,
                    "pdf_source": pdf_source,
                },
                "count": count,
            }
        )

    def add_keep_decision(
        self,
        *,
        form: str,
        variable_id: str,
        jurisdictions: list[str] | None,
        matched_rules: list[str] | None,
        rationale: str,
        rule_bundle_sha256: str | None,
    ) -> None:
        """Append one KEEP decision (field retained, not scrubbed)."""
        self._phase4_guard()
        if not form:
            raise ValueError("form must not be empty")
        if not variable_id:
            raise ValueError("variable_id must not be empty")
        self._keeps.append(
            {
                "form": form,
                "variable_id": variable_id,
                "jurisdictions": list(jurisdictions or []),
                "matched_rules": list(matched_rules or []),
                "rationale": rationale,
                "rule_bundle_sha256": rule_bundle_sha256,
            }
        )

    def add_cleanup_event(
        self,
        *,
        form: str,
        variable_id: str,
        action: str,
        rule_project_category: str | None,
        rationale: str,
        dataset_file: str | None,
        count: int | None,
    ) -> None:
        """Append one dataset cleanup event. Raises ValueError on unknown action."""
        self._phase4_guard()
        if not form:
            raise ValueError("form must not be empty")
        if not variable_id:
            raise ValueError("variable_id must not be empty")
        if action not in _CLEANUP_ACTIONS:
            raise ValueError(f"Unknown action: {action!r}")
        if count is not None and count < 0:
            raise ValueError(f"count must be >= 0, got {count}")
        self._events.append(
            {
                "form": form,
                "variable_id": variable_id,
                "action": action,
                "rule": {
                    "taxonomy": None,
                    "project_category": rule_project_category,
                },
                "rationale": rationale,
                "where": {
                    "dataset_file": dataset_file,
                    "pdf_source": None,
                },
                "count": count,
            }
        )

    def flush(self) -> None:
        """Write events to output_path atomically. Safe to call multiple times (overwrites).

        The primary ledger is content-only (no wall-clock timestamps) so that
        byte-identical re-runs on identical input produce byte-identical output.
        Wall-clock fields (``generated_utc``) are written to a parallel timing
        sidecar ``phi_handling_ledger_timing.json`` in the same directory.
        """
        self._phase4_guard()
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        # Primary ledger: content-only, no timestamps.
        envelope: dict = {
            "run_id": self._run_id,
            "study": self._study,
            "leg": self._leg,
            "events": self._events,
        }
        if self._compliance_posture is not None:
            envelope["compliance_posture"] = self._compliance_posture
        if self._scrub_config_hash is not None:
            envelope["scrub_config_hash"] = self._scrub_config_hash
        if self._input_dataset_hash is not None:
            envelope["input_dataset_hash"] = self._input_dataset_hash
        if self._keeps:
            envelope["keep_decisions"] = self._keeps
        _atomic_write_json(self._output_path, envelope)

        # Timing sidecar: wall-clock fields only, excluded from content hash.
        generated_utc = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        timing_path = self._output_path.parent / PHI_LEDGER_TIMING_FILENAME
        timing: dict = {
            "run_id": self._run_id,
            "generated_utc": generated_utc,
        }
        _atomic_write_json(timing_path, timing)

    def event_count(self) -> int:
        """Return number of events collected so far."""
        return len(self._events)
