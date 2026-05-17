from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import aws_light.dependencies as deps
import aws_light.log as log
from aws_light.api import deployments as deployments_router
from aws_light.api import iac as iac_router
from aws_light.api import iam as iam_router
from aws_light.api import nodes as nodes_router
from aws_light.api import overview as overview_router
from aws_light.api import platform as platform_router
from aws_light.api import secrets as secrets_router
from aws_light.api import services as services_router
from aws_light.api import storage as storage_router
from aws_light.api import topology as topology_router
from aws_light.api import websocket as websocket_router
from aws_light.config import settings
from aws_light.events.redis_event_bus import RedisEventBus
from aws_light.iac.applier import Applier
from aws_light.iac.differ import Differ
from aws_light.iam.auth import make_default_admin
from aws_light.models.deployment import RolloutState
from aws_light.models.iam import UserSpec
from aws_light.models.node import NodeState
from aws_light.models.secret import SecretSpec
from aws_light.models.service import ServiceState
from aws_light.secrets.secrets_manager import SecretsManager
from aws_light.storage.presigned import PresignedUrlService
from aws_light.storage.storage_service import StorageService
from aws_light.store.postgres_store import PostgresStore

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log.configure("control-plane")
    settings.ensure_data_directories()

    pool = await asyncpg.create_pool(settings.database_url, min_size=2, max_size=10)
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    event_bus = RedisEventBus(redis_client)

    service_store: PostgresStore[ServiceState] = PostgresStore(pool, "services", ServiceState)
    deployment_store: PostgresStore[RolloutState] = PostgresStore(pool, "deployments", RolloutState)
    node_store: PostgresStore[NodeState] = PostgresStore(pool, "nodes", NodeState)
    secret_pg_store: PostgresStore[SecretSpec] = PostgresStore(pool, "secrets", SecretSpec)
    user_store: PostgresStore[UserSpec] = PostgresStore(pool, "users", UserSpec)

    for store in [service_store, deployment_store, node_store, secret_pg_store, user_store]:
        await store.create_table()

    secrets_manager = SecretsManager(secret_store=secret_pg_store)
    storage_service = StorageService(storage_root=settings.data_directory / "storage")
    presigned_service = PresignedUrlService(
        secret_key=settings.jwt_secret,
        base_url=f"http://localhost:{settings.api_port}",
    )
    applier = Applier(
        service_store=service_store,
        secrets_manager=secrets_manager,
        storage_service=storage_service,
        differ=Differ(),
        event_bus=event_bus,
    )

    # Register into the shared dependency registry used by all API routes.
    deps._service_store = service_store
    deps._deployment_store = deployment_store
    deps._node_store = node_store
    deps._user_store = user_store
    deps._secrets_manager = secrets_manager
    deps._storage_service = storage_service
    deps._presigned_service = presigned_service
    deps._applier = applier
    deps._event_bus = event_bus

    await _seed_default_admin(user_store)
    logger.info("Control-plane ready on port %d", settings.api_port)

    yield

    await redis_client.aclose()
    await pool.close()


async def _seed_default_admin(user_store: PostgresStore[UserSpec]) -> None:
    if not await user_store.exists(settings.default_admin_username):
        admin = make_default_admin()
        await user_store.put(admin.username, admin)


def create_app(lifespan_override: object = None) -> FastAPI:
    chosen_lifespan = lifespan_override if lifespan_override is not None else lifespan
    app = FastAPI(title="AWS Light", version="0.1.0", lifespan=chosen_lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(iam_router.router)
    app.include_router(services_router.router)
    app.include_router(nodes_router.router)
    app.include_router(overview_router.router)
    app.include_router(platform_router.router)
    app.include_router(topology_router.router)
    app.include_router(deployments_router.router)
    app.include_router(secrets_router.router)
    app.include_router(storage_router.router)
    app.include_router(iac_router.router)
    app.include_router(websocket_router.router)

    static_path = Path(deps.__file__).parent / "static"
    if static_path.exists():
        app.mount("/static", StaticFiles(directory=static_path), name="static")

        @app.get("/")
        async def serve_dashboard() -> FileResponse:
            return FileResponse(static_path / "index.html")

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=settings.api_port)
