"""MongoDB access using motor. Documents keyed by string ids (uuid4 hex)."""
from __future__ import annotations

import os
from functools import lru_cache

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase


@lru_cache(maxsize=1)
def get_db() -> AsyncIOMotorDatabase:
    # D8: a down/unreachable mongod fails in ~2s instead of pymongo's
    # default 30s server-selection timeout.
    client = AsyncIOMotorClient(os.environ["MONGO_URL"], serverSelectionTimeoutMS=2000)
    return client[os.environ["DB_NAME"]]


# collections:
#   sessions -> Session
