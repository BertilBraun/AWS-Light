from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from aws_light.autoscaler.metrics_collector import MetricsCollector
from aws_light.config import settings
from aws_light.dashboard.event_bus import EventBus
from aws_light.models.events import EventKind, WebSocketEvent
from aws_light.models.service import ServiceState
from aws_light.store.json_store import JsonStore

logger = logging.getLogger(__name__)

_SCALE_UP_CPU_THRESHOLD = 70.0
_SCALE_UP_RPS_THRESHOLD = 100.0
_SCALE_DOWN_CPU_THRESHOLD = 20.0
_SCALE_DOWN_RPS_THRESHOLD = 10.0
_SCALE_DOWN_CONSECUTIVE_CHECKS_REQUIRED = 3


class Autoscaler:
    def __init__(
        self,
        service_store: JsonStore[ServiceState],
        metrics_collector: MetricsCollector,
        event_bus: EventBus,
    ) -> None:
        self._service_store = service_store
        self._metrics_collector = metrics_collector
        self._event_bus = event_bus
        self._scale_down_counters: dict[str, int] = {}
        self._running = False

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._autoscale_loop())

    async def stop(self) -> None:
        self._running = False

    async def _autoscale_loop(self) -> None:
        while self._running:
            try:
                await self._evaluate_all_services()
            except Exception:
                logger.exception("Error in autoscaler loop")
            await asyncio.sleep(settings.autoscaler_interval_seconds)

    async def _evaluate_all_services(self) -> None:
        all_services = await self._service_store.list()
        for service_state in all_services:
            spec = service_state.spec
            if spec.min_replicas == spec.max_replicas:
                continue
            await self._evaluate_service(service_state)

    async def _evaluate_service(self, service_state: ServiceState) -> None:
        spec = service_state.spec
        current_replicas = spec.replicas
        metrics = await self._metrics_collector.collect(spec.name)

        scale_up = (
            metrics.average_cpu_percent > _SCALE_UP_CPU_THRESHOLD
            or metrics.requests_per_second > _SCALE_UP_RPS_THRESHOLD
        )
        scale_down_candidate = (
            metrics.average_cpu_percent < _SCALE_DOWN_CPU_THRESHOLD
            and metrics.requests_per_second < _SCALE_DOWN_RPS_THRESHOLD
        )

        if scale_up and current_replicas < spec.max_replicas:
            new_replica_count = min(current_replicas + 1, spec.max_replicas)
            self._scale_down_counters.pop(spec.name, None)
            await self._apply_scale(service_state, new_replica_count, "scale_up", metrics)
        elif scale_down_candidate and current_replicas > spec.min_replicas:
            consecutive = self._scale_down_counters.get(spec.name, 0) + 1
            self._scale_down_counters[spec.name] = consecutive
            if consecutive >= _SCALE_DOWN_CONSECUTIVE_CHECKS_REQUIRED:
                new_replica_count = max(current_replicas - 1, spec.min_replicas)
                self._scale_down_counters.pop(spec.name, None)
                await self._apply_scale(service_state, new_replica_count, "scale_down", metrics)
        else:
            self._scale_down_counters.pop(spec.name, None)

    async def _apply_scale(
        self,
        service_state: ServiceState,
        new_replica_count: int,
        reason: str,
        metrics: object,
    ) -> None:
        spec = service_state.spec
        old_replica_count = spec.replicas
        refreshed = await self._service_store.get(spec.name)
        if refreshed is None:
            return
        refreshed.spec.replicas = new_replica_count
        refreshed.updated_at = datetime.utcnow()
        await self._service_store.put(spec.name, refreshed)

        logger.info(
            "Autoscaler: %s %s -> %d replicas (reason=%s)",
            spec.name,
            old_replica_count,
            new_replica_count,
            reason,
        )
        await self._event_bus.publish(
            WebSocketEvent(
                kind=EventKind.AUTOSCALE_TRIGGERED,
                payload={
                    "service_name": spec.name,
                    "from_replicas": old_replica_count,
                    "to_replicas": new_replica_count,
                    "reason": reason,
                },
            )
        )
