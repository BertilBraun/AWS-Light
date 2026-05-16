from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import aws_light.config as config_module
import aws_light.dependencies as dependencies_module
from aws_light.iam.auth import make_default_admin
from aws_light.main import create_app


@asynccontextmanager
async def _minimal_lifespan(app: FastAPI) -> AsyncIterator[None]:
    config_module.settings.ensure_data_directories()
    user_store = dependencies_module.get_user_store()
    admin_username = config_module.settings.default_admin_username
    if not await user_store.exists(admin_username):
        admin = make_default_admin()
        await user_store.put(admin.username, admin)
    yield


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(config_module, "settings", config_module.Settings(data_directory=tmp_path))
    monkeypatch.setattr(dependencies_module, "_user_store", None)

    app = create_app(lifespan_override=_minimal_lifespan)
    with TestClient(app) as test_client:
        yield test_client
