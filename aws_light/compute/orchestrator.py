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
from aws_light.models.deployment import RolloutState
from aws_light.models.events import EventKind, WebSocketEvent
from aws_light.models.service import ReplicaState, ServiceState
from aws_light.proxy.routing_table import AnyRoutingTable, ReplicaEndpoint
from aws_light.secrets.secrets_manager import SecretsManager
from aws_light.store.json_store import JsonStore

logger = logging.getLogger(__name__)

_DOCKER_LABEL_MANAGED = "aws-light.managed"
_DOCKER_LABEL_SERVICE = "aws-light.service"
_DOCKER_LABEL_REPLICA = "aws-light.replica-id"
_DOCKER_LABEL_NODE = "aws-light.node"

_ROLLOUT_HEALTH_WAIT_INTERVAL = 2
_ROLLOUT_HEALTH_WAIT_TIMEOUT = 60


class ComputeOrchestrator:
    def __init__(
        self,
        service_store: JsonStore[ServiceState],
        deployment_store: JsonStore[RolloutState],
        docker_client: DockerClient,
        node_manager: NodeManager,
        scheduler: BinPackScheduler,
        event_bus: EventBus,
        routing_table: AnyRoutingTable,
        secrets_manager: SecretsManager,
        redis_client: object | None = None,
    ) -> None:
        self._service_store = service_store
        self._deployment_store = deployment_store
        self._docker_client = docker_client
        self._node_manager = node_manager
        self._scheduler = scheduler
        self._event_bus = event_bus
        self._routing_table = routing_table
        self._secrets_manager = secrets_manager
        self._redis = redis_client
        self._running = False
        self._executing_rollouts: set[str] = set()

    async def start(self) -> None:
        self._running = True
        self._docker_client.ensure_network(settings.docker_network)
        self._node_manager.initialize()
        await self._remove_orphan_containers()
        asyncio.create_task(self._reconcile_loop())
        asyncio.create_task(self._rollout_loop())
        if self._redis is not None:
            asyncio.create_task(self._cpu_stats_loop())

    async def stop(self) -> None:
        self._running = False

    # ── Reconcile loop ────────────────────────────────────────────────────────

    async def _reconcile_loop(self) -> None:
        while self._running:
            try:
                await self._reconcile_all()
            except Exception:
                logger.exception("Error during reconcile loop")
            await asyncio.sleep(settings.reconcile_interval_seconds)

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

        stale_replicas = [r for r in running_replicas if r.image and r.image != spec.image]
        for stale in stale_replicas:
            await self._remove_replica(service_state, stale)
            replicas_changed = True

        if stale_replicas:
            service_state = await self._service_store.get(spec.name) or service_state
            running_replicas = [
                r for r in service_state.replicas if r.status == ResourceStatus.RUNNING
            ]
            actual_count = len(running_replicas)

        if actual_count < desired_count:
            for _ in range(desired_count - actual_count):
                await self._create_replica(service_state)
                replicas_changed = True
        elif actual_count > desired_count:
            for replica in running_replicas[desired_count:]:
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

    # ── Rollout loop ──────────────────────────────────────────────────────────

    async def _rollout_loop(self) -> None:
        while self._running:
            try:
                await self._dispatch_pending_rollouts()
            except Exception:
                logger.exception("Error during rollout loop")
            await asyncio.sleep(settings.rollout_poll_interval_seconds)

    async def _dispatch_pending_rollouts(self) -> None:
        for rollout in await self._deployment_store.list():
            if (
                rollout.status == ResourceStatus.PENDING
                and rollout.deployment_id not in self._executing_rollouts
            ):
                self._executing_rollouts.add(rollout.deployment_id)
                asyncio.create_task(self._execute_rollout(rollout))

    async def _execute_rollout(self, rollout: RolloutState) -> None:
        try:
            await self._run_rollout(rollout)
        finally:
            self._executing_rollouts.discard(rollout.deployment_id)

    async def _run_rollout(self, rollout: RolloutState) -> None:
        rollout.status = ResourceStatus.UPDATING
        await self._deployment_store.put(rollout.deployment_id, rollout)

        service_state = await self._service_store.get(rollout.spec.service_name)
        if service_state is None:
            return

        strategy = rollout.spec.strategy
        old_replicas = list(service_state.replicas)
        total_steps = len(old_replicas)
        step = 0

        service_state.spec.image = rollout.spec.new_image
        service_state.status = ResourceStatus.UPDATING
        service_state.updated_at = datetime.utcnow()
        await self._service_store.put(service_state.spec.name, service_state)

        logger.info(
            "Rollout %s: updating %s to %s (%d replicas)",
            rollout.deployment_id[:8],
            rollout.spec.service_name,
            rollout.spec.new_image,
            total_steps,
        )

        batch_size = max(1, strategy.max_surge)
        while old_replicas:
            batch = old_replicas[:batch_size]
            old_replicas = old_replicas[batch_size:]

            current_service = await self._service_store.get(rollout.spec.service_name)
            if current_service is None:
                break

            current_service.spec.replicas += len(batch)
            await self._service_store.put(current_service.spec.name, current_service)

            waited = 0
            while waited < _ROLLOUT_HEALTH_WAIT_TIMEOUT:
                await asyncio.sleep(_ROLLOUT_HEALTH_WAIT_INTERVAL)
                waited += _ROLLOUT_HEALTH_WAIT_INTERVAL
                current_service = await self._service_store.get(rollout.spec.service_name)
                if current_service is None:
                    break
                old_ids = {r.replica_id for r in batch}
                running_new = sum(
                    1
                    for r in current_service.replicas
                    if r.status == ResourceStatus.RUNNING and r.replica_id not in old_ids
                )
                if running_new >= len(batch):
                    break

            current_service = await self._service_store.get(rollout.spec.service_name)
            if current_service is None:
                break
            current_service.spec.replicas -= len(batch)
            await self._service_store.put(current_service.spec.name, current_service)

            for old_replica in batch:
                await self._remove_replica(current_service, old_replica)

            step += len(batch)
            await self._emit_rollout_progress(
                rollout.deployment_id, rollout.spec.service_name, step, total_steps
            )

        rollout.status = ResourceStatus.RUNNING
        rollout.completed_at = datetime.utcnow()
        await self._deployment_store.put(rollout.deployment_id, rollout)

        final_service = await self._service_store.get(rollout.spec.service_name)
        if final_service is not None:
            final_service.status = ResourceStatus.RUNNING
            final_service.updated_at = datetime.utcnow()
            await self._service_store.put(final_service.spec.name, final_service)

        logger.info(
            "Rollout %s completed for %s", rollout.deployment_id[:8], rollout.spec.service_name
        )

    # ── Replica lifecycle ─────────────────────────────────────────────────────

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
            container_id, container_ip = self._docker_client.create_container(
                image=spec.image,
                name=container_name,
                env=merged_env,
                cpu_quota=spec.cpu_request,
                memory_mb=spec.memory_request_mb,
                network=settings.docker_network,
                labels=labels,
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
            container_ip=container_ip,
            image=spec.image,
            started_at=datetime.utcnow(),
        )

        current_service = await self._service_store.get(spec.name)
        if current_service is not None:
            current_service.replicas.append(replica)
            current_service.updated_at = datetime.utcnow()
            await self._service_store.put(spec.name, current_service)
            await self._sync_routing_table(current_service)

        logger.info(
            "Started replica %s for %s on node %s (ip=%s)",
            replica_id[:8],
            spec.name,
            target_node.spec.node_id,
            container_ip,
        )
        await self._event_bus.publish(
            WebSocketEvent(
                kind=EventKind.REPLICA_STARTED,
                payload={
                    "replica_id": replica_id,
                    "service_name": spec.name,
                    "node_id": target_node.spec.node_id,
                    "container_ip": container_ip,
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

        logger.info("Stopped replica %s for %s", replica.replica_id[:8], spec.name)
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
                host=replica.container_ip,
                port=service_state.spec.port,
            )
            for replica in service_state.replicas
            if replica.status == ResourceStatus.RUNNING and replica.container_ip
        ]
        await self._routing_table.update_service(service_state.spec.name, endpoints)

    async def _remove_orphan_containers(self) -> None:
        known_container_ids: set[str] = set()
        for service_state in await self._service_store.list():
            for replica in service_state.replicas:
                known_container_ids.add(replica.container_id)

        for orphan in self._docker_client.list_containers_by_label(_DOCKER_LABEL_MANAGED, "true"):
            if orphan.container_id not in known_container_ids:
                logger.info("Removing orphan container %s", orphan.name)
                self._docker_client.remove_container(orphan.container_id)

    # ── Emitters ──────────────────────────────────────────────────────────────

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

    async def _emit_rollout_progress(
        self, deployment_id: str, service_name: str, step: int, total_steps: int
    ) -> None:
        await self._event_bus.publish(
            WebSocketEvent(
                kind=EventKind.ROLLOUT_PROGRESS,
                payload={
                    "deployment_id": deployment_id,
                    "service_name": service_name,
                    "step": step,
                    "total_steps": total_steps,
                    "status": "in_progress" if step < total_steps else "complete",
                },
            )
        )

    # ── CPU stats loop ────────────────────────────────────────────────────────

    async def _cpu_stats_loop(self) -> None:
        while self._running:
            try:
                await self._collect_and_publish_cpu_stats()
            except Exception:
                logger.exception("Error in CPU stats loop")
            await asyncio.sleep(settings.cpu_stats_interval_seconds)

    async def _collect_and_publish_cpu_stats(self) -> None:
        loop = asyncio.get_running_loop()
        for service_state in await self._service_store.list():
            samples = []
            for replica in service_state.replicas:
                container_id = replica.container_id
                stats = await loop.run_in_executor(
                    None, self._docker_client.get_container_stats, container_id
                )
                if stats is not None:
                    samples.append(stats.cpu_percent)
            average_cpu = sum(samples) / len(samples) if samples else 0.0
            await self._redis.set(f"cpu:{service_state.spec.name}", average_cpu)  # type: ignore[union-attr]

    # ── Public API ────────────────────────────────────────────────────────────

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
        logger.info("Deleted service %s", service_name)
