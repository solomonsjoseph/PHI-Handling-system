"""Guided study-setup wizard (Note 11, Half B) — author the per-study config.

Half A (the authoritative YAML + fail-closed phase-0 validation) already exists;
this is the optional convenience front-end that INTERVIEWS the maintainer and
WRITES ``config/<study>/_study_privacy.yaml`` + ``config/<study>/_forms_manifest.yaml``
so they don't hand-edit YAML. The pipeline still re-validates at phase 0, so the
wizard is a guardrail (catch typos up front), not a gatekeeper.

Design: the decisions are PURE, deterministic, unit-testable functions
(``discover_datasets``, ``available_*``, ``suggest_form_classification``,
``build_*``, ``validate_*``, ``write_configs``); the interactive ``input()``/
``print()`` loop is a thin shell over them (injectable I/O for tests).

Reads only file NAMES + config; never a dataset row value (GR-1).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

import config

# Dataset extensions the pipeline ingests; lock/temp files are ignored.
_DATASET_EXTS = (".xlsx", ".csv")
_IGNORE_PREFIXES = ("~$", ".~")
# Filename shapes that usually mark a superseded/duplicate copy.
_DUP_SUFFIX_RE = re.compile(r"(_final|_copy|_v\d+|_\d+|\bcopy\b|-\d+)$", re.IGNORECASE)


def discover_datasets(study: str) -> list[str]:
    """Return the sorted dataset file NAMES under data/raw/<study>/datasets/.

    Lock/temp files (``~$…``) and non-dataset extensions are skipped. Never reads
    file contents — names only.
    """
    datasets_dir = Path(config.RAW_DATA_DIR) / study / "datasets"
    if not datasets_dir.is_dir():
        return []
    names = [
        p.name
        for p in datasets_dir.iterdir()
        if p.is_file()
        and p.suffix.lower() in _DATASET_EXTS
        and not p.name.startswith(_IGNORE_PREFIXES)
    ]
    return sorted(names)


def available_jurisdictions() -> list[str]:
    """The jurisdictions the rulebook supports (offered as choices)."""
    from scripts.security.phi_review import _SUPPORTED_JURISDICTIONS

    return sorted(_SUPPORTED_JURISDICTIONS)


def available_postures() -> list[str]:
    """The compliance postures the scrub engine supports (offered as choices)."""
    try:
        from scripts.security.phi_scrub import (
            _POSTURE_LIMITED_DATASET,
            _POSTURE_SAFE_HARBOR,
        )

        return [_POSTURE_SAFE_HARBOR, _POSTURE_LIMITED_DATASET]
    except Exception:
        return ["safe_harbor", "limited_dataset"]


def _normalize_stem(name: str) -> str:
    """Normalize a filename for duplicate grouping (lower, strip ext + punctuation)."""
    stem = Path(name).stem.lower()
    return re.sub(r"[^a-z0-9]+", "", stem)


def suggest_form_classification(files: list[str]) -> dict[str, str]:
    """Heuristic Required/Reject suggestion per file (the maintainer confirms).

    Rejects a file that (a) normalizes to the same stem as an earlier file
    (probable duplicate) or (b) carries a superseded-copy suffix (``_final``,
    ``_v2``, ``_1``, ``copy``). Everything else is suggested Required. Pure hint —
    over-protection is fine since the human reviews each line.
    """
    suggestions: dict[str, str] = {}
    seen_stems: set[str] = set()
    for name in sorted(files):
        norm = _normalize_stem(name)
        base_norm = _DUP_SUFFIX_RE.sub("", Path(name).stem.lower())
        base_norm = re.sub(r"[^a-z0-9]+", "", base_norm)
        if (
            norm in seen_stems
            or (base_norm != norm and base_norm in seen_stems)
            or _DUP_SUFFIX_RE.search(Path(name).stem)
        ):
            suggestions[name] = "reject"
        else:
            suggestions[name] = "required"
        seen_stems.add(norm)
        seen_stems.add(base_norm)
    return suggestions


def build_privacy_config(
    *,
    jurisdictions: list[str],
    data_as_of: str | None = None,
    rule_refresh: str = "pinned_only",
    conflict_policy: str = "strictest_wins",
    kanon_publish_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose the _study_privacy.yaml mapping (does not write).

    NOTE: ``compliance_posture`` is intentionally NOT written here — the scrub
    engine reads it from ``phi_scrub.yaml`` (``load_scrub_config``), not from
    ``_study_privacy.yaml``. The wizard writes the posture via
    :func:`write_scrub_override` so the maintainer's choice actually takes effect
    (writing it here would be silently ignored — the N11 posture bug).
    """
    cfg: dict[str, Any] = {
        "jurisdictions": [j.upper() for j in jurisdictions],
        "rule_refresh": rule_refresh,
        "conflict_policy": conflict_policy,
        "approval": {"mode": "hybrid", "max_synthetic_attempts": 5},
    }
    if data_as_of:
        cfg["data_as_of"] = data_as_of
    if kanon_publish_gate:
        cfg["kanon_publish_gate"] = kanon_publish_gate
    return cfg


