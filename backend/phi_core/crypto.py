"""At-rest encryption for the BYO LLM API key.

Uses Fernet (AES-128-CBC + HMAC-SHA256). The key lives in
``APP_ENCRYPTION_KEY`` in ``backend/.env``. If the env var is missing the
first call generates one and persists it to ``backend/.env`` so subsequent
processes decrypt what earlier processes wrote.

Fernet tokens are URL-safe base64 strings; we prefix stored ciphertext with
``fernet:`` so we can distinguish encrypted values from any legacy plaintext
key that may still live in the settings collection.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

_ENC_PREFIX = "fernet:"
_ENV_KEY = "APP_ENCRYPTION_KEY"
_BACKEND_ENV = Path(__file__).resolve().parent.parent / ".env"


class KeyRotated(Exception):
    """Raised when a stored ciphertext cannot be decrypted under the
    current APP_ENCRYPTION_KEY (key rotated, or the row predates
    encryption). Distinct from a bare empty result so callers can tell a
    caller-visible key problem apart from 'nothing stored'."""


def _key_from_env_file() -> str:
    """The last non-empty ``APP_ENCRYPTION_KEY`` assignment in
    ``backend/.env``, or an empty string. Last wins, matching how
    python-dotenv resolves a duplicated name, so this reads the same value
    a normally-started server does."""
    try:
        lines = _BACKEND_ENV.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    found = ""
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith(f"{_ENV_KEY}="):
            continue
        value = stripped.split("=", 1)[1].strip().strip("'\"")
        if value:
            found = value
    return found


def _load_or_create_key() -> bytes:
    val = os.environ.get(_ENV_KEY, "").strip()
    if val:
        return val.encode()
    if os.environ.get("PHI_ENV", "production") != "dev":
        # 4.1 already refuses to boot without APP_ENCRYPTION_KEY in
        # production; this is the same rule enforced at the point of use
        # so a key that goes missing mid-process (unset env, container
        # restart with a different env) fails loudly instead of silently
        # generating a throwaway key that can never decrypt existing rows.
        raise RuntimeError(f"{_ENV_KEY} must be set (PHI_ENV != 'dev')")
    # Before generating anything, look for a key this deployment already
    # wrote. A process that imports phi_core without loading backend/.env
    # (a one-off script, a REPL) used to skip straight to generation and
    # append a fresh key, which became the last assignment in the file and
    # therefore the key the next server start read. Every secret encrypted
    # under the previous key then failed to decrypt, which is what silently
    # destroyed the operator's saved provider key in Settings, repeatedly:
    # backend/.env accumulated 44 APP_ENCRYPTION_KEY lines that way.
    # Reading the file's own last assignment makes the dev fallback
    # idempotent instead of rotating the deployment's key by accident.
    existing = _key_from_env_file()
    if existing:
        os.environ[_ENV_KEY] = existing
        return existing.encode()
    # dev convenience only: generate and persist so the next process finds it.
    generated = Fernet.generate_key()
    try:
        with _BACKEND_ENV.open("a", encoding="utf-8") as f:
            f.write(f"\n{_ENV_KEY}={generated.decode()}\n")
    except OSError:
        # In read-only environments, keep it in-process only.
        pass
    os.environ[_ENV_KEY] = generated.decode()
    return generated


def _cipher() -> Fernet:
    return Fernet(_load_or_create_key())

def egress_digest_key() -> bytes:
    """Derive the dedicated key used to authenticate outbound payload digests."""
    return hmac.new(_load_or_create_key(), b"egress-digest-v1", hashlib.sha256).digest()


def encrypt_api_key(plaintext: str) -> str:
    """Return an encrypted, self-identifying token for the stored key.

    Empty input returns an empty string so downstream ``if doc.get("api_key")``
    checks continue to work.
    """
    if not plaintext:
        return ""
    token = _cipher().encrypt(plaintext.encode()).decode()
    return _ENC_PREFIX + token


def decrypt_api_key(stored: str) -> str:
    """Reverse :func:`encrypt_api_key`. Raises :class:`KeyRotated` when
    ``stored`` cannot be decrypted under the current key -- including a
    row that predates encryption and was never Fernet-wrapped at all;
    there is no plaintext-passthrough fallback."""
    if not stored:
        return ""
    if not stored.startswith(_ENC_PREFIX):
        raise KeyRotated("stored value is not a recognised ciphertext")
    try:
        return _cipher().decrypt(stored[len(_ENC_PREFIX):].encode()).decode()
    except InvalidToken:
        raise KeyRotated("stored value cannot be decrypted under the current key") from None

def encrypt_display_name(plaintext: str) -> str:
    """Encrypt an upload name that may itself contain restricted content."""
    return encrypt_api_key(plaintext)


def decrypt_display_name(stored: str) -> str:
    """Decrypt an owner-scoped upload name without a plaintext fallback."""
    return decrypt_api_key(stored)


def encrypt_opaque_value(plaintext: str) -> str:
    """Encrypt one run-scoped opaque map entry's canonical value (D5).

    Same Fernet primitive as :func:`encrypt_api_key`, matching the
    existing per-use-case-wrapper convention ``encrypt_display_name``
    already sets. ``control.opaque.OpaqueMap`` calls this so
    ``WorkflowRun.opaque_map`` never holds a sensitive header/dataset
    identifier in cleartext at rest -- only the token generation
    (HMAC-SHA256 digest, collision check) stays unchanged."""
    return encrypt_api_key(plaintext)


def decrypt_opaque_value(stored: str) -> str:
    """Reverse :func:`encrypt_opaque_value` without a plaintext fallback."""
    return decrypt_api_key(stored)


def encrypt_reversal_map(payload: dict) -> str:
    """Encrypt the study's reversal key (pseudonym map + salt) at rest.

    Same Fernet primitive as :func:`encrypt_api_key`. There is no per-study
    BYO encryption key today -- only a BYO *LLM provider* key exists in this
    codebase -- so this is server-key encryption, same trust boundary as
    every other Fernet-wrapped secret here. Kept separate from the
    shareable export at all times; never written into ``exports`` or the
    publication bundle.
    """
    import json
    token = _cipher().encrypt(json.dumps(payload).encode()).decode()
    return _ENC_PREFIX + token


def decrypt_reversal_map(stored: str) -> dict:
    """Reverse :func:`encrypt_reversal_map`. Raises :class:`KeyRotated` on a
    stored value that cannot be decrypted under the current key."""
    import json
    if not stored:
        return {}
    if not stored.startswith(_ENC_PREFIX):
        raise KeyRotated("stored value is not a recognised ciphertext")
    try:
        return json.loads(_cipher().decrypt(stored[len(_ENC_PREFIX):].encode()).decode())
    except InvalidToken:
        raise KeyRotated("stored value cannot be decrypted under the current key") from None


def _pseudonym_salt_key() -> bytes:
    """Domain-separated key for :func:`pseudonym_salt` (D6). Same HKDF-label
    pattern as :func:`egress_digest_key` above -- a dedicated sub-key
    derived from the app key under a fixed label, rather than HMAC-ing the
    session id directly under the raw app key, so this function's outputs
    can never be confused with (or algebraically related to) any other
    HMAC computed under the same root key, including the principal-cookie
    signature below."""
    return hmac.new(_load_or_create_key(), b"pseudonym-salt-v1", hashlib.sha256).digest()


def pseudonym_salt(session_id: str) -> str:
    """HMAC-SHA256 of the session id under a dedicated, domain-separated
    key (D6). Never leaves the process, so a bundle recipient cannot
    reproduce the digest."""
    return hmac.new(_pseudonym_salt_key(), session_id.encode(), hashlib.sha256).hexdigest()


_PRINCIPAL_COOKIE_KEY_VERSION = 1
# Mirrors the cookie's own `max_age=43200` (server.py's `/api/auth/session`)
# so the server-side check below enforces the same window the client-side
# cookie attribute only ever hinted at.
_PRINCIPAL_COOKIE_MAX_AGE_S = 43200
# Small forward-clock tolerance: a cookie whose `issued_at` is implausibly
# far in the future (clock skew between processes, or a crafted value) is
# rejected rather than trusted just because the HMAC still checks out.
_PRINCIPAL_COOKIE_CLOCK_SKEW_S = 60


def _principal_cookie_key() -> bytes:
    """Domain-separated key for :func:`sign_principal_cookie` (D6). Same
    HKDF-label pattern as :func:`egress_digest_key`/:func:`_pseudonym_salt_key`
    above: a dedicated sub-key under its own fixed label, so a principal
    cookie signature can never be reused as, or confused with, a
    pseudonym-salt digest or any other HMAC computed under the raw app key."""
    return hmac.new(_load_or_create_key(), b"principal-cookie-v1", hashlib.sha256).digest()


def sign_principal_cookie(principal: str) -> str:
    """Return a self-verifying cookie value:
    ``<principal>.<issued_at>.<key_version>.<hmac-hex>`` (D6).

    No server-side session table is needed -- the HMAC under a
    domain-separated key is the only thing that makes the cookie
    trustworthy, so it never leaves the process and a forged principal is
    rejected on the next request rather than silently trusted. Signing
    ``principal|issued_at|key_version`` (rather than the bare principal, as
    before D6) lets :func:`verify_principal_cookie` enforce the cookie's
    age itself instead of relying solely on the browser-side `max_age`
    cookie attribute, which is only ever a client-supplied hint."""
    issued_at = str(int(time.time()))
    key_version = str(_PRINCIPAL_COOKIE_KEY_VERSION)
    payload = f"{principal}|{issued_at}|{key_version}"
    sig = hmac.new(_principal_cookie_key(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{principal}.{issued_at}.{key_version}.{sig}"


def verify_principal_cookie(value: str, *, max_age_s: int = _PRINCIPAL_COOKIE_MAX_AGE_S) -> str | None:
    """Reverse :func:`sign_principal_cookie`. Returns the principal name on
    a valid, sufficiently-recent signature, ``None`` otherwise (malformed,
    forged, tampered, wrong key version, or expired).

    D6: age is enforced here, server-side, against the wall clock at
    verification time -- not merely inferred from the browser having
    chosen to still send the cookie. ``rsplit(".", 3)`` keeps the same
    tolerance the original single-``rpartition`` design had for a
    principal name that itself contains a literal ``.``: only the three
    rightmost dot-separated fields are ever treated as
    issued_at/key_version/signature."""
    if not value:
        return None
    parts = value.rsplit(".", 3)
    if len(parts) != 4:
        return None
    principal, issued_at_s, key_version_s, sig = parts
    if not principal or not sig:
        return None
    try:
        issued_at = int(issued_at_s)
        key_version = int(key_version_s)
    except ValueError:
        return None
    if key_version != _PRINCIPAL_COOKIE_KEY_VERSION:
        return None
    payload = f"{principal}|{issued_at_s}|{key_version_s}"
    expected = hmac.new(_principal_cookie_key(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    now = int(time.time())
    if issued_at > now + _PRINCIPAL_COOKIE_CLOCK_SKEW_S:
        return None
    if now - issued_at > max_age_s:
        return None
    return principal


_SIGNING_KEY_ENV = "ATTESTATION_SIGNING_KEY"


def _load_signing_key() -> Ed25519PrivateKey | None:
    """Load the Ed25519 private key from ``ATTESTATION_SIGNING_KEY``
    (base64-encoded PKCS8 DER). Returns ``None`` when unset -- the caller
    decides what that means (omit the signature files in dev, refuse to
    boot in production per 4.1)."""
    raw = os.environ.get(_SIGNING_KEY_ENV, "").strip()
    if not raw:
        return None
    try:
        from cryptography.hazmat.primitives.serialization import load_der_private_key
        return load_der_private_key(base64.b64decode(raw), password=None)
    except Exception:
        return None


def signing_public_key_pem() -> str | None:
    """Return the PEM-encoded Ed25519 public key, or ``None`` when no
    signing key is configured."""
    key = _load_signing_key()
    if key is None:
        return None
    pub: Ed25519PublicKey = key.public_key()
    return pub.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode("ascii")


def sign_bytes(payload: bytes) -> str | None:
    """Return a base64 Ed25519 signature over ``payload``, or ``None`` when
    no signing key is configured."""
    key = _load_signing_key()
    if key is None:
        return None
    return base64.b64encode(key.sign(payload)).decode("ascii")
