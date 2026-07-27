"""Dependency-free study-name validation shared by config.py and pipeline_lock.py.

**Why dependency-free, and why top-level (not under ``phi_engine.utils`` or
``phi_engine.config``).** ``phi_engine.config.config`` validates its own
``STUDY_NAME`` env-var fallback partway through its OWN module-level
execution -- before later constants in that same module (e.g.
``STUDY_LLM_SOURCE_DIR``) are defined. ``phi_engine.utils``' package
``__init__.py`` eagerly imports submodules that transitively import
``phi_engine.config.config`` back (``phi_engine.security.secure_env``, via
``phi_engine.utils.lineage``); if config.py imported anything under
``phi_engine.utils`` at that point in its own execution, Python would hand
back its OWN partially-initialized module object to that transitive
importer, which would then raise ``AttributeError`` reaching for a
not-yet-defined constant -- a genuine import-order circular dependency, not
a hypothetical one (reproduced while developing this module). Living
directly under ``phi_engine`` (whose ``__init__.py`` is a bare docstring)
sidesteps that chain entirely: this module has zero ``phi_engine``
dependencies of its own and is always safely importable, at any point
during config.py's execution, from ``phi_engine/cli/main.py`` at module
scope (unlike every other pipeline/config exception class there, which
stays a lazy import specifically because it CAN depend on config having
finished importing).

Both ``config.py``'s ``STUDY_NAME`` fallback validation and
``pipeline_lock.lock_path_for``/``acquire_pipeline_lock`` reuse exactly this
one contract -- there is no second, divergently-permissive convention.
"""

from __future__ import annotations

import re

# Fixed, value-free public code the CLI prints on a validation failure --
# never the offending name, never a raw exception message.
STUDY_NAME_INVALID_CODE = "invalid_study_name"

# Plain single-segment folder name: starts with an ASCII alnum, then up to
# 127 more ASCII alnum/``.``/``_``/``-`` characters (128 chars total). This
# alone rejects path separators (``/``, ``\``), empty strings, and any name
# starting with ``.`` (so bare ``.``/``..`` traversal segments never match --
# both have a non-alnum first character).
_STUDY_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII)

_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)

__all__ = [
    "STUDY_NAME_INVALID_CODE",
    "InvalidStudyNameError",
    "validate_study_name",
]


class InvalidStudyNameError(ValueError):
    """A study name failed the shared plain-folder-name contract.

    Value-free by contract: every raiser passes only the fixed message
    below, never the offending name, so this exception is always safe to
    print/log without redaction. ``ValueError`` subclass for backward
    compatibility with existing ``except ValueError`` call sites.
    """

    code = STUDY_NAME_INVALID_CODE


def validate_study_name(study_name: object) -> str:
    """Validate *study_name* as a plain, single-segment folder name.

    Rejects (all via one ``InvalidStudyNameError``, never a distinct code
    per case): non-``str`` input, path-like input (any separator or ``..``/
    ``.`` traversal segment), dot-ending names (Windows silently strips a
    trailing dot, which could alias an unrelated existing path), overlong
    names (>128 characters), any character outside ``[A-Za-z0-9._-]``, and
    Windows-reserved device names (``CON``, ``COM1``, ... -- checked
    case-insensitively against the segment before the first ``.``, since
    Windows reserves those names regardless of extension).

    Returns *study_name* unchanged on success.
    """
    if (
        not isinstance(study_name, str)
        or _STUDY_NAME_PATTERN.fullmatch(study_name) is None
        or study_name.endswith(".")
        or study_name.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
    ):
        raise InvalidStudyNameError("study must be a plain folder name, not a path")
    return study_name
