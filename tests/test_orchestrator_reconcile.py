from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from aws_light.compute.docker_client import ContainerStats
from aws_light.compute.node_manager import NodeManager
from aws_light.compute.orchestrator import ComputeOrchestrator
from aws_light.compute.scheduler import BinPackScheduler
from aws_light.models.common import ResourceStatus
from aws_light.models.deployment import RolloutState
from aws_light.models.events import EventKind, WebSocketEvent
from aws_light.models.service import ReplicaState, ServiceSpec, ServiceState
from aws_light.proxy.routing_table import RoutingTable
from aws_light.store.json_store import JsonStore


class FakeDockerClient:
    def __init__(self, running_containers: set[str]) -> None:
        self.running_containers = running_containers
        self.created = 0
        self.removed: list[str] = []
        self.stats: dict[str, ContainerStats] = {}
        self.created_envs: list[dict[str, str]] = []

    def container_is_running(self, container_id: str) -> bool:
        return container_id in self.running_containers

    def get_container_ip(self, container_id: str, network: str) -> str:
        return f"10.0.0.{abs(hash(container_id)) % 200 + 1}"

    def remove_container(self, container_id: str) -> None:
        self.running_containers.discard(container_id)
        self.removed.append(container_id)

    def create_container(
        self,
        image: str,
        name: str,
        env: dict[str, str],
        cpu_quota: float,
        memory_mb: int,
        network: str,
        labels: dict[str, str],
        container_port: int,
    ) -> tuple[str, str]:
        self.created += 1
        self.created_envs.append(env)
        container_id = f"new-container-{self.created}"
        self.running_containers.add(container_id)
        return container_id, f"10.0.1.{self.created}"

    def get_container_stats(self, container_id: str) -> ContainerStats | None:
        return self.stats.get(container_id)


class FakeEventBus:
    def __init__(self) -> None:
        self.events: list[WebSocketEvent] = []

    async def publish(self, event: WebSocketEvent) -> None:
        self.events.append(event)


class FakeSecretsManager:
    def __init__(self) -> None:
        self.created_secrets: dict[str, str] = {}

    async def inject_into_env(self, secret_refs: list[str]) -> dict[str, str]:
        return {}

    async def get_secret(self, name: str) -> str | None:
        return self.created_secrets.get(name)

    async def create_secret(self, name: str, value: str) -> None:
        self.created_secrets[name] = value


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    async def set(self, key: str, value: object) -> None:
        self.values[key] = value


async def test_reconcile_replaces_missing_running_replica(tmp_path: Path) -> None:
    service_store: JsonStore[ServiceState] = JsonStore(tmp_path / "services.json", ServiceState)
    deployment_store: JsonStore[RolloutState] = JsonStore(
        tmp_path / "deployments.json", RolloutState
    )
    routing_table = RoutingTable()
    event_bus = FakeEventBus()
    node_manager = NodeManager()
    node_manager.initialize()
    docker_client = FakeDockerClient(running_containers={"container-alive"})

    service = ServiceState(
        spec=ServiceSpec(
            name="svc",
            image="example/service:latest",
            replicas=2,
            cpu_request=0.2,
            memory_request_mb=128,
            port=8000,
        ),
        status=ResourceStatus.RUNNING,
        replicas=[
            ReplicaState(
                replica_id="replica-missing",
                container_id="container-missing",
                node_id="node-00",
                status=ResourceStatus.RUNNING,
                container_ip="10.0.0.10",
                image="example/service:latest",
                started_at=datetime.utcnow(),
            ),
            ReplicaState(
                replica_id="replica-alive",
                container_id="container-alive",
                node_id="node-00",
                status=ResourceStatus.RUNNING,
                container_ip="10.0.0.11",
                image="example/service:latest",
                started_at=datetime.utcnow(),
            ),
        ],
    )
    await service_store.put("svc", service)

    orchestrator = ComputeOrchestrator(
        service_store=service_store,
        deployment_store=deployment_store,
        docker_client=docker_client,  # type: ignore[arg-type]
        node_manager=node_manager,
        scheduler=BinPackScheduler(),
        event_bus=event_bus,  # type: ignore[arg-type]
        routing_table=routing_table,
        secrets_manager=FakeSecretsManager(),  # type: ignore[arg-type]
    )

    await orchestrator._reconcile_service(service)

    updated = await service_store.get("svc")
    assert updated is not None
    running_replicas = [r for r in updated.replicas if r.status == ResourceStatus.RUNNING]
    assert len(running_replicas) == 2
    assert "container-missing" in docker_client.removed
    assert docker_client.created == 1
    assert all(replica.container_id != "container-missing" for replica in running_replicas)

    endpoints = await routing_table.get_endpoints("svc")
    assert {endpoint.replica_id for endpoint in endpoints} == {
        replica.replica_id for replica in running_replicas
    }
    assert all(endpoint.healthy is False for endpoint in endpoints)
    assert any(event.kind.value == "replica.failed" for event in event_bus.events)
    scheduler_events = [
        event for event in event_bus.events if event.kind == EventKind.SCHEDULER_SELECTED
    ]
    assert len(scheduler_events) == 1
    assert scheduler_events[0].payload["service_name"] == "svc"
    assert scheduler_events[0].payload["node_id"] == "node-00"
    assert scheduler_events[0].payload["candidate_nodes"][0]["cpu_capacity"] == 0.5


