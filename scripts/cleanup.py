"""Repository garbage collection.

Removes every git-ignored artifact from the working tree, which is to say
everything a command can regenerate, and leaves the committed source tree
untouched.

The ignore list is the single source of truth for what counts as garbage:
``.gitignore`` declares it and Git's ignored-file listing collects it.
Untracked files that are *not* ignored are never touched, so uncommitted work
in progress survives a sweep.

Dry run by default, mirroring git clean's own refusal to delete without
--force::

    python scripts/cleanup.py            # list what would go
    python scripts/cleanup.py --apply    # remove it
    python scripts/cleanup.py --all --apply

``--all`` is the ``make distclean`` tier (GNU Coding Standards 7.2.6): it also
drops trees that are regenerable but expensive, such as ``node_modules/``.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Ignored but NOT regenerable: local credentials and editor state. Never
# collected, at any scope.
KEEP_NAMES = {".env", ".credentials", "credentials.json", "aws-credentials.ini"}
KEEP_SUFFIXES = (".env", ".pem", ".key")
KEEP_DIRS = {".vscode"}

# Ignored but NOT regenerable: hand-authored documents that happen to be
# gitignored (size, scope, or personal-content reasons), never a build or
# test artifact a command can recreate. Exact repo-relative path, so a
# same-named file elsewhere is never accidentally protected too. Matched
# the same way as EXPENSIVE_PATHS below. Never collected, at any scope --
# `docs/MASTER_ARCHITECTURE_V2.md` (the durable rewrite master prompt,
# cited by section number across dozens of docstrings) was deleted by an
# earlier `--apply` run before this protection existed; it was only
# recoverable because an agent session transcript had captured the
# original paste verbatim.
KEEP_PATHS = {"docs/MASTER_ARCHITECTURE_V2.md"}

# Ignored and regenerable, but expensive to rebuild: needs npm install or a
# fresh virtualenv. Collected only with --all.
EXPENSIVE_DIRS = {"node_modules", ".venv", "venv", "env", "ENV"}
EXPENSIVE_PATHS = {"frontend/build"}



def _ignored_paths(root: Path, *, directories: bool = False) -> list[str]:
    args = ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"]
    if directories:
        args.append("--directory")
    proc = subprocess.run(
        ["git", "-C", str(root), "-c", "core.quotePath=false", *args],
        check=True, capture_output=True,
    )
    return [os.fsdecode(path) for path in proc.stdout.split(b"\0") if path]


def _repo_root() -> Path:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise SystemExit("cleanup: not inside a git repository")
    return Path(proc.stdout.strip())


def _protected(path: str, *, include_expensive: bool) -> bool:
    segments = [s for s in path.strip("/").split("/") if s]
    if not segments:
        return True
    leaf = segments[-1]
    if leaf == ".env" or leaf.startswith(".env."):
        return True
    if leaf in KEEP_NAMES or leaf.endswith(KEEP_SUFFIXES):
        return True
    if any(s in KEEP_DIRS for s in segments):
        return True
    if path.strip("/") in KEEP_PATHS:
        return True
    if not include_expensive:
        if any(s in EXPENSIVE_DIRS for s in segments):
            return True
        if path.strip("/") in EXPENSIVE_PATHS:
            return True
    return False


def _display_paths(files: list[str], directories: list[str],
                   *, include_expensive: bool) -> list[str]:
    displayed: set[str] = set()
    for directory in directories:
        descendants = [path for path in files if path.startswith(directory)]
        if _protected(directory, include_expensive=include_expensive):
            continue
        if any(_protected(path, include_expensive=include_expensive)
               for path in descendants):
            displayed.update(
                path for path in descendants
                if not _protected(path, include_expensive=include_expensive)
            )
        else:
            displayed.add(directory)
    displayed.update(
        path for path in files
        if not any(path.startswith(directory) for directory in directories)
        and not _protected(path, include_expensive=include_expensive)
    )
    return sorted(displayed)


def _remove_empty_directories(root: Path, directories: list[str],
                              *, include_expensive: bool) -> None:
    for directory in sorted(directories, key=lambda path: path.count("/"),
                            reverse=True):
        candidate = root / directory
        if _protected(directory, include_expensive=include_expensive):
            continue
        for child in sorted(candidate.rglob("*"), key=lambda path: len(path.parts),
                            reverse=True):
            relative = child.relative_to(root).as_posix()
            if (child.is_dir() and not child.is_symlink()
                    and not _protected(relative,
                                       include_expensive=include_expensive)):
                try:
                    child.rmdir()
                except OSError:
                    pass
        try:
            candidate.rmdir()
        except OSError:
            pass

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="scripts/cleanup.py")
    p.add_argument("--apply", action="store_true",
                   help="remove the garbage (default: dry run)")
    p.add_argument("--all", dest="include_expensive", action="store_true",
                   help="also remove node_modules/, .venv/, frontend/build/")
    args = p.parse_args(argv)
    root = _repo_root()

    files = _ignored_paths(root)
    directories = _ignored_paths(root, directories=True)
    garbage = [
        path for path in files
        if not _protected(path, include_expensive=args.include_expensive)
    ]
    kept = {
        path for path in files
        if _protected(path, include_expensive=args.include_expensive)
    }
    kept.update(
        path for path in directories
        if _protected(path, include_expensive=args.include_expensive)
    )
    displayed = _display_paths(
        files, directories, include_expensive=args.include_expensive)
    for path in sorted(kept):
        print(f"keep          {path}")
    verb = "removed      " if args.apply else "would remove "
    for path in displayed:
        print(f"{verb} {path}")

    if args.apply:
        for path in garbage:
            target = root / path
            try:
                if target.is_dir() and not target.is_symlink():
                    target.rmdir()
                else:
                    target.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                continue
        _remove_empty_directories(
            root, directories, include_expensive=args.include_expensive)

    tail = "" if args.apply else "  (dry run; re-run with --apply)"
    print(f"\n{len(garbage)} garbage path(s), {len(kept)} protected{tail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
