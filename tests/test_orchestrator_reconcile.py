from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from aws_light.compute.docker_client import ComposeContainerInfo, ContainerInfo, ContainerStats
from aws_light.compute.node_manager import NodeManager
from aws_light.compute.orchestrator import ComputeOrchestrator
from aws_light.compute.scheduler import BinPackScheduler
from aws_light.models.common import ResourceStatus
from aws_light.models.database import DatabaseSpec, DatabaseState
from aws_light.models.deployment import RolloutState
from aws_light.models.events import EventKind, WebSocketEvent
from aws_light.models.service import (
    DatabaseBinding,
    ReplicaState,
    ServiceResourceBindings,
    ServiceSpec,
    ServiceState,
)
from aws_light.proxy.routing_table import RoutingTable
from aws_light.store.json_store import JsonStore


class FakeDockerClient:
    def __init__(self, running_containers: set[str]) -> None:
        self.running_containers = running_containers
        self.containers_by_name: dict[str, ContainerInfo] = {}
        self.created = 0
        self.removed: list[str] = []
        self.stats: dict[str, ContainerStats] = {}
        self.created_envs: list[dict[str, str]] = []
        self.created_images: list[str] = []
        self.created_names: list[str] = []
        self.created_labels: list[dict[str, str]] = []
        self.created_networks: list[str] = []
        self.created_volumes: list[dict[str, str]] = []
        self.ip_network_requests: list[tuple[str, str]] = []
        self.container_ips: dict[tuple[str, str], str] = {}
        self.ensured_networks: list[str] = []
        self.removed_networks: list[str] = []
        self.network_connections: list[tuple[str, str]] = []
        self.network_connection_aliases: list[tuple[str, str, tuple[str, ...]]] = []
        self.compose_containers: list[ComposeContainerInfo] = []

    def ensure_network(self, network_name: str) -> None:
        self.ensured_networks.append(network_name)

    def remove_network(self, network_name: str) -> None:
        self.removed_networks.append(network_name)

    def connect_container_to_network(
        self, container_id: str, network_name: str, aliases: list[str] | None = None
    ) -> None:
        self.network_connections.append((container_id, network_name))
        self.network_connection_aliases.append(
            (container_id, network_name, tuple(aliases or []))
        )

    def container_is_running(self, container_id: str) -> bool:
        return container_id in self.running_containers

    def get_container_ip(self, container_id: str, network: str) -> str:
        self.ip_network_requests.append((container_id, network))
        if (container_id, network) in self.container_ips:
            return self.container_ips[(container_id, network)]
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
        volumes: dict[str, str] | None = None,
    ) -> tuple[str, str]:
        self.created += 1
        self.created_envs.append(env)
        self.created_images.append(image)
        self.created_names.append(name)
        self.created_labels.append(labels)
        self.created_networks.append(network)
        self.created_volumes.append(volumes or {})
        container_id = f"new-container-{self.created}"
        self.running_containers.add(container_id)
        return container_id, f"10.0.1.{self.created}"

    def get_container_stats(self, container_id: str) -> ContainerStats | None:
        return self.stats.get(container_id)

    def list_compose_containers(
        self, project_name: str = "aws-light"
    ) -> list[ComposeContainerInfo]:
        return self.compose_containers

    def list_containers_by_label(
        self, label_key: str, label_value: str
    ) -> list[ContainerInfo]:
        return []

    def get_container_by_name(self, name: str) -> ContainerInfo | None:
        return self.containers_by_name.get(name)


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


async def test_start_does_not_ensure_shared_workload_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service_store: JsonStore[ServiceState] = JsonStore(tmp_path / "services.json", ServiceState)
    deployment_store: JsonStore[RolloutState] = JsonStore(
        tmp_path / "deployments.json", RolloutState
    )
    node_manager = NodeManager()
    docker_client = FakeDockerClient(running_containers=set())

    def fake_create_task(coro: object) -> object:
        close = getattr(coro, "close", None)
        if close is not None:
            close()
        return object()

    monkeypatch.setattr("aws_light.compute.orchestrator.asyncio.create_task", fake_create_task)

    orchestrator = ComputeOrchestrator(
        service_store=service_store,
        deployment_store=deployment_store,
        docker_client=docker_client,  # type: ignore[arg-type]
        node_manager=node_manager,
        scheduler=BinPackScheduler(),
        event_bus=FakeEventBus(),  # type: ignore[arg-type]
        routing_table=RoutingTable(),
        secrets_manager=FakeSecretsManager(),  # type: ignore[arg-type]
    )

    await orchestrator.start()

    assert "aws-light-data" not in docker_client.ensured_networks


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


