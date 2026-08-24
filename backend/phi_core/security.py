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

import hmac
import ipaddress
import os
import re
import socket
from typing import Any
from urllib.parse import urlparse

from fastapi import Cookie, Header, HTTPException


ALLOWED_PROVIDERS_DEFAULT = {"anthropic", "openai", "gemini", "openrouter", "chatgpt"}
# Kept behind an explicit env flag because openai_compatible allows a user-
# controlled base_url which is the SSRF vector called out in SEC-003.
ALLOWED_PROVIDERS_WITH_CUSTOM = ALLOWED_PROVIDERS_DEFAULT | {"openai_compatible"}

# Known host allow-list per standard provider. `base_url` if given must
# resolve to one of these hosts for the corresponding provider. Empty tuple
# means "no base_url is accepted" (provider uses SDK defaults).
PROVIDER_HOSTS: dict[str, tuple[str, ...]] = {
    "anthropic": ("api.anthropic.com",),
    "openai": ("api.openai.com",),
    "gemini": ("generativelanguage.googleapis.com",),
    "openrouter": ("openrouter.ai", "api.openrouter.ai"),
    "chatgpt": ("chatgpt.com", "auth.openai.com"),
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

# SEC-003 (audit iteration_18) broadening -- these patterns catch the
# identifier shapes the LLM most commonly echoes back into `reason` /
# `prompt_text` / `reply_text` from PDF forms or dictionaries.
# Every pattern is scoped tight enough to avoid nuking clinical values
# (heart rate, glucose, etc.).
_DATE_RE = re.compile(
    r"\b("
    r"\d{4}-\d{2}-\d{2}"                          # 2024-05-20
    r"|\d{1,2}/\d{1,2}/\d{2,4}"                   # 5/20/24 or 05/20/2024
    r"|\d{1,2}-(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-\d{2,4}"  # 20-May-2024
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}"  # May 20, 2024
    r")\b",
    flags=re.IGNORECASE,
)
_AGE_OVER_89_RE = re.compile(
    # Require an age/years-old context clue so we don't nuke clinical
    # values like glucose 105 or blood pressure 100.
    r"(?:\bage(?:d)?\s+(?:of\s+)?)?(9\d|1[0-1]\d|1[2-9]\d)\s*(?:years?|yrs?|y/o|-?year[-\s]?old)\b"
    r"|\bage(?:d)?\s+(?:of\s+)?(9\d|1[0-1]\d|1[2-9]\d)\b",
    flags=re.IGNORECASE,
)
_STREET_ADDR_RE = re.compile(
    r"\b\d{1,6}[a-z]?\s+[A-Z][\w.\- ]{2,60}?\s+"
    r"(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Dr|Drive|Ln|Lane|Way|"
    r"Ct|Court|Pl|Place|Ter|Terrace|Cir|Circle|Hwy|Highway|Pkwy|Parkway)"
    r"(?:\.|,|\b)",
)
# MRN / account / IMEI-shaped: 6+ digit runs with optional letter prefix
# and dash separators. Deliberately does NOT match short ints like
# blood-pressure or heart-rate values (max 4 digits) so clinical context
# is preserved.
_MRN_RE = re.compile(
    r"\b(?:MRN|mrn|Medical\s?Record(?:\s?Number)?|Chart(?:\s?ID)?|"
    r"Account(?:\s?Number)?|Acct|HP|ACCT|DEV|UID)"
    r"[\s#:\-]*[A-Z0-9]{4,}[\-A-Z0-9]{0,20}\b",
)
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# URL identifiers -- consumer-facing patient portals / uploads / anything
# with a path segment that looks personally targeted (u/, patient/, etc.).
_URL_RE = re.compile(r"\bhttps?://[\w./\-?=&%+#:]{6,200}\b")

# Statutory citations and fixed regulatory vocabulary are never PHI, but the
# detectors above catch them as false positives: "164" in "45 CFR
# §164.514(b)(2)(i)" reads like a phone/MRN digit run, and "Social Security
# Number" / "Safe Harbor" / "Date of Birth" are two-Title-Case-word phrases
# indistinguishable from the _NAME_RE heuristic. Protect both classes of text
# before running the PHI detectors, then restore them verbatim -- this is the
# only way to keep citations and rationale readable at the human-review
# boundary without weakening real PHI detection.
_CITATION_RE = re.compile(
    r"\b\d{1,3}\s*C\.?\s*F\.?\s*R\.?\s*§?\s*\d+(?:\.\d+)*(?:\([a-zA-Z0-9]+\))*",
    re.IGNORECASE,
)
_REGULATORY_TERMS_RE = re.compile(
    r"\b(?:Social Security Number|Social Security|Safe Harbor|"
    r"Protected Health Information|Medical Record Number|Date of Birth|"
    r"Personally Identifiable Information|Privacy Rule|HIPAA)\b",
    re.IGNORECASE,
)


