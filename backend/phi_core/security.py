"""Security middleware and helpers.

* :func:`require_api_token` -- FastAPI dependency used to gate every mutating
  endpoint (POST/PUT/PATCH/DELETE). Reads ``API_TOKEN`` from ``backend/.env``.
  If ``API_TOKEN`` is unset the dependency is a no-op so local development and
  the preview URL remain usable without configuration.
* :func:`validate_llm_base_url` -- SSRF guard + per-provider host allow-list.
  Rejects private-network, loopback, link-local, or metadata IPs and
  non-HTTPS URLs. For standard providers (`openai`, `anthropic`, `gemini`,
  `openrouter`) restricts to the provider's known hostnames; for
  `openai_compatible` requires ``ALLOWED_LLM_BASE_URL_HOSTS``.
* :func:`validate_llm_provider` -- Provider allow-list.
* :func:`scrub_persisted_text` -- Redact PHI substrings from any string that
  will be stored in Mongo (agent decision reasons/citations, herald drafts,
  ledger notes) so read endpoints cannot echo back raw PHI.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
from typing import Any, AsyncIterator
from urllib.parse import urlparse

from fastapi import Header, HTTPException


ALLOWED_PROVIDERS_DEFAULT = {"emergent", "anthropic", "openai", "gemini", "openrouter"}
# Kept behind an explicit env flag because openai_compatible allows a user-
# controlled base_url which is the SSRF vector called out in SEC-003.
ALLOWED_PROVIDERS_WITH_CUSTOM = ALLOWED_PROVIDERS_DEFAULT | {"openai_compatible"}

# Known host allow-list per standard provider. `base_url` if given must
# resolve to one of these hosts for the corresponding provider. Empty tuple
# means "no base_url is accepted" (provider uses SDK defaults / Emergent
# proxy).
PROVIDER_HOSTS: dict[str, tuple[str, ...]] = {
    "emergent": (),
    "anthropic": ("api.anthropic.com",),
    "openai": ("api.openai.com",),
    "gemini": ("generativelanguage.googleapis.com",),
    "openrouter": ("openrouter.ai", "api.openrouter.ai"),
}


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
    """Enforce https + per-provider host allow-list + non-private IP.

    SEC-003 residual: standard providers (openai/anthropic/gemini/openrouter)
    now only accept a `base_url` if it matches the provider's known hosts.
    `openai_compatible` still requires `ALLOWED_LLM_BASE_URL_HOSTS`.
    """
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
    if provider == "openai_compatible":
        allow = _custom_hosts()
        if not allow:
            raise HTTPException(400, "openai_compatible requires ALLOWED_LLM_BASE_URL_HOSTS")
        if host not in allow:
            raise HTTPException(
                400, f"base_url host {host!r} not in ALLOWED_LLM_BASE_URL_HOSTS"
            )
    else:
        provider_allow = set(PROVIDER_HOSTS.get(provider, ())) | _custom_hosts()
        if not provider_allow:
            raise HTTPException(400, f"provider {provider!r} does not accept base_url")
        if host not in provider_allow:
            raise HTTPException(
                400,
                f"base_url host {host!r} not allowed for provider {provider!r}. "
                f"Allowed: {sorted(provider_allow)}",
            )
    if _is_private_ip(host):
        raise HTTPException(400, f"base_url host {host!r} resolves to a private/reserved IP")


# --- SEC-006: scrub PHI from persisted text --------------------------------
#
# The LLM may echo names/phones/emails/SSN it saw in dictionaries or forms
# into its ``reason`` and ``citation`` strings. We store those strings in
# Mongo and serve them via read endpoints, so we must scrub them before
# persistence -- independent of who reads. Uses the same detectors as the
# free-text scrubber but returns HIPAA-category placeholders like ``[A]``.
_PHONE_RE = re.compile(r"\b(?:\+?\d{1,3}[\s\-.]?)?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}\b")
_EMAIL_RE = re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[A-Za-z]{2,}\b")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# Person-name heuristic: two consecutive Title-Case tokens. Kept
# conservative to avoid nuking every capitalised word.
_NAME_RE = re.compile(r"\b(?:Mr|Mrs|Ms|Dr)\.?\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b|\b[A-Z][a-z]{2,}\s+[A-Z][a-z]{2,}\b")


def scrub_persisted_text(text: str) -> str:
    """Redact obvious PHI substrings from a stored string.

    Intentionally over-cautious: false-positive replacements only cost the
    reviewer some context, whereas false-negative leaks the raw identifier.
    """
    if not text:
        return text or ""
    out = _EMAIL_RE.sub("[F]", text)
    out = _PHONE_RE.sub("[D]", out)
    out = _SSN_RE.sub("[G]", out)
    out = _NAME_RE.sub("[A]", out)
    return out


def scrub_decision(decision: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``decision`` with any free-text fields scrubbed."""
    out = dict(decision)
    for k in ("reason", "citation", "notes", "evidence"):
        v = out.get(k)
        if isinstance(v, str):
            out[k] = scrub_persisted_text(v)
    return out


async def require_api_token(
    x_api_token: str | None = Header(default=None),
    token: str | None = None,  # query fallback for EventSource which cannot set headers
) -> None:
    """Gate every mutating endpoint. No-op when API_TOKEN is unset.

    Accepts the token via the ``X-API-Token`` header (preferred) or the
    ``token`` query parameter (only path EventSource can use because the
    browser SSE API does not allow custom headers).
    """
    required = os.environ.get("API_TOKEN", "").strip()
    if not required:
        return
    provided = x_api_token or token
    if not provided or provided != required:
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