async def test_create_replica_uses_service_network_as_primary_network(
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
        secrets_manager=FakeSecretsManager(),  # type: ignore[arg-type]
    )

    await orchestrator._reconcile_service(service)

    assert docker_client.created_networks == ["aws-light-svc-api"]


async def test_refresh_observed_replicas_reads_ip_from_service_network(
    tmp_path: Path,
) -> None:
    service_store: JsonStore[ServiceState] = JsonStore(tmp_path / "services.json", ServiceState)
    deployment_store: JsonStore[RolloutState] = JsonStore(
        tmp_path / "deployments.json", RolloutState
    )
    event_bus = FakeEventBus()
    node_manager = NodeManager()
    node_manager.initialize()
    docker_client = FakeDockerClient(running_containers={"container-alive"})

    service = ServiceState(
        spec=ServiceSpec(
            name="api",
            image="example/api:latest",
            replicas=1,
            cpu_request=0.2,
            memory_request_mb=128,
            port=8000,
        ),
        status=ResourceStatus.RUNNING,
        replicas=[
            ReplicaState(
                replica_id="replica-alive",
                container_id="container-alive",
                node_id="node-00",
                status=ResourceStatus.RUNNING,
                container_ip="",
                image="example/api:latest",
                started_at=datetime.utcnow(),
            )
        ],
    )

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

    updated = await orchestrator._refresh_observed_replicas(service)

    assert docker_client.ip_network_requests == [
        ("container-alive", "aws-light-svc-api")
    ]
    assert updated.replicas[0].container_ip


async def test_refresh_observed_replicas_repairs_existing_replica_service_network(
    tmp_path: Path,
) -> None:
    service_store: JsonStore[ServiceState] = JsonStore(tmp_path / "services.json", ServiceState)
    deployment_store: JsonStore[RolloutState] = JsonStore(
        tmp_path / "deployments.json", RolloutState
    )
    event_bus = FakeEventBus()
    node_manager = NodeManager()
    node_manager.initialize()
    docker_client = FakeDockerClient(running_containers={"container-alive"})
    docker_client.container_ips[("container-alive", "aws-light-svc-api")] = "172.25.0.4"

    service = ServiceState(
        spec=ServiceSpec(
            name="api",
            image="example/api:latest",
            replicas=1,
            cpu_request=0.2,
            memory_request_mb=128,
            port=8000,
        ),
        status=ResourceStatus.RUNNING,
        replicas=[
            ReplicaState(
                replica_id="replica-alive",
                container_id="container-alive",
                node_id="node-00",
                status=ResourceStatus.RUNNING,
                container_ip="172.23.0.6",
                image="example/api:latest",
                started_at=datetime.utcnow(),
            )
        ],
    )

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

    updated = await orchestrator._refresh_observed_replicas(service)

    assert ("container-alive", "aws-light-svc-api") in docker_client.network_connections
    assert docker_client.ip_network_requests == [
        ("container-alive", "aws-light-svc-api")
    ]
    assert updated.replicas[0].container_ip == "172.25.0.4"


