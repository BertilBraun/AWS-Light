from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import FastAPI

import aws_light.config as config_module
import aws_light.log as log
from aws_light.api import deployments as deployments_router
from aws_light.api import iac as iac_router
from aws_light.api import iam as iam_router
from aws_light.api import nodes as nodes_router
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
from aws_light.dependencies import get_user_store
from aws_light.iac.applier import Applier
from aws_light.iac.differ import Differ
from aws_light.iam.auth import make_default_admin
from aws_light.models.deployment import RolloutState
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

_service_store: JsonStore[ServiceState] | None = None
_deployment_store: JsonStore[RolloutState] | None = None
_node_manager: NodeManager | None = None
_orchestrator: ComputeOrchestrator | None = None
_routing_table: RoutingTable | None = None
_proxy_server: ProxyServer | None = None
_health_checker: HealthChecker | None = None
_event_bus: EventBus | None = None
_autoscaler: Autoscaler | None = None
_secrets_manager: SecretsManager | None = None
_storage_service: StorageService | None = None
_presigned_service: PresignedUrlService | None = None
_applier: Applier | None = None


def get_service_store() -> JsonStore[ServiceState]:
    assert _service_store is not None
    return _service_store


def get_deployment_store() -> JsonStore[RolloutState]:
    assert _deployment_store is not None
    return _deployment_store


def get_node_manager() -> NodeManager:
    assert _node_manager is not None
    return _node_manager


def get_orchestrator() -> ComputeOrchestrator:
    assert _orchestrator is not None
    return _orchestrator


def get_routing_table() -> RoutingTable:
    assert _routing_table is not None
    return _routing_table


def get_event_bus() -> EventBus:
    assert _event_bus is not None
    return _event_bus


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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _service_store, _deployment_store, _node_manager, _orchestrator
    global _routing_table, _proxy_server, _health_checker, _event_bus
    global _autoscaler, _secrets_manager, _storage_service, _presigned_service, _applier

    log.configure("control-plane")
    settings = config_module.settings
    settings.ensure_data_directories()
    await _seed_default_admin()

    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)

    _event_bus = EventBus()
    _service_store = JsonStore(settings.data_directory / "services.json", ServiceState)
    _deployment_store = JsonStore(settings.data_directory / "deployments.json", RolloutState)
    _node_manager = NodeManager()
    _routing_table = RoutingTable()

    secret_store: JsonStore[SecretSpec] = JsonStore(
        settings.data_directory / "secrets.json", SecretSpec
    )
    _secrets_manager = SecretsManager(secret_store=secret_store)
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

    docker_client = DockerClient()
    scheduler = BinPackScheduler()
    _orchestrator = ComputeOrchestrator(
        service_store=_service_store,
        deployment_store=_deployment_store,
        docker_client=docker_client,
        node_manager=_node_manager,
        scheduler=scheduler,
        event_bus=_event_bus,
        routing_table=_routing_table,
        secrets_manager=_secrets_manager,
        redis_client=redis_client,
    )

    balancer = RoundRobinBalancer(_routing_table)
    _proxy_server = ProxyServer(
        balancer=balancer, port=settings.proxy_port, redis_client=redis_client
    )
    _health_checker = HealthChecker(
        routing_table=_routing_table,
        service_store=_service_store,
        event_bus=_event_bus,
    )

    metrics_collector = MetricsCollector(redis_client=redis_client)
    _autoscaler = Autoscaler(
        service_store=_service_store,
        metrics_collector=metrics_collector,
        event_bus=_event_bus,
    )

    await _orchestrator.start()
    await _health_checker.start()
    await _proxy_server.start()
    await _autoscaler.start()

    yield

    await _autoscaler.stop()
    await _proxy_server.stop()
    await _health_checker.stop()
    await _orchestrator.stop()
    await redis_client.aclose()


async def _seed_default_admin() -> None:
    user_store = get_user_store()
    settings = config_module.settings
    if not await user_store.exists(settings.default_admin_username):
        admin = make_default_admin()
        await user_store.put(admin.username, admin)


def create_app(lifespan_override: object = None) -> FastAPI:
    chosen_lifespan = lifespan_override if lifespan_override is not None else lifespan
    app = FastAPI(title="AWS Light", version="0.1.0", lifespan=chosen_lifespan)
    app.include_router(iam_router.router)
    app.include_router(services_router.router)
    app.include_router(nodes_router.router)
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
