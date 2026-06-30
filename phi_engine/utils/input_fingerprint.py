"""Input fingerprint for redundant-run detection (Wave 4 / C5.5, Note 14).

A *full* publish pass is expensive (extraction → scrub → cleanup → publish →
verify → snapshot). When **nothing that can change the published output** has
changed since the last clean snapshot, re-running is pure waste. This module
computes a single content fingerprint over exactly the **scrub-affecting
inputs** so the orchestrator's Phase 0 can short-circuit a redundant run.

**Correctness bias (risk #5).** A fingerprint that is too *narrow* would serve
stale data after a real change — a security/correctness failure. A fingerprint
that is too *broad* merely triggers a spurious re-run — wasteful but safe. We
therefore bias broad: the fingerprint covers the raw data, the Source-Truth
policies, the *effective merged* scrub config, the forms manifest, the study
privacy config, **and** the two code modules whose logic determines the
published column set (``phi_scrub`` and ``phi_review``). If output *could*
change, the fingerprint changes.

Value-free: every component is a SHA-256 of file/dir content — never any row
value. The record written to the audit zone holds hashes only.
"""

from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path

import phi_engine.config.config as config
from phi_engine.utils.integrity import hash_bytes, hash_file
from phi_engine.utils.logging_system import get_logger
from phi_engine.utils.step_cache import hash_directory

__all__ = [
    "InputFingerprint",
    "compute_input_fingerprint",
    "compute_per_form_fingerprint",
    "fingerprint_record_path",
    "is_redundant_run",
    "read_recorded_fingerprint",
    "write_fingerprint_record",
]

_logger = get_logger(__name__)

#: Filename of the fingerprint record under the study audit zone.
FINGERPRINT_RECORD_FILENAME = "input_fingerprint.json"

# Recognized raw dataset extensions. The ordered tuple is the single source of
# truth (deterministic match order in _form_raw_file); the frozenset is the
# content-hash filter. A new extension is added in exactly one place.
_DATA_EXTS_ORDERED = (".xlsx", ".xls", ".csv")
_DATA_EXTS = frozenset(_DATA_EXTS_ORDERED)
_YAML_EXTS = frozenset({".yaml", ".yml"})

# Code modules whose logic determines the published column set. Hashed by source
# file so a scrub/classification code change invalidates a prior fingerprint.
# Note 14 names the detector + date-parsing modules too: a logic change in the PHI
# pattern set or the clinical-date parser changes scrub output, so they must be in
# the fingerprint.
_SCRUB_AFFECTING_MODULES = (
    "scripts.security.phi_scrub",
    "scripts.security.phi_review",
    "scripts.security.phi_patterns",
    "scripts.extraction.io.clinical_dates",
)


def _phi_key_fingerprint_safe() -> str:
    """Value-free PHI key fingerprint (Note 14) — fail-soft to "" when unavailable."""
    try:
        from phi_engine.security.phi_keystore import phi_key_fingerprint

        return phi_key_fingerprint() or ""
    except Exception:
        return ""


def _rulebook_version_safe() -> str:
    """PHI rulebook cache version (Note 14) — fail-soft to "" when unavailable."""
    try:
        from phi_engine.security.phi_rulebook import RULEBOOK_CACHE_VERSION

        return str(RULEBOOK_CACHE_VERSION)
    except Exception:
        return ""


@dataclass(frozen=True)
class InputFingerprint:
    """A content fingerprint of all scrub-affecting inputs for a study."""

    fingerprint: str  # combined SHA-256 over the sorted component hashes
    components: dict[str, str]  # component name -> SHA-256 (or "" when absent)
    study: str


def _dir_component_hash(directory: Path, extensions: frozenset[str]) -> str:
    """SHA-256 over a directory's filtered content (relpath+hash pairs).

    Returns ``""`` when the directory is absent/empty so a missing input is a
    stable, distinguishable component rather than an error.
    """
    try:
        per_file = hash_directory(directory, extensions=extensions)
    except (OSError, ValueError):
        return ""
    if not per_file:
        return ""
    joined = "\n".join(f"{rel}:{sha}" for rel, sha in sorted(per_file.items()))
    return hash_bytes(joined.encode("utf-8"))


def _file_component_hash(path: Path) -> str:
    """SHA-256 of a single file, or ``""`` when absent (fail-soft)."""
    p = Path(path)
    if not p.is_file():
        return ""
    try:
        return hash_file(p)
    except OSError:
        return ""


def _module_source_hash(module_name: str) -> str:
    """SHA-256 of a module's source file, resolved via its import spec.

    Uses ``find_spec`` (not import) so the moved-into-plugin pipeline modules
    are hashed at their real on-disk location without importing heavyweight
    dependencies. Fail-soft to ``""``.
    """
    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, ValueError, ModuleNotFoundError):
        return ""
    if spec is None or not spec.origin or spec.origin == "built-in":
        return ""
    return _file_component_hash(Path(spec.origin))