async def test_create_replica_injects_bound_database_env(tmp_path: Path) -> None:
    service_store: JsonStore[ServiceState] = JsonStore(tmp_path / "services.json", ServiceState)
    database_store: JsonStore[DatabaseState] = JsonStore(tmp_path / "databases.json", DatabaseState)
    deployment_store: JsonStore[RolloutState] = JsonStore(
        tmp_path / "deployments.json", RolloutState
    )
    event_bus = FakeEventBus()
    node_manager = NodeManager()
    node_manager.initialize()
    docker_client = FakeDockerClient(running_containers=set())
    secrets_manager = FakeSecretsManager()
    await database_store.put(
        "app-db",
        DatabaseState(spec=DatabaseSpec(name="app-db", engine="postgres", version="16")),
    )

    service = ServiceState(
        spec=ServiceSpec(
            name="api",
            image="example/api:latest",
            replicas=1,
            cpu_request=0.2,
            memory_request_mb=128,
            port=8000,
            resources=ServiceResourceBindings(
                databases=[DatabaseBinding(name="app-db", access=["connect"])]
            ),
        ),
        status=ResourceStatus.PENDING,
        replicas=[],
    )
    await service_store.put("api", service)

    orchestrator = ComputeOrchestrator(
        service_store=service_store,
        database_store=database_store,
        deployment_store=deployment_store,
        docker_client=docker_client,  # type: ignore[arg-type]
        node_manager=node_manager,
        scheduler=BinPackScheduler(),
        event_bus=event_bus,  # type: ignore[arg-type]
        routing_table=RoutingTable(),
        secrets_manager=secrets_manager,  # type: ignore[arg-type]
    )

    await orchestrator._reconcile_service(service)

    created_env = docker_client.created_envs[0]
    assert created_env["AWS_LIGHT_DATABASE_APP_DB_HOST"] == "aws-light-db-app-db"
    assert created_env["AWS_LIGHT_DATABASE_APP_DB_PORT"] == "5432"
    assert created_env["AWS_LIGHT_DATABASE_APP_DB_NAME"] == "app_db"
    assert created_env["AWS_LIGHT_DATABASE_APP_DB_USER"] == "app_db_user"
    assert created_env["AWS_LIGHT_DATABASE_APP_DB_PASSWORD"]
    assert (
        created_env["AWS_LIGHT_DATABASE_APP_DB_PASSWORD"]
        == secrets_manager.created_secrets["aws-light-database-app-db-password"]
    )
    assert created_env["AWS_LIGHT_DATABASE_APP_DB_URL"].startswith(
        "postgresql://app_db_user:"
    )
    assert created_env["AWS_LIGHT_DATABASE_APP_DB_URL"].endswith(
        "@aws-light-db-app-db:5432/app_db"
    )


async def test_reconcile_all_provisions_pending_database_container(tmp_path: Path) -> None:
    service_store: JsonStore[ServiceState] = JsonStore(tmp_path / "services.json", ServiceState)
    database_store: JsonStore[DatabaseState] = JsonStore(tmp_path / "databases.json", DatabaseState)
    deployment_store: JsonStore[RolloutState] = JsonStore(
        tmp_path / "deployments.json", RolloutState
    )
    event_bus = FakeEventBus()
    node_manager = NodeManager()
    node_manager.initialize()
    docker_client = FakeDockerClient(running_containers=set())
    secrets_manager = FakeSecretsManager()
    database = DatabaseState(
        spec=DatabaseSpec(
            name="app-db",
            engine="postgres",
            version="16",
            storage_mb=512,
        )
    )
    await database_store.put("app-db", database)

    orchestrator = ComputeOrchestrator(
        service_store=service_store,
        database_store=database_store,
        deployment_store=deployment_store,
        docker_client=docker_client,  # type: ignore[arg-type]
        node_manager=node_manager,
        scheduler=BinPackScheduler(),
        event_bus=event_bus,  # type: ignore[arg-type]
        routing_table=RoutingTable(),
        secrets_manager=secrets_manager,  # type: ignore[arg-type]
    )

    await orchestrator._reconcile_all()

    updated = await database_store.get("app-db")
    assert updated is not None
    assert updated.status == ResourceStatus.RUNNING
    assert updated.container_id == "new-container-1"
    assert updated.container_name == "aws-light-db-app-db"
    assert docker_client.created_images == ["postgres:16"]
    assert docker_client.created_names == ["aws-light-db-app-db"]
    assert "aws-light-db-app-db" in docker_client.ensured_networks
    assert docker_client.created_networks == ["aws-light-db-app-db"]
    assert docker_client.created_volumes == [
        {"aws-light-db-app-db-data": "/var/lib/postgresql/data"}
    ]
    assert docker_client.created_envs[0]["POSTGRES_DB"] == "app_db"
    assert docker_client.created_envs[0]["POSTGRES_USER"] == "app_db_user"
    assert docker_client.created_envs[0]["POSTGRES_PASSWORD"]
    assert (
        docker_client.created_envs[0]["POSTGRES_PASSWORD"]
        == secrets_manager.created_secrets["aws-light-database-app-db-password"]
    )
    assert docker_client.created_labels[0]["aws-light.database"] == "app-db"


