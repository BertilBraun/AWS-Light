from __future__ import annotations

import importlib.util
from pathlib import Path

from aws_light.iac.parser import parse_manifests
from aws_light.models.manifest import ServiceManifest

ROOT = Path(__file__).parents[1]


def _load_storage_service():  # type: ignore[no-untyped-def]
    module_path = ROOT / "examples" / "storage-service" / "main.py"
    spec = importlib.util.spec_from_file_location("storage_service_example", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_storage_service_manifest_binds_demo_bucket() -> None:
    manifests = parse_manifests((ROOT / "examples" / "storage-service.yaml").read_text())
    service = next(manifest for manifest in manifests if isinstance(manifest, ServiceManifest))

    assert "STORE_ROOT" not in service.spec.env
    assert service.spec.resources.buckets[0].name == "demo-objects"
    assert service.spec.resources.buckets[0].access == ["read", "write"]
    assert service.spec.ingress.external is True


def test_storage_service_builds_platform_storage_request(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    storage_service = _load_storage_service()
    monkeypatch.setenv("AWS_LIGHT_STORAGE_URL", "http://proxy:8080/_aws-light/storage")
    monkeypatch.setenv("AWS_LIGHT_SERVICE_TOKEN", "storage-token")

    assert (
        storage_service.object_url("hello.txt")
        == "http://proxy:8080/_aws-light/storage/buckets/demo-objects/objects/hello.txt"
    )
    assert storage_service.storage_headers("text/plain") == {
        "X-AWS-Light-Service-Token": "storage-token",
        "content-type": "text/plain",
    }
