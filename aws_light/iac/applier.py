from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from aws_light.dashboard.event_bus import EventBus
from aws_light.iac.differ import Differ, ManifestDiff
from aws_light.models.events import EventKind, WebSocketEvent
from aws_light.models.manifest import (
    AnyManifest,
    BucketManifest,
    SecretManifest,
    ServiceManifest,
)
from aws_light.models.service import ServiceSpec, ServiceState
from aws_light.secrets.secrets_manager import SecretsManager
from aws_light.storage.storage_service import StorageService
from aws_light.store.json_store import JsonStore


@dataclass
class ApplyResult:
    kind: str
    name: str
    action: Literal["created", "updated", "unchanged", "error"]
    detail: str = ""


class Applier:
    def __init__(
        self,
        service_store: JsonStore[ServiceState],
        secrets_manager: SecretsManager,
        storage_service: StorageService,
        differ: Differ,
        event_bus: EventBus | None = None,
    ) -> None:
        self._service_store = service_store
        self._secrets_manager = secrets_manager
        self._storage_service = storage_service
        self._differ = differ
        self._event_bus = event_bus

    async def apply(self, manifests: list[AnyManifest]) -> list[ApplyResult]:
        results = []
        for manifest in manifests:
            result = await self._apply_one(manifest)
            results.append(result)
        return results

    async def destroy(self, manifests: list[AnyManifest]) -> list[ApplyResult]:
        results = []
        for manifest in manifests:
            result = await self._destroy_one(manifest)
            results.append(result)
        return results

    async def diff(self, manifests: list[AnyManifest]) -> list[ManifestDiff]:
        diffs = []
        for manifest in manifests:
            current = await self._fetch_current(manifest)
            diffs.append(self._differ.compute_diff(manifest, current))
        return diffs

    async def _apply_one(self, manifest: AnyManifest) -> ApplyResult:
        kind = manifest.kind.value
        name = manifest.metadata.name
        try:
            match manifest:
                case ServiceManifest():
                    return await self._apply_service(manifest)
                case SecretManifest():
                    return await self._apply_secret(manifest)
                case BucketManifest():
                    return await self._apply_bucket(manifest)
                case _:
                    return ApplyResult(kind=kind, name=name, action="error", detail="Unknown kind")
        except Exception as error:
            return ApplyResult(kind=kind, name=name, action="error", detail=str(error))

    async def _apply_service(self, manifest: ServiceManifest) -> ApplyResult:
        name = manifest.metadata.name
        spec_data = manifest.spec
        service_spec = ServiceSpec(
            name=name,
            image=spec_data.image,
            replicas=spec_data.replicas,
            min_replicas=spec_data.min_replicas,
            max_replicas=spec_data.max_replicas,
            cpu_request=spec_data.cpu_request,
            memory_request_mb=spec_data.memory_request_mb,
            port=spec_data.port,
            health_check_path=spec_data.health_check_path,
            env=spec_data.env,
            secret_refs=spec_data.secret_refs,
            labels={**manifest.metadata.labels, **spec_data.labels},
        )

        existing = await self._service_store.get(name)
        if existing is None:
            from aws_light.models.common import ResourceStatus

            service_state = ServiceState(spec=service_spec, status=ResourceStatus.PENDING)
            await self._service_store.put(name, service_state)
            return ApplyResult(kind="Service", name=name, action="created")
        else:
            existing.spec = service_spec
            from datetime import datetime

            existing.updated_at = datetime.utcnow()
            await self._service_store.put(name, existing)
            return ApplyResult(kind="Service", name=name, action="updated")

    async def _apply_secret(self, manifest: SecretManifest) -> ApplyResult:
        name = manifest.metadata.name
        if await self._secrets_manager.exists(name):
            await self._secrets_manager.delete_secret(name)
            await self._secrets_manager.create_secret(name, manifest.spec.value)
            return ApplyResult(kind="Secret", name=name, action="updated")
        await self._secrets_manager.create_secret(name, manifest.spec.value)
        await self._emit(EventKind.SECRET_CREATED, {"secret_name": name})
        return ApplyResult(kind="Secret", name=name, action="created")

    async def _apply_bucket(self, manifest: BucketManifest) -> ApplyResult:
        name = manifest.metadata.name
        if self._storage_service.bucket_exists(name):
            return ApplyResult(kind="Bucket", name=name, action="unchanged")
        self._storage_service.create_bucket(name)
        await self._emit(EventKind.BUCKET_CREATED, {"bucket_name": name})
        return ApplyResult(kind="Bucket", name=name, action="created")

    async def _destroy_one(self, manifest: AnyManifest) -> ApplyResult:
        kind = manifest.kind.value
        name = manifest.metadata.name
        try:
            match manifest:
                case ServiceManifest() if await self._service_store.exists(name):
                    await self._service_store.delete(name)
                    return ApplyResult(kind=kind, name=name, action="updated", detail="deleted")
                case SecretManifest() if await self._secrets_manager.exists(name):
                    await self._secrets_manager.delete_secret(name)
                    return ApplyResult(kind=kind, name=name, action="updated", detail="deleted")
                case BucketManifest() if self._storage_service.bucket_exists(name):
                    self._storage_service.delete_bucket(name)
                    return ApplyResult(kind=kind, name=name, action="updated", detail="deleted")
                case _:
                    return ApplyResult(kind=kind, name=name, action="unchanged", detail="not found")
        except Exception as error:
            return ApplyResult(kind=kind, name=name, action="error", detail=str(error))

    async def _fetch_current(self, manifest: AnyManifest) -> AnyManifest | None:
        return None

    async def _emit(self, kind: EventKind, payload: dict) -> None:
        if self._event_bus is not None:
            await self._event_bus.publish(WebSocketEvent(kind=kind, payload=payload))
