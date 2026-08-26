"""Phase 4 audit-zone deny helper (+ W1 snapshot-root deny).

Audit-zone deny — two checks (defense in depth):
1. realpath escape check - denies any path that resolves into
   ``output/*/audit/<...>``.
2. .gitattributes audit-attr check - denies any path tagged with
   ``report-ai-portal-no-llm=true`` per repo .gitattributes.

Either signal triggers ``PermissionError``. Both must pass for allow.

Snapshot-root deny (W1) — :func:`deny_if_snapshot_root` denies any path under
``<OUTPUT_DIR>/<study>/snapshots/<id>/`` that is NOT inside that snapshot's
``<id>/llm_source/`` subtree. The snapshot root holds the run's approval,
verifier report, and manifest (all off-limits to the LLM); only a selected
``<id>/llm_source/`` subtree may be exposed.

Detection is defense-in-depth: it is keyed FIRST on the configured
``config.OUTPUT_DIR`` layout (robust to any layout, including test layouts whose
resolved paths carry no literal ``output`` segment), then falls back to a
literal ``output/<study>/snapshots/<id>/`` segment scan for paths outside the
configured OUTPUT_DIR (e.g. synthetic absolute paths). Either positive signal
denies.
"""

from __future__ import annotations

import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

import phi_engine.config.config as config
from phi_engine.utils.logging_system import get_logger

logger = get_logger(__name__)


_AUDIT_SEGMENT = "audit"
_OUTPUT_SEGMENT = "output"
_SNAPSHOTS_SEGMENT = "snapshots"
_LLM_SOURCE_SEGMENT = "llm_source"


class AuditZoneViolation(PermissionError):  # noqa: N818
    """Raised when a path is rejected for being in the audit zone."""


class SnapshotZoneViolation(PermissionError):  # noqa: N818
    """Raised when a path is rejected for being in the snapshot root zone.

    The snapshot ROOT (``output/<study>/snapshots/<id>/``) holds the run's
    approval report, verifier report, and manifest — all OFF-LIMITS to the LLM.
    Only the ``<id>/llm_source/`` subtree may ever be exposed, and only when a
    maintainer explicitly selects that snapshot (which repoints the agent read
    root). This guard is defence-in-depth: it denies the snapshot root and all
    non-``llm_source`` children regardless of how read roots are configured.
    """


def _is_inside_audit_zone_by_path(path: Path) -> bool:
    real = Path(str(path)).resolve()
    parts = real.parts
    # Look for ``output/<study>/audit/`` anywhere in the resolved path.
    for i, part in enumerate(parts):
        if part == _OUTPUT_SEGMENT and i + 2 < len(parts) and parts[i + 2] == _AUDIT_SEGMENT:
            return True
    return False


def _denied_after_snapshots(parts: tuple[str, ...], snapshots_idx: int) -> bool:
    """Given ``parts`` and the index of the ``snapshots`` segment, decide deny.

    Deny the ``snapshots`` dir itself and the snapshot root + every non-
    ``llm_source`` child; exempt only ``<id>/llm_source/...``. The segment
    immediately after ``<id>`` (i.e. ``snapshots_idx + 2``) must be
    ``llm_source`` to be exempt.
    """
    if snapshots_idx + 1 >= len(parts):
        # ``.../snapshots`` itself — deny.
        return True
    llm_source_idx = snapshots_idx + 2
    return not (llm_source_idx < len(parts) and parts[llm_source_idx] == _LLM_SOURCE_SEGMENT)


def _is_denied_by_output_dir_layout(real: Path) -> bool | None:
    """Layout-aware snapshot-root detection keyed on the *configured* OUTPUT_DIR.

    Resolves ``config.OUTPUT_DIR`` and tests whether *real* sits under
    ``<OUTPUT_DIR>/<study>/snapshots/<id>/`` (and NOT under that
    ``<id>/llm_source/`` subtree). This works regardless of whether the resolved
    path carries a literal ``output`` segment — e.g. tests with
    ``OUTPUT_DIR=tmp_path`` have none, and the legacy literal-segment scan never
    fired there, leaving denial to read-root containment alone.

    Returns ``True`` to deny, ``False`` to exempt (it IS a snapshot path but an
    ``llm_source`` subtree), or ``None`` when the path is not under OUTPUT_DIR /
    OUTPUT_DIR is unavailable — so the caller can fall back to the literal scan.
    """
    output_dir = getattr(config, "OUTPUT_DIR", None)
    if output_dir is None:
        return None
    try:
        output_real = Path(str(output_dir)).resolve()
    except (OSError, ValueError):
        return None
    try:
        rel_parts = real.relative_to(output_real).parts
    except ValueError:
        # Not under OUTPUT_DIR — let the caller fall back to the literal scan.
        return None
    # Layout under OUTPUT_DIR is ``<study>/snapshots/<id>/[llm_source/...]``.
    if len(rel_parts) >= 2 and rel_parts[1] == _SNAPSHOTS_SEGMENT:
        # snapshots is at rel index 1; translate to the helper's convention.
        return _denied_after_snapshots(rel_parts, 1)
    return None


