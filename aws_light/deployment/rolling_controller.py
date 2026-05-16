from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime

from aws_light.compute.orchestrator import ComputeOrchestrator
from aws_light.dashboard.event_bus import EventBus
from aws_light.models.common import ResourceStatus
from aws_light.models.deployment import DeploymentSpec, RolloutState
from aws_light.models.events import EventKind, WebSocketEvent
from aws_light.models.service import ServiceState
from aws_light.store.json_store import JsonStore

logger = logging.getLogger(__name__)

_HEALTH_WAIT_INTERVAL_SECONDS = 2
_HEALTH_WAIT_TIMEOUT_SECONDS = 60


class RollingController:
    def __init__(
        self,
        service_store: JsonStore[ServiceState],
        deployment_store: JsonStore[RolloutState],
        orchestrator: ComputeOrchestrator,
        event_bus: EventBus,
    ) -> None:
        self._service_store = service_store
        self._deployment_store = deployment_store
        self._orchestrator = orchestrator
        self._event_bus = event_bus

    async def start_rollout(self, spec: DeploymentSpec) -> RolloutState:
        service_state = await self._service_store.get(spec.service_name)
        if service_state is None:
            raise ValueError(f"Service '{spec.service_name}' not found")

        rollout = RolloutState(
            deployment_id=str(uuid.uuid4()),
            spec=spec,
            status=ResourceStatus.PENDING,
            old_replica_ids=[r.replica_id for r in service_state.replicas],
        )
        await self._deployment_store.put(rollout.deployment_id, rollout)
        asyncio.create_task(self._execute_rollout(rollout))
        return rollout

    async def _execute_rollout(self, rollout: RolloutState) -> None:
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

        batch_size = max(1, strategy.max_surge)

        while old_replicas:
            batch = old_replicas[:batch_size]
            old_replicas = old_replicas[batch_size:]

            current_service = await self._service_store.get(rollout.spec.service_name)
            if current_service is None:
                break

            current_service.spec.replicas += len(batch)
            await self._service_store.put(current_service.spec.name, current_service)

            await asyncio.sleep(_HEALTH_WAIT_INTERVAL_SECONDS)
            waited = _HEALTH_WAIT_INTERVAL_SECONDS
            while waited < _HEALTH_WAIT_TIMEOUT_SECONDS:
                current_service = await self._service_store.get(rollout.spec.service_name)
                if current_service is None:
                    break
                running_count = sum(
                    1
                    for r in current_service.replicas
                    if r.status == ResourceStatus.RUNNING
                    and r.replica_id not in {old.replica_id for old in batch}
                )
                if running_count >= len(batch):
                    break
                await asyncio.sleep(_HEALTH_WAIT_INTERVAL_SECONDS)
                waited += _HEALTH_WAIT_INTERVAL_SECONDS

            current_service = await self._service_store.get(rollout.spec.service_name)
            if current_service is None:
                break
            current_service.spec.replicas -= len(batch)
            await self._service_store.put(current_service.spec.name, current_service)

            for old_replica in batch:
                await self._orchestrator._remove_replica(current_service, old_replica)

            step += len(batch)
            await self._emit_progress(
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

    async def _emit_progress(
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
