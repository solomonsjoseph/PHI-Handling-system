"""Human-review feedback loop: decision memory + review-item listing.

Persistent per-study decision store
(``<study config dir>/<study>/review_decisions.yaml``)::

    {HEADER_UPPER: {decision: keep|drop|override, action: <Action name>,
                     decided_by: str, decided_at: ISO8601, source: cli|file}}

Consumed by :func:`phi_engine.pipeline.run.run_pipeline`'s classification
step:

- ``keep``     -> ``confirmed_keep_headers`` (the existing
  ``review_form_headers`` parameter designed exactly for this: un-holds a
  PHI-risky-NAMED header the SoT/heuristic path would otherwise force-drop).
- ``drop``     -> merged into the form's ``force_drop_headers`` (removed by
  the scrubber regardless of what the name-rules alone would have decided).
- ``override`` -> the header's classification ``action`` is replaced
  (``dataclasses.replace``) BEFORE the approval JSON is written and BEFORE
  ``synthesize_study_config`` runs, so the scrubber applies the OVERRIDDEN
  method on the NEXT run.

Also fixes prior-audit M3: ``llm_detector._write_review_queue``'s
``review_queue_path`` previously had no resolved default and could be
handed a path relative to the caller's cwd; :data:`DEFAULT_LLM_QUEUE_PATH`
here resolves it through ``config.STUDY_AUDIT_DIR`` (the zone-guarded
chokepoint), and callers should pass that instead of an ad hoc path.
"""

from __future__ import annotations

import json
from dataclasses import replace as _dc_replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

import phi_engine.config.config as config
from phi_engine.audit import review_paths
from phi_engine.security.phi_review import Action, HeaderClassification

__all__ = [
    "DECISIONS_FILENAME",
    "DEFAULT_LLM_QUEUE_PATH",
    "apply_decisions_to_classifications",
    "confirmed_keep_headers",
    "decide",
    "extra_force_drop_headers",
    "list_review_items",
    "load_review_decisions",
]

DECISIONS_FILENAME = "review_decisions.yaml"
DECISIONS_TRAIL_FILENAME = "decisions.jsonl"
_VALID_DECISIONS = frozenset({"keep", "drop", "override"})


def DEFAULT_LLM_QUEUE_PATH() -> Path:  # noqa: N802 -- reads as a named constant at call sites
    """Zone-guarded default for the LLM header-classifier's uncertain-queue
    path (fixes prior-audit M3: no longer relative to the caller's cwd)."""
    return review_paths.human_review_root(Path(config.STUDY_AUDIT_DIR)) / "llm_uncertain.jsonl"


def _decisions_dir(study: str) -> Path:
    return Path(config.study_config_dir(study))


def _decisions_path(study: str) -> Path:
    return _decisions_dir(study) / DECISIONS_FILENAME


def load_review_decisions(study: str) -> dict[str, dict[str, Any]]:
    """Return ``{HEADER_UPPER: {decision, action, decided_by, decided_at, source}}``."""
    path = _decisions_path(study)
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k).upper(): dict(v) for k, v in data.items() if isinstance(v, dict)}


def decide(
    study: str,
    *,
    header: str,
    decision: str,
    action: str | None = None,
    decided_by: str = "cli",
    source: str = "cli",
) -> Path:
    """Record one review decision (non-interactive, scriptable).

    Idempotent overwrite semantics: a repeat call for the same header
    replaces the prior decision -- the store holds CURRENT state, applied on
    the NEXT pipeline run. Every call also appends to the append-only
    ``decisions.jsonl`` audit trail.
    """
    if decision not in _VALID_DECISIONS:
        raise ValueError(f"decision must be one of {sorted(_VALID_DECISIONS)}, got {decision!r}")
    if decision == "override" and not action:
        raise ValueError("decision='override' requires an explicit --action")
    valid_actions = {a.value for a in Action}
    if action is not None and action not in valid_actions:
        raise ValueError(f"unknown action {action!r}; must be one of {sorted(valid_actions)}")

    decisions = load_review_decisions(study)
    record = {
        "decision": decision,
        "action": action,
        "decided_by": decided_by,
        "decided_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
    }
    decisions[header.upper()] = record

    out_dir = _decisions_dir(study)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / DECISIONS_FILENAME
    path.write_text(yaml.safe_dump(decisions, sort_keys=True), encoding="utf-8")

    trail_path = out_dir / DECISIONS_TRAIL_FILENAME
    with trail_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"header": header.upper(), **record}, sort_keys=True) + "\n")
    return path


