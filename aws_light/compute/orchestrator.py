from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime

from aws_light.compute.docker_client import DockerClient
from aws_light.compute.node_manager import NodeManager
from aws_light.compute.scheduler import Scheduler, SchedulingError
from aws_light.config import settings
from aws_light.dashboard.event_bus import EventBus
from aws_light.models.common import ResourceStatus
from aws_light.models.deployment import RolloutState
from aws_light.models.events import EventKind, WebSocketEvent
from aws_light.models.node import NodeState, ResourceUsage
from aws_light.models.service import ReplicaState, ServiceState
from aws_light.proxy.routing_table import AnyRoutingTable, ReplicaEndpoint
from aws_light.secrets.secrets_manager import SecretsManager
from aws_light.store.base import AnyStore

logger = logging.getLogger(__name__)

_DOCKER_LABEL_MANAGED = "aws-light.managed"
_DOCKER_LABEL_SERVICE = "aws-light.service"
_DOCKER_LABEL_REPLICA = "aws-light.replica-id"
_DOCKER_LABEL_NODE = "aws-light.node"

_ROLLOUT_HEALTH_WAIT_INTERVAL = 2
_ROLLOUT_HEALTH_WAIT_TIMEOUT = 60


def _node_capacity_payload(node: NodeState) -> dict[str, object]:
    return {
        "node_id": node.spec.node_id,
        "cpu_used": node.usage.cpu_used,
        "cpu_capacity": node.spec.cpu_capacity,
        "cpu_available": node.available_cpu,
        "actual_cpu_used": node.actual_usage.cpu_used,
        "memory_used_mb": node.usage.memory_used_mb,
        "memory_capacity_mb": node.spec.memory_capacity_mb,
        "memory_available_mb": node.available_memory_mb,
        "actual_memory_used_mb": node.actual_usage.memory_used_mb,
        "replica_count": len(node.replica_ids),
    }


