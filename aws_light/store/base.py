from __future__ import annotations

from typing import Protocol, TypeVar

T = TypeVar("T")


class AnyStore(Protocol[T]):
    async def get(self, identifier: str) -> T | None: ...

    async def put(self, identifier: str, item: T) -> None: ...

    async def list(self) -> list[T]: ...

    async def delete(self, identifier: str) -> None: ...

    async def exists(self, identifier: str) -> bool: ...
