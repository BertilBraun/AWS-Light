from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from aws_light.store.cached_store import TTLStoreCache


class Item(BaseModel):
    name: str
    value: int


@dataclass
class MutableClock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now


class FakeStore:
    def __init__(self, items: dict[str, Item] | None = None) -> None:
        self.items = dict(items or {})
        self.get_calls: list[str] = []
        self.exists_calls: list[str] = []
        self.list_calls = 0

    async def get(self, identifier: str) -> Item | None:
        self.get_calls.append(identifier)
        return self.items.get(identifier)

    async def put(self, identifier: str, item: Item) -> None:
        self.items[identifier] = item

    async def list(self) -> list[Item]:
        self.list_calls += 1
        return list(self.items.values())

    async def delete(self, identifier: str) -> None:
        self.items.pop(identifier, None)

    async def exists(self, identifier: str) -> bool:
        self.exists_calls.append(identifier)
        return identifier in self.items


async def test_get_reuses_cached_item_until_ttl_expires() -> None:
    clock = MutableClock()
    store = FakeStore({"svc": Item(name="svc", value=1)})
    cached = TTLStoreCache(store, ttl_seconds=5.0, max_entries=8, clock=clock)

    assert await cached.get("svc") == Item(name="svc", value=1)
    store.items["svc"] = Item(name="svc", value=2)
    assert await cached.get("svc") == Item(name="svc", value=1)

    clock.now = 5.1
    assert await cached.get("svc") == Item(name="svc", value=2)
    assert store.get_calls == ["svc", "svc"]


async def test_get_evicts_least_recently_used_item_when_full() -> None:
    store = FakeStore(
        {
            "a": Item(name="a", value=1),
            "b": Item(name="b", value=2),
            "c": Item(name="c", value=3),
        }
    )
    cached = TTLStoreCache(store, ttl_seconds=60.0, max_entries=2)

    assert await cached.get("a") == Item(name="a", value=1)
    assert await cached.get("b") == Item(name="b", value=2)
    assert await cached.get("a") == Item(name="a", value=1)
    assert await cached.get("c") == Item(name="c", value=3)
    assert await cached.get("b") == Item(name="b", value=2)

    assert store.get_calls == ["a", "b", "c", "b"]


async def test_put_updates_cached_item_and_delete_invalidates_it() -> None:
    store = FakeStore({"svc": Item(name="svc", value=1)})
    cached = TTLStoreCache(store, ttl_seconds=60.0, max_entries=8)

    assert await cached.get("svc") == Item(name="svc", value=1)
    await cached.put("svc", Item(name="svc", value=2))
    assert await cached.get("svc") == Item(name="svc", value=2)

    await cached.delete("svc")
    assert await cached.get("svc") is None
    assert store.get_calls == ["svc", "svc"]


async def test_exists_uses_cached_item_but_does_not_cache_missing_items() -> None:
    store = FakeStore({"svc": Item(name="svc", value=1)})
    cached = TTLStoreCache(store, ttl_seconds=60.0, max_entries=8)

    assert await cached.get("svc") == Item(name="svc", value=1)
    assert await cached.exists("svc") is True
    assert store.exists_calls == []

    assert await cached.exists("missing") is False
    store.items["missing"] = Item(name="missing", value=2)
    assert await cached.exists("missing") is True
    assert store.exists_calls == ["missing", "missing"]


async def test_list_delegates_without_using_cached_entries() -> None:
    store = FakeStore({"svc": Item(name="svc", value=1)})
    cached = TTLStoreCache(store, ttl_seconds=60.0, max_entries=8)

    assert await cached.get("svc") == Item(name="svc", value=1)
    store.items["other"] = Item(name="other", value=2)

    assert await cached.list() == [Item(name="svc", value=1), Item(name="other", value=2)]
    assert store.list_calls == 1
