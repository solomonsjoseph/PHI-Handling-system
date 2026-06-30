"""raw-data-intake (skill 0): sort an unorganized study delivery into the
canonical data/raw/<study>/ layout. Classification is filename + extension
ONLY — no workbook is ever opened (GR-1). Standalone prep, not a DAG phase.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

ANNOTATED_PDFS = "annotated_pdfs"
DATA_DICTIONARY = "data_dictionary"
DATASETS = "datasets"
UNCLASSIFIED = "_unclassified"

#: substrings (case-insensitive) that mark a spreadsheet as the data dictionary.
DICTIONARY_NAME_HINTS = ("mapping", "dictionary", "deb", "codebook")

_DATASET_EXTS = (".xlsx", ".csv")


def classify(filename: str) -> str:
    """Return the target bucket for *filename* by name + extension only."""
    lower = filename.lower()
    if lower.endswith(".pdf"):
        return ANNOTATED_PDFS
    if lower.endswith(_DATASET_EXTS):
        if any(hint in lower for hint in DICTIONARY_NAME_HINTS):
            return DATA_DICTIONARY
        return DATASETS
    return UNCLASSIFIED


def _safe_dest(workdir: Path, name: str) -> tuple[Path, bool]:
    """Return a collision-free destination path and whether a collision occurred."""
    dest = workdir / name
    if not dest.exists():
        return dest, False
    stem = Path(name).stem
    suffix = Path(name).suffix
    n = 1
    while True:
        candidate = workdir / f"{stem}.collid{n}{suffix}"
        if not candidate.exists():
            return candidate, True
        n += 1


# Subdirectories that are pipeline-managed / VCS / junk — never part of a
# delivery. The destination raw tree is excluded separately via exclude_under so
# pointing SRC at data/ (which contains data/raw/) never re-ingests the study.
_IGNORED_DIR_NAMES = frozenset(
    {"snapshots", "output", "tmp", ".git", "__pycache__", "node_modules"}
)
_IGNORED_FILE_NAMES = frozenset({".DS_Store", "Thumbs.db"})


def _is_ignored_source(path: Path, src_root: Path, exclude_under: list[Path]) -> bool:
    """True if *path* is junk, hidden, in a managed subdir, or under a dest tree."""
    if path.name in _IGNORED_FILE_NAMES or path.name.startswith("."):
        return True
    rel_parents = path.relative_to(src_root).parts[:-1]
    if any(part in _IGNORED_DIR_NAMES or part.startswith(".") for part in rel_parents):
        return True
    resolved = path.resolve()
    for ex in exclude_under:
        try:
            resolved.relative_to(Path(ex).resolve())
            return True  # lives under a destination/managed root
        except ValueError:
            continue
    return False


def _iter_source_files(src: Path, exclude_under: list[Path]) -> list[Path]:
    """The source files a directory SRC contributes (filtered), or [src] for a file."""
    src = Path(src)
    if src.is_file():
        return [src]
    return sorted(
        p for p in src.rglob("*") if p.is_file() and not _is_ignored_source(p, src, exclude_under)
    )


def prune_source(src: Path, exclude_under: list[Path] | None = None) -> list[str]:
    """Delete the loose source files that were staged (same filter as stage_source).

    Only files that would be ingested are removed — never anything under
    *exclude_under* (the destination raw tree) or a managed/hidden subdir. The
    SRC root and any non-empty directory are left in place; empty leftover
    subdirectories are best-effort removed. Returns the names deleted.
    """
    src = Path(src)
    exclude_under = exclude_under or []
    pruned: list[str] = []
    for f in _iter_source_files(src, exclude_under):
        try:
            f.unlink()
            pruned.append(f.name)
        except OSError:
            continue
    if src.is_dir():
        # Remove now-empty leftover subdirs bottom-up; rmdir only succeeds on an
        # empty dir, so managed/excluded dirs (which keep their files) are safe.
        subdirs = sorted(
            (p for p in src.rglob("*") if p.is_dir()),
            key=lambda p: len(p.parts),
            reverse=True,
        )
        for d in subdirs:
            try:
                d.rmdir()
            except OSError:
                continue
    return pruned


def stage_source(
    src: Path,
    workdir: Path,
    collisions: list | None = None,
    *,
    exclude_under: list[Path] | None = None,
) -> list[Path]:
    """Copy SRC (file or dir) into WORKDIR and extract any .zip. Non-destructive.

    Returns the flat list of staged regular files (zips extracted, not returned).

    When SRC is a directory it is walked recursively, but pipeline-managed
    subdirs (``_IGNORED_DIR_NAMES``), hidden/junk files, and anything under a
    path in *exclude_under* (e.g. the destination ``data/raw`` tree) are skipped
    — so pointing SRC at ``data/`` files only the loose new files and never
    re-ingests the study's own organized tree or snapshots.

    When two source files share a basename the later file is written under a
    disambiguated name (``stem.collidN.ext``) and its original basename is
    appended to *collisions* (if provided), so no data is silently lost.
    """
    src = Path(src)
    if not src.exists():
        raise FileNotFoundError(f"intake source not found: {src}")
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    exclude_under = exclude_under or []

    sources = _iter_source_files(src, exclude_under)
    for item in sources:
        dest, collided = _safe_dest(workdir, item.name)
        shutil.copy2(item, dest)
        if collided and collisions is not None:
            collisions.append(item.name)

    # Extract any staged zips (one level; extracted zips themselves are dropped).
    for zpath in sorted(workdir.glob("*.zip")):
        with zipfile.ZipFile(zpath) as zf:
            for member in zf.namelist():
                if member.endswith("/"):
                    continue
                orig_name = Path(member).name  # flatten; name + ext is all we need
                dest, collided = _safe_dest(workdir, orig_name)
                with zf.open(member) as fh, open(dest, "wb") as out:
                    shutil.copyfileobj(fh, out)
                if collided and collisions is not None:
                    collisions.append(orig_name)
        zpath.unlink()

    return sorted(p for p in workdir.glob("*") if p.is_file() and p.suffix.lower() != ".zip")


_REAL_BUCKETS = (ANNOTATED_PDFS, DATA_DICTIONARY, DATASETS)
_ALL_BUCKETS = (*_REAL_BUCKETS, UNCLASSIFIED)


@dataclass
class IntakeResult:
    counts: dict = field(default_factory=dict)
    unclassified: list = field(default_factory=list)
    manifest_written: bool = False
    skipped: bool = False
    review_note: str | None = None
    already_present: list = field(default_factory=list)
    pruned: list = field(default_factory=list)
    manifest_gaps: list = field(default_factory=list)


def _validate_study_name(name: str) -> None:
    """Pre-run check: the study name must be a plain folder name, not a path."""
    if not name or not name.strip():
        raise ValueError("study name is empty")
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise ValueError(f"study name must be a plain folder name, not a path: {name!r}")


def _detect_existing_study(raw_root: Path) -> str | None:
    """The first existing study folder under raw_root that has a datasets/ dir."""
    raw_root = Path(raw_root)
    if not raw_root.is_dir():
        return None
    excluded = {".backup", ".DS_Store", "output"}
    for p in sorted(raw_root.iterdir()):
        if p.is_dir() and not p.name.startswith(".") and p.name not in excluded:
            if (p / DATASETS).is_dir():
                return p.name
    return None


def resolve_study_name(
    explicit: str | None,
    *,
    raw_root: Path | None = None,
    env_study_name: str | None = None,
) -> tuple[str, str]:
    """Resolve + validate the target study folder name BEFORE filing anything.

    Precedence: an explicit name (CLI ``--study`` / ``STUDY=``) → an intentional
    ``STUDY_NAME`` env var → an auto-detected existing ``data/raw/<x>/datasets``
    study. If NONE of these resolves, the call **refuses** (raises ``ValueError``)
    rather than inventing a generic default — so a brand-new study is never
    silently filed into another study's folder. Returns ``(name, source)`` where
    source is ``explicit`` | ``detected``. The name is validated (a path-injected
    name like ``../evil`` is rejected up front), so files are never filed into a
    bad folder.
    """
    if explicit and explicit.strip():
        name = explicit.strip()
        _validate_study_name(name)
        return name, "explicit"

    import os

    import config

    env = env_study_name if env_study_name is not None else os.environ.get("STUDY_NAME")
    if env and env.strip():
        name = env.strip()
        _validate_study_name(name)
        return name, "explicit"

    raw_root = Path(raw_root) if raw_root is not None else Path(config.RAW_DATA_DIR)
    detected = _detect_existing_study(raw_root)
    if detected:
        return detected, "detected"

    raise ValueError(
        "no study specified and none detected under data/raw/; "
        "pass STUDY=<name> (or --study) to name the new study folder"
    )


def is_already_organized(raw_study_dir: Path) -> bool:
    """True iff the bucket dirs exist and datasets/ already holds files."""
    raw_study_dir = Path(raw_study_dir)
    if not all((raw_study_dir / b).is_dir() for b in _REAL_BUCKETS):
        return False
    return any((raw_study_dir / DATASETS).iterdir())


def draft_manifest(dataset_names: list, manifest_path: Path) -> bool:
    manifest_path = Path(manifest_path)
    if manifest_path.exists():
        return False  # never clobber a hand-tuned manifest
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# DRAFT — operator must confirm before make study.", "required:"]
    lines += [f"  - {name}" for name in sorted(dataset_names)]
    lines += ["optional: []", "reject: []", ""]
    manifest_path.write_text("\n".join(lines))
    return True


def _manifest_known_names(manifest_path: Path) -> set[str] | None:
    """Names listed under required/optional/reject, or None if absent/unreadable."""
    manifest_path = Path(manifest_path)
    if not manifest_path.exists():
        return None
    import yaml

    try:
        data = yaml.safe_load(manifest_path.read_text()) or {}
    except yaml.YAMLError:
        return None  # malformed — don't guess; skip gap detection
    if not isinstance(data, dict):
        return None
    known: set[str] = set()
    for key in ("required", "optional", "reject"):
        vals = data.get(key) or []
        if isinstance(vals, list):
            known.update(str(v) for v in vals)
    return known


def append_to_manifest_required(manifest_path: Path, names: list[str]) -> list[str]:
    """Append *names* under the manifest's ``required:`` block, append-only.

    Existing entries, ordering, and comments are preserved — new items are
    inserted after the last current ``required:`` item, matching its indentation
    (or converting an inline ``required: []`` to block form). Returns the names
    appended (empty if there is no ``required:`` key to extend).
    """
    manifest_path = Path(manifest_path)
    if not names or not manifest_path.exists():
        return []
    text = manifest_path.read_text()
    lines = text.split("\n")

    req_idx = next((i for i, ln in enumerate(lines) if re.match(r"^required\s*:", ln)), None)
    if req_idx is None:
        return []  # no required: key to extend — never fabricate structure

    inline = lines[req_idx].split(":", 1)[1].strip()
    item_re = re.compile(r"^(\s*)-\s")
    indent = ""
    last_item = req_idx
    found_item = False
    j = req_idx + 1
    while j < len(lines):
        m = item_re.match(lines[j])
        if m:
            indent = m.group(1)
            last_item = j
            found_item = True
            j += 1
            continue
        if lines[j].strip() == "" or lines[j].lstrip().startswith("#"):
            j += 1
            continue
        break  # next top-level key

    if inline.startswith("["):
        # inline list form: rebuild as a block, preserving any inline items
        import yaml

        existing = yaml.safe_load(lines[req_idx].split(":", 1)[1]) or []
        block = ["required:"] + [f"  - {x}" for x in existing] + [f"  - {n}" for n in names]
        lines[req_idx : req_idx + 1] = block
    else:
        use_indent = indent if found_item else ""
        lines[last_item + 1 : last_item + 1] = [f"{use_indent}- {n}" for n in names]

    out = "\n".join(lines)
    manifest_path.write_text(out)
    return list(names)


def write_review_note(audit_dir: Path, unclassified: list[tuple[str, str]]) -> str | None:
    """unclassified: list[(filename, reason_code)]. Count-only; no contents."""
    if not unclassified:
        return None

    from scripts.audit.review_paths import intake_review_path

    note_path = intake_review_path(Path(audit_dir))
    note_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# raw-data-intake review — items needing attention",
        "",
        f"count: {len(unclassified)}",
        "",
        "| file | reason |",
        "| --- | --- |",
    ]
    lines += [f"| {name} | {reason} |" for name, reason in unclassified]
    lines.append("")
    note_path.write_text("\n".join(lines))
    return str(note_path)


def organize(
    study: str,
    src: Path,
    *,
    force: bool = False,
    add: bool = False,
    prune: bool = False,
    raw_root: Path | None = None,
    config_root: Path | None = None,
    audit_dir: Path | None = None,
) -> IntakeResult:
    import config

    raw_root = Path(raw_root) if raw_root is not None else Path(config.RAW_DATA_DIR)
    config_root = Path(config_root) if config_root is not None else Path(config.CONFIG_DIR)
    audit_dir = Path(audit_dir) if audit_dir is not None else Path(config.STUDY_AUDIT_DIR)

    raw_study_dir = raw_root / study
    # ponytail: force bypasses the no-op guard and re-sorts additively (same-named files overwritten); a destructive clean is out of scope — dedup is skill 2
    # add mode files NEW files into an already-organized study without the force
    # rebuild semantics: it never overwrites an existing file (records it as
    # already_present instead) and never re-ingests the study's own buckets
    # (SRC is the inbox). Newly placed datasets missing from an existing manifest
    # are auto-appended to required: + flagged (see below).
    if not force and not add and is_already_organized(raw_study_dir):
        return IntakeResult(skipped=True)

    # Pre-create the canonical buckets so the layout is complete even when a
    # bucket gets no files (e.g. a delivery with no data dictionary). This also
    # makes a re-run correctly no-op via is_already_organized.
    for bucket in _ALL_BUCKETS:
        (raw_study_dir / bucket).mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        collisions: list[str] = []
        # Exclude the destination raw tree so SRC=data (which contains data/raw)
        # never re-ingests the study's own organized files.
        staged = stage_source(Path(src), Path(tmp), collisions=collisions, exclude_under=[raw_root])
        counts = dict.fromkeys(_ALL_BUCKETS, 0)
        unclassified: list = []
        already_present: list = []
        placed_datasets: list[str] = []
        for path in staged:
            bucket = classify(path.name)
            dest_dir = raw_study_dir / bucket
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / path.name
            if add and dest.exists():
                already_present.append(path.name)  # never overwrite in add mode
                continue
            shutil.copy2(path, dest)
            counts[bucket] += 1
            if bucket == UNCLASSIFIED:
                unclassified.append((path.name, "unrecognized_name_or_extension"))
            elif bucket == DATASETS:
                placed_datasets.append(path.name)
        # Record collisions so a human can verify no data was lost.
        collision_entries = [(name, "name_collision") for name in collisions]

    dataset_names = [p.name for p in (raw_study_dir / DATASETS).glob("*") if p.is_file()]
    manifest_path = config_root / study / "_forms_manifest.yaml"
    manifest_written = draft_manifest(dataset_names, manifest_path)

    # When a manifest already exists (not freshly drafted), auto-append any newly
    # placed dataset that it does not yet list under required/optional/reject —
    # append-only, preserving existing entries + comments — so a new form can't
    # silently trip ManifestMismatchError at `make study`. Each appended form is
    # also flagged in the count-only review note for the audit trail.
    manifest_gaps: list[str] = []
    if not manifest_written and placed_datasets:
        known = _manifest_known_names(manifest_path)
        if known is not None:
            gaps = sorted(n for n in placed_datasets if n not in known)
            manifest_gaps = append_to_manifest_required(manifest_path, gaps)
    gap_entries = [(name, "manifest_gap_appended") for name in manifest_gaps]

    review_note = write_review_note(audit_dir, unclassified + collision_entries + gap_entries)

    # Opt-in: remove the loose source files now that they are filed into the raw
    # tree (the skill is copy-by-default; prune is explicit). Never touches the
    # destination raw tree (excluded) — only the loose delivery files in SRC.
    pruned = prune_source(Path(src), exclude_under=[raw_root]) if prune else []

    return IntakeResult(
        counts=counts,
        unclassified=[name for name, _ in unclassified],
        manifest_written=manifest_written,
        skipped=False,
        review_note=review_note,
        already_present=already_present,
        pruned=pruned,
        manifest_gaps=manifest_gaps,
    )