def write_scrub_override(study: str, posture: str, *, force: bool = False) -> Path:
    """Write config/<study>/phi_scrub.yaml pinning the chosen compliance_posture.

    The scrub engine reads ``compliance_posture`` from phi_scrub.yaml (merged over
    the defaults), so the wizard's posture choice must land HERE to take effect.
    A per-study override carrying only ``compliance_posture`` deep-merges over the
    default rules (it changes the posture, keeps the rules).
    """
    path = Path(config.study_config_path(config.PHI_SCRUB_CONFIG_FILENAME, study=study))
    if path.is_file() and not force:
        raise FileExistsError(f"scrub config already exists ({path}); pass force=True to overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"compliance_posture": posture}, sort_keys=False), encoding="utf-8"
    )
    return path


def limited_dataset_authority_missing(posture: str) -> bool:
    """True when posture is limited_dataset but its required IRB authority note is absent.

    ``limited_dataset`` keeps + jitters birthdates and REQUIRES
    ``authorities/phi_limited_dataset.md``; ``load_scrub_config`` fail-closes
    without it. The wizard warns so the maintainer adds the note before running.
    """
    if posture != "limited_dataset":
        return False
    return not (Path(config.BASE_DIR) / "authorities" / "phi_limited_dataset.md").is_file()


def build_forms_manifest(
    *, required: list[str], optional: list[str], reject: list[str]
) -> dict[str, list[str]]:
    """Compose the _forms_manifest.yaml mapping (does not write)."""
    return {
        "required": sorted(required),
        "optional": sorted(optional),
        "reject": sorted(reject),
    }


def validate_privacy_inputs(
    *, jurisdictions: list[str], posture: str, data_as_of: str | None
) -> list[str]:
    """Return human-readable validation errors for the privacy inputs (live check)."""
    errors: list[str] = []
    avail = set(available_jurisdictions())
    if not jurisdictions:
        errors.append("at least one jurisdiction is required")
    bad = [j for j in jurisdictions if j.upper() not in avail]
    if bad:
        errors.append(
            f"unsupported jurisdiction(s): {', '.join(bad)} (choose from {sorted(avail)})"
        )
    if posture not in available_postures():
        errors.append(f"unsupported compliance_posture: {posture!r}")
    if data_as_of is not None:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", data_as_of):
            errors.append(f"data_as_of must be ISO YYYY-MM-DD; got {data_as_of!r}")
        else:
            # Shape is right — also confirm it's a REAL calendar date (matches the
            # phase-0 gate, so the wizard catches 2024-13-45 up front, not later).
            from datetime import date

            try:
                date.fromisoformat(data_as_of)
            except ValueError:
                errors.append(f"data_as_of is not a real calendar date: {data_as_of!r}")
    return errors


