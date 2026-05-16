from __future__ import annotations

import pytest

from aws_light.proxy.load_balancer import NoHealthyReplicaError, RoundRobinBalancer
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
