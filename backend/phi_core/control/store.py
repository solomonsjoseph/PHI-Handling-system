"""Durable-record storage abstractions for the control plane.

The protocol intentionally exposes document-level compare-and-set operations.
Phase 4 builds leases and outbox transitions from these atomic primitives.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Protocol

from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

Document = dict[str, Any]
Query = Mapping[str, Any]


class ControlStore(Protocol):
    """Minimal asynchronous document store required by control-plane services."""

    async def insert(self, collection: str, document: BaseModel | Mapping[str, Any]) -> None: ...

    async def get_one(self, collection: str, query: Query) -> Document | None: ...

    async def find_many(self, collection: str, query: Query) -> list[Document]: ...

    async def replace_one(self, collection: str, query: Query, replacement: BaseModel | Mapping[str, Any]) -> bool: ...

    async def compare_and_set(
        self,
        collection: str,
        query: Query,
        expected: Query,
        replacement: BaseModel | Mapping[str, Any],
    ) -> bool: ...

    async def delete_one(self, collection: str, query: Query) -> bool: ...


def _plain(value: Any) -> Any:
    """Recursively convert frozen/immutable pydantic container types
    (``MappingProxyType``, ``frozenset``) into plain, JSON/BSON-safe
    containers. ``model_dump(mode="json")`` cannot serialize a
    ``MappingProxyType`` leaf directly, so records with a frozen
    ``Mapping`` field (``CapabilityGrant.tools`` et al.) go through
    ``mode="python"`` first and are normalized here."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, frozenset, set)):
        return [_plain(v) for v in value]
    return value


def _document(value: BaseModel | Mapping[str, Any]) -> Document:
    if isinstance(value, BaseModel):
        return _plain(value.model_dump(mode="python"))
    return _plain(dict(value))


def _matches(document: Mapping[str, Any], query: Query) -> bool:
    return all(document.get(field) == value for field, value in query.items())


class MemoryControlStore:
    """In-process store with the same CAS behavior as the Mongo implementation."""

    def __init__(self) -> None:
        self._collections: dict[str, list[Document]] = {}

    async def insert(self, collection: str, document: BaseModel | Mapping[str, Any]) -> None:
        self._collections.setdefault(collection, []).append(_document(document))

    async def get_one(self, collection: str, query: Query) -> Document | None:
        for document in self._collections.get(collection, []):
            if _matches(document, query):
                return deepcopy(document)
        return None

    async def find_many(self, collection: str, query: Query) -> list[Document]:
        return [deepcopy(document) for document in self._collections.get(collection, []) if _matches(document, query)]

    async def replace_one(self, collection: str, query: Query, replacement: BaseModel | Mapping[str, Any]) -> bool:
        documents = self._collections.get(collection, [])
        for index, document in enumerate(documents):
            if _matches(document, query):
                documents[index] = _document(replacement)
                return True
        return False

    async def compare_and_set(
        self,
        collection: str,
        query: Query,
        expected: Query,
        replacement: BaseModel | Mapping[str, Any],
    ) -> bool:
        documents = self._collections.get(collection, [])
        for index, document in enumerate(documents):
            if _matches(document, query) and _matches(document, expected):
                documents[index] = _document(replacement)
                return True
        return False

    async def delete_one(self, collection: str, query: Query) -> bool:
        documents = self._collections.get(collection, [])
        for index, document in enumerate(documents):
            if _matches(document, query):
                del documents[index]
                return True
        return False


class MongoControlStore:
    """Motor-backed implementation of ``ControlStore``."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._db = db

    async def insert(self, collection: str, document: BaseModel | Mapping[str, Any]) -> None:
        await self._db[collection].insert_one(_document(document))

    async def get_one(self, collection: str, query: Query) -> Document | None:
        document = await self._db[collection].find_one(dict(query))
        return _without_object_id(document)

    async def find_many(self, collection: str, query: Query) -> list[Document]:
        cursor = self._db[collection].find(dict(query))
        return [_without_object_id(document) async for document in cursor]

    async def replace_one(self, collection: str, query: Query, replacement: BaseModel | Mapping[str, Any]) -> bool:
        result = await self._db[collection].replace_one(dict(query), _document(replacement))
        return result.matched_count == 1

    async def compare_and_set(
        self,
        collection: str,
        query: Query,
        expected: Query,
        replacement: BaseModel | Mapping[str, Any],
    ) -> bool:
        match = dict(query)
        match.update(expected)
        result = await self._db[collection].replace_one(match, _document(replacement))
        return result.matched_count == 1

    async def delete_one(self, collection: str, query: Query) -> bool:
        result = await self._db[collection].delete_one(dict(query))
        return result.deleted_count == 1


def _without_object_id(document: Mapping[str, Any] | None) -> Document | None:
    if document is None:
        return None
    result = deepcopy(dict(document))
    result.pop("_id", None)
    return result