async def test_reconcile_all_adopts_running_database_container_by_name(
    tmp_path: Path,
) -> None:
    service_store: JsonStore[ServiceState] = JsonStore(tmp_path / "services.json", ServiceState)
    database_store: JsonStore[DatabaseState] = JsonStore(tmp_path / "databases.json", DatabaseState)
    deployment_store: JsonStore[RolloutState] = JsonStore(
        tmp_path / "deployments.json", RolloutState
    )
    event_bus = FakeEventBus()
    node_manager = NodeManager()
    node_manager.initialize()
    docker_client = FakeDockerClient(running_containers={"existing-db-container"})
    docker_client.containers_by_name["aws-light-db-app-db"] = ContainerInfo(
        container_id="existing-db-container",
        name="aws-light-db-app-db",
        status="running",
        labels={"aws-light.database": "app-db"},
    )
    docker_client.container_ips[("existing-db-container", "aws-light-db-app-db")] = "10.0.2.5"
    await database_store.put("app-db", DatabaseState(spec=DatabaseSpec(name="app-db")))

    orchestrator = ComputeOrchestrator(
        service_store=service_store,
        database_store=database_store,
        deployment_store=deployment_store,
        docker_client=docker_client,  # type: ignore[arg-type]
        node_manager=node_manager,
        scheduler=BinPackScheduler(),
        event_bus=event_bus,  # type: ignore[arg-type]
        routing_table=RoutingTable(),
        secrets_manager=FakeSecretsManager(),  # type: ignore[arg-type]
    )

    await orchestrator._reconcile_all()

    updated = await database_store.get("app-db")
    assert updated is not None
    assert updated.status == ResourceStatus.RUNNING
    assert updated.container_id == "existing-db-container"
    assert updated.container_name == "aws-light-db-app-db"
    assert updated.container_ip == "10.0.2.5"
    assert docker_client.created == 0


async def test_reconcile_all_removes_stale_database_container_before_create(
    tmp_path: Path,
) -> None:
    service_store: JsonStore[ServiceState] = JsonStore(tmp_path / "services.json", ServiceState)
    database_store: JsonStore[DatabaseState] = JsonStore(tmp_path / "databases.json", DatabaseState)
    deployment_store: JsonStore[RolloutState] = JsonStore(
        tmp_path / "deployments.json", RolloutState
    )
    event_bus = FakeEventBus()
    node_manager = NodeManager()
    node_manager.initialize()
    docker_client = FakeDockerClient(running_containers=set())
    docker_client.containers_by_name["aws-light-db-app-db"] = ContainerInfo(
        container_id="stale-db-container",
        name="aws-light-db-app-db",
        status="exited",
        labels={"aws-light.database": "app-db"},
    )
    await database_store.put("app-db", DatabaseState(spec=DatabaseSpec(name="app-db")))

    orchestrator = ComputeOrchestrator(
        service_store=service_store,
        database_store=database_store,
        deployment_store=deployment_store,
        docker_client=docker_client,  # type: ignore[arg-type]
        node_manager=node_manager,
        scheduler=BinPackScheduler(),
        event_bus=event_bus,  # type: ignore[arg-type]
        routing_table=RoutingTable(),
        secrets_manager=FakeSecretsManager(),  # type: ignore[arg-type]
    )

    await orchestrator._reconcile_all()

    updated = await database_store.get("app-db")
    assert updated is not None
    assert updated.status == ResourceStatus.RUNNING
    assert updated.container_id == "new-container-1"
    assert docker_client.removed == ["stale-db-container"]
    assert docker_client.created == 1