def confirmed_keep_headers(decisions: dict[str, dict[str, Any]]) -> frozenset[str]:
    """Headers with a ``keep`` decision -- feeds
    ``review_form_headers(confirmed_keep_headers=...)``."""
    return frozenset(h for h, d in decisions.items() if d.get("decision") == "keep")


def extra_force_drop_headers(decisions: dict[str, dict[str, Any]]) -> frozenset[str]:
    """Headers with a ``drop`` decision -- merged into a form's
    ``force_drop_headers`` regardless of the header's own rule-based action."""
    return frozenset(h for h, d in decisions.items() if d.get("decision") == "drop")


def apply_decisions_to_classifications(
    classifications: tuple[HeaderClassification, ...],
    decisions: dict[str, dict[str, Any]],
) -> tuple[HeaderClassification, ...]:
    """Apply ``override`` decisions to a form's classification tuple.

    ``keep``/``drop`` decisions are threaded through
    ``review_form_headers``'s ``confirmed_keep_headers`` param / a
    ``force_drop_headers`` merge instead (see :func:`confirmed_keep_headers`
    / :func:`extra_force_drop_headers`) -- only ``override`` needs a
    classification MUTATION, because it is the only decision kind that names
    a DIFFERENT applied method (read by both the approval JSON and
    ``synthesize_study_config``).
    """
    updated: list[HeaderClassification] = []
    for item in classifications:
        entry = decisions.get(item.header.upper())
        if entry and entry.get("decision") == "override" and entry.get("action"):
            new_action = Action(entry["action"])
            if new_action != item.action:
                updated.append(
                    _dc_replace(
                        item,
                        action=new_action,
                        matched_rules=(*item.matched_rules, "review_decision_override"),
                        reasons=(f"review-decision override -> {new_action.value}",),
                    )
                )
                continue
        updated.append(item)
    return tuple(updated)


def list_review_items(study: str) -> dict[str, Any]:
    """Everything currently awaiting human review for *study*:

    (a) the organizer review bucket, (b) header holds from the latest run's
    approval JSON, (c) the LLM header-classifier's uncertain-queue entries
    (when that optional path is in use), plus the decisions already on file.
    """
    audit_dir = Path(config.STUDY_AUDIT_DIR)

    organizer_bucket: list[dict[str, Any]] = []
    organizer_path = review_paths.organizer_review_path(audit_dir)
    if organizer_path.is_file():
        for line in organizer_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                organizer_bucket.append(json.loads(line))

    held_forms: list[dict[str, Any]] = []
    runs_dir = Path(config.STUDY_OUTPUT_DIR) / "runs"
    if runs_dir.is_dir():
        run_ids = sorted(p.name for p in runs_dir.iterdir() if p.is_dir())
        if run_ids:
            approval_path = runs_dir / run_ids[-1] / "phi_handling_approval.json"
            if approval_path.is_file():
                approval = json.loads(approval_path.read_text(encoding="utf-8"))
                for form in approval.get("forms", []):
                    if form.get("status") == "held":
                        held_forms.append(
                            {"form_name": form.get("form_name"), "reasons": form.get("reasons", [])}
                        )

    llm_uncertain: list[dict[str, Any]] = []
    queue_path = DEFAULT_LLM_QUEUE_PATH()
    if queue_path.is_file():
        for line in queue_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                llm_uncertain.append(json.loads(line))

    return {
        "study": study,
        "organizer_review_bucket": organizer_bucket,
        "held_forms": held_forms,
        "llm_uncertain_queue": llm_uncertain,
        "decisions_on_file": load_review_decisions(study),
    }
