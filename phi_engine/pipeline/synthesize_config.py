"""Classification-driven per-study scrub-config synthesis.

Today the packaged ``phi_scrub.yaml`` defaults are RePORTaLiN-specific: a
header ``phi_review`` classifies (jitter_date/pseudonymize/cap/drop/
generalize/suppress/keep) but that matches NONE of the packaged patterns
falls through ``_scrub_row`` unclassified -- i.e. is published raw as an
implicit KEEP, regardless of what ``phi_review`` decided. This module closes
that gap: it starts from ``_defaults/phi_scrub.yaml`` (every packaged rule
stays in force) and appends each classified header as an exact-match
anchored regex (``^HEADER$``, ``re.escape``d) to the section matching its
action, so every classification decision actually reaches the row scrubber.

Regenerated on every pipeline run from the CURRENT classification set (never
hand-maintained), so a review-decision override (Phase 4) is reflected on
the NEXT run without manual YAML editing.
"""

from __future__ import annotations

import re
from datetime import datetime as _dt
from datetime import timezone as _tz
from pathlib import Path
from typing import Iterable, Mapping

import yaml

import phi_engine.config.config as config
from phi_engine.security.phi_review import Action, HeaderClassification

__all__ = ["bootstrap_study_privacy", "synthesize_study_config"]


def bootstrap_study_privacy(study: str, jurisdiction: str) -> dict[str, object]:
    """Idempotent: populate ``<study config root>/<study>/_study_privacy.yaml``
    plus the sidecar PHI HMAC key.

    Moved here from ``harness/run_phi_system.py::_ensure_study_config`` (the
    harness now imports this function instead of owning the logic). Unlike
    the original helper, ``phi_scrub.yaml`` itself is NOT bootstrapped here
    -- it is fully owned by :func:`synthesize_study_config`, which
    regenerates it every run from the current classification set.
    """
    from phi_engine.security import phi_scrub

    defaults_dir = Path(config.CONFIG_DEFAULTS_DIR)
    out_dir = Path(config.study_config_dir(study))
    out_dir.mkdir(parents=True, exist_ok=True)

    privacy_path = out_dir / "_study_privacy.yaml"
    privacy_created = False
    if not privacy_path.is_file():
        default_privacy = (defaults_dir / "_study_privacy.yaml").read_text(encoding="utf-8")
        content = default_privacy.replace(
            "jurisdictions:\n  - USA\n",
            f"jurisdictions:\n  - {jurisdiction}\n",
        )
        # The packaged template's data_as_of is a literal "YYYY-MM-DD"
        # placeholder -- load_study_privacy_config raises on any non-ISO,
        # non-absent value, so it must be resolved here rather than left
        # for a maintainer to notice after a run already fails. Stamped
        # with the bootstrap date (an honest "as of first run" default,
        # correctable by a maintainer at any time before the next run).
        content = content.replace(
            'data_as_of: "YYYY-MM-DD"',
            f'data_as_of: "{_dt.now(_tz.utc).strftime("%Y-%m-%d")}"',
        )
        privacy_path.write_text(content, encoding="utf-8")
        privacy_created = True

    key_path = Path(config.PHI_KEY_PATH)
    key_created = False
    if not key_path.is_file():
        phi_scrub.bootstrap_key(key_path)
        key_created = True

    return {
        "config_dir": str(out_dir),
        "study_privacy_yaml": str(privacy_path),
        "study_privacy_yaml_created_this_run": privacy_created,
        "phi_key_path": str(key_path),
        "phi_key_created_this_run": key_created,
    }


def _anchor(header: str) -> str:
    return f"^{re.escape(header)}$"


def _id_label(header: str) -> str:
    """3-5 uppercase ASCII chars, mirroring the packaged convention
    (SUBJ, FAM, SCRN, ...). Falls back to ``GEN`` for a header with no
    letters at all (defensive; header names are expected to be identifiers)."""
    alnum = re.sub(r"[^A-Za-z]", "", header).upper()
    return alnum[:5] or "GEN"


