from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime

from aws_light.compute.docker_client import DockerClient
from aws_light.compute.node_manager import NodeManager
from aws_light.compute.scheduler import BinPackScheduler, SchedulingError
from aws_light.config import settings
from aws_light.dashboard.event_bus import EventBus
from aws_light.models.common import ResourceStatus
from aws_light.models.events import EventKind, WebSocketEvent
from aws_light.models.service import ReplicaState, ServiceState
from aws_light.proxy.routing_table import ReplicaEndpoint, RoutingTable
from aws_light.secrets.secrets_manager import SecretsManager
from aws_light.store.json_store import JsonStore

logger = logging.getLogger(__name__)

_DOCKER_LABEL_MANAGED = "aws-light.managed"
_DOCKER_LABEL_SERVICE = "aws-light.service"
_DOCKER_LABEL_REPLICA = "aws-light.replica-id"
_DOCKER_LABEL_NODE = "aws-light.node"


class ComputeOrchestrator:
    def __init__(
        self,
        service_store: JsonStore[ServiceState],
        docker_client: DockerClient,
        node_manager: NodeManager,
        scheduler: BinPackScheduler,
        event_bus: EventBus,
        port_counter_path: object,
        routing_table: RoutingTable,
        secrets_manager: SecretsManager,
    ) -> None:
        self._service_store = service_store
        self._docker_client = docker_client
        self._node_manager = node_manager
        self._scheduler = scheduler
        self._event_bus = event_bus
        self._port_counter_path = port_counter_path
        self._routing_table = routing_table
        self._secrets_manager = secrets_manager
        self._next_port = settings.replica_port_start
        self._running = False

    async def start(self) -> None:
        self._running = True
        self._docker_client.ensure_network(settings.docker_network)
        self._node_manager.initialize()
        await self._remove_orphan_containers()
        asyncio.create_task(self._reconcile_loop())

    async def stop(self) -> None:
        self._running = False

    async def _reconcile_loop(self) -> None:
        while self._running:
            try:
                await self._reconcile_all()
            except Exception:
                logger.exception("Error during reconcile loop")
            await asyncio.sleep(5)

    async def _reconcile_all(self) -> None:
        all_services = await self._service_store.list()
        for service_state in all_services:
            await self._reconcile_service(service_state)

    async def _reconcile_service(self, service_state: ServiceState) -> None:
        spec = service_state.spec
        running_replicas = [
            replica
            for replica in service_state.replicas
            if replica.status == ResourceStatus.RUNNING
        ]
        desired_count = spec.replicas
        actual_count = len(running_replicas)
        replicas_changed = False

        if actual_count < desired_count:
            for _ in range(desired_count - actual_count):
                await self._create_replica(service_state)
                replicas_changed = True
        elif actual_count > desired_count:
            excess_replicas = running_replicas[desired_count:]
            for replica in excess_replicas:
                await self._remove_replica(service_state, replica)
                replicas_changed = True

        updated_service = await self._service_store.get(spec.name)
        if updated_service is not None:
            running_count = sum(
                1 for r in updated_service.replicas if r.status == ResourceStatus.RUNNING
            )
            new_status = ResourceStatus.RUNNING if running_count >= 1 else ResourceStatus.DEGRADED
            status_changed = updated_service.status != new_status
            if status_changed:
                updated_service.status = new_status
                updated_service.updated_at = datetime.utcnow()
                await self._service_store.put(spec.name, updated_service)
            if status_changed or replicas_changed:
                await self._emit_service_updated(updated_service)

    async def _create_replica(self, service_state: ServiceState) -> None:
        spec = service_state.spec
        nodes = self._node_manager.get_all_nodes()
        try:
            target_node = self._scheduler.select_node(
                nodes, spec.cpu_request, spec.memory_request_mb
            )
        except SchedulingError:
            logger.warning("Cannot schedule replica for %s: no capacity", spec.name)
            return

        replica_id = str(uuid.uuid4())
        host_port = self._next_port
        self._next_port += 1

        container_name = f"aws-light-{spec.name}-{replica_id[:8]}"
        labels = {
            _DOCKER_LABEL_MANAGED: "true",
            _DOCKER_LABEL_SERVICE: spec.name,
            _DOCKER_LABEL_REPLICA: replica_id,
            _DOCKER_LABEL_NODE: target_node.spec.node_id,
        }

        secret_env = await self._secrets_manager.inject_into_env(spec.secret_refs)
        merged_env = {**spec.env, **secret_env}

        try:
            container_id = self._docker_client.create_container(
                image=spec.image,
                name=container_name,
                env=merged_env,
                cpu_quota=spec.cpu_request,
                memory_mb=spec.memory_request_mb,
                network=settings.docker_network,
                labels=labels,
                host_port=host_port,
                container_port=spec.port,
            )
        except Exception:
            logger.exception("Failed to create container for %s", spec.name)
            return

        self._node_manager.allocate(
            target_node.spec.node_id, replica_id, spec.cpu_request, spec.memory_request_mb
        )
        await self._emit_node_updated(target_node.spec.node_id)

        replica = ReplicaState(
            replica_id=replica_id,
            container_id=container_id,
            node_id=target_node.spec.node_id,
            status=ResourceStatus.RUNNING,
            host_port=host_port,
            started_at=datetime.utcnow(),
        )

        current_service = await self._service_store.get(spec.name)
        if current_service is not None:
            current_service.replicas.append(replica)
            current_service.updated_at = datetime.utcnow()
            await self._service_store.put(spec.name, current_service)
            await self._sync_routing_table(current_service)

        await self._event_bus.publish(
            WebSocketEvent(
                kind=EventKind.REPLICA_STARTED,
                payload={
                    "replica_id": replica_id,
                    "service_name": spec.name,
                    "node_id": target_node.spec.node_id,
                    "host_port": host_port,
                },
            )
        )

    async def _remove_replica(self, service_state: ServiceState, replica: ReplicaState) -> None:
        spec = service_state.spec
        self._docker_client.remove_container(replica.container_id)
        self._node_manager.deallocate(
            replica.node_id, replica.replica_id, spec.cpu_request, spec.memory_request_mb
        )
        await self._emit_node_updated(replica.node_id)

        current_service = await self._service_store.get(spec.name)
        if current_service is not None:
            current_service.replicas = [
                r for r in current_service.replicas if r.replica_id != replica.replica_id
            ]
            current_service.updated_at = datetime.utcnow()
            await self._service_store.put(spec.name, current_service)
            await self._sync_routing_table(current_service)

        await self._event_bus.publish(
            WebSocketEvent(
                kind=EventKind.REPLICA_STOPPED,
                payload={
                    "replica_id": replica.replica_id,
                    "service_name": spec.name,
                    "node_id": replica.node_id,
                },
            )
        )

    async def _sync_routing_table(self, service_state: ServiceState) -> None:
        endpoints = [
            ReplicaEndpoint(
                replica_id=replica.replica_id,
                host="localhost",
                port=replica.host_port,
            )
            for replica in service_state.replicas
            if replica.status == ResourceStatus.RUNNING
        ]
        await self._routing_table.update_service(service_state.spec.name, endpoints)

    async def _remove_orphan_containers(self) -> None:
        known_container_ids: set[str] = set()
        for service_state in await self._service_store.list():
            for replica in service_state.replicas:
                known_container_ids.add(replica.container_id)

        orphans = self._docker_client.list_containers_by_label(_DOCKER_LABEL_MANAGED, "true")
        for orphan in orphans:
            if orphan.container_id not in known_container_ids:
                logger.info("Removing orphan container %s", orphan.name)
                self._docker_client.remove_container(orphan.container_id)

    async def _emit_service_updated(self, service_state: ServiceState) -> None:
        await self._event_bus.publish(
            WebSocketEvent(
                kind=EventKind.SERVICE_UPDATED,
                payload={
                    "service_name": service_state.spec.name,
                    "status": service_state.status.value,
                    "replica_count": len(service_state.replicas),
                    "service": service_state.model_dump(mode="json"),
                },
            )
        )

    async def _emit_node_updated(self, node_id: str) -> None:
        node = self._node_manager.get_node(node_id)
        if node is None:
            return
        await self._event_bus.publish(
            WebSocketEvent(
                kind=EventKind.NODE_UPDATED,
                payload={
                    "node_id": node_id,
                    "cpu_used": node.usage.cpu_used,
                    "memory_used_mb": node.usage.memory_used_mb,
                    "replica_count": len(node.replica_ids),
                },
            )
        )

    async def delete_service(self, service_name: str) -> None:
        service_state = await self._service_store.get(service_name)
        if service_state is None:
            return
        for replica in service_state.replicas:
            self._docker_client.remove_container(replica.container_id)
            self._node_manager.deallocate(
                replica.node_id,
                replica.replica_id,
                service_state.spec.cpu_request,
                service_state.spec.memory_request_mb,
            )
        await self._service_store.delete(service_name)
        await self._routing_table.remove_service(service_name)
