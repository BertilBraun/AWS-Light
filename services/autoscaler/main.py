from __future__ import annotations

import asyncio
import logging

import asyncpg
import redis.asyncio as aioredis

import aws_light.log as log
from aws_light.autoscaler.autoscaler import Autoscaler
from aws_light.autoscaler.metrics_collector import MetricsCollector
from aws_light.config import settings
from aws_light.events.redis_event_bus import RedisEventBus
from aws_light.models.service import ServiceState
from aws_light.store.postgres_store import PostgresStore

logger = logging.getLogger(__name__)


async def main() -> None:
    log.configure("autoscaler")

    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=3)
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)

    service_store: PostgresStore[ServiceState] = PostgresStore(pool, "services", ServiceState)
    await service_store.create_table()

    event_bus = RedisEventBus(redis_client)
    metrics_collector = MetricsCollector(redis_client=redis_client)

    autoscaler = Autoscaler(
        service_store=service_store,  # type: ignore[arg-type]
        metrics_collector=metrics_collector,
        event_bus=event_bus,  # type: ignore[arg-type]
    )

    await autoscaler.start()
    logger.info("Autoscaler started — interval %ds", settings.autoscaler_interval_seconds)

    try:
        await asyncio.Event().wait()
    finally:
        await autoscaler.stop()
        await redis_client.aclose()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
