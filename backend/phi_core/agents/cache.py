"""Cache-first web fetch helper used by Statute, Praxis, and Scout agents.

Cache lives in Mongo `web_cache` collection keyed by (topic, jurisdiction).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

REFRESH_DAYS = 7


async def cache_get(db: AsyncIOMotorDatabase, topic: str, jurisdiction: str = "us") -> dict[str, Any] | None:
    doc = await db.web_cache.find_one({"topic": topic, "jurisdiction": jurisdiction}, {"_id": 0})
    if not doc:
        return None
    fetched = datetime.fromisoformat(doc["fetched_at"])
    if datetime.now(timezone.utc) - fetched > timedelta(days=REFRESH_DAYS):
        return None
    return doc


async def cache_put(db: AsyncIOMotorDatabase, topic: str, jurisdiction: str, content: str, source: str = "llm") -> None:
    doc = {
        "topic": topic,
        "jurisdiction": jurisdiction,
        "content": content,
        "source": source,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.web_cache.replace_one({"topic": topic, "jurisdiction": jurisdiction}, doc, upsert=True)