def validate_manifest_inputs(
    study: str, *, required: list[str], optional: list[str], reject: list[str]
) -> list[str]:
    """Return validation errors for the manifest inputs (files exist; no overlap)."""
    errors: list[str] = []
    present = set(discover_datasets(study))
    buckets = {"required": required, "optional": optional, "reject": reject}
    # A file may not appear in two buckets.
    seen: dict[str, str] = {}
    for bucket, names in buckets.items():
        for name in names:
            if name in seen:
                errors.append(f"{name!r} is in both {seen[name]} and {bucket}")
            seen[name] = bucket
    # Required + optional files must actually exist (reject may name absent junk).
    errors.extend(
        f"{name!r} marked required/optional but not found in datasets dir"
        for name in [*required, *optional]
        if name not in present
    )
    # Every present file should be classified (no silent omission).
    unclassified = sorted(present - set(seen))
    if unclassified:
        errors.append(f"unclassified dataset file(s): {', '.join(unclassified)}")
    return errors


def write_configs(
    study: str,
    privacy: dict[str, Any],
    manifest: dict[str, list[str]],
    *,
    force: bool = False,
) -> tuple[Path, Path]:
    """Write the two config YAMLs; refuse to overwrite existing files unless *force*.

    The existence check ALSO covers the per-study phi_scrub.yaml override (written
    separately by :func:`write_scrub_override`), so the wizard is all-or-nothing:
    if any of the three targets exists, nothing is written (no partial config).
    """
    privacy_path = Path(config.study_config_path("_study_privacy.yaml", study=study))
    manifest_path = Path(config.study_config_path("_forms_manifest.yaml", study=study))
    scrub_path = Path(config.study_config_path(config.PHI_SCRUB_CONFIG_FILENAME, study=study))
    if not force:
        existing = [p for p in (privacy_path, manifest_path, scrub_path) if p.is_file()]
        if existing:
            raise FileExistsError(
                f"config already exists ({', '.join(str(p) for p in existing)}); "
                "pass force=True to overwrite"
            )
    privacy_path.parent.mkdir(parents=True, exist_ok=True)
    privacy_path.write_text(yaml.safe_dump(privacy, sort_keys=False), encoding="utf-8")
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return privacy_path, manifest_path


def scaffold_manifest_draft(study: str) -> dict[str, Any]:
    """Propose required/optional/reject from directory listing + duplicate heuristics (N11).

    Manual maintainer YAML always wins — this draft is for review at scale (10k+ forms).
    Never consumed by ``make study``; write ``*.scaffold.yaml`` sidecars only.
    """
    files = discover_datasets(study)
    suggestions = suggest_form_classification(files)
    required = sorted(name for name, kind in suggestions.items() if kind == "required")
    optional = sorted(name for name, kind in suggestions.items() if kind == "optional")
    reject = sorted(name for name, kind in suggestions.items() if kind == "reject")
    return {
        "_scaffold_note": (
            "Auto-generated manifest draft — review every entry; merge into "
            "_forms_manifest.yaml manually. Pipeline never auto-edits config during make study."
        ),
        "required": required,
        "optional": optional,
        "reject": reject,
    }


def scaffold_date_locales_outline(study: str) -> dict[str, Any]:
    """Return an empty ``date_locales`` sidecar template for maintainer fill-in (N11).

    Full generation from a header/dictionary scan is planned; this stub documents the
    expected shape. Keys are column NAMES only — never row values.
    """
    _ = study  # reserved for future header-scan generation
    return {
        "_scaffold_note": (
            "Merge reviewed entries into config/<study>/_forms_manifest.yaml under "
            "date_locales: after maintainer review. Example: COLUMN_NAME: DMY"
        ),
        "date_locales": {},
    }