class ComputeOrchestrator:
    def __init__(
        self,
        service_store: AnyStore[ServiceState],
        deployment_store: AnyStore[RolloutState],
        docker_client: DockerClient,
        node_manager: NodeManager,
        scheduler: Scheduler,
        event_bus: EventBus,
        routing_table: AnyRoutingTable,
        secrets_manager: SecretsManager,
        redis_client: object | None = None,
        node_store: object | None = None,
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
        self._node_store = node_store
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
            if service_state.status == ResourceStatus.DELETING:
                await self._teardown_service(service_state)
            else:
                await self._reconcile_service(service_state)
        if self._node_store is not None:
            await self._sync_nodes_to_store()

    async def _sync_nodes_to_store(self) -> None:
        for node_state in self._node_manager.get_all_nodes():
            await self._node_store.put(node_state.spec.node_id, node_state)  # type: ignore[union-attr]

    async def _teardown_service(self, service_state: ServiceState) -> None:
        for replica in list(service_state.replicas):
            await self._remove_replica(service_state, replica)
        await self._service_store.delete(service_state.spec.name)
        await self._routing_table.remove_service(service_state.spec.name)
        logger.info("Tore down deleted service %s", service_state.spec.name)

    async def _reconcile_service(self, service_state: ServiceState) -> None:
        spec = service_state.spec
        service_state = await self._refresh_observed_replicas(service_state)
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

    async def _refresh_observed_replicas(self, service_state: ServiceState) -> ServiceState:
        spec = service_state.spec
        changed = False
        observed_replicas: list[ReplicaState] = []

        for replica in service_state.replicas:
            if replica.status != ResourceStatus.RUNNING:
                observed_replicas.append(replica)
                continue

            if not self._docker_client.container_is_running(replica.container_id):
                logger.warning(
                    "Replica %s for %s is missing or stopped; removing from observed state",
                    replica.replica_id[:8],
                    spec.name,
                )
                self._docker_client.remove_container(replica.container_id)
                self._node_manager.deallocate(
                    replica.node_id, replica.replica_id, spec.cpu_request, spec.memory_request_mb
                )
                await self._emit_node_updated(replica.node_id)
                changed = True
                await self._event_bus.publish(
                    WebSocketEvent(
                        kind=EventKind.REPLICA_FAILED,
                        payload={
                            "replica_id": replica.replica_id,
                            "service_name": spec.name,
                            "node_id": replica.node_id,
                            "error": "container missing or stopped",
                        },
                    )
                )
                continue

            if not replica.container_ip:
                replica.container_ip = self._docker_client.get_container_ip(
                    replica.container_id, settings.docker_network
                )
                changed = True

            self._node_manager.allocate(
                replica.node_id, replica.replica_id, spec.cpu_request, spec.memory_request_mb
            )
            observed_replicas.append(replica)

        if changed:
            service_state.replicas = observed_replicas
            service_state.updated_at = datetime.utcnow()
            await self._service_store.put(spec.name, service_state)

        await self._sync_routing_table(service_state)
        return service_state

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
            await self._event_bus.publish(
                WebSocketEvent(
                    kind=EventKind.SCHEDULER_NO_CAPACITY,
                    payload={
                        "service_name": spec.name,
                        "cpu_request": spec.cpu_request,
                        "memory_request_mb": spec.memory_request_mb,
                        "scheduler_policy": settings.scheduler_policy,
                        "candidate_nodes": [
                            _node_capacity_payload(node)
                            for node in sorted(nodes, key=lambda item: item.spec.node_id)
                        ],
                    },
                )
            )
            return
        await self._event_bus.publish(
            WebSocketEvent(
                kind=EventKind.SCHEDULER_SELECTED,
                payload={
                    "service_name": spec.name,
                    "node_id": target_node.spec.node_id,
                    "cpu_request": spec.cpu_request,
                    "memory_request_mb": spec.memory_request_mb,
                    "scheduler_policy": settings.scheduler_policy,
                    "candidate_nodes": [
                        _node_capacity_payload(node)
                        for node in sorted(nodes, key=lambda item: item.spec.node_id)
                    ],
                },
            )
        )

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
                    "actual_cpu_used": node.actual_usage.cpu_used,
                    "memory_used_mb": node.usage.memory_used_mb,
                    "actual_memory_used_mb": node.actual_usage.memory_used_mb,
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
        actual_usage_by_node: dict[str, ResourceUsage] = {
            node.spec.node_id: ResourceUsage() for node in self._node_manager.get_all_nodes()
        }
        for service_state in await self._service_store.list():
            cpu_utilization_samples = []
            requested_cpu = max(service_state.spec.cpu_request, 0.001)
            stats_results = await asyncio.gather(
                *[
                    loop.run_in_executor(
                        None, self._docker_client.get_container_stats, replica.container_id
                    )
                    for replica in service_state.replicas
                ]
            )
            for replica, stats in zip(service_state.replicas, stats_results, strict=True):
                if stats is not None:
                    actual_cpu_cores = stats.cpu_percent / 100
                    cpu_utilization_samples.append((actual_cpu_cores / requested_cpu) * 100)
                    node_usage = actual_usage_by_node.setdefault(replica.node_id, ResourceUsage())
                    node_usage.cpu_used += actual_cpu_cores
                    node_usage.memory_used_mb += stats.memory_mb
            average_cpu = (
                sum(cpu_utilization_samples) / len(cpu_utilization_samples)
                if cpu_utilization_samples
                else 0.0
            )
            await self._redis.set(f"cpu:{service_state.spec.name}", average_cpu)  # type: ignore[union-attr]

        for node_id, usage in actual_usage_by_node.items():
            self._node_manager.set_actual_usage(node_id, usage.cpu_used, usage.memory_used_mb)
            await self._emit_node_updated(node_id)
        if self._node_store is not None:
            await self._sync_nodes_to_store()

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
