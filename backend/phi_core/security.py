"""Security middleware and helpers.

* :func:`require_api_token` -- FastAPI dependency used to gate every mutating
  endpoint (POST/PUT/PATCH/DELETE). Reads ``API_TOKEN`` from ``backend/.env``.
  If ``API_TOKEN`` is unset the dependency is a no-op so local development and
  the preview URL remain usable without configuration.
* :func:`validate_llm_base_url` -- SSRF guard. Rejects private-network,
  loopback, link-local, or metadata IPs and non-HTTPS URLs. Optional allow-
  list via ``ALLOWED_LLM_BASE_URL_HOSTS`` (comma-separated hostnames).
* :func:`validate_llm_provider` -- Provider allow-list. ``openai_compatible``
  requires the ``base_url`` allow-list to be set explicitly.
* :func:`streaming_size_guard` -- Async iterator wrapper that raises
  :class:`UploadTooLarge` after ``max_bytes``.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from typing import AsyncIterator
from urllib.parse import urlparse

from fastapi import Header, HTTPException


ALLOWED_PROVIDERS_DEFAULT = {"emergent", "anthropic", "openai", "gemini", "openrouter"}
# Kept behind an explicit env flag because openai_compatible allows a user-
# controlled base_url which is the SSRF vector called out in SEC-003.
ALLOWED_PROVIDERS_WITH_CUSTOM = ALLOWED_PROVIDERS_DEFAULT | {"openai_compatible"}


class UploadTooLarge(HTTPException):
    def __init__(self, max_bytes: int):
        super().__init__(413, f"upload exceeds {max_bytes} byte limit")


def allowed_providers() -> set[str]:
    if _custom_hosts():
        return ALLOWED_PROVIDERS_WITH_CUSTOM
    return ALLOWED_PROVIDERS_DEFAULT


def _custom_hosts() -> set[str]:
    raw = os.environ.get("ALLOWED_LLM_BASE_URL_HOSTS", "").strip()
    if not raw:
        return set()
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def validate_llm_provider(provider: str) -> None:
    if provider not in allowed_providers():
        raise HTTPException(
            400,
            f"provider {provider!r} not allowed. Allowed: {sorted(allowed_providers())}. "
            "Set ALLOWED_LLM_BASE_URL_HOSTS to enable openai_compatible.",
        )


def _is_private_ip(host: str) -> bool:
    """Best-effort SSRF filter. Resolves the host and checks every returned IP."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # If we cannot resolve, treat it as unsafe.
        return True
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return True
        # AWS/GCP metadata service
        if str(ip) in {"169.254.169.254", "fd00:ec2::254"}:
            return True
    return False


def validate_llm_base_url(base_url: str, provider: str) -> None:
    """Enforce https + host allow-list + non-private IP."""
    if not base_url:
        if provider == "openai_compatible":
            raise HTTPException(400, "openai_compatible requires base_url")
        return
    parsed = urlparse(base_url)
    if parsed.scheme not in {"https"}:
        raise HTTPException(400, f"base_url must be https, got {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise HTTPException(400, "base_url missing host")
    allow = _custom_hosts()
    if allow and host not in allow:
        raise HTTPException(400, f"base_url host {host!r} not in ALLOWED_LLM_BASE_URL_HOSTS")
    if _is_private_ip(host):
        raise HTTPException(400, f"base_url host {host!r} resolves to a private/reserved IP")


async def require_api_token(x_api_token: str | None = Header(default=None)) -> None:
    """Gate every mutating endpoint. No-op when API_TOKEN is unset."""
    required = os.environ.get("API_TOKEN", "").strip()
    if not required:
        return
    if not x_api_token or x_api_token != required:
        raise HTTPException(401, "invalid or missing X-API-Token")


async def enforce_upload_size(file, max_bytes: int) -> int:
    """Copy an UploadFile stream in chunks with a hard byte cap.

    Returns the number of bytes written. Kept as a small helper because the
    caller decides where to write (temp file / session dir).
    """
    # Callers pass an already-opened destination; this helper just meters bytes.
    total = 0
    while True:
        chunk = await _read_chunk(file, 1 << 20)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise UploadTooLarge(max_bytes)
        yield chunk


async def _read_chunk(file, n: int) -> bytes:
    # UploadFile.read is async
    return await file.read(n)
