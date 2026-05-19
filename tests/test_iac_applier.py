from __future__ import annotations

from pathlib import Path

import pytest

from aws_light.dashboard.event_bus import EventBus
from aws_light.iac.applier import Applier
from aws_light.iac.differ import Differ
from aws_light.iac.parser import parse_manifests
from aws_light.models.database import DatabaseState
from aws_light.models.secret import SecretSpec
from aws_light.models.service import ServiceState
from aws_light.secrets.secrets_manager import SecretsManager
from aws_light.storage.storage_service import StorageService
from aws_light.store.json_store import JsonStore


def _applier(tmp_path: Path) -> Applier:
    secret_store: JsonStore[SecretSpec] = JsonStore(tmp_path / "secrets.json", SecretSpec)
    return Applier(
        service_store=JsonStore(tmp_path / "services.json", ServiceState),
        database_store=JsonStore(tmp_path / "databases.json", DatabaseState),
        secrets_manager=SecretsManager(secret_store),
        storage_service=StorageService(tmp_path / "storage"),
        differ=Differ(),
        event_bus=EventBus(),
    )


@pytest.mark.anyio
async def test_apply_service_rejects_missing_bucket_binding(tmp_path: Path) -> None:
    manifests = parse_manifests(
        """\
apiVersion: aws-light/v1
kind: Service
metadata:
  name: storage-service
spec:
  image: storage-service:latest
  resources:
    buckets:
      - name: demo-objects
        access: [read, write]
"""
    )

    result = await _applier(tmp_path).apply(manifests)

    assert result[0].kind == "Service"
    assert result[0].name == "storage-service"
    assert result[0].action == "error"
    assert "Missing bucket resource: demo-objects" in result[0].detail


@pytest.mark.anyio
async def test_apply_service_allows_bucket_created_in_same_apply_set(tmp_path: Path) -> None:
    manifests = parse_manifests(
        """\
apiVersion: aws-light/v1
kind: Bucket
metadata:
  name: demo-objects
spec:
  versioning: false
---
apiVersion: aws-light/v1
kind: Service
metadata:
  name: storage-service
spec:
  image: storage-service:latest
  resources:
    buckets:
      - name: demo-objects
        access: [read, write]
  ingress:
    external: true
"""
    )
    applier = _applier(tmp_path)

    result = await applier.apply(manifests)
    service = await applier._service_store.get("storage-service")

    assert [(item.kind, item.name, item.action) for item in result] == [
        ("Bucket", "demo-objects", "created"),
        ("Service", "storage-service", "created"),
    ]
    assert service is not None
    assert service.spec.resources.buckets[0].name == "demo-objects"
    assert service.spec.resources.buckets[0].access == ["read", "write"]
    assert service.spec.ingress.external is True


@pytest.mark.anyio
async def test_apply_database_and_bound_service(tmp_path: Path) -> None:
    manifests = parse_manifests(
        """\
apiVersion: aws-light/v1
kind: Database
metadata:
  name: app-db
spec:
  engine: postgres
  version: "16"
  storageMb: 512
---
apiVersion: aws-light/v1
kind: Service
metadata:
  name: api
spec:
  image: api:latest
  resources:
    databases:
      - name: app-db
        access: [connect]
"""
    )
    applier = _applier(tmp_path)

    result = await applier.apply(manifests)
    database = await applier._database_store.get("app-db")
    service = await applier._service_store.get("api")

    assert [(item.kind, item.name, item.action) for item in result] == [
        ("Database", "app-db", "created"),
        ("Service", "api", "created"),
    ]
    assert database is not None
    assert database.spec.engine == "postgres"
    assert service is not None
    assert service.spec.resources.databases[0].name == "app-db"


@pytest.mark.anyio
async def test_apply_service_rejects_unknown_internal_ingress_caller(
    tmp_path: Path,
) -> None:
    manifests = parse_manifests(
        """\
apiVersion: aws-light/v1
kind: Service
metadata:
  name: backend
spec:
  image: backend:latest
  ingress:
    internal:
      allowFrom:
        - frontend
"""
    )

    result = await _applier(tmp_path).apply(manifests)

    assert result[0].kind == "Service"
    assert result[0].name == "backend"
    assert result[0].action == "error"
    assert "Unknown internal ingress caller: frontend" in result[0].detail


@pytest.mark.anyio
async def test_apply_service_allows_internal_ingress_caller_in_same_apply_set(
    tmp_path: Path,
) -> None:
    manifests = parse_manifests(
        """\
apiVersion: aws-light/v1
kind: Service
metadata:
  name: frontend
spec:
  image: frontend:latest
---
apiVersion: aws-light/v1
kind: Service
metadata:
  name: backend
spec:
  image: backend:latest
  ingress:
    internal:
      allowFrom:
        - frontend
"""
    )

    result = await _applier(tmp_path).apply(manifests)

    assert [(item.kind, item.name, item.action) for item in result] == [
        ("Service", "frontend", "created"),
        ("Service", "backend", "created"),
    ]


@pytest.mark.anyio
async def test_apply_service_allows_existing_internal_ingress_caller(
    tmp_path: Path,
) -> None:
    applier = _applier(tmp_path)
    await applier.apply(
        parse_manifests(
            """\
apiVersion: aws-light/v1
kind: Service
metadata:
  name: frontend
spec:
  image: frontend:latest
"""
        )
    )

    result = await applier.apply(
        parse_manifests(
            """\
apiVersion: aws-light/v1
kind: Service
metadata:
  name: backend
spec:
  image: backend:latest
  ingress:
    internal:
      allowFrom:
        - frontend
"""
        )
    )

    assert [(item.kind, item.name, item.action) for item in result] == [
        ("Service", "backend", "created")
    ]
