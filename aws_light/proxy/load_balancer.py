from __future__ import annotations

import asyncio

from aws_light.proxy.routing_table import AnyRoutingTable, ReplicaEndpoint


class NoHealthyReplicaError(Exception):
    pass


class RoundRobinBalancer:
    def __init__(self, routing_table: AnyRoutingTable) -> None:
        self._routing_table = routing_table
        self._counters: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def next_healthy_replica(self, service_name: str) -> ReplicaEndpoint:
        return (await self.healthy_replicas_for_request(service_name))[0]

    async def healthy_replicas_for_request(self, service_name: str) -> list[ReplicaEndpoint]:
        endpoints = await self._routing_table.get_endpoints(service_name)
        healthy_endpoints = [endpoint for endpoint in endpoints if endpoint.healthy]
        if not healthy_endpoints:
            raise NoHealthyReplicaError(f"No healthy replicas for service '{service_name}'")

        async with self._lock:
            current_index = self._counters.get(service_name, 0)
            self._counters[service_name] = current_index + 1

        offset = current_index % len(healthy_endpoints)
        return healthy_endpoints[offset:] + healthy_endpoints[:offset]
