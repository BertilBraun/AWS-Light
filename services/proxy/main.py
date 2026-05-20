from __future__ import annotations

import asyncio
import logging

import asyncpg
import redis.asyncio as aioredis

import aws_light.log as log
from aws_light.config import settings
from aws_light.events.redis_event_bus import RedisEventBus
from aws_light.models.events import EventKind, WebSocketEvent
from aws_light.models.secret import SecretSpec
from aws_light.models.service import ServiceState
from aws_light.proxy.load_balancer import RoundRobinBalancer
from aws_light.proxy.proxy_server import ProxyServer
from aws_light.proxy.redis_routing_table import RedisRoutingTable
from aws_light.secrets.secrets_manager import SecretsManager
from aws_light.store.cached_store import TTLStoreCache
from aws_light.store.postgres_store import PostgresStore

logger = logging.getLogger(__name__)

_PROXY_STORE_CACHE_TTL_SECONDS = 5.0
_PROXY_STORE_CACHE_MAX_ENTRIES = 1024


async def main() -> None:
    log.configure("proxy")

    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=3)
    event_bus = RedisEventBus(redis_client)
    routing_table = RedisRoutingTable(redis_client)
    service_store: PostgresStore[ServiceState] = PostgresStore(pool, "services", ServiceState)
    secret_store: PostgresStore[SecretSpec] = PostgresStore(pool, "secrets", SecretSpec)
    await service_store.create_table()
    await secret_store.create_table()
    cached_service_store = TTLStoreCache(
        service_store,
        ttl_seconds=_PROXY_STORE_CACHE_TTL_SECONDS,
        max_entries=_PROXY_STORE_CACHE_MAX_ENTRIES,
    )
    cached_secret_store = TTLStoreCache(
        secret_store,
        ttl_seconds=_PROXY_STORE_CACHE_TTL_SECONDS,
        max_entries=_PROXY_STORE_CACHE_MAX_ENTRIES,
    )
    secrets_manager = SecretsManager(secret_store=cached_secret_store)
    balancer = RoundRobinBalancer(routing_table)
    proxy = ProxyServer(
        balancer=balancer,
        port=settings.proxy_port,
        redis_client=redis_client,
        event_bus=event_bus,  # type: ignore[arg-type]
        service_store=cached_service_store,
        secrets_manager=secrets_manager,
    )

    await proxy.start()
    logger.info("Proxy listening on port %d", settings.proxy_port)
    await event_bus.publish(
        WebSocketEvent(
            kind=EventKind.PLATFORM_STARTED,
            payload={"component": "proxy", "port": settings.proxy_port},
        )
    )

    try:
        await asyncio.Event().wait()
    finally:
        await proxy.stop()
        await redis_client.aclose()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
