from __future__ import annotations

from cryptography.fernet import Fernet

from aws_light.models.secret import SecretSpec
from aws_light.store.base import AnyStore


class SecretsManager:
    def __init__(self, secret_store: AnyStore[SecretSpec]) -> None:
        self._secret_store = secret_store
        self._fernet = _load_or_create_fernet()

    async def create_secret(self, name: str, value: str) -> None:
        encrypted_value = self._fernet.encrypt(value.encode()).decode()
        await self._secret_store.put(name, SecretSpec(name=name, value=encrypted_value))

    async def get_secret(self, name: str) -> str | None:
        secret_spec = await self._secret_store.get(name)
        if secret_spec is None:
            return None
        return self._fernet.decrypt(secret_spec.value.encode()).decode()

    async def delete_secret(self, name: str) -> None:
        await self._secret_store.delete(name)

    async def list_secret_names(self) -> list[str]:
        all_secrets = await self._secret_store.list()
        return [secret.name for secret in all_secrets]

    async def exists(self, name: str) -> bool:
        return await self._secret_store.exists(name)

    async def inject_into_env(self, secret_refs: list[str]) -> dict[str, str]:
        env_vars: dict[str, str] = {}
        for secret_name in secret_refs:
            value = await self.get_secret(secret_name)
            if value is not None:
                env_key = secret_name.upper().replace("-", "_")
                env_vars[env_key] = value
        return env_vars


def _load_or_create_fernet() -> Fernet:
    import aws_light.config as config_module

    encryption_key = config_module.settings.encryption_key
    if encryption_key:
        return Fernet(encryption_key.encode())
    return Fernet(Fernet.generate_key())
