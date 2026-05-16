from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol


@dataclass
class ReplicaEndpoint:
    replica_id: str
    host: str
    port: int
    healthy: bool = True


class AnyRoutingTable(Protocol):
    async def update_service(self, service_name: str, endpoints: list[ReplicaEndpoint]) -> None: ...
    async def get_endpoints(self, service_name: str) -> list[ReplicaEndpoint]: ...
    async def set_healthy(self, replica_id: str, healthy: bool) -> None: ...
    async def remove_service(self, service_name: str) -> None: ...
    async def all_service_names(self) -> list[str]: ...


class RoutingTable:
    """In-memory routing table — used in tests and the monolith."""

    def __init__(self) -> None:
        self._table: dict[str, list[ReplicaEndpoint]] = {}
        self._lock = asyncio.Lock()

    async def update_service(self, service_name: str, endpoints: list[ReplicaEndpoint]) -> None:
        async with self._lock:
            self._table[service_name] = endpoints

    async def get_endpoints(self, service_name: str) -> list[ReplicaEndpoint]:
        async with self._lock:
            return list(self._table.get(service_name, []))

    async def set_healthy(self, replica_id: str, healthy: bool) -> None:
        async with self._lock:
            for endpoints in self._table.values():
                for endpoint in endpoints:
                    if endpoint.replica_id == replica_id:
                        endpoint.healthy = healthy
                        return

    async def remove_service(self, service_name: str) -> None:
        async with self._lock:
            self._table.pop(service_name, None)

    async def all_service_names(self) -> list[str]:
        async with self._lock:
            return list(self._table.keys())
