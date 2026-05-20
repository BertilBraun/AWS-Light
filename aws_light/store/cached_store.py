from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from aws_light.store.base import AnyStore

T = TypeVar("T")


@dataclass(frozen=True)
class _CacheEntry(Generic[T]):
    value: T
    expires_at: float


class TTLStoreCache(Generic[T]):
    def __init__(
        self,
        store: AnyStore[T],
        *,
        ttl_seconds: float,
        max_entries: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than 0")
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._store = store
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[str, _CacheEntry[T]] = OrderedDict()

    async def get(self, identifier: str) -> T | None:
        cached = self._fresh_entry(identifier)
        if cached is not None:
            return cached.value

        item = await self._store.get(identifier)
        if item is not None:
            self._remember(identifier, item)
        return item

    async def put(self, identifier: str, item: T) -> None:
        await self._store.put(identifier, item)
        self._remember(identifier, item)

    async def list(self) -> list[T]:
        return await self._store.list()

    async def delete(self, identifier: str) -> None:
        await self._store.delete(identifier)
        self._entries.pop(identifier, None)

    async def exists(self, identifier: str) -> bool:
        cached = self._fresh_entry(identifier)
        if cached is not None:
            return True
        return await self._store.exists(identifier)

    def _fresh_entry(self, identifier: str) -> _CacheEntry[T] | None:
        entry = self._entries.get(identifier)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            self._entries.pop(identifier, None)
            return None
        self._entries.move_to_end(identifier)
        return entry

    def _remember(self, identifier: str, item: T) -> None:
        self._entries[identifier] = _CacheEntry(
            value=item,
            expires_at=self._clock() + self._ttl_seconds,
        )
        self._entries.move_to_end(identifier)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
