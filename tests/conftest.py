from __future__ import annotations

import importlib.util
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import aws_light.config as config_module
import aws_light.dependencies as deps
from aws_light.dashboard.event_bus import EventBus
from aws_light.iam.auth import make_default_admin
from aws_light.models.deployment import RolloutState
from aws_light.models.iam import UserSpec
from aws_light.models.node import NodeState
from aws_light.models.secret import SecretSpec
from aws_light.models.service import ServiceState
from aws_light.secrets.secrets_manager import SecretsManager
from aws_light.storage.presigned import PresignedUrlService
from aws_light.storage.storage_service import StorageService
from aws_light.store.json_store import JsonStore

_CONTROL_PLANE_MAIN = Path(__file__).parents[1] / "services" / "control-plane" / "main.py"
_CONTROL_PLANE_SPEC = importlib.util.spec_from_file_location(
    "aws_light_test_control_plane_main", _CONTROL_PLANE_MAIN
)
assert _CONTROL_PLANE_SPEC is not None and _CONTROL_PLANE_SPEC.loader is not None
_CONTROL_PLANE_MODULE = importlib.util.module_from_spec(_CONTROL_PLANE_SPEC)
_CONTROL_PLANE_SPEC.loader.exec_module(_CONTROL_PLANE_MODULE)
create_app = _CONTROL_PLANE_MODULE.create_app


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def hgetall(self, key: str) -> dict[str, str]:
        return self.hashes.get(key, {})


@asynccontextmanager
async def _minimal_lifespan(app: FastAPI) -> AsyncIterator[None]:
    config_module.settings.ensure_data_directories()
    data_dir = config_module.settings.data_directory
    user_store: JsonStore[UserSpec] = JsonStore(data_dir / "users.json", UserSpec)
    secret_store: JsonStore[SecretSpec] = JsonStore(data_dir / "secrets.json", SecretSpec)
    deps._user_store = user_store
    deps._service_store = JsonStore(data_dir / "services.json", ServiceState)
    deps._deployment_store = JsonStore(data_dir / "deployments.json", RolloutState)
    deps._node_store = JsonStore(data_dir / "nodes.json", NodeState)
    deps._secrets_manager = SecretsManager(secret_store)
    deps._storage_service = StorageService(data_dir / "storage")
    deps._presigned_service = PresignedUrlService(
        secret_key=config_module.settings.jwt_secret,
        base_url="http://testserver",
    )
    deps._event_bus = EventBus()
    deps._redis_client = FakeRedis()
    admin_username = config_module.settings.default_admin_username
    if not await user_store.exists(admin_username):
        admin = make_default_admin()
        await user_store.put(admin.username, admin)
    yield


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(config_module, "settings", config_module.Settings(data_directory=tmp_path))
    monkeypatch.setattr(deps, "_user_store", None)

    app = create_app(lifespan_override=_minimal_lifespan)
    with TestClient(app) as test_client:
        yield test_client
