"""MongoDB access using motor. Documents keyed by string ids (uuid4 hex)."""
from __future__ import annotations

import os
from functools import lru_cache

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase


@lru_cache(maxsize=1)
def get_db() -> AsyncIOMotorDatabase:
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return client[os.environ["DB_NAME"]]


# collections:
#   sessions -> Session
