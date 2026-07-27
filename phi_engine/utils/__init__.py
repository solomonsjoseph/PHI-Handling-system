"""Canonical utilities package for RePORT AI Portal.

**What.** Shared infrastructure that the pipeline legs, the PHI surface, the
agent, and the test suite all rely on: centralised logging
(:mod:`.logging_system`), structured error envelopes (:mod:`.errors`),
hardened AMBER-zone staging (:mod:`.secure_staging`), per-run lineage
manifest emission (:mod:`.lineage`), PHI log redaction (:mod:`.log_hygiene`), and
streamed-SHA-256 integrity helpers (:mod:`.integrity`).

**Why.** Downstream code should not need to know which submodule lives where
for the common entry points — a single ``from phi_engine.utils import …`` is
the idiomatic import path. Keeping this module's ``__all__`` tight also
surfaces which symbols belong to the stable runtime contract vs. which
are internal implementation detail.

**How.** Each submodule is independently importable for its full surface
(including private helpers used by tests). This ``__init__`` only re-exports
the symbols that downstream callers should reach for; new submodules can
grow here without upsetting existing call sites.
"""

from __future__ import annotations

# __version__ was sourced from the RePORTaLiN package; stub here for standalone use
__version__ = "2.0.0-phi-engine"

from .errors import RePORTError, format_for_log, format_for_user, wrap
from .integrity import hash_bytes, hash_file
from .lineage import LineageManifestError, emit_lineage_manifest
from .log_hygiene import PHIRedactingFilter, install_phi_redactor
from .logging_system import (
    critical,
    debug,
    error,
    exception,
    get_log_file_path,
    get_logger,
    info,
    reset_logging,
    setup_logger,
    setup_logging,
    success,
    warning,
)
from .secure_staging import (
    prepare_staging,
    resolve_staging_root,
    scoped_umask,
    secure_remove_tree,
)

__all__ = [  # noqa: RUF022 — grouped by concept for readability, not alphabetical
    # Version marker
    "__version__",
    # Error envelopes
    "RePORTError",
    "format_for_log",
    "format_for_user",
    "wrap",
    # Logging
    "critical",
    "debug",
    "error",
    "exception",
    "get_log_file_path",
    "get_logger",
    "info",
    "reset_logging",
    "setup_logger",
    "setup_logging",
    "success",
    "warning",
    # AMBER-zone staging hardening
    "prepare_staging",
    "resolve_staging_root",
    "scoped_umask",
    "secure_remove_tree",
    # Integrity + lineage
    "LineageManifestError",
    "emit_lineage_manifest",
    "hash_bytes",
    "hash_file",
    # PHI log redaction
    "PHIRedactingFilter",
    "install_phi_redactor",
]
