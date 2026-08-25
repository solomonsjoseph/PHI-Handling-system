"""Run-scoped opaque identifiers for provider-bound content."""
from __future__ import annotations

import hashlib
import hmac
from collections.abc import MutableMapping

from phi_core.crypto import egress_digest_key


class OpaqueLookupError(LookupError):
    """Raised when an opaque token is absent from its run-scoped map."""


class OpaqueMap:
    """Maps canonical identifiers to deterministic, run-scoped opaque tokens."""

    def __init__(self, run_id: str, opaque_map: MutableMapping[str, str]) -> None:
        self._opaque_map = opaque_map
        self._run_key = hmac.new(
            egress_digest_key(), f"opaque-map-v1\0{run_id}".encode("utf-8"), hashlib.sha256
        ).digest()

    def to_opaque(self, kind: str, canonical: str) -> str:
        if not kind or not canonical:
            raise ValueError("opaque identifier kind and canonical value are required")
        digest = hmac.new(
            self._run_key, f"{kind}\0{canonical}".encode("utf-8"), hashlib.sha256
        ).hexdigest()[:16]
        token = f"{kind}_{digest}"
        existing = self._opaque_map.get(token)
        if existing is not None and existing != canonical:
            raise OpaqueLookupError("opaque identifier collision")
        self._opaque_map[token] = canonical
        return token

    def from_opaque(self, token: str) -> str:
        try:
            return self._opaque_map[token]
        except KeyError as exc:
            raise OpaqueLookupError(f"unknown opaque identifier {token!r}") from exc
