from __future__ import annotations

from datetime import datetime
from pathlib import Path

from aws_light.compute.node_manager import NodeManager
from aws_light.compute.orchestrator import ComputeOrchestrator
from aws_light.compute.scheduler import BinPackScheduler
from aws_light.models.common import ResourceStatus
from aws_light.models.deployment import RolloutState
from aws_light.models.events import WebSocketEvent
from aws_light.models.service import ReplicaState, ServiceSpec, ServiceState
from aws_light.proxy.routing_table import RoutingTable
from aws_light.store.json_store import JsonStore


class FakeDockerClient:
    def __init__(self, running_containers: set[str]) -> None:
        self.running_containers = running_containers
        self.created = 0
        self.removed: list[str] = []

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
        container_id = f"new-container-{self.created}"
        self.running_containers.add(container_id)
        return container_id, f"10.0.1.{self.created}"


class FakeEventBus:
    def __init__(self) -> None:
        self.events: list[WebSocketEvent] = []

    async def publish(self, event: WebSocketEvent) -> None:
        self.events.append(event)


class FakeSecretsManager:
    async def inject_into_env(self, secret_refs: list[str]) -> dict[str, str]:
        return {}


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
    assert any(event.kind.value == "replica.failed" for event in event_bus.events)
