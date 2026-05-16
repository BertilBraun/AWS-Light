from __future__ import annotations

from pathlib import Path

import pytest

from aws_light.models.secret import SecretSpec
from aws_light.secrets.secrets_manager import SecretsManager
from aws_light.store.json_store import JsonStore


@pytest.fixture()
def secrets_manager(tmp_path: Path) -> SecretsManager:
    store: JsonStore[SecretSpec] = JsonStore(tmp_path / "secrets.json", SecretSpec)
    return SecretsManager(secret_store=store)


async def test_create_and_retrieve_secret(secrets_manager: SecretsManager) -> None:
    await secrets_manager.create_secret("db-password", "supersecret")
    value = await secrets_manager.get_secret("db-password")
    assert value == "supersecret"


async def test_secret_is_stored_encrypted(secrets_manager: SecretsManager, tmp_path: Path) -> None:
    await secrets_manager.create_secret("api-key", "plaintext-value")
    raw_text = (tmp_path / "secrets.json").read_text()
    assert "plaintext-value" not in raw_text


async def test_delete_secret_removes_it(secrets_manager: SecretsManager) -> None:
    await secrets_manager.create_secret("temp", "value")
    await secrets_manager.delete_secret("temp")
    assert await secrets_manager.get_secret("temp") is None


async def test_list_secret_names_returns_names(secrets_manager: SecretsManager) -> None:
    await secrets_manager.create_secret("alpha", "1")
    await secrets_manager.create_secret("beta", "2")
    names = await secrets_manager.list_secret_names()
    assert set(names) == {"alpha", "beta"}


async def test_inject_into_env_returns_decrypted_values(secrets_manager: SecretsManager) -> None:
    await secrets_manager.create_secret("my-secret", "injected-value")
    env = await secrets_manager.inject_into_env(["my-secret"])
    assert env["MY_SECRET"] == "injected-value"


async def test_inject_skips_missing_secrets(secrets_manager: SecretsManager) -> None:
    env = await secrets_manager.inject_into_env(["nonexistent"])
    assert "NONEXISTENT" not in env