def _is_denied_snapshot_root_path(path: Path) -> bool:
    """True iff *path* is under ``<OUTPUT_DIR>/<study>/snapshots/<id>/`` but NOT
    under that snapshot's ``<id>/llm_source/`` subtree.

    The ``<id>/llm_source/`` subtree is the ONLY LLM-readable part of a snapshot
    (and only once selected). Everything else under the snapshot root — the root
    itself, the approval/verifier/manifest JSON — is denied.

    Defense-in-depth: detection is keyed first on the *configured* OUTPUT_DIR
    layout (robust to any test/prod layout, including one with no literal
    ``output`` segment), then falls back to a literal ``output/<study>/snapshots``
    segment scan for paths outside the configured OUTPUT_DIR (e.g. synthetic
    absolute paths). Either positive signal denies; ``llm_source`` subtrees are
    always exempt.
    """
    real = Path(str(path)).resolve()

    # Primary: configured-layout detection (OUTPUT_DIR-relative).
    layout_decision = _is_denied_by_output_dir_layout(real)
    if layout_decision is not None:
        return layout_decision

    # Fallback: literal ``output/<study>/snapshots/<id>/...`` segment scan, for
    # paths that are not under the configured OUTPUT_DIR.
    parts = real.parts
    for i, part in enumerate(parts):
        # Match ``output/<study>/snapshots/<id>/...`` — snapshots is 2 after output.
        if part == _OUTPUT_SEGMENT and i + 2 < len(parts) and parts[i + 2] == _SNAPSHOTS_SEGMENT:
            return _denied_after_snapshots(parts, i + 2)
    return False


@lru_cache(maxsize=2048)
def _has_no_llm_attribute(path: Path) -> bool:
    """Return True iff `git check-attr` reports the audit attribute set."""

    git_bin = shutil.which("git")
    if git_bin is None:
        return False
    try:
        result = subprocess.run(  # noqa: S603
            [git_bin, "check-attr", config.AUDIT_NO_LLM_ZONE_ATTRIBUTE, str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("zone_guards.attr_check_failed path=%s err=%s", str(path), exc)
        return False
    if result.returncode != 0:
        return False
    # Output: `<path>: <attr>: <value>` - value is `true`, `false`, or `unspecified`.
    line = (result.stdout or "").strip().splitlines()[-1] if result.stdout else ""
    return line.endswith(": true")


def deny_if_audit_zone(path: str | Path) -> None:
    """Raise ``AuditZoneViolation`` if *path* is in the audit zone.

    Two checks; either positive triggers deny.
    """

    p = Path(path)
    if _is_inside_audit_zone_by_path(p):
        raise AuditZoneViolation(f"audit zone read denied (realpath check): {p}")
    if _has_no_llm_attribute(p):
        raise AuditZoneViolation(f"audit zone read denied (gitattributes attr): {p}")


def deny_if_snapshot_root(path: str | Path) -> None:
    """Raise ``SnapshotZoneViolation`` if *path* is in the protected snapshot zone.

    Denies the snapshot root and every non-``llm_source`` child under
    ``output/<study>/snapshots/<id>/`` (the approval/verifier/manifest JSON and
    the ``.NO_LLM_ZONE`` sentinel). The ``<id>/llm_source/`` subtree is exempt
    here; its readability is still gated by the agent read-root containment
    check (only reachable when the snapshot has been explicitly selected).
    """
    p = Path(path)
    if _is_denied_snapshot_root_path(p):
        raise SnapshotZoneViolation(f"snapshot root read denied (realpath check): {p}")
