from __future__ import annotations

import importlib.util
from pathlib import Path

from aws_light.iac.parser import parse_manifests
from aws_light.models.manifest import DatabaseManifest, ServiceManifest

ROOT = Path(__file__).parents[1]


def _load_database_service():  # type: ignore[no-untyped-def]
    module_path = ROOT / "examples" / "database-service" / "main.py"
    spec = importlib.util.spec_from_file_location("database_service_example", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_database_service_manifest_declares_database_binding() -> None:
    manifests = parse_manifests((ROOT / "examples" / "database-service.yaml").read_text())
    database = next(manifest for manifest in manifests if isinstance(manifest, DatabaseManifest))
    service = next(manifest for manifest in manifests if isinstance(manifest, ServiceManifest))

    assert database.metadata.name == "app-db"
    assert database.spec.engine == "postgres"
    assert database.spec.version == "16"
    assert database.spec.storage_mb == 512
    assert service.metadata.name == "database-service"
    assert service.spec.resources.databases[0].name == "app-db"
    assert service.spec.resources.databases[0].access == ["connect"]
    assert service.spec.ingress.external is True


def test_database_service_reads_injected_database_environment(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database_service = _load_database_service()
    monkeypatch.setenv("AWS_LIGHT_DATABASE_APP_DB_HOST", "aws-light-db-app-db")
    monkeypatch.setenv("AWS_LIGHT_DATABASE_APP_DB_PORT", "5432")
    monkeypatch.setenv("AWS_LIGHT_DATABASE_APP_DB_NAME", "app_db")
    monkeypatch.setenv("AWS_LIGHT_DATABASE_APP_DB_USER", "app_db_user")
    monkeypatch.setenv("AWS_LIGHT_DATABASE_APP_DB_PASSWORD", "secret")
    monkeypatch.setenv(
        "AWS_LIGHT_DATABASE_APP_DB_URL",
        "postgresql://app_db_user:secret@aws-light-db-app-db:5432/app_db",
    )

    assert database_service.database_settings("app-db") == {
        "host": "aws-light-db-app-db",
        "port": 5432,
        "database": "app_db",
        "user": "app_db_user",
        "password": "secret",
        "url": "postgresql://app_db_user:secret@aws-light-db-app-db:5432/app_db",
    }
