from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import asyncpg
import redis.asyncio as aioredis

import aws_light.log as log
from aws_light.compute.docker_client import DockerClient
from aws_light.compute.node_manager import NodeManager
from aws_light.compute.orchestrator import ComputeOrchestrator
from aws_light.compute.scheduler import create_scheduler
from aws_light.config import settings
from aws_light.events.redis_event_bus import RedisEventBus
from aws_light.models.database import DatabaseState
from aws_light.models.deployment import RolloutState
from aws_light.models.events import EventKind, WebSocketEvent
from aws_light.models.node import NodeState
from aws_light.models.secret import SecretSpec
from aws_light.models.service import ServiceState
from aws_light.proxy.redis_routing_table import RedisRoutingTable
from aws_light.secrets.secrets_manager import SecretsManager
from aws_light.store.postgres_store import PostgresStore

logger = logging.getLogger(__name__)

_HEALTHY_MARKER = Path("/app/healthy")


async def main() -> None:
    log.configure("orchestrator")

    docker_client = DockerClient()
    _HEALTHY_MARKER.touch()

    pool = await asyncpg.create_pool(settings.database_url, min_size=2, max_size=5)
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    event_bus = RedisEventBus(redis_client)

    service_store: PostgresStore[ServiceState] = PostgresStore(pool, "services", ServiceState)
    database_store: PostgresStore[DatabaseState] = PostgresStore(pool, "databases", DatabaseState)
    deployment_store: PostgresStore[RolloutState] = PostgresStore(pool, "deployments", RolloutState)
    secret_store: PostgresStore[SecretSpec] = PostgresStore(pool, "secrets", SecretSpec)
    node_store: PostgresStore[NodeState] = PostgresStore(pool, "nodes", NodeState)

    for store in [service_store, database_store, deployment_store, secret_store, node_store]:
        await store.create_table()

    routing_table = RedisRoutingTable(redis_client)
    secrets_manager = SecretsManager(secret_store=secret_store)
    node_manager = NodeManager()
    scheduler = create_scheduler(settings.scheduler_policy)

    orchestrator = ComputeOrchestrator(
        service_store=service_store,
        deployment_store=deployment_store,
        docker_client=docker_client,
        node_manager=node_manager,
        scheduler=scheduler,
        event_bus=event_bus,
        routing_table=routing_table,
        secrets_manager=secrets_manager,
        database_store=database_store,
        redis_client=redis_client,
        node_store=node_store,
    )

    await orchestrator.start()
    logger.info(
        "Orchestrator started — reconcile interval %ds, scheduler=%s",
        settings.reconcile_interval_seconds,
        settings.scheduler_policy,
    )

    await event_bus.publish(
        WebSocketEvent(
            kind=EventKind.PLATFORM_STARTED,
            payload={
                "component": "orchestrator",
                "reconcile_interval_seconds": settings.reconcile_interval_seconds,
                "scheduler_policy": settings.scheduler_policy,
            },
        )
    )

    try:
        await asyncio.Event().wait()
    finally:
        await orchestrator.stop()
        await redis_client.aclose()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
