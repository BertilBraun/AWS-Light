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

import aws_light.log as log
from aws_light.api import deployments as deployments_router
from aws_light.api import iac as iac_router
from aws_light.api import iam as iam_router
from aws_light.api import nodes as nodes_router
from aws_light.api import secrets as secrets_router
from aws_light.api import services as services_router
from aws_light.api import storage as storage_router
from aws_light.api import websocket as websocket_router
from aws_light.config import settings
from aws_light.events.redis_event_bus import RedisEventBus
from aws_light.iac.applier import Applier
from aws_light.iac.differ import Differ
from aws_light.iam.auth import make_default_admin
from aws_light.models.deployment import RolloutState
from aws_light.models.secret import SecretSpec
from aws_light.models.service import ServiceState
from aws_light.secrets.secrets_manager import SecretsManager
from aws_light.storage.presigned import PresignedUrlService
from aws_light.storage.storage_service import StorageService
from aws_light.store.postgres_store import PostgresStore

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None
_redis: aioredis.Redis | None = None
_service_store: PostgresStore[ServiceState] | None = None
_deployment_store: PostgresStore[RolloutState] | None = None
_secrets_manager: SecretsManager | None = None
_storage_service: StorageService | None = None
_presigned_service: PresignedUrlService | None = None
_applier: Applier | None = None
_event_bus: RedisEventBus | None = None


def get_service_store() -> PostgresStore[ServiceState]:
    assert _service_store is not None
    return _service_store


def get_deployment_store() -> PostgresStore[RolloutState]:
    assert _deployment_store is not None
    return _deployment_store


def get_secrets_manager() -> SecretsManager:
    assert _secrets_manager is not None
    return _secrets_manager


def get_storage_service() -> StorageService:
    assert _storage_service is not None
    return _storage_service


def get_presigned_service() -> PresignedUrlService:
    assert _presigned_service is not None
    return _presigned_service


def get_applier() -> Applier:
    assert _applier is not None
    return _applier


def get_event_bus() -> RedisEventBus:
    assert _event_bus is not None
    return _event_bus


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _pool, _redis, _service_store, _deployment_store
    global _secrets_manager, _storage_service, _presigned_service, _applier, _event_bus

    log.configure("control-plane")
    settings.ensure_data_directories()

    _pool = await asyncpg.create_pool(settings.database_url, min_size=2, max_size=10)
    _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    _event_bus = RedisEventBus(_redis)

    _service_store = PostgresStore(_pool, "services", ServiceState)
    _deployment_store = PostgresStore(_pool, "deployments", RolloutState)
    secret_pg_store: PostgresStore[SecretSpec] = PostgresStore(_pool, "secrets", SecretSpec)

    for store in [_service_store, _deployment_store, secret_pg_store]:
        await store.create_table()

    _secrets_manager = SecretsManager(secret_store=secret_pg_store)
    _storage_service = StorageService(storage_root=settings.data_directory / "storage")
    _presigned_service = PresignedUrlService(
        secret_key=settings.jwt_secret,
        base_url=f"http://localhost:{settings.api_port}",
    )
    _applier = Applier(
        service_store=_service_store,
        secrets_manager=_secrets_manager,
        storage_service=_storage_service,
        differ=Differ(),
        event_bus=_event_bus,
    )

    await _seed_default_admin()
    logger.info("Control-plane ready on port %d", settings.api_port)

    yield

    await _redis.aclose()
    await _pool.close()


async def _seed_default_admin() -> None:
    from aws_light.models.iam import UserSpec
    from aws_light.store.postgres_store import PostgresStore as PG

    user_store: PG[UserSpec] = PostgresStore(_pool, "users", UserSpec)  # type: ignore[arg-type]
    await user_store.create_table()
    if not await user_store.exists(settings.default_admin_username):
        admin = make_default_admin()
        await user_store.put(admin.username, admin)


def create_app() -> FastAPI:
    app = FastAPI(title="AWS Light", version="0.1.0", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(iam_router.router)
    app.include_router(services_router.router)
    app.include_router(nodes_router.router)
    app.include_router(deployments_router.router)
    app.include_router(secrets_router.router)
    app.include_router(storage_router.router)
    app.include_router(iac_router.router)
    app.include_router(websocket_router.router)

    static_path = Path(__file__).parent.parent.parent / "aws_light" / "static"
    if static_path.exists():
        app.mount("/static", StaticFiles(directory=static_path), name="static")

        @app.get("/")
        async def serve_dashboard() -> FileResponse:
            return FileResponse(static_path / "index.html")

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=settings.api_port)