def _shared_scrub_components(study_name: str) -> dict[str, str]:
    """The scrub-affecting components SHARED across every form in a study.

    These inputs are identical for all forms: the effective merged scrub config,
    the study privacy config, the PHI key fingerprint, the rulebook version, and
    the scrub/classification code modules. Both the whole-study fingerprint and
    the per-form fingerprint (Note 16) build on this single helper so they can
    never disagree about the shared inputs. Value-free, fail-soft.

    Note 14: the PHI key determines pseudonyms and the rulebook version
    determines the rules — both are scrub-affecting, so a key rotation or rulebook
    bump MUST change any fingerprint that includes them (else the redundant-run /
    cache-validity checks would skip a study that actually needs re-scrubbing).
    """
    # Effective merged scrub config hash — the SAME helper run_scrub + assertion 5
    # use, so the fingerprint can never disagree with the applied config.
    try:
        from phi_engine.security.phi_scrub import effective_scrub_config_hash

        scrub_cfg = effective_scrub_config_hash(study_name) or ""
    except Exception:  # pragma: no cover - defensive; phi_scrub import is stable
        _logger.warning("input_fingerprint: effective scrub-config hash unavailable", exc_info=True)
        scrub_cfg = ""

    shared: dict[str, str] = {
        "scrub_config_effective": scrub_cfg,
        "study_privacy": _file_component_hash(Path(config.STUDY_PRIVACY_PATH)),
        "phi_key_fingerprint": _phi_key_fingerprint_safe(),
        "phi_rulebook_version": _rulebook_version_safe(),
    }
    for module_name in _SCRUB_AFFECTING_MODULES:
        shared[f"code:{module_name}"] = _module_source_hash(module_name)
    return shared


def _form_raw_file(stem: str, datasets: Path) -> Path | None:
    """Resolve the single raw dataset file backing a form *stem*, or ``None``."""
    for ext in _DATA_EXTS_ORDERED:
        cand = datasets / f"{stem}{ext}"
        if cand.is_file():
            return cand
    return None


def compute_input_fingerprint(
    *,
    study: str | None = None,
    datasets_dir: Path | None = None,
    sot_dir: Path | None = None,
) -> InputFingerprint:
    """Compute the scrub-affecting input fingerprint for *study*.

    Imports :mod:`phi_scrub` lazily for the effective-config hash so this module
    stays dependency-light for callers that only need the path helpers.
    """
    study_name = study if study is not None else config.STUDY_NAME
    datasets = Path(datasets_dir) if datasets_dir is not None else Path(config.DATASETS_DIR)
    sot = Path(sot_dir) if sot_dir is not None else Path(config.SOT_DIR)

    components: dict[str, str] = {
        "raw_datasets": _dir_component_hash(datasets, _DATA_EXTS),
        "sot": _dir_component_hash(sot, _YAML_EXTS),
        "forms_manifest": _file_component_hash(Path(config.FORMS_MANIFEST_PATH)),
        **_shared_scrub_components(study_name),
    }

    canonical = "\n".join(f"{name}={components[name]}" for name in sorted(components))
    combined = hash_bytes(canonical.encode("utf-8"))
    return InputFingerprint(fingerprint=combined, components=components, study=study_name)


def compute_per_form_fingerprint(
    form: str,
    *,
    study: str | None = None,
    datasets_dir: Path | None = None,
) -> str:
    """Per-form scrub-affecting fingerprint (Note 16 — per-form input fingerprinting).

    Combines the content hash of the single raw dataset file backing *form* (a
    bare stem, e.g. ``9_EEval``, or a filename — a recognized dataset extension is
    stripped) with the SHARED scrub-affecting components
    (:func:`_shared_scrub_components`). Two runs yield the same per-form
    fingerprint iff neither that form's raw bytes NOR any shared scrub input
    changed — letting the crash-recovery readback classify a prior ``complete``
    form as still cache-valid vs. changed.

    Value-free: a SHA-256 over file *bytes* + shared hashes — never a row value.
    Returns ``""`` when the form's raw file is absent (a stable, distinguishable
    component rather than an error).

    This fingerprint drives readback classification + observability; it does NOT
    drive partial/incremental promotion — the publish leg is a whole-leg atomic
    replace that always re-scrubs from raw (fail-closed). See CLAUDE.md §4 and the
    orchestrator SKILL for that accepted deviation.
    """
    study_name = study if study is not None else config.STUDY_NAME
    datasets = Path(datasets_dir) if datasets_dir is not None else Path(config.DATASETS_DIR)
    stem = str(form)
    low = stem.lower()
    for ext in _DATA_EXTS_ORDERED:
        if low.endswith(ext):
            stem = stem[: -len(ext)]
            break
    raw = _form_raw_file(stem, datasets)
    components: dict[str, str] = {"raw_form": _file_component_hash(raw) if raw else ""}
    components.update(_shared_scrub_components(study_name))
    canonical = "\n".join(f"{name}={components[name]}" for name in sorted(components))
    return hash_bytes(canonical.encode("utf-8"))


def fingerprint_record_path(audit_dir: Path) -> Path:
    """Path of the fingerprint record under the (no-LLM) audit zone."""
    return Path(audit_dir) / FINGERPRINT_RECORD_FILENAME


def read_recorded_fingerprint(path: Path) -> str | None:
    """Read the recorded combined fingerprint, or ``None`` (fail-soft)."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _logger.warning("input_fingerprint record unreadable: %s (ignored)", p)
        return None
    if not isinstance(data, dict):
        return None
    fp = data.get("fingerprint")
    return fp if isinstance(fp, str) else None


def write_fingerprint_record(path: Path, fingerprint: InputFingerprint) -> Path:
    """Persist the fingerprint (hashes only) to *path*; return it."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fingerprint": fingerprint.fingerprint,
        "study": fingerprint.study,
        "components": dict(sorted(fingerprint.components.items())),
    }
    p.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return p


def is_redundant_run(current: InputFingerprint, recorded: str | None) -> bool:
    """True iff *current* matches the *recorded* fingerprint (nothing changed)."""
    return recorded is not None and recorded == current.fingerprint