async def test_reconcile_emits_no_capacity_when_replica_cannot_fit(tmp_path: Path) -> None:
    service_store: JsonStore[ServiceState] = JsonStore(tmp_path / "services.json", ServiceState)
    deployment_store: JsonStore[RolloutState] = JsonStore(
        tmp_path / "deployments.json", RolloutState
    )
    event_bus = FakeEventBus()
    node_manager = NodeManager()
    node_manager.initialize()
    docker_client = FakeDockerClient(running_containers=set())

    service = ServiceState(
        spec=ServiceSpec(
            name="too-large",
            image="example/service:latest",
            replicas=1,
            cpu_request=2.0,
            memory_request_mb=128,
            port=8000,
        ),
        status=ResourceStatus.PENDING,
        replicas=[],
    )
    await service_store.put("too-large", service)

    orchestrator = ComputeOrchestrator(
        service_store=service_store,
        deployment_store=deployment_store,
        docker_client=docker_client,  # type: ignore[arg-type]
        node_manager=node_manager,
        scheduler=BinPackScheduler(),
        event_bus=event_bus,  # type: ignore[arg-type]
        routing_table=RoutingTable(),
        secrets_manager=FakeSecretsManager(),  # type: ignore[arg-type]
    )

    await orchestrator._reconcile_service(service)

    assert docker_client.created == 0
    no_capacity_events = [
        event for event in event_bus.events if event.kind == EventKind.SCHEDULER_NO_CAPACITY
    ]
    assert len(no_capacity_events) == 1
    assert no_capacity_events[0].payload["service_name"] == "too-large"
    assert no_capacity_events[0].payload["cpu_request"] == 2.0


async def test_create_replica_injects_platform_identity_env(tmp_path: Path) -> None:
    service_store: JsonStore[ServiceState] = JsonStore(tmp_path / "services.json", ServiceState)
    deployment_store: JsonStore[RolloutState] = JsonStore(
        tmp_path / "deployments.json", RolloutState
    )
    event_bus = FakeEventBus()
    node_manager = NodeManager()
    node_manager.initialize()
    docker_client = FakeDockerClient(running_containers=set())
    secrets_manager = FakeSecretsManager()

    service = ServiceState(
        spec=ServiceSpec(
            name="storage-service",
            image="example/storage-service:latest",
            replicas=1,
            cpu_request=0.2,
            memory_request_mb=128,
            port=8000,
            env={"APP_MODE": "demo"},
        ),
        status=ResourceStatus.PENDING,
        replicas=[],
    )
    await service_store.put("storage-service", service)

    orchestrator = ComputeOrchestrator(
        service_store=service_store,
        deployment_store=deployment_store,
        docker_client=docker_client,  # type: ignore[arg-type]
        node_manager=node_manager,
        scheduler=BinPackScheduler(),
        event_bus=event_bus,  # type: ignore[arg-type]
        routing_table=RoutingTable(),
        secrets_manager=secrets_manager,  # type: ignore[arg-type]
    )

    await orchestrator._reconcile_service(service)

    assert docker_client.created == 1
    created_env = docker_client.created_envs[0]
    assert created_env["APP_MODE"] == "demo"
    assert created_env["AWS_LIGHT_SERVICE_NAME"] == "storage-service"
    assert created_env["AWS_LIGHT_PROXY_URL"] == "http://proxy:8080"
    assert created_env["AWS_LIGHT_STORAGE_URL"] == "http://proxy:8080/_aws-light/storage"
    assert created_env["AWS_LIGHT_SERVICE_TOKEN"]
    assert (
        secrets_manager.created_secrets["aws-light-service-token-storage-service"]
        == created_env["AWS_LIGHT_SERVICE_TOKEN"]
    )


