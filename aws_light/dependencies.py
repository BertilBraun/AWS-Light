from __future__ import annotations

from typing import Any

from aws_light.iac.applier import Applier
from aws_light.proxy.redis_routing_table import RedisRoutingTable
from aws_light.secrets.secrets_manager import SecretsManager
from aws_light.storage.presigned import PresignedUrlService
from aws_light.storage.storage_service import StorageService

# All module-level state is registered here by the control-plane runtime entrypoint.
# API route modules always import getters from here, never from a specific service main.

_service_store: Any = None
_deployment_store: Any = None
_node_store: Any = None
_user_store: Any = None
_secrets_manager: SecretsManager | None = None
_storage_service: StorageService | None = None
_presigned_service: PresignedUrlService | None = None
_applier: Applier | None = None
_event_bus: Any = None
_redis_client: Any = None


def get_service_store() -> Any:
    assert _service_store is not None
    return _service_store


def get_deployment_store() -> Any:
    assert _deployment_store is not None
    return _deployment_store


def get_node_store() -> Any:
    assert _node_store is not None
    return _node_store


def get_user_store() -> Any:
    assert _user_store is not None
    return _user_store


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


def get_event_bus() -> Any:
    assert _event_bus is not None
    return _event_bus


def get_redis_client() -> Any:
    assert _redis_client is not None
    return _redis_client


def get_routing_table() -> RedisRoutingTable:
    return RedisRoutingTable(get_redis_client())
