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
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
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
        raise KeyRotated("stored value cannot be decrypted under the current key")


def pseudonym_salt(session_id: str) -> str:
    """HMAC-SHA256 of the session id under the server-held key. Never leaves
    the process, so a bundle recipient cannot reproduce the digest."""
    return hmac.new(_load_or_create_key(), session_id.encode(), hashlib.sha256).hexdigest()


def sign_principal_cookie(principal: str) -> str:
    """Return a self-verifying cookie value: ``<principal>.<hmac-hex>``.

    No server-side session table is needed -- the HMAC under the
    server-held key is the only thing that makes the cookie trustworthy,
    so it never leaves the process and a forged principal is rejected on
    the next request rather than silently trusted.
    """
    sig = hmac.new(_load_or_create_key(), principal.encode(), hashlib.sha256).hexdigest()
    return f"{principal}.{sig}"


def verify_principal_cookie(value: str) -> str | None:
    """Reverse :func:`sign_principal_cookie`. Returns the principal name on
    a valid signature, ``None`` otherwise (malformed, forged, or tampered)."""
    if not value or "." not in value:
        return None
    principal, _, sig = value.rpartition(".")
    if not principal or not sig:
        return None
    expected = hmac.new(_load_or_create_key(), principal.encode(), hashlib.sha256).hexdigest()
    if hmac.compare_digest(sig, expected):
        return principal
    return None


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