async def test_reconcile_all_marks_missing_database_container_pending(
    tmp_path: Path,
) -> None:
    service_store: JsonStore[ServiceState] = JsonStore(tmp_path / "services.json", ServiceState)
    database_store: JsonStore[DatabaseState] = JsonStore(tmp_path / "databases.json", DatabaseState)
    deployment_store: JsonStore[RolloutState] = JsonStore(
        tmp_path / "deployments.json", RolloutState
    )
    event_bus = FakeEventBus()
    node_manager = NodeManager()
    node_manager.initialize()
    docker_client = FakeDockerClient(running_containers=set())
    database = DatabaseState(
        spec=DatabaseSpec(name="app-db"),
        status=ResourceStatus.RUNNING,
        container_id="missing-container",
        container_name="aws-light-db-app-db",
    )
    await database_store.put("app-db", database)

    orchestrator = ComputeOrchestrator(
        service_store=service_store,
        database_store=database_store,
        deployment_store=deployment_store,
        docker_client=docker_client,  # type: ignore[arg-type]
        node_manager=node_manager,
        scheduler=BinPackScheduler(),
        event_bus=event_bus,  # type: ignore[arg-type]
        routing_table=RoutingTable(),
        secrets_manager=FakeSecretsManager(),  # type: ignore[arg-type]
    )

    await orchestrator._reconcile_all()

    updated = await database_store.get("app-db")
    assert updated is not None
    assert updated.status == ResourceStatus.PENDING
    assert updated.container_id == ""
    assert updated.container_name == ""


async def test_teardown_database_removes_container_and_network_but_keeps_volume(
    tmp_path: Path,
) -> None:
    service_store: JsonStore[ServiceState] = JsonStore(tmp_path / "services.json", ServiceState)
    database_store: JsonStore[DatabaseState] = JsonStore(tmp_path / "databases.json", DatabaseState)
    deployment_store: JsonStore[RolloutState] = JsonStore(
        tmp_path / "deployments.json", RolloutState
    )
    event_bus = FakeEventBus()
    node_manager = NodeManager()
    node_manager.initialize()
    docker_client = FakeDockerClient(running_containers={"database-container"})
    database = DatabaseState(
        spec=DatabaseSpec(name="app-db"),
        status=ResourceStatus.DELETING,
        container_id="database-container",
        container_name="aws-light-db-app-db",
    )
    await database_store.put("app-db", database)

    orchestrator = ComputeOrchestrator(
        service_store=service_store,
        database_store=database_store,
        deployment_store=deployment_store,
        docker_client=docker_client,  # type: ignore[arg-type]
        node_manager=node_manager,
        scheduler=BinPackScheduler(),
        event_bus=event_bus,  # type: ignore[arg-type]
        routing_table=RoutingTable(),
        secrets_manager=FakeSecretsManager(),  # type: ignore[arg-type]
    )

    await orchestrator._reconcile_all()

    assert "database-container" in docker_client.removed
    assert "aws-light-db-app-db" in docker_client.removed_networks
    assert await database_store.get("app-db") is None


