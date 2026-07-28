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

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


_ENC_PREFIX = "fernet:"
_ENV_KEY = "APP_ENCRYPTION_KEY"
_BACKEND_ENV = Path(__file__).resolve().parent.parent / ".env"


def _load_or_create_key() -> bytes:
    val = os.environ.get(_ENV_KEY, "").strip()
    if val:
        return val.encode()
    # Generate and persist so the next process finds it.
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
    """Reverse :func:`encrypt_api_key`. Falls back to returning the input as
    plaintext if it does not carry the ``fernet:`` prefix (legacy rows)."""
    if not stored:
        return ""
    if not stored.startswith(_ENC_PREFIX):
        # Legacy plaintext row -- return as-is so existing sessions keep working.
        return stored
    try:
        return _cipher().decrypt(stored[len(_ENC_PREFIX):].encode()).decode()
    except InvalidToken:
        return ""
