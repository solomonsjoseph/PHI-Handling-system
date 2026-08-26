"""Pre-delete checksum manifest writer and legacy-directory remover.

Called by ``make clean-legacy`` after Phase 5a ensures no live code writes
to output/<study>/staging/ or output/<study>/human_review/.

The manifest captures a SHA-256 fingerprint of every file under the legacy
output sub-trees (``trio_bundle/``, ``staging/``, ``human_review/``) before
deletion, so the deletion is auditable and reversible-by-reconstruction.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from datetime import datetime, timezone
UTC = timezone.utc  # py3.9 compat
from pathlib import Path

import phi_engine.config.config as config
from phi_engine.utils._extraction_io import atomic_write_json
from phi_engine.security.secure_env import assert_output_zone
from phi_engine.utils.logging_system import get_logger

logger = get_logger(__name__)

_LEGACY_DIR_NAMES: tuple[str, ...] = ("trio_bundle", "staging", "human_review")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def write_pre_delete_manifest(*, output_root: Path, manifest_path: Path) -> dict:
    """Write a SHA-256 manifest of all files in legacy dirs to ``manifest_path``."""
    output_root = Path(output_root)
    study = output_root.name
    entries: list[dict] = []

    for dir_name in _LEGACY_DIR_NAMES:
        legacy_dir = output_root / dir_name
        if not legacy_dir.is_dir():
            continue
        entries.extend(
            {
                "path": str(fpath.relative_to(output_root)),
                "sha256": _sha256_file(fpath),
                "size_bytes": fpath.stat().st_size,
            }
            for fpath in sorted(legacy_dir.rglob("*"))
            if fpath.is_file()
        )

    manifest = {
        "schema_version": "1.0",
        "study": study,
        "generated_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "deleted_dirs": list(_LEGACY_DIR_NAMES),
        "deleted_files": entries,
    }
    assert_output_zone(manifest_path.parent)
    atomic_write_json(manifest_path, manifest)
    logger.info(
        "pre-delete manifest: %d files captured for %d legacy dirs",
        len(entries),
        len(_LEGACY_DIR_NAMES),
    )
    return manifest


def delete_legacy_dirs(*, output_root: Path) -> None:
    """Remove legacy output dirs. Caller must call write_pre_delete_manifest first."""
    output_root = Path(output_root)
    for dir_name in _LEGACY_DIR_NAMES:
        target = output_root / dir_name
        if target.is_dir():
            shutil.rmtree(target)
            logger.info("deleted legacy dir: %s", target)
        else:
            logger.debug("legacy dir not present, skipping: %s", target)


def _print_dry_run_plan(*, output_root: Path) -> None:
    """Print what the real run WOULD delete. Read-only."""
    print("\n=== DRY RUN — no files will be deleted ===\n")

    print(f"Legacy directories under {output_root}:")
    for name in _LEGACY_DIR_NAMES:
        target = output_root / name
        if target.is_dir():
            file_count = sum(1 for _ in target.rglob("*") if _.is_file())
            print(f"  [WILL DELETE] {target} ({file_count} files)")
        else:
            print(f"  [skip — not present] {target}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 5b: clean legacy output dirs.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be deleted; do not modify the filesystem (except the manifest).",
    )
    args = parser.parse_args(argv)

    output_root = Path(config.STUDY_OUTPUT_DIR)
    audit_dir = Path(config.STUDY_AUDIT_DIR)

    manifest_path = audit_dir / "lineage_manifest_pre_delete.json"

    logger.info("Phase 5b clean-legacy: study=%s (dry_run=%s)", config.STUDY_NAME, args.dry_run)

    # Manifest write is observational (SHA-256 snapshot) — safe in dry-run.
    # Skip only if audit_dir is absent AND we're in dry-run (avoid creating dirs).
    if audit_dir.is_dir() or not args.dry_run:
        audit_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Writing pre-delete manifest to %s", manifest_path)
        write_pre_delete_manifest(output_root=output_root, manifest_path=manifest_path)

    if args.dry_run:
        _print_dry_run_plan(output_root=output_root)
        print("\n=== END DRY RUN — re-run without --dry-run to execute. ===\n")
        return 0

    logger.info("Deleting legacy output dirs under %s", output_root)
    delete_legacy_dirs(output_root=output_root)

    logger.info("Phase 5b clean-legacy complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
