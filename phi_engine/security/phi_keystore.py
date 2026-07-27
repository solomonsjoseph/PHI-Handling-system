"""Process-singleton, role-gated registry for the PHI HMAC key (Wave 3 C1).

Why this module exists
----------------------
The 32-byte HMAC key drives :func:`scripts.security.phi_scrub.pseudo_id`: anyone
holding it can forge the deterministic, form-scoped subject pseudonyms and so
re-link or de-anonymize subjects. Historically every consumer
(``main.py``, the chat CLI, the web UI) called
:func:`scripts.security.phi_scrub.load_key` directly, re-reading and re-decoding
the keyfile on each use and leaving the raw key sitting in an *immutable*
``bytes`` object that cannot be wiped from memory.

``PHIKeyStore`` is the single funnel for key access:

* **Singleton** — the key is read and validated *once* per process and cached,
  so the file-permission / length / hex checks run a single time and repeated
  consumers share one instance.
* **Role-gated** — an ``REPORTAL_PROCESS_ROLE=llm-agent`` process is refused the
  key (:class:`~scripts.security.phi_scrub.PHIKeyAccessDeniedError`). This is the
  key-access analogue of the ledger-write gate in :mod:`scripts.audit.ledger`.
  The gate is enforced at *both* layers (here and in ``load_key`` itself) as
  defense-in-depth.
* **Zeroizable** — the cached master copy lives in a ``bytearray`` so
  :meth:`PHIKeyStore.clear` can overwrite the bytes in place rather than waiting
  for the garbage collector. Callers receive immutable ``bytes`` copies.
* **Fingerprintable** — :meth:`PHIKeyStore.fingerprint` returns
  ``sha256(<raw 32 key bytes>).hexdigest()``, **byte-identical** to the historical
  ``phi_key_fingerprint`` value (``hashlib.sha256(load_key()).hexdigest()``) the
  lineage manifest records, so existing IRB evidence and the rotation detector
  (:mod:`scripts.security.key_rotation`) compare cleanly across the migration.

Dependency direction
--------------------
``phi_keystore`` (shared ``scripts/``) → ``scripts.security.phi_scrub`` (the
low-level file reader, physically in the phi-scrubbing skill, reachable under its
canonical name via the Note-19 migration bridge). ``phi_scrub`` never imports
this module — the wrap is one-way, so there is no import cycle.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import phi_engine.config.config as config
from phi_engine.audit import is_llm_agent
from phi_engine.security.phi_scrub import (
    PHIKeyAccessDeniedError,
)
from phi_engine.security.phi_scrub import (
    load_key as _read_key_file,
)

__all__ = [
    "PHIKeyStore",
    "clear_phi_key",
    "get_phi_key",
    "phi_key_fingerprint",
]


class PHIKeyStore:
    """Per-process singleton holding the PHI HMAC key in zeroizable storage.

    Obtain the shared instance with :meth:`instance`; do not construct directly
    in application code (the constructor is kept public only for test isolation,
    where a fresh, non-global store is occasionally useful).
    """

    _singleton: PHIKeyStore | None = None
    _singleton_lock = threading.Lock()

    def __init__(self) -> None:
        self._key: bytearray | None = None
        self._source_path: Path | None = None
        self._lock = threading.Lock()

    # ── singleton access ─────────────────────────────────────────────────────
    @classmethod
    def instance(cls) -> PHIKeyStore:
        """Return the process-wide singleton, creating it on first use."""
        if cls._singleton is None:
            with cls._singleton_lock:
                if cls._singleton is None:
                    cls._singleton = cls()
        return cls._singleton

    @classmethod
    def reset_singleton(cls) -> None:
        """Drop and zeroize the process singleton (test isolation hook)."""
        with cls._singleton_lock:
            if cls._singleton is not None:
                cls._singleton.clear()
            cls._singleton = None

    # ── loading ──────────────────────────────────────────────────────────────
    def _ensure_loaded(self, path: Path | None) -> bytearray:
        """Load + cache the key once. Re-reads only if the source path changes.

        The role gate is checked here *and* inside ``load_key`` — belt and
        braces, so neither the cached fast-path nor a direct reader call can
        leak the key to an ``llm-agent`` process.
        """
        if is_llm_agent():
            raise PHIKeyAccessDeniedError(
                "PHI HMAC key access refused: REPORTAL_PROCESS_ROLE=llm-agent. "
                "The PHIKeyStore is restricted to the trusted publish/scrub path."
            )
        # Resolve the default to the *actual* configured path so the cache key
        # reflects the real source — switching studies / snapshots (which
        # repoints ``config.PHI_KEY_PATH``) then busts the cache instead of
        # silently serving a stale key.
        resolved = Path(path) if path is not None else Path(config.PHI_KEY_PATH)
        with self._lock:
            # Cache hit only when the (resolved) source matches the cached one.
            if self._key is not None and resolved == self._source_path:
                return self._key
            # Source changed (or first load) — wipe any prior material first.
            self._zeroize_locked()
            raw = _read_key_file(resolved)  # bytes; raises on missing/bad-mode/bad-hex
            self._key = bytearray(raw)
            self._source_path = resolved
            return self._key

    # ── public API ───────────────────────────────────────────────────────────
    def get_key(self, path: Path | None = None) -> bytes:
        """Return an immutable copy of the HMAC key bytes.

        The returned ``bytes`` is a fresh copy; the zeroizable master stays inside
        the store. Pass ``path`` only to read a non-default keyfile (tests); the
        default resolves to ``config.PHI_KEY_PATH`` via the underlying reader.
        """
        return bytes(self._ensure_loaded(path))

    def fingerprint(self, path: Path | None = None) -> str:
        """Return ``sha256(<raw key bytes>).hexdigest()`` — historical-compatible."""
        return hashlib.sha256(self._ensure_loaded(path)).hexdigest()

    def clear(self) -> None:
        """Overwrite the cached key bytes in place and forget the source."""
        with self._lock:
            self._zeroize_locked()

    def _zeroize_locked(self) -> None:
        """Zero the bytearray in place (assumes ``self._lock`` held)."""
        if self._key is not None:
            for i in range(len(self._key)):
                self._key[i] = 0
        self._key = None
        self._source_path = None


# ── module-level conveniences (the funnel most call sites use) ────────────────
def get_phi_key(path: Path | None = None) -> bytes:
    """Return the HMAC key via the process singleton (role-gated, cached)."""
    return PHIKeyStore.instance().get_key(path)


def phi_key_fingerprint(path: Path | None = None) -> str:
    """Return the historical-compatible SHA-256 fingerprint of the HMAC key."""
    return PHIKeyStore.instance().fingerprint(path)


def clear_phi_key() -> None:
    """Zeroize the singleton's cached key material."""
    PHIKeyStore.instance().clear()