def synthesize_study_config(
    study: str,
    jurisdiction: str,
    classifications: Iterable[HeaderClassification],
    *,
    generalization_map_overlay: Mapping[str, Mapping[str, str]] | None = None,
) -> Path:
    """Regenerate ``<study>/phi_scrub.yaml`` from the CURRENT classification set.

    Starts from the packaged ``_defaults/phi_scrub.yaml`` and appends each
    classified header to the section matching its action:
    ``jitter_date -> date_fields``, ``pseudonymize -> id_fields``,
    ``drop -> drop_fields``, ``cap -> cap_fields`` (packaged age-cap
    threshold/label default), ``generalize -> generalize_fields``,
    ``suppress -> suppress_small_cell_fields``, ``keep -> keep_fields``.

    A GENERALIZE-classified header has no value-level taxonomy available
    from header classification alone, so it is bound to a fresh, EMPTY
    ``generalization_maps`` entry -- ``generalize_value`` fails closed on any
    unmapped non-empty value (quarantine, never published raw;
    ``phi_scrub.py`` rung 5), so the column is held for human curation of a
    real mapping rather than silently published or silently dropped.
    """
    defaults_path = Path(config.CONFIG_DEFAULTS_DIR) / config.PHI_SCRUB_CONFIG_FILENAME
    raw: dict[str, object] = yaml.safe_load(defaults_path.read_text(encoding="utf-8")) or {}

    keep_fields = list(raw.get("keep_fields") or [])
    date_fields = list(raw.get("date_fields") or [])
    id_fields = list(raw.get("id_fields") or [])
    drop_fields = list(raw.get("drop_fields") or [])
    cap_fields = list(raw.get("cap_fields") or [])
    generalize_fields = list(raw.get("generalize_fields") or [])
    suppress_fields = list(raw.get("suppress_small_cell_fields") or [])
    generalization_maps = dict(raw.get("generalization_maps") or {})

    age_cap = raw.get("age_cap") or {}
    default_cap_threshold = int(age_cap.get("threshold", 89)) if isinstance(age_cap, dict) else 89
    default_cap_label = str(age_cap.get("label", "90+")) if isinstance(age_cap, dict) else "90+"

    seen_headers: set[str] = set()
    for item in classifications:
        header = item.header
        if header in seen_headers:
            continue
        seen_headers.add(header)
        pattern = _anchor(header)
        action = item.action

        if action == Action.KEEP:
            # Deliberately NOT added to keep_fields. keep_fields is
            # priority-1 (checked FIRST, short-circuits every other rule,
            # including id_fields/date_fields/drop_fields). A header with NO
            # matching pattern anywhere is ALREADY published unchanged by
            # default -- that already IS "keep", no rule needed. Adding an
            # explicit exact-match keep_fields entry for every KEEP-
            # classified header would instead ACTIVELY OVERRIDE any more
            # specific packaged pattern that correctly protects that exact
            # header today (discovered via the standalone-refactor stress
            # test: IC_SCRNNUM is classified 'keep' by the pinned USA
            # regulation rules -- phi_review has no scrn-num-specific rule --
            # but the packaged defaults' id_fields pattern
            # ``^I[CS]_SCRNNUM$`` already pseudonymizes it; forcing it into
            # keep_fields would have silently published it raw).
            continue
        elif action == Action.JITTER_DATE:
            date_fields.append(pattern)
        elif action == Action.PSEUDONYMIZE:
            id_fields.append({"pattern": pattern, "label": _id_label(header)})
        elif action == Action.DROP:
            drop_fields.append(pattern)
        elif action == Action.CAP:
            cap_fields.append(
                {"pattern": pattern, "threshold": default_cap_threshold, "label": default_cap_label}
            )
        elif action == Action.SUPPRESS:
            # Deliberately DOES NOT append to suppress_small_cell_fields.
            # phi_scrub.run_scrub has a documented dual-path for SUPPRESS
            # (Note 32, phi_scrub.py:2844-2852): a header is force-dropped by
            # DEFAULT purely from its classification action=='suppress' in
            # the approval JSON -- UNLESS cfg.field_is_suppress_small_cell(h)
            # is True, which opts it into small-cell clamping instead. Header
            # classification alone cannot tell a free-text NOTES column from
            # a numeric household-contact-count column, so the safe default
            # (force-drop, never publish raw) is achieved by adding NOTHING
            # here -- adding an exact-match pattern would have flipped every
            # SUPPRESS header into the small-cell-clamp path, which passes a
            # non-numeric value through UNCHANGED (clamp is a no-op on
            # strings) -- silently publishing free text raw. A human curator
            # can opt a specific numeric column into clamping via a review
            # decision (Phase 4) or a manual suppress_small_cell_fields edit.
            pass
        elif action == Action.GENERALIZE:
            map_name = f"_synth_generalize_{header.lower()}"
            overlay_map = (generalization_map_overlay or {}).get(map_name)
            if overlay_map:
                # Eligible dictionary/mapping support supplied this header's value
                # taxonomy -> fill the map so the GENERALIZE column can publish as
                # categories instead of staying fail-closed (quarantined).
                generalization_maps[map_name] = dict(overlay_map)
            else:
                generalization_maps.setdefault(map_name, {})
            generalize_fields.append({"pattern": pattern, "mapping": map_name})
        # No BAND action exists in the classification Action enum -- band
        # rules are scrub-config-only and never reached via classification.

    raw["keep_fields"] = keep_fields
    raw["date_fields"] = date_fields
    raw["id_fields"] = id_fields
    raw["drop_fields"] = drop_fields
    raw["cap_fields"] = cap_fields
    raw["generalize_fields"] = generalize_fields
    raw["suppress_small_cell_fields"] = suppress_fields
    raw["generalization_maps"] = generalization_maps
    # Provenance only -- not read by load_scrub_config's known-key parser,
    # but useful for a maintainer inspecting the generated file directly.
    raw["_synthesized_for_jurisdiction"] = jurisdiction

    out_path = Path(config.study_config_path(config.PHI_SCRUB_CONFIG_FILENAME, study=study))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return out_path
