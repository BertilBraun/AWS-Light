from __future__ import annotations

import json

import pytest

from aws_light.proxy.load_balancer import NoHealthyReplicaError, RoundRobinBalancer
from aws_light.proxy.redis_routing_table import RedisRoutingTable
from aws_light.proxy.routing_table import ReplicaEndpoint, RoutingTable


@pytest.fixture()
def routing_table() -> RoutingTable:
    return RoutingTable()


@pytest.fixture()
def balancer(routing_table: RoutingTable) -> RoundRobinBalancer:
    return RoundRobinBalancer(routing_table)


async def test_next_healthy_replica_raises_when_no_service_registered(
    balancer: RoundRobinBalancer,
) -> None:
    with pytest.raises(NoHealthyReplicaError):
        await balancer.next_healthy_replica("unknown-service")


async def test_new_endpoint_defaults_unhealthy_until_checked(routing_table: RoutingTable) -> None:
    await routing_table.update_service(
        "svc",
        [ReplicaEndpoint(replica_id="r1", host="127.0.0.1", port=20001)],
    )

    endpoints = await routing_table.get_endpoints("svc")
    assert endpoints[0].healthy is False


async def test_next_healthy_replica_raises_when_all_replicas_unhealthy(
    routing_table: RoutingTable, balancer: RoundRobinBalancer
) -> None:
    await routing_table.update_service(
        "svc",
        [ReplicaEndpoint(replica_id="r1", host="127.0.0.1", port=20001, healthy=False)],
    )
    with pytest.raises(NoHealthyReplicaError):
        await balancer.next_healthy_replica("svc")


async def test_next_healthy_replica_returns_healthy_endpoint(
    routing_table: RoutingTable, balancer: RoundRobinBalancer
) -> None:
    await routing_table.update_service(
        "svc",
        [ReplicaEndpoint(replica_id="r1", host="127.0.0.1", port=20001, healthy=True)],
    )
    endpoint = await balancer.next_healthy_replica("svc")
    assert endpoint.port == 20001


async def test_round_robin_cycles_through_healthy_replicas(
    routing_table: RoutingTable, balancer: RoundRobinBalancer
) -> None:
    await routing_table.update_service(
        "svc",
        [
            ReplicaEndpoint(replica_id="r1", host="127.0.0.1", port=20001, healthy=True),
            ReplicaEndpoint(replica_id="r2", host="127.0.0.1", port=20002, healthy=True),
        ],
    )
    first = await balancer.next_healthy_replica("svc")
    second = await balancer.next_healthy_replica("svc")
    third = await balancer.next_healthy_replica("svc")
    assert first.port != second.port
    assert third.port == first.port


async def test_healthy_replicas_for_request_returns_retry_order(
    routing_table: RoutingTable, balancer: RoundRobinBalancer
) -> None:
    await routing_table.update_service(
        "svc",
        [
            ReplicaEndpoint(replica_id="r1", host="127.0.0.1", port=20001, healthy=True),
            ReplicaEndpoint(replica_id="r2", host="127.0.0.1", port=20002, healthy=True),
            ReplicaEndpoint(replica_id="r3", host="127.0.0.1", port=20003, healthy=True),
        ],
    )

    first = await balancer.healthy_replicas_for_request("svc")
    second = await balancer.healthy_replicas_for_request("svc")

    assert [endpoint.replica_id for endpoint in first] == ["r1", "r2", "r3"]
    assert [endpoint.replica_id for endpoint in second] == ["r2", "r3", "r1"]


async def test_set_healthy_false_removes_replica_from_rotation(
    routing_table: RoutingTable, balancer: RoundRobinBalancer
) -> None:
    await routing_table.update_service(
        "svc",
        [
            ReplicaEndpoint(replica_id="r1", host="127.0.0.1", port=20001, healthy=True),
            ReplicaEndpoint(replica_id="r2", host="127.0.0.1", port=20002, healthy=True),
        ],
    )
    await routing_table.set_healthy("r1", False)
    for _ in range(4):
        endpoint = await balancer.next_healthy_replica("svc")
        assert endpoint.replica_id == "r2"


async def test_update_service_preserves_existing_replica_health(
    routing_table: RoutingTable,
) -> None:
    await routing_table.update_service(
        "svc",
        [ReplicaEndpoint(replica_id="r1", host="127.0.0.1", port=20001)],
    )
    await routing_table.set_healthy("r1", True)
    await routing_table.update_service(
        "svc",
        [
            ReplicaEndpoint(replica_id="r1", host="127.0.0.2", port=20001),
            ReplicaEndpoint(replica_id="r2", host="127.0.0.3", port=20002),
        ],
    )

    endpoints = await routing_table.get_endpoints("svc")
    assert [(endpoint.replica_id, endpoint.healthy) for endpoint in endpoints] == [
        ("r1", True),
        ("r2", False),
    ]


class FakeRedisForRouting:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: object) -> None:
        self.values[key] = str(value)

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)

    async def keys(self, pattern: str) -> list[str]:
        if pattern.endswith("*"):
            prefix = pattern[:-1]
            return [key for key in self.values if key.startswith(prefix)]
        return [key for key in self.values if key == pattern]

    async def eval(
        self,
        script: str,
        key_count: int,
        key: str,
        replica_id: str,
        healthy: str,
    ) -> int:
        del script, key_count

        raw = self.values.get(key)
        if raw is None:
            return 0
        endpoints = json.loads(raw)
        found = False
        for endpoint in endpoints:
            if endpoint["replica_id"] == replica_id:
                endpoint["healthy"] = healthy == "true"
                found = True
        if not found:
            return 0
        self.values[key] = json.dumps(endpoints)
        return 1


async def test_redis_routing_table_set_healthy_continues_until_replica_match() -> None:
    redis = FakeRedisForRouting()
    routing_table = RedisRoutingTable(redis)  # type: ignore[arg-type]
    await routing_table.update_service(
        "a-service",
        [ReplicaEndpoint(replica_id="a1", host="127.0.0.1", port=20001)],
    )
    await routing_table.update_service(
        "b-service",
        [ReplicaEndpoint(replica_id="b1", host="127.0.0.2", port=20002)],
    )

    await routing_table.set_healthy("b1", True)

    a_endpoints = await routing_table.get_endpoints("a-service")
    b_endpoints = await routing_table.get_endpoints("b-service")
    assert a_endpoints[0].healthy is False
    assert b_endpoints[0].healthy is True
