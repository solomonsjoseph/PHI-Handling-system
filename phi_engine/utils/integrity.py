"""Integrity helpers — streamed SHA-256 hashing for the pipeline integrity chain.

**What.** Two pure functions that produce the hex SHA-256 of a file and of an
arbitrary byte stream in 64 KiB chunks. Extracted from
:mod:`scripts.extraction.dataset_pipeline` and :mod:`scripts.utils.lineage`
where the same logic was duplicated.

**Why.** Every raw input, every staged JSONL, every published trio artifact,
and the lineage manifest itself must be hashable with a stable, memory-
bounded implementation so the NIST SP 800-188 §5.2 integrity chain holds
across stages. A single authoritative helper keeps the hash behaviour
identical everywhere and avoids drift when the chunk size or the hash
algorithm is revisited.

**How.** :func:`hash_file` opens the path in binary mode, reads 64 KiB at a
time, feeds each chunk into a ``hashlib.sha256`` instance, and returns the
lowercase hex digest. :func:`hash_bytes` is the same but takes an in-memory
``bytes``/``bytearray`` buffer — useful for test fixtures and for hashing
small audit payloads without a filesystem round-trip.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "hash_bytes",
    "hash_file",
]

DEFAULT_CHUNK_SIZE = 1 << 16  # 64 KiB — same as the retired per-module constants
"""Streaming read-chunk size. Matches the 2025 guidance for balanced memory
pressure + syscall overhead on modern filesystems."""


def hash_file(path: Path, *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> str:
    """Return lowercase hex SHA-256 of *path* contents, streamed.

    **What.** SHA-256 hex digest of the file at *path*.
    **Why.** Stable integrity anchor for NIST SP 800-188 §5.2; carried in
    every extracted record's ``_provenance.raw_sha256`` and in every
    ``lineage_manifest.json`` input/output entry.
    **How.** Open the path binary, read ``chunk_size`` bytes at a time,
    feed each chunk into ``hashlib.sha256``. Works on arbitrarily large
    files without exhausting memory.
    """
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def hash_bytes(data: bytes | bytearray | memoryview) -> str:
    """Return lowercase hex SHA-256 of an in-memory *data* buffer.

    **What.** SHA-256 hex digest of *data*.
    **Why.** Lets tests seed known fixtures without a filesystem round-trip
    and lets audit payloads hash themselves when no file backing exists.
    **How.** Single ``hashlib.sha256(data).hexdigest()`` call — the buffer
    is already in memory so chunking adds no benefit.
    """
    return hashlib.sha256(bytes(data)).hexdigest()
