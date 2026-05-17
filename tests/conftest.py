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
from aws_light.iam.auth import make_default_admin
from aws_light.models.iam import UserSpec
from aws_light.store.json_store import JsonStore

_CONTROL_PLANE_MAIN = Path(__file__).parents[1] / "services" / "control-plane" / "main.py"
_CONTROL_PLANE_SPEC = importlib.util.spec_from_file_location(
    "aws_light_test_control_plane_main", _CONTROL_PLANE_MAIN
)
assert _CONTROL_PLANE_SPEC is not None and _CONTROL_PLANE_SPEC.loader is not None
_CONTROL_PLANE_MODULE = importlib.util.module_from_spec(_CONTROL_PLANE_SPEC)
_CONTROL_PLANE_SPEC.loader.exec_module(_CONTROL_PLANE_MODULE)
create_app = _CONTROL_PLANE_MODULE.create_app


@asynccontextmanager
async def _minimal_lifespan(app: FastAPI) -> AsyncIterator[None]:
    config_module.settings.ensure_data_directories()
    data_dir = config_module.settings.data_directory
    user_store: JsonStore[UserSpec] = JsonStore(data_dir / "users.json", UserSpec)
    deps._user_store = user_store
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
