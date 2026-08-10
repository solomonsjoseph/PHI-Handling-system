"""Shared pytest configuration: load backend/.env so EMERGENT_LLM_KEY and
MONGO_URL are visible to unit tests that check integrations directly.

Tests that require the live LLM guard on ``EMERGENT_LLM_KEY``; without the
key they skip. Tests that require Mongo guard on ``MONGO_URL``.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    _ENV = Path(__file__).resolve().parents[1] / ".env"
    if _ENV.exists():
        load_dotenv(_ENV)
except Exception:
    # dotenv is optional; the test-suite must still run in environments
    # where python-dotenv isn't installed.
    pass

# Sensible defaults for local unit tests when the .env file is absent.
os.environ.setdefault("DB_NAME", "phi_handling")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
