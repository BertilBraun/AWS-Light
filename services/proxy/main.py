from __future__ import annotations

import asyncio
import logging

import redis.asyncio as aioredis

import aws_light.log as log
from aws_light.config import settings
from aws_light.proxy.load_balancer import RoundRobinBalancer
from aws_light.proxy.proxy_server import ProxyServer
from aws_light.proxy.redis_routing_table import RedisRoutingTable

logger = logging.getLogger(__name__)


async def main() -> None:
    log.configure("proxy")

    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    routing_table = RedisRoutingTable(redis_client)
    balancer = RoundRobinBalancer(routing_table)
    proxy = ProxyServer(balancer=balancer, port=settings.proxy_port, redis_client=redis_client)

    await proxy.start()
    logger.info("Proxy listening on port %d", settings.proxy_port)

    try:
        await asyncio.Event().wait()
    finally:
        await proxy.stop()
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
