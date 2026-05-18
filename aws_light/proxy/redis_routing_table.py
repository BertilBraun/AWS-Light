from __future__ import annotations

import json

from redis.asyncio import Redis

from aws_light.proxy.routing_table import ReplicaEndpoint

_SET_HEALTHY_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local endpoints = cjson.decode(raw)
local found = 0
for i, ep in ipairs(endpoints) do
    if ep['replica_id'] == ARGV[1] then
        ep['healthy'] = ARGV[2] == 'true'
        endpoints[i] = ep
        found = 1
    end
end
if found == 0 then return 0 end
redis.call('SET', KEYS[1], cjson.encode(endpoints))
return 1
"""


def _key(service_name: str) -> str:
    return f"routing:{service_name}"


class RedisRoutingTable:
    def __init__(self, redis_client: Redis) -> None:  # type: ignore[type-arg]
        self._redis = redis_client

    async def update_service(self, service_name: str, endpoints: list[ReplicaEndpoint]) -> None:
        existing_health = {
            endpoint.replica_id: endpoint.healthy
            for endpoint in await self.get_endpoints(service_name)
        }
        data = json.dumps(
            [
                {
                    "replica_id": ep.replica_id,
                    "host": ep.host,
                    "port": ep.port,
                    "healthy": existing_health.get(ep.replica_id, ep.healthy),
                }
                for ep in endpoints
            ]
        )
        await self._redis.set(_key(service_name), data)

    async def get_endpoints(self, service_name: str) -> list[ReplicaEndpoint]:
        raw = await self._redis.get(_key(service_name))
        if raw is None:
            return []
        items = json.loads(raw)
        return [
            ReplicaEndpoint(
                replica_id=item["replica_id"],
                host=item["host"],
                port=item["port"],
                healthy=item["healthy"],
            )
            for item in items
        ]

    async def set_healthy(self, replica_id: str, healthy: bool) -> None:
        keys = await self.all_service_names()
        for service_name in keys:
            result = await self._redis.eval(
                _SET_HEALTHY_LUA,
                1,
                _key(service_name),
                replica_id,
                "true" if healthy else "false",
            )
            if result:
                return

    async def remove_service(self, service_name: str) -> None:
        await self._redis.delete(_key(service_name))

    async def all_service_names(self) -> list[str]:
        keys = await self._redis.keys("routing:*")
        prefix = len("routing:")
        return [(k.decode() if isinstance(k, bytes) else k)[prefix:] for k in keys]
