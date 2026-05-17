from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import FastAPI

import aws_light.config as config_module
import aws_light.dependencies as deps
import aws_light.log as log
from aws_light.api import deployments as deployments_router
from aws_light.api import iac as iac_router
from aws_light.api import iam as iam_router
from aws_light.api import nodes as nodes_router
from aws_light.api import platform as platform_router
from aws_light.api import secrets as secrets_router
from aws_light.api import services as services_router
from aws_light.api import storage as storage_router
from aws_light.api import websocket as websocket_router
from aws_light.autoscaler.autoscaler import Autoscaler
from aws_light.autoscaler.metrics_collector import MetricsCollector
from aws_light.compute.docker_client import DockerClient
from aws_light.compute.node_manager import NodeManager
from aws_light.compute.orchestrator import ComputeOrchestrator
from aws_light.compute.scheduler import BinPackScheduler
from aws_light.dashboard.event_bus import EventBus
from aws_light.iac.applier import Applier
from aws_light.iac.differ import Differ
from aws_light.iam.auth import make_default_admin
from aws_light.models.deployment import RolloutState
from aws_light.models.iam import UserSpec
from aws_light.models.node import NodeState
from aws_light.models.secret import SecretSpec
from aws_light.models.service import ServiceState
from aws_light.proxy.health_checker import HealthChecker
from aws_light.proxy.load_balancer import RoundRobinBalancer
from aws_light.proxy.proxy_server import ProxyServer
from aws_light.proxy.routing_table import RoutingTable
from aws_light.secrets.secrets_manager import SecretsManager
from aws_light.storage.presigned import PresignedUrlService
from aws_light.storage.storage_service import StorageService
from aws_light.store.json_store import JsonStore


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log.configure("monolith")
    settings = config_module.settings
    settings.ensure_data_directories()

    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)

    event_bus = EventBus()
    service_store: JsonStore[ServiceState] = JsonStore(
        settings.data_directory / "services.json", ServiceState
    )
    deployment_store: JsonStore[RolloutState] = JsonStore(
        settings.data_directory / "deployments.json", RolloutState
    )
    node_store: JsonStore[NodeState] = JsonStore(settings.data_directory / "nodes.json", NodeState)
    user_store: JsonStore[UserSpec] = JsonStore(settings.data_directory / "users.json", UserSpec)
    secret_store: JsonStore[SecretSpec] = JsonStore(
        settings.data_directory / "secrets.json", SecretSpec
    )

    node_manager = NodeManager()
    routing_table = RoutingTable()
    secrets_manager = SecretsManager(secret_store=secret_store)
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

    await _seed_default_admin(user_store, settings)

    docker_client = DockerClient()
    orchestrator = ComputeOrchestrator(
        service_store=service_store,
        deployment_store=deployment_store,
        docker_client=docker_client,
        node_manager=node_manager,
        scheduler=BinPackScheduler(),
        event_bus=event_bus,
        routing_table=routing_table,
        secrets_manager=secrets_manager,
        redis_client=redis_client,
        node_store=node_store,
    )

    balancer = RoundRobinBalancer(routing_table)
    proxy_server = ProxyServer(
        balancer=balancer, port=settings.proxy_port, redis_client=redis_client
    )
    health_checker = HealthChecker(
        routing_table=routing_table,
        service_store=service_store,
        event_bus=event_bus,
    )
    metrics_collector = MetricsCollector(redis_client=redis_client)
    autoscaler = Autoscaler(
        service_store=service_store,
        metrics_collector=metrics_collector,
        event_bus=event_bus,
    )

    await orchestrator.start()
    await health_checker.start()
    await proxy_server.start()
    await autoscaler.start()

    yield

    await autoscaler.stop()
    await proxy_server.stop()
    await health_checker.stop()
    await orchestrator.stop()
    await redis_client.aclose()


async def _seed_default_admin(user_store: JsonStore[UserSpec], settings: object) -> None:
    username = getattr(settings, "default_admin_username", "admin")
    if not await user_store.exists(username):
        admin = make_default_admin()
        await user_store.put(admin.username, admin)


def create_app(lifespan_override: object = None) -> FastAPI:
    chosen_lifespan = lifespan_override if lifespan_override is not None else lifespan
    app = FastAPI(title="AWS Light", version="0.1.0", lifespan=chosen_lifespan)
    app.include_router(iam_router.router)
    app.include_router(services_router.router)
    app.include_router(nodes_router.router)
    app.include_router(platform_router.router)
    app.include_router(deployments_router.router)
    app.include_router(secrets_router.router)
    app.include_router(storage_router.router)
    app.include_router(iac_router.router)
    app.include_router(websocket_router.router)
    static_path = Path(__file__).parent / "static"
    if static_path.exists():
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles

        app.mount("/static", StaticFiles(directory=static_path), name="static")

        @app.get("/")
        async def serve_dashboard() -> FileResponse:
            return FileResponse(static_path / "index.html")

    return app


app = create_app()
