from __future__ import annotations

import asyncio
import logging

import asyncpg
import redis.asyncio as aioredis

import aws_light.log as log
from aws_light.config import settings
from aws_light.events.redis_event_bus import RedisEventBus
from aws_light.models.events import EventKind, WebSocketEvent
from aws_light.models.service import ServiceState
from aws_light.proxy.health_checker import HealthChecker
from aws_light.proxy.redis_routing_table import RedisRoutingTable
from aws_light.store.postgres_store import PostgresStore

logger = logging.getLogger(__name__)


async def main() -> None:
    log.configure("health-checker")

    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=3)
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)

    routing_table = RedisRoutingTable(redis_client)
    event_bus = RedisEventBus(redis_client)
    service_store: PostgresStore[ServiceState] = PostgresStore(pool, "services", ServiceState)
    await service_store.create_table()

    checker = HealthChecker(
        routing_table=routing_table,
        service_store=service_store,  # type: ignore[arg-type]
        event_bus=event_bus,  # type: ignore[arg-type]
    )

    await checker.start()
    logger.info("Health checker started — interval %ds", settings.health_check_interval_seconds)

    await event_bus.publish(
        WebSocketEvent(
            kind=EventKind.PLATFORM_STARTED,
            payload={
                "component": "health-checker",
                "interval_seconds": settings.health_check_interval_seconds,
                "success_threshold": settings.health_check_success_threshold,
                "failure_threshold": settings.health_check_failure_threshold,
            },
        )
    )

    try:
        await asyncio.Event().wait()
    finally:
        await checker.stop()
        await redis_client.aclose()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