def write_scaffold_sidecars(study: str, *, force: bool = False) -> tuple[Path, Path]:
    """Write ``*.scaffold.yaml`` drafts under config/<study>/ (never overwrites live config)."""
    cfg_dir = Path(config.CONFIG_DIR) / study
    cfg_dir.mkdir(parents=True, exist_ok=True)
    manifest_scaffold = cfg_dir / "_forms_manifest.scaffold.yaml"
    locales_scaffold = cfg_dir / "date_locales.scaffold.yaml"
    if not force:
        existing = [p for p in (manifest_scaffold, locales_scaffold) if p.is_file()]
        if existing:
            raise FileExistsError(
                f"scaffold sidecar(s) already exist ({', '.join(str(p) for p in existing)}); "
                "pass force=True to overwrite"
            )
    manifest_scaffold.write_text(
        yaml.safe_dump(scaffold_manifest_draft(study), sort_keys=False), encoding="utf-8"
    )
    locales_scaffold.write_text(
        yaml.safe_dump(scaffold_date_locales_outline(study), sort_keys=False), encoding="utf-8"
    )
    return manifest_scaffold, locales_scaffold


# ── interactive shell (thin wrapper over the pure functions) ────────────────


def run_interactive(
    study: str,
    *,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[..., None] = print,
    force: bool = False,
) -> tuple[Path, Path]:
    """Interview the maintainer and write the config (Q&A over the pure core)."""
    p = print_fn
    files = discover_datasets(study)
    p(f"RePORTal study setup — {study}")
    p(f"data/raw/{study}/datasets/: {len(files)} dataset file(s) found")

    # Jurisdictions
    juris_opts = available_jurisdictions()
    p(f"[1] Jurisdictions {juris_opts} — comma list:")
    jurisdictions = [j.strip().upper() for j in input_fn("> ").split(",") if j.strip()]

    # Posture
    posture_opts = available_postures()
    p(f"[2] Compliance posture {posture_opts} (default {posture_opts[0]}):")
    posture = input_fn("> ").strip() or posture_opts[0]

    # data_as_of
    p("[3] data_as_of (YYYY-MM-DD, blank to skip):")
    data_as_of = input_fn("> ").strip() or None

    perr = validate_privacy_inputs(
        jurisdictions=jurisdictions, posture=posture, data_as_of=data_as_of
    )
    if perr:
        raise ValueError("privacy config invalid: " + "; ".join(perr))

    # Forms — smart suggestions, maintainer confirms
    suggestions = suggest_form_classification(files)
    required, optional, reject = [], [], []
    p("[4] Classify each file [R]equired / [O]ptional / [x]Reject:")
    for name in files:
        sug = suggestions[name]
        # Accepting the default (empty input) means the FULL suggestion string
        # (e.g. "reject"), NOT its first letter — otherwise "reject"[:1] == "r"
        # would collide with the [R]equired key and silently flip a reject default
        # to required. An explicit single-letter answer (r/o/x) still works.
        typed = input_fn(f"  {name} ({sug})> ").strip().lower()
        ans = typed or sug
        if ans.startswith("x") or ans == "reject":
            reject.append(name)
        elif ans.startswith("o") or ans == "optional":
            optional.append(name)
        else:
            required.append(name)

    merr = validate_manifest_inputs(study, required=required, optional=optional, reject=reject)
    if merr:
        raise ValueError("manifest invalid: " + "; ".join(merr))

    privacy = build_privacy_config(jurisdictions=jurisdictions, data_as_of=data_as_of)
    manifest = build_forms_manifest(required=required, optional=optional, reject=reject)
    paths = write_configs(study, privacy, manifest, force=force)
    scrub_path = write_scrub_override(study, posture, force=force)
    p(f"✓ wrote {paths[0]}")
    p(f"✓ wrote {paths[1]}")
    p(f"✓ wrote {scrub_path} (compliance_posture: {posture})")
    if limited_dataset_authority_missing(posture):
        p(
            "⚠ compliance_posture 'limited_dataset' requires "
            "authorities/phi_limited_dataset.md — the scrub will FAIL-CLOSE until "
            "you add that IRB authority note."
        )
    return paths