async def test_create_replica_reuses_existing_platform_identity_token(
    tmp_path: Path,
) -> None:
    service_store: JsonStore[ServiceState] = JsonStore(tmp_path / "services.json", ServiceState)
    deployment_store: JsonStore[RolloutState] = JsonStore(
        tmp_path / "deployments.json", RolloutState
    )
    event_bus = FakeEventBus()
    node_manager = NodeManager()
    node_manager.initialize()
    docker_client = FakeDockerClient(running_containers=set())
    secrets_manager = FakeSecretsManager()
    secrets_manager.created_secrets["aws-light-service-token-api"] = "existing-token"

    service = ServiceState(
        spec=ServiceSpec(
            name="api",
            image="example/api:latest",
            replicas=1,
            cpu_request=0.2,
            memory_request_mb=128,
            port=8000,
        ),
        status=ResourceStatus.PENDING,
        replicas=[],
    )
    await service_store.put("api", service)

    orchestrator = ComputeOrchestrator(
        service_store=service_store,
        deployment_store=deployment_store,
        docker_client=docker_client,  # type: ignore[arg-type]
        node_manager=node_manager,
        scheduler=BinPackScheduler(),
        event_bus=event_bus,  # type: ignore[arg-type]
        routing_table=RoutingTable(),
        secrets_manager=secrets_manager,  # type: ignore[arg-type]
    )

    await orchestrator._reconcile_service(service)

    assert docker_client.created_envs[0]["AWS_LIGHT_SERVICE_TOKEN"] == "existing-token"


async def test_collect_cpu_stats_updates_actual_node_usage(tmp_path: Path) -> None:
    service_store: JsonStore[ServiceState] = JsonStore(tmp_path / "services.json", ServiceState)
    deployment_store: JsonStore[RolloutState] = JsonStore(
        tmp_path / "deployments.json", RolloutState
    )
    event_bus = FakeEventBus()
    node_manager = NodeManager()
    node_manager.initialize()
    docker_client = FakeDockerClient(running_containers={"container-1", "container-2"})
    docker_client.stats = {
        "container-1": ContainerStats(cpu_percent=20.0, memory_mb=40.0),
        "container-2": ContainerStats(cpu_percent=10.0, memory_mb=30.0),
    }
    redis = FakeRedis()

    service = ServiceState(
        spec=ServiceSpec(
            name="svc",
            image="example/service:latest",
            replicas=2,
            cpu_request=0.2,
            memory_request_mb=128,
            port=8000,
        ),
        status=ResourceStatus.RUNNING,
        replicas=[
            ReplicaState(
                replica_id="replica-1",
                container_id="container-1",
                node_id="node-00",
                status=ResourceStatus.RUNNING,
                container_ip="10.0.0.10",
                image="example/service:latest",
                started_at=datetime.utcnow(),
            ),
            ReplicaState(
                replica_id="replica-2",
                container_id="container-2",
                node_id="node-00",
                status=ResourceStatus.RUNNING,
                container_ip="10.0.0.11",
                image="example/service:latest",
                started_at=datetime.utcnow(),
            ),
        ],
    )
    await service_store.put("svc", service)

    orchestrator = ComputeOrchestrator(
        service_store=service_store,
        deployment_store=deployment_store,
        docker_client=docker_client,  # type: ignore[arg-type]
        node_manager=node_manager,
        scheduler=BinPackScheduler(),
        event_bus=event_bus,  # type: ignore[arg-type]
        routing_table=RoutingTable(),
        secrets_manager=FakeSecretsManager(),  # type: ignore[arg-type]
        redis_client=redis,
    )

    await orchestrator._collect_and_publish_cpu_stats()

    node = node_manager.get_node("node-00")
    assert node is not None
    assert node.usage.cpu_used == 0
    assert node.actual_usage.cpu_used == pytest.approx(0.3)
    assert node.actual_usage.memory_used_mb == 70.0
    assert redis.values["cpu:svc"] == pytest.approx(75.0)