async def test_bound_service_and_database_join_service_network(tmp_path: Path) -> None:
    service_store: JsonStore[ServiceState] = JsonStore(tmp_path / "services.json", ServiceState)
    database_store: JsonStore[DatabaseState] = JsonStore(tmp_path / "databases.json", DatabaseState)
    deployment_store: JsonStore[RolloutState] = JsonStore(
        tmp_path / "deployments.json", RolloutState
    )
    event_bus = FakeEventBus()
    node_manager = NodeManager()
    node_manager.initialize()
    docker_client = FakeDockerClient(running_containers=set())
    secrets_manager = FakeSecretsManager()
    await database_store.put("app-db", DatabaseState(spec=DatabaseSpec(name="app-db")))
    service = ServiceState(
        spec=ServiceSpec(
            name="api",
            image="example/api:latest",
            replicas=1,
            cpu_request=0.2,
            memory_request_mb=128,
            port=8000,
            resources=ServiceResourceBindings(
                databases=[DatabaseBinding(name="app-db", access=["connect"])]
            ),
        ),
        status=ResourceStatus.PENDING,
        replicas=[],
    )
    await service_store.put("api", service)

    orchestrator = ComputeOrchestrator(
        service_store=service_store,
        database_store=database_store,
        deployment_store=deployment_store,
        docker_client=docker_client,  # type: ignore[arg-type]
        node_manager=node_manager,
        scheduler=BinPackScheduler(),
        event_bus=event_bus,  # type: ignore[arg-type]
        routing_table=RoutingTable(),
        secrets_manager=secrets_manager,  # type: ignore[arg-type]
    )

    await orchestrator._reconcile_all()

    assert "aws-light-svc-api" in docker_client.ensured_networks
    assert ("new-container-1", "aws-light-svc-api") in docker_client.network_connections
    assert docker_client.created_networks[1] == "aws-light-svc-api"


async def test_proxy_and_health_checker_join_service_network(tmp_path: Path) -> None:
    service_store: JsonStore[ServiceState] = JsonStore(tmp_path / "services.json", ServiceState)
    deployment_store: JsonStore[RolloutState] = JsonStore(
        tmp_path / "deployments.json", RolloutState
    )
    event_bus = FakeEventBus()
    node_manager = NodeManager()
    node_manager.initialize()
    docker_client = FakeDockerClient(running_containers=set())
    docker_client.compose_containers = [
        ComposeContainerInfo(
            service="proxy",
            container_id="proxy-container",
            name="aws-light-proxy-1",
            image="aws-light-proxy",
            status="running",
            health="",
            ports=[],
        ),
        ComposeContainerInfo(
            service="health-checker",
            container_id="health-container",
            name="aws-light-health-checker-1",
            image="aws-light-health-checker",
            status="running",
            health="",
            ports=[],
        ),
        ComposeContainerInfo(
            service="autoscaler",
            container_id="autoscaler-container",
            name="aws-light-autoscaler-1",
            image="aws-light-autoscaler",
            status="running",
            health="",
            ports=[],
        ),
    ]
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
        secrets_manager=FakeSecretsManager(),  # type: ignore[arg-type]
    )

    await orchestrator._reconcile_all()

    assert ("proxy-container", "aws-light-svc-api") in docker_client.network_connections
    assert ("health-container", "aws-light-svc-api") in docker_client.network_connections
    assert (
        "proxy-container",
        "aws-light-svc-api",
        ("proxy",),
    ) in docker_client.network_connection_aliases
    assert (
        "health-container",
        "aws-light-svc-api",
        ("health-checker",),
    ) in docker_client.network_connection_aliases
    assert ("autoscaler-container", "aws-light-svc-api") not in docker_client.network_connections


async def test_teardown_service_removes_service_network(tmp_path: Path) -> None:
    service_store: JsonStore[ServiceState] = JsonStore(tmp_path / "services.json", ServiceState)
    deployment_store: JsonStore[RolloutState] = JsonStore(
        tmp_path / "deployments.json", RolloutState
    )
    event_bus = FakeEventBus()
    node_manager = NodeManager()
    node_manager.initialize()
    docker_client = FakeDockerClient(running_containers={"container-1"})
    service = ServiceState(
        spec=ServiceSpec(
            name="api",
            image="example/api:latest",
            replicas=1,
            cpu_request=0.2,
            memory_request_mb=128,
            port=8000,
        ),
        status=ResourceStatus.DELETING,
        replicas=[
            ReplicaState(
                replica_id="replica-1",
                container_id="container-1",
                node_id="node-00",
                status=ResourceStatus.RUNNING,
                container_ip="172.25.0.4",
                image="example/api:latest",
                started_at=datetime.utcnow(),
            )
        ],
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
        secrets_manager=FakeSecretsManager(),  # type: ignore[arg-type]
    )

    await orchestrator._reconcile_all()

    assert "container-1" in docker_client.removed
    assert "aws-light-svc-api" in docker_client.removed_networks
    assert await service_store.get("api") is None


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