def scrub_persisted_text(text: str) -> str:
    """Redact obvious PHI substrings from a stored string.

    Intentionally over-cautious: false-positive replacements only cost the
    reviewer some context, whereas false-negative leaks the raw identifier.
    Statutory citations and known regulatory terms are protected first so
    they survive the PHI detectors verbatim.
    """
    if not text:
        return text or ""
    protected: list[str] = []

    def _protect(m: re.Match) -> str:
        protected.append(m.group(0))
        return f"\x00{len(protected) - 1}\x00"

    out = _CITATION_RE.sub(_protect, text)
    out = _REGULATORY_TERMS_RE.sub(_protect, out)
    out = _EMAIL_RE.sub("[F]", out)
    out = _PHONE_RE.sub("[D]", out)
    out = _SSN_RE.sub("[G]", out)
    # Order matters: strip MRN-shaped runs BEFORE the date/age regex so
    # something like "MRN 202305" cannot leak the numeric tail as an
    # unrecognised span.
    out = _MRN_RE.sub("[H]", out)
    out = _URL_RE.sub("[N]", out)
    out = _IPV4_RE.sub("[O]", out)
    out = _STREET_ADDR_RE.sub("[B]", out)
    out = _DATE_RE.sub("[C]", out)
    out = _AGE_OVER_89_RE.sub("[C-age]", out)
    out = _NAME_RE.sub("[A]", out)
    for i, original in enumerate(protected):
        out = out.replace(f"\x00{i}\x00", original)
    return out


def scrub_nested(value: Any, _key: str = "") -> Any:
    """Recursively scrub PHI from any string leaf inside a dict/list/scalar.

    Used by read endpoints that return LLM-authored blobs where PHI may
    hide inside arbitrarily nested payloads (e.g. agent-trace messages
    where ``payload`` is a dict of ``prompt_text``/``reply_text``
    strings).

    An allow-list of ``_key`` names is kept UN-scrubbed because those fields
    are audit-trail identifiers that operators need to read verbatim
    (reviewer email, timestamps). PHI could still hide inside such a field
    but the operational cost of scrubbing them outweighs the small risk.
    """
    _AUDIT_KEYS = {
        "reviewer", "reviewed_at",
        "session_review", "id", "session_id", "file_id",
        "ts", "updated_at", "created_at",
    }
    if _key in _AUDIT_KEYS and isinstance(value, str):
        return value
    if isinstance(value, str):
        return scrub_persisted_text(value)
    if isinstance(value, dict):
        return {k: scrub_nested(v, _key=k) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_nested(v, _key=_key) for v in value]
    if isinstance(value, tuple):
        return tuple(scrub_nested(v, _key=_key) for v in value)
    return value


def scrub_decision(decision: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``decision`` with any free-text fields scrubbed."""
    out = dict(decision)
    for k in ("reason", "citation", "notes", "evidence", "reviewer_comment"):
        v = out.get(k)
        if isinstance(v, str):
            out[k] = scrub_persisted_text(v)
    return out

async def require_api_token(
    x_api_token: str | None = Header(default=None),
    phi_session: str | None = Cookie(default=None),
) -> None:
    """Gate every mutating endpoint. No-op when no token is configured.

    Accepts the token via the ``X-API-Token`` header (scripted/test
    clients) or the ``phi_session`` cookie (the browser UI, set by
    ``POST /api/auth/session``).
    """
    principals = token_principals()
    if not principals:
        return
    if x_api_token and any(hmac.compare_digest(x_api_token, tok) for tok in principals):
        return
    from .crypto import verify_principal_cookie
    if phi_session and verify_principal_cookie(phi_session) is not None:
        return
    raise HTTPException(401, "invalid or missing credential")


def token_principals() -> dict[str, str]:
    """Parse ``API_TOKENS`` (``name:token,name2:token2``) plus the legacy
    bare ``API_TOKEN`` (mapped to principal ``operator``) into token ->
    principal name. Returns an empty dict when no token is configured at
    all, which callers treat as "auth disabled" (dev convenience)."""
    out: dict[str, str] = {}
    raw = os.environ.get("API_TOKENS", "").strip()
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        name, _, tok = pair.partition(":")
        name, tok = name.strip(), tok.strip()
        if name and tok:
            out[tok] = name
    legacy = os.environ.get("API_TOKEN", "").strip()
    if legacy:
        out.setdefault(legacy, "operator")
    return out


async def resolve_principal(
    x_api_token: str | None = Header(default=None),
    phi_session: str | None = Cookie(default=None),
) -> str:
    """Resolve the calling principal's name from the request credential.

    Reads, in order: the ``phi_session`` cookie (browser UI), then the
    ``X-API-Token`` header (scripted and test clients). In ``PHI_ENV=dev``
    with no token configured at all, returns the fixed principal ``"dev"``
    so local development needs no setup. Otherwise a missing or
    unrecognised credential is a 401 -- this is the identity gate every
    owner-scoped route depends on.
    """
    principals = token_principals()
    if not principals:
        if os.environ.get("PHI_ENV", "production") == "dev":
            return "dev"
        raise HTTPException(401, "no API token configured")
    from .crypto import verify_principal_cookie
    if phi_session:
        principal = verify_principal_cookie(phi_session)
        if principal is not None:
            return principal
    if x_api_token:
        for tok, name in principals.items():
            if hmac.compare_digest(x_api_token, tok):
                return name
    raise HTTPException(401, "invalid or missing credential")


def resolve_principal_soft(x_api_token: str | None, phi_session: str | None) -> str | None:
    """Best-effort variant of :func:`resolve_principal` for rate-limit
    keying (4.20): returns the resolved principal name or ``None`` instead
    of raising, so a request without credentials still gets a rate-limit
    bucket (keyed by client address by the caller) rather than a 401 from
    the limiter itself -- authorization stays exclusively the job of
    :func:`resolve_principal` / :func:`require_api_token` on the route."""
    principals = token_principals()
    if not principals:
        return "dev" if os.environ.get("PHI_ENV", "production") == "dev" else None
    from .crypto import verify_principal_cookie
    if phi_session:
        principal = verify_principal_cookie(phi_session)
        if principal is not None:
            return principal
    if x_api_token:
        for tok, name in principals.items():
            if hmac.compare_digest(x_api_token, tok):
                return name
    return None


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
