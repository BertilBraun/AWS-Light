from __future__ import annotations

import asyncio
import logging

import httpx

from aws_light.config import settings
from aws_light.dashboard.event_bus import EventBus
from aws_light.models.events import EventKind, WebSocketEvent
from aws_light.models.service import ServiceState
from aws_light.proxy.routing_table import RoutingTable
from aws_light.store.json_store import JsonStore

logger = logging.getLogger(__name__)

_FAILURE_THRESHOLD = 3


class HealthChecker:
    def __init__(
        self,
        routing_table: RoutingTable,
        service_store: JsonStore[ServiceState],
        event_bus: EventBus,
    ) -> None:
        self._routing_table = routing_table
        self._service_store = service_store
        self._event_bus = event_bus
        self._consecutive_failures: dict[str, int] = {}
        self._running = False

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._health_check_loop())

    async def stop(self) -> None:
        self._running = False

    async def _health_check_loop(self) -> None:
        while self._running:
            try:
                await self._check_all_services()
            except Exception:
                logger.exception("Error during health check loop")
            await asyncio.sleep(settings.health_check_interval_seconds)

    async def _check_all_services(self) -> None:
        all_services = await self._service_store.list()
        for service_state in all_services:
            health_check_path = service_state.spec.health_check_path
            for replica in service_state.replicas:
                await self._check_replica(
                    replica.replica_id,
                    replica.host_port,
                    health_check_path,
                    service_state.spec.name,
                )

    async def _check_replica(
        self,
        replica_id: str,
        host_port: int,
        health_check_path: str,
        service_name: str,
    ) -> None:
        url = f"http://127.0.0.1:{host_port}{health_check_path}"
        healthy = await _probe_http(url)

        if healthy:
            self._consecutive_failures.pop(replica_id, None)
            await self._routing_table.set_healthy(replica_id, True)
        else:
            failures = self._consecutive_failures.get(replica_id, 0) + 1
            self._consecutive_failures[replica_id] = failures
            if failures >= _FAILURE_THRESHOLD:
                await self._routing_table.set_healthy(replica_id, False)
                await self._event_bus.publish(
                    WebSocketEvent(
                        kind=EventKind.HEALTH_CHECK_FAILED,
                        payload={
                            "replica_id": replica_id,
                            "service_name": service_name,
                            "consecutive_failures": failures,
                        },
                    )
                )


async def _probe_http(url: str) -> bool:
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=2.0, read=5.0, write=5.0, pool=5.0)
        ) as client:
            response = await client.get(url)
            return response.status_code < 500
    except Exception:
        return False
